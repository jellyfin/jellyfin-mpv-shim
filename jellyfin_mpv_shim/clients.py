from jellyfin_apiclient_python import JellyfinClient
from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE
from .conf import settings
from . import conffile
from getpass import getpass
from .constants import CLIENT_VERSION, USER_APP_NAME, USER_AGENT, APP_NAME

import sys
import os.path
import json
import uuid

class ClientManager(object):
    def __init__(self):
        self.callback = lambda client, event_name, data: None
        self.credentials = []
        self.clients = {}
    
    def cli_connect(self):
        is_logged_in = self.try_connect()
        add_another = False

        if len(sys.argv) > 1 and sys.argv[1] == "add":
            add_another = True

        while not is_logged_in or add_another:
            server = input("Server URL: ")
            username = input("Username: ")
            password = getpass("Password: ")

            is_logged_in = self.login(server, username, password)

            if is_logged_in:
                print("Successfully added server.")
                add_another = input("Add another server? [y/N] ")
                add_another = add_another in ("y", "Y", "yes", "Yes")
            else:
                print("Adding server failed.")        

    def client_factory(self):
        client = JellyfinClient()
        client.config.data['app.default'] = True
        client.config.app(USER_APP_NAME, CLIENT_VERSION, settings.player_name, settings.client_uuid)
        client.config.data['http.user_agent'] = USER_AGENT
        client.config.data['auth.ssl'] = True
        return client

    def try_connect(self):
        self.credentials = []
        credentials_location = conffile.get(APP_NAME,'cred.json')            
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

        is_logged_in = False
        for server in self.credentials:
            client = self.client_factory()
            state = client.authenticate({"Servers":[server]})
            server["connected"] = state['State'] == CONNECTION_STATE['SignedIn']
            if server["connected"]:
                is_logged_in = True
                self.clients[server["uuid"]] = client
                self.setup_client(client)

        return is_logged_in

    def save_credentials(self):
        credentials_location = conffile.get(APP_NAME,'cred.json')
        with open(credentials_location, "w") as cf:
            json.dump(self.credentials, cf)

    def login(self, server, username, password):
        client = self.client_factory()
        client.auth.connect_to_address(server)
        client.auth.login(server, username, password)
        state = client.auth.connect()
        is_logged_in = state['State'] == CONNECTION_STATE['SignedIn']
        if is_logged_in:
            credentials = client.auth.credentials.get_credentials()
            client.authenticate(credentials)
            server = credentials["Servers"][0]
            server["connected"] = True
            server["uuid"] = str(uuid.uuid4())
            server["username"] = username
            self.clients[server["uuid"]] = client
            self.credentials.append(server)
            self.save_credentials()
            self.setup_client(client)
        return is_logged_in

    def setup_client(self, client):
        def event(event_name, data):
            self.callback(client, event_name, data)

        client.callback = event
        client.callback_ws = event
        client.start(websocket=True)

        client.jellyfin.post_capabilities({
            'PlayableMediaTypes': "Video",
            'SupportsMediaControl': True,
            'SupportedCommands': (
                "MoveUp,MoveDown,MoveLeft,MoveRight,Select,"
                "Back,ToggleFullscreen,"
                "GoHome,GoToSettings,TakeScreenshot,"
                "VolumeUp,VolumeDown,ToggleMute,"
                "SetAudioStreamIndex,SetSubtitleStreamIndex,"
                "Mute,Unmute,SetVolume,DisplayContent,"
                "Play,Playstate,PlayNext,PlayMediaSource"
            ),
        })

    def remove_client(self, uuid):
        self.credentials = [server for server in self.credentials if server["uuid"] != uuid]
        self.save_credentials()

        if not uuid in self.clients:
            return

        self.clients[uuid].stop()
        del self.clients[uuid]

    def stop(self):
        for client in self.clients.values():
            client.stop()

clientManager = ClientManager()
