"""One guided session that records REAL mouse-wheel and touchpad input and what the renderer drew for it.

The start, the throw-away server, the 120-poster library, the single window and the 3 GiB scope are the same as
ab_session.py (this reuses its checks); what differs is the question. There is no blinding and no rating here:
the app runs with the scroll recorder on (JMS_FRAME_DIAG + JMS_SCROLL_DIAG, via si_launch.sh) and the phases are
cut into the recorder's own file with P lines, so si_analyze.py can answer
  * what scales do a mouse wheel and a touchpad actually deliver, and does a touchpad frame ever reach 0.99;
  * how does the DRAWN offset follow the stored one, frame by frame, for slow and fast wheel scrolling,
    reversals, the ends of the grid, and slow and fast touchpad gestures.

Nothing is sent to the app: no synthetic input, no IPC. Run it only through si_manual.sh.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import ab_session as AB                                       # noqa: E402
import si_analyze                                             # noqa: E402

Abort, say, ask, utc = AB.Abort, AB.say, AB.ask, AB.utc
FLUSH_WAIT_S = 2.5     # the recorder writes a second after the last event; wait past that before a marker

PHASES = (
    ("wheel_slow", 20, "MOUSE WHEEL, slow and steady: about 3 to 6 notches a second. Scroll DOWN through the "
                       "grid, then back UP. Count roughly 'one-two-three' per second."),
    ("wheel_fast", 10, "MOUSE WHEEL, fast: flings of many notches, down through the grid and back up, as hard "
                       "as you normally scroll."),
    ("wheel_reversal", 15, "MOUSE WHEEL, reversals: 3 or 4 notches DOWN, then straight away 3 or 4 notches UP, "
                           "and repeat, about 4 notches a second."),
    ("wheel_edges", 20, "MOUSE WHEEL, the ends: at the very TOP scroll up 4 more notches (nothing should move); "
                        "then fling to the very BOTTOM and keep going 6 more notches; then fling back up to "
                        "the top and 4 more."),
    ("pad_slow", 15, "TOUCHPAD (no mouse): a slow two-finger drag, down then up, finger speed like reading."),
    ("pad_fast", 15, "TOUCHPAD (no mouse): fast swipes and hard flicks, down and up, as fast as you can make "
                     "the pad report. Include a few very quick short flicks."),
)


def mark(log, name, kind):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("P\t%s\t%s\t%s\n" % (name, kind, utc()))


def run_phases(log, alive):
    done = {}
    for name, secs, text in PHASES:
        say("\n--- %s (%d s) ---\n%s" % (name, secs, text))
        ans = ask("Press Enter when ready to START%s: " % (" (s = skip, no touchpad)" if name.startswith("pad") else ""))
        if name.startswith("pad") and ans.strip().lower() == "s":
            done[name] = "skipped"
            continue
        say("recording starts in %d s: put the pointer over the JellyHome window now" % AB.LEAD_S)
        for _ in range(AB.LEAD_S * 5):
            if not alive():
                raise Abort("the app window closed")
            time.sleep(0.2)
        mark(log, name, "start")
        t0 = time.monotonic()
        say("GO  \a(%d s)" % secs)
        while time.monotonic() - t0 < secs:
            if not alive():
                raise Abort("the app window closed during %s" % name)
            time.sleep(0.2)
        say("STOP \a  (hands off for a moment)")
        time.sleep(FLUSH_WAIT_S)
        mark(log, name, "end")
        done[name] = "recorded"
    return done


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="a new directory for this session's files")
    ap.add_argument("--dry-run", action="store_true", help="check everything, start nothing")
    a = ap.parse_args(argv)
    work = os.environ.get("JH_JF_WORK", os.path.join(HERE, "work"))
    py = os.environ.get("JH_PY", os.path.join(REPO, "dist", "local-test", "venv", "bin", "python"))
    launcher = os.environ.get("JH_SI_REAL_APP", os.path.join(HERE, "ab_app.sh"))
    bad = AB.preflight(work, py, need_scope=not a.dry_run)
    if not os.access(launcher, os.X_OK):
        bad.append("no launcher at %s (set JH_SI_REAL_APP)" % launcher)
    if os.path.exists(a.out):
        bad.append("%s already exists; a session directory is never reused" % a.out)
    if bad:
        say("si_session: cannot run:\n  " + "\n  ".join(bad))
        return 65
    say("phases: " + ", ".join("%s %ds" % (n, s) for n, s, _t in PHASES))
    if a.dry_run:
        say("(dry run: nothing started)")
        return 0

    os.makedirs(a.out)
    log = os.path.join(a.out, "scroll_diag.tsv")
    flag = os.path.join(a.out, "stop.flag")
    rec = {"status": "failed", "started_utc": utc()}
    jf = None
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JMS_", "JH_AB_", "JH_SI_"))}
        env.update({"JH_JF_WORK": work, "JH_JF_MODE": "scroll", "JH_JF_FLAG": flag, "JH_JF_EXPECT": "120",
                    "JH_JF_MEDIA": os.path.join(work, "media", "MoviesGrid"),
                    "JH_JF_APP": os.path.join(HERE, "si_launch.sh"), "JH_SI_REAL_APP": launcher,
                    "JH_SI_LOG": log, "PYTHONUNBUFFERED": "1"})
        out = open(os.path.join(a.out, "jf_run.out"), "w", encoding="utf-8")
        jf = subprocess.Popen([py, os.path.join(HERE, "jf_run.py"), "full"], env=env, stdout=out,
                              stderr=subprocess.STDOUT, start_new_session=True)
        alive = lambda: jf.poll() is None                      # noqa: E731
        say("starting the throw-away server and the app window (about a minute)...")

        def ready():
            with open(os.path.join(a.out, "jf_run.out"), encoding="utf-8", errors="replace") as fh:
                return "READY: window is up" in fh.read()
        AB.wait_for(ready, "the app window", 300, 1.0, alive)
        AB.wait_for(lambda: os.path.exists(log) and os.path.getsize(log) > 0, "the scroll recorder's file",
                    30, 0.3, alive)
        say("\nThe window is up on your desktop. In it, open the Movies library and wait for the grid "
            "(do not scroll yet).")
        ask("Press Enter when the Movies grid is showing: ")
        rec["phases"] = run_phases(log, alive)
        rec["status"] = "complete"
    except (Abort, KeyboardInterrupt) as e:
        rec["error"] = str(e) or "interrupted"
        say("si_session: %s" % rec["error"])
    except Exception as e:                                      # noqa: BLE001
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        say("si_session: %s" % rec["error"])
    finally:
        if jf is not None:
            open(flag, "w").close()
            try:
                jf.wait(90)
            except subprocess.TimeoutExpired:
                os.killpg(jf.pid, signal.SIGTERM)
                try:
                    jf.wait(20)
                except subprocess.TimeoutExpired:
                    os.killpg(jf.pid, signal.SIGKILL)
        try:
            shutil.copy2(os.path.join(work, "out", "client.log.raw"), os.path.join(a.out, "app_stdout.log"))
        except OSError:
            pass
        rec["finished_utc"] = utc()
        with open(os.path.join(a.out, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1, sort_keys=True)
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = si_analyze.render(si_analyze.analyze(fh.read()))
        with open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        say("\n" + text)
    say("\nsession %s. Files in %s" % (rec["status"], a.out))
    return 0 if rec["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
