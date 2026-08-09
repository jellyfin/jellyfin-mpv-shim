"""Claiming keys, and giving them back (#16).

The shim held `space`, `f` and the arrows for the life of the process. Now
it takes whatever currently *means* pause/seek/fullscreen, only while
something needs it, and re-issues the user's own intent through the
operations that know about SyncPlay and about remembering a choice.

An input SECTION rather than per-key bindings, because that is the one
mechanism both backends have: libmpv can unregister a key binding and
python_mpv_jsonipc cannot — it has `bind_key_press` and no unbind at all —
while define-section/enable-section/disable-section are ordinary commands on
either.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from unittest import mock                                     # noqa: E402

from jellyfin_mpv_shim.keysweep import (                      # noqa: E402
    FULLSCREEN, PAUSE, SEEK)
from jellyfin_mpv_shim.player import PlayerManager            # noqa: E402


BINDINGS = [
    {"key": "SPACE", "cmd": "cycle pause", "is_weak": True, "priority": -1},
    {"key": "p", "cmd": "cycle pause", "is_weak": True, "priority": -1},
    {"key": "PLAYONLY", "cmd": "set pause no", "is_weak": True,
     "priority": -1},
    {"key": "LEFT", "cmd": "seek -5", "is_weak": True, "priority": -1},
    {"key": "Shift+RIGHT", "cmd": "seek 1 exact", "is_weak": True,
     "priority": -1},
    {"key": "f", "cmd": "cycle fullscreen", "is_weak": True, "priority": -1},
    {"key": "m", "cmd": "cycle mute", "is_weak": True, "priority": -1},
]


class _Player:
    def __init__(self):
        self.commands = []
        self.input_bindings = list(BINDINGS)
        self.fs = False

    def command(self, *args):
        self.commands.append(args)


def _pm():
    pm = PlayerManager.__new__(PlayerManager)
    pm._player = _Player()
    pm._key_claims = {}
    pm._key_actions = {}
    pm._swept = None
    import threading
    pm._lock = threading.RLock()
    return pm


def _sections(pm):
    """{name: enabled} from what was actually commanded."""
    state = {}
    for c in pm._player.commands:
        if c[0] == "enable-section":
            state[c[1]] = True
        elif c[0] == "disable-section":
            state[c[1]] = False
    return state


def _defined(pm):
    return [c[2] for c in pm._player.commands if c[0] == "define-section"]


class ClaimLifecycleTest(unittest.TestCase):
    def test_a_claim_installs_only_the_keys_it_asked_for(self):
        pm = _pm()
        pm.claim_keys("syncplay", {PAUSE, SEEK})
        body = _defined(pm)[-1]
        self.assertIn("SPACE script-message", body)
        self.assertIn("p script-message", body)
        self.assertIn("LEFT script-message", body)
        self.assertNotIn("f script-message", body, "fullscreen not claimed")
        self.assertNotIn("m script-message", body, "mute is not ours")
        self.assertTrue(_sections(pm)[PlayerManager.KEY_SECTION])

    def test_releasing_the_last_claim_disables_the_section(self):
        pm = _pm()
        pm.claim_keys("syncplay", {PAUSE})
        pm.claim_keys("syncplay", None)
        self.assertFalse(_sections(pm)[PlayerManager.KEY_SECTION])
        self.assertEqual(pm._key_actions, {})

    def test_owners_are_independent(self):
        """SyncPlay joining a group must not disturb the standing
        fullscreen claim, and leaving must not take it away."""
        pm = _pm()
        pm.claim_keys("fullscreen", {FULLSCREEN})
        pm.claim_keys("syncplay", {PAUSE, SEEK})
        pm.claim_keys("syncplay", None)
        body = _defined(pm)[-1]
        self.assertIn("f script-message", body)
        self.assertNotIn("SPACE script-message", body)
        self.assertTrue(_sections(pm)[PlayerManager.KEY_SECTION])

    def test_the_sweep_does_not_see_our_own_section(self):
        """The lines we install are non-weak, so a re-sweep would find them
        winning every claimed key, classify them as nothing, and quietly
        drop the claim on the next refresh."""
        pm = _pm()
        pm.claim_keys("syncplay", {PAUSE})
        # mpv would now report our section back to us.
        pm._player.input_bindings = BINDINGS + [
            {"key": "SPACE", "cmd": "script-message jms-key pause SPACE",
             "is_weak": False, "priority": 0}]
        pm.claim_keys("fullscreen", {FULLSCREEN})
        body = _defined(pm)[-1]
        self.assertIn("SPACE script-message", body,
                      "the claim was dropped by a re-sweep of our own lines")


class DispatchTest(unittest.TestCase):
    """A claimed key re-issues the user's intent, through the shim."""

    def _armed(self):
        pm = _pm()
        pm.claim_keys("syncplay", {PAUSE, SEEK})
        pm.claim_keys("fullscreen", {FULLSCREEN})
        return pm

    def test_a_toggle_key_toggles(self):
        pm = self._armed()
        with mock.patch.object(pm, "toggle_pause") as toggle:
            pm._on_claimed_key(PAUSE, "SPACE")
        toggle.assert_called_once_with()

    def test_a_play_only_key_does_not_toggle(self):
        """PLAYONLY is `set pause no`. Answering it with a toggle would
        pause a playing file from the key whose entire job is not to."""
        pm = self._armed()
        with mock.patch.object(pm, "set_paused") as setp, \
                mock.patch.object(pm, "toggle_pause") as toggle:
            pm._on_claimed_key(PAUSE, "PLAYONLY")
        toggle.assert_not_called()
        setp.assert_called_once_with(False)

    def test_a_seek_carries_the_users_amount_and_exactness(self):
        pm = self._armed()
        with mock.patch.object(pm, "seek") as seek:
            pm._on_claimed_key(SEEK, "LEFT")
            pm._on_claimed_key(SEEK, "Shift+RIGHT")
        self.assertEqual(
            [c.args or c.kwargs for c in seek.call_args_list],
            [(-5.0,), (1.0,)])
        self.assertEqual([c.kwargs for c in seek.call_args_list],
                         [{"exact": False}, {"exact": True}])

    def test_fullscreen_records_the_users_intent(self):
        # The whole reason the key is claimed at all: a change that came
        # through our binding is user-initiated by construction, where an
        # observer plus an ignore flag needs that flag set and cleared
        # around every self-initiated change.
        pm = self._armed()
        with mock.patch.object(pm, "set_fullscreen") as fs:
            pm._on_claimed_key(FULLSCREEN, "f")
        fs.assert_called_once_with(True, persist=True)

    def test_an_unknown_key_is_ignored(self):
        pm = self._armed()
        with mock.patch.object(pm, "toggle_pause") as toggle:
            pm._on_claimed_key(PAUSE, "nosuchkey")
        toggle.assert_not_called()

    def test_a_released_key_does_nothing(self):
        pm = self._armed()
        pm.claim_keys("syncplay", None)
        with mock.patch.object(pm, "toggle_pause") as toggle:
            pm._on_claimed_key(PAUSE, "SPACE")
        toggle.assert_not_called()


class SyncPlayClaimTest(unittest.TestCase):
    """Joining a group claims pause and seek; leaving gives them back."""

    def _sp(self):
        from jellyfin_mpv_shim.syncplay import SyncPlayManager

        sp = SyncPlayManager.__new__(SyncPlayManager)
        sp.playerManager = mock.Mock()
        return sp

    def test_joining_claims_and_leaving_releases(self):
        sp = self._sp()
        sp._claim_keys(True)
        sp.playerManager.claim_keys.assert_called_once_with(
            "syncplay", {PAUSE, SEEK})
        sp.playerManager.claim_keys.reset_mock()
        sp._claim_keys(False)
        sp.playerManager.claim_keys.assert_called_once_with("syncplay", None)

    def test_a_player_that_cannot_do_it_does_not_stop_a_join(self):
        sp = self._sp()
        sp.playerManager = object()          # no claim_keys at all
        sp._claim_keys(True)                 # must not raise

    def test_a_raising_player_does_not_stop_a_join(self):
        sp = self._sp()
        sp.playerManager.claim_keys.side_effect = RuntimeError("boom")
        sp._claim_keys(True)                 # must not raise


if __name__ == "__main__":
    unittest.main()
