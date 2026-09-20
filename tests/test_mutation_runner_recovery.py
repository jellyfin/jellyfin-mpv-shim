"""``tools/mutate_round.py`` must not leave a mutation in the working tree.

A round rewrites source files under you, one at a time, and restores each
from a byte copy taken up front. Two ways that goes wrong, both of which
happened on 2026-09-13 within an hour of each other:

* the round is killed between the write and the restore, so a deliberate
  defect stays in the tree looking exactly like code somebody wrote --
  it reached three test modules before the suite caught it;
* somebody edits one of the round's files while it holds them, and the
  restore for an unrelated mutation reverts the edit twenty minutes later.

The runner answers both with a journal under ``.git/mutate-round/``. This
pins the two decisions in it that can rot silently and that a run of the
tool would not notice: **which on-disk contents count as "the round put this
here"**, and **when a parallel split is refused**. The end-to-end behaviour
was verified by reproducing both failures; that needs killing processes
mid-suite and does not belong in the unit suite.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runner():
    """``tools/mutate_round.py`` as a module, without installing it.

    Loaded by path for the reason `test_parallel_runner` gives: putting
    ``tools/`` on sys.path would shadow the repo root, which is the hazard
    those runners' own docstrings are about.
    """
    import importlib.util

    path = os.path.join(ROOT, "tools", "mutate_round.py")
    spec = importlib.util.spec_from_file_location("_jms_mutate_round", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(text):
    r"""The hash of the bytes a fixture is expected to have PUT ON DISK.

    So every write here passes ``newline=""``: a text-mode write translates
    "\n" to "\r\n" on Windows, and the runner hashes the file in binary, so
    without it the fixture disagrees with itself and every restore reports a
    rescue. A no-op on Linux, which is why it took the Windows suite to see.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class RestoreKnowsItsOwnMutationTest(unittest.TestCase):
    """Restoring a killed round must tell the round's own mutation from an
    edit somebody else made, and only shout about the second.

    Both are "the file is not what we backed up". Treating them the same
    made every crash recovery report a rescue, and a warning that fires on
    the ordinary path is one you learn to scroll past -- which costs the
    case it exists for.
    """

    ORIGINAL = "original\n"
    MUTATED = "mutated\n"
    EDITED = "somebody was working here\n"

    def setUp(self):
        self.mod = _runner()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The runner resolves paths against ROOT and writes rescues under
        # STATE_DIR; point both at the sandbox so nothing touches the repo.
        self.mod.ROOT = self.tmp
        self.mod.STATE_DIR = os.path.join(self.tmp, "state")
        self.rel = "pkg/mod.py"
        self.full = os.path.join(self.tmp, self.rel)
        os.makedirs(os.path.dirname(self.full))
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        self.backup = os.path.join(backups, "pkg_mod.py")
        with open(self.backup, "w", encoding="utf-8", newline="") as fh:
            fh.write(self.ORIGINAL)

    def _state(self, applied_sha=None):
        return {"backup_dir": os.path.dirname(self.backup),
                "backups": {self.rel: self.backup},
                "expected": {self.rel: _sha(self.ORIGINAL)},
                "applied": ({"rel": self.rel, "name": "m",
                             "sha": applied_sha} if applied_sha else None)}

    def _write(self, text):
        with open(self.full, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def _restore(self, state):
        """Call the runner with its progress lines swallowed. It prints to
        stdout by design -- a round is watched live -- and eight copies of
        that in the suite output is noise nobody reads."""
        with contextlib.redirect_stdout(io.StringIO()):
            return self.mod.restore_from_state(state, "test")

    def _rescues(self):
        try:
            return os.listdir(os.path.join(self.mod.STATE_DIR, "rescued"))
        except OSError:
            return []

    def test_the_mutation_is_put_back_without_a_rescue(self):
        self._write(self.MUTATED)
        self._restore(self._state(_sha(self.MUTATED)))
        self.assertEqual(_read(self.full), self.ORIGINAL)
        self.assertEqual(self._rescues(), [],
                         "the round's own mutation was reported as an edit")

    def test_an_edit_made_under_the_round_is_rescued(self):
        self._write(self.EDITED)
        self._restore(self._state(_sha(self.MUTATED)))
        self.assertEqual(_read(self.full), self.ORIGINAL)
        rescued = self._rescues()
        self.assertEqual(len(rescued), 1, "the edit was thrown away")
        self.assertEqual(
            _read(os.path.join(self.mod.STATE_DIR, "rescued", rescued[0])),
            self.EDITED)

    def test_a_file_killed_before_its_write_is_not_a_rescue(self):
        """A round can die between journalling and writing, leaving the file
        untouched. That is the clean case and must be silent."""
        self._write(self.ORIGINAL)
        self._restore(self._state(_sha(self.MUTATED)))
        self.assertEqual(self._rescues(), [])

    def test_an_edit_to_a_file_no_mutation_was_holding_is_rescued_too(self):
        """The failure that actually happened, three times.

        The edit was to a file the round was not mutating *at that moment*,
        so the per-mutation restore never looked at it and the end-of-round
        loop -- a bare `shutil.copyfile` over every backup -- reverted it
        with no warning. Both restores go through this function now, which
        is why one test covers both.
        """
        self._write(self.EDITED)
        self._restore(self._state(applied_sha=None))
        self.assertEqual(len(self._rescues()), 1)
        self.assertEqual(_read(self.full), self.ORIGINAL)

    def test_restoring_one_file_leaves_the_others_alone(self):
        """`only=` is what the per-mutation restore passes. Without it that
        restore would put every file back after every mutation, undoing an
        edit the moment it was made rather than at the end -- faster, and
        still wrong."""
        other = "pkg/other.py"
        other_full = os.path.join(self.tmp, other)
        with open(other_full, "w", encoding="utf-8", newline="") as fh:
            fh.write(self.EDITED)
        other_backup = os.path.join(self.tmp, "backups", "pkg_other.py")
        with open(other_backup, "w", encoding="utf-8", newline="") as fh:
            fh.write(self.ORIGINAL)

        state = self._state(_sha(self.MUTATED))
        state["backups"][other] = other_backup
        state["expected"][other] = _sha(self.ORIGINAL)
        self._write(self.MUTATED)

        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.restore_from_state(state, "test", only=self.rel)

        self.assertEqual(_read(self.full), self.ORIGINAL)
        self.assertEqual(_read(other_full), self.EDITED,
                         "a restore scoped to one file touched another")
        self.assertEqual(self._rescues(), [])


class TheRoundItselfRescuesAnEditToAnyOfItsFilesTest(unittest.TestCase):
    """The wiring, which the helper's own tests cannot see.

    Reverting the `finally:` loop to a bare `shutil.copyfile` left every
    test in the class above green -- they call `restore_from_state`
    directly, so they prove the check exists and say nothing about whether
    the loop uses it. That is the "tests that cannot fail" shape, and it is
    the exact reason this defect survived being written *and* being
    documented as fixed.

    So this drives `run()` for real, with the suite stubbed out, and edits a
    file the round is holding but is **not currently mutating** -- which is
    the ordering that actually happened, three times.
    """

    def setUp(self):
        self.mod = _runner()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mod.ROOT = self.tmp
        self.mod.STATE_DIR = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.tmp, "pkg"))
        for name in ("a.py", "b.py"):
            with open(os.path.join(self.tmp, "pkg", name), "w",
                      encoding="utf-8") as fh:
                fh.write("original\n")
        self.b = os.path.join(self.tmp, "pkg", "b.py")

    def _run(self, mutations, on_suite):
        calls = []

        def fake_suite(_select):
            calls.append(len(calls))
            on_suite(len(calls))
            return True, "OK"       # everything survives; not the subject

        self.mod._run_suite = fake_suite
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.mod.run(mutations, ["-k", "nothing"])
        return out.getvalue()

    def test_an_edit_during_another_files_mutation_is_not_silently_reverted(self):
        mutations = [("first", "pkg/a.py", "original", "mutated"),
                     ("second", "pkg/b.py", "original", "mutated")]

        def edit_b_during_the_first_mutation(call):
            if call == 2:       # 1 is the baseline, 2 is mutation "first"
                with open(self.b, "w", encoding="utf-8", newline="") as fh:
                    fh.write("somebody was working here\n")

        printed = self._run(mutations, edit_b_during_the_first_mutation)

        self.assertEqual(_read(self.b), "original\n",
                         "the round is still expected to put its copy back")
        rescued = os.listdir(os.path.join(self.mod.STATE_DIR, "rescued"))
        self.assertEqual(len(rescued), 1,
                         "the edit was reverted with no rescue and no line")
        self.assertEqual(
            _read(os.path.join(self.mod.STATE_DIR, "rescued", rescued[0])),
            "somebody was working here\n")
        self.assertIn("pkg/b.py", printed, "and it said nothing about it")

    def test_a_later_mutation_still_applies_after_an_edit_to_its_file(self):
        """Mutating the *backup* rather than the file on disk.

        With an edit sitting in `b.py`, `old` no longer appears in it, so a
        disk-sourced `str.replace` is a no-op -- and the tool's own docstring
        says a mutation that changes nothing is indistinguishable from one
        that survived. So the round would report a false survivor for a
        guard that is perfectly fine, which is the direction that wastes a
        session chasing a defect that is not there.
        """
        seen = {}
        mutations = [("first", "pkg/a.py", "original", "mutated"),
                     ("second", "pkg/b.py", "original", "mutated")]

        def watch(call):
            if call == 2:
                with open(self.b, "w", encoding="utf-8", newline="") as fh:
                    fh.write("somebody was working here\n")
            if call == 3:       # the suite run for mutation "second"
                seen["b"] = _read(self.b)

        self._run(mutations, watch)
        self.assertEqual(seen.get("b"), "mutated\n",
                         "the second mutation was a silent no-op, which "
                         "reports as a survivor")

    def test_an_untouched_round_rescues_nothing(self):
        """The control, so "rescue" cannot become "rescue every file every
        time" -- which would bury the one that matters."""
        self._run([("first", "pkg/a.py", "original", "mutated")],
                  lambda call: None)
        self.assertFalse(os.path.exists(
            os.path.join(self.mod.STATE_DIR, "rescued")))


class TheJournalFollowsTheStateDirectoryTest(unittest.TestCase):
    """`STATE_DIR` and the journal path must not be able to disagree.

    They were two module constants, so pointing the first at a sandbox left
    the second addressing the real `.git/mutate-round/state.json` -- every
    test in this file was writing its journal into the repository's own,
    invisibly, for as long as that directory happened to exist. It became a
    crash the first time the directory was cleaned up, which is the good
    outcome; the bad one is a test round's journal being found and acted on
    by a real invocation.
    """

    def test_the_journal_lives_wherever_state_dir_points(self):
        mod = _runner()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mod.STATE_DIR = tmp
        self.assertEqual(os.path.dirname(mod._state_path()), tmp)

        mod.write_state({"backups": {}, "expected": {}, "applied": None})
        self.assertTrue(os.path.exists(os.path.join(tmp, "state.json")))
        self.assertIsNotNone(mod.read_state())


class ParallelSplitIsRefusedWhenItWouldChangeTheSetTest(unittest.TestCase):
    """`-k` matches class and method names as well as module names, so a
    module-level split is only equivalent when every pattern names a module.

    Silently running a different set of tests than the serial round is how a
    mutation gets called killed by tests that never touched it.
    """

    def setUp(self):
        self.mod = _runner()

    def test_module_patterns_split(self):
        got = self.mod._select_modules(
            ["-k", "test_parallel_runner", "-k", "test_no_tkinter"])
        self.assertEqual(got, ["test_no_tkinter.py",
                               "test_parallel_runner.py"])

    def test_a_pattern_naming_no_module_refuses(self):
        self.assertIsNone(self.mod._select_modules(
            ["-k", "test_parallel_runner", "-k", "SomeTestClass"]))

    def test_anything_that_is_not_a_k_pair_refuses(self):
        self.assertIsNone(self.mod._select_modules(
            ["-k", "test_parallel_runner", "-v"]))

    def test_an_empty_select_refuses(self):
        self.assertIsNone(self.mod._select_modules([]))


if __name__ == "__main__":
    unittest.main()
