from jellyfin_apiclient_python import Jellyfin
from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE
from .conf import settings
from . import conffile
from getpass import getpass
from .constants import CLIENT_VERSION, USER_APP_NAME, USER_AGENT, APP_NAME

import sys
import os.path
import json

class ClientManager(object):
    def __init__(self):
        self.default_client = None
        self.callback = lambda client, event_name, data: None
    
    def cli_connect(self):
        is_logged_in = self.try_connect()

        if len(sys.argv) > 1 and sys.argv[1] == "add":
            is_logged_in = False

        while not is_logged_in:
            server = input("Server URL: ")
            username = input("Username: ")
            password = getpass("Password: ")

            self.login(server, username, password)

            add_another = input("Add another server? [y/N] ")
            if add_another in ("y", "Y", "yes", "Yes"):
                is_logged_in = False
        
        self.setup_clients()

    def try_connect(self):
        credentials = None
        credentials_location = conffile.get(APP_NAME,'cred.json')
        if os.path.exists(credentials_location):
            with open(credentials_location) as cf:
                credentials = json.load(cf)

        client = Jellyfin(None)
        self.default_client = client
        client.config.data['app.default'] = True
        client.config.app(USER_APP_NAME, CLIENT_VERSION, settings.player_name, settings.client_uuid)
        client.config.data['http.user_agent'] = USER_AGENT
        client.config.data['auth.ssl'] = True

        is_logged_in = False

        if credentials is not None:
            state = client.authenticate(credentials)
            is_logged_in = state['State'] == CONNECTION_STATE['SignedIn']

        return is_logged_in

    def login(self, server, username, password):
        client = self.default_client
        client.auth.connect_to_address(server)
        client.auth.login(server, username, password)
        state = client.auth.connect()
        is_logged_in = state['State'] == CONNECTION_STATE['SignedIn']
        if is_logged_in:
            credentials = client.auth.credentials.get_credentials()
            credentials_location = conffile.get(APP_NAME,'cred.json')
            with open(credentials_location, "w") as cf:
                json.dump(credentials, cf)
            client.authenticate(credentials)

    def setup_clients(self):
        clients = self.default_client.get_active_clients()
        for name, client in clients.items():
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

    def stop(self):
        clients = self.default_client.get_active_clients()
        for _, client in clients.items():
            client.stop()

clientManager = ClientManager()
