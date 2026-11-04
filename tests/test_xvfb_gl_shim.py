"""tools/xvfb_gl_shim must stay a test-environment-only default.

It exists so the real-player integration legs can render (in software) on a
virtual display. These tests pin what it must NOT do: act without the explicit
opt-in, act on a Wayland session, override a caller's own choice, or leave the
application's code touching it.
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
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "tools", "xvfb_gl_shim")

STUB_MPV = (
    "class MPV:\n"
    "    def __init__(self, *a, **kw):\n"
    "        self.kw = kw\n")

PROBE = ("import mpv\n"
         "print(sorted(mpv.MPV(**{k: v for k, v in %s.items()}).kw.items()))\n")


class SitecustomizeShimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.stub.name, "mpv.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(STUB_MPV)

    @classmethod
    def tearDownClass(cls):
        cls.stub.cleanup()

    def _kwargs_seen(self, env_extra, explicit=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JH_XVFB_GL_SHIM", "WAYLAND_DISPLAY",
                            "PYTHONPATH")}
        env.update(env_extra)
        env["PYTHONPATH"] = os.pathsep.join([SHIM, self.stub.name])
        out = subprocess.run(
            [sys.executable, "-c", PROBE % repr(explicit or {})],
            env=env, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        return out.stdout.strip()

    def test_inert_without_the_opt_in(self):
        self.assertEqual(self._kwargs_seen({}), "[]")

    def test_applies_with_the_opt_in_on_a_virtual_display(self):
        self.assertEqual(self._kwargs_seen({"JH_XVFB_GL_SHIM": "1"}),
                         "[('gpu_sw', 'yes')]")

    def test_never_applies_on_a_wayland_session(self):
        self.assertEqual(
            self._kwargs_seen({"JH_XVFB_GL_SHIM": "1",
                               "WAYLAND_DISPLAY": "wayland-0"}), "[]")

    def test_an_explicit_choice_wins(self):
        self.assertEqual(
            self._kwargs_seen({"JH_XVFB_GL_SHIM": "1"},
                              explicit={"gpu_sw": "no"}),
            "[('gpu_sw', 'no')]")


@unittest.skipUnless(os.name == "posix", "shell wrapper")
class WrapperShimTest(unittest.TestCase):
    def _args_seen(self, env_extra):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "realmpv")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\necho "ARGS:$*"\n')
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("JH_XVFB_GL_SHIM", "WAYLAND_DISPLAY")}
            env.update(env_extra)
            env["JH_REAL_MPV"] = fake
            out = subprocess.run([os.path.join(SHIM, "mpv"), "--idle", "x"],
                                 env=env, capture_output=True, text=True,
                                 timeout=30, encoding="utf-8",
                                 errors="replace")
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_pure_pass_through_without_the_opt_in(self):
        self.assertEqual(self._args_seen({}), "ARGS:--idle x")

    def test_adds_gpu_sw_only_with_the_opt_in(self):
        self.assertEqual(self._args_seen({"JH_XVFB_GL_SHIM": "1"}),
                         "ARGS:--gpu-sw=yes --idle x")

    def test_never_adds_it_on_wayland(self):
        self.assertEqual(self._args_seen({"JH_XVFB_GL_SHIM": "1",
                                          "WAYLAND_DISPLAY": "wayland-0"}),
                         "ARGS:--idle x")


class ApplicationNeverReferencesTheShimTest(unittest.TestCase):
    def test_no_application_code_sets_software_rendering_or_names_the_shim(self):
        pkg = os.path.join(ROOT, "jellyfin_mpv_shim")
        hits = []
        for dirpath, _dirs, files in os.walk(pkg):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                for needle in ("gpu_sw", "gpu-sw", "xvfb_gl_shim",
                               "JH_XVFB_GL_SHIM"):
                    if needle in text:
                        hits.append((os.path.relpath(path, ROOT), needle))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
