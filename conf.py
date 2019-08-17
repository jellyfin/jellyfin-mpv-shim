import logging
import os
import requests
import uuid
import xml.etree.ElementTree as ET
import pickle as pickle
import socket

log = logging.getLogger('conf')

class Settings(object):
    _listeners = []

    _path = None
    _data = {
        "myplex_username":      "",
        "myplex_password":      "",
        "myplex_token":         "",
        "player_name":          socket.gethostname(),
        "plex_server":          "",
        "http_port":            "3000",
        "audio_output":         "hdmi",
        "audio_ac3passthrough": False,
        "audio_dtspassthrough": False,
        "client_uuid":          str(uuid.uuid4()),
        "display_sleep":        0,
        "display_mode":         ""
    }

    def __getattr__(self, name):
        return self._data[name]

    def __setattr__(self, name, value):
        if name in self._data:
            self._data[name] = value
            self.save()

            for callback in self._listeners:
                try:
                    callback(name, value)
                except:
                    pass
        else:
            super(Settings, self).__setattr__(name, value)

    def __get_file(self, path, mode="rb+", create=True):
        created = False

        if not os.path.exists(path):
            try:
                fh = open(path, mode)
            except IOError as e:
                if e.errno == 2 and create:
                    fh = open(path, 'wb+')
                    pickle.dump(self._data, fh)
                    fh.close()
                    created = True
                else:
                    raise e
            except Exception as e:
                log.error("Error opening settings from path: %s" % path)
                return None

        # This should work now
        return open(path, mode), created

    def load(self, path, create=True):
        fh, created = self.__get_file(path, "rb+", create)
        if not created:
            try:
                data = pickle.load(fh)
                self._data.update(data)
            except Exception as e:
                log.error("Error loading settings from pickle: %s" % e)
                fh.close()
                return False

        self._path = path
        fh.close()
        return True

    def save(self):
        fh, created = self.__get_file(self._path, "wb+", True)

        try:
            pickle.dump(self._data, fh)
            fh.flush()
            fh.close()
        except Exception as e:
            log.error("Error saving settings to pickle: %s" % e)
            return False

        return True

    def login_myplex(self, username, password, test=False):
        url     = "https://my.plexapp.com/users/sign_in.xml"
        auth    = (username, password)
        token   = None
        headers = {
            "Content-Type":             "application/xml",
            "X-Plex-Client-Identifier": self.client_uuid,
            "X-Plex-Product":           "Plex Media Player",
            "X-Plex-Version":           "1.0",
            "X-Plex-Provides":          "player",
            "X-Plex-Platform":          "Raspberry Pi"
        }

        try:
            response = requests.post(url, auth=auth, headers=headers)
            root     = ET.fromstring(response.text)
            token    = root.findall('authentication-token')[0].text
            user     = root.findall('username')[0].text
            log.info("Logged in to myPlex as %s" % user)
        except Exception as e:
            log.error("Error logging into MyPlex: %s" % e)

        if not test and token:
            self.myplex_token = token

        if token is not None:
            self.myplex_token    = token
            self.myplex_username = username
            self.myplex_password = password
            return True

        return False

    def add_listener(self, callback):
        """
        Register a callback to be called anytime a setting value changes.
        An example callback function:

            def my_callback(key, value):
                # Do something with the new setting ``value``...

        """
        if callback not in self._listeners:
            self._listeners.append(callback)

settings = Settings()
