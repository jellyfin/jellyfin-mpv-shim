from queue import Empty, Queue
import threading
import time
import urllib.request
from werkzeug.serving import make_server
from flask import Flask, request, jsonify
from time import sleep
import os.path
import json
import base64

# So, most of my use of the webview library is ugly but there's a reason for this!
# Debian's python3-webview package is super old (2.3), pip3's pywebview package is much newer (3.2).
# I would just say "fuck it, install from testing/backports/unstable" except that's not any newer either.
# So instead I'm going to try supporting *both* here, which gets rather ugly because they made very significant changes.
#
# The key differences seem to be:
# 3.2's create_window() returns a Window object immediately, then you need to call start()
# 2.3's create_window() blocks forever effectivelly calling start() itself
#
# 3.2's Window has a .loaded Event that you need to subscribe to notice when the window is ready for input
# 2.3 has a webview_ready() function that blocks until webview is ready (or timeout is passed)
import webview  # Python3-webview in Debian, pywebview in pypi
import sys
from threading import Event
import datetime

from ..clients import clientManager
from ..player import playerManager
from ..conf import settings
from ..event_handler import eventHandler
from ..constants import CAPABILITIES, CLIENT_VERSION, USER_APP_NAME, APP_NAME
from ..utils import get_resource
from .. import conffile
import logging

log = logging.getLogger("webclient")

remember_layout = conffile.get(APP_NAME, "layout.json")


def do_not_cache(response):
    response.cache_control.no_store = True
    if response.cache_control.max_age:
        response.cache_control.max_age = None
    if response.cache_control.public:
        response.cache_control.public = False


# Based on https://stackoverflow.com/questions/15562446/
class Server(threading.Thread):
    def __init__(self):
        self.srv = None
        self.ctx = None

        threading.Thread.__init__(self)

    def stop(self):
        if self.srv is not None:
            self.srv.shutdown()
        self.join()

    def run(self):
        app = Flask(
            __name__,
            static_url_path="",
            static_folder=get_resource("webclient_view", "webclient"),
        )

        pl_event_queue = Queue()
        last_server_id = ""
        last_user_id = ""
        last_user_name = ""

        def wrap_playstate(active, playstate=None, item=None):
            if playstate is None:
                playstate = {
                    "CanSeek": False,
                    "IsPaused": False,
                    "IsMuted": False,
                    "RepeatMode": "RepeatNone",
                }
            res = {
                "PlayState": playstate,
                "AdditionalUsers": [],
                "Capabilities": {
                    "PlayableMediaTypes": CAPABILITIES["PlayableMediaTypes"].split(","),
                    "SupportedCommands": CAPABILITIES["SupportedCommands"].split(","),
                    "SupportsMediaControl": True,
                    "SupportsContentUploading": False,
                    "SupportsPersistentIdentifier": False,
                    "SupportsSync": False,
                },
                "RemoteEndPoint": "0.0.0.0",
                "PlayableMediaTypes": CAPABILITIES["PlayableMediaTypes"].split(","),
                "Id": settings.client_uuid,
                "UserId": last_user_id,
                "UserName": last_user_name,
                "Client": USER_APP_NAME,
                "LastActivityDate": datetime.datetime.utcnow().isoformat(),
                "LastPlaybackCheckIn": "0001-01-01T00:00:00.0000000Z",
                "DeviceName": settings.player_name,
                "DeviceId": settings.client_uuid,
                "ApplicationVersion": CLIENT_VERSION,
                "IsActive": active,
                "SupportsMediaControl": True,
                "SupportsRemoteControl": True,
                "HasCustomDeviceName": False,
                "ServerId": last_server_id,
                "SupportedCommands": CAPABILITIES["SupportedCommands"].split(","),
                "dest": "player",
            }
            if "NowPlayingQueue" in playstate:
                res["NowPlayingQueue"] = playstate["NowPlayingQueue"]
            if "PlaylistItemId" in playstate:
                res["PlaylistItemId"] = playstate["PlaylistItemId"]
            if item:
                res["NowPlayingItem"] = item
            return res

        def on_playstate(state, payload=None, item=None):
            pl_event_queue.put(wrap_playstate(True, payload, item))
            if state == "stopped":
                pl_event_queue.put(wrap_playstate(False))

        def it_on_event(name, event):
            server_id = event["ServerId"]
            if type(event) is dict and "value" in event and len(event) == 2:
                event = event["value"]
            pl_event_queue.put(
                {
                    "dest": "ws",
                    "MessageType": name,
                    "Data": event,
                    "ServerId": server_id,
                }
            )

        playerManager.on_playstate = on_playstate
        eventHandler.it_on_event = it_on_event
        eventHandler.it_event_set = {
            "ActivityLogEntry",
            "LibraryChanged",
            "PackageInstallationCancelled",
            "PackageInstallationCompleted",
            "PackageInstallationFailed",
            "PackageInstalling",
            "RefreshProgress",
            "RestartRequired",
            "ScheduledTasksInfo",
            "SeriesTimerCancelled",
            "SeriesTimerCancelled",
            "SeriesTimerCreated",
            "SeriesTimerCreated",
            "ServerRestarting",
            "ServerShuttingDown",
            "Sessions",
            "TimerCancelled",
            "TimerCreated",
            "UserDataChanged",
        }

        @app.after_request
        def add_header(response):
            if request.path == "/index.html":
                do_not_cache(response)
                client_data = base64.b64encode(
                    json.dumps(
                        {
                            "appName": USER_APP_NAME,
                            "appVersion": CLIENT_VERSION,
                            "deviceName": settings.player_name,
                            "deviceId": settings.client_uuid,
                        }
                    ).encode("ascii")
                )
                # We need access to this data before we can make an async web call.
                replacement = (
                    b"""<body><script type="application/json" id="clientData">%s</script>"""
                    % client_data
                )
                if settings.desktop_scale != 1.0:
                    f_scale = float(settings.desktop_scale)
                    replacement = replacement + (
                        b"""<style>body { zoom: %.2f; }</style>""" % f_scale
                    )
                response.make_sequence()
                response.set_data(response.get_data().replace(b"<body>", replacement,))

                return response
            if not response.cache_control.no_store:
                response.cache_control.max_age = 2592000
            return response

        @app.route("/mpv_shim_session", methods=["POST"])
        def mpv_shim_session():
            nonlocal last_server_id, last_user_id, last_user_name
            if request.headers["Content-Type"] != "application/json; charset=UTF-8":
                return "Go Away"
            req = request.json
            log.info(
                "Recieved session for server: {0}, user: {1}".format(
                    req["Name"], req["username"]
                )
            )
            if req["Id"] not in clientManager.clients:
                is_logged_in = clientManager.connect_client(req)
                log.info("Connection was successful.")
            else:
                is_logged_in = True
                log.info("Ignoring as client already exists.")
            last_server_id = req["Id"]
            last_user_id = req["UserId"]
            last_user_name = req["username"]
            resp = jsonify({"success": is_logged_in})
            resp.status_code = 200 if is_logged_in else 500
            do_not_cache(resp)
            return resp

        @app.route("/mpv_shim_event", methods=["POST"])
        def mpv_shim_event():
            if request.headers["Content-Type"] != "application/json; charset=UTF-8":
                return "Go Away"
            try:
                queue_item = pl_event_queue.get(timeout=5)
            except Empty:
                queue_item = {}
            resp = jsonify(queue_item)
            resp.status_code = 200
            do_not_cache(resp)
            return resp

        @app.route("/mpv_shim_message", methods=["POST"])
        def mpv_shim_message():
            if request.headers["Content-Type"] != "application/json; charset=UTF-8":
                return "Go Away"
            req = request.json
            client = clientManager.clients.get(req["payload"]["ServerId"])
            resp = jsonify({})
            resp.status_code = 200
            do_not_cache(resp)
            if client is None:
                log.warning("Message recieved but no client available. Ignoring.")
                return resp
            eventHandler.handle_event(
                client, req["name"], req["payload"], from_web=True
            )
            return resp

        @app.route("/mpv_shim_wsmessage", methods=["POST"])
        def mpv_shim_wsmessage():
            if request.headers["Content-Type"] != "application/json; charset=UTF-8":
                return "Go Away"
            req = request.json
            client = clientManager.clients.get(req["ServerId"])
            resp = jsonify({})
            resp.status_code = 200
            do_not_cache(resp)
            if client is None:
                log.warning("Message recieved but no client available. Ignoring.")
                return resp
            client.wsc.send(req["name"], req.get("payload", ""))
            return resp

        @app.route("/mpv_shim_teardown", methods=["POST"])
        def mpv_shim_teardown():
            if request.headers["Content-Type"] != "application/json; charset=UTF-8":
                return "Go Away"
            log.info("Client teardown requested.")
            clientManager.stop_all_clients()
            resp = jsonify({})
            resp.status_code = 200
            do_not_cache(resp)
            return resp

        @app.route("/mpv_shim_syncplay_join", methods=["POST"])
        def mpv_shim_join():
            if request.headers["Content-Type"] != "application/json; charset=UTF-8":
                return "Go Away"
            req = request.json
            client = list(clientManager.clients.values())[0]
            playerManager.syncplay.client = client
            playerManager.syncplay.join_group(req["GroupId"])
            resp = jsonify({})
            resp.status_code = 200
            do_not_cache(resp)
            return resp

        self.srv = make_server("127.0.0.1", 18096, app, threaded=True)
        self.ctx = app.app_context()
        self.ctx.push()
        self.srv.serve_forever()


# This makes me rather uncomfortable, but there's no easy way around this other than
# importing display_mirror in helpers. Lambda needed because the 2.3 version of the JS
# api adds an argument even when not used.
class WebviewClient(object):
    def __init__(self, cef=False):
        self.open_player_menu = lambda: None
        self.server = Server()
        self.cef = cef
        self.webview = None

    def start(self):
        pass

    @staticmethod
    def login_servers():
        pass

    def get_webview(self):
        return self.webview

    def run(self):
        self.server.start()

        extra_options = {}
        if os.path.exists(remember_layout):
            with open(remember_layout) as fh:
                layout_options = json.load(fh)
            if (
                settings.desktop_keep_pos
                and layout_options.get("x")
                and layout_options.get("y")
            ):
                extra_options["x"] = layout_options["x"]
                extra_options["y"] = layout_options["y"]
            if (
                settings.desktop_keep_size
                and layout_options.get("width")
                and layout_options.get("height")
            ):
                extra_options["width"] = layout_options["width"]
                extra_options["height"] = layout_options["height"]
        else:
            # Set a reasonable window size
            extra_options.update({"width": 1280, "height": 720})
        if (
            not self.cef
            and sys.platform.startswith("win32")
            or sys.platform.startswith("cygwin")
        ):
            # I wasted half a day here. Turns out that pywebview does something that
            # breaks Jellyfin on Windows in both EdgeHTML and CEF. This kills that.
            try:
                from webview.platforms import winforms

                winforms.BrowserView.EdgeHTML.on_navigation_completed = lambda: None
            except Exception:
                pass

        # Apparently pywebview3 breaks everywhere, not just on Windows.
        if self.cef:
            try:
                from webview.platforms import cef

                cef.settings.update({"cache_path": conffile.get(APP_NAME, "cache")})
                cef.Browser.initialize = lambda self: None
            except Exception:
                pass
        try:
            from webview.platforms import cocoa

            def override_cocoa(_self, webview, _nav):
                # Add the webview to the window if it's not yet the contentView
                i = cocoa.BrowserView.get_instance("webkit", webview)

                if i:
                    if not webview.window():
                        i.window.setContentView_(webview)
                        i.window.makeFirstResponder_(webview)

            cocoa.BrowserView.BrowserDelegate.webView_didFinishNavigation_ = (
                override_cocoa
            )
        except Exception:
            pass
        try:
            from webview.platforms import gtk

            # noinspection PyUnresolvedReferences
            def override_gtk(_self, webview, _status):
                if not webview.props.opacity:
                    gtk.glib.idle_add(webview.set_opacity, 1.0)

            gtk.BrowserView.on_load_finish = override_gtk
        except Exception:
            pass
        try:
            from webview.platforms import qt

            qt.BrowserView.on_load_finished = lambda self: None
        except Exception:
            pass

        url = "http://127.0.0.1:18096/index.html"
        # Wait until the server is ready.
        while True:
            try:
                urllib.request.urlopen(url)
                break
            except Exception:
                pass
            sleep(0.1)

        # Webview needs to be run in the MainThread.
        window = webview.create_window(
            url=url,
            title="Jellyfin MPV Desktop",
            fullscreen=settings.desktop_fullscreen,
            **extra_options
        )
        if window is not None:
            self.webview = window

            def handle_close():
                x, y = window.x, window.y
                extra_options = {
                    "x": x,
                    "y": y,
                    "width": window.width,
                    "height": window.height,
                }
                with open(remember_layout, "w") as fh:
                    json.dump(extra_options, fh)

            window.closing += handle_close
            if self.cef:
                webview.start(gui="cef")
            else:
                webview.start()
        self.server.stop()

    def stop(self):
        self.server.stop()
