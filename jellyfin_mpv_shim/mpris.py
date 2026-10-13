"""MPRIS media controls on Linux.

Lets media keys, headset buttons and desktop widgets control the shim, with
next/previous following the Jellyfin queue. Needs PyGObject; without it this
does nothing. Method calls are queued on the action thread. Cover art is
fetched with the auth header and handed out as a local file so the token
never ends up in a URL.
"""

import logging
import os
import sys
import threading
import time

log = logging.getLogger("mpris")

BUS_NAME = "org.mpris.MediaPlayer2.jellyfin_mpv_shim"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
DESKTOP_ENTRY = "com.github.iwalton3.jellyfin-mpv-shim"
TRACK_PREFIX = "/com/github/iwalton3/jellyfin_mpv_shim/track/"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

SEEK_TOLERANCE = 2.0
ART_KEEP = 20
ART_WIDTH = 512

INTROSPECTION = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_SIG = {
    "PlaybackStatus": "s",
    "Metadata": "a{sv}",
    "Volume": "d",
    "CanGoNext": "b",
    "CanGoPrevious": "b",
    "CanPlay": "b",
    "CanPause": "b",
    "CanSeek": "b",
}
_META_SIG = {
    "mpris:trackid": "o",
    "mpris:length": "x",
    "mpris:artUrl": "s",
    "xesam:title": "s",
    "xesam:album": "s",
    "xesam:artist": "as",
    "xesam:albumArtist": "as",
    "xesam:trackNumber": "i",
}


def _us(seconds):
    return int(round(float(seconds or 0.0) * 1000000))


def track_path(item_id):
    """A valid D-Bus object path for an item id."""
    safe = "".join(c if c.isalnum() or c == "_" else "_"
                   for c in str(item_id or ""))
    return TRACK_PREFIX + (safe or "unknown")


def art_source(item):
    """``(item_id, tag)`` of the item's, album's or series' Primary image."""
    tags = item.get("ImageTags") or {}
    if tags.get("Primary") and item.get("Id"):
        return item["Id"], tags["Primary"]
    if item.get("AlbumId") and item.get("AlbumPrimaryImageTag"):
        return item["AlbumId"], item["AlbumPrimaryImageTag"]
    if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
        return item["SeriesId"], item["SeriesPrimaryImageTag"]
    return None


def metadata_for(snapshot, item, art_path=None):
    """MPRIS Metadata for a playstate snapshot."""
    if not snapshot or snapshot.get("stopped"):
        return {"mpris:trackid": NO_TRACK}
    item = item or {}
    meta = {
        "mpris:trackid": track_path(snapshot.get("id") or item.get("Id")),
        "xesam:title": snapshot.get("title") or item.get("Name") or "",
    }
    duration = snapshot.get("duration")
    if duration:
        meta["mpris:length"] = _us(duration)
    artists = list(item.get("Artists") or [])
    if not artists and snapshot.get("series_name"):
        artists = [snapshot["series_name"]]
    if artists:
        meta["xesam:artist"] = artists
    album_artist = item.get("AlbumArtist")
    if album_artist:
        meta["xesam:albumArtist"] = [album_artist]
    album = snapshot.get("album") or item.get("Album")
    if album:
        meta["xesam:album"] = album
    if isinstance(item.get("IndexNumber"), int):
        meta["xesam:trackNumber"] = item["IndexNumber"]
    if art_path:
        meta["mpris:artUrl"] = "file://" + art_path
    return meta


def player_props(snapshot, metadata):

    stopped = not snapshot or snapshot.get("stopped")
    if stopped:
        status = "Stopped"
    elif snapshot.get("paused"):
        status = "Paused"
    else:
        status = "Playing"
    playing = not stopped
    return {
        "PlaybackStatus": status,
        "Metadata": metadata,
        "Volume": (0.0 if stopped else
                   max(0.0, float(snapshot.get("volume", 100)) / 100.0)),
        "CanGoNext": playing,
        "CanGoPrevious": playing,
        "CanPlay": playing,
        "CanPause": playing,
        "CanSeek": playing and not snapshot.get("is_photo"),
    }


class SeekDetector:
    """Tells a seek apart from normal playback progress."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._key = None
        self._pos = None
        self._paused = True
        self._at = None

    def feed(self, key, position, paused):
        now = self._clock()
        jumped = False
        if key == self._key and self._pos is not None:
            expected = self._pos
            if not self._paused:
                expected += now - self._at
            jumped = abs(position - expected) > SEEK_TOLERANCE
        self._key, self._pos, self._paused, self._at = (
            key, position, paused, now)
        return jumped


class Controls:


    def __init__(self, player_manager):
        self.pm = player_manager

    def _task(self, func, *args):
        self.pm.put_task(func, *args)

    def play(self):
        self._task(self.pm.play_if_paused)

    def pause(self):
        self._task(self.pm.pause_if_playing)

    def play_pause(self):
        self._task(self.pm.toggle_pause)

    def stop(self):
        self._task(self.pm.stop)

    def next(self):
        # Honours media_key_seek.
        self._task(self.pm._on_media_next)

    def previous(self):
        self._task(self.pm._on_media_prev)

    def seek(self, offset_us):
        self._task(self.pm.seek, offset_us / 1000000.0)

    def set_position(self, position_us):
        self._task(self.pm.seek, position_us / 1000000.0, True)

    def set_volume(self, fraction):
        pct = max(0.0, min(1.5, float(fraction))) * 100.0
        self._task(self.pm.set_volume, pct)

    def position_us(self):
        try:
            player = self.pm._player
            if player is None or player.playback_abort:
                return 0
            return _us(player.playback_time)
        except Exception:
            return None


class MprisService:


    def __init__(self, controls=None, bus=None, fetch_art=None):
        self.controls = controls
        self.bus = bus
        self.fetch_art = fetch_art
        self._lock = threading.Lock()
        self._props = player_props(None, metadata_for(None, None))
        self._snapshot = None
        self._item = None
        self._art = {}
        self._seek = SeekDetector()

    def publish(self, snapshot, video=None):
        try:
            self._publish(snapshot, video)
        except Exception:
            log.debug("MPRIS publish failed", exc_info=True)

    def _publish(self, snapshot, video):
        item = (getattr(video, "item", None) or {}) if video else {}
        stopped = not snapshot or snapshot.get("stopped")
        art_path = None
        if not stopped:
            art_path = self._art_for(item, video)
        meta = metadata_for(snapshot, item, art_path)
        props = player_props(snapshot, meta)
        with self._lock:
            changed = {k: v for k, v in props.items()
                       if self._props.get(k) != v}
            self._props = props
            self._snapshot = snapshot
            self._item = item
            jumped = False
            if not stopped:
                jumped = self._seek.feed(
                    meta["mpris:trackid"],
                    float(snapshot.get("position") or 0.0),
                    bool(snapshot.get("paused")))
        if changed and self.bus is not None:
            self.bus.emit_changed(changed)
        if jumped and self.bus is not None:
            self.bus.emit("Seeked", "(x)",
                          (_us(snapshot.get("position")),))

    def _art_for(self, item, video):
        source = art_source(item)
        if source is None:
            return None
        path = self._art.get(source)
        if path is not None:
            return path
        if self.fetch_art is not None and source not in self._art:
            self._art[source] = None
            client = getattr(video, "client", None)
            threading.Thread(target=self._fetch, args=(source, client),
                             name="mpris-art", daemon=True).start()
        return None

    def _fetch(self, source, client):
        path = None
        try:
            path = self.fetch_art(client, *source)
        except Exception:
            log.debug("MPRIS cover fetch failed", exc_info=True)
        if not path:
            return
        with self._lock:
            self._art[source] = path
            snapshot, item = self._snapshot, self._item
        if snapshot and not snapshot.get("stopped") \
                and art_source(item or {}) == source:
            self.publish(snapshot, _Held(item))

    def get(self, iface, prop):
        if iface == ROOT_IFACE:
            return {
                "CanQuit": False,
                "CanRaise": False,
                "HasTrackList": False,
                "Identity": "Jellyfin MPV Shim",
                "DesktopEntry": DESKTOP_ENTRY,
                "SupportedUriSchemes": [],
                "SupportedMimeTypes": [],
            }.get(prop)
        if prop == "Position":
            pos = self.controls.position_us() if self.controls else None
            if pos is None:
                with self._lock:
                    snap = self._snapshot or {}
                pos = _us(snap.get("position"))
            return pos
        if prop in ("Rate", "MinimumRate", "MaximumRate"):
            return 1.0
        if prop == "CanControl":
            return True
        with self._lock:
            return self._props.get(prop)

    def call(self, method, args):
        c = self.controls
        if c is None:
            return True
        if method == "Play":
            c.play()
        elif method == "Pause":
            c.pause()
        elif method == "PlayPause":
            c.play_pause()
        elif method == "Stop":
            c.stop()
        elif method == "Next":
            c.next()
        elif method == "Previous":
            c.previous()
        elif method == "Seek":
            c.seek(int(args[0]))
        elif method == "SetPosition":
            track, position = args
            with self._lock:
                current = self._props["Metadata"].get("mpris:trackid")
            # Per spec, ignore a stale TrackId.
            if track == current and position >= 0:
                c.set_position(int(position))
        elif method in ("Raise", "Quit", "OpenUri"):
            pass
        else:
            return False
        return True

    def set(self, prop, value):
        if prop == "Volume" and self.controls is not None:
            self.controls.set_volume(value)


class _Held:
    """Carries ``item`` for re-publishing from the art thread."""

    def __init__(self, item):
        self.item = item
        self.client = None


# -- Gio -------------------------------------------------------------------


def _variant(GLib, sig, value):
    if sig == "a{sv}":
        return GLib.Variant("a{sv}", {
            k: GLib.Variant(_META_SIG.get(k, "s"), v)
            for k, v in value.items()})
    return GLib.Variant(sig, value)


_ROOT_SIG = {
    "CanQuit": "b", "CanRaise": "b", "HasTrackList": "b", "Identity": "s",
    "DesktopEntry": "s", "SupportedUriSchemes": "as",
    "SupportedMimeTypes": "as",
}
_PLAYER_SIG = dict(_SIG, Position="x", Rate="d", MinimumRate="d",
                   MaximumRate="d", CanControl="b")


class _GioBus:
    def __init__(self, service):
        self.service = service
        self.conn = None
        self.loop = None
        self.context = None
        self._reg = []
        self._owner = None
        self.thread = None

    def start(self):
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        ready = threading.Event()
        failure = []

        def run():
            try:
                self.context = GLib.MainContext.new()
                self.context.push_thread_default()
                self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
                for iface in node.interfaces:
                    self._reg.append(self.conn.register_object(
                        OBJECT_PATH, iface, self._on_call, self._on_get,
                        self._on_set))
                self._owner = Gio.bus_own_name_on_connection(
                    self.conn, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                    None, None)
                self.loop = GLib.MainLoop.new(self.context, False)
            except Exception as exc:
                failure.append(exc)
                ready.set()
                return
            ready.set()
            self.loop.run()

        self.thread = threading.Thread(target=run, name="mpris",
                                       daemon=True)
        self.thread.start()
        ready.wait(5)
        if failure:
            raise failure[0]
        if self.loop is None:
            raise RuntimeError("MPRIS bus did not come up")

    def stop(self):
        try:
            if self._owner is not None:
                self.Gio.bus_unown_name(self._owner)
            for reg in self._reg:
                self.conn.unregister_object(reg)
        except Exception:
            log.debug("MPRIS unregister failed", exc_info=True)
        if self.loop is not None:
            self.loop.quit()
        if self.thread is not None:
            self.thread.join(2)

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            args = params.unpack() if params is not None else ()
            if iface == PLAYER_IFACE or iface == ROOT_IFACE:
                handled = self.service.call(method, args)
            else:
                handled = False
        except Exception:
            log.debug("MPRIS method %s failed", method, exc_info=True)
            invocation.return_dbus_error(
                "org.mpris.MediaPlayer2.Error.Failed", method)
            return
        if handled:
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method)

    def _on_get(self, conn, sender, path, iface, prop):
        sigs = _ROOT_SIG if iface == ROOT_IFACE else _PLAYER_SIG
        sig = sigs.get(prop)
        value = self.service.get(iface, prop)
        if sig is None or value is None:
            return None
        return _variant(self.GLib, sig, value)

    def _on_set(self, conn, sender, path, iface, prop, value):
        try:
            self.service.set(prop, value.unpack())
        except Exception:
            log.debug("MPRIS set %s failed", prop, exc_info=True)
        return True

    def emit_changed(self, changed):
        GLib = self.GLib
        body = GLib.Variant("(sa{sv}as)", (
            PLAYER_IFACE,
            {k: _variant(GLib, _SIG[k], v) for k, v in changed.items()},
            []))
        self.conn.emit_signal(None, OBJECT_PATH,
                              "org.freedesktop.DBus.Properties",
                              "PropertiesChanged", body)

    def emit(self, member, signature, args):
        self.conn.emit_signal(None, OBJECT_PATH, PLAYER_IFACE, member,
                              self.GLib.Variant(signature, args))


# -- cover art -------------------------------------------------------------


def _art_dir():
    from .conffile import cachedir
    from .constants import APP_NAME

    return os.path.join(cachedir(APP_NAME), "mpris")


def fetch_art(client, item_id, tag, directory=None, get=None):
    """Download an item's Primary image and return the file path."""
    from .utils import same_origin

    directory = directory or _art_dir()
    safe = "".join(c for c in f"{item_id}_{tag}" if c.isalnum() or c == "_")
    path = os.path.join(directory, safe + ".jpg")
    if os.path.exists(path):
        return path
    if client is None:
        return None
    url = client.jellyfin.artwork(item_id, "Primary", ART_WIDTH,
                                  include_apikey=False)
    server = client.config.data.get("auth.server", "") or ""
    headers = {}
    if same_origin(url, server):
        headers["Authorization"] = client.http._get_authenication_header()
    if get is None:
        import requests

        get = requests.get
    resp = get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(resp.content)
    os.replace(tmp, path)
    _prune(directory)
    return path


def _prune(directory, keep=ART_KEEP):
    try:
        files = [os.path.join(directory, f) for f in os.listdir(directory)
                 if f.endswith(".jpg")]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        log.debug("MPRIS art prune failed", exc_info=True)


# -- the module's single instance -------------------------------------------

_service = None
_bus = None


def start(player_manager):
    """Start MPRIS. Returns whether it is running."""
    global _service, _bus
    if _service is not None:
        return True
    if not sys.platform.startswith("linux"):
        return False
    service = MprisService(Controls(player_manager), fetch_art=fetch_art)
    bus = _GioBus(service)
    try:
        bus.start()
    except Exception:
        log.info("MPRIS media controls unavailable.", exc_info=True)
        return False
    service.bus = bus
    _service, _bus = service, bus
    log.info("MPRIS media controls registered as %s.", BUS_NAME)
    return True


def publish(snapshot, video=None):
    service = _service
    if service is not None:
        service.publish(snapshot, video)


def stop():
    global _service, _bus
    bus, _bus, _service = _bus, None, None
    if bus is not None:
        bus.stop()
