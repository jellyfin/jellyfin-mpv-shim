"""Which classic OSC gets loaded, and why it is a runtime choice.

`trickplay-osc.lua` is a fork of mpv 0.38's `osc.lua` whose entire purpose is
seek-bar thumbnails -- its own header says "sadly there is no way to do this
without forking the entire OSC script", and its whole patch set is six
thumbfast blocks plus one compatibility fix.

mpv 0.41 made that unnecessary. The **OSC Preview API**
(`DOCS/man/osc.rst`) has the OSC publish `user-data/osc/draw-preview` --
{x, y, w, h, hover-sec, ass} -- for any thumbnailer script to draw into, and
nil to clear. Measured against a real mpv: hovering the seekbar sets it to
`{x:147, y:350, w:120, h:90, hover-sec:12.15, ass:'...'}`.

So a modern mpv gets mpv's OWN OSC -- upstream's fixes, upstream's look, no
fork to maintain -- and `thumbfast.lua` answers the property. The fork stays
for older builds, because Debian and others still ship 0.40.

**Why the choice cannot be made at option-build time**, which is where every
other script is chosen: it depends on the version of the mpv about to be
constructed, and nothing knows that yet. The libmpv client API cannot stand
in either -- 0.40 and 0.41 both report 2.5 -- so this is a `load-script`
immediately after construction, while nothing is on screen.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.mpv_options import mpv_scripts  # noqa: E402


def _preview_api(version):
    """`player.osc_preview_api_works` without importing player.

    Importing `jellyfin_mpv_shim.player` builds its module-level singleton
    and opens a real mpv window (see CLAUDE.md), which is not something a
    unit test about a version string should cost. The predicate is pure, so
    it is read out of the source and executed on its own -- the same trick
    `test_mpv_options` uses to keep option behaviour testable without a
    player.
    """
    import ast
    import pathlib
    import re

    src = pathlib.Path(
        __file__).resolve().parent.parent.joinpath(
            "jellyfin_mpv_shim", "player.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "osc_preview_api_works")
    ns = {"re": re}
    exec(compile(ast.Module([fn], []), "<player>", "exec"), ns)
    return ns["osc_preview_api_works"](version)


class PreviewApiVersionTest(unittest.TestCase):
    def test_041_and_newer_have_it(self):
        for version in ("mpv 0.41.0", "mpv v0.41.0",
                        "mpv v0.41.0-1011-g182fa6ca4", "mpv 0.42.0",
                        "mpv 1.0.0"):
            with self.subTest(version=version):
                self.assertTrue(_preview_api(version))

    def test_040_and_older_do_not(self):
        """The versions that keep the fork. Debian ships 0.40."""
        for version in ("mpv 0.40.0", "mpv v0.40.0-99-gabcdef",
                        "mpv 0.38.0", "mpv 0.29.1"):
            with self.subTest(version=version):
                self.assertFalse(_preview_api(version))

    def test_an_unreadable_version_is_treated_as_old(self):
        """Fail-safe in the direction that works everywhere: the fork runs
        on every mpv we have measured, while an OSC with no previews is a
        feature silently missing under a style named for it."""
        for version in ("", None, "mpv git-2026-08-28", "unknown"):
            with self.subTest(version=repr(version)):
                self.assertFalse(_preview_api(version))

    def test_it_is_its_own_predicate(self):
        """Same 0.41 threshold as `runtime_force_window_works` and
        deliberately not the same function: two different questions that
        happen to share an answer today, and folding them would make a
        backport of either a silent lie about the other."""
        import pathlib

        src = pathlib.Path(
            __file__).resolve().parent.parent.joinpath(
                "jellyfin_mpv_shim", "player.py").read_text(encoding="utf-8")
        self.assertIn("def osc_preview_api_works", src)
        self.assertIn("def runtime_force_window_works", src)


def script_names(style, trickplay):
    """The basenames mpv is told to load, for the running platform.

    `os.path.basename`, not `rsplit("/")`: `mpv_scripts` returns native
    paths, so on Windows every one of these came back as the whole
    `C:\\...\\thumbfast.lua` and the comparisons below stopped meaning
    anything. The `assertIn` failed and said so; the two `assertNotIn`
    passed, because a full path is never equal to a bare filename either
    -- so the checks that guard "two OSCs at once" were vacuously green on
    Windows rather than broken-looking.
    """
    return [os.path.basename(p) for p in mpv_scripts(style, trickplay)]

class ConstructionScriptsTest(unittest.TestCase):
    def test_no_osc_is_loaded_at_construction(self):
        """Two OSCs at once is the failure this prevents: a construction
        entry would load the fork, and `_load_classic_osc` would then load
        whichever it chose on top of it."""
        for style in ("mpv", "mpvtk", "default", "none"):
            with self.subTest(style=style):
                names = script_names(style, True)
                self.assertNotIn("trickplay-osc.lua", names)

    def test_thumbfast_is_still_loaded_for_every_style(self):
        """It is the thumbnailer for BOTH paths now -- the fork's
        client-messages and the stock OSC's property -- so it must not have
        become conditional on the OSC choice."""
        for style in ("mpv", "mpvtk", "default"):
            with self.subTest(style=style):
                names = script_names(style, True)
                self.assertIn("thumbfast.lua", names)

    def test_and_not_when_trickplay_never_started(self):
        names = script_names("mpv", False)
        self.assertNotIn("thumbfast.lua", names)


class IdleScreenTest(unittest.TestCase):
    """mpv's "Drop files or URLs to play" logo must never be drawn.

    The shim always draws its own idle window, so the OSC's logo is never
    wanted -- and once an OSC has drawn it, asking it to stop can leave the
    logo parked on a second overlay for the session (the measurement behind
    `enable_osc`'s comment).

    `enable_osc` broadcasts `osc-idlescreen no` and notes it "has to land
    BEFORE the OSC's first draw". That held while every OSC was loaded at
    construction. It stopped holding when the classic OSC became a runtime
    `load-script`, because that is asynchronous: the message races the
    script's `register_script_message`, and losing the race puts the logo
    behind the library. Found by hand-testing, not by this suite.

    Setting it as a SCRIPT-OPT instead is read by construction rather than
    in time -- every osc.lua and every fork reads `script-opts` under the
    `osc` prefix at startup. Measured against a real mpv: without it the OSC
    starts with `idlescreen = true`, with it `false`.
    """

    def _src(self):
        import pathlib

        return pathlib.Path(
            __file__).resolve().parent.parent.joinpath(
                "jellyfin_mpv_shim", "player.py").read_text(encoding="utf-8")

    def _init_mpv_body(self):
        src = self._src()
        body = src[src.index("    def _init_mpv(self):"):]
        return body[:body.index("\n    def ")]

    def test_the_option_is_set_before_any_osc_is_chosen(self):
        body = self._init_mpv_body()
        opt = body.index('"osc-idlescreen=no"')
        load = body.index("_load_classic_osc()")
        self.assertLess(opt, load,
                        "the idlescreen option must be set before an OSC is "
                        "loaded, or it races the script's own startup")

    def test_it_is_set_for_every_style_not_just_the_one_we_load(self):
        """The styles where MPV loads the OSC itself need it most -- there
        is no `load-script` of ours to order against, so the broadcast has
        nothing to beat the race with. Asserted structurally: the call sits
        outside the `if osc_style ==` guard."""
        body = self._init_mpv_body()
        opt = body.index('"osc-idlescreen=no"')
        guard = body.index('if osc_style == "mpv":')
        self.assertLess(opt, guard,
                        "the option is inside the style guard, so a style "
                        "that loads its own OSC never gets it")

    def test_it_appends_rather_than_replacing_script_opts(self):
        """`script-opts` is shared with the user's own scripts. A whole
        write would take their options with it -- the same rule as never
        writing over their mpv.conf."""
        self.assertIn('"change-list", "script-opts", "append"',
                      self._init_mpv_body())

    def test_it_is_set_exactly_once(self):
        """One place that knows, so the two OSC paths cannot drift."""
        self.assertEqual(self._src().count('"osc-idlescreen=no"'), 1)


class ThumbfastAnswersBothDoorsTest(unittest.TestCase):
    """Read from the lua source: the two front doors, one handler."""

    def _src(self):
        import pathlib

        return pathlib.Path(
            __file__).resolve().parent.parent.joinpath(
                "jellyfin_mpv_shim", "thumbfast.lua").read_text(
                    encoding="utf-8")

    def test_it_observes_the_preview_property(self):
        self.assertIn("user-data/osc/draw-preview", self._src())

    def test_it_still_answers_the_forks_client_message(self):
        """Both paths stay, and cannot both be live: the property only
        appears when the stock OSC runs, and the messages only come from the
        fork, which is loaded only when the stock one cannot do this."""
        src = self._src()
        self.assertIn('event_name == "thumb"', src)
        self.assertIn('event_name == "clear"', src)

    def test_the_preview_path_reuses_the_message_handler(self):
        """One implementation of "draw the frame for this timestamp here".
        Two copies would drift, and the fork's path is the one nobody runs
        on a modern machine -- so it is the copy that would rot unseen."""
        self.assertIn("client_message_handler({args = {\"thumb\"",
                      self._src())


if __name__ == "__main__":
    unittest.main()
