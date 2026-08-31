"""A remote seeks the same distance the keyboard does.

#16 gave the arrow keys back to mpv, so a seek distance lives in the
user's `input.conf` and nowhere else -- the keyboard reads it from there
whether the key is mpv's own or one the shim claimed. `kb_seek`, the path
a phone or the web remote takes, kept reading `settings.seek_*` instead.

So the two diverged: after the migration wrote `up seek 30` into
input.conf and cleared the setting back to 60, the arrow key seeked 30 and
the remote's Up seeked 60, on the same machine, in the same session.

The settings are gone now (config version 4), which fixes it by removing
the second source of truth rather than by keeping two in step.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.player import PlayerManager        # noqa: E402


class _Fake(PlayerManager):
    """Enough PlayerManager to answer a seek, and nothing else."""

    def __init__(self, swept, web_seek=False):
        self._swept_result = swept
        self.seeks = []
        self._web = web_seek

    def _swept_keys(self):
        return self._swept_result

    def seek(self, amount, exact=False, **kw):
        self.seeks.append((amount, exact))

    def get_seek_times(self):
        return (-15.0, 30.0)


def _pm(swept, web_seek=False):
    pm = _Fake(swept, web_seek)
    return pm


def _settings(**kw):
    from jellyfin_mpv_shim.conf import Settings
    return Settings().parse_obj(dict(Settings().dict(), **kw))


class RemoteSeekTest(unittest.TestCase):
    def _seek(self, action, swept, **settings_kw):
        pm = _pm(swept)
        with mock.patch("jellyfin_mpv_shim.player.settings",
                        _settings(**settings_kw)):
            pm.kb_seek(action)
        return pm.seeks

    # The tuples below are what `keysweep.sweep` ACTUALLY returns:
    # `(mpv key name, semantic, parsed argument)`. They used to say
    # `("up", "seek", "seek 30")` -- lowercase, and the raw command -- which
    # is neither. That fixture agreed with the two bugs in
    # `_seek_like_the_keyboard` (it compared `RIGHT` with `right`, and fed the
    # already-parsed argument back to the command parser), so the tests passed
    # while every remote arrow silently used the stock distance. A stand-in
    # that models the shape the caller assumes rather than the shape the
    # callee returns cannot fail.

    def test_it_uses_the_distance_in_mpvs_binding(self):
        # The migrated case: input.conf says `up seek 30`, so the remote's
        # Up must seek 30 -- not the 60 the deleted setting used to hold.
        got = self._seek("up", [("UP", "seek", (30.0, False))])
        self.assertEqual(got, [(30.0, False)])

    def test_it_carries_exactness_too(self):
        got = self._seek("right", [("RIGHT", "seek", (5.0, True))])
        self.assertEqual(got, [(5.0, True)])

    def test_an_mpv_that_says_nothing_gets_mpvs_own_defaults(self):
        # No binding swept (an mpv whose input-bindings could not be read).
        # 60/-60/5/-5 is what mpv itself binds, so the remote still behaves
        # like the keyboard -- it just cannot see a customisation.
        for action, want in (("up", 60), ("down", -60),
                             ("right", 5), ("left", -5)):
            with self.subTest(action):
                self.assertEqual(self._seek(action, []), [(want, False)])

    def test_it_ignores_a_binding_for_a_different_key(self):
        got = self._seek("up", [("DOWN", "seek", (-30.0, False))])
        self.assertEqual(got, [(60, False)])

    def test_a_non_seek_binding_on_the_key_is_not_read_as_a_distance(self):
        got = self._seek("up", [("UP", "pause", None)])
        self.assertEqual(got, [(60, False)])

    def test_web_seek_replaces_the_distance_by_sign(self):
        # jellyfin-web's variable seek: mpv cannot express it, so it is
        # applied here, routed by the sign of whatever the binding says --
        # a binding tells you which way it goes, never which arrow it was.
        self.assertEqual(
            self._seek("up", [("UP", "seek", (30.0, False))], use_web_seek=True),
            [(30.0, False)])
        self.assertEqual(
            self._seek("down", [("DOWN", "seek", (-30.0, False))],
                       use_web_seek=True),
            [(-15.0, False)])

    def test_a_swept_read_that_raises_falls_back(self):
        class Boom(_Fake):
            def _swept_keys(self):
                raise RuntimeError("mpv went away")

        pm = Boom([])
        with mock.patch("jellyfin_mpv_shim.player.settings", _settings()):
            pm.kb_seek("up")
        self.assertEqual(pm.seeks, [(60, False)])

    def test_a_non_seek_action_still_reaches_the_menu(self):
        pm = _pm([])
        pm.menu = mock.Mock()
        with mock.patch("jellyfin_mpv_shim.player.settings", _settings()):
            pm.kb_seek("home")
        pm.menu.menu_action.assert_called_once_with("home")
        self.assertEqual(pm.seeks, [])


class SettingsAreGoneTest(unittest.TestCase):
    def test_the_seek_settings_are_no_longer_in_the_schema(self):
        """They were dead: never in the settings UI or the README, and
        after #16 a changed distance only bought a claim that then seeked
        by mpv's amount anyway. [iw]: "should drop the dead settings
        post-migration."
        """
        from jellyfin_mpv_shim.conf import Settings

        for name in ("seek_up", "seek_down", "seek_right", "seek_left",
                     "seek_v_exact", "seek_h_exact"):
            with self.subTest(name):
                self.assertNotIn(name, Settings.__annotations__)

    def test_a_stale_value_in_a_config_file_is_ignored(self):
        # parse_obj only reads annotated keys, so an old conf.json keeps
        # working and the key drops out on the next save.
        from jellyfin_mpv_shim.conf import Settings

        s = Settings().parse_obj({"seek_up": 30, "always_transcode": True})
        self.assertFalse(hasattr(s, "seek_up"))
        self.assertIs(s.always_transcode, True)


if __name__ == "__main__":
    unittest.main()
