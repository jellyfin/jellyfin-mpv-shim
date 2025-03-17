from jellyfin_apiclient_python import JellyfinClient
from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE
from .conf import settings
from . import conffile
from getpass import getpass
from .constants import CAPABILITIES, CLIENT_VERSION, USER_APP_NAME, USER_AGENT, APP_NAME
from .i18n import _

import sys
import os.path
import json
import uuid
import time
import logging
import re
import threading

log = logging.getLogger("clients")
path_regex = re.compile("^(https?://)?(?:(\[[^/]+\])|([^/:]+))(:[0-9]+)?(/.*)?$")

from typing import Optional


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

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join()

    def run(self):
        while not self.halt:
            if not self.trigger.wait(settings.health_check_interval):
                self.callback()


class ClientManager(object):
    def __init__(self):
        self.callback = lambda client, event_name, data: None
        self.credentials = []
        self.clients = {}
        self.usernames = {}
        self.is_stopping = False

        self.health_check = None
        if settings.health_check_interval is not None:
            self.health_check = PeriodicHealthCheck(self.check_all_clients)
            self.health_check.start()

    def cli_connect(self):
        is_logged_in = self.try_connect()
        add_another = False

        if "add" in sys.argv:
            add_another = True

        while not is_logged_in or add_another:
            server = input(_("Server URL: "))
            username = input(_("Username: "))
            password = getpass(_("Password: "))

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
        for server in self.credentials:
            if self.connect_client(server):
                is_logged_in = True
        return is_logged_in

    def try_connect(self):
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

        is_logged_in = self._connect_all()
        if settings.connect_retry_mins and not is_logged_in:
            log.warning(
                "Connection failed. Will retry for {0} minutes.".format(
                    settings.connect_retry_mins
                )
            )
            for attempt in range(settings.connect_retry_mins * 2):
                time.sleep(30)
                is_logged_in = self._connect_all()
                if is_logged_in:
                    break

        return is_logged_in

    def save_credentials(self):
        credentials_location = conffile.get(APP_NAME, "cred.json")
        with open(credentials_location, "w") as cf:
            json.dump(self.credentials, cf)

    def login(
        self, server: str, username: str, password: str, force_unique: bool = False
    ):
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

        server = "".join(filter(bool, (protocol, ipv6_host, ipv4_host, port, path)))

        client = self.client_factory()
        client.auth.connect_to_address(server)
        result = client.auth.login(server, username, password)
        if "AccessToken" in result:
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
        return False

    def validate_client(self, client: "JellyfinClient", dry_run=False):
        client_list = client.jellyfin.sessions(
            params={"ControllableByUserId": "{UserId}"}
        )

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
                # WebSocketDisconnect doesn't always happen here.
                client.callback = lambda *_: None
                client.callback_ws = lambda *_: None
                client.stop()
                client.callback("WebSocketDisconnect", None)
            return False

        return True

    def setup_client(self, client: "JellyfinClient", server):
        def event(event_name, data):
            if event_name == "WebSocketDisconnect":
                timeout_gen = expo(100)
                if server["uuid"] in self.clients:
                    while not self.is_stopping:
                        timeout = next(timeout_gen)
                        log.info(
                            "No connection to server. Next try in {0} second(s)".format(
                                timeout
                            )
                        )
                        self._disconnect_client(server=server)
                        time.sleep(timeout)
                        if self.connect_client(server, False):
                            break
            else:
                self.callback(client, event_name, data)

        client.callback = event
        client.callback_ws = event
        client.start(websocket=True)

        client.jellyfin.post_capabilities(CAPABILITIES)

        # Check connection
        if self.validate_client(client, True):
            return True

        # Wait and check connection again before destroying/re-creating client
        log.info("Not connected yet, waiting 3 seconds...")
        time.sleep(3)
        is_connected = self.validate_client(client)

        if is_connected:
            log.info("Actually connected now.")
        return is_connected

    def remove_client(self, uuid: str):
        self.credentials = [
            server for server in self.credentials if server["uuid"] != uuid
        ]
        self.save_credentials()
        self._disconnect_client(uuid=uuid)

    def connect_client(self, server, do_retries=True):
        if self.is_stopping:
            return False

        is_logged_in = False
        client = self.client_factory()
        state = client.authenticate({"Servers": [server]}, discover=False)
        server["connected"] = state["State"] == CONNECTION_STATE["SignedIn"]
        if server["connected"]:
            is_logged_in = self.setup_client(client, server)
            if is_logged_in:
                self.clients[server["uuid"]] = client
                if server.get("username"):
                    self.usernames[server["uuid"]] = server["username"]
            elif do_retries:
                # Jellyfin client sometimes "connects" halfway but doesn't actually work.
                # Retry three times to reduce odds of this happening.
                partial_reconnect_attempts = 3
                for i in range(partial_reconnect_attempts):
                    log.warning(
                        f"Partially connected. Retrying {i+1}/{partial_reconnect_attempts}."
                    )
                    self._disconnect_client(server=server)
                    time.sleep(1)
                    if self.connect_client(server, False):
                        is_logged_in = True
                        break

        return is_logged_in

    def _disconnect_client(self, uuid: Optional[str] = None, server=None):
        if uuid is None and server is not None:
            uuid = server["uuid"]

        if uuid not in self.clients:
            return

        if server is not None:
            server["connected"] = False

        client = self.clients[uuid]
        del self.clients[uuid]
        client.stop()

    def remove_all_clients(self):
        self.stop_all_clients()
        self.credentials = []
        self.save_credentials()

    def stop_all_clients(self):
        for key, client in list(self.clients.items()):
            del self.clients[key]
            client.stop()

    def check_all_clients(self):
        log.info("Performing client health check...")
        for client in self.clients.values():
            self.validate_client(client)

    def stop(self):
        if self.health_check:
            self.health_check.stop()
            self.health_check = None

        self.is_stopping = True
        for client in self.clients.values():
            client.stop()

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
