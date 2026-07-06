from jellyfin_apiclient_python import JellyfinClient
from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE
from .conf import settings
from . import conffile
from getpass import getpass
from .constants import CAPABILITIES, CLIENT_VERSION, USER_APP_NAME, USER_AGENT, APP_NAME
from .i18n import _

import os.path
import json
import uuid
import time
import logging
import re
import threading

import socket
import ipaddress
from urllib.parse import urlparse


# Get all local IPv4 addresses for host machine
def get_local_ips():
    local_ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ips.append(ipaddress.ip_address(s.getsockname()[0]))
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ipaddress.ip_address(info[4][0])
            if ip not in local_ips and ip.is_private:
                local_ips.append(ip)
    except socket.gaierror:
        pass

    return local_ips


# Extract hostname/IP from a URL.
def extract_host(url):
    parsed = urlparse(url)
    return parsed.hostname  # Handles port stripping automatically


# Check if string is valid IPv4 address
def is_ipv4(host):
    try:
        ipaddress.IPv4Address(host)
        return True
    except ipaddress.AddressValueError:
        return False


# Check if host IP is on the same subnet as any local IP
def is_local_subnet(host, local_ips, prefix_length=24):
    try:
        target_ip = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False

    if not target_ip.is_private:
        return False

    for local_ip in local_ips:
        net = ipaddress.ip_network(f"{local_ip}/{prefix_length}", strict=False)
        if target_ip in net:
            return True
    return False


log = logging.getLogger("clients")
path_regex = re.compile(r"^(https?://)?(?:(\[[^/]+\])|([^/:]+))(:[0-9]+)?(/.*)?$")

# How often to poll the server while waiting for the user to authorize a
# Quick Connect request, and how long to keep polling before giving up.
QUICK_CONNECT_POLL_SECS = 3
QUICK_CONNECT_TIMEOUT_SECS = 300


class QuickConnectError(Exception):
    """Raised when a Quick Connect login cannot be completed.

    The message is user-facing (already translated) so callers can surface it
    directly in the CLI or GUI.
    """

from typing import Optional


# Keys written onto the server credential dicts at runtime to track live
# connection state. They are only meaningful for the current session, so they
# must be stripped before persisting to cred.json — a stale "connected: true"
# from a previous run is misleading. Loading tolerates their presence.
VOLATILE_CREDENTIAL_KEYS = frozenset({"connected"})


def clean_credentials_for_save(credentials):
    """Return a copy of the credentials list with volatile runtime keys removed.

    Copies each server dict so the live dicts (which other threads read) are
    left untouched.
    """
    cleaned = []
    for server in credentials:
        cleaned.append(
            {k: v for k, v in server.items() if k not in VOLATILE_CREDENTIAL_KEYS}
        )
    return cleaned


def expo(max_value: Optional[int] = None):
    n = 0
    while True:
        a = 2**n
        if max_value is None or a < max_value:
            yield a
            n += 1
        else:
            yield max_value


class PeriodicHealthCheck(threading.Thread):
    def __init__(self, callback):
        self.halt = False
        self.trigger = threading.Event()
        self.callback = callback

        threading.Thread.__init__(self, daemon=True)

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join()

    def run(self):
        while not self.halt:
            if not self.trigger.wait(settings.health_check_interval):
                try:
                    self.callback()
                except Exception:
                    log.exception("Client health check failed.")


class ClientManager(object):
    def __init__(self):
        self.callback = lambda client, event_name, data: None
        # Fired when a server's status changes outside a direct user action —
        # e.g. the background cast-session verifier confirms or gives up — so
        # the UI can refresh the servers list. Set by the GUI.
        self.on_servers_changed = lambda: None
        self.credentials = []
        self.clients = {}
        self.usernames = {}
        # Guards registry state (clients dict, the _connecting reservations).
        # Never held across network I/O — see connect_client.
        self._client_lock = threading.RLock()
        # Server uuids with a connect in flight, so concurrent connectors
        # (health check vs websocket reconnect) don't build duplicates.
        self._connecting = set()
        # Set on stop(); lets reconnect/retry sleeps end immediately instead
        # of holding shutdown hostage for up to their full backoff interval.
        self._stop_event = threading.Event()

        self.health_check = None
        if settings.health_check_interval is not None:
            self.health_check = PeriodicHealthCheck(self.check_all_clients)
            self.health_check.start()

    @staticmethod
    def _get_cli_credential_args():
        from .args import get_args
        a = get_args()
        if a.server and a.username:
            return a.server, a.username, a.password
        return None

    def _find_existing_credential(self, server: str, username: str):
        """Find an existing credential matching the given server and username."""
        if server.endswith("/"):
            server = server[:-1]
        for cred in self.credentials:
            cred_address = cred.get("address", "").rstrip("/")
            cred_username = cred.get("username", "")
            if cred_address == server and cred_username == username:
                return cred
        return None

    def _update_account(self, server: str, username: str, password: str):
        """Update an existing account by re-authenticating with new credentials."""
        existing = self._find_existing_credential(server, username)
        if existing is None:
            return False

        # Disconnect the old client
        self._disconnect_client(uuid=existing["uuid"])
        # Remove the old credential
        self.credentials = [
            c for c in self.credentials if c["uuid"] != existing["uuid"]
        ]
        self.save_credentials()

        # Re-login with updated credentials
        return self.login(server, username, password)

    def _cli_quick_connect(self, server: str):
        """Run a Quick Connect login from the terminal, printing the code."""

        def show_code(code):
            print(
                _(
                    "Open Jellyfin, go to your user menu -> Quick Connect, "
                    "and enter this code:"
                )
            )
            print("    " + code)
            print(_("Waiting for authorization..."))

        try:
            return self.login_with_quick_connect(server, code_callback=show_code)
        except QuickConnectError as e:
            log.warning(str(e))
            return False

    def cli_connect(self):
        from .args import get_args
        cli_commands = set(get_args().command or [])

        is_logged_in = self.try_connect()
        add_another = "add" in cli_commands
        clear_accounts = "clear" in cli_commands
        use_quick_connect = get_args().quick_connect
        server_arg = get_args().server

        cli_creds = self._get_cli_credential_args()

        if clear_accounts:
            log.info(_("Clearing all existing accounts."))
            self.remove_all_clients()
            is_logged_in = False

        # Non-interactive Quick Connect: only the server URL is required.
        if use_quick_connect and server_arg:
            cli_creds = None
            if not is_logged_in or add_another or clear_accounts:
                if self._cli_quick_connect(server_arg):
                    log.info(_("Successfully added server."))
                    is_logged_in = True
                else:
                    log.warning(_("Adding server failed."))

        if cli_creds:
            server, username, password = cli_creds
            if not clear_accounts:
                existing = self._find_existing_credential(server, username)
                if existing is not None:
                    log.info(_("Account already exists. Updating credentials."))
                    if self._update_account(server, username, password):
                        log.info(_("Successfully updated server credentials."))
                        is_logged_in = True
                    else:
                        log.warning(_("Updating server credentials failed."))
                    cli_creds = None

            if cli_creds and (not is_logged_in or add_another or clear_accounts):
                is_logged_in = self.login(server, username, password)
                if is_logged_in:
                    log.info(_("Successfully added server."))
                else:
                    log.warning(_("Adding server failed."))

        while not is_logged_in or add_another:
            server = input(_("Server URL: "))
            if use_quick_connect:
                is_logged_in = self._cli_quick_connect(server)
            else:
                username = input(_("Username: "))
                try:
                    password = getpass(_("Password: "))
                except (EOFError, OSError):
                    password = ""

                is_logged_in = self.login(server, username, password)

            if is_logged_in:
                log.info(_("Successfully added server."))
                add_another = input(_("Add another server?") + " [y/N] ")
                add_another = add_another in ("y", "Y", "yes", "Yes")
            else:
                log.warning(_("Adding server failed."))

    @staticmethod
    def client_factory():
        client = JellyfinClient(allow_multiple_clients=True)
        client.config.data["app.default"] = True
        client.config.app(
            USER_APP_NAME, CLIENT_VERSION, settings.player_name, settings.client_uuid
        )
        client.config.data["http.user_agent"] = USER_AGENT
        client.config.data["auth.ssl"] = not settings.ignore_ssl_cert

        if settings.tls_client_cert:
            client.config.data["auth.tls_client_cert"] = settings.tls_client_cert
            client.config.data["auth.tls_client_key"] = settings.tls_client_key
            client.config.data["auth.tls_server_ca"] = settings.tls_server_ca
            client.auth.create_session_with_client_auth()

        return client

    def _connect_all(self):
        is_logged_in = False

        local_ips = get_local_ips()

        def connection_priority(server):
            host = extract_host(server["address"])

            # Highest priority: same subnet as us
            if is_ipv4(host) and is_local_subnet(host, local_ips):
                return 0

            # Second priority: other private IPs (maybe reachable via VPN, etc.)
            if is_ipv4(host):
                try:
                    if ipaddress.IPv4Address(host).is_private:
                        return 1
                except ipaddress.AddressValueError:
                    pass

            # Lowest priority: hostnames / external addresses
            return 2

        # Sort creds list by local-first priority
        sorted_credentials = sorted(self.credentials, key=connection_priority)

        # Array to stash server Ids, to avoid double-connecting to servers
        # and avoid clobbering the preferred connection
        connected_servers = []

        for server in sorted_credentials:
            # Test if we've connected to this server already
            if server["Id"] in connected_servers:
                # If so, skip connecting
                continue
            if self.connect_client(server):
                is_logged_in = True

                # If valid connection, add Id of server to array
                connected_servers.append(server["Id"])
        return is_logged_in

    def load_credentials(self):
        """Read saved credentials from disk. Fast (no network) — call this
        before connecting so callers know which servers exist up front."""
        credentials_location = conffile.get(APP_NAME, "cred.json")
        if os.path.exists(credentials_location):
            with open(credentials_location) as cf:
                self.credentials = json.load(cf)

        if "Servers" in self.credentials:
            credentials_old = self.credentials
            self.credentials = []
            for server in credentials_old["Servers"]:
                server["uuid"] = str(uuid.uuid4())
                server["username"] = ""
                self.credentials.append(server)

    def connect_all(self):
        """Connect to all loaded credentials (call load_credentials first),
        honouring connect_retry_mins. Returns whether any server connected."""
        is_logged_in = self._connect_all()
        if settings.connect_retry_mins and not is_logged_in:
            log.warning(
                "Connection failed. Will retry for {0} minutes.".format(
                    settings.connect_retry_mins
                )
            )
            for attempt in range(settings.connect_retry_mins * 2):
                if self._stop_event.wait(30):
                    break
                is_logged_in = self._connect_all()
                if is_logged_in:
                    break

        return is_logged_in

    def try_connect(self):
        self.load_credentials()
        return self.connect_all()

    def save_credentials(self):
        credentials_location = conffile.get(APP_NAME, "cred.json")
        with open(credentials_location, "w") as cf:
            json.dump(clean_credentials_for_save(self.credentials), cf)

    @staticmethod
    def _normalize_server(server: str) -> str:
        if server.endswith("/"):
            server = server[:-1]

        protocol, ipv6_host, ipv4_host, port, path = path_regex.match(server).groups()

        if not protocol:
            log.warning("Adding http:// because it was not provided.")
            protocol = "http://"

        if protocol == "http://" and not port:
            log.warning("Adding port 8096 for insecure local http connection.")
            log.warning(
                "If you want to connect to standard http port 80, use :80 in the url."
            )
            port = ":8096"

        return "".join(filter(bool, (protocol, ipv6_host, ipv4_host, port, path)))

    def _finalize_login(
        self, client: "JellyfinClient", username: str, force_unique: bool = False
    ):
        """Stash a freshly-authenticated client into our credential store.

        The client must already hold a valid AccessToken for its first server
        (either from a password login or a Quick Connect exchange).
        """
        credentials = client.auth.credentials.get_credentials()
        server = credentials["Servers"][0]
        if force_unique:
            server["uuid"] = server["Id"]
        else:
            server["uuid"] = str(uuid.uuid4())
        server["username"] = username
        if force_unique and server["Id"] in self.clients:
            return True
        self.connect_client(server)
        self.credentials.append(server)
        self.save_credentials()
        return True

    def login(
        self, server: str, username: str, password: str, force_unique: bool = False
    ):
        server = self._normalize_server(server)

        client = self.client_factory()
        client.auth.connect_to_address(server)
        result = client.auth.login(server, username, password)
        if "AccessToken" in result:
            return self._finalize_login(client, username, force_unique)
        return False

    def quick_connect_initiate(self, server: str):
        """Start a Quick Connect request against ``server``.

        Returns ``(client, secret, code)``. The ``code`` must be shown to the
        user to enter in their (already authenticated) Jellyfin session.
        Raises QuickConnectError if the server is unreachable or has Quick
        Connect disabled.
        """
        server = self._normalize_server(server)
        client = self.client_factory()
        client.auth.connect_to_address(server)
        servers = client.auth.credentials.get_credentials().get("Servers")
        if not servers:
            raise QuickConnectError(_("Could not connect to the server."))
        address = servers[0]["address"]
        session = client.auth.session

        if not client.auth.API.quick_connect_enabled(address, session):
            raise QuickConnectError(_("Quick Connect is not enabled on this server."))

        data = client.auth.API.quick_connect_initiate(address, session)
        if not data:
            raise QuickConnectError(_("Could not start Quick Connect."))

        return client, data["Secret"], data["Code"]

    def quick_connect_wait(self, client, secret: str, should_cancel=None):
        """Poll until the Quick Connect request is authorized, then log in.

        Returns True on success, False on timeout/cancellation/failure.
        ``should_cancel`` is an optional callable polled between attempts.
        """
        address = client.auth.credentials.get_credentials()["Servers"][0]["address"]
        session = client.auth.session

        deadline = time.time() + QUICK_CONNECT_TIMEOUT_SECS
        authorized = False
        while time.time() < deadline:
            if should_cancel is not None and should_cancel():
                return False
            state = client.auth.API.quick_connect_state(address, secret, session)
            if state.get("Authenticated"):
                authorized = True
                break
            time.sleep(QUICK_CONNECT_POLL_SECS)

        if not authorized:
            log.warning("Quick Connect timed out waiting for authorization.")
            return False

        result = client.auth.login_with_quick_connect(address, secret)
        if "AccessToken" not in result:
            log.warning("Quick Connect authentication failed.")
            return False

        return self._finalize_login(client, result["User"]["Name"])

    def login_with_quick_connect(self, server: str, code_callback=None, should_cancel=None):
        """High-level Quick Connect login.

        Initiates the request, hands the user-facing code to ``code_callback``,
        then blocks polling until authorized. Raises QuickConnectError on setup
        failure; returns True/False from the wait phase.
        """
        client, secret, code = self.quick_connect_initiate(server)
        if code_callback is not None:
            code_callback(code)
        return self.quick_connect_wait(client, secret, should_cancel=should_cancel)

    def validate_client(self, client: "JellyfinClient", dry_run=False, server=None):
        # Use the apiclient's lower-level _http to bound retries and timeout
        # for this specific call. The default 30s × 5 retries can wedge the
        # health-check thread for ~2.5 minutes if the server is unresponsive.
        # On exception, fall through to the "not in client list" branch below
        # to force a reconnect (a timeout is a broken connection).
        try:
            client_list = client.jellyfin._http(
                "GET", "Sessions", {"params": None, "timeout": 10, "retry": 1}
            )
        except Exception:
            log.warning("Health check session query failed; treating as disconnected.", exc_info=True)
            client_list = []

        if client_list is None:
            log.warning(
                "Client check failed, proceeding anyways. (Client list is unset.)"
            )
            return True

        for f_client in client_list:
            if f_client.get("DeviceId") == settings.client_uuid:
                break
        else:
            if not dry_run:
                log.warning(
                    "Client is not actually connected. (It does not show in the client list.)"
                )
                # Silence the client's own callbacks so stopping it can't fire
                # the websocket reconnect loop on top of us, then drop it from
                # the registry — check_all_clients' credential pass (or the
                # next health-check tick) reconnects it cleanly.
                client.callback = lambda *_: None
                client.callback_ws = lambda *_: None
                removed = server is not None and self._disconnect_client(
                    server=server, expected_client=client
                )
                if not removed:
                    # Not (or no longer) the registered client for this
                    # server — a reconnect may have replaced it while we were
                    # probing. Stop our stale handle and leave the registry
                    # alone.
                    client.stop()
            return False

        return True

    def setup_client(self, client: "JellyfinClient", server, do_retries=True):
        def event(event_name, data):
            if event_name == "WebSocketDisconnect":
                timeout_gen = expo(100)
                if server["uuid"] in self.clients:
                    while not self.is_stopping and not settings.work_offline:
                        timeout = next(timeout_gen)
                        log.info(
                            "No connection to server. Next try in {0} second(s)".format(
                                timeout
                            )
                        )
                        self._disconnect_client(server=server)
                        # Interruptible: this runs on a non-daemon websocket
                        # thread, and an uninterruptible sleep here used to
                        # hold app exit hostage for up to the full backoff.
                        if self._stop_event.wait(timeout):
                            break
                        if self.connect_client(server, False):
                            break
            elif event_name == "WebSocketConnect":
                log.info("WebSocket connected, posting capabilities")
                # API might not be ready yet. retry a few times.
                for i in range(6):
                    if self.is_stopping:
                        break
                    try:
                        client.jellyfin.post_capabilities(CAPABILITIES)
                        break
                    except Exception:
                        if i == 5:
                            log.warning(
                                "Failed to post capabilities on connect",
                                exc_info=True,
                            )
                        else:
                            time.sleep(2)
                self.callback(client, event_name, data)
            else:
                self.callback(client, event_name, data)

        client.callback = event
        client.callback_ws = event
        client.start(websocket=True)

        # The "is this device actually connected" test (does it appear in the
        # server's Sessions list?) only governs whether we're a usable cast /
        # remote-control target — browsing and direct playback work as soon as
        # we hold a valid token. That check blocks for ~3s+ (and longer when it
        # retries on a "halfway connected" client), so don't hold up the UI on
        # it: verify in the background. Only a top-level connect (do_retries)
        # owns the verifier; rebuilds it spawns are validated inline by it.
        if do_retries:
            threading.Thread(target=self._verify_connected, args=(client, server),
                             name="verify-connected", daemon=True).start()

    def _verify_connected(self, client, server):
        """Background confirmation that the websocket session registered with
        the server (needed only for cast / remote control; browsing already
        works via the token). On a confirmed "halfway connected" client — one
        that never shows up in the server's session list — rebuild it with
        bounded retries, the same remedy the startup path used to run
        synchronously, now off the UI's critical path."""
        if self._is_session_live(client, server):
            self._mark_cast_ready(server)
            return

        # Jellyfin client sometimes "connects" halfway but doesn't actually
        # work. Retry a few times to reduce the odds of this happening.
        partial_reconnect_attempts = 3
        for i in range(partial_reconnect_attempts):
            if self.is_stopping:
                return
            log.warning(
                f"Partially connected. Retrying {i+1}/{partial_reconnect_attempts}."
            )
            self._disconnect_client(server=server)
            if self._stop_event.wait(1):
                return
            # do_retries=False: no nested verifier — we confirm it ourselves.
            if not self.connect_client(server, False):
                continue
            if self._is_session_live(self.clients.get(server["uuid"]), server):
                self._mark_cast_ready(server)
                return
        # Gave up: the device browses fine but can't be a cast target. Leave
        # cast_ready False and let the UI reflect the final (yellow) state.
        self.on_servers_changed()

    def _mark_cast_ready(self, server):
        if not server.get("cast_ready"):
            server["cast_ready"] = True
            self.on_servers_changed()

    def _is_session_live(self, client, server=None):
        """True once this device shows in the server's session list. Probes
        once, then once more after a short delay (the session can take a moment
        to register after the websocket connects). The second probe is
        non-dry-run: pass the server dict so a failed client is removed from
        the registry (identity-checked), not just stopped in place."""
        if client is None:
            return False
        if self.validate_client(client, True):
            return True
        log.info("Not connected yet, waiting 3 seconds...")
        if self._stop_event.wait(3):
            return False
        if self.validate_client(client, server=server):
            log.info("Actually connected now.")
            return True
        return False

    def remove_client(self, uuid: str):
        self.credentials = [
            server for server in self.credentials if server["uuid"] != uuid
        ]
        self.save_credentials()
        self._disconnect_client(uuid=uuid)

    def connect_client(self, server, do_retries=True):
        uuid = server["uuid"]

        # The lock only guards registry state; the network work below runs
        # outside it. Holding the lock across authenticate (10s+ timeouts)
        # would stall every reconnect loop and block shutdown for the full
        # HTTP timeout stack. A per-uuid reservation keeps concurrent
        # connectors (health check, websocket reconnect, the cast verifier)
        # from building duplicate clients for the same server.
        with self._client_lock:
            if self.is_stopping:
                return False
            if uuid in self.clients:
                return True
            if uuid in self._connecting:
                return False  # another thread is already on it
            self._connecting.add(uuid)

        try:
            client = self.client_factory()
            state = client.authenticate({"Servers": [server]}, discover=False)
            server["connected"] = state["State"] == CONNECTION_STATE["SignedIn"]
            if not server["connected"]:
                return False

            # Register the client immediately; the cast/remote-control session
            # check (and its half-connect retries) runs in the background so
            # the UI isn't held up. See setup_client / _verify_connected.
            if do_retries:
                # Top-level connect: casting is unconfirmed until the verifier
                # says so — surfaced as "Connected (casting unavailable)" until
                # then. Reconnect paths without a verifier are left as-is.
                server["cast_ready"] = False
            self.setup_client(client, server, do_retries)
            registered = False
            with self._client_lock:
                if not self.is_stopping:
                    self.clients[uuid] = client
                    if server.get("username"):
                        self.usernames[uuid] = server["username"]
                    registered = True
            if not registered:
                # stop() drained the registry while we were connecting;
                # don't resurrect a client it can no longer see.
                client.stop()
                return False
            return True
        finally:
            with self._client_lock:
                self._connecting.discard(uuid)

    def _disconnect_client(self, uuid: Optional[str] = None, server=None,
                           expected_client=None):
        """Remove and stop the registered client. With expected_client, only
        act if that exact instance is still the registered one — a probe that
        raced a reconnect must not tear down the healthy replacement. Returns
        True if a client was removed."""
        with self._client_lock:
            if uuid is None and server is not None:
                uuid = server["uuid"]

            client = self.clients.get(uuid)
            if client is None:
                return False
            if expected_client is not None and client is not expected_client:
                return False

            if server is not None:
                server["connected"] = False

            del self.clients[uuid]
        client.stop()
        return True

    def remove_all_clients(self):
        self.stop_all_clients()
        self.credentials = []
        self.save_credentials()

    def stop_all_clients(self):
        with self._client_lock:
            clients, self.clients = dict(self.clients), {}
        for client in clients.values():
            client.stop()

    def check_all_clients(self):
        if settings.work_offline:
            return  # don't touch the network in offline mode
        log.info("Performing client health check...")
        # Iterate credentials so validate_client gets the server dict: on a
        # failed check it disconnects the client, and the retry pass right
        # below then reconnects it in the same tick.
        for server in list(self.credentials):
            client = self.clients.get(server["uuid"])
            if client is not None:
                self.validate_client(client, server=server)
        # Retry credentials that aren't currently connected. Without this, a
        # server that fails the initial connect (e.g. shim started before LAN
        # was up) is never tried again until the user restarts the app —
        # the long-standing reliability hole behind issues #344 / #410.
        for server in self.credentials:
            if server["uuid"] not in self.clients and not self.is_stopping:
                log.info(
                    "Health check: retrying disconnected server %s",
                    server.get("address"),
                )
                self.connect_client(server, do_retries=False)

    @property
    def is_stopping(self):
        return self._stop_event.is_set()

    def stop(self):
        # Flag first so no in-flight connect_client registers a new client
        # after we've drained the registry; setting the event also wakes
        # every sleeping reconnect/retry loop.
        self._stop_event.set()

        if self.health_check:
            self.health_check.stop()
            self.health_check = None

        self.stop_all_clients()

    def get_username_from_client(self, client):
        # This is kind of convoluted. It may fail if a server
        # was added before we started saving usernames.
        for uuid, client2 in self.clients.items():
            if client2 is client:
                if uuid in self.usernames:
                    return self.usernames[uuid]
                for server in self.credentials:
                    if server["uuid"] == uuid:
                        return server.get("username", "Unknown")
                break

        return "Unknown"


clientManager = ClientManager()
