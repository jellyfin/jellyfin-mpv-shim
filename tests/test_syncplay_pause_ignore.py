"""``pause_ignore`` must mean "the pause value we just commanded", nothing else.

It exists to stop a SyncPlay feedback loop. ``set_paused`` records the value it
is about to write to mpv; when mpv's ``pause`` observer fires for that change,
``_on_pause_change`` sees its own value and does not report it back to the
group. Without that, a pause we performed *because SyncPlay told us to* is
announced to SyncPlay as a local user pause, and the two bounce.

The bug these tests were written for: ``get_timeline_options`` also wrote the
flag, from the timeline thread, with mpv's *live* pause state — a periodic
latch with a different meaning from the one-shot guard. Worse, it sampled
``pause`` twenty-five lines and four mpv property reads before it wrote it, so
the value it stamped down could be badly stale. Overwriting a fresh guard with
a stale sample makes the *next* genuine local pause or unpause compare equal
and be silently swallowed: the local player changes state and the group is
never told.

The reproduction below is deterministic rather than timing-based. It uses the
one property ``get_timeline_options`` reads *after* ``pause`` to drive the user
pausing at exactly the moment the real race needs, which is the interleaving
the timeline thread hits by ordinary scheduling luck.
"""

import sys
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class FakeSyncplay:
    """Records what the player tells the group."""

    def __init__(self):
        self.enabled = True
        self.paused = 0
        self.played = 0
        self.current_group = None

    def is_enabled(self):
        return self.enabled

    def pause_request(self):
        self.paused += 1

    def play_request(self):
        self.played += 1


class FakePlayer:
    playback_abort = False
    volume = 80
    mute = False
    duration = 100.0
    cache_buffering_state = 100

    def __init__(self, on_playback_time_read=None):
        self.pause = False
        self._hook = on_playback_time_read
        self.keep_open = False

    @property
    def playback_time(self):
        # Read by get_timeline_options *after* `pause`, and before the write
        # of pause_ignore. Whatever happens here happens inside the window.
        if self._hook is not None:
            hook, self._hook = self._hook, None
            hook()
        return 12.0


class FakeVideo:
    is_tv = False
    is_transcode = False
    sid = aid = None
    item_id = "v1"
    playback_info = {"PlaySessionId": "s1"}
    media_source = {"Id": "ms1"}
    item = {"Name": "Thing", "Type": "Movie"}
    client = None

    class parent:
        queue = []
        has_next = False

    def get_duration(self):
        return 100.0

    def get_playlist_id(self):
        return "pl1"


def build_player(**kw):
    """A PlayerManager with only the state these paths touch.

    __new__ so no mpv is constructed; the attributes below are the ones
    PlayerManager.__init__ would have set.
    """
    pm = PlayerManager.__new__(PlayerManager)
    pm._player = FakePlayer(**kw)
    pm._video = FakeVideo()
    pm.syncplay = FakeSyncplay()
    pm._lock = threading.RLock()
    pm._tl_lock = threading.RLock()
    pm.pause_ignore = False
    pm.do_not_handle_pause = False
    pm.start_time = 0.0
    pm.repeat_mode = "none"
    pm.last_seek = None
    pm._last_playback_position = 0
    pm.timeline_trigger = None
    pm.on_playstate = None
    return pm


class TheGuardMeansWhatWeCommanded(unittest.TestCase):
    """Baseline behaviour the guard exists to provide."""

    def test_our_own_pause_is_not_reported_back_to_the_group(self):
        pm = build_player()
        pm.set_paused(True, True)               # force=True: as SyncPlay does
        pm._on_pause_change("pause", True)      # mpv echoes it back
        self.assertEqual(pm.syncplay.paused, 0,
                         "the player announced its own pause to the group")

    def test_a_local_pause_is_reported(self):
        pm = build_player()
        pm._player.pause = True                 # changed without set_paused
        pm._on_pause_change("pause", True)
        self.assertEqual(pm.syncplay.paused, 1)

    def test_a_local_unpause_is_reported(self):
        pm = build_player()
        pm.set_paused(True, True)               # paused by the group
        pm._on_pause_change("pause", True)
        pm._player.pause = False
        pm._on_pause_change("pause", False)
        self.assertEqual(pm.syncplay.played, 1,
                         "the user's unpause never reached the group")


class TheTimelineThreadMustNotTouchTheGuard(unittest.TestCase):
    """The regression. Each test drives the real get_timeline_options."""

    def test_a_report_does_not_overwrite_a_guard_set_during_it(self):
        """The exact interleaving: the timeline samples pause=False, the user
        pauses (guard <- True), the timeline writes its stale False."""
        pm = build_player()

        def user_pauses_mid_report():
            pm._player.pause = True
            pm._on_pause_change("pause", True)   # forwarded correctly
            pm.set_paused(True, True)            # SyncPlay echoes it back

        pm._player._hook = user_pauses_mid_report
        pm.get_timeline_options()

        self.assertIs(pm.pause_ignore, True,
                      "a stale sample from the timeline thread overwrote the "
                      "guard set while the report was in flight")

    def test_the_unpause_after_that_still_reaches_the_group(self):
        """What the user actually experiences: with the guard clobbered, the
        next unpause compares equal and is swallowed, so the local player
        resumes and the group stays paused."""
        pm = build_player()

        def user_pauses_mid_report():
            pm._player.pause = True
            pm._on_pause_change("pause", True)
            pm.set_paused(True, True)

        pm._player._hook = user_pauses_mid_report
        pm.get_timeline_options()

        pm._player.pause = False
        pm._on_pause_change("pause", False)
        self.assertEqual(pm.syncplay.played, 1,
                         "the unpause was swallowed — the local player "
                         "resumed and the group was never told")

    def test_a_plain_report_leaves_the_guard_alone(self):
        """No interleaving at all: reporting is not a pause event and has no
        business moving the flag in either direction."""
        pm = build_player()
        pm.set_paused(True, True)
        self.assertIs(pm.pause_ignore, True)
        pm._player.pause = False        # mpv drifted; only set_paused decides
        pm.get_timeline_options()
        self.assertIs(pm.pause_ignore, True,
                      "get_timeline_options moved the guard")

    def test_the_report_itself_still_carries_the_live_pause_state(self):
        """Removing the write must not change what is *reported* — IsPaused
        still comes from mpv, it just no longer leaks into the guard."""
        pm = build_player()
        pm._player.pause = True
        options = pm.get_timeline_options()
        self.assertIs(options["IsPaused"], True)


if __name__ == "__main__":
    unittest.main()
