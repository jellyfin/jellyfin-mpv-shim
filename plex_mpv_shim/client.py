import datetime
import json
import logging
import os
import posixpath
import threading
import urllib.request, urllib.parse, urllib.error
import urllib.parse
import socket

from http.server import HTTPServer
from http.server import SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from .utils import upd_token
from .conf import settings

try:
    from xml.etree import cElementTree as et
except:
    from xml.etree import ElementTree as et

from io import BytesIO

from .conf import settings
from .media import Media
from .player import playerManager
from .subscribers import remoteSubscriberManager, RemoteSubscriber
from .timeline import timelineManager

log = logging.getLogger("client")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

NAVIGATION_DICT = {
    "/player/navigation/moveDown": "down",
    "/player/navigation/moveUp": "up",
    "/player/navigation/select": "ok",
    "/player/navigation/moveLeft": "left",
    "/player/navigation/moveRight": "right",
    "/player/navigation/home": "home",
    "/player/navigation/back": "back"
}

class HttpHandler(SimpleHTTPRequestHandler):
    xmlOutput   = None
    completed   = False
    
    handlers    = (
        (("/resources",),                       "resources"),
        (("/player/playback/playMedia",
          "/player/application/playMedia",),    "playMedia"),
        (("/player/playback/stepForward",
          "/player/playback/stepBack",),        "stepFunction"),
        (("/player/playback/skipNext",),        "skipNext"),
        (("/player/playback/skipPrevious",),    "skipPrevious"),
        (("/player/playback/stop",),            "stop"),
        (("/player/playback/seekTo",),          "seekTo"),
        (("/player/playback/skipTo",),          "skipTo"),
        (("/player/playback/setParameters",),   "set"),
        (("/player/playback/setStreams",),      "setStreams"),
        (("/player/playback/pause",
          "/player/playback/play",),            "pausePlay"),
        (("/player/timeline/subscribe",),       "subscribe"),
        (("/player/timeline/unsubscribe",),     "unsubscribe"),
        (("/player/timeline/poll",),            "poll"),
        (("/player/application/setText",
          "/player/application/sendString",),   "sendString"),
        (("/player/application/sendVirtualKey",
          "/player/application/sendKey",),      "sendVKey"),
        (("/player/playback/bigStepForward",
          "/player/playback/bigStepBack",),     "stepFunction"),
        (("/player/playback/refreshPlayQueue",),"refreshPlayQueue"),
        (("/player/mirror/details",),           "mirror"),
    )

    def log_request(self, *args, **kwargs):
        pass

    def setStandardResponse(self, code=200, status="OK"):
        el = et.Element("Response")
        el.set("code",      str(code))
        el.set("status",    str(status))

        if self.xmlOutput:
            self.xmlOutput.append(el)
        else:
            self.xmlOutput = el

    def getSubFromRequest(self, arguments):
        uuid = self.headers.get("X-Plex-Client-Identifier", None)
        name = self.headers.get("X-Plex-Device-Name",  None)
        if not name:
            name = arguments.get("X-Plex-Device-Name")

        if not uuid:
            log.warning("HttpHandler::getSubFromRequest subscriber didn't set X-Plex-Client-Identifier")
            self.setStandardResponse(500, "subscriber didn't set X-Plex-Client-Identifier")
            return

        if not name:
            log.warning("HttpHandler::getSubFromRequest subscriber didn't set X-Plex-Device-Name")
            self.setStandardResponse(500, "subscriber didn't set X-Plex-Device-Name")
            return

        port        = int(arguments.get("port", 32400))
        commandID   = int(arguments.get("commandID", -1))
        protocol    = arguments.get("protocol", "http")
        ipaddress   = self.client_address[0]

        return RemoteSubscriber(uuid, commandID, ipaddress, port, protocol, name)

    def get_querydict(self, query):
        querydict = {}
        for key, value in urllib.parse.parse_qsl(query):
            querydict[key] = value
        return querydict

    def updateCommandID(self, arguments):
        if "commandID" not in arguments:
            if self.path.find("unsubscribe") < 0:
                log.warning("HttpHandler::updateCommandID no commandID sent to this request!")
            return

        commandID = -1
        try:
            commandID = int(arguments["commandID"])
        except:
            log.error("HttpHandler::updateCommandID invalid commandID: %s" % arguments["commandID"])
            return

        uuid = self.headers.get("X-Plex-Client-Identifier", None)
        if not uuid:
            log.warning("HttpHandler::updateCommandID subscriber didn't set X-Plex-Client-Identifier")
            self.setStandardResponse(500, "When commandID is set you also need to specify X-Plex-Client-Identifier")
            return

        sub = remoteSubscriberManager.findSubscriberByUUID(uuid)
        if sub:
            sub.commandID = commandID

    def handle_request(self, method):
        if 'X-Plex-Device-Name' in self.headers:
            log.debug("HttpHandler::handle_request request from '%s' to '%s'" % (self.headers["X-Plex-Device-Name"], self.path))
        else:
            log.debug("HttpHandler::handle_request request to '%s'" % self.path)

        path  = urllib.parse.urlparse(self.path)
        query = self.get_querydict(path.query)

        if method == "OPTIONS" and "Access-Control-Request-Method" in self.headers:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS, DELETE, PUT, HEAD")
            self.send_header("Access-Control-Max-Age", "1209600")
            self.send_header("Connection", "close")

            if "Access-Control-Request-Headers" in self.headers:
                self.send_header("Access-Control-Allow-Headers", self.headers["Access-Control-Request-Headers"])

            self.end_headers()
            self.wfile.flush()

            return

        self.setStandardResponse()

        self.updateCommandID(query)

        match = False
        for paths, handler in self.handlers:
            if path.path in paths:
                match = True
                getattr(self, handler)(path, query)
                break

        if not match:
            if path.path.startswith("/player/navigation"):
                self.navigation(path, query)
            else:
                self.setStandardResponse(500, "Nope, not implemented, sorry!")

        self.send_end()

    def translate_path(self, path):
        path = path.split('?',1)[0]
        path = path.split('#',1)[0]
        path = posixpath.normpath(urllib.parse.unquote(path))
        return os.path.join(STATIC_DIR, path.lstrip("/"))


    def do_OPTIONS(self):
        self.handle_request("OPTIONS")

    def do_GET(self):
        self.handle_request("GET")
    
    def send_end(self):
        if self.completed:
            return

        response = BytesIO()
        tree     = et.ElementTree(self.xmlOutput)
        tree.write(response, encoding="utf-8", xml_declaration=True)
        response.seek(0)

        xmlData = response.read()

        self.send_response(200)

        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Plex-Client-Identifier")
        self.send_header("X-Plex-Client-Identifier",    settings.client_uuid)
        self.send_header("Content-type", "text/xml")
        self.send_header("Date", datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self.send_header("Content-Length", str(len(xmlData)))
        
        self.end_headers()

        self.wfile.write(xmlData)
        self.wfile.flush()

        self.completed = True

    #--------------------------------------------------------------------------
    #   URL Handlers
    #--------------------------------------------------------------------------
    def subscribe(self, path, arguments):
        sub = self.getSubFromRequest(arguments)
        if sub:
            remoteSubscriberManager.addSubscriber(sub)
            
            self.send_end()

            timelineManager.SendTimelineToSubscriber(sub)

    def unsubscribe(self, path, arguments):
        remoteSubscriberManager.removeSubscriber(self.getSubFromRequest(arguments))

    def poll(self, path, arguments):
        uuid = self.headers.get("X-Plex-Client-Identifier", None)
        name = self.headers.get("X-Plex-Device-Name", "")

        commandID = -1
        try:
            commandID = int(arguments.get("commandID", -1))
        except:
            pass

        if commandID == -1 or not uuid:
            log.warning("HttpHandler::poll the poller needs to set both X-Plex-Client-Identifier header and commandID arguments.")
            self.setStandardResponse(500, "You need to specify both x-Plex-Client-Identifier as a header and commandID as a argument")
            return

        pollSubscriber = RemoteSubscriber(uuid, commandID, name=name)
        remoteSubscriberManager.addSubscriber(pollSubscriber)

        if "wait" in arguments and arguments["wait"] in ("1", "true"):
            self.xmlOutput = timelineManager.WaitForTimeline(pollSubscriber)
        else:
            self.xmlOutput = timelineManager.GetCurrentTimeLinesXML(pollSubscriber)

    def resources(self, path, arguments):
        mediaContainer = et.Element("MediaContainer")
        player = et.Element("Player")

        capabilities = "timeline,playback,navigation"
        if settings.enable_play_queue:
            capabilities = "timeline,playback,navigation,playqueues"

        info = (("deviceClass",               "pc"),
                ("machineIdentifier",         settings.client_uuid),
                ("product",                   "Plex MPV Shim"),
                ("protocolCapabilities",      capabilities),
                ("protocolVersion",           "1"),
                ("title",                     settings.player_name),
                ("version",                   "1.0"))

        for key, value in info:
            player.set(key, value)

        mediaContainer.append(player)
        self.xmlOutput = mediaContainer

    def playMedia(self, path, arguments):
        address     = arguments.get("address",      None)
        protocol    = arguments.get("protocol",     "http")
        port        = arguments.get("port",         "32400")
        key         = arguments.get("key",          None)
        offset      = int(int(arguments.get("offset",   0))/1e3)
        url         = urllib.parse.urljoin("%s://%s:%s" % (protocol, address, port), key)
        playQueue   = arguments.get("containerKey", None)

        token = arguments.get("token", None)
        if token:
            upd_token(address, token)

        if settings.enable_play_queue and playQueue.startswith("/playQueue"):
            media = Media(url, play_queue=playQueue)
        else:
            media = Media(url)

        log.debug("HttpHandler::playMedia %s" % media)

        # TODO: Select video, media and part here based off user settings
        video = media.get_video(0)
        if video:
            if settings.pre_media_cmd:
                os.system(settings.pre_media_cmd)
            playerManager.play(video, offset)
            timelineManager.SendTimelineToSubscribers()

    def stop(self, path, arguments):
        playerManager.stop()
        timelineManager.SendTimelineToSubscribers()

    def pausePlay(self, path, arguments):
        playerManager.toggle_pause()
        timelineManager.SendTimelineToSubscribers()

    def skipNext(self, path, arguments):
        playerManager.play_next()

    def skipPrevious(self, path, arguments):
        playerManager.play_prev()

    def stepFunction(self, path, arguments):
        log.info("HttpHandler::stepFunction not implemented yet")

    def seekTo(self, path, arguments):
        offset = int(int(arguments.get("offset", 0))*1e-3)
        log.debug("HttpHandler::seekTo offset %ss" % offset)
        playerManager.seek(offset)

    def skipTo(self, path, arguments):
        playerManager.skip_to(arguments["key"])

    def set(self, path, arguments):
        if "volume" in arguments:
            volume = arguments["volume"]
            log.debug("HttpHandler::set settings volume to %s" % volume)
            playerManager.set_volume(float(volume)/100.0)
        if "autoPlay" in arguments:
            settings.auto_play = arguments["autoPlay"] == "1"
            settings.save()

    def setStreams(self, path, arguments):
        audioStreamID = None
        subtitleStreamID = None
        if "audioStreamID" in arguments:
            audioStreamID = arguments["audioStreamID"]
        if "subtitleStreamID" in arguments:
            subtitleStreamID = arguments["subtitleStreamID"]
        playerManager.set_streams(audioStreamID, subtitleStreamID)

    def refreshPlayQueue(self, path, arguments):
        playerManager._video.parent.upd_play_queue()
        timelineManager.SendTimelineToSubscribers()

    def mirror(self, path, arguments):
        timelineManager.delay_idle()

    def navigation(self, path, arguments):
        path = path.path
        if path in NAVIGATION_DICT:
            playerManager.menu_action(NAVIGATION_DICT[path])

class HttpSocketServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True

class HttpServer(threading.Thread):
    def __init__(self, port):
        super(HttpServer, self).__init__(name="HTTP Server")
        self.port = port
        self.sock = None
        self.addr = ('', port)
    
    def run(self):
        log.info("Started HTTP server")
        self.sock = socket.socket (socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(self.addr)
        self.sock.listen(5)

        self.servers = [HttpServerThread(i, self.sock, self.addr) for i in range(5)]

    def stop(self):
        log.info("Stopping HTTP server...")
        
        # Note: All of the server threads will die after the socket closes.
        # Attempting to stop a waiting thread prior will block indefinitely.
        self.sock.close()

# Adapted from https://stackoverflow.com/a/46224191
class HttpServerThread(threading.Thread):
    def __init__(self, i, sock, addr):
        super(HttpServerThread, self).__init__(name="HTTP Server %s" % i)

        self.i              = i
        self.daemon         = True
        self.server         = None
        self.addr           = addr
        self.sock           = sock

        self.start()

    def run(self):
        self.server = HttpSocketServer(self.addr, HttpHandler, False)
        self.server.socket = self.sock
        self.server.server_bind = self.server_close = lambda self: None
        self.server.serve_forever()


