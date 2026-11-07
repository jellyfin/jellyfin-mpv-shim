"""The blinded cadence A/B harness (tools/jf_disposable/ab_*.py, ab_launch.sh, ab_manual.sh).

Nothing here starts a server, an app or a window, or reads the real cgroup of the machine: the
pieces that touch /proc, /sys and sockets are pointed at fixtures in a temporary directory.
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import random
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools", "jf_disposable")
sys.path.insert(0, TOOLS)
import ab_config as CF  # noqa: E402
import ab_metrics as M  # noqa: E402
import ab_report as R  # noqa: E402
import ab_session as S  # noqa: E402

LAUNCH = os.path.join(TOOLS, "ab_launch.sh")
MANUAL = os.path.join(TOOLS, "ab_manual.sh")


def _json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name


class Plan(Tmp):
    def new(self, mode="pilot", seed=1):
        s = os.path.join(self.tmp, "s")
        CF.new_session(s, mode, random.Random(seed))
        return s

    def test_public_files_name_no_arm_and_no_tick(self):
        s = self.new("full")
        for name in ("plan.json", "ratings.json"):
            text = _text(os.path.join(s, name))
            for word in ("tick", "\"arm", "16.7", "\"A\"", "\"B\""):
                self.assertNotIn(word, text, (name, word))

    def test_the_key_is_private_and_holds_both_ticks(self):
        s = self.new()
        p = os.path.join(s, ".key.json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        arms = _json(p)["arms"]
        self.assertEqual(sorted(arms.values()), [16.7, 30.0])

    def test_orders(self):
        for seed in range(20):
            pilot = CF.arm_order("pilot", random.Random(seed))
            self.assertEqual(sorted(pilot), ["A", "B"])
            self.assertIn("".join(CF.arm_order("full", random.Random(seed))),
                          ("ABBA", "BAAB"))

    def test_both_labellings_and_both_orders_occur(self):
        seen_map, seen_order = set(), set()
        for seed in range(40):
            d = os.path.join(self.tmp, "x%d" % seed)
            CF.new_session(d, "full", random.Random(seed))
            k = _json(os.path.join(d, ".key.json"))
            seen_map.add(k["arms"]["A"])
            seen_order.add("".join(k["order"]))
        self.assertEqual(seen_map, {30.0, 16.7})
        self.assertEqual(seen_order, {"ABBA", "BAAB"})

    def test_a_session_directory_is_never_reused(self):
        s = self.new()
        with self.assertRaises(CF.PlanError):
            CF.new_session(s, "pilot")

    def test_the_tick_for_a_run_follows_the_key_and_rejects_bad_names(self):
        s = self.new("full")
        key = _json(os.path.join(s, ".key.json"))
        for i, arm in enumerate(key["order"], 1):
            self.assertEqual(CF.tick_for_run(s, "run-%02d" % i), key["arms"][arm])
        for bad in ("run-9", "../x", "run-05", "", None):
            with self.assertRaises(CF.PlanError, msg=bad):
                CF.tick_for_run(s, bad)

    def test_phases_cover_every_condition_and_pilot_is_shorter(self):
        pilot, full = CF.phases_for("pilot"), CF.phases_for("full")
        self.assertEqual([p["name"] for p in pilot],
                         ["cold_wheel", "warm_fling", "warm_nudge", "touchpad", "idle"])
        self.assertEqual([p["name"] for p in pilot], [p["name"] for p in full])
        self.assertLess(sum(p["seconds"] for p in pilot), sum(p["seconds"] for p in full))
        with self.assertRaises(CF.PlanError):
            CF.phases_for("long")

    def test_next_run_skips_anything_with_a_record(self):
        s = self.new("full")
        self.assertEqual(CF.next_run(s), "run-01")
        os.makedirs(os.path.join(s, "runs", "run-01"))          # crashed: no run.json
        self.assertEqual(CF.run_state(s, "run-01"), "failed")
        self.assertEqual(CF.next_run(s), "run-02")

    def test_ratings_are_validated_and_not_rewritten(self):
        s = self.new()
        for bad in ((0, 3), (3, 6), (True, 3), ("3", 3)):
            with self.assertRaises(CF.PlanError, msg=bad):
                CF.record_rating(s, "run-01", *bad)
        with self.assertRaises(CF.PlanError):
            CF.record_rating(s, "run-01", 3, 3, guess="25")
        CF.record_rating(s, "run-01", 4, 3, "16.7", "n")
        with self.assertRaises(CF.PlanError):
            CF.record_rating(s, "run-01", 1, 1)

    def test_reveal_waits_for_the_ratings_of_finished_runs(self):
        s = self.new()
        with self.assertRaisesRegex(CF.PlanError, "not run yet"):
            CF.reveal(s)                                         # nothing started
        _write(os.path.join(s, "runs", "run-01", "run.json"), '{"status": "complete"}')
        with self.assertRaisesRegex(CF.PlanError, "run-02"):
            CF.reveal(s)                                         # one still to do
        _write(os.path.join(s, "runs", "run-02", "run.json"), '{"status": "complete"}')
        with self.assertRaisesRegex(CF.PlanError, "no rating recorded for run-01, run-02"):
            CF.reveal(s)
        CF.record_rating(s, "run-01", 4, 4)
        CF.record_rating(s, "run-02", 2, 3, "30")
        table = CF.reveal(s)
        self.assertEqual([e["state"] for e in table], ["complete", "complete"])
        self.assertEqual(sorted(e["tick_ms"] for e in table), [16.7, 30.0])
        self.assertEqual(table[1]["rating"]["guess"], "30")

    def test_a_failed_run_does_not_block_the_reveal(self):
        s = self.new()
        os.makedirs(os.path.join(s, "runs", "run-01"))           # crashed
        _write(os.path.join(s, "runs", "run-02", "run.json"), '{"status": "complete"}')
        CF.record_rating(s, "run-02", 3, 3)
        self.assertEqual([e["state"] for e in CF.reveal(s)], ["failed", "complete"])


class Cgroup(Tmp):
    def test_the_cgroup_path_comes_from_proc(self):
        _write(os.path.join(self.tmp, "proc", "42", "cgroup"), "0::/user.slice/jh-limited-1.scope\n")
        os.makedirs(os.path.join(self.tmp, "cg", "user.slice", "jh-limited-1.scope"))
        d = M.cgroup_dir(42, os.path.join(self.tmp, "cg"), os.path.join(self.tmp, "proc"))
        self.assertTrue(d.endswith("jh-limited-1.scope"))
        self.assertIsNone(M.cgroup_dir(43, os.path.join(self.tmp, "cg"), os.path.join(self.tmp, "proc")))

    def test_cpu_and_memory_files_are_read(self):
        d = os.path.join(self.tmp, "cg")
        _write(os.path.join(d, "cpu.stat"), "usage_usec 5000000\nuser_usec 3000000\n"
                                            "system_usec 2000000\nnr_throttled 2\n")
        _write(os.path.join(d, "memory.current"), "1048576\n")
        _write(os.path.join(d, "memory.peak"), "2097152\n")
        _write(os.path.join(d, "memory.max"), "3221225472\n")
        r = M.read_cgroup(d)
        self.assertEqual((r["cpu_usec"], r["cpu_system_usec"], r["throttled"]), (5000000, 2000000, 2))
        self.assertEqual((r["mem_current"], r["mem_peak"], r["mem_max"]), (1048576, 2097152, 3221225472))

    def test_missing_files_are_none_not_zero(self):
        r = M.read_cgroup(os.path.join(self.tmp, "nothing"))
        self.assertIsNone(r["cpu_usec"])
        self.assertIsNone(r["mem_peak"])

    def test_memory_max_unlimited_is_none(self):
        _write(os.path.join(self.tmp, "memory.max"), "max\n")
        self.assertIsNone(M.read_cgroup(self.tmp)["mem_max"])


class Procs(Tmp):
    def proc(self, pid, ppid, utime, stime, pss, rss):
        base = os.path.join(self.tmp, "proc", str(pid))
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 10
        _write(base + "/stat", "%d (my app) %s\n" % (pid, " ".join(fields)))
        _write(base + "/smaps_rollup", "Rss: %d kB\nPss: %d kB\nPss_Anon: 1 kB\n" % (rss, pss))

    def test_the_tree_and_its_sums(self):
        self.proc(10, 1, 100, 50, 1000, 2000)
        self.proc(11, 10, 20, 5, 300, 500)
        self.proc(12, 11, 1, 1, 10, 20)
        self.proc(99, 1, 9999, 9999, 99999, 99999)        # unrelated
        pr = os.path.join(self.tmp, "proc")
        self.assertEqual(sorted(M.tree_pids(10, pr)), [10, 11, 12])
        self.assertEqual(M.read_proc_tree([10, 11, 12], pr), (1310, 2520, 177))

    def test_a_vanished_process_is_skipped(self):
        self.proc(10, 1, 1, 1, 5, 6)
        self.assertEqual(M.read_proc_tree([10, 77], os.path.join(self.tmp, "proc")), (5, 6, 2))


class Gpu(Tmp):
    def test_busy_percent_is_read_where_present(self):
        _write(os.path.join(self.tmp, "card0", "device", "gpu_busy_percent"), "37\n")
        _write(os.path.join(self.tmp, "card0", "device", "vendor"), "0x1002\n")
        g = M.gpu_sources(self.tmp)
        self.assertEqual(M.read_gpu(g["paths"]), 37)
        self.assertEqual(g["vendors"], ["0x1002"])

    def test_absent_means_none(self):
        g = M.gpu_sources(self.tmp)
        self.assertEqual(g["paths"], [])
        self.assertIsNone(M.read_gpu(g["paths"]))


class Sampling(Tmp):
    def test_snap_and_phase_summary(self):
        cg = os.path.join(self.tmp, "cg")
        clock = iter([0.0, 10.0])
        ticks = iter([(100, 200, 1000), (150, 260, 1400)])    # pss, rss, utime+stime
        usec = iter([1_000_000, 3_000_000])

        def fake_tree(_pid, _proc="/proc"):
            return [1]
        orig = (M.read_proc_tree, M.tree_pids, M.read_cgroup)
        self.addCleanup(lambda: setattr(M, "read_proc_tree", orig[0]) or setattr(M, "tree_pids", orig[1])
                        or setattr(M, "read_cgroup", orig[2]))
        M.tree_pids = fake_tree
        M.read_proc_tree = lambda pids, proc="/proc": next(ticks)
        M.read_cgroup = lambda d: {"cpu_usec": next(usec), "cpu_user_usec": 0, "cpu_system_usec": 0,
                                   "throttled": 0, "mem_current": 50 * 1048576, "mem_peak": 60 * 1048576,
                                   "mem_max": None}
        s = M.Sampler(cg, lambda: 5, [], clock=lambda: next(clock))
        s.snap()
        s.snap()
        out = M.summarize_samples(s.between(0, 10))
        self.assertEqual(out["wall_s"], 10.0)
        self.assertEqual(out["cgroup_cpu_pct"], 20.0)         # 2 s of CPU in 10 s
        self.assertEqual(out["app_cpu_pct"], round(400 / M.CLK_TCK / 10 * 100, 1))
        self.assertEqual(out["pss_max_mib"], round(150 / 1024, 1))
        self.assertIsNone(out["gpu_busy_max_pct"])

    def test_too_few_samples_do_not_invent_numbers(self):
        self.assertEqual(M.summarize_samples([{"t": 1}]), {"samples": 1})


class Frames(Tmp):
    def test_tail_stamps_lines_and_waits_for_whole_ones(self):
        p = os.path.join(self.tmp, "f.tsv")
        now = [1.0]
        t = M.FrameTail(p, clock=lambda: now[0])
        self.assertEqual(t.poll_once(), 0)                      # no file yet
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("0.1\t30.0\t5.0\t1.0\t3\t1000\n0.2\t31.0\t6.0")
        self.assertEqual(t.poll_once(), 1)                      # the second line is partial
        now[0] = 2.0
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("\t1.0\t3\t1000\ngarbage\n0.3\tx\t1\t1\t1\t1\n")
        self.assertEqual(t.poll_once(), 1)                      # garbage and non-numeric are dropped
        self.assertEqual([r[6] for r in t.rows], [1.0, 2.0])
        self.assertEqual(len(t.between(1.5, 3.0)), 1)

    def rows(self, gaps, render=10.0, adds=3, nbytes=1_000_000):
        return [(float(i), g, render, 5.0, adds, nbytes, float(i)) for i, g in enumerate(gaps)]

    def test_summary_numbers(self):
        out = M.summarize_frames(self.rows([20.0] * 98 + [60.0, 400.0]))
        self.assertEqual(out["frames"], 100)
        self.assertEqual(out["idle_gaps"], 1)                    # the 400 ms pause is not a stutter
        self.assertEqual(out["slow_gaps_ge_50ms"], 1)
        self.assertEqual(out["gap_ms"]["p50"], 20.0)
        self.assertEqual(out["adds_per_frame"], 3.0)
        span = (98 * 20.0 + 60.0) / 1000
        self.assertEqual(out["fps"], round(99 / span, 1))
        self.assertEqual(out["overlay_mb_per_s"], round(99 * 1.0 / span, 1))
        self.assertEqual(out["render_busy_pct"], round(99 * 10.0 / 10 / span, 1))

    def test_no_active_frames_is_reported_as_such(self):
        out = M.summarize_frames(self.rows([500.0, 900.0]))
        self.assertEqual(out["active_frames"], 0)
        self.assertNotIn("fps", out)
        self.assertEqual(M.summarize_frames([])["frames"], 0)


class Log(Tmp):
    def test_queued_event_lines_are_counted_between_offsets(self):
        p = os.path.join(self.tmp, "log")
        a = "ordinary line\n"
        b = "[cplayer] Too many events queued\nsomething dropped here\nunrelated\n"
        c = "[x] event queue full\n"
        _write(p, a + b + c)
        whole = M.scan_log(p)
        self.assertEqual((whole["queued_events"], whole["suspect"]), (2, 1))
        mid = M.scan_log(p, len(a), len(a) + len(b))
        self.assertEqual((mid["queued_events"], mid["suspect"]), (1, 1))
        self.assertTrue(M.scan_log(p, len(a + b + c))["readable"])
        self.assertFalse(M.scan_log(os.path.join(self.tmp, "none"))["readable"])

    def test_example_lines_are_capped(self):
        p = os.path.join(self.tmp, "log")
        _write(p, "".join("Too many events queued %d\n" % i for i in range(50)))
        self.assertEqual(len(M.scan_log(p)["examples"]), 8)


class Ipc(Tmp):
    def test_the_socket_lives_in_a_short_private_directory(self):
        src = _text(os.path.join(ROOT, "tools", "jf_disposable", "ab_session.py"))
        self.assertIn('tempfile.mkdtemp(prefix="jhab-")', src)
        self.assertNotIn('os.path.join(run_dir, "mpv.sock")', src)
        self.assertIn("shutil.rmtree(sock_dir", src)
        self.assertLess(len(os.path.join(tempfile.gettempdir(), "jhab-xxxxxxxx", "mpv.sock")), 100)

    def test_properties_are_read_over_a_unix_socket_ignoring_events(self):
        path = os.path.join(self.tmp, "mpv.sock")
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)

        def serve():
            c, _ = srv.accept()
            buf = b""
            while True:
                data = c.recv(4096)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    req = json.loads(ln)
                    prop = req["command"][1]
                    c.sendall(b'{"event": "tick"}\n')
                    if prop == "missing":
                        resp = {"request_id": req["request_id"], "error": "property not found"}
                    else:
                        resp = {"request_id": req["request_id"], "error": "success",
                                "data": {"tick_ms": 16.7} if prop.startswith("user-data") else "gpu"}
                    c.sendall((json.dumps(resp) + "\n").encode())
        threading.Thread(target=serve, daemon=True).start()
        got = S.ipc_props(path, ["user-data/mpvtk/diag", "current-vo", "missing"])
        self.assertEqual(got, {"user-data/mpvtk/diag": {"tick_ms": 16.7}, "current-vo": "gpu",
                               "missing": None})


class Launcher(Tmp):
    def fake_app(self):
        app = os.path.join(self.tmp, "app.sh")
        _write(app, '#!/bin/sh\nenv | grep -E "^JMS_" | sort > "%s/env.$$"\necho "$@" > "%s/args.$$"\n'
               % (self.tmp, self.tmp))
        os.chmod(app, 0o755)
        return app

    def run_launch(self, *args):
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg, exist_ok=True)
        _write(os.path.join(cfg, "mpv.conf"), "")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JMS_", "JH_"))}
        env.update({"JH_AB_REAL_APP": self.fake_app(), "JH_AB_IPC": "/x/mpv.sock",
                    "JH_AB_FRAME_LOG": "/x/f.tsv", "JH_AB_ART_DIAG": "/x/a.json", "JH_AB_TICK_MS": "16.7"})
        for f in os.listdir(self.tmp):
            if f.startswith(("env.", "args.")):
                os.unlink(os.path.join(self.tmp, f))
        subprocess.run([LAUNCH] + [a.replace("CFG", cfg) for a in args], env=env, check=True)
        envs = sorted(f for f in os.listdir(self.tmp) if f.startswith("env."))
        return cfg, _text(os.path.join(self.tmp, envs[0]))

    def test_start_sets_the_diagnostics_and_the_socket_only(self):
        cfg, env = self.run_launch("--config", "CFG", "--mpv-loglevel", "info")
        self.assertEqual(env.split(), ["JMS_ART_DIAG=/x/a.json", "JMS_FRAME_DIAG=1",
                                       "JMS_FRAME_LOG=/x/f.tsv", "JMS_TICK_MS=16.7"])
        self.assertEqual(_text(os.path.join(cfg, "mpv.conf")),
                         "input-ipc-server=/x/mpv.sock\n")

    def test_stop_passes_through_without_diagnostics_or_config_changes(self):
        cfg, env = self.run_launch("--config", "CFG", "stop")
        self.assertEqual(env, "")
        self.assertEqual(_text(os.path.join(cfg, "mpv.conf")), "")


class Preflight(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        for k in [k for k in os.environ if k.startswith("JMS_")]:
            del os.environ[k]

    def test_an_empty_work_dir_and_a_bad_environment_are_all_reported(self):
        os.environ["JMS_TICK_MS"] = "20"
        os.environ.pop("JH_MAX_MPV", None)
        with tempfile.TemporaryDirectory() as w:
            bad = "\n".join(S.preflight(w, os.path.join(w, "nopython"), need_scope=False))
        for want in ("no server", "need 120 posters", "no python", "JH_MAX_MPV must be 1", "JMS_TICK_MS"):
            self.assertIn(want, bad)

    def test_a_real_run_must_be_inside_the_three_gib_scope(self):
        os.environ["JH_MAX_MPV"] = "1"
        with tempfile.TemporaryDirectory() as w:
            bad = "\n".join(S.preflight(w, "/nonexistent", need_scope=True))
        self.assertTrue("not inside a tools/limited_run.sh scope" in bad or "3 GiB" in bad, bad)


class Report(Tmp):
    def complete_run(self, s, run, **over):
        phases = {}
        for ph in CF.phases_for("pilot"):
            phases[ph["name"]] = {
                "frames": {"active_frames": 0 if ph["name"] == "idle" else 80, "gap_ms": {"p50": 21.0}},
                "resources": {"cgroup_cpu_pct": 30.0, "app_cpu_pct": 20.0, "pss_max_mib": 300.0,
                              "mem_current_max_mib": 800.0},
                "mpv_log": {"readable": True, "queued_events": 0},
                "art_decodes": 0}
        phases["cold_wheel"]["art_decodes"] = 50
        phases["cold_wheel"]["settle_s"] = 5.0
        m = {"verify": {"ok": True}, "phases": phases, "gpu": {"source": "amdgpu gpu_busy_percent"},
             "whole_run": {"scope_mem_peak_mib": 900.0, "mpv_log": {"queued_events": 0}}}
        m.update(over)
        d = os.path.join(s, "runs", run)
        _write(os.path.join(d, "metrics.json"), json.dumps(m))
        _write(os.path.join(d, "run.json"), '{"status": "complete"}')

    def test_a_good_pilot_validates_and_names_no_arm(self):
        s = os.path.join(self.tmp, "s")
        CF.new_session(s, "pilot", random.Random(3))
        for run in ("run-01", "run-02"):
            self.complete_run(s, run)
            CF.record_rating(s, run, 3, 3)
        fails, warns = R.validate(s)
        self.assertEqual((fails, warns), ([], []))

    def test_problems_are_failures_and_warnings(self):
        s = os.path.join(self.tmp, "s")
        CF.new_session(s, "pilot", random.Random(3))
        self.complete_run(s, "run-01")                                     # unrated
        bad = _json(os.path.join(s, "runs", "run-01", "metrics.json"))
        bad["phases"]["warm_fling"]["art_decodes"] = 4
        bad["phases"]["warm_nudge"]["frames"]["active_frames"] = 3
        bad["phases"]["touchpad"]["resources"]["app_cpu_pct"] = None
        bad["gpu"] = {"source": None}
        bad["whole_run"]["mpv_log"]["queued_events"] = 2
        _write(os.path.join(s, "runs", "run-01", "metrics.json"), json.dumps(bad))
        fails, warns = R.validate(s)
        text = "\n".join(fails + warns)
        for want in ("no rating", "only 3 active frames", "app_cpu_pct not measured",
                     "not fully warm", "queued-event warnings", "no GPU utilisation"):
            self.assertIn(want, text)
        self.assertNotIn("16.7", text)

    def test_reveal_text_joins_key_rating_and_metrics(self):
        s = os.path.join(self.tmp, "s")
        CF.new_session(s, "pilot", random.Random(3))
        self.complete_run(s, "run-01")
        self.complete_run(s, "run-02")
        with self.assertRaises(CF.PlanError):
            R.reveal_text(s)
        CF.record_rating(s, "run-02", 2, 2)
        CF.record_rating(s, "run-01", 5, 4, "?", "fine")
        out = R.reveal_text(s)
        self.assertRegex(out, r"== run-01  complete  floor (30|16\.7) ms  smoothness 5  responsiveness 4")
        self.assertIn("warm_fling", out)


class Manual(Tmp):
    def sh(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JMS_", "JH_"))}
        env["JH_JF_WORK"] = self.tmp
        return subprocess.run([MANUAL] + list(args), env=env, capture_output=True, text=True)

    def test_plan_status_and_reveal_refusal(self):
        s = os.path.join(self.tmp, "sess")
        r = self.sh("plan", "--pilot", "--session", s)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotRegex(r.stdout, r"16\.7")
        st = self.sh("status", "--session", s)
        self.assertEqual(st.stdout.split(), ["run-01", "pending", "run-02", "pending"])
        self.assertEqual(self.sh("plan", "--session", s).returncode, 1)     # never reused
        r = self.sh("reveal", "--session", s)                               # nothing run yet: sealed
        self.assertEqual(r.returncode, 65)
        self.assertNotRegex(r.stdout, r"floor")

    def test_unknown_arguments_and_commands_are_refused(self):
        self.assertEqual(self.sh("plan", "--nope").returncode, 64)
        self.assertEqual(self.sh("bogus").returncode, 64)
        self.assertEqual(self.sh("run").returncode, 65)                     # needs --session

    def test_dry_run_reports_what_is_missing_and_names_no_tick(self):
        s = os.path.join(self.tmp, "sess")
        self.sh("plan", "--pilot", "--session", s)
        r = self.sh("run", "--session", s, "--dry-run")
        self.assertEqual(r.returncode, 65, r.stdout + r.stderr)
        self.assertIn("no server", r.stdout)
        self.assertNotIn("16.7", r.stdout + r.stderr)


class Boundaries(unittest.TestCase):
    """The rules of this change, pinned as text so they cannot erode quietly."""

    def src(self, name):
        with open(os.path.join(TOOLS, name), encoding="utf-8") as fh:
            return fh.read()

    def test_no_synthetic_input_and_no_wayland_debug(self):
        for name in ("ab_session.py", "ab_launch.sh", "ab_metrics.py", "ab_config.py"):
            text = self.src(name)
            self.assertNotRegex(text, r"keypress|WHEEL_(UP|DOWN)|xdotool\s+click", name)
            self.assertNotRegex(text, r"WAYLAND_DEBUG\s*=|export WAYLAND_DEBUG", name)
        self.assertIn("is NOT enabled", self.src("ab_manual.sh"))

    def test_the_library_is_the_120_poster_folder_the_preflight_checks(self):
        """jf_run.py scans media/Movies, which holds nothing in a work dir made for scroll_probe: the
        first live attempt timed out on 'library scan finds the movie'. The run must point it at
        MoviesGrid, the folder preflight counts posters in."""
        self.assertRegex(self.src("jf_run.py"), r'MEDIA = os\.environ\.get\("JH_JF_MEDIA"\) or os\.path\.join\(WORK, "media", "Movies"\)')
        session = self.src("ab_session.py")
        self.assertIn('"JH_JF_MEDIA": os.path.join(work, "media", "MoviesGrid")', session)
        self.assertIn('os.path.join(work, "media", "MoviesGrid")', session.split("def preflight")[1])

    def test_the_limits_are_kept(self):
        manual = self.src("ab_manual.sh")
        self.assertIn("tools/limited_run.sh", manual)
        self.assertIn("JH_MAX_MPV=1", manual)
        self.assertRegex(self.src("ab_session.py"), r"GIB3\s*=\s*3 \* 1024 \*\* 3")
        self.assertIn('os.environ.get("JH_MAX_MPV") != "1"', self.src("ab_session.py"))

    def test_the_tick_is_never_printed_or_stored_with_a_run(self):
        text = self.src("ab_session.py")
        for m in re.finditer(r"say\(([^\n]*)", text):
            self.assertNotIn("tick", m.group(1))
        for m in re.finditer(r"^\s*metrics\[[^\n]*", text, re.M):
            self.assertNotIn("tick", m.group(0))
        self.assertNotIn("tick", text.split("def main")[1].split("metrics = {}")[1].split("finally:")[0]
                         .split('"JH_AB_TICK_MS"')[0].split("eff =")[0].replace("tick_for_run", ""))


if __name__ == "__main__":
    unittest.main()
