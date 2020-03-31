import threading
import importlib.resources
import urllib.request
from datetime import date
from werkzeug.serving import make_server
from flask import Flask, request, jsonify
from time import sleep
import os.path
import json

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

from ..clients import clientManager
from ..conf import settings
from ..constants import USER_APP_NAME
from .. import conffile
from ..constants import APP_NAME

remember_layout = conffile.get(APP_NAME, 'layout.json')

# Based on https://stackoverflow.com/questions/15562446/
class Server(threading.Thread):
    def __init__(self):
        self.srv = None

        threading.Thread.__init__(self)
    
    def stop(self):
        if (self.srv is not None):
            self.srv.shutdown()
        self.join()

    def run(self):
        with importlib.resources.path(__package__, 'webclient') as static_wc:
            app = Flask(__name__, static_url_path='',
                static_folder=static_wc)
            @app.after_request
            def add_header(response):
                if request.path == "/index.html":
                    return response
                if not response.cache_control.no_store:
                    response.cache_control.max_age = 2592000
                return response

            @app.route('/mpv_shim_callback', methods=['POST'])
            def callback():
                if request.headers['Content-Type'] != 'application/json; charset=UTF-8':
                    return "Go Away"
                server = request.json
                clientManager.remove_all_clients()
                clientManager.try_connect(credentials=[{
                    "address": server.get("ManualAddress") or server.get("LocalAddress"),
                    "Name": server["Name"],
                    "Id": server["Id"],
                    "DateLastAccessed": date.fromtimestamp(server["DateLastAccessed"]//1000)
                                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "UserId": server["UserId"],
                    "AccessToken": server["AccessToken"],
                    "Users": [{"Id": server["UserId"], "IsSignedInOffline": True}],
                    "connected": True,
                    "uuid": server["Id"]
                }])
                resp = jsonify({
                    "appName": USER_APP_NAME,
                    "deviceName": settings.player_name
                })
                resp.status_code = 200
                resp.cache_control.no_store = True
                return resp

            self.srv = make_server('127.0.0.1', 18096, app, threaded=True)
            self.ctx = app.app_context()
            self.ctx.push()
            self.srv.serve_forever()

# This makes me rather uncomfortable, but there's no easy way around this other than importing display_mirror in helpers.
# Lambda needed because the 2.3 version of the JS api adds an argument even when not used.
class WebviewClient(object):
    def __init__(self, cef=False):
        self.open_player_menu = lambda: None
        self.server = Server()
        self.cef = cef

    def start(self):
        pass

    def login_servers(self):
        pass

    def run(self):
        self.server.start()

        extra_options = {}
        if os.path.exists(remember_layout):
            with open(remember_layout) as fh:
                extra_options = json.load(fh)
        if not self.cef and sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
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
                cef.settings.update({
                    'cache_path': conffile.get(APP_NAME, 'cache')
                })
                cef.Browser.initialize = lambda self: None
            except Exception: pass
        try:
            from webview.platforms import cocoa
            cocoa.BrowserView.BrowserDelegate.webView_didFinishNavigation_ = lambda self, webview, nav: None
        except Exception: pass
        try:
            from webview.platforms import gtk
            def override_gtk(self, webview, status):
                if not webview.props.opacity:
                    gtk.glib.idle_add(webview.set_opacity, 1.0)
            gtk.BrowserView.on_load_finish = override_gtk
        except Exception: pass
        try:
            from webview.platforms import qt
            qt.BrowserView.on_load_finished = lambda self: None
        except Exception: pass

        url = "http://127.0.0.1:18096/index.html"
        # Wait until the server is ready.
        while True:
            try:
                urllib.request.urlopen(url)
                break
            except Exception: pass
            sleep(0.1)

        # Webview needs to be run in the MainThread.
        window = webview.create_window(url=url, title="Jellyfin MPV Shim", **extra_options)
        if window is not None:
            def handle_close():
                x, y = window.x, window.y
                # For some reason it seems like X and Y are swapped?
                # https://github.com/r0x0r/pywebview/issues/480
                if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
                    x, y = y, x
                extra_options = {
                    "x": x,
                    "y": y,
                    "width": window.width,
                    "height": window.height
                }
                with open(remember_layout, "w") as fh:
                    json.dump(extra_options, fh)
            window.closing += handle_close
            if self.cef:
                webview.start(gui='cef')
            else:
                webview.start()
        self.server.stop()

    def stop(self):
        self.server.stop()

