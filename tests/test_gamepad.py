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




class BindingTableTest(unittest.TestCase):
    """`gamepad.bindings` -- the table itself."""

    def test_swap_moves_the_two_face_buttons_and_nothing_else(self):
        from jellyfin_mpv_shim import gamepad

        plain = {k: rest for k, *rest in gamepad.bindings()}
        swapped = {k: rest
                   for k, *rest in gamepad.bindings(swap_confirm=True)}
        self.assertEqual(set(plain), set(swapped))
        moved = {k for k in plain if plain[k] != swapped[k]}
        self.assertEqual(moved, {gamepad.CONFIRM_BUTTON, gamepad.BACK_BUTTON})
        # ...and they trade, rather than both becoming one thing. A swap
        # that assigned instead of exchanging would leave the pad with two
        # back buttons and no way to select anything.
        self.assertEqual(swapped[gamepad.CONFIRM_BUTTON],
                         plain[gamepad.BACK_BUTTON])
        self.assertEqual(swapped[gamepad.BACK_BUTTON],
                         plain[gamepad.CONFIRM_BUTTON])

    def test_every_key_is_bound_once(self):
        from jellyfin_mpv_shim import gamepad

        for swap in (False, True):
            keys = [row[0] for row in gamepad.bindings(swap)]
            self.assertEqual(len(keys), len(set(keys)), swap)

    def test_the_seek_directions_are_ones_kb_seek_answers(self):
        # The renderer hands these straight to PlayerManager.kb_seek, which
        # sends anything it does not recognise to the OSD menu instead --
        # so a typo here is a stick that silently opens a menu rather than
        # seeking, and nothing else would notice.
        from jellyfin_mpv_shim import gamepad
        from jellyfin_mpv_shim.player import PlayerManager

        known = set(PlayerManager._DEFAULT_SEEK)
        got = {arg for _k, kind, arg, _r in gamepad.bindings()
               if kind == gamepad.SEEK}
        self.assertTrue(got)
        self.assertLessEqual(got, known)

    def test_the_nav_actions_are_ones_menu_action_answers(self):
        # Same shape of risk one door along: menu_action's final fallback
        # is kb_seek, so an action it does not know reaches the OSD menu.
        from jellyfin_mpv_shim import gamepad
        from jellyfin_mpv_shim.player import PlayerManager

        # No literal for "menu" in here. It is already in _NAV_KEYPRESS,
        # and whitelisting it would make this a set-theory identity: `got`
        # is exactly {"menu"}, so `got <= known` would hold however
        # menu_action changed, including if the action stopped being
        # handled at all.
        known = (set(PlayerManager._NAV_KEYPRESS)
                 | set(PlayerManager._NAV_COMMANDS))
        got = {arg for _k, kind, arg, _r in gamepad.bindings()
               if kind == gamepad.NAV}
        self.assertTrue(got)
        self.assertLessEqual(got, known)

    def test_the_right_stick_seeks_and_the_left_one_does_not(self):
        # The asymmetry IS the feature: a controller has two sticks and
        # does not have to share one set of directions the way a keyboard
        # does. Binding the left stick to a seek is the bug this replaced.
        from jellyfin_mpv_shim import gamepad

        for key, kind, _arg, _rate in gamepad.bindings():
            if "RIGHT_STICK" in key:
                self.assertEqual(kind, gamepad.SEEK, key)
            elif "LEFT_STICK" in key or "DPAD" in key:
                self.assertEqual(kind, gamepad.KEY, key)

    def test_nothing_a_press_MEANS_auto_repeats(self):
        # Holding a button is not a request to press it again. An
        # auto-repeating Select activates whatever it lands on over and
        # over, and an auto-repeating Back walks out of the app.
        from jellyfin_mpv_shim import gamepad

        held = {"UP", "DOWN", "LEFT", "RIGHT", "PGUP", "PGDWN"}
        for key, kind, arg, rate in gamepad.bindings():
            repeats = rate > 0
            wanted = kind == gamepad.SEEK or arg in held
            self.assertEqual(repeats, wanted, key)

    def test_a_held_control_is_slower_than_mpvs_own_repeat(self):
        # mpv's --input-ar-rate default is 40 a second. Anything at or
        # near that is the bug: [iw] "it spams inputs way faster than I can
        # control them".
        from jellyfin_mpv_shim import gamepad

        for key, _kind, _arg, rate in gamepad.bindings():
            if rate:
                self.assertGreaterEqual(rate, 1 / 15.0, key)

    def test_a_seek_repeats_more_slowly_than_a_selection_moves(self):
        """A direction repeat moves one row; a seek repeat moves real time,
        so at the same rate a resting thumb crosses a film in seconds.

        Asserted on the TABLE, not on the two constants. Comparing
        `SEEK_REPEAT > DIRECTION_REPEAT` says nothing about what the rows
        actually carry -- putting DIRECTION_REPEAT on the right stick
        satisfies it, and satisfies every other rate test here too
        (`test_nothing_a_press_MEANS_auto_repeats` only asks for > 0, and
        the mpv-rate one only for >= 1/15).
        """
        from jellyfin_mpv_shim import gamepad

        rates = {}
        for key, kind, arg, rate in gamepad.bindings():
            if kind == gamepad.SEEK:
                rates.setdefault("seek", set()).add(rate)
            elif arg in ("UP", "DOWN", "LEFT", "RIGHT"):
                rates.setdefault("direction", set()).add(rate)
            elif arg in ("PGUP", "PGDWN"):
                rates.setdefault("page", set()).add(rate)
        # One rate per class, or "the seek is slower than a direction" is
        # not a statement about the table.
        for name, seen in rates.items():
            self.assertEqual(len(seen), 1, "%s rows disagree: %r"
                             % (name, seen))
        self.assertGreater(min(rates["seek"]), min(rates["direction"]))
        # A page is a jump, so it sits between them.
        self.assertGreater(min(rates["page"]), min(rates["direction"]))
        self.assertLess(min(rates["page"]), min(rates["seek"]))

    def test_it_is_json_safe(self):
        # It is sent to the renderer as JSON; a tuple would arrive as a
        # list anyway, so the table says so here rather than round-tripping
        # into a different shape than the tests assert on.
        import json

        from jellyfin_mpv_shim import gamepad

        table = gamepad.bindings()
        self.assertEqual(json.loads(json.dumps(table)), table)


class SdlSignalHandlerTest(unittest.TestCase):
    """SIGTERM survives having gamepad input on.

    SDL installs its own SIGINT/SIGTERM handlers, but only over a handler
    still at SIG_DFL -- which is why standalone mpv is fine and the shim is
    not: mpv installs its handlers first, and CPython leaves SIGTERM at the
    default. In process, SDL took it and turned it into an SDL_QUIT that
    mpv's gamepad loop drops, so the app could not be killed by anything
    short of SIGKILL. Measured, with no controller attached.
    """

    def setUp(self):
        import os

        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(),
                                 os.environ.update(self.env)))
        os.environ.pop("SDL_NO_SIGNAL_HANDLERS", None)

    def _construct(self, options):
        from jellyfin_mpv_shim import player

        me = mock.Mock()
        me._lua_works = None
        me._gamepad_works = None
        with mock.patch.object(player, "mpv") as fake_mpv:
            fake_mpv.MPV.return_value = "player"
            player.PlayerManager._construct_mpv(me, options)

    def test_it_is_set_before_mpv_is_constructed(self):
        import os

        self._construct({"input_gamepad": True, "vo": "gpu"})
        self.assertEqual(os.environ.get("SDL_NO_SIGNAL_HANDLERS"), "1")

    def test_it_is_set_even_when_the_shim_passes_no_gamepad_option(self):
        """Unconditional, and it has to be.

        `input-gamepad` is an ordinary mpv option with no M_OPT_NOCFG, so a
        line in the user's own mpv.conf starts the SDL thread with the
        option absent from anything the shim built -- and that config file
        is exactly what `mpv_ext_no_ovr` users are told to use. There is no
        third place to ask, and no way to ask mpv in time: the gamepad
        thread starts inside mpv_initialize.

        This is the test the first version of this failed. It gated on the
        option and read as obviously correct.
        """
        import os

        self._construct({"vo": "gpu"})
        self.assertEqual(os.environ.get("SDL_NO_SIGNAL_HANDLERS"), "1")

    def test_a_blank_value_counts_as_unset(self):
        """SDL's own reading: `SDL_GetHintBoolean` returns the DEFAULT for
        an empty string, so `""` means "install handlers" exactly as unset
        does. Preserving one -- which an `is None` guard does -- leaves the
        unkillable-app bug in place for anybody whose launcher or systemd
        unit exports the variable empty.
        """
        import os

        os.environ["SDL_NO_SIGNAL_HANDLERS"] = ""
        self._construct({"input_gamepad": True})
        self.assertEqual(os.environ.get("SDL_NO_SIGNAL_HANDLERS"), "1")

    def test_an_explicit_value_from_the_user_is_not_overwritten(self):
        # Somebody who wrote "0" wants SDL's handlers, and quietly
        # reversing them is worse than the bug.
        import os

        os.environ["SDL_NO_SIGNAL_HANDLERS"] = "0"
        self._construct({"input_gamepad": True})
        self.assertEqual(os.environ.get("SDL_NO_SIGNAL_HANDLERS"), "0")

    def test_honouring_it_says_so_in_the_log(self):
        """...and it is the one value where the shim knows the user is
        about to lose SIGTERM. Obeying it silently turns a choice into a
        mystery for whoever reads the bug report."""
        import os

        os.environ["SDL_NO_SIGNAL_HANDLERS"] = "0"
        with self.assertLogs("player", level="WARNING") as caught:
            self._construct({"input_gamepad": True})
        self.assertTrue(
            any("SIGTERM" in line for line in caught.output),
            "the warning does not say what the user loses: %r"
            % (caught.output,))


class ControllerReachesThePlayerTest(unittest.TestCase):
    """The two buttons that are not synthetic keypresses, end to end from
    the renderer's event to the player.

    Everything else on the pad is a keypress the renderer issues itself, so
    these two are the whole of the wiring -- and a handler left unwired is
    silent: the stick simply does nothing.
    """

    def _browser(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import FakeSource

        app = mock.Mock()
        controller = mock.Mock()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=controller)
        b.set_app(app)
        return app, controller

    def test_the_right_stick_seeks_the_way_the_arrow_keys_do(self):
        app, controller = self._browser()
        app.on_gamepad_seek("left")
        controller.kb_seek.assert_called_once_with("left")
        # NOT seek_relative: the distance is the user's own input.conf
        # number, which only kb_seek reads.
        controller.seek_relative.assert_not_called()

    def test_start_goes_through_the_remote_controls_ladder(self):
        app, controller = self._browser()
        app.on_gamepad_nav("menu")
        controller.remote_action.assert_called_once_with("menu")

    def test_the_handler_names_are_the_ones_a_real_app_offers(self):
        """`set_app` guards each wire with `hasattr`, so a rename on the
        MpvtkApp side does not fail -- it silently skips the wiring and the
        stick goes dead.

        `_browser` above builds the app as a Mock, where every hasattr is
        true, so it cannot see this. Asserted against the real class.
        """
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        for name in ("on_gamepad_seek", "on_gamepad_nav"):
            with self.subTest(handler=name):
                self.assertIn(name, MpvtkApp.__init__.__code__.co_names,
                              "MpvtkApp.__init__ no longer sets %r, so "
                              "set_app's hasattr guard will skip it" % name)

    def test_the_gateway_hands_both_to_the_player(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        for method, target, arg in (("kb_seek", "kb_seek", "up"),
                                    ("remote_action", "menu_action", "menu")):
            with self.subTest(method=method):
                gateway = PlayerGateway.__new__(PlayerGateway)
                pm = mock.Mock()
                gateway._act = lambda fn, pm=pm: fn(pm)
                getattr(gateway, method)(arg)
                getattr(pm, target).assert_called_once_with(arg)


# At the END of the file, deliberately. It used to sit mid-file with later
# test classes appended below it, so `python3 tests/test_gamepad.py` ran 13
# of 29 and reported OK. `discover` was unaffected, which is what made it
# invisible. See the "uncollected" shape in the notes on tests that cannot
# fail.
if __name__ == "__main__":
    unittest.main()
