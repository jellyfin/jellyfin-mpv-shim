#!/usr/bin/env python3
"""Check and read out a blinded A/B session (see ab_config.py, ab_manual.sh).

    ab_report.py validate SESSION   do the recorded runs hold what the analysis needs? (never shows ticks)
    ab_report.py reveal   SESSION   join the sealed key to ratings and metrics; refuses until every
                                    finished run has a rating

Stdlib only.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ab_config as CF                                       # noqa: E402

WARM = ("warm_fling", "warm_nudge")


def _metrics(session, run):
    try:
        with open(os.path.join(session, "runs", run, "metrics.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def validate(session):
    """(failures, warnings) for the runs recorded so far. Reports nothing that names an arm."""
    plan = CF.load_plan(session)
    ratings = CF._load(os.path.join(session, "ratings.json"))
    fails, warns = [], []
    done = 0
    for r in plan["runs"]:
        run = r["run"]
        st = CF.run_state(session, run)
        if st == "pending":
            continue
        if st != "complete":
            fails.append("%s: status %s" % (run, st))
            continue
        done += 1
        m = _metrics(session, run) or {}
        if not (m.get("verify") or {}).get("ok"):
            fails.append("%s: the effective cadence floor was not confirmed" % run)
        if run not in ratings:
            fails.append("%s: no rating recorded" % run)
        for ph in plan["phases"]:
            p = (m.get("phases") or {}).get(ph["name"])
            tag = "%s/%s" % (run, ph["name"])
            if p is None:
                fails.append("%s: phase missing" % tag)
                continue
            if p.get("skipped"):
                warns.append("%s: skipped" % tag)
                continue
            fr, rs = p.get("frames", {}), p.get("resources", {})
            if ph["name"] != "idle" and fr.get("active_frames", 0) < 10:
                fails.append("%s: only %s active frames" % (tag, fr.get("active_frames")))
            for k in ("cgroup_cpu_pct", "app_cpu_pct", "pss_max_mib", "mem_current_max_mib"):
                if rs.get(k) is None:
                    fails.append("%s: %s not measured" % (tag, k))
            if not p.get("mpv_log", {}).get("readable"):
                fails.append("%s: the app log could not be read" % tag)
            if ph["name"] in WARM and p.get("art_decodes"):
                warns.append("%s: %d artwork decodes, not fully warm" % (tag, p["art_decodes"]))
            if ph["name"] == "cold_wheel":
                if not p.get("art_decodes"):
                    warns.append("%s: no artwork decoded, the cache may not have been cold" % tag)
                if p.get("settle_s") is None:
                    warns.append("%s: artwork had not settled within 40 s" % tag)
        w = m.get("whole_run") or {}
        if w.get("mpv_log", {}).get("queued_events"):
            warns.append("%s: %d queued-event warnings in the app log" % (run, w["mpv_log"]["queued_events"]))
        if not (m.get("gpu") or {}).get("source"):
            warns.append("%s: no GPU utilisation source on this machine" % run)
    if not done and not fails:
        warns.append("no run has finished yet")
    return fails, warns


def _row(run, tick, ph, p):
    fr, rs = p.get("frames", {}), p.get("resources", {})
    g = fr.get("gap_ms") or {}
    return ("%-7s %-5s %-10s gap p50/p90/p99 %s/%s/%s  fps %s  slow %s  busy %s%%  adds/fr %s  MB/s %s | "
            "cgroup cpu %s%%  app cpu %s%%  pss %s MiB  gpu %s%%  queued %s"
            % (run, tick, ph, g.get("p50"), g.get("p90"), g.get("p99"), fr.get("fps"),
               fr.get("slow_gaps_ge_50ms"), fr.get("render_busy_pct"), fr.get("adds_per_frame"),
               fr.get("overlay_mb_per_s"), rs.get("cgroup_cpu_pct"), rs.get("app_cpu_pct"),
               rs.get("pss_max_mib"), rs.get("gpu_busy_mean_pct"), (p.get("mpv_log") or {}).get("queued_events")))


def reveal_text(session):
    lines = []
    plan = CF.load_plan(session)
    for e in CF.reveal(session):
        rt = e["rating"] or {}
        lines.append("== %s  %s  floor %g ms  smoothness %s  responsiveness %s  guessed %s  %s"
                     % (e["run"], e["state"], e["tick_ms"], rt.get("smoothness"), rt.get("responsiveness"),
                        rt.get("guess"), rt.get("notes", "")))
        m = _metrics(session, e["run"]) or {}
        for ph in plan["phases"]:
            p = (m.get("phases") or {}).get(ph["name"])
            if p and not p.get("skipped"):
                lines.append(_row(e["run"], "%g" % e["tick_ms"], ph["name"], p))
        w = m.get("whole_run") or {}
        lines.append("   scope memory peak %s MiB, queued-event warnings %s"
                     % (w.get("scope_mem_peak_mib"), (w.get("mpv_log") or {}).get("queued_events")))
    return "\n".join(lines)


def main(argv):
    if len(argv) != 2 or argv[0] not in ("validate", "reveal"):
        print(__doc__)
        return 64
    try:
        if argv[0] == "validate":
            fails, warns = validate(argv[1])
            for w in warns:
                print("WARN", w)
            for f in fails:
                print("FAIL", f)
            print("validation: %d failure(s), %d warning(s)" % (len(fails), len(warns)))
            return 1 if fails else 0
        print(reveal_text(argv[1]))
        return 0
    except CF.PlanError as e:
        print("ab_report: %s" % e)
        return 65


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
