#!/usr/bin/env python3
"""One run of the blinded renderer-cadence comparison, driven by a person on the real desktop.

    tools/jf_disposable/ab_manual.sh run --session DIR        # the supported way (adds the limits)

Starts the disposable Jellyfin and ONE JellyHome window on the real display, through the existing
jf_run.py scroll mode (login seeded in a private Xvfb, then the normal GPU configuration), with the
frame diagnostics on and the renderer cadence floor set to the tick the sealed plan assigns to this
run. The person then scrolls with a REAL mouse wheel and touchpad through the phases printed in the
terminal, while this process records frames (the app's own log), the scope's cgroup CPU and memory,
the app's PSS and the AMD GPU busy counter, and counts queued-event warnings in the app's log.

No synthetic wheel events are sent, and nothing is changed in the application. The only addition to
how it is launched is ab_launch.sh, which sets the diagnostics' environment and an mpv IPC socket for
two read-only property reads (the effective tick, the video output).

The tick never appears in this process's output or in the run's metrics; ab_config.reveal prints it
after the ratings are recorded.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import ab_config as CF                                       # noqa: E402
import ab_metrics as M                                       # noqa: E402
import scroll_config                                         # noqa: E402

GIB3 = 3 * 1024 ** 3
LEAD_S = 5      # between Enter in the terminal and the start of a phase; not measured


class Abort(RuntimeError):
    pass


def say(*a):
    print(*a, flush=True)


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----------------------------------------------------------------------------- preconditions

def preflight(work, py, need_scope):
    """Everything that can be checked without starting anything. Returns a list of problems."""
    bad = []
    if not os.access(os.path.join(work, "server", "jellyfin", "jellyfin"), os.X_OK):
        bad.append("no server at %s/server/jellyfin (set JH_JF_WORK; see README.md)" % work)
    posters = sum(1 for _r, _d, fs in os.walk(os.path.join(work, "media", "MoviesGrid")) for f in fs
                  if f == "poster.jpg")
    if posters < 120:
        bad.append("need 120 posters in %s/media/MoviesGrid (found %d)" % (work, posters))
    if not os.access(py, os.X_OK):
        bad.append("no python at %s (set JH_PY)" % py)
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        bad.append("no display: this test is meant for your desktop")
    for tool in ("Xvfb", "xdotool"):
        if not shutil.which(tool):
            bad.append("%s is not installed (the login seeding needs it)" % tool)
    if os.environ.get("JH_MAX_MPV") != "1":
        bad.append("JH_MAX_MPV must be 1")
    inherited = sorted(k for k in os.environ if k.startswith("JMS_"))
    if inherited:
        bad.append("unset %s: the run sets its own diagnostics" % ", ".join(inherited))
    if need_scope:
        cg = M.cgroup_dir()
        if not cg or "jh-limited-" not in cg:
            bad.append("not inside a tools/limited_run.sh scope")
        elif M.read_cgroup(cg)["mem_max"] != GIB3:
            bad.append("the scope's memory.max is not 3 GiB")
    return bad


# ----------------------------------------------------------------------------- mpv IPC (read only)

def ipc_props(path, props, timeout=4.0):
    """Values of mpv properties over its JSON IPC socket; a missing one is None."""
    out = {}
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(path)
        buf = b""
        for i, p in enumerate(props, 1):
            s.sendall((json.dumps({"command": ["get_property", p], "request_id": i}) + "\n").encode())
            while True:
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(ln)
                    except ValueError:
                        continue
                    if msg.get("request_id") == i:
                        out[p] = msg.get("data") if msg.get("error") == "success" else None
                        break
                else:
                    buf += s.recv(65536)
                    continue
                break
    finally:
        s.close()
    return out


def wait_for(fn, what, timeout, every=0.5, alive=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        if alive is not None and not alive():
            raise Abort("%s: the driver exited" % what)
        time.sleep(every)
    raise Abort("timed out waiting for %s" % what)


def art_decodes(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh).get("decode", {}).get("n", 0))
    except (OSError, ValueError, TypeError):
        return None


def settle(path, quiet=3.0, timeout=40.0):
    """Until the artwork counters stop changing for `quiet` s; seconds waited, or None on timeout."""
    t0, last, since = time.monotonic(), None, None
    while time.monotonic() - t0 < timeout:
        try:
            with open(path, encoding="utf-8") as fh:
                cur = fh.read()
        except OSError:
            cur = ""
        if cur == last:
            since = since or time.monotonic()
            if time.monotonic() - since >= quiet:
                return round(time.monotonic() - t0, 1)
        else:
            last, since = cur, None
        time.sleep(0.5)
    return None


# ----------------------------------------------------------------------------- the guided phases

def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise Abort("no terminal input")


def run_phases(plan, tail, sampler, client_log, art_path, alive):
    results = {}
    for ph in plan["phases"]:
        name, secs = ph["name"], ph["seconds"]
        say("\n--- %s (%d s) ---\n%s" % (name, secs, ph["text"]))
        ans = ask("Press Enter when ready to START%s: " % (" (s = skip)" if name == "touchpad" else ""))
        if name == "touchpad" and ans.strip().lower() == "s":
            results[name] = {"skipped": True}
            continue
        say("recording starts in %d s: put the pointer over the JellyHome window now" % LEAD_S)
        for _ in range(LEAD_S * 5):
            if not alive():
                raise Abort("the app window closed")
            time.sleep(0.2)
        dec0, off0 = art_decodes(art_path), M.file_size(client_log)
        sampler.snap()
        t0 = time.monotonic()
        say("GO  \a(%d s)" % secs)
        while time.monotonic() - t0 < secs:
            if not alive():
                raise Abort("the app window closed during %s" % name)
            time.sleep(0.2)
        t1 = time.monotonic()
        say("STOP \a")
        sampler.snap()
        time.sleep(0.15)                       # the last frames reach the log
        dec1, off1 = art_decodes(art_path), M.file_size(client_log)
        res = {"seconds_planned": secs, "seconds_actual": round(t1 - t0, 2),
               "frames": M.summarize_frames(tail.between(t0, t1)),
               "resources": M.summarize_samples(sampler.between(t0, t1 + 0.01)),
               "mpv_log": M.scan_log(client_log, off0, off1),
               "art_decodes": None if dec0 is None or dec1 is None else dec1 - dec0}
        if name == "cold_wheel":
            res["settle_s"] = settle(art_path)
        results[name] = res
    return results


def ask_rating():
    say("\nRate this run before anything is revealed.")

    def num(label):
        while True:
            v = ask("%s (1 = poor .. 5 = excellent): " % label).strip()
            if v in ("1", "2", "3", "4", "5"):
                return int(v)
    sm, rs = num("Smoothness"), num("Responsiveness")
    while True:
        g = ask("Your guess for the renderer floor, 30, 16.7 or ? : ").strip() or "?"
        if g in ("30", "16.7", "?"):
            break
    return sm, rs, g, ask("Notes (optional): ").strip()


# ----------------------------------------------------------------------------- one run

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--run", help="default: the next pending run")
    ap.add_argument("--dry-run", action="store_true", help="check everything, start nothing")
    a = ap.parse_args(argv)
    work = os.environ.get("JH_JF_WORK", os.path.join(HERE, "work"))
    py = os.environ.get("JH_PY", os.path.join(REPO, "dist", "local-test", "venv", "bin", "python"))
    launcher = os.environ.get("JH_AB_REAL_APP", os.path.join(HERE, "ab_app.sh"))
    try:
        plan = CF.load_plan(a.session)
        run = a.run or CF.next_run(a.session)
        if run is None:
            say("every run in the plan has been started; nothing to do")
            return 0
        if CF.run_state(a.session, run) != "pending":
            raise Abort("%s already has a record; start a new session or remove its directory" % run)
        tick = CF.tick_for_run(a.session, run)          # never printed
    except CF.PlanError as e:
        say("ab_session: %s" % e)
        return 64
    bad = preflight(work, py, need_scope=not a.dry_run)
    if not os.access(launcher, os.X_OK):
        bad.append("no launcher at %s (set JH_AB_REAL_APP)" % launcher)
    if bad:
        say("ab_session: cannot run:\n  " + "\n  ".join(bad))
        return 65
    say("%s of %d (%s plan): phases %s" % (run, len(plan["runs"]), plan["mode"],
                                          ", ".join("%s %ds" % (p["name"], p["seconds"]) for p in plan["phases"])))
    if a.dry_run:
        say("(dry run: nothing started)")
        return 0

    run_dir = os.path.join(a.session, "runs", run)
    os.makedirs(run_dir, exist_ok=False)
    rec = {"status": "failed", "started_utc": utc(), "run": run}
    jf = tail = sampler = None
    flag = os.path.join(run_dir, "stop.flag")
    # A unix socket path is limited to ~107 bytes; a session path is far longer, so the socket
    # lives in a short private (0700) directory that is removed at the end of the run.
    sock_dir = tempfile.mkdtemp(prefix="jhab-")
    sock = os.path.join(sock_dir, "mpv.sock")
    frame_log, art_path = os.path.join(run_dir, "frame_log.tsv"), os.path.join(run_dir, "art_diag.json")
    client_log = os.path.join(work, "out", "client.log.raw")
    metrics = {}
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JMS_", "JH_AB_"))}
        env.update({"JH_JF_WORK": work, "JH_JF_MODE": "scroll", "JH_JF_FLAG": flag, "JH_JF_EXPECT": "120",
                    "JH_JF_MEDIA": os.path.join(work, "media", "MoviesGrid"),
                    "JH_JF_APP": os.path.join(HERE, "ab_launch.sh"), "JH_AB_REAL_APP": launcher,
                    "JH_AB_IPC": sock, "JH_AB_FRAME_LOG": frame_log, "JH_AB_ART_DIAG": art_path,
                    "JH_AB_TICK_MS": scroll_config.app_env({"tick_ms": tick})["JMS_TICK_MS"],
                    "PYTHONUNBUFFERED": "1"})
        out = open(os.path.join(run_dir, "jf_run.out"), "w", encoding="utf-8")
        jf = subprocess.Popen([py, os.path.join(HERE, "jf_run.py"), "full"], env=env, stdout=out,
                              stderr=subprocess.STDOUT, start_new_session=True)
        alive = lambda: jf.poll() is None                         # noqa: E731
        say("starting the throw-away server and the app window (about a minute)...")

        def ready():
            with open(os.path.join(run_dir, "jf_run.out"), encoding="utf-8", errors="replace") as fh:
                return "READY: window is up" in fh.read()
        wait_for(ready, "the app window", 300, 1.0, alive)
        with open(os.path.join(run_dir, "jf_run.out"), encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        pid = int(txt.split("client pid ", 1)[1].split()[0])
        app_alive = lambda: alive() and os.path.exists("/proc/%d" % pid)      # noqa: E731

        wait_for(lambda: os.path.exists(sock), "the mpv IPC socket", 30, 0.3, alive)
        props = ipc_props(sock, ["user-data/mpvtk/diag", "current-vo", "gpu-api", "gpu-context",
                                 "hwdec-current", "display-fps", "osd-width", "osd-height"])
        eff = (props.get("user-data/mpvtk/diag") or {}).get("tick_ms")
        ok = eff is not None and abs(eff - tick) < 1e-6
        verify = {"ok": ok, "current_vo": props.get("current-vo"), "gpu_api": props.get("gpu-api"),
                  "gpu_context": props.get("gpu-context"), "hwdec": props.get("hwdec-current"),
                  "display_fps": props.get("display-fps"),
                  "window": [props.get("osd-width"), props.get("osd-height")]}
        metrics["verify"] = verify
        CF._dump(os.path.join(a.session, ".effective-%s.json" % run), {"tick_ms": eff}, 0o600)
        if eff is None:
            raise Abort("the app publishes no frame diagnostics (is it running the repository source "
                        "and not an older installed copy?); run discarded")
        if not ok:
            raise Abort("the app does not report the cadence floor the plan assigned; run discarded")
        if verify["current_vo"] not in ("gpu", "gpu-next"):
            raise Abort("the video output is not a GPU one (%s)" % verify["current_vo"])

        gsrc = M.gpu_sources()
        metrics["gpu"] = {"source": "amdgpu gpu_busy_percent" if gsrc["paths"] else None,
                          "files": len(gsrc["paths"]), "vendors": gsrc["vendors"]}
        cg = M.cgroup_dir()
        sampler = M.Sampler(cg, lambda: pid if os.path.exists("/proc/%d" % pid) else None, gsrc["paths"])
        tail = M.FrameTail(frame_log)
        sampler.start()
        tail.start()
        first = sampler.snap()
        t_start = time.monotonic()
        say("\nThe window is up on your desktop. In it, open the Movies library and wait for the grid "
            "(do not scroll yet).")
        ask("Press Enter when the Movies grid is showing: ")
        phases = run_phases(plan, tail, sampler, client_log, art_path, app_alive)
        metrics["phases"] = phases
        last = sampler.snap()
        metrics["whole_run"] = {"seconds": round(time.monotonic() - t_start, 1),
                                "scope_mem_peak_mib": round((last["mem_peak"] or 0) / 1048576, 1),
                                "mem_current_start_mib": round((first["mem_current"] or 0) / 1048576, 1),
                                "mpv_log": M.scan_log(client_log),
                                "throttled_periods": last["throttled"]}
        rating = ask_rating()
        CF.record_rating(a.session, run, rating[0], rating[1], rating[2], rating[3])
        rec["status"] = "complete"
    except (Abort, KeyboardInterrupt) as e:
        rec["error"] = str(e) or "interrupted"
        say("ab_session: %s" % rec["error"])
    except Exception as e:                                         # noqa: BLE001
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        say("ab_session: %s" % rec["error"])
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
        for stopper in (tail, sampler):
            if stopper:
                stopper.stop()
        if jf is not None:
            open(flag, "w", encoding="utf-8").close()
            try:
                jf.wait(90)
            except subprocess.TimeoutExpired:
                os.killpg(jf.pid, signal.SIGTERM)
                try:
                    jf.wait(20)
                except subprocess.TimeoutExpired:
                    os.killpg(jf.pid, signal.SIGKILL)
        for f, dst in (("client.log.raw", "app_stdout.log"), ("samples.csv", "process_samples.csv")):
            try:
                shutil.copy2(os.path.join(work, "out", f), os.path.join(run_dir, dst))
            except OSError:
                pass
        rec["finished_utc"] = utc()
        rec["verify_ok"] = bool((metrics.get("verify") or {}).get("ok"))
        CF._dump(os.path.join(run_dir, "metrics.json"), metrics)
        CF._dump(os.path.join(run_dir, "run.json"), rec)
    say("\n%s %s. Nothing about the configuration has been shown." % (run, rec["status"]))
    return 0 if rec["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
