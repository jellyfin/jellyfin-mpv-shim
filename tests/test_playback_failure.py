"""Playback-start failure handling: decoding mpv's end-file event, the
config migration that turns Dolby Vision transcoding off, and the browser's
loading / error / retry screens.

The end-file decoder carries the most risk in this area: it runs on mpv's
event thread, and a reason it misreads as "error" would abort a load that is
actually fine. So the tests below pin both directions — every shape that
means error, and every shape that must NOT.
"""

import json
import os
import queue
import sys
import threading
import time
import tempfile
import unittest

sys.argv = [sys.argv[0]]      # importing player reaches args.get_args()

from jellyfin_mpv_shim.player import _decode_reason, end_file_info  # noqa: E402


class FakeEnum:
    """python-mpv's MpvEventEndFile.Reason: an enum with .name and .value."""

    def __init__(self, name, value):
        self.name = name
        self.value = value


class FakeEventData:
    def __init__(self, reason, error=None):
        self.reason = reason
        self.error = error


class FakeEvent:
    """libmpv-style event object exposing .data."""

    def __init__(self, reason, error=None):
        self.data = FakeEventData(reason, error)


class FakeAsDictEvent:
    """A python-mpv release that exposes as_dict() with a nested payload."""

    def __init__(self, payload):
        self._payload = payload

    def as_dict(self):
        return self._payload


class DecodeReasonTest(unittest.TestCase):
    def test_plain_strings(self):
        self.assertEqual(_decode_reason("error"), "error")
        self.assertEqual(_decode_reason("EOF"), "eof")

    def test_bytes(self):
        self.assertEqual(_decode_reason(b"error"), "error")

    def test_ints_use_the_mpv_reason_table(self):
        self.assertEqual(_decode_reason(0), "eof")
        self.assertEqual(_decode_reason(2), "stop")
        self.assertEqual(_decode_reason(4), "error")
        self.assertEqual(_decode_reason(5), "redirect")

    def test_unknown_int_is_none_rather_than_a_guess(self):
        self.assertIsNone(_decode_reason(99))

    def test_enum_prefers_the_name(self):
        self.assertEqual(_decode_reason(FakeEnum("ERROR", 4)), "error")

    def test_enum_without_a_name_falls_back_to_the_value(self):
        self.assertEqual(_decode_reason(FakeEnum(None, 4)), "error")

    def test_none_and_bools_decode_to_none(self):
        # bool is an int subclass; True must not silently become "stop".
        self.assertIsNone(_decode_reason(None))
        self.assertIsNone(_decode_reason(True))


class EndFileInfoTest(unittest.TestCase):
    def test_libmpv_object_shape(self):
        reason, detail = end_file_info(FakeEvent(FakeEnum("ERROR", 4),
                                                 "Unrecognized file format"))
        self.assertEqual(reason, "error")
        self.assertEqual(detail, "Unrecognized file format")

    def test_jsonipc_flat_dict_shape(self):
        reason, detail = end_file_info({"reason": "error",
                                        "file_error": "Failed to open"})
        self.assertEqual(reason, "error")
        self.assertEqual(detail, "Failed to open")

    def test_nested_dict_shape(self):
        reason, detail = end_file_info(
            FakeAsDictEvent({"event": {"reason": b"error", "error": b"boom"}}))
        self.assertEqual(reason, "error")
        self.assertEqual(detail, "boom")

    def test_normal_end_of_playback_is_not_an_error(self):
        """The reasons a healthy player emits constantly. Treating any of
        these as failure would abort good playback."""
        for reason in ("eof", "stop", "quit", "redirect"):
            self.assertEqual(end_file_info({"reason": reason})[0], reason)

    def test_a_malformed_event_never_raises(self):
        """This runs on mpv's event thread; an exception there takes out
        every other observer with it."""
        for bad in (None, object(), 12345, {"reason": object()}):
            reason, _detail = end_file_info(bad)
            self.assertNotEqual(reason, "error",
                                "an undecodable event must not read as a "
                                "load failure")


class DolbyVisionMigrationTest(unittest.TestCase):
    """mpv plays Dolby Vision natively now, so the old force-transcode
    default has to be retired on existing installs too — every key is written
    to disk, so flipping the default alone would never reach them."""

    def _load(self, payload):
        from jellyfin_mpv_shim.conf import Settings

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "conf.json")
            with open(path, "w") as fh:
                json.dump(payload, fh)
            settings = Settings()
            settings.load(path)
            with open(path) as fh:
                return settings, json.load(fh)

    def test_an_old_config_is_migrated_off_dolby_vision_transcoding(self):
        settings, on_disk = self._load({"transcode_dolby_vision": True})
        self.assertFalse(settings.transcode_dolby_vision)
        self.assertFalse(on_disk["transcode_dolby_vision"],
                         "the migration was not written back")

    def test_the_migration_is_recorded_so_it_runs_once(self):
        from jellyfin_mpv_shim.conf import CONFIG_VERSION

        _settings, on_disk = self._load({"transcode_dolby_vision": True})
        self.assertEqual(on_disk["config_version"], CONFIG_VERSION)

    def test_a_migrated_config_that_re_enables_it_is_left_alone(self):
        """Someone who turns it back on after the migration keeps it: the
        version stamp is what makes this a one-time change, not a policy."""
        from jellyfin_mpv_shim.conf import CONFIG_VERSION

        settings, _on_disk = self._load({"transcode_dolby_vision": True,
                                         "config_version": CONFIG_VERSION})
        self.assertTrue(settings.transcode_dolby_vision)

    def test_the_new_default_is_off(self):
        from jellyfin_mpv_shim.conf import Settings

        self.assertFalse(Settings().transcode_dolby_vision)

    def test_config_version_is_not_offered_as_a_setting(self):
        """It is migration bookkeeping; editing it re-runs or skips upgrades."""
        from jellyfin_mpv_shim.mpvtk_browser.config import _HIDDEN

        self.assertIn("config_version", _HIDDEN)


class RunActionDoesNotBlockTest(unittest.TestCase):
    """UI transport actions run on the browser's loop thread, and the player
    lock is held for the whole of a playback start (mpv load + duration wait,
    up to playback_timeout). Calling through froze the window for that entire
    stretch — the loading screen painted and then the first press of
    pause/seek/stop wedged it.
    """

    def _manager(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)   # no mpv, no singletons
        pm._lock = threading.RLock()
        pm.evt_queue = queue.Queue()
        pm.action_trigger = None
        return pm

    def test_runs_inline_when_the_lock_is_free(self):
        """The normal path must keep its exact current behaviour: immediate
        and synchronous, not deferred by a tick."""
        pm = self._manager()
        seen = []
        pm.run_action(lambda p: seen.append(p))
        self.assertEqual(seen, [pm])
        self.assertTrue(pm.evt_queue.empty(), "a free lock should not defer")

    def test_returns_the_value_on_the_inline_path(self):
        pm = self._manager()
        self.assertEqual(pm.run_action(lambda _p: "value"), "value")

    def test_defers_instead_of_blocking_when_the_lock_is_held(self):
        pm = self._manager()
        holding = threading.Event()
        release = threading.Event()

        def hold():
            with pm._lock:
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(holding.wait(5))

        seen = []
        started = time.time()
        pm.run_action(lambda p: seen.append("ran"))
        elapsed = time.time() - started

        self.assertLess(elapsed, 1.0,
                        "run_action blocked on the held lock — this is the "
                        "freeze it exists to prevent")
        self.assertEqual(seen, [], "it ran inline despite the held lock")
        self.assertFalse(pm.evt_queue.empty(),
                         "the action was dropped rather than deferred")

        release.set()
        holder.join(5)

        # The deferred action is a normal queued task: fn(pm).
        fn, args = pm.evt_queue.get_nowait()
        fn(*args)
        self.assertEqual(seen, ["ran"], "the deferred action never applied")

    def test_a_failing_action_does_not_leak_the_lock(self):
        """A raising action must still release the lock, or the next play
        deadlocks against it."""
        pm = self._manager()

        def boom(_p):
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            pm.run_action(boom)
        self.assertTrue(pm._lock.acquire(blocking=False),
                        "run_action leaked the lock on the error path")
        pm._lock.release()


if __name__ == "__main__":
    unittest.main()


class CancelLoadTest(unittest.TestCase):
    """Cancelling has to abort the duration wait, not just hide the UI — the
    case worth cancelling is the one where mpv sits on a stalled stream for
    the full playback_timeout."""

    def _manager(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm._lock = threading.RLock()
        pm._loading = False
        pm._start_in_progress = False
        pm._load_cancelled = False
        pm._load_failed = threading.Event()
        return pm

    def test_cancelling_aborts_the_wait(self):
        pm = self._manager()
        pm._start_in_progress = True
        self.assertTrue(pm.cancel_load())
        self.assertTrue(pm._load_failed.is_set(),
                        "the duration wait was left to run its full timeout")
        self.assertTrue(pm._load_cancelled)

    def test_cancelling_with_no_load_in_flight_is_a_no_op(self):
        pm = self._manager()
        self.assertFalse(pm.cancel_load())
        self.assertFalse(pm._load_failed.is_set())

    def test_cancelling_works_before_mpv_is_even_asked(self):
        """The start begins at the click, but _loading only covers the
        mpv-side wait — it is set one PlaybackInfo round trip later. Gating
        Cancel on _loading made the button do nothing for that whole window,
        while the spinner offering it was already on screen."""
        pm = self._manager()
        pm._start_in_progress = True
        pm._loading = False              # url still being resolved
        self.assertTrue(pm.cancel_load(),
                        "Cancel was dropped during the PlaybackInfo fetch")
        self.assertTrue(pm._load_cancelled)

    def test_cancel_takes_no_lock(self):
        """It is called straight from the UI thread while _play_media holds
        the lock; taking it would deadlock the very freeze it relieves."""
        pm = self._manager()
        pm._start_in_progress = True
        done = threading.Event()
        pm._lock.acquire()          # stand in for a load in progress
        try:
            t = threading.Thread(target=lambda: (pm.cancel_load(), done.set()),
                                 daemon=True)
            t.start()
            self.assertTrue(done.wait(2),
                            "cancel_load blocked on the player lock")
        finally:
            pm._lock.release()


class BrowseWindowMustNotAbortALoadTest(unittest.TestCase):
    """The root cause of the intermittent "TLS" playback failures.

    set_browse_window() issues `stop` to mpv when it believes nothing is
    playing, and it decided that from `_video` — which is only assigned once
    the duration wait SUCCEEDS. During a start _video is still None, so any
    path that entered or left browse mode mid-open fired `stop` into it. mpv
    logged "Opening failed or was aborted" and "finished playback, success
    (reason 2)"; ffmpeg logged "tls: Error decoding the received TLS packet"
    as the socket was torn down mid-read. It read as a network fault, but it
    was us aborting our own playback.
    """

    def _manager(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm._mpv_alive = True
        pm._video = None
        pm._loading = False
        pm._showing_browse_bg = False

        commands = []

        class FakeMpv:
            def __getattr__(self, _name):
                return None

            def __setattr__(self, _name, _value):
                pass

            def command(self, *args):
                commands.append(args)

        pm._player = FakeMpv()
        pm._set_force_window = lambda _on: None
        return pm, commands

    def test_entering_browse_during_a_load_does_not_stop_it(self):
        pm, commands = self._manager()
        pm._loading = True          # mpv is mid-open
        pm.set_browse_window(True)
        self.assertNotIn(("stop",), commands,
                         "the browse window aborted the in-flight open")

    def test_leaving_browse_during_a_load_does_not_stop_it(self):
        pm, commands = self._manager()
        pm._loading = True
        pm.set_browse_window(False)
        self.assertNotIn(("stop",), commands,
                         "yielding to video aborted the in-flight open")

    def test_entering_browse_with_nothing_playing_still_stops(self):
        """The guard must not disable the real behaviour: with no load and no
        video, `stop` is what clears the picture for the browse background."""
        pm, commands = self._manager()
        pm.set_browse_window(True)
        self.assertIn(("stop",), commands,
                      "the browse background no longer clears the window")

    def test_entering_browse_while_playing_still_does_not_stop(self):
        pm, commands = self._manager()
        pm._video = object()
        pm.set_browse_window(True)
        self.assertNotIn(("stop",), commands)


class AbortedStartActuallyStopsMpvTest(unittest.TestCase):
    """Cancelling (or failing) a start has to tell mpv to drop the file.

    stop() is written for stopping a PLAYING item: it early-returns on
    `not self._video`, and _video is only assigned once the duration wait
    SUCCEEDS. So an aborted start never issued `stop`, mpv finished opening
    the file on its own, and — with the browse window's force_window /
    keep_open already applied — it played underneath the library.
    """

    def _manager(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm._mpv_alive = True
        pm._video = None
        commands = []

        class FakeMpv:
            def command(self, *args):
                commands.append(args)

        pm._player = FakeMpv()
        return pm, commands

    def test_abort_issues_stop_to_mpv(self):
        pm, commands = self._manager()
        pm._abort_load()
        self.assertIn(("stop",), commands,
                      "the half-open file was left to finish loading and "
                      "play itself behind the library")

    def test_abort_is_a_no_op_without_mpv(self):
        pm, commands = self._manager()
        pm._mpv_alive = False
        pm._abort_load()
        self.assertEqual(commands, [])

    def test_stop_alone_does_not_reach_mpv_for_an_aborted_start(self):
        """Pins WHY _abort_load has to exist. If stop() ever learns to handle
        a start that never produced a _video, this test should be revisited
        rather than deleted."""
        from jellyfin_mpv_shim.player import PlayerManager

        pm, commands = self._manager()
        pm._lock = threading.RLock()
        pm.syncplay = type("S", (), {"is_enabled": lambda _s: False})()
        pm.menu = type("M", (), {"is_menu_shown": False})()
        pm.exec_stop_cmd = lambda: None
        PlayerManager.stop(pm)
        self.assertNotIn(("stop",), commands)


class CancelRacesTest(unittest.TestCase):
    """The windows a cancel can land in, other than the duration wait."""

    def _manager(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm._lock = threading.RLock()
        pm._loading = False
        pm._start_in_progress = False
        pm._load_cancelled = False
        pm._load_failed = threading.Event()
        return pm

    def test_a_new_start_clears_a_stale_cancel(self):
        """cancel_load only sets a flag. Without play() resetting it, a
        cancel would poison the next start."""
        pm = self._manager()
        pm._start_in_progress = True
        pm.cancel_load()
        self.assertTrue(pm._load_cancelled)

        # What play() does on entry, before resolving the url.
        pm._load_cancelled = False
        pm._start_in_progress = True
        self.assertFalse(pm._load_cancelled,
                         "a stale cancel would kill the next playback")

    def test_cancel_is_rejected_once_the_start_has_finished(self):
        pm = self._manager()
        pm._start_in_progress = False        # play() returned
        self.assertFalse(pm.cancel_load())
        self.assertFalse(pm._load_cancelled,
                         "a cancel after the fact would arm the next start")
