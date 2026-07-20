from jellyfin_apiclient_python import JellyfinClient
from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE
from .conf import settings
from . import conffile
from .users import userManager
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
from collections import OrderedDict
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

# After the cast-session verifier's fast retries give up, keep retrying the
# dropped server with exponential backoff for this long before surrendering
# to the periodic health check (which may be disabled entirely).
VERIFY_RETRY_WINDOW_SECS = 600


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
VOLATILE_CREDENTIAL_KEYS = frozenset({"connected", "cast_ready"})


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
        # Fired when a server actually (re)connects in the background (health
        # check, websocket reconnect). Distinct from on_servers_changed: this
        # one means a server became BROWSABLE, so the GUI must push the full
        # servers payload (rebuild the browse source / switcher), not just a
        # status badge. Set by the GUI.
        self.on_server_connected = lambda: None
        self.credentials = []
        self.clients = {}
        self.usernames = {}
        # Guards registry state (clients dict, the _connecting reservations).
        # Never held across network I/O — see connect_client.
        self._client_lock = threading.RLock()
        # Server uuids with a connect in flight, so concurrent connectors
        # (health check vs websocket reconnect) don't build duplicates.
        self._connecting = set()
        # Server uuids the user removed. A health-check tick that captured the
        # credentials list before the removal could otherwise re-register the
        # deleted server (a zombie session that outlives its credential).
        # Cleared on an explicit re-login with the same uuid (force_unique).
        self._removed_uuids = set()
        # Set on stop(); lets reconnect/retry sleeps end immediately instead
        # of holding shutdown hostage for up to their full backoff interval.
        self._stop_event = threading.Event()
        # The active user's Jellyfin device id / device name. Populated for real
        # from userManager in load_credentials; these placeholders only matter
        # if a connect somehow runs before load. Switching users swaps them so
        # each user presents a distinct device to the servers.
        self.device_id = settings.client_uuid
        self.device_name = settings.player_name
        # Set for the duration of a user switch. The health-check thread and the
        # websocket reconnect loops must stand down while we tear down one
        # user's clients and swap in another's, or a stale tick could resurrect
        # a just-disconnected server under the wrong device id.
        self._switching = threading.Event()
        # Serializes concurrent switch_user calls (e.g. impatient double-click)
        # AND every mutation of self.credentials + its save. The credential
        # list is touched from several worker threads (a finishing login, a
        # switch, remove/update); an unlocked rebind racing an append loses
        # the appended server, and a login finishing mid-switch would file its
        # credential under the wrong user. RLock: switch_user holds it while
        # calling helpers that also take it. Never held across network I/O.
        self._switch_lock = threading.RLock()

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
        # Remove the old credential. Rebuild-and-rebind under the switch lock:
        # an unlocked filter racing another thread's append would drop the
        # appended server from the rebound list.
        with self._switch_lock:
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

    def client_factory(self):
        client = JellyfinClient(allow_multiple_clients=True)
        client.config.data["app.default"] = True
        client.config.app(
            USER_APP_NAME, CLIENT_VERSION, self.device_name, self.device_id
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

        # Group by server Id, preserving the priority order within each group.
        # Different servers connect CONCURRENTLY; the addresses for one server
        # stay a serial fallback chain, because the sort put the most local
        # address first and racing them would let a worse route win.
        chains = OrderedDict()
        for server in sorted_credentials:
            chains.setdefault(server["Id"], []).append(server)
        chains = list(chains.values())
        if not chains:
            return False

        # The server the UI wants to open on. If it comes up we can render
        # immediately and let the rest arrive behind the homepage; only if it
        # is unreachable do we wait for the others.
        prefer_uuid = None
        try:
            prefer_uuid = userManager.get_last_server()
        except Exception:
            log.debug("Could not read the remembered server.", exc_info=True)
        # None = no preference, in which case the FIRST server to answer
        # releases the UI. Defaulting to chain 0 instead would have recreated
        # the original problem for anyone who has not switched servers yet:
        # if chain 0 is the dead one, we would wait out its timeout again.
        preferred = None
        if prefer_uuid:
            for index, chain in enumerate(chains):
                if any(s.get("uuid") == prefer_uuid for s in chain):
                    preferred = index
                    break

        ready = threading.Event()      # UI may render
        done = threading.Event()       # every chain has finished
        state = {"connected": 0, "finished": 0}
        lock = threading.Lock()

        def run(index, chain):
            ok = False
            try:
                for server in chain:
                    if self.is_stopping:
                        break
                    if self.connect_client(server):
                        ok = True
                        break
            except Exception:
                log.error("Connecting to server failed.", exc_info=True)
            with lock:
                if ok:
                    state["connected"] += 1
                state["finished"] += 1
                all_done = state["finished"] == len(chains)
                # Late = the UI has already rendered without this server, so
                # it has to be told to pick it up.
                late = ok and ready.is_set()
                # With a remembered server we hold for that one specifically,
                # so the library does not open on whichever server happened to
                # answer first and then jump when the right one arrives.
                # Without one, any success will do.
                release = ok if preferred is None else (ok and index == preferred)
                if release or all_done:
                    ready.set()
                if all_done:
                    done.set()
            if late:
                try:
                    self.on_server_connected()
                except Exception:
                    log.debug("Late-connect notification failed.",
                              exc_info=True)

        threads = []
        for index, chain in enumerate(chains):
            # Daemon: connect_all returns as soon as the UI can render, so
            # these outlive it, and a server stuck in authenticate's timeout
            # must never hold up process exit.
            thread = threading.Thread(
                target=run, args=(index, chain), daemon=True,
                name="connect-%d" % index)
            threads.append(thread)
            thread.start()

        # Unblocks on the preferred server connecting, or on every chain
        # finishing. Waiting for ALL of them was the old behaviour by
        # accident: authenticate carries a 10s+ timeout, so one server being
        # down delayed the library by that much before anything rendered.
        ready.wait()
        with lock:
            is_logged_in = state["connected"] > 0
            pending = len(chains) - state["finished"]
        if pending > 0:
            log.info("Rendering with the preferred server; %d still connecting.",
                     pending)
        return is_logged_in

    def load_credentials(self):
        """Load the active user's saved credentials. Fast (no network) — call
        this before connecting so callers know which servers exist up front.

        userManager owns persistence now (users.json); on first run it migrates
        the legacy cred.json into a "(default)" user. We adopt that user's
        device id / name so its servers see the right device."""
        userManager.load()
        self._adopt_active_user()

    def _adopt_active_user(self):
        """Point our live credential list + device identity at the active user."""
        self.credentials = userManager.credentials_for_active()
        self.device_id = userManager.active_device_id
        self.device_name = userManager.active_device_name

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
        # Persist into the active user's record in users.json. Strip volatile
        # runtime keys first (a stale "connected: true" is misleading on reload).
        userManager.set_active_credentials(
            clean_credentials_for_save(self.credentials)
        )

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
        self, client: "JellyfinClient", username: str, force_unique: bool = False,
        owner_id=None,
    ):
        """Stash a freshly-authenticated client into our credential store.

        The client must already hold a valid AccessToken for its first server
        (either from a password login or a Quick Connect exchange).
        ``owner_id`` is the local user who initiated the login; if the active
        user changed while the (slow) login ran, the credential is filed under
        that user instead of leaking into whoever is active now.
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
        with self._switch_lock:
            if owner_id is not None and owner_id != userManager.active_id:
                # The user switched while we were logging in. Persist the
                # credential to the initiating user (it connects next time
                # they're active) and don't register a live client under the
                # wrong user's session.
                log.warning(
                    "Login finished after a user switch; filing the server "
                    "under the original user."
                )
                return userManager.append_credentials_for(
                    owner_id, clean_credentials_for_save([server])[0]
                )
            # An explicit login supersedes any earlier removal of this uuid
            # (force_unique reuses the server Id as uuid across add/remove
            # cycles).
            with self._client_lock:
                self._removed_uuids.discard(server["uuid"])
            self.credentials.append(server)
            self.save_credentials()
        self.connect_client(server)
        if owner_id is not None:
            with self._switch_lock:
                if owner_id != userManager.active_id:
                    # A switch slipped in between the save and the connect: the
                    # credential is already safely filed (it was saved while
                    # the owner was still active), but the live client we just
                    # registered belongs to the old user — tear it down.
                    self._disconnect_client(server=server)
        return True

    def login(
        self, server: str, username: str, password: str, force_unique: bool = False
    ):
        server = self._normalize_server(server)
        owner_id = userManager.active_id

        client = self.client_factory()
        client.auth.connect_to_address(server)
        result = client.auth.login(server, username, password)
        if "AccessToken" in result:
            return self._finalize_login(client, username, force_unique,
                                        owner_id=owner_id)
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
        # The poll below can run for minutes; remember who started it so the
        # credential lands under them even if the active user changes meanwhile.
        owner_id = userManager.active_id

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

        return self._finalize_login(client, result["User"]["Name"],
                                    owner_id=owner_id)

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
            if f_client.get("DeviceId") == self.device_id:
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
        # The apiclient's WSClient redials in a tight loop with NO delay while
        # the server is unreachable — a refused connect returns instantly, so
        # a down server gets hammered with tens of thousands of attempts. Its
        # on_error callback runs on that same redial thread, so blocking here
        # (interruptibly) is the backoff. Reset ONLY on WebSocketConnect: it
        # runs on the same WS thread (no cross-thread write to the cell), and
        # HTTP-layer failure events ("ServerUnreachable") arrive through this
        # same closure from other threads mid-outage — resetting on those
        # would both race the generator and keep restarting the backoff at 1s.
        ws_error_backoff = [None]

        def event(event_name, data):
            if event_name == "WebSocketError":
                gen = ws_error_backoff[0]
                if gen is None:
                    gen = ws_error_backoff[0] = expo(60)
                self._stop_event.wait(next(gen))
                self.callback(client, event_name, data)
                return

            if event_name == "WebSocketDisconnect":
                timeout_gen = expo(100)
                # Identity check, not membership: a stale WSClient thread
                # (e.g. parked in the error backoff above while its client was
                # replaced) fires a final WebSocketDisconnect on exit; acting
                # on it would tear down the healthy replacement.
                if self.clients.get(server["uuid"]) is client:
                    while not self.is_stopping and not settings.work_offline:
                        timeout = next(timeout_gen)
                        log.info(
                            "No connection to server. Next try in {0} second(s)".format(
                                timeout
                            )
                        )
                        self._disconnect_client(server=server,
                                                expected_client=client)
                        # Interruptible: this runs on a non-daemon websocket
                        # thread, and an uninterruptible sleep here used to
                        # hold app exit hostage for up to the full backoff.
                        if self._stop_event.wait(timeout):
                            break
                        if self.connect_client(server, False):
                            self.on_server_connected()
                            break
            elif event_name == "WebSocketConnect":
                ws_error_backoff[0] = None  # same WS thread as the errors
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
        # Gave up on the fast retries: reflect the degraded state now, then
        # keep trying with bounded backoff. A flaky server (e.g. one that
        # blackholes rather than refuses) routinely needs longer than the
        # three quick attempts, and without this the server stays dropped
        # until the next health-check tick — or forever if health checks are
        # disabled.
        self.on_servers_changed()
        backoff = expo(60)
        deadline = time.time() + VERIFY_RETRY_WINDOW_SECS
        while time.time() < deadline:
            if self._stop_event.wait(next(backoff)):
                return
            if settings.work_offline or self._switching.is_set():
                return
            with self._client_lock:
                if server["uuid"] in self._removed_uuids:
                    return  # the user removed this server; stop retrying it
            if server["uuid"] in self.clients:
                return  # a health tick or ws redial reconnected it already
            if self.connect_client(server, False):
                # Browsable again (the token works). Confirm the cast session
                # with a single dry probe; if it still isn't registered, keep
                # the connection rather than churning through more rebuilds.
                client = self.clients.get(server["uuid"])
                if client is not None and self.validate_client(client, dry_run=True):
                    self._mark_cast_ready(server)
                else:
                    self.on_servers_changed()
                self.on_server_connected()
                return
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
        with self._client_lock:
            self._removed_uuids.add(uuid)
        with self._switch_lock:
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
                if not self.is_stopping and uuid not in self._removed_uuids:
                    self.clients[uuid] = client
                    if server.get("username"):
                        self.usernames[uuid] = server["username"]
                    registered = True
            if not registered:
                # stop() drained the registry while we were connecting, or
                # the user removed this server mid-connect; don't resurrect a
                # client nothing can see or that was just deleted.
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
        # Silence before stopping: the WSClient thread fires one final
        # WebSocketDisconnect when its loop exits, even on an intentional
        # stop; letting it reach the reconnect handler would tear down or
        # rebuild a server we just deliberately disconnected.
        client.callback = lambda *_: None
        client.callback_ws = lambda *_: None
        client.stop()
        return True

    def switch_user(self, user_id):
        """Disconnect the active user's servers and connect ``user_id``'s.

        The heavy connect_all() runs after the swap with the switch flag
        cleared, so the health check can help it along and shutdown stays
        responsive. Returns True on a successful (attempted) switch, False if
        the target user doesn't exist. A target with no servers is still a
        successful switch — it simply lands on the login screen."""
        # Serialize switches; the flag makes the health check / reconnect loops
        # stand down while we drain and swap the registry.
        with self._switch_lock:
            if self.is_stopping:
                return False
            target = userManager.get(user_id)
            if target is None:
                return False
            if user_id == userManager.active_id and self.clients:
                return True  # already here and connected; nothing to do

            self._switching.set()
            try:
                # Persist whatever the active user currently has, then tear its
                # live clients down.
                self.save_credentials()
                self.stop_all_clients()
                # Swap identity + credentials to the target user.
                userManager.set_active(user_id)
                self._adopt_active_user()
                # A fresh user starts with a clean removal ledger; stale uuids
                # from the previous user must not suppress its (possibly
                # uuid-colliding) reconnects.
                with self._client_lock:
                    self._removed_uuids.clear()
            finally:
                self._switching.clear()

        self.connect_all()
        return True

    def remove_all_clients(self):
        self.stop_all_clients()
        with self._switch_lock:
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
        if self._switching.is_set():
            # A user switch is tearing down/standing up clients; a health tick
            # that captured the old credential list mid-swap could otherwise
            # reconnect a just-disconnected server. Skip this tick.
            return
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
        # No pre-probe is needed: authenticate() resolves through the
        # apiclient's connect_to_server, whose first step is a single
        # get_public_info with a 5-second timeout and no retries, so a dead
        # (even SYN-blackholed) server costs this loop ~5s per tick.
        reconnected = False
        for server in list(self.credentials):
            if server["uuid"] not in self.clients and not self.is_stopping:
                log.info(
                    "Health check: retrying disconnected server %s",
                    server.get("address"),
                )
                if self.connect_client(server, do_retries=False):
                    reconnected = True
        if reconnected:
            # The server is browsable again; without this the UI never learns
            # (a server that was offline at startup stayed missing from the
            # browser until an app restart).
            self.on_server_connected()

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
