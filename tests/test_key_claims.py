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
    {"key": "WHEEL_LEFT", "cmd": "seek -10", "is_weak": True,
     "priority": -1},
    {"key": "WHEEL_RIGHT", "cmd": "seek 10", "is_weak": True,
     "priority": -1},
    {"key": "WHEEL_UP", "cmd": "add volume 2", "is_weak": True,
     "priority": -1},
    {"key": "MBTN_LEFT_DBL", "cmd": "cycle fullscreen", "is_weak": True,
     "priority": -1},
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
    pm._swept_ptr = None
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


class PointerSuppressionTest(unittest.TestCase):
    """The pointer is never claimed -- it is the renderer's -- but a seek
    nobody reports is a desync a group then corrects, and mpv binds
    WHEEL_LEFT/RIGHT to one.

    [iw]: "should just disable wheel seek during syncplay." Suppressed for
    the duration rather than routed: a wheel gesture delivers dozens of
    notches, all to reach an operation the group would refuse anyway.
    """

    def test_a_seek_claim_suppresses_the_wheel(self):
        pm = _pm()
        pm.claim_keys("syncplay", {PAUSE, SEEK})
        body = _defined(pm)[-1]
        self.assertIn("WHEEL_LEFT ignore", body)
        self.assertIn("WHEEL_RIGHT ignore", body)

    def test_it_suppresses_only_what_the_pointer_means_for_seeking(self):
        pm = _pm()
        pm.claim_keys("syncplay", {SEEK})
        body = _defined(pm)[-1]
        self.assertNotIn("WHEEL_UP", body, "volume is not ours to take")
        self.assertNotIn("MBTN_LEFT_DBL", body,
                         "fullscreen on the pointer is the renderer's")

    def test_the_pointer_is_still_never_claimed(self):
        pm = _pm()
        pm.claim_keys("syncplay", {PAUSE, SEEK})
        self.assertNotIn("WHEEL_LEFT", pm._key_actions,
                         "suppressed is not the same as claimed")
        body = _defined(pm)[-1]
        self.assertNotIn("WHEEL_LEFT script-message", body)

    def test_a_fullscreen_only_claim_suppresses_nothing(self):
        pm = _pm()
        pm.claim_keys("fullscreen", {FULLSCREEN})
        self.assertNotIn("ignore", _defined(pm)[-1])

    def test_releasing_gives_the_wheel_back(self):
        pm = _pm()
        pm.claim_keys("syncplay", {SEEK})
        pm.claim_keys("syncplay", None)
        self.assertFalse(_sections(pm)[PlayerManager.KEY_SECTION])


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


class SeekClaimGateTest(unittest.TestCase):
    """Whether the shim's seek does something mpv's cannot.

    The four distances default to 60/-60/5/-5, which is `seek 60` /
    `seek -60` / `seek 5` / `seek -5` character for character — so in a
    default configuration the old arrow bindings existed to reimplement
    mpv's arrows exactly. Where the shim IS different the keys are now
    claimed rather than bound, which follows a remapped key.
    """

    def _differs(self, **kw):
        import sys

        sys.argv = [sys.argv[0]]
        from jellyfin_mpv_shim.conf import Settings

        s = Settings().parse_obj(dict(Settings().dict(), **kw))
        pm = PlayerManager.__new__(PlayerManager)
        with mock.patch("jellyfin_mpv_shim.player.settings", s):
            return pm._seek_is_ours()

    def test_a_default_config_claims_nothing(self):
        # Against a SAVED config -- every key present -- because that is
        # what every real install looks like.
        self.assertFalse(self._differs())

    def test_a_changed_distance_claims_until_it_is_migrated(self):
        self.assertTrue(self._differs(seek_up=30))

    def test_an_exact_flag_claims(self):
        self.assertTrue(self._differs(seek_h_exact=True))

    def test_web_seek_claims(self):
        self.assertTrue(self._differs(use_web_seek=True))

    def test_skip_intro_on_seek_needs_no_claim(self):
        """[iw]: "doesn't seek to skip intro listen for seeks too?" It does
        — `_on_seeking` observes the `seeking` property and applies it to
        any forward seek, mpv's own bindings included, which is what its
        comment has always said. Claiming a key for it would double-handle,
        since the claim's own self.seek() raises that same observer."""
        self.assertFalse(self._differs(skip_intro_on_seek=True))

    def test_a_remapped_MENU_key_does_not_claim_a_seek(self):
        """The split: kb_menu_* is the menu's key and says nothing about
        seeking. [iw]: "the menu logically uses arrow keys and the only
        other thing I could see someone binding those to are wasd." """
        self.assertFalse(self._differs(kb_menu_up="w"))


class AfterMigrationTest(unittest.TestCase):
    """What the config looks like on the run AFTER the migration.

    The review's worst finding was that the migration cleared the arrow
    settings while the gate still said "ours", so the key reached neither
    the menu nor the shim. Splitting the menu key from the seek distance is
    what makes that unreachable: the migration touches distances only, and
    the menu has its own key which nothing migrates.
    """

    def _settings(self, **kw):
        import sys

        sys.argv = [sys.argv[0]]
        from jellyfin_mpv_shim.conf import Settings

        return Settings().parse_obj(dict(Settings().dict(), **kw))

    def _migrated(self, **kw):
        import os
        import tempfile

        from jellyfin_mpv_shim import input_conf

        s = self._settings(**kw)
        input_conf.migrate(s, os.path.join(tempfile.mkdtemp(), "input.conf"))
        return s

    def test_a_migration_never_touches_a_menu_key(self):
        s = self._migrated(seek_up=30, seek_h_exact=True, kb_pause="P")
        for key in ("kb_menu_left", "kb_menu_right", "kb_menu_up",
                    "kb_menu_down"):
            self.assertEqual(getattr(s, key), getattr(type(s), key),
                             "%s is the MENU's key and nothing seeks with "
                             "it; migrating a distance must not disturb it"
                             % key)

    def test_the_claim_stops_once_the_distance_has_moved(self):
        """Otherwise the keys stay claimed and the handler uses the
        binding's amount, so the distance the user configured would be
        migrated into input.conf and then ignored."""
        s = self._migrated(seek_up=30)
        pm = PlayerManager.__new__(PlayerManager)
        with mock.patch("jellyfin_mpv_shim.player.settings", s):
            self.assertFalse(pm._seek_is_ours())

    def test_an_inexpressible_feature_keeps_its_claim(self):
        # web seek has no mpv equivalent, so nothing is migrated and the
        # claim is what delivers it -- on whatever key actually seeks.
        s = self._migrated(use_web_seek=True, seek_up=30)
        self.assertEqual(s.seek_up, 30, "nothing should have been migrated")
        pm = PlayerManager.__new__(PlayerManager)
        with mock.patch("jellyfin_mpv_shim.player.settings", s):
            self.assertTrue(pm._seek_is_ours())
