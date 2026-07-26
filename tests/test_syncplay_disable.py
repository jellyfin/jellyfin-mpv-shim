import unittest
from datetime import datetime, timedelta

# Importing syncplay must be side-effect safe: it must not pull in player.py
# (which needs libmpv) at module import time.
from jellyfin_mpv_shim.syncplay import SyncPlayManager, seconds_in_ticks


class FakeMenu:
    is_menu_shown = False


class FakePlayer:
    """Minimal stand-in for PlayerManager covering what disable/schedule touch."""

    def __init__(self):
        self.menu = FakeMenu()
        self.speed = 1.0
        self.paused = None
        self.seeks = []

    def get_speed(self):
        return self.speed

    def set_speed(self, speed):
        self.speed = speed

    def set_paused(self, paused, *args):
        self.paused = paused

    def seek(self, *args, **kwargs):
        self.seeks.append((args, kwargs))

    def show_text(self, *args, **kwargs):
        pass


class FakeTimesync:
    """server_date_to_local always returns a time in the past, so scheduled
    pause/seek callbacks execute synchronously (the "now" branch)."""

    def server_date_to_local(self, when):
        return datetime.utcnow() - timedelta(seconds=10)


class DisableSyncPlayTests(unittest.TestCase):
    def _enabled_manager(self):
        sp = SyncPlayManager(FakePlayer())
        # Pretend a group is active without going through the (client-heavy)
        # enable_sync_play() path.
        sp.enabled_at = datetime.utcnow()
        sp.playback_rate = 1.0
        return sp

    def test_disable_clears_all_scheduled_state(self):
        sp = self._enabled_manager()

        calls = {"scheduled": False, "sync_timeout": False, "speed_timeout": False}
        sp.scheduled_command = lambda: calls.__setitem__("scheduled", True)
        sp.sync_timeout = lambda: calls.__setitem__("sync_timeout", True)
        sp.speed_timeout = lambda: calls.__setitem__("speed_timeout", True)
        sp.sync_enabled = True

        gen_before = sp.sync_generation
        sp.disable_sync_play(True)

        # Every scheduled TimeoutThread had its stop() invoked...
        self.assertTrue(calls["scheduled"])
        self.assertTrue(calls["sync_timeout"])
        self.assertTrue(calls["speed_timeout"])

        # ...and the references were dropped so nothing lingers.
        self.assertIsNone(sp.scheduled_command)
        self.assertIsNone(sp.sync_timeout)
        self.assertIsNone(sp.speed_timeout)

        # Session is fully disabled and the generation advanced.
        self.assertFalse(sp.is_enabled())
        self.assertFalse(sp.sync_enabled)
        self.assertGreater(sp.sync_generation, gen_before)

    def test_disable_restores_playback_rate(self):
        sp = self._enabled_manager()
        sp.playback_rate = 1.5
        sp.playerManager.set_speed(0.75)  # mid speed-to-sync

        sp.disable_sync_play(True)

        self.assertEqual(sp.playerManager.speed, 1.5)

    def test_scheduled_callback_noops_after_disable(self):
        # A pause/seek scheduled while the session is disabled must not touch
        # the player (belt-and-braces guard for a callback that fires late).
        sp = SyncPlayManager(FakePlayer())
        sp.timesync = FakeTimesync()
        sp.enabled_at = None  # disabled

        sp.schedule_pause(datetime.utcnow(), 100 * seconds_in_ticks)

        self.assertIsNone(sp.playerManager.paused)
        self.assertEqual(sp.playerManager.seeks, [])

    def test_scheduled_callback_runs_when_enabled(self):
        # Positive control: with the session enabled the same path does act.
        sp = SyncPlayManager(FakePlayer())
        sp.timesync = FakeTimesync()
        sp.enabled_at = datetime.utcnow()

        sp.schedule_pause(datetime.utcnow(), 100 * seconds_in_ticks)

        self.assertTrue(sp.playerManager.paused)
        self.assertEqual(len(sp.playerManager.seeks), 1)


if __name__ == "__main__":
    unittest.main()
