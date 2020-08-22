from .clients import clientManager


class UserInterface(object):
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop = lambda: None

    @staticmethod
    def login_servers():
        clientManager.cli_connect()

    def start(self):
        pass

    def stop(self):
        pass


user_interface = UserInterface()
