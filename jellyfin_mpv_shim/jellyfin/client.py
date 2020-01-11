# -*- coding: utf-8 -*-
from __future__ import division, absolute_import, print_function, unicode_literals

#################################################################################################

import logging

from . import api
from .configuration import Config
from .http import HTTP
from .ws_client import WSClient
from .connection_manager import ConnectionManager, CONNECTION_STATE

#################################################################################################

LOG = logging.getLogger('JELLYFIN.' + __name__)

#################################################################################################


def callback(message, data):

    ''' Callback function should received message, data
        message: string
        data: json dictionary
    '''
    pass


class JellyfinClient(object):

    logged_in = False

    def __init__(self):
        LOG.debug("JellyfinClient initializing...")

        self.config = Config()
        self.http = HTTP(self)
        self.wsc = WSClient(self)
        self.auth = ConnectionManager(self)
        self.jellyfin = api.API(self.http)
        self.callback_ws = callback
        self.callback = callback

    def set_credentials(self, credentials=None):
        self.auth.credentials.set_credentials(credentials or {})

    def get_credentials(self):
        return self.auth.credentials.get_credentials()

    def authenticate(self, credentials=None, options=None):

        self.set_credentials(credentials or {})
        state = self.auth.connect(options or {})

        if state['State'] == CONNECTION_STATE['SignedIn']:

            LOG.info("User is authenticated.")
            self.logged_in = True
            self.callback("ServerOnline", {'Id': self.auth.server_id})

        state['Credentials'] = self.get_credentials()

        return state

    def start(self, websocket=False, keep_alive=True):

        if not self.logged_in:
            raise ValueError("User is not authenticated.")

        self.http.start_session()

        if keep_alive:
            self.http.keep_alive = True

        if websocket:
            self.start_wsc()

    def start_wsc(self):
        self.wsc.start()

    def stop(self):

        self.wsc.stop_client()
        self.http.stop_session()
