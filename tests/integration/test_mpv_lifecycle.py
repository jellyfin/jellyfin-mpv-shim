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
# ...and the repo root. Run as a script -- which the __main__ block at the
# bottom invites -- `sys.path[0]` is this directory and the root is on the
# path nowhere, so `jellyfin_mpv_shim` resolves to whatever is pip-installed:
# silently, and it *runs*, against the previous release. Measured once as a
# renderer.lua from a fortnight ago failing a test about this tree.
# run_integration.py is unaffected (it spawns -m unittest with cwd=root).
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__)))))
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


class StaleQueueDrainTest(unittest.TestCase):
    """REGRESSION LOCK for the real re-open wedge cause. As the outgoing mpv is
    torn down its dying shutdown/eof observers ``put_task`` onto ``evt_queue``
    (``_handle_mpv_shutdown``, stray ``finished_callback``s). If those survive
    into the re-opened session the pump runs them against the NEW video —
    ``_handle_mpv_shutdown`` nulls ``self._video``, after which the new player's
    eof is ignored and auto-advance silently stops. ``_teardown_player`` must
    drain ``evt_queue`` (after joining the terminate thread, so the old event
    thread is dead and nothing re-queues) on every re-open. Backend-agnostic —
    the defect and fix are pure queue handling."""

    def test_teardown_drains_stale_queued_tasks(self):
        pm = h.build_player(player_module)
        ran = []
        pm.put_task(pm._handle_mpv_shutdown)          # the stale teardown task
        pm.put_task(lambda: ran.append("stray"))      # a stray finished_callback
        self.assertFalse(pm.evt_queue.empty())

        pm._teardown_player()

        self.assertTrue(pm.evt_queue.empty(),
                        "stale tasks from the outgoing mpv were not drained")
        self.assertEqual(ran, [],
                         "a stale task ran instead of being discarded")

    def test_reopen_drops_stale_shutdown_so_new_eof_survives(self):
        # Full re-open path: a stale _handle_mpv_shutdown is queued (as the
        # outgoing instance would), then _ensure_mpv -> _init_mpv ->
        # _teardown_player must drain it. The new session's _video must survive
        # and the new player's eof must still queue finished_callback.
        pm = h.build_player(player_module)
        pm.put_task(pm._handle_mpv_shutdown)          # queued by the outgoing mpv

        pm._mpv_alive = False
        pm._ensure_mpv()                              # re-open: drain + new player
        self.assertTrue(pm.evt_queue.empty(),
                        "re-open did not drain the stale shutdown task")

        # New session begins playing; nothing should have nulled _video.
        pm._video = object()
        pm._reached_eof = False
        pm._player.fire_property("eof-reached", True)

        self.assertIsNotNone(pm._video,
                             "a surviving stale shutdown nulled the new _video")
        queued = [item[0] for item in list(pm.evt_queue.queue)]
        self.assertIn(pm.finished_callback, queued,
                      "the re-opened player's eof did not queue finished_callback "
                      "(auto-advance would be dead)")


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
        if with_trickplay:
            pm.trickplay = FakeTrickPlay()
        return pm

    def _assert_noop(self, pm):
        player = pm._player
        pm.idle_quit()
        self.assertTrue(pm._mpv_alive, "idle_quit wrongly killed a needed mpv")
        self.assertFalse(pm._idle_quit, "idle_quit set the intentional flag")
        self.assertFalse(player.terminated, "idle_quit terminated the player")

    def _assert_gated_noop(self, pm):
        # Force the user-launched-external backend gate open (mpv_ext_start True)
        # so the *specific* gate the sub-test set (video / menu / syncplay /
        # webview) is what makes idle_quit no-op — not the backend gate. Works on
        # both fake legs (on jsonipc the harness sets mpv_ext_start False, which
        # would otherwise block).
        with mock.patch.object(player_module.settings, "mpv_ext_start", True):
            self._assert_noop(pm)

    def _assert_fires(self, pm):
        tp = pm.trickplay
        player = pm._player
        pm.idle_quit()
        self.assertTrue(pm._idle_quit, "intentional-quit flag not set")
        self.assertFalse(pm._mpv_alive, "mpv still marked alive after idle_quit")
        if tp is not None:
            self.assertEqual(
                tp.stop_calls, [False],
                "idle_quit did not stop the trickplay worker (join=False)")
            self.assertIsNone(pm.trickplay)
        self.assertTrue(_wait_true(lambda: player.terminated),
                        "idle_quit never terminated the mpv process")


class IdleQuitGatingTest(_IdleMixin, unittest.TestCase):
    """idle_quit() is hard-gated: it fires on both libmpv and a *managed*
    external mpv (the re-open re-creates the player and drains the outgoing
    instance's stale tasks), but never while a video, an open menu, an active
    SyncPlay group, an on-screen in-window UI, or a *user-launched* external
    mpv (``mpv_ext_start`` False) is in play. Backend globals are patched so both
    fake legs exercise both branches deterministically (no real spawn)."""

    def test_noop_when_mpv_not_alive(self):
        pm = self._idle_player()
        pm._mpv_alive = False
        pm.idle_quit()
        self.assertFalse(pm._idle_quit)

    def test_noop_when_video_playing(self):
        pm = self._idle_player()
        pm._video = object()
        self._assert_gated_noop(pm)

    def test_noop_when_menu_shown(self):
        pm = self._idle_player()
        pm.menu.is_menu_shown = True
        self._assert_gated_noop(pm)

    def test_noop_when_syncplay_enabled(self):
        pm = self._idle_player()
        pm.syncplay._enabled = True
        self._assert_gated_noop(pm)

    def test_noop_when_mpvtk_browser_active(self):
        # The in-window mpvtk browser owns the window while browsing — and,
        # in headless mode, while the cast screen is up. idle_quit must not
        # tear it down. This subsumes the old get_webview() gate, which
        # guarded the display mirror back when it was a separate UI.
        pm = self._idle_player()
        pm.mpvtk_active = True
        self._assert_gated_noop(pm)

    def test_noop_for_user_launched_external_mpv(self):
        # External mpv the user started themselves (mpv_ext_start False) must
        # never be killed.
        pm = self._idle_player()
        with mock.patch.object(player_module, "is_using_ext_mpv", True), \
                mock.patch.object(player_module.settings, "mpv_ext_start", False):
            self._assert_noop(pm)

    def test_fires_on_in_process_libmpv(self):
        # In-process libmpv re-creates fine (the reopen wedge was stale queued
        # tasks, since fixed by draining evt_queue in _teardown_player), so
        # idle_quit fires here when fully idle.
        pm = self._idle_player(with_trickplay=True)
        with mock.patch.object(player_module, "is_using_ext_mpv", False), \
                mock.patch.object(player_module.settings, "mpv_ext_start", True):
            self._assert_fires(pm)

    def test_fires_on_managed_external_mpv(self):
        # A managed external mpv (mpv_ext_start True): idle_quit terminates it;
        # the re-open spawns a fresh process.
        pm = self._idle_player(with_trickplay=True)
        with mock.patch.object(player_module, "is_using_ext_mpv", True), \
                mock.patch.object(player_module.settings, "mpv_ext_start", True):
            self._assert_fires(pm)


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


class MenuSurvivesReopenTest(unittest.TestCase):
    """Regression: _init_mpv used to build a FRESH OSDMenu on every re-open,
    resetting is_menu_shown to False. The systray "Application Menu" opened the
    menu (possibly re-creating a dead mpv mid-show), the state landed on the
    discarded object, and idle_quit — gated on menu.is_menu_shown — killed the
    window while the user was looking at the menu."""

    def test_menu_object_and_state_survive_reopen(self):
        pm = h.build_player(player_module)
        menu = pm.menu
        menu.is_menu_shown = True     # menu is on screen
        pm._mpv_alive = False

        pm._ensure_mpv()              # crash-recovery / idle-quit re-open

        self.assertIs(pm.menu, menu, "re-open replaced the menu object")
        self.assertTrue(pm.menu.is_menu_shown,
                        "re-open reset is_menu_shown — idle_quit would now "
                        "kill mpv under an open menu")
        self.assertIs(getattr(menu, "player", None), pm._player,
                      "menu was not pointed at the re-created player")

    def test_idle_quit_blocked_by_menu_shown_after_reopen(self):
        pm = h.build_player(player_module)
        pm.menu.is_menu_shown = True
        pm._mpv_alive = False
        pm._ensure_mpv()

        with mock.patch.object(player_module.settings, "mpv_idle_quit", True):
            pm.idle_quit()

        self.assertTrue(pm._mpv_alive,
                        "idle_quit killed mpv while the menu (opened via the "
                        "systray) was still on screen")

    def test_real_osdmenu_created_once_then_reused(self):
        # With menu=None (first init), a real OSDMenu is built; a second
        # re-init must reuse it, not construct a new one.
        from jellyfin_mpv_shim.menu import OSDMenu

        pm = h.build_player(player_module)
        pm.menu = None
        pm._mpv_alive = False
        pm._ensure_mpv()
        first = pm.menu
        self.assertIsInstance(first, OSDMenu)

        pm._mpv_alive = False
        pm._ensure_mpv()
        self.assertIs(pm.menu, first, "re-init rebuilt the OSDMenu")


class SyncPlaySurvivesReopenTest(unittest.TestCase):
    """The same invariant as the menu, for the other object that is not a
    property of the mpv handle — and reached by an ordinary route, not a
    crash: stop() *halts* a SyncPlay session rather than leaving it, and
    idle_quit's gate is is_enabled(), which a halted member passes by design.
    So "back out to the library, let mpv idle-quit, come back" was enough to
    silently zero group membership while the server still held the seat.
    """

    def _halted_member(self):
        pm = h.build_player(player_module)
        pm.syncplay._enabled = True
        pm.syncplay.halt_group_playback()   # what stop() does to a session
        pm._video = None
        pm.menu.is_menu_shown = False
        return pm

    def test_membership_survives_an_idle_quit_and_reopen(self):
        pm = self._halted_member()
        sp = pm.syncplay

        # mpv_ext_start True for the same reason _assert_gated_noop does it:
        # on the jsonipc leg the harness leaves it False, which is realistic
        # (never kill an mpv the user launched) and makes idle_quit a no-op —
        # so without this the test asserts nothing on that backend, which is
        # exactly what the matrix caught.
        with mock.patch.object(player_module.settings, "mpv_idle_quit", True), \
                mock.patch.object(player_module.settings, "mpv_ext_start", True):
            pm.idle_quit()
        self.assertFalse(pm._mpv_alive,
                         "a halted member should not hold mpv open")
        pm._ensure_mpv()                    # tray Show / cast / a group update

        self.assertIs(pm.syncplay, sp, "re-open replaced the syncplay manager")
        self.assertTrue(pm.syncplay.in_group(),
                        "re-open forgot the group; the server still has us in "
                        "it and terminate() will never say we left")

    def test_membership_survives_repeated_re_creations(self):
        """One cycle is a leak, several are the shape it takes in practice —
        minimize to the tray, come back, repeat. Each rebuild used to strand
        one manager holding a timesync subscription and a ping."""
        pm = self._halted_member()
        sp = pm.syncplay
        for _cycle in range(5):
            pm._mpv_alive = False
            pm._ensure_mpv()
            self.assertIs(pm.syncplay, sp)
        self.assertTrue(pm.syncplay.in_group())

    def test_real_manager_created_once_then_reused(self):
        from jellyfin_mpv_shim.syncplay import SyncPlayManager

        pm = h.build_player(player_module)
        pm.syncplay = None
        pm._mpv_alive = False
        pm._ensure_mpv()
        first = pm.syncplay
        self.assertIsInstance(first, SyncPlayManager)

        pm._mpv_alive = False
        pm._ensure_mpv()
        self.assertIs(pm.syncplay, first, "re-init rebuilt the manager")


if __name__ == "__main__":
    unittest.main()


class WindowLifecycleTest(unittest.TestCase):
    """With the in-window UI there is ONE window, shared by the library and
    playback, and it must survive everything except being minimized.

        state                       playback_abort  force_window
        library browser             yes             yes
        media playing               no              yes
        "minimized" (tray only)     yes             no
        cast to, library not open   no              no

    Every path that used to drop force_window — stopping playback, the OSC's
    close button, closing the OSD menu — showed as the window vanishing and
    being rebuilt.
    """

    def _player(self, browsing=True):
        pm = h.build_player(player_module)
        pm._mpv_alive = True
        pm._video = None
        pm.mpvtk_active = browsing
        pm._player.force_window = True
        return pm

    def test_stop_and_close_keeps_the_window_while_the_browser_owns_it(self):
        pm = self._player()
        pm.stop_and_close()
        self.assertTrue(pm._player.force_window)

    def test_stop_and_close_still_closes_without_the_in_window_ui(self):
        pm = self._player(browsing=False)
        pm.stop_and_close()
        self.assertFalse(pm._player.force_window)

    def test_menu_force_window_off_cannot_close_the_browser_window(self):
        pm = self._player()
        pm.force_window(False)
        self.assertTrue(pm._player.force_window)

    def test_minimize_is_the_one_path_that_releases_the_window(self):
        pm = self._player()
        # on_minimize() clears mpvtk_active first — that's what lets it past
        # the guard.
        pm.mpvtk_active = False
        pm.set_browse_window(False)
        self.assertFalse(pm._player.force_window)

    @staticmethod
    def _stops(pm):
        return [c for c in pm._player.commands if c and c[0] == "stop"]

    def test_stop_to_browser_does_not_resummon_a_window_the_browser_dropped(
            self):
        """The window flicker the `window:` trace caught, from a real log:

            browse=off  <- gateway.playback.on_minimize
            force_window=False
            browse=on   <- player.stop_to_browser        <-- blank window
            browse=off  <- gateway.playback.on_minimize  <-- and gone again

        ``stop()`` ends in ``push_playstate(stopped=True)``, which is how the
        browser is told to take the window back — and it is equally free to
        decide it is minimizing instead. When it does, it clears
        ``mpvtk_active`` and drops force_window *inside* this call, and the
        unconditional re-assert that followed summoned a fresh blank window
        the browser then tore down again.

        Timing-dependent, which is why it showed on one backend: lose the
        race and the re-assert lands on a window that still exists and does
        nothing at all.
        """
        pm = self._player()

        def browser_minimizes(**kw):
            # Exactly what gateway.playback.on_minimize does, in the order it
            # does it — the flag first, which is what makes reading it safe.
            pm.mpvtk_active = False
            pm.set_browse_window(False)

        pm.push_playstate = browser_minimizes
        # stop()'s tail, which is the seam that matters here: the rest of it
        # (timeline reports, transcode teardown) has nothing to do with who
        # ends up owning the window.
        pm.stop = lambda: pm.push_playstate(stopped=True)
        pm.stop_to_browser()
        self.assertFalse(
            pm._player.force_window,
            "stop_to_browser re-summoned a window the browser had just "
            "released — a blank one, since nothing is loaded")

    def test_stop_to_browser_still_keeps_the_window_for_a_browser_that_wants_it(
            self):
        """The other half: when the browser does re-enter browse mode, the
        re-assert is what it is there for and must still happen."""
        pm = self._player()
        pm.push_playstate = lambda **kw: None   # browser stays in browse mode
        pm.stop = lambda: pm.push_playstate(stopped=True)
        pm._player.force_window = False         # ...and has not asked yet
        pm.stop_to_browser()
        self.assertTrue(pm._player.force_window,
                        "the window was not handed back to the browser")

    def test_browse_window_is_idempotent(self):
        """Re-arming the window over itself tears the video output down and
        back up, which reads as the window closing and reopening."""
        pm = self._player()
        pm.set_browse_window(True)
        first = len(self._stops(pm))
        pm.set_browse_window(True)
        pm.set_browse_window(True)
        self.assertEqual(len(self._stops(pm)), first)

    def test_real_media_re_arms_the_background(self):
        pm = self._player()
        pm.set_browse_window(True)
        n = len(self._stops(pm))
        pm._showing_browse_bg = False      # what _play_media does
        pm.set_browse_window(True)
        self.assertEqual(len(self._stops(pm)), n + 1)

    def test_the_window_is_painted_not_decoded(self):
        """force_window with nothing loaded shows an empty window painted
        with background-color — no file decoded just to hold it open, and
        no video-output churn when it is re-armed."""
        pm = self._player()
        pm.set_browse_window(True)
        self.assertEqual(pm._player.played, [], "decoded a background file")
        self.assertEqual(pm._player.background, "color")
        self.assertEqual(pm._player.background_color, "#141414")

    def test_audio_playback_is_not_stopped_by_re_arming(self):
        """Audio keeps the browser up, so the window must not be re-armed
        out from under it."""
        pm = self._player()
        pm._video = object()
        pm._showing_browse_bg = False
        pm.set_browse_window(True)
        self.assertEqual(self._stops(pm), [], "stopped the music")


class KeyClaimsSurviveReopenTest(unittest.TestCase):
    """A claim lives in an mpv **input section**, which dies with the handle.

    The menu object and the SyncPlay session survive an mpv re-creation --
    two classes above assert that -- because they are the player's, not the
    handle's. A key claim is the opposite: it is state inside mpv, so
    re-creating mpv silently un-claims every key unless the claims are
    re-installed on the new one.

    Nothing would notice. A re-open only happens on the external backend
    after an idle-quit, and the symptom is that the keys the shim had taken
    over go back to meaning what mpv thinks they mean -- pause still pauses,
    seek still seeks, so the app is *almost* right and only the parts that
    needed the interception (the menu's arrows, the seek gate) are wrong.
    """

    def _player(self):
        pm = h.build_player(player_module)
        pm._mpv_alive = True
        pm._video = None
        pm.mpvtk_active = False
        return pm

    def _reopen(self, pm):
        """A re-creation by the seam both crash recovery and idle-quit reach.

        **Not** through ``idle_quit``, which is gated on the backend: a
        *user-launched* external mpv is never quit, and the fake jsonipc leg
        is exactly that (``import_player_with_fake_mpv`` sets
        ``mpv_ext_start`` False so the fake cannot spawn a process). Driven
        that way, this re-created nothing on one of the two legs and the
        assertions then ran against an empty journal -- which is how a test
        passes for the wrong reason on the leg you were not watching.
        `MenuSurvivesReopenTest` already uses this shape.

        The precondition is asserted rather than assumed for the same
        reason: "nothing was rebuilt" and "it was rebuilt wrongly" have to
        be told apart by the failure, not by reading the code afterwards.
        """
        previous = pm._player
        pm._mpv_alive = False
        pm.journal.mark("reopening")
        pm._ensure_mpv()
        self.assertIsNot(
            pm._player, previous,
            "no new mpv was built, so nothing below is being tested")
        return previous

    def test_a_standing_claim_is_reinstalled_on_the_new_handle(self):
        pm = self._player()
        pm.claim_keys("menu", {"pause": "menu-pause", "seek": "menu-seek"})
        pm.journal.reset()

        self._reopen(pm)

        after = pm.journal.since("test:reopening")
        after.order("mpv.cmd:define-section", "mpv.cmd:enable-section")
        self.assertIn("SPACE", pm._player._sections.get("jms_keys", {}),
                      "the new mpv has no claim on the keys the menu took")

    def test_the_claimed_key_still_reaches_python_on_the_new_handle(self):
        """The section being installed is not the claim working -- asserting
        the command that installs it is asserting our own request back. What
        the user has is a key press, and what has to come out of it is a
        client message naming the semantic."""
        pm = self._player()
        pm.claim_keys("menu", {"pause": "menu-pause"})
        self._reopen(pm)
        pm.journal.mark("pressing")

        pm._player.press_key("SPACE")

        pm.journal.since("test:pressing").order("mpv.key:SPACE",
                                                "mpv.event:client-message")

    def test_the_menu_is_pointed_at_the_new_handle_once_it_exists(self):
        """Ordering, because the other order is silent: re-pointing the menu
        before the handle is built leaves it holding the dead one, and the
        menu only draws when somebody opens it."""
        pm = self._player()
        pm.journal.reset()
        self._reopen(pm)
        pm.journal.since("test:reopening").order("mpv.create",
                                                 "menu.update_player")
        self.assertIs(pm.menu.player, pm._player)


class BrowseHandoffTest(unittest.TestCase):
    """What ``browse_yield`` has to undo when video takes the window back.

    ``set_browse_window`` paints the browser's own window instead of
    decoding a file to hold it open, and the two properties it paints with
    are **global VO options that outlive the file change**. That is what
    makes them work for music -- and it is why handing the window to video
    without putting them back gave every letterboxed film #141414 bars
    instead of black, *for the rest of the mpv process's life* (dde0f2a1).
    Reported as "a grey background", which is not what it looks like.

    None of this could be asserted before the fake modelled ``background``,
    ``background_color`` and ``keepaspect``: the writes conjured the
    attributes, so the only readable state was the one the shim had just
    written, and there was no starting value for a restore to return to.
    """

    def _player(self):
        pm = h.build_player(player_module)
        pm._mpv_alive = True
        pm._video = None
        pm.mpvtk_active = True
        pm._player.force_window = True
        return pm

    def test_video_taking_the_window_back_restores_mpvs_own_background(self):
        pm = self._player()
        pm.set_browse_window(True)
        self.assertEqual(pm._player.background_color, "#141414")

        pm.browse_yield()

        self.assertEqual(pm._player.background, "tiles")
        self.assertEqual(pm._player.background_color, "#000000",
                         "video took the window back with the browser's "
                         "grey still armed, so every letterbox bar is grey")

    def test_the_browse_background_does_not_leak_over_several_handoffs(self):
        """One handoff cannot see this, and the bug it hides is precisely a
        leak: the options survive the file change, so what matters is the
        state after browsing and playing repeatedly -- which is what a
        session actually is."""
        pm = self._player()
        seen = []
        for _ in range(3):
            pm.set_browse_window(True)
            pm.browse_yield()
            seen.append(pm._player.background_color)
        self.assertEqual(seen, ["#000000", "#000000", "#000000"])

    def test_the_handoff_undoes_the_browse_window_in_order(self):
        """The same claim as the three above, made as a sequence.

        Those assert the *end state*, which is what a reader of the code
        would check. What they cannot see is a handoff that reaches the
        right state by the wrong route -- the browse window being re-armed
        after the yield, say, which ends with mpv configured correctly and
        the window torn down and rebuilt in front of the user on the way.
        The mark is what names "the moment I handed it over": nothing the
        fakes record identifies it, because `browse_yield` is the first
        thing that happens afterwards.
        """
        pm = self._player()
        pm.set_browse_window(True)
        pm.journal.mark("handing over")
        pm.browse_yield()

        pm.journal.order("mpv.set:keepaspect=False",
                         "mpv.set:background_color='#141414'",
                         "test:handing over",
                         "mpv.set:keepaspect=True",
                         "mpv.set:background_color='#000000'")
        pm.journal.since("test:handing over").never(
            "mpv.set:background_color='#141414'",
            msg="the browse background was re-armed after the window had "
                "been handed to video")

    def test_the_aspect_ratio_comes_back_with_the_video(self):
        """``set_browse_window`` turns ``keepaspect`` off so the library
        window resizes freely rather than snapping to the last video's
        shape. Left off, mpv stretches whatever is loaded to fill the
        window -- the film plays distorted and ``video-zoom`` does nothing,
        both from one property."""
        pm = self._player()
        pm.set_browse_window(True)
        self.assertFalse(pm._player.keepaspect)
        pm.browse_yield()
        self.assertTrue(pm._player.keepaspect,
                        "video took the window back stretched")

    def test_a_dead_mpv_is_not_written_to(self):
        pm = self._player()
        pm.set_browse_window(True)
        pm._mpv_alive = False
        pm.browse_yield()
        self.assertEqual(pm._player.background_color, "#141414",
                         "browse_yield wrote to a handle that is being "
                         "torn down")


class StopReleasesSyncPlayTest(unittest.TestCase):
    """Stopping playback **halts** a SyncPlay group, unless there would be no
    way back into it -- then it leaves.

    Halting keeps the membership: the group carries on, and the member can
    rejoin from the SyncPlay menu when they are done. That is only a kindness
    while the menu is reachable, and on two surfaces it is not -- no GUI at
    all, or playback cast to a shim whose browser was never opened, where the
    window goes away with the video and the menu lives in the browser's
    chrome. Leaving is right there; the alternative is a group the user is in,
    is not watching, and cannot get out of.

    Both halves were only covered end to end, against a real server and a
    real group (`tests/e2e/test_syncplay_playback.py`). The *decision* needs
    neither: what it turns on is a hook the browser installs, and which of
    the two calls comes out is now readable in the journal.
    """

    def _player(self, reachable):
        from tests.integration.test_player_state_machine import FakeVideo

        pm = h.build_player(player_module)
        pm._mpv_alive = True
        pm._video = FakeVideo()
        pm.syncplay._enabled = True
        pm.syncplay._following = True
        pm.syncplay_menu_reachable = (None if reachable is None
                                      else (lambda: reachable))
        pm.send_timeline_stopped = lambda *a, **kw: None
        pm._player.playback_abort = False
        return pm

    def test_a_stop_with_the_menu_reachable_halts(self):
        pm = self._player(reachable=True)
        pm.journal.mark("stopping")
        pm.stop()
        after = pm.journal.since("test:stopping")
        after.happened("syncplay.halt_group_playback")
        after.never("syncplay.disable_sync_play",
                    msg="stopping playback left a group the user could have "
                        "rejoined from the menu")

    def test_a_stop_with_no_way_back_leaves(self):
        pm = self._player(reachable=False)
        pm.journal.mark("stopping")
        pm.stop()
        after = pm.journal.since("test:stopping")
        after.happened("syncplay.disable_sync_play")
        after.never("syncplay.halt_group_playback",
                    msg="the user is halted in a group with no menu to leave "
                        "it from")

    def test_no_hook_at_all_counts_as_no_way_back(self):
        """The CLI has no browser to install one. Defaulting to "reachable"
        would strand exactly the surface that cannot recover."""
        pm = self._player(reachable=None)
        pm.journal.mark("stopping")
        pm.stop()
        pm.journal.since("test:stopping").happened("syncplay.disable_sync_play")

    def test_the_group_is_released_before_the_file_is_stopped(self):
        """Ordering, and the reason is what the group hears. The release is
        what tells the group we are stepping out; commanding mpv first means
        the eof/abort observers can fire into a session that is still a
        member and report the finish to everybody."""
        pm = self._player(reachable=True)
        pm.journal.mark("stopping")
        pm.stop()
        pm.journal.since("test:stopping").order("syncplay.halt_group_playback",
                                                "mpv.cmd:stop")


class DyingMpvTest(unittest.TestCase):
    """Closing the window makes mpv end the file **and** shut down, so the
    end-file callback runs on the action thread while the terminate thread
    is already inside ``player.terminate()``.

    ``except _mpv_errors`` is genuinely enough on the external backend --
    the command is a socket write and the race surfaces as BrokenPipeError.
    On in-process libmpv the handle has been freed underneath us and the
    command is a use-after-free: SIGSEGV, which no except clause can see,
    and which took two runs in three against a real server (b3b3687f). The
    guard is ``_mpv_alive``, cleared by ``_terminate_mpv`` *before* it calls
    terminate, and it is worth a permanent test because the failure mode is
    a crash rather than an assertion.
    """

    def _player(self):
        from tests.integration.test_player_state_machine import FakeVideo

        pm = h.build_player(player_module)
        pm._video = FakeVideo(has_next=False)
        pm.should_send_timeline = True
        pm.send_timeline_stopped = lambda *a, **kw: None
        return pm

    def test_a_finish_on_a_dying_mpv_issues_no_command(self):
        pm = self._player()
        pm._mpv_alive = False
        before = len(pm._player.commands)
        pm.finished_callback(has_lock=True)
        self.assertEqual(
            [c for c in pm._player.commands[before:] if c and c[0] == "stop"],
            [], "commanded a handle that is being freed")

    def test_a_finish_on_a_live_mpv_still_stops_it(self):
        """The other half. A guard that is simply always on leaves the
        ended file on screen, paused on its last frame."""
        pm = self._player()
        pm._mpv_alive = True
        pm.finished_callback(has_lock=True)
        self.assertIn(("stop",), pm._player.commands)


class FullscreenPersistTest(unittest.TestCase):
    """A fullscreen toggle the *user* made should be remembered; one the app
    made for its own reasons (the update notice, the browser opening
    windowed) should not."""

    def setUp(self):
        self.settings = player_module.settings
        self._saved = (self.settings.fullscreen,
                       self.settings.browser_fullscreen)
        self.settings.save = lambda: None
        self.addCleanup(self._restore)

    def _restore(self):
        (self.settings.fullscreen,
         self.settings.browser_fullscreen) = self._saved

    def _player(self):
        pm = h.build_player(player_module)
        pm._mpv_alive = True
        pm._video = None
        return pm

    def test_toggle_while_browsing_writes_browser_fullscreen(self):
        pm = self._player()
        self.settings.browser_fullscreen = False
        pm.toggle_fullscreen()
        self.assertTrue(self.settings.browser_fullscreen)
        self.assertEqual(self.settings.fullscreen, self._saved[0])

    def test_toggle_while_playing_writes_fullscreen(self):
        pm = self._player()
        pm._video = object()
        self.settings.fullscreen = False
        pm.toggle_fullscreen()
        self.assertTrue(self.settings.fullscreen)

    def test_app_initiated_changes_are_not_persisted(self):
        pm = self._player()
        self.settings.browser_fullscreen = True
        pm.set_fullscreen(False)          # e.g. the update notice
        self.assertTrue(self.settings.browser_fullscreen)
