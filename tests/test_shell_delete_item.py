"""Delete from Disk (#4) — the one destructive thing in the browser.

Three properties, and each has already been a bug somewhere in this app:

* **the gate is the item's own ``CanDelete``**, not the account's policy.
  The server grants deletion per *library*, so a client-side reading of
  the user is right about the account and wrong about half their libraries
  — and absent must mean no, which is the opposite of ``may_download``'s
  fail-open (see docs/PERMISSION_GAPS.md §4/§6).
* **the confirmation says what it destroys, in full.** ``_confirm`` drew
  its message with a bare Text, which ellipsizes at the shell width; a
  dialog whose whole point is the warning cannot have the warning cut off.
* **the local copy goes with it**, or the library says the item is gone
  while this machine still plays it.
"""

import unittest

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeController,
    FakeSource,
    _SyncPool,
    build_scene,
    ids,
)


class DeleteMenuEntryTest(unittest.TestCase):

    def _browser(self, offline=False):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b.nav_stack = [{"kind": "grid", "server": "srv"}]
        b._pool = _SyncPool()
        b._offline = offline
        return b

    def _entries(self, item, offline=False):
        return [e[2] for e in self._browser(offline)._tile_menu_entries(item)]

    def test_an_item_the_server_says_is_deletable_offers_it(self):
        self.assertIn("deleteitem", self._entries(
            {"Id": "x", "Type": "Movie", "CanDelete": True}))

    def test_an_item_that_says_no_does_not(self):
        self.assertNotIn("deleteitem", self._entries(
            {"Id": "x", "Type": "Movie", "CanDelete": False}))

    def test_an_absent_answer_is_a_no(self):
        """The opposite of may_download's fail-open, deliberately: hiding a
        delete the user could have made is an inconvenience; offering one
        that 403s at the point of no return is not. A list query really does
        omit the field unless asked (measured against 10.11), so this is the
        state a stale field set produces."""
        self.assertNotIn("deleteitem",
                         self._entries({"Id": "x", "Type": "Movie"}))

    def test_it_is_the_last_entry(self):
        # The only entry that destroys anything; next to Play it gets
        # misclicked.
        entries = self._entries({"Id": "x", "Type": "Movie",
                                 "CanDelete": True})
        self.assertEqual(entries[-1], "deleteitem")

    def test_offline_does_not_offer_it(self):
        # Nothing to delete *on*, and the local copy has its own
        # Remove Download.
        self.assertNotIn("deleteitem",
                         self._entries({"Id": "x", "Type": "Movie",
                                        "CanDelete": True}, offline=True))

    def test_it_is_not_restricted_by_type(self):
        """Deliberately no type set: the server already answered, and a
        second gate here could only ever be wrong in the direction of
        refusing something the user is allowed to do."""
        for t in ("Movie", "Episode", "Series", "Season", "Audio", "Book",
                  "MusicAlbum", "Video"):
            self.assertIn("deleteitem",
                          self._entries({"Id": "x", "Type": t,
                                         "CanDelete": True}),
                          "%s should offer it when the server says yes" % t)


class DeleteFlowTest(unittest.TestCase):

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b.nav_stack = [{"kind": "grid", "server": "srv"}]
        self.b._pool = _SyncPool()
        self.item = {"Id": "m1", "Type": "Movie", "Name": "The Film",
                     "CanDelete": True}

    def _texts(self, nodes):
        return [n.get("text") or "" for n in nodes]

    def _confirm_scene(self):
        self.b._actions.confirm_delete_item(self.item, "srv")
        return build_scene(self.b, (1280, 720))

    def _calls(self, name):
        """Gateway calls by name. The base FakeController routes anything
        it does not declare through its catch-all recorder, which is where
        both of these land -- `transport_kw` because the local cleanup is
        called with a keyword."""
        return [(a, k) for n, a, k in self.b.controller.transport_kw
                if n == name]

    def test_it_asks_before_doing_anything(self):
        nodes, _h = self._confirm_scene()
        self.assertIn("confirm", ids(nodes))
        self.assertEqual(self._calls("delete_item"), [])

    def test_the_warning_says_it_destroys_the_file(self):
        nodes, _h = self._confirm_scene()
        joined = " ".join(self._texts(nodes))
        self.assertIn("delete it from both the file system and your media "
                      "library", joined)
        self.assertIn("The Film", joined)

    def test_the_warning_is_not_cut_off(self):
        """A bare Text ellipsizes at the shell width, and this dialog is
        nothing but its explanation. Asserted on the drawn nodes, because
        that is the only place the truncation would show."""
        nodes, _h = self._confirm_scene()
        self.assertNotIn("…", " ".join(self._texts(nodes)))

    def test_both_the_title_and_the_button_say_from_disk(self):
        # Every other Delete on these screens removes a download; the two
        # are one careless press apart.
        nodes, _h = self._confirm_scene()
        self.assertEqual(
            [t for t in self._texts(nodes) if t == "Delete from Disk"],
            ["Delete from Disk", "Delete from Disk"])

    def test_cancelling_deletes_nothing(self):
        _nodes, handlers = self._confirm_scene()
        handlers["dlg-cancel"]["click"]()
        self.assertEqual(self._calls("delete_item"), [])
        self.assertEqual(self._calls("delete_download"), [])
        nodes, _h = build_scene(self.b, (1280, 720))
        self.assertNotIn("confirm", ids(nodes))

    def test_confirming_deletes_on_the_server(self):
        _nodes, handlers = self._confirm_scene()
        handlers["dlg-ok"]["click"]()
        calls = [c for c in self.b.controller.transport
                 if c[0] == "delete_item"]
        self.assertEqual(calls, [("delete_item", ("srv", "m1"))])

    def test_the_downloaded_copy_goes_too(self):
        """Or the library says the item is gone while this machine still
        plays it — and the local file is now the only copy of something the
        user asked to destroy."""
        _nodes, handlers = self._confirm_scene()
        handlers["dlg-ok"]["click"]()
        self.assertEqual(self._calls("delete_download"),
                         [((), {"item_id": "m1"})])

    def test_a_refused_delete_is_reported(self):
        """A destructive call that reports success it did not have is the
        bug delete_download was fixed for."""
        def boom(*_a, **_k):
            raise RuntimeError("403")

        self.b.controller.delete_item = boom
        _nodes, handlers = self._confirm_scene()
        handlers["dlg-ok"]["click"]()
        self.assertIn("could not be deleted", self.b.status)
        # And the local copy survives: the server still has it.
        self.assertEqual(self._calls("delete_download"), [])

    def test_a_failed_local_cleanup_does_not_hide_the_success(self):
        # The server delete is the one that has to be reported; a catalog
        # row that failed to clear is cosmetic next to it.
        def boom(*_a, **_k):
            raise RuntimeError("catalog locked")

        self.b.controller.delete_download = boom
        _nodes, handlers = self._confirm_scene()
        handlers["dlg-ok"]["click"]()
        self.assertIn("was deleted", self.b.status)


if __name__ == "__main__":
    unittest.main()


class DeleteFromTheDetailPageTest(unittest.TestCase):
    """The other door. It differs from the tile menu in what happens
    *after*: a grid re-reads its list, but this screen IS the deleted item,
    so re-reading it would fetch a 404 and show an error where a film was."""

    def _browser(self, can_delete=True):
        src = FakeSource()
        item = {"Id": "m1", "Type": "Movie", "Name": "The Film",
                "MediaSources": [], "CanDelete": can_delete}
        src.get_item = lambda server, item_id: dict(item)
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.nav_stack = [{"kind": "grid", "server": "srv"},
                       {"kind": "detail", "server": "srv", "item_id": "m1",
                        "title": "The Film"}]
        b._load_route(b.route)
        return b

    def test_the_button_is_offered_when_the_server_allows_it(self):
        nodes, _h = build_scene(self._browser(), (1280, 720))
        self.assertIn("act-delete", ids(nodes))

    def test_and_absent_when_it_does_not(self):
        nodes, _h = build_scene(self._browser(can_delete=False), (1280, 720))
        self.assertNotIn("act-delete", ids(nodes))

    def test_pressing_it_asks_first(self):
        b = self._browser()
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["act-delete"]["click"]()
        nodes, _h = build_scene(b, (1280, 720))
        self.assertIn("confirm", ids(nodes))

    def test_confirming_deletes_and_leaves_the_page(self):
        b = self._browser()
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["act-delete"]["click"]()
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["dlg-ok"]["click"]()
        calls = [(a, k) for n, a, k in b.controller.transport_kw
                 if n == "delete_item"]
        self.assertEqual(calls, [(("srv", "m1"), {})])
        # Back to the grid it was opened from — not still showing a page
        # whose item no longer exists.
        self.assertEqual([r["kind"] for r in b.nav_stack], ["grid"])
