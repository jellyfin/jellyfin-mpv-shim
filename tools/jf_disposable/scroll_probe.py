#!/usr/bin/env python3
"""Movies-grid scroll, first pass vs after the posters have loaded, with diagnostics.

    JH_JF_WORK=$W tools/limited_run.sh -t 600 -- env -u WAYLAND_DISPLAY xvfb-run -a \
        -s "-screen 0 1280x720x24 -nolisten tcp" python3 tools/jf_disposable/scroll_probe.py [N]

Optional, for the cadence experiment (see scroll_config.py and scroll_manual.sh --matrix):
JH_TICK_MS (frame-cadence floor, forwarded to the app as JMS_TICK_MS), JH_SCROLL_DELAY (seconds
between notches, default 0.03) and JH_RUN_LABEL (keep this run in $JH_JF_WORK/out/runs/<label>-<UTC
time>/ instead of overwriting the files in out/). `--check` prints the settings and exits.

Needs 120 posters in $W/media/MoviesGrid (make_library.py). Starts the disposable server of
jf_run.py, signs one JellyHome in (one player, a throw-away config dir that also holds its artwork
cache), opens the Movies grid and wheels down N notches and back up, twice with the same commands:
pass 1 while the posters are still arriving, pass 2 once they are cached. For each pass it reports,
as differences of the app's own opt-in counters (JMS_FRAME_DIAG: gaps between frames submitted to
mpv, overlay adds and bytes; JMS_ART_DIAG: time in fetch, decode, resize and strip composite), what
the pass cost. Results go to $JH_JF_WORK/out/scroll_probe.json.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import scroll_config                                        # noqa: E402

_pos = [a for a in sys.argv[1:] if not a.startswith("--")]
if any(a.startswith("--") and a != "--check" for a in sys.argv[1:]) or len(_pos) > 1 \
        or (_pos and not _pos[0].isdigit()):
    sys.exit("usage: scroll_probe.py [--check] [NOTCHES]")
NOTCHES = int(_pos[0]) if _pos else 90
try:
    CFG = scroll_config.from_env()
except scroll_config.ConfigError as _e:
    sys.exit("scroll_probe: %s" % _e)
RATE = CFG["wheel_delay_s"]    # seconds between notches (a steady mouse wheel)
if "--check" in sys.argv:       # before anything heavy is imported or started
    print(json.dumps(dict(CFG, notches=NOTCHES), sort_keys=True))
    sys.exit(0)
sys.path.insert(0, os.path.join(REPO, "tests", "e2e"))
_argv = sys.argv
sys.argv = [sys.argv[0], "server"]
import chapter_probe as C                                   # noqa: E402
J = C.J
sys.argv = _argv
OUT = {"config": dict(CFG, notches=NOTCHES)}
RUN_DIR = J.OUT      # replaced in main() when JH_RUN_LABEL is set
WHEEL_STEPS = []     # seconds between consecutive notches actually sent


def library(tok):
    media = os.path.join(J.WORK, "media", "MoviesGrid")
    opts = {"EnableRealtimeMonitor": False, "EnableChapterImageExtraction": False,
            "ExtractChapterImagesDuringLibraryScan": False, "EnableTrickplayImageExtraction": False,
            "ExtractTrickplayImagesDuringLibraryScan": False, "EnableInternetProviders": False,
            "EnablePhotos": False, "MetadataSavers": [], "PathInfos": [{"Path": media}]}
    J.requests.post(J.BASE + "/Library/VirtualFolders",
                    params={"name": "Movies", "collectionType": "movies", "refreshLibrary": "true"},
                    headers=J.hdr(tok), json={"LibraryOptions": opts}, timeout=30).raise_for_status()

    def ready():
        r = J.api("GET", "/Items", tok, params={"Recursive": "true", "IncludeItemTypes": "Movie",
                                               "Fields": "PrimaryImageAspectRatio"}).json()["Items"]
        return len(r) >= 120 and all(i.get("ImageTags", {}).get("Primary") for i in r)
    J.wait_for(ready, "120 movies with a primary image", 240, every=3)


def diag(app, art_path):
    d = {}
    try:
        d["frames"] = app.mpv.command("get_property", "user-data/mpvtk/diag") or {}
    except Exception:
        d["frames"] = {}
    try:
        with open(art_path, encoding="utf-8") as fh:
            d["art"] = json.load(fh)
    except (OSError, ValueError):
        d["art"] = {}
    return d


def delta(a, b):
    out = {"frames": {}, "art": {}}
    fa, fb = a["frames"], b["frames"]
    for k in ("frames", "idle", "ov_adds", "ov_bytes", "render_ms_sum", "add_ms_sum", "n_with", "n_without",
              "ms_with", "ms_without", "slow_with", "slow_without"):
        out["frames"][k] = round((fb.get(k) or 0) - (fa.get(k) or 0), 1)
    bounds = fb.get("bounds_ms") or []
    ha, hb = fa.get("hist") or [0] * len(bounds), fb.get("hist") or [0] * len(bounds)
    hist = [(hb[i] if i < len(hb) else 0) - (ha[i] if i < len(ha) else 0) for i in range(len(bounds))]
    out["frames"]["gap_hist_ms"] = {"<%d" % b: h for b, h in zip(bounds, hist)}
    out["frames"]["render_ms_max_so_far"] = round(fb.get("render_ms_max") or 0, 1)
    out["frames"]["slow_frames_total_so_far"] = fb.get("slow_n") or 0
    out["frames"]["slow_recent"] = [{k: round(v, 1) for k, v in r.items()} for r in (fb.get("slow") or [])][-12:]
    out["frames"]["gaps_ge_50ms"] = sum(h for b, h in zip(bounds, hist) if b > 50)
    for k in set(a["art"]) | set(b["art"]):
        x, y = a["art"].get(k, {"n": 0, "sum_ms": 0}), b["art"].get(k, {"n": 0, "sum_ms": 0})
        out["art"][k] = {"n": y["n"] - x["n"], "sum_ms": round(y["sum_ms"] - x["sum_ms"], 1),
                         "max_ms_so_far": round(y.get("max_ms", 0), 1)}
    return out


def frame_rows(path):
    """Per-frame log lines (JMS_FRAME_LOG): (t, gap_ms, render_ms, add_ms, adds, bytes)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return [tuple(float(x) for x in ln.split("\t")) for ln in fh if ln.count("\t") == 5]
    except OSError:
        return []


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else 0.0


def corr(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5, 2)


def alignment(rows):
    """How gaps line up with overlay-add time, over the frames of one active scroll.

    ``gap`` runs from the end of the previous frame to the end of this one, so it already
    contains this frame's own cost; ``same`` correlates it with this frame's add time, ``next``
    with the previous frame's (a stall that follows an expensive frame)."""
    rows = [r for r in rows if r[1] < 250]          # idle time is not a stutter
    if len(rows) < 5:
        return {"frames": len(rows)}
    gap = [r[1] for r in rows]; ms = [r[2] for r in rows]; add = [r[3] for r in rows]
    slow = [i for i, g in enumerate(gap) if g >= 50]
    excess = sum(gap[i] - 33.0 for i in slow)
    return {"frames": len(rows),
            "gap_ms": {"p50": pct(gap, .5), "p90": pct(gap, .9), "p99": pct(gap, .99), "max": round(max(gap), 1)},
            "render_ms": {"p50": pct(ms, .5), "p90": pct(ms, .9), "max": round(max(ms), 1)},
            "overlay_add_ms": {"p50": pct(add, .5), "p90": pct(add, .9), "max": round(max(add), 1)},
            "frames_with_adds": sum(1 for r in rows if r[4] > 0),
            "slow_gaps": len(slow),
            "slow_gaps_with_add_ms_ge_10": sum(1 for i in slow if add[i] >= 10),
            "corr_gap_vs_add_same_frame": corr(gap, add),
            "corr_gap_vs_render_same_frame": corr(gap, ms),
            "corr_gap_vs_add_previous_frame": corr(gap[1:], add[:-1]),
            "share_of_slow_gap_excess_that_is_add_time": round(sum(add[i] for i in slow) / excess, 2) if excess > 0 else None}


def settle(app, art_path, quiet=2.5, timeout=40):
    """Until no artwork step has finished and no overlay has been added for `quiet` s."""
    end, last, since = time.time() + timeout, None, None
    while time.time() < end:
        d = diag(app, art_path)
        key = (json.dumps(d["art"], sort_keys=True), d["frames"].get("ov_adds"))
        if key == last:
            since = since or time.time()
            if time.time() - since >= quiet:
                return True
        else:
            last, since = key, None
        time.sleep(0.5)
    return False


def wheel(app, key, n):
    last = None
    for _ in range(n):
        app.mpv.command("keypress", key)
        now = time.time()
        if last is not None:
            WHEEL_STEPS.append(now - last)
        last = now
        time.sleep(RATE)


class _PeakRss:
    """Highest summed RSS (KiB) of the app's process tree, sampled twice a second."""

    def __init__(self, pid):
        import threading
        self.pid, self.peak, self._stop = pid, 0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            tot = sum((J.stat(p) or (0, 0))[0] for p in J.tree_pids(self.pid))
            self.peak = max(self.peak, tot)
            self._stop.wait(0.5)

    def stop(self):
        self._stop.set()
        self._t.join(2)
        return self.peak


def verdict():
    """Plain-language reading of OUT; the numbers stay in the JSON."""
    lines = []
    mpv = OUT.get("mpv", {})
    vo = str(mpv.get("current-vo"))
    gpu = vo in ("gpu", "gpu-next")
    lines.append("video output: %s (gpu-api %s, context %s, display-fps %s)"
                 % (vo, mpv.get("gpu-api"), mpv.get("gpu-context"), mpv.get("display-fps")))
    if not gpu:
        lines.append("NOT a GPU output: a software output measures the software path, not yours.")
    for name in ("pass1_first_view", "pass2_after_loaded"):
        p = OUT.get(name)
        if not p:
            continue
        f = p["down"]["frames"]
        n = max(f["frames"], 1)
        mw = f["ms_with"] / f["n_with"] if f["n_with"] else 0.0
        mo = f["ms_without"] / f["n_without"] if f["n_without"] else 0.0
        art = p["down"]["art"]
        lines.append("%s: %d frames in %.1fs; gaps >=50ms: %d (%.0f%%); frames that added overlays: %d, mean %.1f ms "
                     "vs %.1f ms without; slow gaps ending in an add-frame: %d of %d; overlay-add time %.0f ms of %.0f ms "
                     "render; %.0f MB added; composites %d (%.0f ms), decodes %d"
                     % (name, f["frames"], p["scroll_down_s"], f["gaps_ge_50ms"], 100.0 * f["gaps_ge_50ms"] / n,
                        f["n_with"], mw, mo, f["slow_with"], f["slow_with"] + f["slow_without"],
                        f["add_ms_sum"], f["render_ms_sum"], f["ov_bytes"] / 1e6,
                        art.get("compose", {}).get("n", 0), art.get("compose", {}).get("sum_ms", 0),
                        art.get("decode", {}).get("n", 0)))
    for name in ("pass1_first_view", "pass2_after_loaded"):
        a = (OUT.get(name) or {}).get("down_alignment") or {}
        if a.get("gap_ms"):
            lines.append("%s, active scrolling, raw: gap ms %s; render ms %s; overlay-add ms %s; %d of %d frames added "
                         "overlays" % (name, a["gap_ms"], a["render_ms"], a["overlay_add_ms"], a["frames_with_adds"], a["frames"]))
            lines.append("  alignment: %d slow gaps (>=50 ms), %d of them with >=10 ms in overlay-add; corr(gap, add) same "
                         "frame %s, previous frame %s; corr(gap, render) %s; overlay-add is %s of the slow gaps' excess over 33 ms"
                         % (a["slow_gaps"], a["slow_gaps_with_add_ms_ge_10"], a["corr_gap_vs_add_same_frame"],
                            a["corr_gap_vs_add_previous_frame"], a["corr_gap_vs_render_same_frame"],
                            a["share_of_slow_gap_excess_that_is_add_time"]))
    p2 = OUT.get("pass2_after_loaded")
    if p2:
        f = p2["down"]["frames"]
        slow = f["slow_with"] + f["slow_without"]
        hitch = f["gaps_ge_50ms"] >= 0.10 * max(f["frames"], 1)
        mw = f["ms_with"] / f["n_with"] if f["n_with"] else 0.0
        mo = f["ms_without"] / f["n_without"] if f["n_without"] else 0.0
        tracks = slow > 0 and f["slow_with"] >= 0.7 * slow and mw >= 3 * max(mo, 0.1)
        lines.append("loaded pass: hitch %s; tracks the overlay calls %s."
                     % ("REPRODUCES" if hitch else "does not reproduce", "YES" if tracks else "no"))
        if hitch and tracks and gpu:
            lines.append("-> a fix aimed at the per-frame overlay-add cost is justified (see the proposal in the notes).")
        elif not gpu:
            lines.append("-> no conclusion for your hardware: the output was not a GPU one.")
        else:
            lines.append("-> do not change the renderer on this result.")
    lines.append("peak app RSS %s MiB" % OUT.get("peak_app_rss_mib"))
    return lines


def run_app(upw, art_path, flog):
    import _app
    first = _app.App(backend="libmpv", conf={"check_updates": False})
    cfg = first.config_dir
    try:
        first.start(timeout=60)
        first.wait_for(lambda f: _app.shown(f, "login-server"), timeout=60, what="login")
        first.type_into("login-server", J.BASE)
        first.type_into("login-user", "viewer")
        first.type_into("login-pass", upw, masked=True)
        first.key("ENTER")
        first.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60, what="home")
        first.quit(timeout=20)
    except Exception:
        first.close()
        raise
    # A fresh launch on a copy: the signed-in config, an empty artwork cache.
    shutil_cache = os.path.join(cfg, "cache")
    if os.path.isdir(shutil_cache):
        import shutil
        shutil.rmtree(shutil_cache)
    app = _app.App.copy_of(cfg, backend="libmpv", conf={"check_updates": False},
                           env=dict({"JMS_FRAME_DIAG": "1", "JMS_ART_DIAG": art_path, "JMS_FRAME_LOG": flog},
                            **scroll_config.app_env(CFG)))
    try:
        app.start(timeout=60)
        f = app.wait_for(lambda f: any((n.get("id") or "").startswith("row-libs-") and len(n["id"]) == 41
                                       for n in f.get("nodes", [])), timeout=60, what="libraries row")
        lib = next(n["id"] for n in f["nodes"] if (n.get("id") or "").startswith("row-libs-") and len(n["id"]) == 41)
        app.move_to(lib)
        app.press_until("ENTER", lambda f: _app.shown(f, "grid-sort"), what="the Movies grid")
        w, h = app.frame()["w"], app.frame()["h"]
        app.point(w * 0.5, h * 0.6)
        time.sleep(1.0)
        OUT["window"] = [w, h]
        sampler = _PeakRss(app.proc.pid)
        for prop in ("current-vo", "gpu-api", "gpu-context", "hwdec-current", "display-fps", "osd-width", "osd-height"):
            try:
                OUT.setdefault("mpv", {})[prop] = app.prop(prop)
            except Exception as e:
                OUT.setdefault("mpv", {})[prop] = "n/a %s" % e
        # What the app says it is using, so a run cannot be mislabelled by an ignored override.
        eff = diag(app, art_path)["frames"].get("tick_ms")
        OUT["config"]["tick_ms_effective"] = eff
        want = CFG["tick_ms"] if CFG["tick_ms"] is not None else 30.0
        if eff is None or abs(eff - want) > 1e-6:
            raise RuntimeError("the app reports a %s ms cadence floor, the run asked for %s" % (eff, want))
        for name in ("pass1_first_view", "pass2_after_loaded"):
            before = diag(app, art_path)
            n0 = len(frame_rows(flog))
            t0 = time.time()
            wheel(app, "WHEEL_DOWN", NOTCHES)
            t_scroll = time.time() - t0
            mid = diag(app, art_path)
            active = frame_rows(flog)[n0:]
            shot = os.path.join(RUN_DIR, "scroll_%s_bottom.png" % name)
            try:
                app.mpv.command("screenshot-to-file", shot, "window")
            except Exception:
                pass
            wheel(app, "WHEEL_UP", NOTCHES + 30)
            t_total = time.time() - t0
            settled = settle(app, art_path)
            after = diag(app, art_path)
            OUT[name] = {"scroll_down_s": round(t_scroll, 2), "down_and_up_s": round(t_total, 2),
                         "settled": settled, "down": delta(before, mid), "down_alignment": alignment(active), "up_and_settle": delta(mid, after)}
            J.log(name, json.dumps(OUT[name]))
            if name == "pass1_first_view":
                settle(app, art_path)
        OUT["peak_app_rss_mib"] = round(sampler.stop() / 1024, 1)
        st = sorted(WHEEL_STEPS)
        OUT["wheel_interval_ms"] = {"asked": round(RATE * 1000, 2), "sent": len(st),
                                    "p50": pct([x * 1000 for x in st], .5),
                                    "p90": pct([x * 1000 for x in st], .9)} if st else {}
    finally:
        try:
            OUT["quit_rc"] = app.quit(timeout=20)
        except Exception:
            app.kill()
        app.close()
        first.close()


def main():
    global RUN_DIR
    t0 = time.time(); rc = 1
    if CFG["label"]:
        # One directory per run, never reused: a repeat of a configuration keeps both results.
        RUN_DIR = os.path.join(J.OUT, "runs", "%s-%s" % (CFG["label"], time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
        os.makedirs(RUN_DIR, exist_ok=False)
        J.log("run_dir:", RUN_DIR)
    try:
        J.start_server()
        tok, upw = C.wizard()
        library(tok)
        art = os.path.join(RUN_DIR, "art_diag.json")
        if os.path.exists(art):
            os.unlink(art)
        flog = os.path.join(RUN_DIR, "frame_log.tsv")
        if os.path.exists(flog):
            os.unlink(flog)
        run_app(upw, art, flog)
        rep = verdict()
        with open(os.path.join(RUN_DIR, "scroll_report.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(rep) + "\n")
        J.log("\n" + "\n".join(rep))
        rc = 0
    except Exception as e:
        import traceback
        J.log("FAILED:", type(e).__name__, J.redact(str(e)), J.redact(traceback.format_exc()[-1500:]))
    finally:
        with open(os.path.join(RUN_DIR, "scroll_probe.json"), "w", encoding="utf-8") as fh:
            json.dump(OUT, fh, indent=1)
        J.teardown(t0)
        if RUN_DIR != J.OUT:    # teardown writes these to out/, where the next run would replace them
            import shutil
            for f in os.listdir(J.OUT):
                if f == "samples.csv" or f.startswith("server_"):
                    try:
                        shutil.copy2(os.path.join(J.OUT, f), os.path.join(RUN_DIR, f))
                    except OSError:
                        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
