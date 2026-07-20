"""The playlist editor's batch-move algorithm.

The editor reorders a local list and emits (entry_id, absolute_index) moves;
the server applies them SEQUENTIALLY, each one a remove-then-insert. So the
invariant is not "the moves look right" — it is that replaying them in
emission order against the ORIGINAL server order reproduces exactly what the
editor is showing. Anything else and the user's screen and the server
disagree, silently, until the next reload.

Ported from the Tk editor when that browser was removed: same emitted
shape, same invariant, driven through the in-window editor's _pe_move.
"""

import random
import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests.test_mpvtk_browser_shell import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


def apply_server_moves(order, moves):
    """Replay moves the way the server does: remove the entry, insert at the
    absolute index — one at a time, in emission order."""
    order = list(order)
    for entry_id, new_index in moves:
        order.remove(entry_id)
        order.insert(new_index, entry_id)
    return order


class PlaylistMoveTest(unittest.TestCase):
    def _editor(self, n=5, selected=()):
        ctl = FakeController()
        self.batches = []
        ctl.playlist_move_many = lambda srv, pid, batch: self.batches.append(
            list(batch))
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "playlist_edit", "server": "srv1", "item_id": "PL",
                 "_items": [{"PlaylistItemId": "p%d" % i,
                             "Name": "Item %d" % i} for i in range(n)],
                 "_sel": set(selected)}
        b.nav_stack = [route]
        return b, route

    @staticmethod
    def _ids(route):
        return [e["PlaylistItemId"] for e in route["_items"]]

    def _emitted(self):
        return [m for batch in self.batches for m in batch]

    def _check_replay(self, route, before):
        self.assertEqual(
            apply_server_moves(before, self._emitted()), self._ids(route),
            "sequential replay diverged from what the editor is showing")

    def test_move_up_single(self):
        b, route = self._editor(selected=[2])
        before = self._ids(route)
        b._pe_move(route, "up")
        self.assertEqual(self._ids(route), ["p0", "p2", "p1", "p3", "p4"])
        self._check_replay(route, before)

    def test_move_down_single(self):
        b, route = self._editor(selected=[1])
        before = self._ids(route)
        b._pe_move(route, "down")
        self.assertEqual(self._ids(route), ["p0", "p2", "p1", "p3", "p4"])
        self._check_replay(route, before)

    def test_a_contiguous_block_moves_together(self):
        b, route = self._editor(selected=[1, 2])
        before = self._ids(route)
        b._pe_move(route, "down")
        self.assertEqual(self._ids(route), ["p0", "p3", "p1", "p2", "p4"])
        self._check_replay(route, before)

    def test_a_non_contiguous_selection_is_gathered(self):
        """Picks that are not adjacent collapse into a block at the target,
        keeping their relative order."""
        b, route = self._editor(selected=[0, 3])
        before = self._ids(route)
        b._pe_move(route, "down")
        self._check_replay(route, before)

    def test_top_and_bottom(self):
        for where in ("top", "bottom"):
            with self.subTest(where=where):
                b, route = self._editor(selected=[1, 3])
                before = self._ids(route)
                b._pe_move(route, where)
                self._check_replay(route, before)

    def test_at_the_edge_it_does_not_emit_a_no_op(self):
        """Moving the top row up must do nothing at all — not emit a move
        that reorders the rest around it."""
        b, route = self._editor(selected=[0])
        b._pe_move(route, "up")
        self.assertEqual(self._emitted(), [])
        self.assertEqual(self._ids(route), ["p0", "p1", "p2", "p3", "p4"])

    def test_the_whole_selection_moves_even_from_the_edge(self):
        """A block whose FIRST row is already at the top used to no-op the
        entire selection, so rows 1..n never moved."""
        b, route = self._editor(n=6, selected=[0, 4])
        before = self._ids(route)
        b._pe_move(route, "down")
        self.assertNotEqual(self._ids(route), before,
                            "an edge row froze the whole selection")
        self._check_replay(route, before)

    def test_random_selections_replay_exactly(self):
        """The property, over shapes nobody would think to enumerate."""
        rng = random.Random(20260720)
        for trial in range(300):
            n = rng.randint(2, 9)
            k = rng.randint(1, n)
            sel = rng.sample(range(n), k)
            where = rng.choice(["up", "down", "top", "bottom"])
            with self.subTest(trial=trial, n=n, sel=sorted(sel), where=where):
                b, route = self._editor(n=n, selected=sel)
                before = self._ids(route)
                b._pe_move(route, where)
                self._check_replay(route, before)


if __name__ == "__main__":
    unittest.main()
