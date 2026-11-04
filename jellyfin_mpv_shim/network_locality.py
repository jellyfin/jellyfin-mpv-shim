"""Is a Jellyfin server on our own network? Decided without any third party.

JellyHome Player uses the answer for exactly one thing: **bandwidth** -- whether
to lift the remote bitrate cap and whether to warn about a remote transcode.
It is never an authentication, TLS or filesystem decision.

Two separate answers come out of one lookup, on purpose:

``local``
    Bandwidth locality. True if the user chose *Local*; False if they chose
    *Remote*; for *Auto* (the default), true when the server's name resolves to
    a private/loopback/link-local address, or -- when it resolves to a public
    one (hairpin NAT, a public IPv6 prefix, split-horizon DNS that does not
    apply here) -- when the Jellyfin server itself says the request came from
    inside its configured local networks (``/System/Endpoint`` ->
    ``IsInNetwork``).

``direct_path_trusted``
    May a server-supplied media path be opened directly from this machine's
    filesystem? True only if **every** address the name resolves to is private
    *and* the user did not pick *Remote*. The *Local* override and the server's
    ``IsInNetwork`` claim never grant this: a user saying "treat it as local for
    bandwidth" has not said "let the server name files I will open", and a
    reverse proxy can make ``IsInNetwork`` true for the whole internet.

Anything that cannot be established (no DNS, no endpoint answer, a malformed
URL, an exception) resolves to *Remote* / untrusted. Failing open here would
turn an unreachable server into a trusted one.

No request is made to anything but the Jellyfin server the user configured.
"""

import ipaddress
import logging
import socket
import threading
import time
import urllib.parse
from typing import Callable, NamedTuple, Optional

log = logging.getLogger("network_locality")

AUTO = "auto"
LOCAL = "local"
REMOTE = "remote"
CONNECTION_TYPES = (AUTO, LOCAL, REMOTE)

#: How long an Auto answer is reused. A laptop that changes network gets a
#: new connect (and so a new address resolution) anyway; this only keeps
#: every queue item from repeating a DNS lookup and an API call.
CACHE_SECONDS = 300.0
#: A failure is retried sooner than a success is re-confirmed.
FAILURE_CACHE_SECONDS = 20.0


def normalize_connection_type(value) -> str:
    """``auto``/``local``/``remote``; anything else (hand-edited, newer
    build) is ``auto`` rather than an error."""
    value = str(value or "").strip().lower()
    return value if value in CONNECTION_TYPES else AUTO


class Locality(NamedTuple):
    local: bool
    direct_path_trusted: bool
    #: Short machine-readable reason, for logs and tests.
    reason: str


REMOTE_UNKNOWN = Locality(False, False, "unknown")


def is_lan_address(ip) -> bool:
    """Private, loopback, link-local or unique-local. Not CGNAT (100.64/10,
    which Tailscale and carrier NAT use), which is treated as remote."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local) \
        and not _is_cgnat(ip)


#: Name suffixes only a LAN resolver answers.
LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain",
                ".home.arpa")


def is_lan_hostname(host) -> bool:
    """Whether a *typed* host looks like a home-network server: a private IP
    literal, ``localhost``, a dotless name, or a LAN-only suffix. Syntactic
    (no lookup), used to choose a default scheme -- never to grant trust."""
    host = str(host or "").strip("[]").rstrip(".").lower()
    if not host:
        return False
    try:
        return is_lan_address(ipaddress.ip_address(host))
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith(LAN_SUFFIXES)


def _is_cgnat(ip) -> bool:
    return isinstance(ip, ipaddress.IPv4Address) and \
        ip in ipaddress.ip_network("100.64.0.0/10")


def _resolve(host, resolver) -> Optional[list]:
    """Every address ``host`` resolves to, or None when it cannot be resolved.

    ``getaddrinfo`` takes no timeout; callers must not hold a lock a UI
    thread needs while calling this.
    """
    try:
        literal = ipaddress.ip_address(host)
        return [literal]
    except ValueError:
        pass
    try:
        infos = resolver(host, None)
    except Exception:
        log.debug("Could not resolve %s", host, exc_info=True)
        return None
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except (ValueError, IndexError):
            continue
    return found or None


def detect(address: str,
           ask_server: Optional[Callable[[], Optional[bool]]] = None,
           resolver=None) -> Locality:
    """The *Auto* answer for a server address. See the module docstring.

    ``ask_server`` returns the server's ``IsInNetwork`` or None; it is only
    called when the name resolves to a public address, and any exception from
    it is swallowed (-> remote).
    """
    resolver = resolver or socket.getaddrinfo
    try:
        host = urllib.parse.urlsplit(str(address or "")).hostname
    except ValueError:
        return REMOTE_UNKNOWN
    if not host:
        return REMOTE_UNKNOWN
    ips = _resolve(host, resolver)
    if not ips:
        return REMOTE_UNKNOWN
    lan = [is_lan_address(ip) for ip in ips]
    if all(lan):
        return Locality(True, True, "private-address")
    if any(lan):
        # A dual-stack name with one LAN and one public record: the
        # connection may have used either. Good enough for bandwidth, not
        # for letting the server name files to open.
        return Locality(True, False, "mixed-address")
    if ask_server is not None:
        try:
            if ask_server() is True:
                return Locality(True, False, "server-reports-in-network")
        except Exception:
            log.debug("Could not read the server's network information.",
                      exc_info=True)
    return Locality(False, False, "public-address")


def apply_override(connection_type, auto: Locality) -> Locality:
    """Combine the user's choice with the Auto answer."""
    connection_type = normalize_connection_type(connection_type)
    if connection_type == LOCAL:
        # Bandwidth only. Trust is whatever Auto independently established.
        return Locality(True, auto.direct_path_trusted, "user-local")
    if connection_type == REMOTE:
        return Locality(False, False, "user-remote")
    return auto


class LocalityCache:
    """Memoises Auto answers per ``(key, address)``. Thread-safe."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, key, address, compute: Callable[[], Locality]) -> Locality:
        now = self._clock()
        with self._lock:
            hit = self._entries.get((key, address))
            if hit is not None and hit[0] > now:
                return hit[1]
        value = compute()  # outside the lock: this can block on DNS/HTTP
        ttl = CACHE_SECONDS if value.reason != "unknown" and \
            value.reason != "public-address" else FAILURE_CACHE_SECONDS
        with self._lock:
            self._entries[(key, address)] = (self._clock() + ttl, value)
        return value

    def forget(self, key=None):
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                for k in [k for k in self._entries if k[0] == key]:
                    del self._entries[k]


_cache = LocalityCache()


def for_client(client, connection_type=AUTO) -> Locality:
    """Locality for a live ``JellyfinClient``. Never raises."""
    try:
        address = client.config.data.get("auth.server", "")
    except Exception:
        return REMOTE_UNKNOWN

    def ask():
        info = client.jellyfin.get_endpoint_info()
        if isinstance(info, dict) and "IsInNetwork" in info:
            return bool(info["IsInNetwork"])
        return None

    try:
        auto = _cache.get(id(client), address, lambda: detect(address, ask))
    except Exception:
        log.warning("Locality detection failed; treating the server as "
                    "remote.", exc_info=True)
        auto = REMOTE_UNKNOWN
    return apply_override(connection_type, auto)


def forget(client=None):
    _cache.forget(id(client) if client is not None else None)
