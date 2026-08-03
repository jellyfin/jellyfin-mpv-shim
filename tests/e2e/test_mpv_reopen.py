"""Closing mpv mid-playback, then playing again.

`docs/REGRESSION_CHECKLIST_2026-07-25.md` calls this "the historic stale-queue
bug; the highest-value single item on this page", and it is the flagship of
the tracker's largest cluster (#458, 18 comments).

The mechanism, from the integration suite's write-up: when mpv goes away, its
dying observers have already queued `_handle_mpv_shutdown` — and that task
nulls `self._video`. Run against the *new* session after a re-open, the
re-opened player's `eof-reached` then sees no video and does nothing, so
**auto-advance silently stops**. Nothing crashes; the queue just never moves
again, which is why it took eighteen comments to pin down. So the assertion
that matters is not "it re-opened" but "the episode after the one you
restarted actually plays" — which needs a genuine EOF, which needs real media.

Everything here runs **out of process** (`_close_child.py`). One of the
outcomes under test is a segfault, and a child is what turns that into a
readable failure instead of a lost run. See `_close_child` for why.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "_close_child.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def run_child(mode, timeout=180):
    env = dict(os.environ)
    env["JMS_TEST_BACKEND"] = _e2e.BACKEND
    proc = subprocess.run(
        [sys.executable, CHILD, mode], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=timeout)
    result = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT:"):
            result = line
    return proc, result


class _ChildCase(_e2e.E2ETestCase):
    """Drives the child; never builds a player of its own."""

    @classmethod
    def setUpClass(cls):
        pass            # no in-process player: the child owns one

    def setUp(self):
        pass            # and no session either

    def check(self, mode, timeout=180):
        proc, result = run_child(mode, timeout=timeout)
        if proc.returncode < 0:
            self.fail(
                "the child died on signal %d (segfault = a use-after-free on "
                "the mpv handle).\nstdout:\n%s\nstderr tail:\n%s" % (
                    -proc.returncode, proc.stdout,
                    "\n".join((proc.stderr or "").splitlines()[-25:])))
        self.assertTrue(result, "child printed no RESULT line.\nstdout:\n%s\n"
                                "stderr tail:\n%s" % (
                                    proc.stdout,
                                    "\n".join((proc.stderr or "")
                                              .splitlines()[-25:])))
        return proc, result


@_e2e.require_server_and_mpv
class MpvReopenTest(_ChildCase):

    def test_closing_mpv_then_playing_again_still_auto_advances(self):
        proc, result = self.check("reopen")
        self.assertEqual(proc.returncode, 0, result)


@_e2e.require_server_and_mpv
class AbandonedItemTest(_ChildCase):
    """The #458 caveat, recorded in `docs/ISSUES_TO_VERIFY.md`:

        a later comment describes a residual X-button freeze that also
        *marks the item watched* via `get_timeline_options`. Verify that
        specific path before closing.

    It was never verified, so these two tests do it. They differ only in the
    length of the item, and that turns out to be the whole story.
    """

    def test_a_long_item_abandoned_early_is_not_marked_watched(self):
        """Three hours, abandoned at ~3 seconds. The unambiguous case."""
        proc, result = self.check("abandon-long")
        self.assertEqual(proc.returncode, 0, result)


@_e2e.require_server_and_mpv
class AbortReportedPositionTest(_ChildCase):
    """An abort must be reported where it aborted, whatever the runtime.

    These two are the same action against two items, and they disagree — which
    is the diagnosis. `_finished_at_eof` (player.py) decides whether the end
    that just happened was genuine:

        return position >= duration * 0.95 or duration - position <= 10

    The second clause is an *absolute* ten-second margin, there to absorb the
    timeline tick interval and metadata duration drift. It has no lower bound
    relative to the runtime, so for anything shorter than ten seconds it is
    true at **every** position including zero, and for anything under about
    twenty it is true across most of the file. The abort is then reported at
    full duration and the server records the item as watched.

    Measured against this server: a 10.0s episode aborted at 2.58s was
    reported with `session_stop` at 10.02s and came back `Played=True`; the
    same on the 3h item reported 0.33s and came back `Played=False`. Both
    backends.

    Driven through `send_timeline_stopped(finished=True)` rather than through
    a window close: the close is the realistic trigger, but it is a race
    between `finished_callback` and the shutdown teardown and either can win,
    so a test built on it passes about a third of the time. This is the same
    seam without the coin toss.
    """

    def test_a_long_item_aborted_early_is_not_marked_watched(self):
        proc, result = self.check("abort-report-long")
        self.assertEqual(proc.returncode, 0, result)

    @unittest.expectedFailure
    def test_a_short_item_aborted_early_is_not_marked_watched(self):
        """**Currently fails** — pinned, not skipped, so it becomes a hard
        failure the moment the margin is bounded. Same instrument the
        integration suite uses for `_rearm_sync`.

        The arithmetic itself would be cheaper to pin as a unit test on
        `_finished_at_eof`; this exists to show the server-visible
        consequence, which is an item you watched three seconds of appearing
        as watched."""
        proc, result = self.check("abort-report-short")
        self.assertEqual(proc.returncode, 0, result)


if __name__ == "__main__":
    unittest.main()
