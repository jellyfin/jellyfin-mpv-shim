"""Tests for the playlist editor's move/remove batch logic.

The editor mutates a local entries list and emits (entry_id, new_index)
moves; the main process applies them to the server SEQUENTIALLY (each move =
remove entry, insert at index). The key invariant: replaying the emitted
moves against the original server order must reproduce the editor's local
order, for every selection shape (blocks at edges, non-contiguous picks).
"""

import random
import unittest
from unittest import mock

from jellyfin_mpv_shim.library_browser.views import PlaylistEditView


def make_view(n=5, selected=()):
    view = PlaylistEditView.__new__(PlaylistEditView)
    view.entries = [{"PlaylistItemId": "p%d" % i, "Name": "Item %d" % i}
                    for i in range(n)]
    view.route = {"playlist_id": "PL"}
    view.app = mock.Mock()
    view.app.current_server = "srv"
    view.sent = []
    view.app.playlist_edit = view.sent.append
    view.tree = mock.Mock()
    view.tree.selection.return_value = ["p%d" % i for i in selected]
    view._rebuild = mock.Mock()
    return view


def ids(view):
    return [e["PlaylistItemId"] for e in view.entries]


def emitted_moves(view):
    return [m for p in view.sent if p.get("op") == "move"
            for m in p.get("moves", [])]


def apply_server_moves(order, moves):
    """Replay moves the way the server does: remove the entry, insert at the
    absolute index — one at a time, in emission order."""
    order = list(order)
    for entry_id, new_index in moves:
        order.remove(entry_id)
        order.insert(new_index, entry_id)
    return order


class MoveAlgorithmTest(unittest.TestCase):
    def check_replay(self, view, before):
        self.assertEqual(apply_server_moves(before, emitted_moves(view)),
                         ids(view),
                         "sequential replay diverged from the local order")

    def test_move_up_single(self):
        view = make_view(selected=[2])
        view._move_up()
        self.assertEqual(ids(view), ["p0", "p2", "p1", "p3", "p4"])
        self.assertEqual(emitted_moves(view), [("p2", 1)])

    def test_move_up_block_at_top_is_noop(self):
        view = make_view(selected=[0, 1])
        view._move_up()
        self.assertEqual(ids(view), ["p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(emitted_moves(view), [])
        self.assertEqual(view.sent, [])  # no pointless server round-trip

    def test_move_up_partially_blocked(self):
        # p0, p1 are packed against the top; only p3 can rise.
        view = make_view(selected=[0, 1, 3])
        view._move_up()
        self.assertEqual(ids(view), ["p0", "p1", "p3", "p2", "p4"])
        self.check_replay(view, ["p0", "p1", "p2", "p3", "p4"])

    def test_move_down_single(self):
        view = make_view(selected=[2])
        view._move_down()
        self.assertEqual(ids(view), ["p0", "p1", "p3", "p2", "p4"])
        self.check_replay(view, ["p0", "p1", "p2", "p3", "p4"])

    def test_move_down_block_at_bottom_is_noop(self):
        view = make_view(selected=[3, 4])
        view._move_down()
        self.assertEqual(ids(view), ["p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(view.sent, [])

    def test_move_top_non_contiguous(self):
        view = make_view(selected=[1, 3])
        view._move_top()
        self.assertEqual(ids(view), ["p1", "p3", "p0", "p2", "p4"])
        self.check_replay(view, ["p0", "p1", "p2", "p3", "p4"])

    def test_move_bottom_non_contiguous(self):
        view = make_view(selected=[0, 2])
        view._move_bottom()
        self.assertEqual(ids(view), ["p1", "p3", "p4", "p0", "p2"])
        self.check_replay(view, ["p0", "p1", "p2", "p3", "p4"])

    def test_replay_invariant_randomized(self):
        rng = random.Random(20260713)
        ops = ["_move_up", "_move_down", "_move_top", "_move_bottom"]
        for _ in range(200):
            n = rng.randint(1, 8)
            k = rng.randint(1, n)
            selected = sorted(rng.sample(range(n), k))
            op = rng.choice(ops)
            view = make_view(n=n, selected=selected)
            before = ids(view)
            getattr(view, op)()
            self.check_replay(view, before)


class RemoveSelectedTest(unittest.TestCase):
    def test_bulk_remove_is_one_message(self):
        # The jf-web pain point: any number of entries, ONE delete call.
        view = make_view(n=6, selected=[1, 2, 3, 4])
        view._remove_selected()
        self.assertEqual(ids(view), ["p0", "p5"])
        self.assertEqual(len(view.sent), 1)
        payload = view.sent[0]
        self.assertEqual(payload["op"], "remove")
        self.assertEqual(payload["entry_ids"], ["p1", "p2", "p3", "p4"])
        self.assertEqual(payload["playlist_id"], "PL")

    def test_remove_nothing_selected_is_noop(self):
        view = make_view(selected=[])
        view._remove_selected()
        self.assertEqual(len(ids(view)), 5)
        self.assertEqual(view.sent, [])


class EditResultTest(unittest.TestCase):
    def test_failure_reloads_from_server(self):
        view = make_view()
        view._load = mock.Mock()
        view.on_edit_result({"kind": "playlist", "ok": False,
                             "error": "boom"})
        view._load.assert_called_once()

    def test_success_keeps_local_model(self):
        view = make_view()
        view._load = mock.Mock()
        view.on_edit_result({"kind": "playlist", "ok": True})
        view._load.assert_not_called()

    def test_other_kinds_ignored(self):
        view = make_view()
        view._load = mock.Mock()
        view.on_edit_result({"kind": "collection", "ok": False})
        view._load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
