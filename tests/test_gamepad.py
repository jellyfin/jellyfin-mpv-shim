"""Game controller support: the option, and surviving an mpv without it.

mpv has SDL2 gamepad input built in, but `sdl2-gamepad` is
``value: 'disabled'`` in mpv's own ``meson.options`` -- not ``auto`` -- so
most builds simply do not have ``--input-gamepad``, and an mpv without it
does not ignore the flag, it **refuses to start**. That is the whole risk of
this feature: a user ticks a box, updates mpv, and the application will not
launch, with the box inside the application they can no longer open.

So the tests here are mostly about the failure, not the feature.
"""

import unittest
from unittest import mock

from jellyfin_mpv_shim import mpv_options
from jellyfin_mpv_shim.conf import settings

from test_mpv_options import SettingsCase


class GamepadOptionTest(SettingsCase):
    def test_it_is_absent_unless_the_user_asks(self):
        # The default path must never carry a build-gated option: somebody
        # who leaves this alone cannot be affected by any of it.
        self.set(input_gamepad=False)
        self.assertNotIn(mpv_options.GAMEPAD_OPTION, self.build("mpvtk"))

    def test_it_is_added_when_enabled(self):
        self.set(input_gamepad=True)
        self.assertIs(self.build("mpvtk")[mpv_options.GAMEPAD_OPTION], True)

    def test_the_option_name_is_the_one_mpv_knows(self):
        # Underscored in the dict python-mpv is called with, dashed on the
        # command line. Getting this wrong is silent: mpv would reject a
        # name it does not have and the feature would look build-gated
        # everywhere.
        self.assertEqual(mpv_options.GAMEPAD_OPTION, "input_gamepad")
        self.assertEqual(mpv_options.GAMEPAD_OPTION.replace("_", "-"),
                         "input-gamepad")


def _libmpv_error(name=b"input-gamepad", value=b"yes"):
    """What python-mpv raises for an option this mpv does not have."""
    return AttributeError("mpv option does not exist", -5,
                          (object(), name, value))


def _jsonipc_error(bad_option="input-gamepad"):
    """What python-mpv-jsonipc >= 1.3.0 raises, which names the option.

    Older releases flattened every start failure into "MPV process retry
    limit reached." after spending the whole retry budget on it, which is
    why the shim's floor is 1.3.0.
    """
    error = Exception("MPV rejected the option --{0}.".format(bad_option))
    error.bad_option = bad_option
    return error


class RejectedOptionTest(unittest.TestCase):
    """Reading the option name back out of two very different exceptions."""

    def setUp(self):
        from jellyfin_mpv_shim import player
        self.rejected = player._rejected_option

    def test_libmpv_puts_the_name_in_the_exception_args(self):
        self.assertEqual(self.rejected(_libmpv_error()), "input_gamepad")

    def test_jsonipc_names_it_outright(self):
        self.assertEqual(self.rejected(_jsonipc_error()), "input_gamepad")

    def test_an_unrelated_failure_blames_nothing(self):
        # The important negative. Answering with a guess here would drop an
        # option over a failure that had nothing to do with it, and hide
        # the real error behind a retry.
        self.assertIsNone(self.rejected(ValueError("something else")))
        self.assertIsNone(self.rejected(AttributeError("no args")))

    def test_a_short_or_odd_argument_tuple_is_not_indexed_blindly(self):
        self.assertIsNone(self.rejected(AttributeError("a", -5)))
        self.assertIsNone(self.rejected(AttributeError("a", -5, ("x",))))


class GamepadConstructRetryTest(unittest.TestCase):
    """`_construct_mpv` against an mpv that refuses --input-gamepad.

    Called unbound against a stand-in, as the lua tests do: importing
    `player` for real opens a window.
    """

    def _construct(self, factory, options, lua_works=None, gamepad_works=None):
        from jellyfin_mpv_shim import player

        me = mock.Mock()
        me._lua_works = lua_works
        me._gamepad_works = gamepad_works
        with mock.patch.object(player, "mpv") as fake_mpv:
            fake_mpv.MPV.side_effect = factory
            got = player.PlayerManager._construct_mpv(me, options)
        return got, fake_mpv.MPV.call_args_list, me

    @staticmethod
    def _rejects(option, exc):
        def factory(**kw):
            if option in kw:
                raise exc
            return "player"
        return factory

    def test_it_retries_without_the_option_and_starts(self):
        for label, error in (("libmpv", _libmpv_error()),
                             ("jsonipc", _jsonipc_error())):
            with self.subTest(backend=label):
                got, calls, me = self._construct(
                    self._rejects("input_gamepad", error),
                    {"input_gamepad": True, "vo": "gpu"})
                self.assertEqual(got, "player")
                self.assertEqual(len(calls), 2)
                self.assertNotIn("input_gamepad", calls[1].kwargs)
                self.assertEqual(calls[1].kwargs["vo"], "gpu")

    def test_it_records_the_answer_so_the_next_mpv_does_not_pay_again(self):
        _got, _calls, me = self._construct(
            self._rejects("input_gamepad", _libmpv_error()),
            {"input_gamepad": True})
        self.assertIs(me._gamepad_works, False)

    def test_a_remembered_answer_skips_the_failed_construction(self):
        got, calls, _me = self._construct(
            self._rejects("input_gamepad", _libmpv_error()),
            {"input_gamepad": True, "vo": "gpu"}, gamepad_works=False)
        self.assertEqual(got, "player")
        self.assertEqual(len(calls), 1, "paid for a construction it knew would fail")
        self.assertNotIn("input_gamepad", calls[0].kwargs)

    def test_dropping_the_gamepad_does_not_claim_lua_is_missing(self):
        # Two build gates, two independent answers. Conflating them would
        # drop the whole UI to the CLI because a controller option was
        # unavailable.
        _got, _calls, me = self._construct(
            self._rejects("input_gamepad", _libmpv_error()),
            {"input_gamepad": True, "osc": False})
        self.assertIs(me._gamepad_works, False)
        self.assertIsNone(me._lua_works)

    def test_an_mpv_missing_both_gated_options_still_starts(self):
        # The case the old "there is only one option this can be" reasoning
        # could not express: a minimal build with neither SDL2 gamepad nor
        # lua. It must end up running, not raising.
        def factory(**kw):
            if "input_gamepad" in kw:
                raise _libmpv_error()
            if "osc" in kw:
                raise _libmpv_error(b"osc", b"no")
            return "player"

        got, calls, me = self._construct(
            factory, {"input_gamepad": True, "osc": False, "vo": "gpu"})
        self.assertEqual(got, "player")
        self.assertEqual(len(calls), 3)
        self.assertIs(me._gamepad_works, False)
        self.assertIs(me._lua_works, False)

    def test_an_unrelated_failure_is_not_blamed_on_the_gamepad(self):
        # It still falls back to dropping --osc, which is the pre-existing
        # behaviour for any unexplained failure, and then surfaces.
        def factory(**kw):
            raise RuntimeError("the display is on fire")

        from jellyfin_mpv_shim import player
        me = mock.Mock()
        me._lua_works = None
        me._gamepad_works = None
        with mock.patch.object(player, "mpv") as fake_mpv:
            fake_mpv.MPV.side_effect = factory
            with self.assertRaises(RuntimeError):
                player.PlayerManager._construct_mpv(me, {"input_gamepad": True})
        self.assertIsNot(me._gamepad_works, False)


if __name__ == "__main__":
    unittest.main()
