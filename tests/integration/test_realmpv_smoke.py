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

    def test_seek_to_end_fires_eof_autoadvance(self):
        # Issue #541: skipping to the very end of a file must still fire the
        # end-of-file event and auto-advance. The historical bug was mpv NOT
        # emitting eof when you seek right up to the end, so the queue stalled.
        # Play a short clip, seek to its last fraction of a second (absolute),
        # let it play out, and assert a genuine EOF advances to the next clip.
        import tempfile
        tmp = tempfile.mkdtemp(prefix="jms-seekend-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        clip1 = h.make_test_clip(os.path.join(tmp, "a.mp4"), duration=3)
        clip2 = h.make_test_clip(os.path.join(tmp, "b.mp4"), duration=2)

        client = FakeClient()
        second = RealVideo(clip2, client, item_id="seek-second")
        first = RealVideo(clip1, client, item_id="seek-first", next_video=second)

        with mock.patch.object(self.player_module.settings, "auto_play", True):
            self.pm.play(first, is_initial_play=True)
            self.assertIs(self.pm._video, first)
            dur = self.pm._player.duration
            self.assertTrue(dur and dur > 0, "real mpv never reported a duration")

            # Seek to the last ~0.3s (absolute, exact). keep_open holds the
            # finished file at eof (there is a next item), so a genuine
            # eof-reached must fire and auto-advance.
            self.pm.seek(max(dur - 0.3, 0), absolute=True, exact=True)

            advanced = self._pump_until(lambda: self.pm._video is second,
                                        timeout=30)
            self.assertTrue(advanced,
                            "seek-to-end did not fire EOF / auto-advance")
            self.pm.stop()

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

    @unittest.skipUnless(h.BACKEND == "jsonipc",
                         "idle-quit fires only for a managed external mpv")
    def test_idle_quit_fires_and_reopens_managed_external(self):
        # Batch B (commit 012961c): on a MANAGED external mpv (jsonipc,
        # mpv_ext_start default True), idle_quit() terminates the process
        # intentionally and the next play() re-opens a fresh one that decodes.
        import tempfile
        tmp = tempfile.mkdtemp(prefix="jms-idle-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        clip = h.make_test_clip(os.path.join(tmp, "i.mp4"), duration=2)
        client = FakeClient()

        with mock.patch.object(self.player_module.settings, "mpv_idle_quit", True), \
                mock.patch.object(self.player_module.settings,
                                  "mpv_idle_quit_secs", 0):
            first = RealVideo(clip, client, item_id="idle-1")
            self.pm.play(first, is_initial_play=True)
            self.assertIs(self.pm._video, first)
            self.pm.stop()
            self.assertIsNone(self.pm._video)

            # Drive the idle path directly (the timeline thread's role).
            self.pm.idle_quit()
            self.assertTrue(self.pm._idle_quit,
                            "idle_quit did not mark the termination intentional")
            self.assertFalse(self.pm._mpv_alive,
                             "real mpv not marked down after idle_quit")

            # The next play must re-open a fresh mpv process and decode.
            second = RealVideo(clip, client, item_id="idle-2")
            self.pm.play(second, is_initial_play=True)
            self.assertIs(self.pm._video, second)
            self.assertFalse(self.pm._idle_quit,
                             "_idle_quit not cleared on re-open")
            self.assertTrue(self.pm._mpv_alive)
            self.assertTrue(self.pm._player.duration and
                            self.pm._player.duration > 0,
                            "re-opened real mpv never reported a duration")
            self.pm.stop()

    @unittest.skipUnless(h.BACKEND == "libmpv",
                         "the in-process no-op gate is libmpv-specific")
    def test_idle_quit_is_noop_on_in_process_libmpv(self):
        # Batch B (commit 012961c): libmpv is in-process and cannot be
        # re-created with a working eof-reached (see the KNOWN LIMITATION in the
        # README). idle_quit() must therefore NO-OP here — the mpv stays alive —
        # rather than tear down and wedge auto-advance on re-open.
        import tempfile
        tmp = tempfile.mkdtemp(prefix="jms-idle-noop-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        clip = h.make_test_clip(os.path.join(tmp, "i.mp4"), duration=2)
        client = FakeClient()

        with mock.patch.object(self.player_module.settings, "mpv_idle_quit", True), \
                mock.patch.object(self.player_module.settings,
                                  "mpv_idle_quit_secs", 0):
            video = RealVideo(clip, client, item_id="idle-noop")
            self.pm.play(video, is_initial_play=True)
            self.pm.stop()
            self.assertIsNone(self.pm._video)

            self.pm.idle_quit()
            self.assertFalse(self.pm._idle_quit,
                             "idle_quit wrongly fired on in-process libmpv")
            self.assertTrue(self.pm._mpv_alive,
                            "idle_quit wrongly tore down in-process libmpv")

            # The still-alive player must play normally afterwards.
            again = RealVideo(clip, client, item_id="idle-noop-2")
            self.pm.play(again, is_initial_play=True)
            self.assertIs(self.pm._video, again)
            self.assertTrue(self.pm._player.duration and
                            self.pm._player.duration > 0)
            self.pm.stop()


@h.require_real_mpv
class IdleQuitReopenIsolatedTest(unittest.TestCase):
    """End-to-end validation of the idle-quit lifecycle fix (commit 012961c),
    in a subprocess so any regression that wedged eof-reached couldn't poison
    the rest of the real-mpv leg.

    The child runs: play → stop → idle_quit() → play (re-open) → stop → play a
    clip WITH a next item → pump for EOF auto-advance, and prints ADVANCED /
    STALLED. Both backends must ADVANCE:

    * libmpv — idle_quit() no-ops (in-process can't be re-created), so nothing
      is torn down and the later playback's eof fires normally.
    * jsonipc — idle_quit() fires and re-opens a fresh mpv *process*, whose eof
      fires normally.

    (Was a pinned `@expectedFailure` for the pre-fix libmpv re-open wedge; the
    fix scopes idle-quit to managed external mpv, so it now passes on both.)
    """

    def test_idle_quit_then_playback_advances(self):
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        child = os.path.join(here, "_idle_reopen_child.py")
        repo = os.path.dirname(os.path.dirname(here))
        env = dict(os.environ)
        env["JMS_TEST_BACKEND"] = h.BACKEND
        proc = subprocess.run(
            [sys.executable, child], cwd=repo, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=120,
        )
        verdict = ((proc.stdout or "").strip().splitlines()[-1:] or [""])[0]
        self.assertEqual(
            verdict, "ADVANCED",
            "idle_quit + playback did not auto-advance on %s "
            "(rc=%d, stdout=%r, stderr=%r)"
            % (h.BACKEND, proc.returncode, proc.stdout, proc.stderr[-500:]),
        )


if __name__ == "__main__":
    unittest.main()
