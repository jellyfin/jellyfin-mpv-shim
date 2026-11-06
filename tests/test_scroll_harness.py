"""The scroll benchmark's settings (tools/jf_disposable/scroll_config.py, scroll_manual.sh).

Nothing here starts a server, an app or a window: it pins what a run would be asked to do, so
a typo cannot silently produce a mislabelled result on the real desktop.
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools", "jf_disposable")
sys.path.insert(0, TOOLS)
import scroll_config as C  # noqa: E402

MANUAL = os.path.join(TOOLS, "scroll_manual.sh")
PROBE = os.path.join(TOOLS, "scroll_probe.py")


def _env(**kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("JH_", "JMS_TICK"))}
    env.update(kw)
    return env


class Config(unittest.TestCase):
    def test_unset_means_the_apps_own_default_and_no_override(self):
        cfg = C.from_env({})
        self.assertEqual(cfg, {"tick_ms": None, "wheel_delay_s": 0.03,
                               "label": None})
        self.assertEqual(C.app_env(cfg), {})

    def test_a_valid_tick_is_forwarded_as_jms_tick_ms(self):
        for text, want in (("30", "30"), ("16.7", "16.7"), ("5", "5"),
                           ("100", "100")):
            cfg = C.from_env({"JH_TICK_MS": text})
            self.assertEqual(C.app_env(cfg), {"JMS_TICK_MS": want}, text)

    def test_invalid_ticks_are_refused_not_ignored(self):
        for text in ("0", "-5", "4.9", "100.1", "nan", "inf", "1e1", "0x14",
                     " ", "abc", "20ms", ".5", "1.2.3"):
            with self.assertRaises(C.ConfigError, msg=text):
                C.from_env({"JH_TICK_MS": text})

    def test_the_wheel_interval_is_bounded(self):
        self.assertEqual(C.from_env({"JH_SCROLL_DELAY": "0.008"})
                         ["wheel_delay_s"], 0.008)
        for text in ("0", "-0.01", "0.0005", "0.51", "nan", "fast"):
            with self.assertRaises(C.ConfigError, msg=text):
                C.from_env({"JH_SCROLL_DELAY": text})

    def test_an_inherited_jms_tick_ms_cannot_pose_as_the_default(self):
        with self.assertRaises(C.ConfigError):
            C.from_env({"JMS_TICK_MS": "20"})
        C.from_env({"JMS_TICK_MS": "20", "JH_TICK_MS": "20"})

    def test_labels_cannot_escape_the_runs_directory(self):
        for text in ("../x", "a/b", ".hidden", "x" * 49, "a b"):
            with self.assertRaises(C.ConfigError, msg=text):
                C.parse_label(text)
        self.assertEqual(C.parse_label("t16.7_w8"), "t16.7_w8")

    def test_the_matrix_is_the_two_by_two(self):
        self.assertEqual(
            [(t, w) for _, t, w in C.MATRIX],
            [(30.0, 30.0), (16.7, 30.0), (30.0, 8.0), (16.7, 8.0)])
        self.assertEqual(len({n for n, _, _ in C.MATRIX}), 4)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Script(unittest.TestCase):
    def _manual(self, *args, **env):
        return subprocess.run(["bash", MANUAL, "--config-only", *args],
                              capture_output=True, text=True, timeout=60,
                              env=_env(**env))

    def test_the_matrix_checks_four_configurations_and_starts_nothing(self):
        p = self._manual("--matrix")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = [json.loads(ln.split("config ok: ", 1)[1])
                for ln in p.stdout.splitlines() if ln.startswith("config ok")]
        self.assertEqual([(r["tick_ms"], round(r["wheel_delay_s"] * 1000))
                          for r in rows],
                         [(30.0, 30), (16.7, 30), (30.0, 8), (16.7, 8)])
        self.assertIn("nothing started", p.stdout)

    def test_no_arguments_is_the_original_single_default_run(self):
        p = self._manual()
        self.assertEqual(p.returncode, 0, p.stderr)
        row = json.loads(p.stdout.splitlines()[0].split("config ok: ")[1])
        self.assertEqual((row["tick_ms"], row["label"], row["notches"]),
                         (None, None, 90))

    def test_a_tick_and_wheel_interval_get_a_label(self):
        p = self._manual("--tick", "16.7", "--wheel-ms", "8")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"label": "t16.7_w8"', p.stdout)

    def test_bad_values_stop_before_anything_starts(self):
        for args in (("--tick", "3"), ("--tick", "x"), ("--wheel-ms", "0.1"),
                     ("--wheel-ms", "x"), ("--tick",)):
            p = self._manual(*args)
            self.assertNotEqual(p.returncode, 0, args)
            self.assertNotIn("nothing started", p.stdout, args)

    def test_the_matrix_refuses_hand_picked_values(self):
        p = self._manual("--matrix", "--tick", "20")
        self.assertEqual(p.returncode, 64)

    def test_the_probe_check_prints_and_exits_without_importing_the_server(self):
        p = subprocess.run([sys.executable, PROBE, "--check", "12"],
                           capture_output=True, text=True, timeout=60,
                           env=_env(JH_TICK_MS="20", JH_RUN_LABEL="x"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["notches"], 12)

    def test_the_limits_and_isolation_stay_in_the_script(self):
        with open(MANUAL, encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("JH_MAX_MPV=1", "tools/limited_run.sh", "-t 420",
                       ":18097", "break"):
            self.assertIn(needle, text)
        # Only through limited_run.sh (3 GiB scope, leftover check): no bare launch.
        self.assertEqual(text.count("scroll_probe.py\" \"$notches\""), 1)


if __name__ == "__main__":
    unittest.main()
