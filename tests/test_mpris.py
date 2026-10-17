"""Tests for the MPRIS service, with a recorder in place of the bus."""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import mpris


class RecorderBus:

    def __init__(self):
        self.changed = []
        self.signals = []

    def emit_changed(self, changed):
        self.changed.append(dict(changed))

    def emit(self, member, signature, args):
        self.signals.append((member, signature, args))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakePM:

    def __init__(self):
        self.tasks = []
        self.player_reads = []
        pm = self

        class Player:
            # Any read is recorded: the bus thread must never touch mpv.
            def __getattr__(self, name):
                pm.player_reads.append(name)
                return 0.0

        self._player = Player()

    def put_task(self, func, *args):
        self.tasks.append((func.__name__, args))

    def play_if_paused(self): ...
    def pause_if_playing(self): ...
    def toggle_pause(self): ...
    def stop(self): ...
    def _on_media_next(self): ...
    def _on_media_prev(self): ...
    def seek(self, *a): ...
    def set_volume(self, *a): ...


class Video:
    def __init__(self, item, client=None):
        self.item = item
        self.client = client


def snap(item_id="a1", pos=0.0, paused=False, **kw):
    d = {"stopped": False, "id": item_id, "title": "T " + item_id,
         "album": "Album", "position": pos, "duration": 300.0,
         "paused": paused, "volume": 100}
    d.update(kw)
    return d


def service(clock=None):
    pm = FakePM()
    if clock is None:
        svc = mpris.MprisService(mpris.Controls(pm), RecorderBus())
    else:
        svc = mpris.MprisService(mpris.Controls(pm), RecorderBus(),
                                 clock=clock)
    return svc, pm


class MetadataTest(unittest.TestCase):
    def test_stopped_is_no_track(self):
        self.assertEqual(mpris.metadata_for({"stopped": True}, {}),
                         {"mpris:trackid": mpris.NO_TRACK})

    def test_song(self):
        meta = mpris.metadata_for(
            snap(), {"Artists": ["A", "B"], "AlbumArtist": "AA",
                     "IndexNumber": 3}, "/x/c.jpg")
        self.assertEqual(meta["xesam:artist"], ["A", "B"])
        self.assertEqual(meta["xesam:albumArtist"], ["AA"])
        self.assertEqual(meta["xesam:trackNumber"], 3)
        self.assertEqual(meta["mpris:length"], 300000000)
        self.assertEqual(meta["mpris:artUrl"], "file:///x/c.jpg")

    def test_episode_falls_back_to_series_as_artist(self):
        meta = mpris.metadata_for(snap(series_name="Show"), {})
        self.assertEqual(meta["xesam:artist"], ["Show"])

    def test_track_path_is_always_a_valid_object_path(self):
        self.assertTrue(mpris.track_path("ab-c.d/e").endswith("/ab_c_d_e"))
        self.assertTrue(mpris.track_path(None).endswith("/unknown"))

    def test_art_source_prefers_own_then_album_then_series(self):
        self.assertEqual(mpris.art_source(
            {"Id": "i", "ImageTags": {"Primary": "t"}, "AlbumId": "al",
             "AlbumPrimaryImageTag": "at"}), ("i", "t"))
        self.assertEqual(mpris.art_source(
            {"Id": "i", "AlbumId": "al", "AlbumPrimaryImageTag": "at"}),
            ("al", "at"))
        self.assertEqual(mpris.art_source(
            {"SeriesId": "s", "SeriesPrimaryImageTag": "st"}), ("s", "st"))
        self.assertIsNone(mpris.art_source({"Id": "i"}))


class PublishTest(unittest.TestCase):
    def test_only_what_changed_is_announced_across_a_session(self):
        clock = Clock()
        svc, _ = service(clock)
        bus = svc.bus
        v = Video({"Id": "a1"})
        svc.publish(snap(pos=0.0), v)
        self.assertEqual(bus.changed[-1]["PlaybackStatus"], "Playing")
        for i in range(1, 4):
            clock.now += 5
            svc.publish(snap(pos=5.0 * i), v)
        self.assertEqual(len(bus.changed), 1, bus.changed)
        svc.publish(snap(pos=15.0, paused=True), v)
        self.assertEqual(bus.changed[-1], {"PlaybackStatus": "Paused"})
        for _ in range(3):
            clock.now += 5
            svc.publish(snap(pos=15.0, paused=True), v)
        self.assertEqual(len(bus.changed), 2)
        svc.publish({"stopped": True}, None)
        self.assertEqual(bus.changed[-1]["PlaybackStatus"], "Stopped")
        self.assertEqual(bus.changed[-1]["Metadata"],
                         {"mpris:trackid": mpris.NO_TRACK})
        self.assertFalse(bus.changed[-1]["CanGoNext"])
        self.assertEqual(bus.signals, [])

    def test_a_jump_is_seeked_and_playback_is_not(self):
        clock = Clock()
        svc, _ = service(clock)
        v = Video({"Id": "a1"})
        for i in range(4):
            svc.publish(snap(pos=10.0 * i), v)
            clock.now += 10
        self.assertEqual(svc.bus.signals, [])
        svc.publish(snap(pos=200.0), v)
        self.assertEqual(svc.bus.signals, [("Seeked", "(x)", (200000000,))])

    def test_the_next_track_starting_is_not_a_seek(self):
        clock = Clock()
        svc, _ = service(clock)
        svc.publish(snap("a1", pos=290.0), Video({"Id": "a1"}))
        clock.now += 1
        svc.publish(snap("a2", pos=0.0), Video({"Id": "a2"}))
        self.assertEqual(svc.bus.signals, [])
        self.assertTrue(svc.bus.changed[-1]["Metadata"]
                        ["mpris:trackid"].endswith("/a2"))

    def test_publish_never_raises(self):
        svc, _ = service()
        svc.bus.emit_changed = mock.Mock(side_effect=RuntimeError)
        svc.publish(snap(), Video({}))

    def test_art_arrives_later_and_republishes(self):
        fetched = []

        def fetch(client, item_id, tag):
            fetched.append((item_id, tag))
            return "/c/%s.jpg" % item_id

        svc, _ = service()
        svc.fetch_art = fetch
        item = {"Id": "a1", "ImageTags": {"Primary": "t"}}
        with mock.patch.object(mpris.threading, "Thread") as T:
            svc.publish(snap(), Video(item))
            target = T.call_args.kwargs["target"]
            args = T.call_args.kwargs["args"]
        self.assertNotIn("mpris:artUrl", svc.bus.changed[-1]["Metadata"])
        target(*args)
        self.assertEqual(svc.bus.changed[-1]["Metadata"]["mpris:artUrl"],
                         "file:///c/a1.jpg")
        for _ in range(3):
            svc.publish(snap(), Video(item))
        self.assertEqual(fetched, [("a1", "t")])


class ControlsTest(unittest.TestCase):
    def test_every_method_goes_through_the_action_thread(self):
        svc, pm = service()
        svc.publish(snap("a1"), Video({"Id": "a1"}))
        track = mpris.track_path("a1")
        for method, args in (("Play", ()), ("Pause", ()), ("PlayPause", ()),
                             ("Stop", ()), ("Next", ()), ("Previous", ()),
                             ("Seek", (-10000000,)),
                             ("SetPosition", (track, 60000000))):
            self.assertTrue(svc.call(method, args))
        self.assertEqual(pm.tasks, [
            ("play_if_paused", ()), ("pause_if_playing", ()),
            ("toggle_pause", ()), ("stop", ()), ("_on_media_next", ()),
            ("_on_media_prev", ()), ("seek", (-10.0,)),
            ("seek", (60.0, True))])

    def test_set_position_for_another_track_is_ignored(self):
        svc, pm = service()
        svc.publish(snap("a2"), Video({"Id": "a2"}))
        svc.call("SetPosition", (mpris.track_path("a1"), 60000000))
        self.assertEqual(pm.tasks, [])

    def test_unknown_method_is_refused(self):
        svc, _ = service()
        self.assertFalse(svc.call("Explode", ()))

    def _position(self, svc):
        return svc.get(mpris.PLAYER_IFACE, "Position")

    def test_position_never_reads_the_player(self):
        """`Position` is answered on the bus thread, and python-mpv's
        terminate() clears the handle before the core is flagged shut down:
        a read in that window hands NULL to mpv_get_property, a segfault no
        except can catch. The snapshot is the only safe source."""
        clock = Clock()
        svc, pm = service(clock)
        svc.publish(snap(pos=12.5), Video({"Id": "a1"}))
        for _ in range(3):
            clock.now += 1
            self._position(svc)
        svc.publish({"stopped": True}, None)
        self._position(svc)
        self.assertEqual(pm.player_reads, [])

    def test_position_follows_the_clock_while_playing(self):
        clock = Clock()
        svc, _ = service(clock)
        svc.publish(snap(pos=12.5), Video({"Id": "a1"}))
        for step in range(1, 4):
            clock.now += 2
            self.assertEqual(self._position(svc), 12500000 + step * 2000000)

    def test_position_holds_while_paused(self):
        clock = Clock()
        svc, _ = service(clock)
        svc.publish(snap(pos=40.0, paused=True), Video({"Id": "a1"}))
        for _ in range(3):
            clock.now += 5
            self.assertEqual(self._position(svc), 40000000)

    def test_position_is_capped_at_the_duration(self):
        """A snapshot that stops arriving -- the end of the file -- must not
        run the position past the track's length."""
        clock = Clock()
        svc, _ = service(clock)
        svc.publish(snap(pos=298.0), Video({"Id": "a1"}))
        clock.now += 30
        self.assertEqual(self._position(svc), 300000000)

    def test_position_is_zero_when_stopped(self):
        svc, _ = service(Clock())
        self.assertEqual(self._position(svc), 0)
        svc.publish(snap(pos=12.5), Video({"Id": "a1"}))
        svc.publish({"stopped": True}, None)
        self.assertEqual(self._position(svc), 0)

    def test_volume_write_is_clamped_and_queued(self):
        svc, pm = service()
        svc.set("Volume", 9.0)
        self.assertEqual(pm.tasks, [("set_volume", (150.0,))])


class FetchArtTest(unittest.TestCase):
    def _client(self, server="https://jf.example"):
        c = mock.Mock()
        c.config.data = {"auth.server": server}
        c.jellyfin.artwork.return_value = (
            "https://jf.example/Items/i/Images/Primary?MaxWidth=512")
        c.http._get_authenication_header.return_value = 'MediaBrowser Token="s"'
        return c

    def test_header_not_token_and_file_reused(self):
        calls = []

        def get(url, headers, timeout):
            calls.append((url, headers))
            return mock.Mock(content=b"jpg", raise_for_status=lambda: None)

        client = self._client()
        with tempfile.TemporaryDirectory() as d:
            for _ in range(3):
                path = mpris.fetch_art(client, "i", "t", directory=d, get=get)
            self.assertTrue(os.path.exists(path))
        self.assertEqual(len(calls), 1)
        client.jellyfin.artwork.assert_called_with(
            "i", "Primary", mpris.ART_WIDTH, include_apikey=False)
        self.assertEqual(calls[0][1],
                         {"Authorization": 'MediaBrowser Token="s"'})

    def test_no_header_to_another_origin(self):
        seen = []
        client = self._client(server="https://elsewhere.example")
        with tempfile.TemporaryDirectory() as d:
            mpris.fetch_art(client, "i", "t", directory=d,
                            get=lambda u, headers, timeout: seen.append(
                                headers) or mock.Mock(
                                    content=b"x",
                                    raise_for_status=lambda: None))
        self.assertEqual(seen, [{}])

    def test_a_failed_write_leaves_no_temp_file(self):
        client = self._client()
        get = lambda u, headers, timeout: mock.Mock(
            content=b"jpg", raise_for_status=lambda: None)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mpris.os, "replace",
                                   side_effect=OSError("in use")):
                with self.assertRaises(OSError):
                    mpris.fetch_art(client, "i", "t", directory=d, get=get)
            self.assertEqual(os.listdir(d), [])

    def test_prune_sweeps_stale_temp_files_but_not_fresh_ones(self):
        """A fresh one may be another art thread's write in progress."""
        with tempfile.TemporaryDirectory() as d:
            stale = os.path.join(d, "a_t.jpg.123.456.tmp")
            fresh = os.path.join(d, "b_t.jpg.123.789.tmp")
            for p in (stale, fresh):
                open(p, "wb").close()
            old = os.path.getmtime(fresh) - mpris.TEMP_MAX_AGE - 60
            os.utime(stale, (old, old))
            mpris._prune(d)
            self.assertEqual(os.listdir(d), [os.path.basename(fresh)])

    def test_prune_keeps_the_newest(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(mpris.ART_KEEP + 5):
                p = os.path.join(d, "%d.jpg" % i)
                open(p, "wb").close()
                os.utime(p, (i, i))
            mpris._prune(d)
            left = sorted(int(f[:-4]) for f in os.listdir(d))
        self.assertEqual(left, list(range(5, mpris.ART_KEEP + 5)))


class StartTest(unittest.TestCase):
    def tearDown(self):
        mpris._service = mpris._bus = None

    def test_without_pygobject_it_stays_off(self):
        with mock.patch.dict(sys.modules, {"gi": None}), \
                mock.patch.object(mpris.sys, "platform", "linux"):
            self.assertFalse(mpris.start(FakePM()))
        self.assertIsNone(mpris._service)
        mpris.publish(snap(), None)
        mpris.stop()

    def test_off_linux_it_stays_off(self):
        with mock.patch.object(mpris.sys, "platform", "win32"):
            self.assertFalse(mpris.start(FakePM()))


class BusNameTest(unittest.TestCase):
    """Flatpak lets an app own `org.mpris.MediaPlayer2.<app-id>.*` with no
    permission, so naming the bus after the app id needs no `--own-name`
    (Flathub review). The cost, accepted: playerctl and Waybar take the text
    before the first period as the player name, so they call it `com`."""

    def manifest(self):
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "flatpak",
                               "com.github.iwalton3.jellyfin-mpv-shim.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_the_name_is_the_flatpak_app_id(self):
        self.assertEqual(mpris.BUS_NAME,
                         "org.mpris.MediaPlayer2." + self.manifest()["app-id"])

    def test_the_manifest_does_not_ask_for_it(self):
        own = [a for a in self.manifest()["finish-args"]
               if a.startswith("--own-name=org.mpris.")]
        self.assertEqual(own, [])


if __name__ == "__main__":
    unittest.main()
