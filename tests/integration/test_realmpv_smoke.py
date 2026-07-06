"""Tier 2: end-to-end smoke against a REAL mpv, under xvfb.

Proves the whole player state machine works with an actual decoding player: a
real clip loads (real ``duration`` observed), timeline progress posts, a genuine
``eof-reached`` auto-advances to the next clip, and the final stop is reported.

Backend matrix: run once per backend (JMS_TEST_BACKEND). libmpv decodes
in-process; jsonipc spawns the real ``mpv`` binary over a JSON IPC socket. The
same assertions must hold for both — this is where identical-behaviour matters
most, since the external-mpv path is the less-travelled one.

Deterministic-by-design choices (justified in the README):
* Media is a local ffmpeg-generated clip played via a file path — no server, no
  transcode, no network for the bytes. This is what makes "real EOF" reliable.
* The Jellyfin *session* side (session_playing/progress/stop) is an in-process
  recording fake, not a socket http.server. A real socket server would add port
  and timing flakiness without exercising any more of the shim's own code (the
  session calls just go to the third-party apiclient). We assert the shim makes
  the right calls with the right payloads; that is the shim's contract.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402


def _import_real_player():
    """Set the backend + quiet settings, then import the real player module
    (constructing the real mpv-backed singleton). Only called after the
    capability gate, so a bare machine never launches mpv."""
    from jellyfin_mpv_shim.conf import settings
    h.prime_args()
    settings.thumbnail_enable = False
    settings.shader_pack_enable = False
    settings.menu_mouse = False
    settings.svp_enable = False
    settings.discord_presence = False
    settings.check_updates = False
    settings.enable_osc = False
    settings.fullscreen = False
    settings.mpv_ext = (h.BACKEND == "jsonipc")
    import jellyfin_mpv_shim.player as player_module
    return player_module


class FakeJellyfinApi:
    def __init__(self):
        self.playing = []
        self.progress = []
        self.stopped = []

    def session_playing(self, options):
        self.playing.append(options)

    def session_progress(self, options):
        self.progress.append(options)

    def session_stop(self, options):
        self.stopped.append(options)


class FakeClient:
    def __init__(self):
        self.jellyfin = FakeJellyfinApi()


class RealParent:
    def __init__(self, next_video=None):
        self._next_video = next_video
        self.has_next = next_video is not None
        self.has_prev = False
        self.is_local = True
        self.queue = []

    def get_next(self):
        return type("Item", (), {"video": self._next_video})()


class RealVideo:
    """A minimally-complete Video for a real local-file playback."""

    def __init__(self, path, client, item_id="v", next_video=None):
        self._path = path
        self.client = client
        self.item_id = item_id
        self.parent = RealParent(next_video)
        self.aid = None
        self.sid = -1                    # subtitles off -> configure_streams noop
        self.is_transcode = False
        self.media_source = {"Id": "ms-%s" % item_id, "MediaStreams": []}
        self.playback_info = {"PlaySessionId": "ps-%s" % item_id}
        self.audio_seq = {}
        self.subtitle_seq = {}
        self.subtitle_url = {}
        self.played = []

    def get_current_intro(self, playback_time):
        return False, None               # no intro/credits segments

    def get_playback_url(self):
        return self._path

    def get_proper_title(self):
        return "clip-%s" % self.item_id

    def get_duration(self):
        return 2

    def get_playlist_id(self):
        return "pl-%s" % self.item_id

    def set_played(self, value=True):
        self.played.append(value)

    def terminate_transcode(self):
        pass


@h.require_real_mpv
class RealMpvSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.player_module = _import_real_player()
        cls.pm = cls.player_module.playerManager
        # Drive the action-queue pump by hand instead of starting the real
        # ActionThread singleton, so the test controls timing deterministically.
        import threading
        cls.pm.action_trigger = threading.Event()
        cls.pm.timeline_trigger = threading.Event()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.pm.terminate()
        except Exception:
            pass

    def _pump_until(self, predicate, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pm.update()
            if predicate():
                return True
            time.sleep(0.05)
        self.pm.update()
        return predicate()

    def test_play_progress_eof_autoadvance_and_stop(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="jms-clip-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        clip1 = h.make_test_clip(os.path.join(tmp, "a.mp4"), duration=2)
        clip2 = h.make_test_clip(os.path.join(tmp, "b.mp4"), duration=2)

        client = FakeClient()
        second = RealVideo(clip2, client, item_id="second")
        first = RealVideo(clip1, client, item_id="first", next_video=second)

        with mock.patch.object(self.player_module.settings,
                                        "auto_play", True):
            # 1) Real playback starts and duration becomes known.
            self.pm.play(first, is_initial_play=True)
            self.assertIs(self.pm._video, first)
            self.assertTrue(self.pm._player.duration and
                            self.pm._player.duration > 0,
                            "real mpv never reported a duration")

            # 2) A timeline progress post carries a sane position/payload.
            self.pm.send_timeline()
            self.assertTrue(client.jellyfin.playing, "no session_playing sent")
            self.assertTrue(client.jellyfin.progress, "no session_progress sent")
            self.assertEqual(client.jellyfin.progress[-1]["ItemId"], "first")

            # 3) Let clip1 reach a genuine EOF and auto-advance to clip2.
            advanced = self._pump_until(lambda: self.pm._video is second,
                                        timeout=30)
            self.assertTrue(advanced, "did not auto-advance on EOF")

            # 4) Let clip2 finish; the last item stops (no next) and reports.
            stopped = self._pump_until(
                lambda: bool(client.jellyfin.stopped), timeout=30)
            self.assertTrue(stopped, "final stop was never reported")

    def test_explicit_stop_reports_session_stop(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="jms-clip2-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        clip = h.make_test_clip(os.path.join(tmp, "s.mp4"), duration=2)
        client = FakeClient()
        video = RealVideo(clip, client, item_id="solo")

        self.pm.play(video, is_initial_play=True)
        self.assertIs(self.pm._video, video)
        self.pm.stop()
        self.assertTrue(client.jellyfin.stopped, "stop() did not report session_stop")
        self.assertIsNone(self.pm._video)


if __name__ == "__main__":
    unittest.main()
