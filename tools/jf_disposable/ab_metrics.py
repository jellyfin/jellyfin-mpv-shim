"""Measurements for the blinded cadence comparison (ab_session.py). Stdlib only.

Everything is read from outside the application: the scope's cgroup files, /proc, the AMD
sysfs busy counter, the app's own opt-in frame log (JMS_FRAME_LOG) and its stdout log. Nothing
is injected into the app, and reading costs a few small file reads a second.

What each number is, and is not:
  cgroup cpu      usage_usec of the WHOLE scope: the app, the throw-away Jellyfin server and the
                  orchestrator. The server's share is not tick-dependent but is in the figure.
  app cpu         utime+stime of the app's process tree, from /proc (includes mpv's threads,
                  since libmpv runs in-process).
  memory          cgroup memory.peak (whole scope, whole run) and memory.current per phase.
  PSS             proportional set size of the app's tree from smaps_rollup.
  GPU             amdgpu's gpu_busy_percent where the kernel exposes it, a coarse counter read
                  once a second. None where it does not exist; nothing else is guessed.
  frames          what the renderer SUBMITTED to mpv (gap between frames, render time, overlay
                  adds and bytes), never what reached the screen.
  mpv warnings    lines of the app's log matching known patterns, counted between byte offsets.
"""
import glob
import os
import re
import threading
import time

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
SLOW_GAP_MS = 50.0
IDLE_GAP_MS = 250.0      # a longer gap is the user pausing, not a stutter (as in scroll_probe)

QUEUED = re.compile(r"too many .{0,40}events|events? .{0,20}queued|queued .{0,20}events?|event queue", re.I)
SUSPECT = re.compile(r"\b(dropp?ed|underrun|overrun|timed? ?out|stall(ed)?)\b", re.I)


# ----------------------------------------------------------------------------- cgroup, /proc

def cgroup_dir(pid="self", root="/sys/fs/cgroup", proc="/proc"):
    """Directory of the cgroup-v2 path of ``pid``, or None."""
    try:
        with open("%s/%s/cgroup" % (proc, pid), encoding="utf-8") as fh:
            for ln in fh:
                if ln.startswith("0::"):
                    d = root + ln[3:].strip()
                    return d if os.path.isdir(d) else None
    except OSError:
        pass
    return None


def _num(path):
    try:
        with open(path, encoding="utf-8") as fh:
            t = fh.read().strip()
        return None if t == "max" else int(t)
    except (OSError, ValueError):
        return None


def read_cgroup(d):
    """cpu usec (total, user, system), throttled periods, memory current and peak (bytes)."""
    out = {"cpu_usec": None, "cpu_user_usec": None, "cpu_system_usec": None, "throttled": None,
           "mem_current": _num(os.path.join(d, "memory.current")),
           "mem_peak": _num(os.path.join(d, "memory.peak")),
           "mem_max": _num(os.path.join(d, "memory.max"))}
    try:
        with open(os.path.join(d, "cpu.stat"), encoding="utf-8") as fh:
            st = dict(ln.split() for ln in fh if len(ln.split()) == 2)
        out["cpu_usec"] = int(st["usage_usec"])
        out["cpu_user_usec"] = int(st.get("user_usec", 0))
        out["cpu_system_usec"] = int(st.get("system_usec", 0))
        out["throttled"] = int(st.get("nr_throttled", 0))
    except (OSError, ValueError, KeyError):
        pass
    return out


def tree_pids(root_pid, proc="/proc"):
    """root_pid and its descendants, from the ppid field of each /proc/<pid>/stat."""
    kids = {}
    for p in glob.glob("%s/[0-9]*" % proc):
        try:
            with open(p + "/stat", encoding="utf-8") as fh:
                st = fh.read()
            kids.setdefault(int(st[st.rindex(")") + 2:].split()[1]), []).append(int(os.path.basename(p)))
        except (OSError, ValueError, IndexError):
            continue
    out, todo = [], [root_pid]
    while todo:
        x = todo.pop()
        if x in out:
            continue
        out.append(x)
        todo += kids.get(x, [])
    return out


def read_proc_tree(pids, proc="/proc"):
    """Summed (pss_kib, rss_kib, cpu_ticks) over pids; a process that vanished is skipped."""
    pss = rss = ticks = 0
    for p in pids:
        try:
            with open("%s/%d/smaps_rollup" % (proc, p), encoding="utf-8") as fh:
                for ln in fh:
                    if ln.startswith("Pss:"):
                        pss += int(ln.split()[1])
                    elif ln.startswith("Rss:"):
                        rss += int(ln.split()[1])
            with open("%s/%d/stat" % (proc, p), encoding="utf-8") as fh:
                st = fh.read()
            f = st[st.rindex(")") + 2:].split()
            ticks += int(f[11]) + int(f[12])
        except (OSError, ValueError, IndexError):
            continue
    return pss, rss, ticks


def gpu_sources(drm="/sys/class/drm"):
    """{'paths': [gpu_busy_percent files], 'vendors': [...]}; empty paths when nothing is exposed."""
    paths = sorted(glob.glob(drm + "/card[0-9]*/device/gpu_busy_percent"))
    vendors = []
    for v in sorted(glob.glob(drm + "/card[0-9]*/device/vendor")):
        try:
            with open(v, encoding="utf-8") as fh:
                vendors.append(fh.read().strip())
        except OSError:
            pass
    return {"paths": paths, "vendors": vendors}


def read_gpu(paths):
    """Highest busy percent across the files, or None when none can be read."""
    vals = [v for v in (_num(p) for p in paths) if v is not None]
    return max(vals) if vals else None


# ----------------------------------------------------------------------------- sampling

class Sampler:
    """Reads everything once a second on a thread, and on demand at phase boundaries."""

    def __init__(self, cgroup, app_pid_fn, gpu_paths=(), interval=1.0, clock=time.monotonic,
                 proc="/proc"):
        self.cgroup, self.app_pid_fn, self.gpu_paths = cgroup, app_pid_fn, list(gpu_paths)
        self.interval, self.clock, self.proc = interval, clock, proc
        self.rows, self._lock = [], threading.Lock()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def snap(self):
        row = {"t": self.clock()}
        row.update(read_cgroup(self.cgroup))
        pid = self.app_pid_fn()
        row["pss_kib"] = row["rss_kib"] = row["app_ticks"] = None
        if pid:
            row["pss_kib"], row["rss_kib"], row["app_ticks"] = read_proc_tree(
                tree_pids(pid, self.proc), self.proc)
        row["gpu_busy"] = read_gpu(self.gpu_paths)
        with self._lock:
            self.rows.append(row)
        return row

    def _run(self):
        while not self._stop.is_set():
            self.snap()
            self._stop.wait(self.interval)

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t.is_alive():
            self._t.join(3)
        return self.snap()

    def between(self, t0, t1):
        with self._lock:
            return [r for r in self.rows if t0 <= r["t"] <= t1]


def summarize_samples(rows):
    """Resource figures over the samples of one phase (first and last bound the deltas)."""
    if len(rows) < 2:
        return {"samples": len(rows)}
    a, b = rows[0], rows[-1]
    wall = b["t"] - a["t"]
    out = {"samples": len(rows), "wall_s": round(wall, 2)}

    def pct(x0, x1, per):
        return None if x0 is None or x1 is None or wall <= 0 else round((x1 - x0) / per / wall * 100, 1)
    out["cgroup_cpu_pct"] = pct(a["cpu_usec"], b["cpu_usec"], 1e6)        # of one core
    out["app_cpu_pct"] = pct(a["app_ticks"], b["app_ticks"], float(CLK_TCK))
    for k, name in (("mem_current", "mem_current_max_mib"),):
        v = [r[k] for r in rows if r[k] is not None]
        out[name] = round(max(v) / 1048576, 1) if v else None
    p = [r["pss_kib"] for r in rows if r["pss_kib"] is not None]
    out["pss_max_mib"] = round(max(p) / 1024, 1) if p else None
    g = [r["gpu_busy"] for r in rows if r["gpu_busy"] is not None]
    out["gpu_busy_mean_pct"] = round(sum(g) / len(g), 1) if g else None
    out["gpu_busy_max_pct"] = max(g) if g else None
    return out


# ----------------------------------------------------------------------------- frame log

class FrameTail:
    """Follows the app's frame log and stamps each new line with the time it was first seen.

    The log's own clock (mpv's) is not the orchestrator's, so a phase is cut by *seen* time,
    which is late by at most the poll interval (5 ms here) -- negligible against phases of
    seconds. Columns of a line: t, gap_ms, render_ms, add_ms, adds, bytes.
    """

    def __init__(self, path, interval=0.005, clock=time.monotonic):
        self.path, self.interval, self.clock = path, interval, clock
        self.rows, self._buf, self._pos = [], "", 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def poll_once(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return 0
        if not chunk:
            return 0
        now = self.clock()
        self._buf += chunk
        *lines, self._buf = self._buf.split("\n")
        n = 0
        for ln in lines:
            cols = ln.split("\t")
            if len(cols) != 6:
                continue
            try:
                row = tuple(float(c) for c in cols) + (now,)
            except ValueError:
                continue
            with self._lock:
                self.rows.append(row)
            n += 1
        return n

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t.is_alive():
            self._t.join(2)
        self.poll_once()

    def between(self, t0, t1):
        with self._lock:
            return [r for r in self.rows if t0 <= r[6] <= t1]


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def summarize_frames(rows):
    """Frame figures for one phase. Idle gaps (>= 250 ms) are excluded from timing and counted."""
    act = [r for r in rows if r[1] < IDLE_GAP_MS and r[1] > 0]
    out = {"frames": len(rows), "active_frames": len(act), "idle_gaps": len(rows) - len(act)}
    if not act:
        return out
    gaps = [r[1] for r in act]
    span = sum(gaps) / 1000.0
    out.update({
        "gap_ms": {"p50": pct(gaps, .5), "p90": pct(gaps, .9), "p99": pct(gaps, .99), "max": round(max(gaps), 1)},
        "render_ms": {"p50": pct([r[2] for r in act], .5), "p90": pct([r[2] for r in act], .9)},
        "slow_gaps_ge_50ms": sum(1 for g in gaps if g >= SLOW_GAP_MS),
        "fps": round(len(act) / span, 1) if span > 0 else None,
        "render_busy_pct": round(sum(r[2] for r in act) / 10.0 / span, 1) if span > 0 else None,
        "adds_per_frame": round(sum(r[4] for r in act) / len(act), 2),
        "overlay_mb_per_s": round(sum(r[5] for r in act) / 1e6 / span, 1) if span > 0 else None,
    })
    return out


# ----------------------------------------------------------------------------- mpv log

def scan_log(path, start=0, end=None, keep=8):
    """Count queued-event and other suspect lines of the app's log between byte offsets."""
    out = {"queued_events": 0, "suspect": 0, "examples": []}
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read() if end is None else fh.read(max(0, end - start))
    except OSError:
        return dict(out, readable=False)
    for ln in data.decode("utf-8", "replace").splitlines():
        q, s = bool(QUEUED.search(ln)), bool(SUSPECT.search(ln))
        out["queued_events"] += q
        out["suspect"] += (s and not q)
        if (q or s) and len(out["examples"]) < keep and ln[:200] not in out["examples"]:
            out["examples"].append(ln[:200])
    out["readable"] = True
    return out


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
