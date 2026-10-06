"""Slice S7, `browse_routes`: the library screens, at the keyboard of the
shipped app.

Critical-path inventory rows 42-47. First: row 44's sort persistence
(#758), where the unit test checks the page's widget tree and the fix-
coverage audit (~/Desktop/mpv-shim-3.1.0-fix-coverage.md) marked the drawn
drop-down WEAK. Measured on the real app: within a session a library comes
back showing its sort; after a relaunch the drop-down said "Name" while the
items were sorted by the stored sort.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
from test_playback_lifecycle import _PlaybackCase  # noqa: E402


class _BrowseCase(_PlaybackCase):

    def library(self, name):
        views = self.session._request("/Users/%s/Views"
                                      % self.session.user_id)["Items"]
        found = [v["Id"] for v in views if v.get("Name") == name]
        self.assertTrue(found, "no %r library" % name)
        return found[0]

    def open_library(self, view_id, landed="grid-sort"):
        """Home, then the library's tile in the Libraries row, by keys."""
        def tile(f):
            return next((n["id"] for n in f.get("nodes", [])
                         if (n.get("id") or "").startswith("row-libs-")
                         and n["id"].endswith(view_id)), None)
        self.app.move_to("nav-home")
        f = self.app.press_until("ENTER", tile, what="Home")
        # The row's first TILE: once it overflows, the row also carries its
        # paging arrows (row-libs-pl / -pr), which are not tiles.
        first = next(n["id"] for n in f["nodes"]
                     if (n.get("id") or "").startswith("row-libs-")
                     and len(n["id"]) == len("row-libs-") + 32)
        self.app.move_to(first)
        self.app.move_to(tile(self.app.frame()), key="RIGHT")
        return self.app.press_until("ENTER",
                                    lambda f: _app.shown(f, landed),
                                    what="the library")


class ALibraryRemembersItsSortTest(_BrowseCase):
    """Row 44 / #758: a sort picked by keys is what the drop-down shows on
    the next visit and after a relaunch -- the stored value is on the
    server (web's display preferences), and the screen must say what the
    query used."""

    LIBRARY = "Movies"

    def drawn_sort(self, settle=3.0):
        """The drop-down's selection as the renderer draws it, once the
        page has loaded (the stored sort arrives with the load)."""
        deadline = time.monotonic() + settle
        sel = None
        while time.monotonic() < deadline:
            sel = _flows.selected(self.app.frame(), "grid-sort")
            time.sleep(0.2)
        return sel

    def test_the_drop_down_shows_the_stored_sort(self):
        view = self.library(self.LIBRARY)
        self.open_library(view)
        start = self.drawn_sort()
        target = 2 if start != 2 else 3
        self.addCleanup(self.put_back, view, start or 0)
        _flows.pick(self.app, "grid-sort", target)
        self.assertEqual(target, self.drawn_sort(1.0))
        self.open_library(view)
        self.assertEqual(target, self.drawn_sort(),
                         "the next visit draws a different sort")
        self.app = _flows.relaunch(self.app, self.relay)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the relaunch")
        self.open_library(view)
        self.assertEqual(target, self.drawn_sort(),
                         "after a relaunch the drop-down draws another sort "
                         "than the one stored (and used)")
        self.put_back(view, start or 0)          # before quitting, not after
        self.assertEqual(0, self.app.quit(timeout=30))

    def put_back(self, view, index):
        """The sort is server state other runs read: restore it through
        the app the same way it was changed."""
        if not self.app.alive():
            return
        try:
            self.open_library(view)
            _flows.pick(self.app, "grid-sort", index)
            time.sleep(1)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
