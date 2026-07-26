"""SyncPlay scheduled-callback staleness tests.

SyncPlay schedules play/pause/seek/speed changes on ``TimeoutThread`` timers.
The audit-era hazard is a timer that fires *after* the session was disabled,
the group was left/rejoined, or a newer command superseded it — yanking the
player around at the wrong moment. The defence is ``sync_generation``: every
enable/disable and every ``clear_scheduled_command`` bumps it, and each
scheduled callback captures the generation it was armed under and no-ops via
``_still_current`` if it changed.

We make this deterministic by replacing ``set_timeout`` with a capture: the
callback is never actually threaded, so a test can mutate session state and then
fire the captured callback by hand to model an arbitrarily-late timer.

``test_playing_now_rearm_sync_is_defined`` guards the ``schedule_play``
"Playing Now" re-arm path, which calls ``_rearm_sync`` — a method that was
referenced but undefined for a while (every SyncPlay Unpause/skip re-arm raised
AttributeError until it was added).
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from jellyfin_mpv_shim.syncplay import SyncPlayManager, seconds_in_ticks  # noqa: E402


class FakeMenu:
    is_menu_shown = False


class FakePlayer:
    def __init__(self):
        self.menu = FakeMenu()
        self.speed = 1.0
        self.paused = None
        self.seeks = []

    def get_speed(self):
        return self.speed

    def set_speed(self, speed):
        self.speed = speed

    def set_paused(self, paused, *a):
        self.paused = paused

    def seek(self, *a, **k):
        self.seeks.append((a, k))

    def show_text(self, *a, **k):
        pass


class FutureTimesync:
    """server_date_to_local returns a future time so schedule_* takes the
    deferred (set_timeout) branch instead of running synchronously. Also carries
    the no-op teardown hooks disable_sync_play calls."""

    def server_date_to_local(self, when):
        return datetime.utcnow() + timedelta(seconds=30)

    def remove_subscriber(self, _cb):
        pass

    def stop_ping(self):
        pass


class CapturingTimeout:
    """Drop-in for syncplay.set_timeout that records callbacks instead of
    starting a TimeoutThread, and returns a stop() that records cancellation.
    Firing a captured callback models the timer expiring at an arbitrary later
    moment."""

    def __init__(self):
        self.captured = []   # list of [callback, args, {"stopped": bool}]

    def __call__(self, ms, callback, *args):
        entry = [callback, args, {"stopped": False}]
        self.captured.append(entry)
        return lambda: entry[2].__setitem__("stopped", True)

    def fire_last(self):
        callback, args, _ = self.captured[-1]
        callback(*args)


class SyncGenerationTest(unittest.TestCase):
    def _enabled_manager(self):
        sp = SyncPlayManager(FakePlayer())
        sp.timesync = FutureTimesync()
        sp.enabled_at = datetime.utcnow()
        sp.playback_rate = 1.0
        return sp

    def test_scheduled_pause_noops_after_disable(self):
        # A deferred Pause armed while enabled must not touch the player once
        # the session has been disabled (generation advanced by disable).
        sp = self._enabled_manager()
        cap = CapturingTimeout()
        with mock.patch("jellyfin_mpv_shim.syncplay.set_timeout", cap):
            sp.schedule_pause(datetime.utcnow(), 50 * seconds_in_ticks)
            sp.disable_sync_play(True)   # session ends before the timer fires
            cap.fire_last()              # the late Pause timer expires now
        self.assertIsNone(sp.playerManager.paused)
        self.assertEqual(sp.playerManager.seeks, [])

    def test_scheduled_pause_runs_while_still_current(self):
        # Positive control: fired while the same generation is active, it acts.
        sp = self._enabled_manager()
        cap = CapturingTimeout()
        with mock.patch("jellyfin_mpv_shim.syncplay.set_timeout", cap):
            sp.schedule_pause(datetime.utcnow(), 50 * seconds_in_ticks)
            cap.fire_last()
        self.assertTrue(sp.playerManager.paused)
        self.assertEqual(len(sp.playerManager.seeks), 1)

    def test_rejoin_supersedes_prior_generation_callback(self):
        # AUDIT RACE (leave then rejoin): a callback armed for the old group
        # must not act after a disable+enable cycle rotates the generation, even
        # though the session is enabled again.
        sp = self._enabled_manager()
        cap = CapturingTimeout()
        with mock.patch("jellyfin_mpv_shim.syncplay.set_timeout", cap):
            sp.schedule_pause(datetime.utcnow(), 10 * seconds_in_ticks)
            armed = cap.captured[-1]
            sp.disable_sync_play(True)     # bump 1
            sp.enabled_at = datetime.utcnow()
            sp.sync_generation += 1        # models enable_sync_play's bump
            armed[0](*armed[1])            # the old timer finally fires
        self.assertIsNone(sp.playerManager.paused)
        self.assertEqual(sp.playerManager.seeks, [])

    def test_new_command_supersedes_prior_scheduled_command(self):
        # AUDIT RACE (command superseded): scheduling a new command calls
        # clear_scheduled_command, which bumps the generation so the previously
        # armed (still-pending) callback no-ops when it fires.
        sp = self._enabled_manager()
        cap = CapturingTimeout()
        with mock.patch("jellyfin_mpv_shim.syncplay.set_timeout", cap):
            sp.schedule_pause(datetime.utcnow(), 10 * seconds_in_ticks)
            stale = cap.captured[-1]
            # A newer pause command arrives and supersedes the first.
            sp.schedule_pause(datetime.utcnow(), 20 * seconds_in_ticks)
            stale[0](*stale[1])            # the superseded timer expires late
        # Only the newest callback (not fired here) would act; the stale one
        # must have no-opped, so nothing happened yet.
        self.assertIsNone(sp.playerManager.paused)
        self.assertEqual(sp.playerManager.seeks, [])

    def test_speed_restore_noops_after_disable(self):
        # The speed-to-sync restore callback restores speed=1 and re-enables
        # sync; after a disable it must leave the (disable-restored) playback
        # rate alone and not flip sync_enabled back on.
        sp = self._enabled_manager()
        sp.playback_rate = 1.5
        cap = CapturingTimeout()
        with mock.patch("jellyfin_mpv_shim.syncplay.set_timeout", cap):
            generation = sp.sync_generation

            def restore():
                if not sp._still_current(generation):
                    return
                sp.playerManager.set_speed(1)
                sp.sync_enabled = True

            sp.speed_timeout = cap(100, restore)
            sp.sync_enabled = False
            sp.disable_sync_play(True)      # restores playback_rate, bumps gen
            cap.fire_last()
        self.assertEqual(sp.playerManager.speed, 1.5)
        self.assertFalse(sp.sync_enabled)

    def test_playing_now_rearm_sync_is_defined(self):
        # Regression: schedule_play's "Playing Now" branch (hit whenever a
        # client joins a group that is already playing) calls self._rearm_sync.
        # That method was referenced but undefined for a while, so this branch
        # raised AttributeError; assert it now completes.
        sp = SyncPlayManager(FakePlayer())

        class PastTimesync:
            def server_date_to_local(self, when):
                return datetime.utcnow() - timedelta(seconds=5)

        sp.timesync = PastTimesync()
        sp.enabled_at = datetime.utcnow()
        # Real set_timeout is fine; it just spawns a daemon timer we don't fire.
        sp.schedule_play(datetime.utcnow(), 10 * seconds_in_ticks)
        # If we get here without AttributeError, the defect is fixed.
        self.assertEqual(sp.playerManager.paused, False)


if __name__ == "__main__":
    unittest.main()
