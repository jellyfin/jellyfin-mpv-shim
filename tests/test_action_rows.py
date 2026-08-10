"""Every button in an action row is the same size.

`components/controls.action_btn` exists so that one function decides
button styling -- its docstring has said so for a long time: "every
button in an action row must come from here, icon or not: the plain
Button widget defaults to a 20px label against this one's 16, which made
the odd trailing button ~5px taller than its neighbours."

It has been broken twice since, both times by *appending* to a row
someone else built:

- the grid bar mixed `action_btn` with plain `Button` (40.0 against
  41.2), invisible until the filter panel moved Play All and Shuffle next
  to the Filter button -- across the bar from each other there was
  nothing to be uneven with;
- the AudioBook page built a row of `PRIMARY_ROW` buttons and appended
  `download_button`, which took the default (40.0 against 42.5).

Both were found by measuring, neither by a test, and the rule was stated
in a docstring each time. So it is measured here instead: render the
pages that carry an action row and assert each row is uniform. A rule
about a ROW cannot be enforced one button at a time.
"""

import collections
import sys
import unittest

# Importing the shim reaches `conffile.confdir` -> `args.get_args()`,
# which parses the REAL argv at import time -- so a `-k` meant for
# unittest lands in the app's parser and the module fails to import.
# Discovery imports alphabetically and this file sorts near the top, so
# it cannot rely on another module having done this first.
sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeSource, _SyncPool, build_scene)

#: Routes that draw an action row, and the id prefix its buttons share.
#: A page whose buttons do not share a prefix is not skipped -- it is
#: grouped by y instead, below.
SCREENS = [
    ("detail", {"kind": "detail", "item_id": "m1", "server": "srv1"}),
    ("series", {"kind": "series", "item_id": "sh1", "server": "srv1"}),
    ("season", {"kind": "season", "item_id": "se1", "series_id": "sh1",
                "server": "srv1"}),
    ("grid", {"kind": "grid", "server": "srv1", "parent_id": "lib1",
              "collection_type": "movies", "title": "Movies"}),
    ("audiobook", {"kind": "audiobook", "item_id": "ab1", "server": "srv1"}),
]

#: Node ids that sit ON an action row's baseline without being one of its
#: buttons -- a dropdown is a different control with its own height, and
#: the grid's sort picker is deliberately one (see UI_FIXES_4 §33).
NOT_A_BUTTON = ("grid-sort",)


def _rows(nodes):
    """Clickable button rects grouped by vertical CENTRE.

    Not by top edge. An action row is `align="center"`, so the odd-sized
    button -- the one this test exists to find -- is the one whose top
    edge differs, and grouping by `y` put it in a bucket of its own where
    nothing compared it to anything. The first version of this test did
    exactly that and passed against a reintroduced regression. Buttons on
    one row share a centre whatever their height.
    """
    by_mid = collections.defaultdict(list)
    for n in nodes:
        nid = n.get("id") or ""
        if (n.get("t") == "rect" and n.get("click") and n.get("radius")
                and nid and not nid.startswith("r.")
                and nid not in NOT_A_BUTTON):
            by_mid[round(n["y"] + n["h"] / 2.0)].append((nid, n["h"]))
    return {mid: row for mid, row in by_mid.items() if len(row) > 1}


class ActionRowTest(unittest.TestCase):
    def _scene(self, route):
        # Built the way tests/_scene_snapshot.py does -- no controller,
        # and the route pushed onto nav_stack rather than navigated to.
        # `navigate` on a detail route resolves the theme, which reaches
        # conffile -> args.get_args() and parses the real argv.
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.nav_stack = [dict(route)]
        b._load_route(b.route)
        nodes, _h = build_scene(b, size=(1280, 720))
        return nodes

    def test_every_action_row_has_one_button_height(self):
        for name, route in SCREENS:
            nodes = self._scene(route)
            rows = _rows(nodes)
            self.assertTrue(rows, "%s drew no row of buttons to check"
                                  % name)
            for y, row in sorted(rows.items()):
                heights = {h for _id, h in row}
                with self.subTest(screen=name, y=y):
                    self.assertEqual(
                        len(heights), 1,
                        "%s has a row at y=%s with mixed button heights: %r"
                        % (name, y, sorted(row)))

    def test_the_check_can_see_a_mixed_row(self):
        """Guard on the guard.

        `_rows` drops single-button rows and non-buttons, so a filter
        that was slightly too strict would quietly find nothing to check
        and pass over any tree -- which is how the two real defects
        survived their own tests.
        """
        # Centred, so the shorter one starts LOWER -- which is what a
        # by-top-edge grouping missed.
        fake = [
            {"id": "a", "t": "rect", "click": True, "radius": 6,
             "y": 10.0, "h": 42.5},
            {"id": "b", "t": "rect", "click": True, "radius": 6,
             "y": 11.25, "h": 40.0},
        ]
        rows = _rows(fake)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len({h for _i, h in list(rows.values())[0]}), 2)
