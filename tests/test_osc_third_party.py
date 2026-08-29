"""Third-party OSCs, and the two levers that reach them.

`osc_style: default` is the supported way to run someone else's OSC (uosc,
ModernZ, ModernX) against the shim, and `osc_style: none` is the settings
form's "No player controls". Neither worked, because both went through the
`osc` property -- which loads and unloads mpv's OWN osc.lua and says nothing
to a script in `<config>/scripts/`:

- "No player controls" left a third-party OSC drawing, and so did opening the
  library browser over a playing video.
- Worse, under `default` the shim WROTE `osc = True` onto the live player at
  construction and on every browse-leave. libmpv defaults the option off, so
  the write was needed -- but a property write cannot be declined, so it
  loaded mpv's built-in OSC underneath the user's, from the first frame.

The fix is two-sided: the enable/disable *message* every osc.lua fork
inherits goes to everyone, and the `osc` option moves to construction, where
the user's own mpv.conf outranks it. docs/mpv-backends.md section 12.
"""

import sys
import threading
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.player import PlayerManager        # noqa: E402

#: Sentinel for "nobody has written this". `None` would not do -- the bug
#: being pinned is a write, and a write of None reads as untouched.
UNTOUCHED = object()


class FakeMpv:
    """Records commands. `osc` exists as an attribute because `enable_osc`
    gates on `hasattr` -- an mpv too old for the option is a separate case
    (see OldMpvTest)."""

    def __init__(self):
        self.commands = []
        self.osc = UNTOUCHED

    def command(self, *args):
        self.commands.append(args)


class OscCase(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.multiple(
            "jellyfin_mpv_shim.player.settings",
            mpv_ext=False, mpv_ext_no_ovr=False)
        patch.start()
        self.addCleanup(patch.stop)

    def pm(self, style, player=None):
        p = PlayerManager.__new__(PlayerManager)
        p._lock = threading.RLock()
        p._mpv_alive = True
        p._osc_style_resolved = style
        p._osc_suppressed = False
        p._player = FakeMpv() if player is None else player
        return p

    @staticmethod
    def messages(p, verb):
        """The arguments every `script-message <verb>` carried, in order."""
        return [c[2] for c in p._player.commands
                if c[0] == "script-message" and c[1] == verb]


class ItReachesEveryOscTest(OscCase):
    STYLES = ("mpvtk", "mpv", "default", "custom", "none")

    def test_hiding_is_announced_under_every_style(self):
        # Broadcast, so one send covers whichever OSC is actually loaded.
        # Gated behind `_osc_script_loaded` before, it only ever reached
        # our own trickplay-osc.lua.
        for style in self.STYLES:
            with self.subTest(style):
                p = self.pm(style)
                p.enable_osc(False)
                self.assertEqual(self.messages(p, "osc-visibility"),
                                 ["never"])

    def test_the_idle_logo_is_put_away_under_every_style(self):
        # The browse window is force_window with nothing loaded, so an OSC
        # that draws the mpv logo while idle draws it over the library.
        for style in self.STYLES:
            with self.subTest(style):
                p = self.pm(style)
                p.enable_osc(False)
                self.assertEqual(self.messages(p, "osc-idlescreen"), ["no"])

    def test_the_idle_logo_goes_even_when_the_controls_stay(self):
        """`_init_mpv` calls enable_osc(True) under `default`, and that is
        the only send that happens before the OSC's first draw. Waiting for
        the library to open is too late -- ModernZ parks the logo on an
        overlay its hide path never wipes, so it sits over the library for
        the rest of the session."""
        for style in self.STYLES:
            with self.subTest(style):
                p = self.pm(style)
                p.enable_osc(True)
                self.assertEqual(self.messages(p, "osc-idlescreen"), ["no"])

    def test_no_player_controls_silences_someone_elses_osc(self):
        """The settings form's "No player controls". `osc = False` answers
        for mpv's own and nothing else, which is why this needs the
        message."""
        p = self.pm("none")
        p.enable_osc(False)
        self.assertIn("never", self.messages(p, "osc-visibility"))
        self.assertIs(p._player.osc, False)


class TheOscPropertyTest(OscCase):
    def test_default_never_writes_it(self):
        """The user's, and set at construction where mpv.conf outranks us.
        A live write cannot be declined -- it stacked mpv's built-in OSC
        under theirs."""
        for enabled in (True, False, True):
            with self.subTest(enabled=enabled):
                p = self.pm("default")
                p.enable_osc(enabled)
                self.assertIs(p._player.osc, UNTOUCHED)

    def test_the_styles_that_bring_their_own_hold_mpvs_off(self):
        # Here the un-decline-ability is the point: this is how `osc=yes`
        # in an mpv.conf is overridden.
        for style in ("mpv", "mpvtk", "none", "custom"):
            for enabled in (True, False):
                with self.subTest(style=style, enabled=enabled):
                    p = self.pm(style)
                    p.enable_osc(enabled)
                    self.assertIs(p._player.osc, False)


class TheRestoreIsLatchedTest(OscCase):
    """Un-hide only what we hid.

    `enable_osc(True)` runs at construction, so restoring unconditionally
    would broadcast "auto" at every startup and overwrite a user who put
    `visibility=never` in their own OSC's config."""

    def test_a_restore_with_nothing_suppressed_says_nothing(self):
        # About visibility only -- the idle logo is suppressed on every
        # call, deliberately, and is not part of the latch.
        p = self.pm("default")
        p.enable_osc(True)
        self.assertEqual(self.messages(p, "osc-visibility"), [])

    def test_it_restores_what_it_suppressed(self):
        p = self.pm("default")
        p.enable_osc(False)
        p.enable_osc(True)
        self.assertEqual(self.messages(p, "osc-visibility"),
                         ["never", "auto"])

    def test_the_idle_logo_is_never_handed_back(self):
        """Asymmetric on purpose: the controls have to return, the mpv logo
        has nowhere to belong in a shim session, and sending "yes" would
        override someone who set `idlescreen=no` themselves."""
        p = self.pm("default")
        for _ in range(3):
            p.enable_osc(False)
            p.enable_osc(True)
        self.assertEqual(set(self.messages(p, "osc-idlescreen")), {"no"})
        self.assertEqual(self.messages(p, "osc-visibility"),
                         ["never", "auto"] * 3)

    def test_the_latch_does_not_walk_over_repeated_browsing(self):
        # Enter/leave is a loop: the tray, the queue and `q` all re-run it.
        # A one-step test cannot see a latch that drifts.
        p = self.pm("default")
        for _ in range(4):
            p.enable_osc(False)      # browse enter
            p.enable_osc(False)      # and again -- menu.py overlaps it
            p.enable_osc(True)       # browse leave
        seen = self.messages(p, "osc-visibility")
        # Suppressing twice re-sends "never" -- idempotent, and cheaper than
        # the state a dedup would need. What must not drift is the RESTORE:
        # one per suppressed period, never one per call, or a doubled hide
        # would leave a stray "auto" to overwrite the user's own config.
        self.assertEqual(seen.count("auto"), 4)
        self.assertEqual(seen[0], "never", "restored before anything was hid")
        for i, mode in enumerate(seen):
            self.assertGreaterEqual(
                seen[:i + 1].count("never"), seen[:i + 1].count("auto"),
                "restore ran ahead of the suppression it belongs to")
        self.assertFalse(p._osc_suppressed)

    def test_it_resets_when_mpv_is_re_created(self):
        """A new mpv brings a new OSC at its own default, so a suppression
        must not survive the re-create and restore over the user's config.
        Asserted on the source because reaching the reset means constructing
        a real player, which opens a window."""
        import inspect

        src = inspect.getsource(PlayerManager._init_mpv)
        self.assertIn("self._osc_suppressed = False", src)


class OldMpvTest(OscCase):
    def test_an_mpv_without_the_osc_option_still_gets_the_message(self):
        """The property is version-gated; the message is not. Falling over
        here would take the whole browse handoff with it."""
        class NoOsc:
            def __init__(self):
                self.commands = []

            def command(self, *args):
                self.commands.append(args)

        p = self.pm("none", player=NoOsc())
        p.enable_osc(False)
        self.assertEqual(self.messages(p, "osc-visibility"), ["never"])


class UserConfigIsLeftAloneTest(OscCase):
    def test_external_mpv_with_no_ovr_is_not_touched_at_all(self):
        with mock.patch.multiple("jellyfin_mpv_shim.player.settings",
                                 mpv_ext=True, mpv_ext_no_ovr=True):
            p = self.pm("none")
            p.enable_osc(False)
            self.assertEqual(p._player.commands, [])
            self.assertIs(p._player.osc, UNTOUCHED)


class PrecedenceAgainstRealLibmpvTest(unittest.TestCase):
    """The measured fact `build_mpv_options` leans on, pinned.

    Setting `osc` at construction rather than on the live player is only a
    fix because the user's own `mpv.conf` OUTRANKS it -- the opposite of the
    precedence a command-line argument would get. That is a property of how
    the binding applies options, not something mpv documents, so if it ever
    inverts the shim goes back to loading mpv's built-in OSC underneath
    someone's uosc with nothing else noticing. docs/mpv-backends.md
    section 12.
    """

    def setUp(self):
        import tempfile
        try:
            import mpv                                   # noqa: F401
        except OSError:                                  # pragma: no cover
            self.skipTest("libmpv not loadable")
        self.dir = tempfile.mkdtemp()

    def build(self, conf_text, **opts):
        import mpv
        import os
        with open(os.path.join(self.dir, "mpv.conf"), "w") as fh:
            fh.write(conf_text)
        p = mpv.MPV(config=True, config_dir=self.dir, vo="null", **opts)
        try:
            return p.osc
        finally:
            p.terminate()

    def test_libmpv_leaves_the_osc_off_by_default(self):
        """Which is why `default` has to ask for it at all -- the option's
        own default is yes, and libmpv overrides that."""
        self.assertIs(self.build(""), False)

    def test_a_construction_option_turns_it_on(self):
        self.assertIs(self.build("", osc=True), True)

    def test_the_users_mpv_conf_overrides_that_option(self):
        # The whole feature: `osc: True` is a default someone running their
        # own OSC can decline.
        self.assertIs(self.build("osc=no\n", osc=True), False)


if __name__ == "__main__":
    unittest.main()
