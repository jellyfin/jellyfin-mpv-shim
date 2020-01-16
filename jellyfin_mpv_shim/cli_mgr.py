import time
from .clients import clientManager

class UserInterface(object):
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop = lambda: None

    def login_servers(self):
        clientManager.cli_connect()

    def run(self):
        while True:
            time.sleep(1)

userInterface = UserInterface()
