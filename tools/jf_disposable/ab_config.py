"""Blinded A/B plan for the renderer-cadence comparison (30 ms against 16.7 ms).

Stdlib only; nothing here starts anything. A *session* is a directory holding

  plan.json      public: the runs in order, the phases, their state. Names no tick and no arm.
  .key.json      sealed (mode 0600): which arm each run used and which tick each arm is.
                 Nothing prints it until ``reveal``, and ``reveal`` refuses while any run has
                 not been started or any finished run has no recorded rating, so the
                 impressions are written down first.
  ratings.json   the operator's ratings per run, written by ``record_rating``.
  runs/<run>/    one directory per run (written by ab_session.py).

The blinding keeps the arm out of every file and message the operator is shown while testing. It
does not stop someone who opens .key.json, and the frame statistics themselves can hint at an
arm to a reader; they are meant to be read after ``reveal``, not during the test.

Both arms always set JMS_TICK_MS (30 or 16.7), so they take the same code path and differ in
one number. The renderer honours it only with JMS_FRAME_DIAG, which the session always sets.
"""
import json
import os
import random
import re

import scroll_config

ARM_TICKS = (30.0, 16.7)       # ms; which arm letter gets which is drawn per session
RUN_RE = re.compile(r"^run-\d{2}$")

# name, how to do it, seconds (pilot, full), hint for what it measures
PHASES = (
    ("cold_wheel", "MOUSE WHEEL. At the top of the Movies grid, scroll steadily DOWN to the very bottom "
                   "and back UP, at an ordinary reading pace. The posters are still arriving.", 20, 30),
    ("warm_fling", "MOUSE WHEEL. Fast flings, down through the grid and back up, as hard as you "
                   "normally scroll. Posters are cached now.", 10, 20),
    ("warm_nudge", "MOUSE WHEEL. Slow single notches, about one a second, down and then up.", 10, 20),
    ("touchpad", "TOUCHPAD (no mouse). Two-finger drag slowly, then flick and let it glide out, "
                 "down and up. Type s + Enter at the prompt to skip if you have no touchpad.", 15, 30),
    ("idle", "HANDS OFF the mouse and touchpad. Leave the window as it is.", 10, 20),
)
MODES = ("pilot", "full")


class PlanError(ValueError):
    pass


def phases_for(mode):
    if mode not in MODES:
        raise PlanError("mode must be one of %s" % (MODES,))
    i = 2 if mode == "pilot" else 3
    return [{"name": p[0], "text": p[1], "seconds": p[i]} for p in PHASES]


def _path(session, name):
    return os.path.join(session, name)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(path, obj, mode=0o644):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def arm_order(mode, rng):
    """Pilot: one run of each arm, in a drawn order. Full: A-B-B-A or B-A-A-B, drawn."""
    if mode == "pilot":
        return rng.sample(["A", "B"], 2)
    return rng.choice((["A", "B", "B", "A"], ["B", "A", "A", "B"]))


def new_session(session, mode="pilot", rng=None):
    """Create the session directory with a public plan and a sealed key. Refuses an existing one."""
    rng = rng or random.SystemRandom()
    if os.path.exists(session):
        raise PlanError("%s already exists; a session is never reused" % session)
    ticks = list(ARM_TICKS)
    rng.shuffle(ticks)
    key = {"arms": {"A": ticks[0], "B": ticks[1]}, "order": arm_order(mode, rng)}
    for t in key["arms"].values():
        scroll_config.parse_tick_ms("%g" % t)           # the renderer's own range check
    os.makedirs(os.path.join(session, "runs"))
    plan = {"mode": mode, "phases": phases_for(mode),
            "runs": [{"run": "run-%02d" % (i + 1)} for i in range(len(key["order"]))]}
    _dump(_path(session, ".key.json"), key, 0o600)
    _dump(_path(session, "plan.json"), plan)
    _dump(_path(session, "ratings.json"), {})
    return plan


def load_plan(session):
    try:
        return _load(_path(session, "plan.json"))
    except OSError:
        raise PlanError("no plan.json in %s (create one with the plan command)" % session)


def run_state(session, run):
    """'pending', 'complete' or 'failed' from the run's own record."""
    try:
        return _load(os.path.join(session, "runs", run, "run.json")).get("status", "failed")
    except (OSError, ValueError):
        return "failed" if os.path.isdir(os.path.join(session, "runs", run)) else "pending"


def next_run(session):
    """The first run with no record at all, or None. A failed run is not retried silently."""
    for r in load_plan(session)["runs"]:
        if run_state(session, r["run"]) == "pending":
            return r["run"]
    return None


def tick_for_run(session, run):
    """The tick (ms) the run must use. For ab_session.py only; never print the result."""
    if not RUN_RE.match(run or ""):
        raise PlanError("bad run name %r" % run)
    names = [r["run"] for r in load_plan(session)["runs"]]
    if run not in names:
        raise PlanError("%s is not in the plan" % run)
    key = _load(_path(session, ".key.json"))
    return key["arms"][key["order"][names.index(run)]]


def record_rating(session, run, smoothness, responsiveness, guess="?", notes=""):
    """Store the operator's impressions of a run (1..5 each). guess is '30', '16.7' or '?'."""
    if not RUN_RE.match(run or ""):
        raise PlanError("bad run name %r" % run)
    for what, v in (("smoothness", smoothness), ("responsiveness", responsiveness)):
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 5:
            raise PlanError("%s must be a whole number from 1 to 5" % what)
    if guess not in ("30", "16.7", "?"):
        raise PlanError("guess must be 30, 16.7 or ?")
    r = _load(_path(session, "ratings.json"))
    if run in r:
        raise PlanError("%s already has a rating; ratings are not rewritten" % run)
    r[run] = {"smoothness": smoothness, "responsiveness": responsiveness, "guess": guess,
              "notes": str(notes)[:500]}
    _dump(_path(session, "ratings.json"), r)


def reveal(session):
    """Run -> arm, tick and rating, once every finished run has a rating."""
    plan = load_plan(session)
    ratings = _load(_path(session, "ratings.json"))
    key = _load(_path(session, ".key.json"))
    pending = [r["run"] for r in plan["runs"] if run_state(session, r["run"]) == "pending"]
    if pending:
        raise PlanError("not revealing: %s not run yet (an abandoned session is simply deleted)"
                        % ", ".join(pending))
    missing = [r["run"] for r in plan["runs"]
               if run_state(session, r["run"]) == "complete" and r["run"] not in ratings]
    if missing:
        raise PlanError("not revealing: no rating recorded for %s" % ", ".join(missing))
    out = []
    for r, arm in zip(plan["runs"], key["order"]):
        out.append({"run": r["run"], "state": run_state(session, r["run"]), "arm": arm,
                    "tick_ms": key["arms"][arm], "rating": ratings.get(r["run"])})
    return out
