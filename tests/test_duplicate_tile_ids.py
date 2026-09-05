"""A row may hold the same item twice, and the ids have to survive it (#20).

Region ids are `"<prefix>-<item id>"`, and `layout` warns when two nodes
share one: *"renderer state and events will target only the last
occurrence"*. So the duplicate ids in the log were the symptom; the bug is
that hovering or clicking the FIRST of the pair drives the second.

[iw] saw it on Cast & Crew, where a person credited as both Actor and
Director is two credits and two tiles — confirmed against a real server.
But it is not people-specific: a playlist can hold the same track twice,
and so can a queue.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource                      # noqa: E402

from jellyfin_mpv_shim.mpvtk.layout import layout                # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402


def _browser():
    return MpvtkBrowser(app=None, source=FakeSource())


def _row(items, prefix="cast"):
    b = _browser()
    node = b.tiles.tile_row("Cast & Crew", items, prefix)
    nodes, _h = layout(node, 1280, 720)
    # `prefix + "-"`, not `prefix`: the row's own scroll container is
    # id'd with the bare prefix and is not a tile.
    return [n["id"] for n in nodes
            if str(n.get("id", "")).startswith(prefix + "-")]


def _person(pid, role):
    return {"Id": pid, "Name": "A Person", "Type": "Person",
            "_subtitle": role}


class DuplicateIdTest(unittest.TestCase):
    def test_one_person_credited_twice_gets_two_ids(self):
        ids = _row([_person("p1", "Supporting"), _person("p1", "Director"),
                    _person("p2", "Writer")])
        self.assertEqual(len(ids), len(set(ids)),
                         "two tiles share an id, so the renderer routes "
                         "both to the last one: %s" % ids)

    def test_a_row_with_no_repeats_keeps_its_plain_ids(self):
        """Suffixing everything would rename every hit region in the app
        for a case that almost never happens. Only the broken rows change."""
        ids = _row([_person("p1", "Actor"), _person("p2", "Director")])
        self.assertEqual(sorted(ids), ["cast-p1", "cast-p2"])

    def test_a_playlist_holding_one_track_twice(self):
        # Not people-specific, which is why the fix is in tile_row rather
        # than in the cast list.
        items = [{"Id": "t1", "Name": "Song", "Type": "Audio"},
                 {"Id": "t1", "Name": "Song", "Type": "Audio"}]
        ids = _row(items, prefix="pl")
        self.assertEqual(len(ids), len(set(ids)))

    def test_three_of_the_same_item(self):
        ids = _row([_person("p1", "A"), _person("p1", "B"),
                    _person("p1", "C")])
        self.assertEqual(len(set(ids)), 3)

    def test_the_layout_warning_is_not_raised(self):
        """The check that would have caught this all along, asserted rather
        than logged."""
        import logging

        items = [_person("p1", "Actor"), _person("p1", "Director")]
        b = _browser()
        node = b.tiles.tile_row("Cast & Crew", items, "cast")
        with self.assertLogs("mpvtk", level=logging.WARNING) as caught:
            logging.getLogger("mpvtk").warning("sentinel")
            layout(node, 1280, 720)
        self.assertEqual(
            [m for m in caught.output if "duplicate node id" in m], [],
            "layout still reports duplicate ids")


if __name__ == "__main__":
    unittest.main()
