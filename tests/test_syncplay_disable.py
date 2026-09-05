# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

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
        self.video = None
        self.stopped = []
        #: (owner, semantics) per claim_keys call; None semantics is a
        #: release. See claim_keys below.
        self.key_claims = []
        self.syncplay_notices = 0

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

    # The rest of the contract SyncPlayManager may call. Present even though
    # these tests never reach them: a stand-in that is a subset of the real
    # player turns "this path is untested" into "this path raises
    # AttributeError and the suite calls it a pass".
    # tests/test_syncplay_player_contract.py is what keeps this honest.
    def get_time(self):
        return 0.0

    def get_video(self):
        return self.video

    def has_video(self):
        return self.video is not None

    def is_not_paused(self):
        return not self.paused

    def get_current_client(self):
        return None

    def play(self, video, offset=None, **kwargs):
        self.video = video

    def stop(self, leave_group=True):
        self.stopped.append(leave_group)

    def put_task(self, func, *args):
        func(*args)

    def send_timeline(self):
        pass

    def timeline_handle(self):
        pass

    def upd_player_hide(self):
        pass

    # --- the key claim (tests/test_syncplay_player_contract.py) ---
    #
    # Reached through `getattr(self.playerManager, "...", None)` in
    # syncplay.py, so until the contract extractor learned that spelling
    # these three were invisible: no stand-in had them, getattr answered
    # None on every test player, and replacing both `_claim_keys` call
    # sites with `pass` -- deleting the feature -- left 34 tests green.
    #
    # Recorders rather than no-ops, so a test can assert the claim
    # HAPPENED and not merely that it did not raise.
    def claim_keys(self, owner, semantics=None):
        self.key_claims.append((owner, semantics))

    def on_syncplay_change(self):
        self.syncplay_notices += 1

    def notify_syncplay(self, *args, **kwargs):
        self.syncplay_notices += 1


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


class LeavingReturnsToAFreshState(unittest.TestCase):
    """Whatever a session did to this object, leaving must undo it.

    Every suite in the tree seats a manager in one group and never takes it
    out again, so nothing could see state crossing that boundary — and three
    fields did. The generic test is the one that keeps paying: enumerating
    fields by hand is how the list got out of date in the first place.
    """

    #: Fields that legitimately outlive a session, and why.
    CARRIED = {
        "playerManager", "menu",        # collaborators, not state
        "client",                       # the server we left; enable reuses it
        "min_buffer_thresh_ms",         # a constant
        "sync_generation",              # must only ever go up
        "last_sync_time",               # a throttle stamp, re-read on use
        "playback_rate",                # the speed to restore, read by disable
        "read_callback",                # replaced wholesale by the next enable
        "notify_sync_ready",            # ditto
        "time_offset", "round_trip_duration",   # timesync's, not the group's
    }

    def _session(self):
        """A manager that has been through a group and left it, having done
        the things a session does — including stalling, which is the case that
        cannot clean up after itself (on_buffer_done sits behind is_enabled)."""
        sp = SyncPlayManager(FakePlayer())
        sp.enabled_at = datetime.utcnow()
        sp.playback_rate = 1.0
        sp.current_group = "g1"
        sp.sync_enabled = True
        sp.last_command = {"Command": "Unpause", "When": datetime.utcnow(),
                           "PositionTicks": 0}
        sp.attempts = 7                    # a bad evening for the network
        sp.enable_speed_sync = False       # gave up on speed corrections
        sp.playback_diff_ms = 4200
        sp.method = "Speed"
        sp.on_buffer()                     # mpv stalled...
        sp.is_buffering = True             # ...and the debounce fired
        sp.disable_sync_play(True)         # user backs out / GroupLeft
        return sp

    def test_leaving_restores_every_field_it_should(self):
        fresh = vars(SyncPlayManager(FakePlayer()))
        left = vars(self._session())
        differing = {k for k in fresh
                     if k not in self.CARRIED and left[k] != fresh[k]}
        self.assertEqual(differing, set(),
                         "state survived leaving the group; it will be "
                         "inherited by whatever group is joined next")

    def test_the_next_group_gets_drift_correction(self):
        """The consequence, stated in the terms the user would notice: a
        latched is_buffering makes sync_playback_time return at its first
        guard forever, so nobody in the next group is ever pulled back into
        line."""
        sp = self._session()
        self.assertFalse(sp.is_buffering)
        self.assertIsNone(sp.last_playback_waiting)

    def test_the_next_group_gets_a_full_attempt_budget(self):
        """attempts is reset in exactly one place — the in-sync branch — which
        a session that already gave up (sync_enabled False) cannot reach. It
        arrived at the next group already over the cap."""
        self.assertEqual(self._session().attempts, 0)
        self.assertTrue(self._session().enable_speed_sync)

    def test_a_buffer_debounce_cannot_fire_into_the_next_session(self):
        """on_buffer's callback guards on a timestamp, and leaving did not
        clear it (halting did). Armed a second before GroupLeft, it landed
        afterwards and set is_buffering on a manager in no group."""
        armed = []
        import jellyfin_mpv_shim.syncplay as syncplay_module
        real = syncplay_module.set_timeout
        syncplay_module.set_timeout = (
            lambda delay, action, *args: armed.append((action, args)) or None)
        self.addCleanup(lambda: setattr(syncplay_module, "set_timeout", real))

        sp = SyncPlayManager(FakePlayer())
        sp.enabled_at = datetime.utcnow()
        sp.playback_rate = 1.0
        sp.on_buffer()
        sp.disable_sync_play(True)

        for action, args in armed:
            action(*args)
        self.assertFalse(sp.is_buffering,
                         "a callback from the group we left is still running")


if __name__ == "__main__":
    unittest.main()


class KeyClaimLifecycleTest(unittest.TestCase):
    """Joining a group claims pause+seek; leaving gives them back.

    The existence half is `tests/test_syncplay_player_contract.py`. This
    is the other half, and it is the one that was missing: the contract
    proves `claim_keys` EXISTS on every player, not that SyncPlay ever
    calls it. Replacing both `_claim_keys` call sites with `pass` --
    deleting the feature outright -- left every SyncPlay suite green,
    because the calls go through `getattr(..., None)` and each fake
    answered None.

    Without the claim, a group joined mid-playback keeps mpv's local
    `cycle pause`, which in a group is not a pause at all: it desyncs the
    member instead of asking the server to pause everyone.
    """

    class _Timesync(FakeTimesync):
        def subscribe_time_offset(self, _cb):
            pass

        def force_update(self):
            pass

        def remove_subscriber(self, _cb):
            pass

        def stop_ping(self):
            pass

    class _Client:
        pass

    def _joined(self, player=None):
        """A manager that has been through the REAL enable path.

        The other tests in this file set `enabled_at` by hand and say why
        ("without going through the client-heavy enable_sync_play path").
        That shortcut is exactly what this class cannot take: the claim is
        made *by* enable_sync_play, so a test that skips it would assert
        against a group nobody joined.
        """
        sp = SyncPlayManager(player or FakePlayer())
        client = self._Client()
        client.timesync = self._Timesync()
        sp.client = client
        sp.enable_sync_play(from_server=True)
        return sp

    def test_joining_claims_pause_and_seek(self):
        from jellyfin_mpv_shim.keysweep import PAUSE, SEEK

        sp = self._joined()
        claims = sp.playerManager.key_claims
        self.assertTrue(claims, "joining a group claimed no keys")
        owner, semantics = claims[-1]
        self.assertEqual(owner, "syncplay")
        self.assertEqual(semantics, {PAUSE, SEEK})

    def test_leaving_releases_them(self):
        sp = self._joined()
        sp.disable_sync_play(from_server=True)
        owner, semantics = sp.playerManager.key_claims[-1]
        self.assertEqual(owner, "syncplay")
        self.assertIsNone(semantics, "left the group still holding the keys")

    def test_the_renderer_is_told_both_times(self):
        """Its own pause paths -- click-to-pause, the summon key,
        right-click in mpv modality -- hand over to Python only while a
        group is on, so they have to be told when that changes."""
        sp = self._joined()
        after_join = sp.playerManager.syncplay_notices
        self.assertGreater(after_join, 0)
        sp.disable_sync_play(from_server=True)
        self.assertGreater(sp.playerManager.syncplay_notices, after_join)

    def test_a_player_that_cannot_claim_still_joins(self):
        """Best effort by design: an old mpv or a stand-in must not stop a
        group being joined. The `getattr` default is what makes that true,
        and it is also what hid the feature -- so it is worth pinning that
        the tolerance is deliberate rather than accidental."""
        class Older(FakePlayer):
            # None rather than absent, which is the same thing to the
            # `getattr(..., None)` guard and does not need the attribute
            # deleted off an instance.
            claim_keys = None
            on_syncplay_change = None

        sp = self._joined(Older())                 # must not raise
        self.assertTrue(sp.is_enabled())
