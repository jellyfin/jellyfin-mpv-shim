"""MPV process-lifecycle tests for the refactor in commit 012961c
(leak-free re-open + opt-in idle-quit).

Backed by :class:`FakeMPV`, so these import player.py and run once per backend
(JMS_TEST_BACKEND). They exercise the seams the refactor added:

* ``_teardown_player`` — stops the previous trickplay worker *without joining*
  (it takes the player ``_lock`` in ``script_message``, so joining under the
  lock ``_teardown_player`` holds would deadlock).
* ``_ensure_mpv`` — the single re-open seam on the play path; clears
  ``_idle_quit`` and re-inits.
* ``idle_quit`` — hard-gated opt-in quit; must never fire while anything still
  needs the window.
* the ``handle_shutdown`` guard — an intentional idle-quit must stay silent.

``import_player_with_fake_mpv`` sets ``thumbnail_enable=False`` so the singleton
has ``trickplay=None``; the leak/teardown tests inject a lightweight fake
trickplay (no Pillow / bifdecode / real worker) to observe the stop calls.
"""

import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
import _harness as h  # noqa: E402


player_module = h.import_player_with_fake_mpv()


class FakeTrickPlay:
    """Stand-in for TrickPlay that records start()/stop(join=...)/clear/fetch
    calls without a real worker thread or Pillow. ``daemon`` mirrors the real
    class so a test can assert the leak-fix contract without importing bifdecode."""

    def __init__(self):
        self.daemon = True
        self.started = False
        self.cleared = 0
        self.fetched = 0
        self.stop_calls = []   # each element is the join= kwarg used

    def start(self):
        self.started = True

    def clear(self):
        self.cleared += 1

    def fetch_thumbnails(self):
        self.fetched += 1

    def stop(self, join=True):
        self.stop_calls.append(join)


def _wait_true(predicate, timeout=1.0):
    tick = threading.Event()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        tick.wait(0.005)
    return predicate()


class TeardownLeakTest(unittest.TestCase):
    """The real leak the refactor fixed: re-opening mpv recreated the trickplay
    worker without stopping the old one — a thread leaked every cycle."""

    def test_teardown_stops_old_trickplay_without_joining(self):
        pm = h.build_player(player_module)
        old = FakeTrickPlay()
        pm.trickplay = old
        pm._teardown_player()
        self.assertEqual(old.stop_calls, [False],
                         "old trickplay must be stopped with join=False "
                         "(joining under _lock would deadlock)")
        self.assertIsNone(pm.trickplay, "trickplay reference not cleared")

    def test_teardown_before_first_init_is_noop(self):
        pm = h.build_player(player_module)
        pm.trickplay = None
        pm._teardown_player()  # must not raise
        self.assertIsNone(pm.trickplay)

    def test_reopen_stops_old_trickplay_and_replaces_it(self):
        # Re-open path: mpv not alive -> _ensure_mpv -> _init_mpv ->
        # _teardown_player. The OLD trickplay must be stopped (join=False) and
        # not left running; no lingering worker across the cycle.
        pm = h.build_player(player_module)
        old = FakeTrickPlay()
        pm.trickplay = old
        pm._mpv_alive = False

        pm._ensure_mpv()

        self.assertEqual(old.stop_calls, [False],
                         "re-open leaked the previous trickplay worker")
        self.assertIsNot(pm.trickplay, old, "trickplay not replaced on re-open")
        self.assertTrue(pm._mpv_alive, "re-open left mpv marked dead")
        self.assertFalse(pm._idle_quit, "re-open did not clear _idle_quit")

    def test_trickplay_is_daemon(self):
        from jellyfin_mpv_shim.trickplay import TrickPlay
        tp = TrickPlay(player=None)
        self.assertTrue(tp.daemon,
                        "TrickPlay must be a daemon so a non-joining stop / a "
                        "leaked worker can't block process exit")


class _IdleMixin:
    def _idle_player(self, with_trickplay=False):
        """A player that is fully idle (mpv alive, no video / menu / syncplay /
        webview) — the precondition idle_quit() requires. Sub-tests then flip a
        single gate on to prove it becomes a no-op."""
        pm = h.build_player(player_module)
        pm._mpv_alive = True
        # _idle_quit / _terminate_thread are seeded by build_player.
        pm._video = None
        pm.menu.is_menu_shown = False
        pm.syncplay._enabled = False
        pm.get_webview = lambda: None
        if with_trickplay:
            pm.trickplay = FakeTrickPlay()
        return pm

    def _assert_noop(self, pm):
        player = pm._player
        pm.idle_quit()
        self.assertTrue(pm._mpv_alive, "idle_quit wrongly killed a needed mpv")
        self.assertFalse(pm._idle_quit, "idle_quit set the intentional flag")
        self.assertFalse(player.terminated, "idle_quit terminated the player")

    def _assert_managed_noop(self, pm):
        # Force the managed-external backend gate open so the *specific* gate the
        # sub-test set (video / menu / syncplay / webview) is what makes
        # idle_quit no-op — not the backend gate.
        with mock.patch.object(player_module, "is_using_ext_mpv", True), \
                mock.patch.object(player_module.settings, "mpv_ext_start", True):
            self._assert_noop(pm)


class IdleQuitGatingTest(_IdleMixin, unittest.TestCase):
    """idle_quit() is hard-gated: it fires only for a *managed external* mpv
    (``is_using_ext_mpv and mpv_ext_start`` — re-open spawns a fresh process),
    and never while a video, an open menu, an active SyncPlay group, a
    display-mirror webview, an in-process libmpv, or a user-launched external
    mpv is in play. The backend globals are patched so both fake legs exercise
    both branches deterministically (no real spawn)."""

    def test_noop_when_mpv_not_alive(self):
        pm = self._idle_player()
        pm._mpv_alive = False
        pm.idle_quit()
        self.assertFalse(pm._idle_quit)

    def test_noop_when_video_playing(self):
        pm = self._idle_player()
        pm._video = object()
        self._assert_managed_noop(pm)

    def test_noop_when_menu_shown(self):
        pm = self._idle_player()
        pm.menu.is_menu_shown = True
        self._assert_managed_noop(pm)

    def test_noop_when_syncplay_enabled(self):
        pm = self._idle_player()
        pm.syncplay._enabled = True
        self._assert_managed_noop(pm)

    def test_noop_when_webview_present(self):
        pm = self._idle_player()
        pm.get_webview = lambda: object()
        self._assert_managed_noop(pm)

    def test_noop_for_in_process_libmpv(self):
        # In-process libmpv (is_using_ext_mpv False) can't be re-created with a
        # working eof-reached, so idle_quit must never fire — even fully idle.
        pm = self._idle_player()
        with mock.patch.object(player_module, "is_using_ext_mpv", False), \
                mock.patch.object(player_module.settings, "mpv_ext_start", True):
            self._assert_noop(pm)

    def test_noop_for_user_launched_external_mpv(self):
        # External mpv the user started themselves (mpv_ext_start False) must
        # never be killed.
        pm = self._idle_player()
        with mock.patch.object(player_module, "is_using_ext_mpv", True), \
                mock.patch.object(player_module.settings, "mpv_ext_start", False):
            self._assert_noop(pm)

    def test_quits_when_fully_idle_managed_external(self):
        # The only firing case: a managed external mpv, fully idle.
        pm = self._idle_player(with_trickplay=True)
        tp = pm.trickplay
        player = pm._player
        with mock.patch.object(player_module, "is_using_ext_mpv", True), \
                mock.patch.object(player_module.settings, "mpv_ext_start", True):
            pm.idle_quit()

        self.assertTrue(pm._idle_quit, "intentional-quit flag not set")
        self.assertFalse(pm._mpv_alive, "mpv still marked alive after idle_quit")
        self.assertEqual(tp.stop_calls, [False],
                         "idle_quit did not stop the trickplay worker (join=False)")
        self.assertIsNone(pm.trickplay)
        self.assertTrue(_wait_true(lambda: player.terminated),
                        "idle_quit never terminated the mpv process")


class ShutdownGuardTest(unittest.TestCase):
    """After an intentional idle-quit, mpv's ``shutdown`` event must be
    swallowed: no stop hook, no teardown task, no re-terminate. An
    *un*intentional shutdown must still tear down (positive control)."""

    def _player_with_observers(self):
        # Register the real shutdown handler on a FakeMPV by driving _init_mpv.
        pm = h.build_player(player_module)
        pm._mpv_alive = False
        pm._ensure_mpv()   # runs _init_mpv -> registers the shutdown callback
        pm._video = None
        # Drop any tasks _init_mpv might have queued (there are none today).
        with pm.evt_queue.mutex:
            pm.evt_queue.queue.clear()
        return pm

    def test_intentional_quit_shutdown_is_silent(self):
        pm = self._player_with_observers()
        pm._idle_quit = True
        stop_cmds = []
        with mock.patch.object(player_module.PlayerManager, "exec_stop_cmd",
                               staticmethod(lambda: stop_cmds.append(True))):
            pm._player.fire_event("shutdown")
        self.assertTrue(pm.evt_queue.empty(),
                        "intentional idle-quit shutdown queued a teardown task")
        self.assertEqual(stop_cmds, [],
                         "intentional idle-quit shutdown ran the stop hook")

    def test_unintentional_shutdown_queues_teardown(self):
        # Positive control: a genuine (non-idle) mpv shutdown still queues the
        # teardown task so the session is reported / the stop hook runs.
        pm = self._player_with_observers()
        pm._idle_quit = False
        pm._player.fire_event("shutdown")
        queued = [item[0] for item in list(pm.evt_queue.queue)]
        self.assertIn(pm._handle_mpv_shutdown, queued,
                      "unintentional shutdown did not queue the teardown")


class ReopenAfterIdleQuitTest(unittest.TestCase):
    """A play after an idle-quit re-creates mpv via _ensure_mpv, clearing the
    intentional-quit flag. (The real clip actually playing is covered by the
    xvfb real-mpv leg.)"""

    def test_ensure_mpv_reopens_and_clears_idle_flag(self):
        pm = h.build_player(player_module)
        # Simulate the post-idle-quit state: process gone, flag set.
        pm._mpv_alive = False
        pm._idle_quit = True
        old_player = pm._player

        pm._ensure_mpv()

        self.assertTrue(pm._mpv_alive, "mpv was not re-opened")
        self.assertFalse(pm._idle_quit, "_idle_quit not cleared on re-open")
        self.assertIsNot(pm._player, old_player, "mpv process was not re-created")


if __name__ == "__main__":
    unittest.main()
