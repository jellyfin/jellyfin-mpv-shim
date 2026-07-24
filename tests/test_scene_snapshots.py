"""Characterization tests: every screen's rendered scene, pinned exactly.

These assert nothing about whether the UI is *correct* — they assert it has
not *changed*. That is the property a refactor needs and no other test in the
suite provides: the behavioural tests check the things someone thought to
check, and a decomposition can preserve all of them while quietly moving a
row, dropping a badge or re-ordering a column.

Because ``layout()`` produces the exact node list pushed to the renderer, a
matching snapshot means matching pixels.

**When one of these fails during a refactor**, that is the tool working. Read
the diff; if the change is intended, regenerate:

    python3 tests/test_scene_snapshots.py --update

and review the resulting diff in the commit like any other. If it is not
intended, the refactor broke something the behavioural tests missed — which is
the entire reason this file exists.

Deliberately small: a handful of representative screens, not every route.
Snapshots are a blunt instrument and a large set of them becomes noise that
people regenerate reflexively instead of reading.
"""

import os
import sys
import unittest

# Importable as `python3 tests/test_scene_snapshots.py --update` as well as
# through discovery, so the regenerate path does not need a PYTHONPATH dance.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ARGS = sys.argv[1:]
sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from tests._scene_snapshot import dumps, load, snapshot, store  # noqa: E402
from tests.test_mpvtk_browser_shell import FakeSource  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402


# name -> route. Chosen to cover the distinct layout shapes rather than every
# kind: a carousel home, a virtualized grid, a text-heavy detail pane, a
# results list and the densest screen in the app.
SCREENS = {
    "home": {"kind": "home", "server": "s1"},
    "grid": {"kind": "grid", "parent_id": "lib1", "server": "s1",
             "title": "Movies"},
    "search": {"kind": "search", "server": "s1", "term": "a"},
    "settings": {"kind": "settings", "server": "s1"},
}


def build_browser():
    return MpvtkBrowser(app=None, source=FakeSource())


class TestSceneSnapshots(unittest.TestCase):
    maxDiff = None

    def _check(self, name):
        expected = load(name)
        actual = snapshot(build_browser(), SCREENS[name])
        if expected == actual:
            return
        # assertMultiLineEqual gives a line-by-line diff, which for one node
        # per line means "this node changed" rather than a wall of JSON.
        self.assertMultiLineEqual(
            dumps(expected), dumps(actual),
            "%s rendered differently. If this change is intended, run "
            "`python3 tests/test_scene_snapshots.py --update` and review "
            "the regenerated snapshot in your commit." % name)

    def test_home(self):
        self._check("home")

    def test_grid(self):
        self._check("grid")

    def test_search(self):
        self._check("search")

    def test_settings(self):
        self._check("settings")

    def test_every_screen_has_a_snapshot(self):
        # Adding a SCREENS entry without generating its baseline would
        # otherwise be a silently absent test.
        for name in SCREENS:
            with self.subTest(name):
                load(name)

    def test_snapshots_are_not_trivially_small(self):
        # A screen that renders as a bare spinner produces a handful of nodes
        # and would pass forever without asserting anything. This is the guard
        # against a snapshot that was captured before its data settled.
        for name in SCREENS:
            with self.subTest(name):
                self.assertGreater(
                    len(load(name)), 20,
                    "%s's snapshot looks like an unsettled screen" % name)


def _update():
    for name, route in SCREENS.items():
        store(name, snapshot(build_browser(), route))
        print("wrote snapshot: %s" % name)


if __name__ == "__main__":
    if "--update" in _ARGS:
        _update()
    else:
        unittest.main()
