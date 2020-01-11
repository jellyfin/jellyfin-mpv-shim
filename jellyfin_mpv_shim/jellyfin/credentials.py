# -*- coding: utf-8 -*-
from __future__ import division, absolute_import, print_function, unicode_literals

#################################################################################################

import logging
import time
from datetime import datetime

#################################################################################################

LOG = logging.getLogger('JELLYFIN.' + __name__)

#################################################################################################


class Credentials(object):

    credentials = None

    def __init__(self):
        LOG.debug("Credentials initializing...")

    def set_credentials(self, credentials):
        self.credentials = credentials

    def get_credentials(self, data=None):

        if data is not None:
            self._set(data)

        return self._get()

    def _ensure(self):

        if not self.credentials:
            try:
                LOG.info(self.credentials)
                if not isinstance(self.credentials, dict):
                    raise ValueError("invalid credentials format")

            except Exception as e:  # File is either empty or missing
                LOG.warning(e)
                self.credentials = {}

            LOG.debug("credentials initialized with: %s", self.credentials)
            self.credentials['Servers'] = self.credentials.setdefault('Servers', [])

    def _get(self):
        self._ensure()

        return self.credentials

    def _set(self, data):

        if data:
            self.credentials.update(data)
        else:
            self._clear()

        LOG.debug("credentialsupdated")

    def _clear(self):
        self.credentials.clear()

    def add_update_user(self, server, user):

        for existing in server.setdefault('Users', []):
            if existing['Id'] == user['Id']:
                # Merge the data
                existing['IsSignedInOffline'] = True
                break
        else:
            server['Users'].append(user)

    def add_update_server(self, servers, server):

        if server.get('Id') is None:
            raise KeyError("Server['Id'] cannot be null or empty")

        # Add default DateLastAccessed if doesn't exist.
        server.setdefault('DateLastAccessed', "2001-01-01T00:00:00Z")

        for existing in servers:
            if existing['Id'] == server['Id']:

                # Merge the data
                if server.get('DateLastAccessed'):
                    if self._date_object(server['DateLastAccessed']) > self._date_object(existing['DateLastAccessed']):
                        existing['DateLastAccessed'] = server['DateLastAccessed']

                if server.get('UserLinkType'):
                    existing['UserLinkType'] = server['UserLinkType']

                if server.get('AccessToken'):
                    existing['AccessToken'] = server['AccessToken']
                    existing['UserId'] = server['UserId']

                if server.get('ExchangeToken'):
                    existing['ExchangeToken'] = server['ExchangeToken']

                if server.get('ManualAddress'):
                    existing['ManualAddress'] = server['ManualAddress']

                if server.get('LocalAddress'):
                    existing['LocalAddress'] = server['LocalAddress']

                if server.get('Name'):
                    existing['Name'] = server['Name']

                if server.get('LastConnectionMode') is not None:
                    existing['LastConnectionMode'] = server['LastConnectionMode']

                if server.get('ConnectServerId'):
                    existing['ConnectServerId'] = server['ConnectServerId']

                return existing
        else:
            servers.append(server)
            return server

    def _date_object(self, date):
        # Convert string to date
        try:
            date_obj = time.strptime(date, "%Y-%m-%dT%H:%M:%SZ")
        except (ImportError, TypeError):
            # TypeError: attribute of type 'NoneType' is not callable
            # Known Kodi/python error
            date_obj = datetime(*(time.strptime(date, "%Y-%m-%dT%H:%M:%SZ")[0:6]))

        return date_obj
