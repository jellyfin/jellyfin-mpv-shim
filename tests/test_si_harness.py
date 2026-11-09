"""The scroll-input session (tools/jf_disposable/si_*.py, si_launch.sh, si_manual.sh).

Nothing here starts a server, an app or a window: the analysis runs on text, the launcher on a fake app, and
the rest is pinned by reading the sources."""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools", "jf_disposable")
sys.path.insert(0, TOOLS)

import si_analyze as A                                       # noqa: E402


def _text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def E(t, interval, scale, cls, before, after, want=None, clamp="-", g0="0", g1="1", d="1", c="c1", snap="-"):
    want = after if want is None else want
    return "E\t%.4f\t%s\t%s\ty\t%s\t%s\t%s\t1200\t%.2f\t%.2f\t%.2f\t%s\t%s\t%s\t%s" % (
        t, interval, d, scale, cls, c, before, after, want, clamp, g0, g1, snap)


def F(t, gap, stored, drawn, dd, glide="1"):
    return "F\t%.4f\t%s\tc1\t%.2f\t%d\t%d\t%s\t%s\t-" % (t, gap, stored, drawn, dd, glide, "-")


SAMPLE = "\n".join([
    "# header", "E\tjunk",
    E(0.5, "-", "-", "W", 0, 5),                          # before any phase: ignored
    "P\twheel\tstart\tz",
    E(1.0, "-", "1.0000", "W", 0, 80),
    E(1.2, "200.00", "1.0000", "W", 80, 160),
    F(1.03, "30.00", 80, 25, 25), F(1.06, "30.00", 80, 43, 18), F(1.09, "30.00", 80, 55, 12),
    F(1.12, "30.00", 80, 63, 8), F(1.15, "30.00", 80, 70, 7),
    "P\twheel\tend\tz",
    "P\tpad\tstart\tz",
    E(2.00, "-", "0.0500", "T", 160, 161, g0="0", g1="0"),
    E(2.007, "7.00", "0.0500", "T", 161, 162, g0="0", g1="0"),
    E(2.014, "7.00", "1.0000", "W", 162, 242),            # a hard flick frame taken for a notch
    E(2.021, "7.00", "0.9800", "T", 242, 263, clamp="hi", want=270),
    F(2.01, "30.00", 162, 162, 1, "0"), F(2.04, "30.00", 263, 200, 38), F(2.07, "30.00", 263, 230, 30),
    "P\tpad\tend\tz", ""])


class Analyze(unittest.TestCase):
    def test_a_file_is_cut_into_phases_and_lines_outside_them_are_ignored(self):
        phases, bad = A.parse(SAMPLE)
        self.assertEqual(list(phases), ["wheel", "pad"])
        self.assertEqual((len(phases["wheel"]["E"]), len(phases["wheel"]["F"])), (2, 5))
        self.assertEqual((len(phases["pad"]["E"]), len(phases["pad"]["F"])), (4, 3))
        self.assertEqual(bad, 1)                                  # the "E junk" line

    def test_a_file_with_no_phase_markers_is_one_phase(self):
        phases, _ = A.parse("\n".join([E(1.0, "-", "1.0000", "W", 0, 80), F(1.03, "30.00", 80, 25, 25)]))
        self.assertEqual(list(phases), ["all"])
        self.assertEqual(len(phases["all"]["E"]), 1)

    def test_input_statistics_expose_a_touchpad_frame_taken_for_a_notch(self):
        rep = A.analyze(SAMPLE)
        pad = rep["phases"]["pad"]["input"]
        self.assertEqual(pad["events"], 4)
        self.assertEqual(pad["scale_ge_099"], 1)
        self.assertEqual(pad["scale_exactly_1"], 1)
        self.assertEqual(pad["w_within_25ms_of_previous"], 1)    # a "notch" 7 ms after the last event
        self.assertEqual(pad["class"], {"W": 1, "T": 3})
        self.assertEqual(pad["scale_hist"]["<0.1"], 2)
        self.assertEqual(pad["scale_hist"]["0.9-0.99"], 1)
        wheel = rep["phases"]["wheel"]["input"]
        self.assertEqual(wheel["w_within_25ms_of_previous"], 0)  # real notches are far apart
        self.assertEqual(wheel["interval_ms"]["p50"], 200.0)

    def test_offset_statistics_count_clamps_and_glides(self):
        o = A.analyze(SAMPLE)["phases"]["pad"]["offsets"]
        self.assertEqual((o["clamp_lo"], o["clamp_hi"]), (0, 1))
        self.assertEqual(o["glide_started"], 2)                   # the "W" and the "T" with g0=0,g1=1 default
        w = A.analyze(SAMPLE)["phases"]["wheel"]["offsets"]
        self.assertEqual(w["glide_started"], 2)

    def test_frame_statistics_describe_the_drawn_motion(self):
        f = A.analyze(SAMPLE)["phases"]["wheel"]["frames"]
        self.assertEqual(f["frames"], 5)
        self.assertEqual(f["max_abs_px_per_frame"], 25)
        self.assertEqual(f["mean_abs_px_per_frame"], 14.0)       # (25+18+12+8+7)/5
        self.assertEqual(f["lag_px"]["max"], 55)                 # |80 - 25|
        self.assertEqual(f["still_frames"], 0)
        self.assertGreater(f["variation"], 0.4)

    def test_the_report_renders_every_phase_and_never_raises_on_an_empty_phase(self):
        text = A.render(A.analyze(SAMPLE))
        self.assertIn("== wheel", text)
        self.assertIn("== pad", text)
        self.assertIn("exactly 1.0: 1", text)
        A.render(A.analyze("P\tx\tstart\tz\nP\tx\tend\tz\n"))

    def test_the_cli_reads_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.tsv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(SAMPLE)
            r = subprocess.run([sys.executable, os.path.join(TOOLS, "si_analyze.py"), p],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("== pad", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "si_analyze.py")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 64)


class Launcher(unittest.TestCase):
    def _run(self, args):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app.sh")
            with open(app, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\necho "args=$*"\nenv | grep ^JMS_ | sort\n')
            os.chmod(app, 0o755)
            env = {"PATH": os.environ["PATH"], "JH_SI_REAL_APP": app, "JH_SI_LOG": "/x/scroll.tsv",
                   "JMS_TICK_MS": "16.7"}
            return subprocess.run([os.path.join(TOOLS, "si_launch.sh")] + args, capture_output=True, text=True,
                                  env=env, timeout=30).stdout.split("\n")

    def test_a_start_turns_on_the_recorder_and_nothing_else(self):
        out = self._run(["--config", "/c", "--mpv-loglevel", "info"])
        self.assertEqual(out[0], "args=--config /c --mpv-loglevel info")
        self.assertEqual([l for l in out[1:] if l], ["JMS_FRAME_DIAG=1", "JMS_SCROLL_DIAG=/x/scroll.tsv"])

    def test_a_stop_call_sets_no_diagnostics(self):
        out = self._run(["--config", "/c", "stop"])
        self.assertEqual([l for l in out[1:] if l], ["JMS_TICK_MS=16.7"])        # only what was inherited


class Boundaries(unittest.TestCase):
    def test_the_session_sends_no_input_and_has_no_wayland_debug(self):
        for name in ("si_session.py", "si_launch.sh", "si_manual.sh", "si_analyze.py"):
            src = _text(os.path.join(TOOLS, name))
            self.assertNotIn("WAYLAND_DEBUG", src.replace("no WAYLAND_DEBUG", ""), name)
            for forbidden in ("xdotool", "ydotool", "wtype", "set_property", "input-ipc", "keypress", '"wheel"'):
                self.assertNotIn(forbidden, src, "%s: %s" % (name, forbidden))

    def test_the_session_keeps_the_resource_limits_and_the_phases(self):
        src = _text(os.path.join(TOOLS, "si_session.py"))
        self.assertIn("AB.preflight(work, py, need_scope=not a.dry_run)", src)   # 3 GiB scope, JH_MAX_MPV=1
        self.assertIn('"JH_JF_MEDIA"', src)
        sh = _text(os.path.join(TOOLS, "si_manual.sh"))
        self.assertIn("limited_run.sh", sh)
        self.assertIn("JH_MAX_MPV=1", sh)
        names = re.findall(r'^    \("([a-z_]+)", \d+,', src, re.M)
        self.assertEqual(names, ["wheel_slow", "wheel_fast", "wheel_reversal", "wheel_edges", "pad_slow", "pad_fast"])

    def test_markers_are_written_only_after_the_recorder_has_flushed(self):
        src = _text(os.path.join(TOOLS, "si_session.py"))
        self.assertIn("time.sleep(FLUSH_WAIT_S)", src)
        self.assertGreater(float(re.search(r"FLUSH_WAIT_S = ([0-9.]+)", src).group(1)), 1.0)   # the recorder's idle flush is 1 s

    def test_the_scripts_are_executable_and_the_dry_run_names_the_phases(self):
        for name in ("si_manual.sh", "si_launch.sh"):
            self.assertTrue(os.stat(os.path.join(TOOLS, name)).st_mode & stat.S_IXUSR, name)

    def test_the_report_command_needs_a_file(self):
        r = subprocess.run([os.path.join(TOOLS, "si_manual.sh"), "report"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 64)


if __name__ == "__main__":
    unittest.main()
