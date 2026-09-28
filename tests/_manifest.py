"""What a release run must have run, and the check that it did.

A manifest is one file per runner and platform
(``tests/manifests/<runner>-<platform>.tsv``). Each line is one test in one
leg, tab-separated::

    <leg>  <test id>  pass|skip  <reason>

``pass`` means the test must run and pass. ``skip`` is an *approved
exclusion*: the test may skip on this platform, and the reason must say why
and which hand check stands in for it (e.g. ``H8: custom OSC fixture absent``).
A skip written by ``--update-manifest`` has the reason ``UNAPPROVED: <the
test's own skip message>``, and the check fails until a person replaces it.

The check exists because ``OK (skipped=K)`` cannot say which tests skipped,
and ``--strict`` only caught a leg where every test skipped. See
docs/testing.md.
"""

import json
import os
import sys

UNAPPROVED = "UNAPPROVED"
PLATFORM = "windows" if sys.platform.startswith("win") else "linux"
MANIFEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "manifests")


def manifest_path(runner, platform=PLATFORM):
    return os.path.join(MANIFEST_DIR, "%s-%s.tsv" % (runner, platform))


def load(path):
    """{(leg, test id): (expected, reason)}. Missing file = empty manifest."""
    entries = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 4 or parts[2] not in ("pass", "skip"):
                    raise ValueError("%s:%d: malformed manifest line: %r"
                                     % (path, n, line))
                entries[(parts[0], parts[1])] = (parts[2], parts[3])
    except FileNotFoundError:
        pass
    return entries


def read_outcomes(path):
    """[{"id", "outcome", "reason"}] from a tests._outcomes file."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out


def skip_lines(legs):
    """One line per skipped test, with the reason it gave: a conditional
    test that skips (no second server, no second volume, no CJK face) says
    why in the summary rather than only adding to a count."""
    lines = []
    for leg, records in sorted(legs.items()):
        for rec in records:
            if rec.get("outcome") == "skip":
                reason = " ".join(str(rec.get("reason", "")).split())
                lines.append("  SKIPPED %s: %s -- %s"
                             % (leg, rec.get("id", "?"), reason or "(no reason)"))
    return lines


def check(manifest, legs):
    """Problems, as a list of strings; empty means the run matches.

    ``legs`` is {leg: [outcome records]} for every leg that ran. Legs the run
    did not attempt (e.g. ``--backend libmpv`` skipped the jsonipc legs) are
    not checked: the manifest says what a leg must contain, not which legs
    one invocation chose to run.
    """
    problems = []
    for leg, records in sorted(legs.items()):
        expected = {tid: exp for (lg, tid), exp in manifest.items()
                    if lg == leg}
        if not records:
            problems.append("%s: reported no outcomes at all" % leg)
            continue
        seen = {}
        for rec in records:
            seen.setdefault(rec["id"], rec)
        for tid, rec in sorted(seen.items()):
            outcome = rec["outcome"]
            if tid not in expected:
                problems.append("%s: %s is not in the manifest (%s); run "
                                "--update-manifest and review the diff"
                                % (leg, tid, outcome))
                continue
            want, reason = expected[tid]
            if outcome == "skip":
                if want != "skip":
                    problems.append("%s: %s skipped unexpectedly: %s"
                                    % (leg, tid, rec.get("reason", "")))
                elif reason.startswith(UNAPPROVED):
                    problems.append("%s: %s skips with no approved reason "
                                    "(edit the manifest line)" % (leg, tid))
            elif outcome not in ("pass", "xfail"):
                # Failures are already a failed leg; listed so the summary
                # names them alongside everything else that is wrong.
                problems.append("%s: %s %s" % (leg, tid, outcome.upper()))
        for tid in sorted(set(expected) - set(seen)):
            problems.append("%s: %s never reported (not collected, or its "
                            "class/module setup failed)" % (leg, tid))
    return problems


def update(manifest, legs):
    """The manifest rewritten from this run's outcomes, for the legs it ran.

    Keeps a reviewed skip reason when the test still skips; everything else
    is taken from the run. Legs not run are kept as they were.
    """
    ran = set(legs)
    new = {k: v for k, v in manifest.items() if k[0] not in ran}
    for leg, records in legs.items():
        for rec in records:
            key = (leg, rec["id"])
            if rec["outcome"] == "skip":
                old = manifest.get(key)
                if old and old[0] == "skip" and not old[1].startswith(
                        UNAPPROVED):
                    new[key] = old
                else:
                    msg = " ".join(str(rec.get("reason", "")).split())
                    new[key] = ("skip", "%s: %s" % (UNAPPROVED, msg))
            elif rec["outcome"] in ("pass", "xfail"):
                new[key] = ("pass", "")
    return new


def write(path, manifest):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# leg\ttest id\tpass|skip\treason -- see tests/_manifest.py"
                 "\n")
        for (leg, tid), (want, reason) in sorted(manifest.items()):
            fh.write("%s\t%s\t%s\t%s\n" % (leg, tid, want, reason))
