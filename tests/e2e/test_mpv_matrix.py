"""The shim against mpv builds this machine has, not the one it links to.

`~/Desktop/mpv-matrix` holds mpvs built from `~/Desktop/mpv-build` with
their options varied; `build-one.sh <name> <ref> [meson args]` adds one.
The variant that matters is **`v0.41-nolua`** (`-Dlua=disabled`), because
everything the shim draws is lua -- the browser, the playback HUD, the
stock OSC, `mouse.lua` -- and until this existed the no-lua path had never
been executed at all. It is the one build that cannot be simulated: mpv
does not merely lack lua there, it lacks `--osc`, and refuses to start when
told to set it.

Not in the discovered suite, and not only because it needs a built mpv.
Discovery imports the modules that import `player`, which puts a live
libmpv in the test process, and spawning an mpv binary out of that process
segfaults at teardown often enough to be flaky.

Run it (from the repo root, or the pip-installed copy answers instead):

    python3 -m unittest tests.e2e.test_mpv_matrix

Each case runs the shim in a **subprocess** with a config of its own. That
is what makes both backends reachable in one run: `player` picks its
backend at import and builds its singleton there, so a second one in the
same interpreter is not a second player.
"""

import json
import os
import subprocess
import sys
import textwrap
import unittest

MATRIX = os.path.expanduser("~/Desktop/mpv-matrix")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def variant(name):
    """``(binary, libdir)`` for a matrix variant, or None if not built."""
    root = os.path.join(MATRIX, name)
    binary = os.path.join(root, "bin", "mpv")
    libdir = os.path.join(root, "lib", "x86_64-linux-gnu")
    return (binary, libdir) if os.path.exists(binary) else None


#: What the child prints back. A dict rather than a line per fact, so a
#: child that dies halfway cannot look like a passing subset.
PROBE = textwrap.dedent("""
    import json, sys
    sys.argv = [sys.argv[0], "--config", sys.argv[1]]
    from jellyfin_mpv_shim import conffile
    from jellyfin_mpv_shim.constants import APP_NAME
    from jellyfin_mpv_shim.conf import settings
    settings.load(conffile.get(APP_NAME, "conf.json"))
    from jellyfin_mpv_shim.player import playerManager, is_using_ext_mpv
    out = {"ext": bool(is_using_ext_mpv),
           "lua": bool(playerManager.lua_works()),
           "version": str(playerManager._player.mpv_version)}
    playerManager.terminate()
    print("PROBE=" + json.dumps(out))
""")


class MpvMatrixTest(unittest.TestCase):
    def _probe(self, tmp, *, binary=None, libdir=None):
        """Start the shim against one mpv and report what it decided."""
        cfg = os.path.join(tmp, "cfg")
        os.makedirs(cfg, exist_ok=True)
        conf = {"enable_gui": True, "osc_style": "mpvtk"}
        if binary:
            conf.update({"mpv_ext": True, "mpv_ext_path": binary})
        with open(os.path.join(cfg, "conf.json"), "w", encoding="utf-8") as fh:
            json.dump(conf, fh)

        env = dict(os.environ)
        if libdir:
            env["LD_LIBRARY_PATH"] = (
                libdir + os.pathsep + env.get("LD_LIBRARY_PATH", ""))
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        done = subprocess.run(
            ["xvfb-run", "-a", sys.executable, "-c", PROBE, cfg],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        line = [l for l in done.stdout.splitlines() if l.startswith("PROBE=")]
        self.assertTrue(line, "the shim never came up:\n%s\n%s"
                        % (done.stdout[-2000:], done.stderr[-4000:]))
        return json.loads(line[0][len("PROBE="):]), done

    # -- the build everything else is checked against --------------------

    def test_the_stock_mpv_still_runs_lua(self):
        """A control. Without it, "lua is off" is not a finding."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            got, _done = self._probe(tmp)
        self.assertFalse(got["ext"])
        self.assertTrue(got["lua"], "the machine's own libmpv lost lua")

    def test_every_libmpv_on_this_machine_comes_up(self):
        """The version spread, from whatever libmpv this box already has.

        Debian 13 ships 0.40.0 next to whatever `mpv-build` installed into
        /usr/local, so a second real version costs nothing -- and 0.40 is
        the interesting one, because it is below the 0.41 line
        `runtime_force_window_works` draws, so it also walks the "this mpv
        cannot give up its window" path.

        Older versions than that are **not** buildable from the matrix
        against mpv-build's ffmpeg: `FF_PROFILE_*` became `AV_PROFILE_*` in
        FFmpeg 7, so mpv 0.40 and below fail to compile against it and
        would each need an ffmpeg of their own era built first.
        """
        import glob
        import tempfile

        seen = {}
        for libdir in ("/usr/lib/x86_64-linux-gnu", "/usr/local/lib",
                       "/usr/local/lib/x86_64-linux-gnu"):
            if not glob.glob(os.path.join(libdir, "libmpv.so.2*")):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                got, _done = self._probe(tmp, libdir=libdir)
            seen[got["version"]] = libdir
            self.assertTrue(got["lua"], "%s lost lua" % libdir)
        self.assertTrue(seen, "no libmpv found anywhere")
        # Not an assertion about *which* versions -- that is the machine's
        # business -- only that they were told apart. One entry means every
        # path resolved to the same library and this test proved nothing.
        if len(seen) < 2:
            self.skipTest("only one libmpv version here: %r" % list(seen))

    # -- -Dlua=disabled, both backends -----------------------------------

    def _nolua(self, ext):
        import tempfile

        built = variant("v0.41-nolua")
        if built is None:
            self.skipTest("build it: ~/Desktop/mpv-matrix/build-one.sh "
                          "v0.41-nolua v0.41.0 -Dlua=disabled")
        binary, libdir = built
        with tempfile.TemporaryDirectory() as tmp:
            return self._probe(tmp, binary=binary if ext else None,
                               libdir=None if ext else libdir)

    def test_a_no_lua_libmpv_starts_and_falls_back(self):
        # It has to *start*: --osc does not exist on this build, and mpv
        # rejects the option rather than ignoring it. Before the retry,
        # this died in the constructor -- so the fallback below could
        # never be reached no matter how right it was.
        got, done = self._nolua(ext=False)
        self.assertFalse(got["ext"])
        self.assertFalse(got["lua"])
        self.assertIn("built without lua", done.stdout + done.stderr)

    def test_a_no_lua_external_mpv_starts_and_falls_back(self):
        # The same fact, reported completely differently: no option name,
        # just a process that will not stay up. Worth its own case because
        # the two backends share no code here.
        got, done = self._nolua(ext=True)
        self.assertTrue(got["ext"])
        self.assertFalse(got["lua"])
        self.assertIn("built without lua", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
