"""Playlists and collections: editing, reordering, and adding to them.

Includes the optimistic-update rollback path, which is where reordering and
removal get their responsiveness and their failure modes.
"""

import unittest
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeController,
    FakeSource,
    _DeferredPool,
    _SyncPool,
    build_scene,
    editor_page,
    grid_scroll,
    home_page,
    ids,
    menu_pick,
    music_songs_scroll,
)


class TestPlaylistEdit(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _open_edit(self):
        self.b.navigate({"kind": "playlist_edit", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        return build_scene(self.b)

    def test_edit_renders_rows_and_toolbar(self):
        nodes, _h = self._open_edit()
        self.assertIn("pe-top", ids(nodes))
        self.assertIn("pe-row-0", ids(nodes))

    def test_move_down_reorders_and_calls_api(self):
        self._open_edit()
        route = self.b.route
        first = route["_items"][0]["PlaylistItemId"]
        editor_page(self.b, route).set_selection({0})
        editor_page(self.b, route)._move("down")
        self.assertEqual(route["_items"][1]["PlaylistItemId"], first)
        self.assertEqual(route["_sel"], {1})
        self.assertIn("playlist_move_many",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_remove_drops_row_and_calls_api(self):
        self._open_edit()
        route = self.b.route
        editor_page(self.b, route).set_selection({1})
        n0 = len(route["_items"])
        editor_page(self.b, route)._remove()
        self.assertEqual(len(route["_items"]), n0 - 1)
        self.assertIn("playlist_remove",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_plain_click_selects_only_that_row(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]({})
        h["pe-row-2"]["click"]({})
        self.assertEqual(self.b.route["_sel"], {2})

    def test_shift_click_selects_the_range_in_two_clicks(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]({})
        h["pe-row-2"]["click"]({"shift": True})
        self.assertEqual(self.b.route["_sel"], {0, 1, 2})

    def test_shift_click_works_upwards_too(self):
        nodes, h = self._open_edit()
        h["pe-row-2"]["click"]({})
        h["pe-row-0"]["click"]({"shift": True})
        self.assertEqual(self.b.route["_sel"], {0, 1, 2})

    def test_ctrl_click_toggles_additively(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]({})
        h["pe-row-2"]["click"]({"ctrl": True})
        self.assertEqual(self.b.route["_sel"], {0, 2})
        h["pe-row-0"]["click"]({"ctrl": True})
        self.assertEqual(self.b.route["_sel"], {2})

    def test_block_move_keeps_selection_contiguous(self):
        self._open_edit()
        route = self.b.route
        ids0 = [i["PlaylistItemId"] for i in route["_items"]]
        editor_page(self.b, route).set_selection({1, 2})
        editor_page(self.b, route)._move("top")
        self.assertEqual([i["PlaylistItemId"] for i in route["_items"]],
                         [ids0[1], ids0[2], ids0[0]])
        self.assertEqual(route["_sel"], {0, 1})

    def test_bulk_remove_sends_one_call(self):
        self._open_edit()
        route = self.b.route
        editor_page(self.b, route).set_selection({0, 2})
        editor_page(self.b, route)._remove()
        self.assertEqual(len(route["_items"]), 1)
        calls = [c for c in self.ctl.transport if c[0] == "playlist_remove"]
        self.assertEqual(len(calls), 1)

class TestAddToPlaylist(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_add_to_dialog_lists_playlists_and_adds(self):
        self.b._open_add_to({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        nodes, handlers = build_scene(self.b)
        self.assertIn("add-pl-0", ids(nodes))
        self.assertIn("add-pl-1", ids(nodes))
        handlers["add-pl-0"]["click"]()
        self.assertIn("playlist_add",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIsNone(self.b._dialog)

    def test_menu_add_to_playlist_opens_dialog(self):
        self.b._pool = _SyncPool()
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        menu_pick(self.b, "addto")
        self.assertIsNone(self.b._menu)
        self.assertIsNotNone(self.b._dialog)

    def test_create_new_playlist(self):
        self.b._open_add_to({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        _n, h = build_scene(self.b)
        self.assertIn("add-newname", ids(_n))
        h["add-newname"]["change"]("Road Trip")
        h["add-create"]["click"]()
        self.assertIn("playlist_new", [c[0] for c in self.ctl.transport])
        self.assertIsNone(self.b._dialog)

    def test_enter_in_the_name_box_creates_the_playlist(self):
        """The button beside it works; Enter did nothing."""
        self.b._open_add_to({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("Road Trip")
        self.assertIn("submit", h["add-newname"], "no Enter on the name box")
        h["add-newname"]["submit"]("Road Trip")
        self.assertIn("playlist_new", [c[0] for c in self.ctl.transport])

class TestPlaylistExtras(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_playlist_shuffle_and_download_buttons(self):
        self.b.navigate({"kind": "playlist", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        nodes, _h = build_scene(self.b)
        for nid in ("pl-play", "pl-shuffle", "pl-download", "pl-edit"):
            self.assertIn(nid, ids(nodes))

    def test_playlist_edit_rename_and_public(self):
        self.b.navigate({"kind": "playlist_edit", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        nodes, h = build_scene(self.b)
        for nid in ("pe-name", "pe-rename", "pe-public"):
            self.assertIn(nid, ids(nodes))
        h["pe-name"]["change"]("Renamed")
        h["pe-rename"]["click"]()
        self.assertEqual(self.b.route["title"], "Renamed")
        self.assertIn("playlist_update", [c[0] for c in self.ctl.transport])
        # The Public toggle refuses until the server's real visibility has
        # been read, so a first click can't flip an already-public list.
        _n, h = build_scene(self.b)
        h["pe-public"]["click"]()
        self.assertFalse(self.b.route.get("_public"))
        self.b.route["_public_known"] = True
        _n, h = build_scene(self.b)
        h["pe-public"]["click"]()
        self.assertTrue(self.b.route["_public"])

class TestPlaylistTileShape(unittest.TestCase):
    """A Jellyfin playlist's own Primary image is square; rendering it in
    the 2:3 poster frame pillarboxes it."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_all_playlist_grid_is_square(self):
        items = [{"Id": "p1", "Type": "Playlist"},
                 {"Id": "p2", "Type": "Playlist"}]
        self.assertIs(self.b._square_geom(items), self.b.geom_square)

    def test_music_stays_square(self):
        self.assertIs(
            self.b._square_geom([{"Id": "a1", "Type": "MusicAlbum"}]),
            self.b.geom_square)

    def test_a_mixed_grid_keeps_posters(self):
        """One strip is composited at a single tile size, so a grid that
        mixes shapes has to pick the default rather than square everything."""
        items = [{"Id": "p1", "Type": "Playlist"},
                 {"Id": "m1", "Type": "Movie"}]
        self.assertIsNone(self.b._square_geom(items))

    def test_movies_are_not_square(self):
        self.assertIsNone(self.b._square_geom([{"Id": "m1", "Type": "Movie"}]))

    def test_empty_grid_keeps_the_default(self):
        self.assertIsNone(self.b._square_geom([]))

    def test_playlists_home_row_is_square(self):
        geom, itype = home_page(self.b)._row_shape(
            {"collection_type": "playlists", "items": [
                {"Id": "p1", "Type": "Playlist"}]})
        self.assertIs(geom, self.b.geom_square)
        self.assertEqual(itype, "Primary")

    def test_an_untyped_playlist_row_is_still_square(self):
        geom, _t = home_page(self.b)._row_shape(
            {"collection_type": None,
             "items": [{"Id": "p1", "Type": "Playlist"}]})
        self.assertIs(geom, self.b.geom_square)

class TestNewPlaylistPrivacy(unittest.TestCase):
    """The server creates playlists PUBLIC unless told otherwise, so
    omitting the flag published every playlist to the whole server."""

    def test_new_playlists_default_to_private(self):
        calls = []
        ctl = FakeController()
        ctl.playlist_new = lambda *a, **kw: calls.append((a, kw))
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._addto_name = {"name": "Road Trip", "private": True}
        b._add_to_new("srv1", "m1")
        self.assertEqual(calls[0][1].get("is_public"), False)

    def test_unticking_private_creates_a_public_playlist(self):
        calls = []
        ctl = FakeController()
        ctl.playlist_new = lambda *a, **kw: calls.append((a, kw))
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._addto_name = {"name": "Shared", "private": False}
        b._add_to_new("srv1", "m1")
        self.assertEqual(calls[0][1].get("is_public"), True)

class TestPlaylistQueueing(unittest.TestCase):
    """Clicking an entry in a video playlist must play the PLAYLIST from
    that point. It went through _open_item, so Play on the detail page
    queued the item's series instead — silently abandoning the playlist."""

    def setUp(self):
        self.ctl = FakeController()
        self.plays = []
        self.ctl.play_list = lambda ids, srv, i, **kw: self.plays.append(
            (list(ids), i, kw.get("offset_ticks")))
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _items(self):
        return [
            {"Id": "m1", "Type": "Movie", "Name": "One"},
            {"Id": "e1", "Type": "Episode", "Name": "Two",
             "UserData": {"PlaybackPositionTicks": 90000000}},
            {"Id": "m2", "Type": "Movie", "Name": "Three"},
        ]

    def test_clicking_an_entry_queues_the_playlist_from_there(self):
        items = self._items()
        ids = [i["Id"] for i in items]
        self.b._play_list(ids, "srv1", 1, items=items)
        played_ids, start, _off = self.plays[0]
        self.assertEqual(played_ids, ids, "queued something other than "
                         "the playlist")
        self.assertEqual(start, 1)

    def test_the_clicked_entry_resumes(self):
        items = self._items()
        self.b._play_list([i["Id"] for i in items], "srv1", 1, items=items)
        self.assertEqual(self.plays[0][2], 90000000, "resume offset lost")

    def test_an_entry_without_progress_starts_from_zero(self):
        items = self._items()
        self.b._play_list([i["Id"] for i in items], "srv1", 0, items=items)
        self.assertIsNone(self.plays[0][2])

    def test_a_missing_id_does_not_shift_the_queue(self):
        """Filtering empties out before using the caller's index moved the
        queue out from under the entry that was clicked."""
        items = [{"Id": None, "Type": "Movie"},
                 {"Id": "m2", "Type": "Movie"},
                 {"Id": "m3", "Type": "Movie"}]
        ids = [i["Id"] for i in items]
        self.b._play_list(ids, "srv1", 2, items=items)
        played_ids, start, _off = self.plays[0]
        self.assertEqual(played_ids[start], "m3",
                         "started the wrong entry")

    def test_an_out_of_range_index_falls_back_to_the_start(self):
        self.b._play_list(["m1", "m2"], "srv1", 9)
        self.assertEqual(self.plays[0][1], 0)

    def test_video_playlists_render_only_supported_types(self):
        route = {"kind": "playlist", "server": "srv1", "item_id": "P",
                 "title": "Mix", "_data": self._items() + [
                     {"Id": "x1", "Type": "Photo", "Name": "Nope"}]}
        self.b.nav_stack = [route]
        nodes, _h = build_scene(self.b)
        rendered = " ".join(str(n.get("id", "")) for n in nodes)
        self.assertNotIn("x1", rendered, "unsupported entry rendered a tile")
        self.assertIn("m1", rendered)

class TestRemoveFromPlaylist(unittest.TestCase):
    """A quick single-entry removal without entering the editor. Removal
    is by PlaylistItemId — the same item can appear twice."""

    def setUp(self):
        self.ctl = FakeController()
        self.removed = []
        self.ctl.playlist_remove = lambda srv, pid, ids: self.removed.append(
            (pid, list(ids)))
        self.ctl.edit_apis = lambda: True
        self.src = FakeSource()
        self.src.get_playlist_items = lambda srv, pid: []
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.nav_stack = [{"kind": "playlist", "server": "srv1",
                             "item_id": "P", "title": "Mix", "_data": []}]

    def _entry(self):
        return {"Id": "m1", "Type": "Movie", "Name": "One",
                "PlaylistItemId": "e9"}

    def test_the_entry_is_offered_inside_a_playlist(self):
        acts = [e[2] for e in self.b._tile_menu_entries(self._entry())]
        self.assertIn("unplaylist", acts)

    def test_it_is_not_offered_outside_a_playlist(self):
        self.b.nav_stack = [{"kind": "grid", "server": "srv1",
                             "parent_id": "lib1"}]
        acts = [e[2] for e in self.b._tile_menu_entries(self._entry())]
        self.assertNotIn("unplaylist", acts)

    def test_an_entry_without_a_playlist_item_id_is_skipped(self):
        acts = [e[2] for e in self.b._tile_menu_entries(
            {"Id": "m1", "Type": "Movie"})]
        self.assertNotIn("unplaylist", acts)

    def test_removing_passes_the_entry_id(self):
        self.b._remove_from_playlist(self._entry())
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.removed, [("P", ["e9"])])

class TestAddToDialogLayout(unittest.TestCase):
    """The dialog listed every playlist as a button and always showed the
    Private checkbox — a flat list of 40 playlists made it unusably tall."""

    def setUp(self):
        self.src = FakeSource()
        self.src.get_playlists = lambda srv: [
            {"Id": "p%d" % i, "Name": "Playlist %d" % i} for i in range(40)]
        self.src.get_collections = lambda srv: [{"Id": "c1", "Name": "Set"}]
        self.b = MpvtkBrowser(app=None, source=self.src,
                              controller=FakeController())
        self.b._pool = _SyncPool()
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})

    def test_the_playlist_list_scrolls(self):
        nodes, _h = build_scene(self.b)
        scrolls = [n for n in nodes if n["t"] == "scroll"
                   and n.get("id") == "add-pl"]
        self.assertTrue(scrolls, "playlist list is not scrollable")
        self.assertLessEqual(scrolls[0]["h"], self.b.PICKER_H + 1)

    def test_the_dialog_fits_the_window(self):
        nodes, _h = build_scene(self.b)
        dialog = [n for n in nodes if str(n.get("id", "")) == "addto"]
        self.assertTrue(dialog)
        self.assertLessEqual(dialog[0]["h"], 720,
                             "dialog is taller than the window")

    def test_private_is_hidden_until_a_name_is_typed(self):
        nodes, h = build_scene(self.b)
        self.assertNotIn("add-private", ids(nodes))
        h["add-newname"]["change"]("Road Trip")
        nodes, _h = build_scene(self.b)
        self.assertIn("add-private", ids(nodes))

    def test_clearing_the_name_hides_it_again(self):
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("x")
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("")
        nodes, _h = build_scene(self.b)
        self.assertNotIn("add-private", ids(nodes))

    def test_whitespace_is_not_a_name(self):
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("   ")
        nodes, _h = build_scene(self.b)
        self.assertNotIn("add-private", ids(nodes))

    def _ticks(self, nodes):
        """How many check marks the Private box is drawing (0 or 1)."""
        return len([n for n in nodes if n.get("text") == "✓"])

    def test_toggling_private_shows_on_screen(self):
        """The value the box would draw, both ways round.

        Note what this does NOT catch: `build_scene` re-renders on demand,
        so it sees a correct tree whether or not anything asked for one. The
        bug here was precisely that nothing asked — see the test below, which
        is the one with teeth. Kept because the value-to-tick mapping is
        worth pinning on its own, not because it guards this.
        """
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("Road Trip")
        nodes, h = build_scene(self.b)
        self.assertEqual(self._ticks(nodes), 1,
                         "playlists are private by default, so the box "
                         "starts ticked")
        h["add-private"]["click"]()
        nodes, h = build_scene(self.b)
        self.assertEqual(self._ticks(nodes), 0, "the tick did not clear")
        h["add-private"]["click"]()
        nodes, _h = build_scene(self.b)
        self.assertEqual(self._ticks(nodes), 1, "the tick did not come back")

    def test_toggling_private_asks_for_a_repaint(self):
        """The half a rebuilt scene cannot see.

        A Checkbox is composited on this side -- the renderer has no notion
        of one and draws whatever colour the last tree gave it -- so in the
        app nothing changes unless something asks for a redraw, and this
        asked for nothing. The box flipped invisibly: no feedback, and after
        two clicks no way to tell what would be created.

        Every state-changing handler in the browser owes this assertion, and
        a scene-based one cannot stand in for it: the harness renders when
        asked, so it draws the corrected tree either way.
        """
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("Road Trip")
        _n, h = build_scene(self.b)
        calls = []
        real = self.b.invalidate
        self.b.invalidate = lambda *a, **kw: (calls.append(1), real(*a, **kw))
        h["add-private"]["click"]()
        self.assertTrue(calls, "toggling Private redrew nothing")

    def test_the_toggled_value_is_what_gets_created(self):
        created = []
        self.b.controller.playlist_new = (
            lambda srv, name, ids, is_public=False:
            created.append((name, is_public)) or "pl-new")
        _n, h = build_scene(self.b)
        h["add-newname"]["change"]("Road Trip")
        _n, h = build_scene(self.b)
        h["add-private"]["click"]()            # private -> public
        _n, h = build_scene(self.b)
        h["add-create"]["click"]()
        self.assertTrue(created, "no playlist was created")
        self.assertEqual(created[0][0], "Road Trip")
        self.assertTrue(created[0][1],   # is_public
                        "the un-ticked box still created a private playlist")

    def test_an_empty_playlist_list_says_so(self):
        self.src.get_playlists = lambda srv: []
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        nodes, _h = build_scene(self.b)
        self.assertIn("No playlists yet.",
                      [n.get("text") for n in nodes if n.get("text")])

class TestPlaylistDeleteNavigation(unittest.TestCase):
    """Deleting a playlist left the user on its now-dead page: the prune
    matched routes by parent_id, but a playlist page keys its id as
    item_id, so nothing was ever pruned."""

    def setUp(self):
        self.ctl = FakeController()
        self.deleted = []
        self.ctl.playlist_delete = lambda srv, pid: self.deleted.append(pid)
        self.src = FakeSource()
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"
        self.b.nav_stack = [
            {"kind": "grid", "server": "srv1", "parent_id": "lib1",
             "title": "Playlists", "_items": [{"Id": "P"}]},
            {"kind": "playlist", "server": "srv1", "item_id": "P",
             "title": "Mix", "_data": []},
            {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
             "title": "Mix", "_items": []},
        ]

    def test_deleting_backs_out_of_the_playlist(self):
        editor_page(self.b, self.b.route)._delete()
        self.assertEqual(self.deleted, ["P"])
        kinds = [r["kind"] for r in self.b.nav_stack]
        self.assertNotIn("playlist", kinds, "left on the deleted playlist")
        self.assertNotIn("playlist_edit", kinds)
        self.assertEqual(self.b.route["kind"], "grid")

    def test_the_route_we_land_on_refetches(self):
        """The grid we came from still listed the deleted playlist, because
        it was rendered from its cached _items."""
        editor_page(self.b, self.b.route)._delete()
        ids = [i.get("Id") for i in self.b.route.get("_items") or [] if i]
        self.assertNotIn("P", ids, "still lists the deleted playlist")
        self.assertTrue(ids, "landed on an unloaded route")

    def test_a_failed_delete_does_not_navigate_away(self):
        def boom(srv, pid):
            raise OSError("server said no")

        self.ctl.playlist_delete = boom
        editor_page(self.b, self.b.route)._delete()
        self.assertEqual(self.b.route["kind"], "playlist_edit",
                         "walked out of a playlist that still exists")
        self.assertIn("could not be deleted", self.b.status)

    def test_an_empty_stack_falls_back_home(self):
        self.b.nav_stack = [{"kind": "playlist", "server": "srv1",
                             "item_id": "P", "title": "Mix"}]
        self.b.after_playlist_deleted("P")
        self.assertEqual(self.b.route["kind"], "home")

    def test_unrelated_routes_survive(self):
        self.b.after_playlist_deleted("OTHER")
        self.assertEqual(len(self.b.nav_stack), 3)

class TestPlaylistReorderBatch(unittest.TestCase):
    """A move is an absolute-index operation, so a multi-row move only
    composes if each lands before the next. They were submitted as N
    concurrent tasks on a 4-worker pool, landing in arbitrary order.

    These used to assert the batch NAMED the selected rows in selection
    order. That encoded a broken contract: the emitted indexes assumed the
    selection ends up contiguous, and downward moves do not compose in
    forward order, so the server ended up with an order different from the
    one on screen (see tests/test_playlist_edit.py, which found it in 95 of
    300 random selections). The batch is now derived from the RESULT, so
    what matters is that replaying it reproduces the screen — not which
    rows it happens to name."""

    @staticmethod
    def _replay(before_ids, moves):
        order = list(before_ids)
        for entry_id, index in moves:
            order.remove(entry_id)
            order.insert(index, entry_id)
        return order

    def setUp(self):
        self.ctl = FakeController()
        self.batches = []
        self.ctl.playlist_move_many = lambda srv, pid, moves: (
            self.batches.append(list(moves)))
        self.src = FakeSource()
        self.entries = [{"Id": "i%d" % i, "Name": "Track %d" % i,
                         "PlaylistItemId": "e%d" % i} for i in range(5)]
        self.src.get_playlist_items = lambda srv, pid: list(self.entries)
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.nav_stack = [{"kind": "playlist_edit", "server": "srv1",
                             "item_id": "P", "title": "Mix",
                             "_items": list(self.entries)}]

    def test_a_multi_row_move_is_one_ordered_batch(self):
        route = self.b.route
        before = [e["PlaylistItemId"] for e in route["_items"]]
        editor_page(self.b, route).set_selection({0, 1})
        editor_page(self.b, route)._move("down")
        self.assertEqual(len(self.batches), 1, "not a single batch")
        self.assertEqual(
            self._replay(before, self.batches[0]),
            [e["PlaylistItemId"] for e in route["_items"]],
            "the server would end up in a different order than the screen")

    def test_the_batch_targets_the_shown_positions(self):
        route = self.b.route
        editor_page(self.b, route).set_selection({3})
        editor_page(self.b, route)._move("top")
        self.assertEqual(self.batches[0], [("e3", 0)])

    def test_a_failed_reorder_resyncs_instead_of_lying(self):
        def boom(srv, pid, moves):
            raise OSError("server refused")

        self.ctl.playlist_move_many = boom
        route = self.b.route
        editor_page(self.b, route).set_selection({0})
        editor_page(self.b, route)._move("down")
        self.assertIn("could not be reordered", self.b.status)
        self.assertEqual([i["PlaylistItemId"] for i in route["_items"]],
                         ["e0", "e1", "e2", "e3", "e4"],
                         "left the optimistic order after a failure")

    def test_entries_without_an_id_are_skipped(self):
        """A row the server cannot address must not appear in the batch —
        and must not derail the rows that can."""
        self.entries[1].pop("PlaylistItemId")
        self.b.route["_items"] = list(self.entries)
        editor_page(self.b, self.b.route).set_selection({0, 1})
        editor_page(self.b, self.b.route)._move("down")
        named = [m[0] for m in self.batches[0]]
        self.assertNotIn(None, named, "emitted a move for an unaddressable row")
        self.assertTrue(named, "emitted nothing at all")

class TestCollectionEditing(unittest.TestCase):
    """collection_remove and collection_new existed on the controller with
    zero call sites — written, committed, unreachable."""

    def setUp(self):
        self.ctl = FakeController()
        self.removed, self.created = [], []
        self.ctl.collection_remove = lambda s, c, i: self.removed.append(
            (c, list(i)))
        self.ctl.collection_new = lambda s, n, i: self.created.append(
            (n, list(i)))
        self.ctl.edit_apis = lambda: True
        self.src = FakeSource()
        self.src.get_collections = lambda srv: [{"Id": "c1", "Name": "Set"}]
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def test_remove_is_offered_inside_a_boxset(self):
        self.b.nav_stack = [{"kind": "grid", "server": "srv1",
                             "parent_id": "c1", "parent_type": "BoxSet"}]
        acts = [e[2] for e in self.b._tile_menu_entries(
            {"Id": "m1", "Type": "Movie"})]
        self.assertIn("uncollect", acts)

    def test_remove_is_not_offered_elsewhere(self):
        self.b.nav_stack = [{"kind": "grid", "server": "srv1",
                             "parent_id": "lib1"}]
        acts = [e[2] for e in self.b._tile_menu_entries(
            {"Id": "m1", "Type": "Movie"})]
        self.assertNotIn("uncollect", acts)

    def test_removing_calls_the_api_and_refetches(self):
        self.b.nav_stack = [{"kind": "grid", "server": "srv1",
                             "parent_id": "c1", "parent_type": "BoxSet",
                             "_items": [{"Id": "m1"}]}]
        self.b._remove_from_collection({"Id": "m1", "Name": "A"})
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.removed, [("c1", ["m1"])])

    def test_the_create_box_reaches_the_collection_dialog(self):
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        _n, h = build_scene(self.b)
        h["add-collections"]["click"]()
        nodes, h = build_scene(self.b)
        self.assertIn("addcol-newname", ids(nodes), "no create box")
        h["addcol-newname"]["change"]("Marathon")
        h["addcol-create"]["click"]()
        self.assertEqual(self.created, [("Marathon", ["m1"])])

class TestAddToResolvesContainers(unittest.TestCase):
    """A music container is not itself a playlist entry — posting its own
    id does nothing. Tk resolves album/artist/genre to track ids first."""

    def setUp(self):
        self.ctl = FakeController()
        self.added = []
        self.ctl.playlist_add = lambda s, p, ids: self.added.append(list(ids))
        self.src = FakeSource()
        self.src.get_playlists = lambda srv: [{"Id": "p1", "Name": "Mix"}]
        self.src.get_album_tracks = lambda srv, aid: [{"Id": "t1"},
                                                      {"Id": "t2"}]
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def test_an_album_is_added_as_its_tracks(self):
        self.b._open_add_to({"Id": "a1", "Type": "MusicAlbum"})
        _n, h = build_scene(self.b)
        h["add-pl-0"]["click"]()
        self.assertEqual(self.added, [["t1", "t2"]])

    def test_a_movie_is_added_as_itself(self):
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        _n, h = build_scene(self.b)
        h["add-pl-0"]["click"]()
        self.assertEqual(self.added, [["m1"]])

    def test_music_containers_are_offered_the_action(self):
        acts = [e[2] for e in self.b._tile_menu_entries(
            {"Id": "a1", "Type": "MusicAlbum"})]
        self.assertIn("addto", acts)

class TestNonContiguousMoves(unittest.TestCase):
    """Up/Down move each selected row one step, against a floor/ceiling, so
    a scattered selection keeps its gaps. Treating it as one block silently
    reordered rows the user had not selected, and a selection whose leading
    row was already at the edge no-opped for the whole selection."""

    def _move(self, items, sel, where):
        from jellyfin_mpv_shim.mpvtk_browser.pages import queue_edit as qe

        return qe.block_move(list(items), set(sel), where)

    ABC = ["a", "b", "c", "d", "e"]

    def test_a_scattered_selection_keeps_its_gaps_going_up(self):
        got, sel = self._move(self.ABC, {1, 3}, "up")
        self.assertEqual(got, ["b", "a", "d", "c", "e"])
        self.assertEqual(sel, {0, 2})

    def test_a_scattered_selection_keeps_its_gaps_going_down(self):
        got, sel = self._move(self.ABC, {1, 3}, "down")
        self.assertEqual(got, ["a", "c", "b", "e", "d"])
        self.assertEqual(sel, {2, 4})

    def test_the_rest_still_moves_when_the_first_row_is_pinned(self):
        """sel[0] at the top used to abandon the whole move."""
        got, sel = self._move(self.ABC, {0, 3}, "up")
        self.assertEqual(got, ["a", "b", "d", "c", "e"])
        self.assertEqual(sel, {0, 2})

    def test_a_contiguous_block_still_moves_as_one(self):
        got, sel = self._move(self.ABC, {1, 2}, "up")
        self.assertEqual(got, ["b", "c", "a", "d", "e"])
        self.assertEqual(sel, {0, 1})

    def test_everything_packed_against_the_edge_is_a_no_op(self):
        self.assertIsNone(self._move(self.ABC, {0, 1}, "up"))
        self.assertIsNone(self._move(self.ABC, {3, 4}, "down"))

    def test_top_and_bottom_still_gather_a_scattered_selection(self):
        """That is the point of them."""
        got, sel = self._move(self.ABC, {1, 3}, "top")
        self.assertEqual(got, ["b", "d", "a", "c", "e"])
        self.assertEqual(sel, {0, 1})
        got, sel = self._move(self.ABC, {0, 2}, "bottom")
        self.assertEqual(got, ["b", "d", "e", "a", "c"])
        self.assertEqual(sel, {3, 4})

    def test_already_at_the_top_is_a_no_op(self):
        self.assertIsNone(self._move(self.ABC, {0, 1}, "top"))
        self.assertIsNone(self._move(self.ABC, {3, 4}, "bottom"))

    def test_an_empty_selection_moves_nothing(self):
        self.assertIsNone(self._move(self.ABC, set(), "up"))
        self.assertIsNone(self._move([], {0}, "up"))

class TestEditorExitReload(unittest.TestCase):
    """Leaving the playlist editor left the page underneath showing the
    order and membership from before the edits."""

    def setUp(self):
        self.src = FakeSource()
        self.src.get_playlist_items = lambda srv, pid: [{"Id": "fresh"}]
        self.b = MpvtkBrowser(app=None, source=self.src,
                              controller=FakeController())
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def test_going_back_refetches_the_playlist(self):
        self.b.nav_stack = [
            {"kind": "playlist", "server": "srv1", "item_id": "P",
             "title": "Mix", "_data": [{"Id": "stale"}]},
            {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
             "title": "Mix", "_items": []},
        ]
        self.b.go_back()
        self.assertEqual([i["Id"] for i in self.b.route["_data"]], ["fresh"])

    def test_jumping_back_past_the_editor_refetches_it_too(self):
        """The history menu (right-click Back) jumps rather than pressing
        Back N times, and it used to reload only Home — so picking the
        playlist out of the menu showed the pre-edit membership as fresh,
        with removed tracks still listed and clickable, while pressing Back
        for the same move refetched. Both go through _land_back now."""
        self.b.nav_stack = [
            {"kind": "home", "server": "srv1"},
            {"kind": "playlist", "server": "srv1", "item_id": "P",
             "title": "Mix", "_data": [{"Id": "stale"}]},
            {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
             "title": "Mix", "_items": []},
        ]
        self.b.go_back_to(2)                 # the playlist, past the editor
        self.assertEqual(self.b.route["kind"], "playlist")
        self.assertEqual([i["Id"] for i in self.b.route["_data"]], ["fresh"])

    def test_a_jump_that_steps_over_the_editor_from_further_in(self):
        """The editor need not be the page directly left: a jump can clear
        several at once, and any of them being the editor makes what is
        underneath stale."""
        self.b.nav_stack = [
            {"kind": "playlist", "server": "srv1", "item_id": "P",
             "title": "Mix", "_data": [{"Id": "stale"}]},
            {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
             "title": "Mix", "_items": []},
            {"kind": "detail", "server": "srv1", "item_id": "m1"},
        ]
        self.b.go_back_to(1)
        self.assertEqual([i["Id"] for i in self.b.route["_data"]], ["fresh"])

    def test_other_pages_are_not_refetched(self):
        self.b.nav_stack = [
            {"kind": "detail", "server": "srv1", "item_id": "m1",
             "_data": {"item": {"Id": "m1"}}},
            {"kind": "playlist", "server": "srv1", "item_id": "P",
             "_data": []},
        ]
        self.b.go_back()
        self.assertIsNotNone(self.b.route.get("_data"))

class TestEditFailuresAreVisible(unittest.TestCase):
    """_edit used to log and return, which silently defeated every
    caller's error path — a failed delete still ran the SUCCESS handler
    and navigated away from a playlist that still existed."""

    def test_edit_raises_so_callers_can_react(self):
        import jellyfin_mpv_shim.clients as clients_mod
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        real = clients_mod.clientManager.clients
        clients_mod.clientManager.clients = {}
        self.addCleanup(lambda: setattr(clients_mod.clientManager,
                                        "clients", real))
        with self.assertRaises(Exception):
            PlayerGateway().playlist_delete("srv1", "P")

    def test_a_failed_delete_keeps_you_on_the_playlist(self):
        ctl = FakeController()

        def boom(srv, pid):
            raise RuntimeError("no server connection")

        ctl.playlist_delete = boom
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b.nav_stack = [
            {"kind": "playlist", "server": "srv1", "item_id": "P"},
            {"kind": "playlist_edit", "server": "srv1", "item_id": "P"},
        ]
        editor_page(b, b.route)._delete()
        self.assertEqual(b.route["kind"], "playlist_edit",
                         "navigated away from a playlist that still exists")
        self.assertIn("could not be deleted", b.status)

    def test_a_failed_add_to_playlist_says_so(self):
        ctl = FakeController()

        def boom(srv, pid, ids):
            raise RuntimeError("rejected")

        ctl.playlist_add = boom
        src = FakeSource()
        src.get_playlists = lambda srv: [{"Id": "p1", "Name": "Mix"}]
        b = MpvtkBrowser(app=None, source=src, controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        b._open_add_to({"Id": "m1", "Type": "Movie"})
        _n, h = build_scene(b)
        h["add-pl-0"]["click"]()
        self.assertIn("could not be applied", b.status,
                      "a rejected add looked exactly like a successful one")

class TestCollectionReachability(unittest.TestCase):
    """Gating the Collections button on having collections meant you could
    never create your first one."""

    def _browser(self, collections, offline=False):
        src = FakeSource()
        src.get_playlists = lambda srv: []
        src.get_collections = lambda srv: list(collections)
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b._offline = offline
        b._open_add_to({"Id": "m1", "Type": "Movie"})
        return b

    def test_reachable_with_no_collections_yet(self):
        b = self._browser([])
        _n, h = build_scene(b)
        self.assertIn("add-collections", h, "cannot create a first collection")
        h["add-collections"]["click"]()
        nodes, _h = build_scene(b)
        self.assertIn("addcol-newname", ids(nodes))

    def test_hidden_offline(self):
        b = self._browser([{"Id": "c1", "Name": "Set"}], offline=True)
        _n, h = build_scene(b)
        self.assertNotIn("add-collections", h)

    def test_a_collection_holds_the_album_not_its_tracks(self):
        """Tk resolves container ids for playlists only."""
        added = []
        ctl = FakeController()
        ctl.collection_add = lambda s, c, ids: added.append(list(ids))
        src = FakeSource()
        src.get_playlists = lambda srv: []
        src.get_collections = lambda srv: [{"Id": "c1", "Name": "Set"}]
        src.get_album_tracks = lambda srv, aid: [{"Id": "t1"}, {"Id": "t2"}]
        b = MpvtkBrowser(app=None, source=src, controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        b._open_add_to({"Id": "a1", "Type": "MusicAlbum"})
        _n, h = build_scene(b)
        h["add-collections"]["click"]()
        _n, h = build_scene(b)
        h["add-col-0"]["click"]()
        self.assertEqual(added, [["a1"]], "inserted every track instead")

class TestOptimisticRollback(unittest.TestCase):
    """Every optimistic editor now puts its change back when the server
    refuses. _pe_move was the only one that did."""

    def _browser(self, **ctl_attrs):
        ctl = FakeController()
        for k, v in ctl_attrs.items():
            setattr(ctl, k, v)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        return b

    @staticmethod
    def _boom(*a, **k):
        raise RuntimeError("server refused")

    def test_remove_puts_the_rows_back(self):
        b = self._browser(playlist_remove=self._boom)
        items = [{"Id": "a", "PlaylistItemId": "e1"},
                 {"Id": "b", "PlaylistItemId": "e2"}]
        route = {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
                 "_items": list(items), "_sel": {0}}
        b.nav_stack = [route]
        editor_page(b, route)._remove()
        self.assertEqual([i["Id"] for i in route["_items"]], ["a", "b"],
                         "rows stayed gone after a refused remove")

    def test_rename_reverts(self):
        b = self._browser(playlist_update=self._boom)
        route = {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
                 "title": "Old", "_newname": "New"}
        b.nav_stack = [route]
        editor_page(b, route)._rename()
        self.assertEqual(route["title"], "Old")

    def test_visibility_reverts(self):
        """The difference between private and public must not be left
        showing a value the server rejected."""
        b = self._browser(playlist_update=self._boom)
        route = {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
                 "_public": False, "_public_known": True}
        b.nav_stack = [route]
        editor_page(b, route)._toggle_public()
        self.assertFalse(route["_public"])

    def test_queue_reorder_reverts(self):
        b = self._browser(queue_reorder=self._boom)
        entries = [{"item": {"Id": "a"}, "pid": "p1"},
                   {"item": {"Id": "b"}, "pid": "p2"}]
        route = {"kind": "queue", "server": "srv1",
                 "_data": {"entries": list(entries), "current_id": "a"},
                 "_sel": {0}}
        b.nav_stack = [route]
        editor_page(b, route)._move("down")
        self.assertEqual([e["pid"] for e in route["_data"]["entries"]],
                         ["p1", "p2"], "queue kept an order the player refused")

    def test_a_successful_edit_keeps_the_change(self):
        b = self._browser(playlist_update=lambda *a, **k: None)
        route = {"kind": "playlist_edit", "server": "srv1", "item_id": "P",
                 "title": "Old", "_newname": "New"}
        b.nav_stack = [route]
        editor_page(b, route)._rename()
        self.assertEqual(route["title"], "New")

class TestRollbackSurvivesNavigation(unittest.TestCase):
    """A rollback must land even if the user navigated away while the edit
    was in flight.

    run_async used to epoch-gate on_error the same way it gates on_done, so
    walking off the screen dropped the rollback: the route dict kept the
    change the server had refused, and coming back showed it. The whole
    TestOptimisticRollback suite above missed this because _SyncPool runs
    the work inline, which never gives the epoch a chance to move.
    """

    def _browser(self, **ctl_attrs):
        ctl = FakeController()
        for k, v in ctl_attrs.items():
            setattr(ctl, k, v)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _DeferredPool()
        return b

    def _pagers(self):
        """(name, route, scroll_fn) per paging route still reachable here:
        the windowed grid and the songs tab, which still appends."""
        items = [{"Id": "i%d" % i, "Name": "N%d" % i} for i in range(20)]
        b = self.b
        return [
            ("grid",
             {"kind": "grid", "server": "srv1", "parent_id": "lib1",
              "_items": list(items), "_total": 99},
             lambda r: grid_scroll(b, r, 0, 100)),
            ("songs",
             {"kind": "music", "server": "srv1", "parent_id": "lib1",
              "_tab": "songs", "_data": list(items), "_total": 99},
             lambda r: music_songs_scroll(b, r, 0, 100)),
        ]

    def setUp(self):
        self.b = self._browser()
        self.b.server = "srv1"

    @staticmethod
    def _boom(*a, **k):
        raise RuntimeError("server refused")

    @staticmethod
    def _edit_route():
        return {"kind": "playlist_edit", "server": "srv1", "item_id": "PL1",
                "title": "Faves",
                "_items": [{"Id": "a", "Name": "Alpha", "PlaylistItemId": "e1"},
                           {"Id": "b", "Name": "Beta", "PlaylistItemId": "e2"}],
                "_sel": {0}}

    def test_rollback_lands_after_navigating_away(self):
        b = self._browser(playlist_remove=self._boom)
        route = self._edit_route()
        b.nav_stack = [route]
        editor_page(b, route)._remove()
        self.assertEqual([i["Id"] for i in route["_items"]], ["b"])
        # walk away -> the epoch moves -> only *then* does the call fail
        b.navigate({"kind": "home", "server": "srv1"})
        b._pool.drain()
        self.assertEqual([i["Id"] for i in route["_items"]], ["a", "b"],
                         "rollback was dropped because the epoch moved")

    def test_the_refused_row_is_back_on_screen_when_you_return(self):
        """The dict assertion above is only worth having next to this one:
        what matters is that the user does not come back to a playlist
        showing an entry the server refused to remove."""
        b = self._browser(playlist_remove=self._boom)
        route = self._edit_route()
        b.nav_stack = [route]
        editor_page(b, route)._remove()
        b.navigate({"kind": "home", "server": "srv1"})
        b._pool.drain()
        b.go_back()
        nodes, _h = build_scene(b)
        labels = [n.get("text") for n in nodes if n.get("text")]
        self.assertTrue(any("Alpha" in t for t in labels),
                        "returned to a playlist missing a row the server kept")

    def test_a_dropped_page_still_clears_the_paging_guard(self):
        """_loading is set before dispatch and used to be cleared only in
        on_done — which run_async skips wholesale when the epoch moved. Scroll
        to the bottom, click into an item, come back, and the list could never
        page again for the rest of the session."""
        for view, route, scroll in self._pagers():
            with self.subTest(view=view):
                b = self.b
                b.nav_stack = [route]
                scroll(route)
                guard = "_win_load" if view == "grid" else "_loading"
                self.assertTrue(route[guard], "no page was dispatched")
                b.navigate({"kind": "home", "server": "srv1"})
                b._pool.drain()          # the page lands, stale, and is dropped
                self.assertFalse(route.get(guard),
                                 "the paging guard survived a stale page")

    def test_a_dropped_downloads_load_clears_its_guard_too(self):
        """Same shape in the downloads panel, where a stuck _dl_loading makes
        every later render of the tab return early."""
        b = self._browser()
        b.controller.list_downloads = lambda: [{"kind": "movies", "id": None,
                                                "title": "M", "size": 0,
                                                "count": 0, "children": []}]
        route = {"kind": "settings", "server": "srv1", "_tab": "downloads"}
        b.nav_stack = [route]
        b._load_downloads(route)
        self.assertTrue(route["_dl_loading"])
        b.navigate({"kind": "home", "server": "srv1"})
        b._pool.drain()
        self.assertFalse(route.get("_dl_loading"),
                         "the downloads panel can never reload itself again")

    def test_a_stale_home_failure_does_not_yank_you_offline(self):
        """The rollback half of on_error is ungated on purpose, but
        _offline_fallback is not a rollback: set_source() throws the nav stack
        away and drops you on the offline home. Against a server that hangs
        rather than refuses, that failure can land tens of seconds after the
        user has moved on."""
        b = self._browser()
        b.source.get_libraries = self._boom
        offline = FakeSource()
        b.controller.offline_source = lambda: offline
        home = {"kind": "home", "server": "srv1"}
        b.nav_stack = [home]
        b._load_route(home)
        b.navigate({"kind": "settings", "server": "srv1", "_tab": "general"})
        b._pool.drain()
        self.assertEqual([r["kind"] for r in b.nav_stack],
                         ["home", "settings"],
                         "a stale home failure discarded the nav stack")
        self.assertIsNot(b.source, offline,
                         "swapped the data source behind the user's back")
        self.assertIsNotNone(home.get("_error"),
                             "the error was not recorded on the home route")

    def test_the_fallback_still_fires_while_home_is_on_screen(self):
        """The guard must not defeat the feature it guards."""
        b = self._browser()
        b.source.get_libraries = self._boom
        offline = FakeSource()
        b.controller.offline_source = lambda: offline
        home = {"kind": "home", "server": "srv1"}
        b.nav_stack = [home]
        b._load_route(home)
        b._pool.drain()
        self.assertIs(b.source, offline, "never fell back to the downloads")

    def test_a_superseded_failure_does_not_land_on_a_route_that_reloaded(self):
        """The guard above is an identity test — "is this route the screen" —
        and it was sound only because a superseded load's route was always one
        the user had navigated OFF. The Home button re-navigates the route
        dict it finds in the stack (``go_home``), so the stale load can be
        holding the screen again.

        Against a server that hangs, the first request times out half a minute
        later, by which time Home has been pressed and has loaded fine: the
        late failure would write an error over a working screen and, for
        anyone with downloads, swap the whole session onto the offline
        catalog."""
        b = self._browser()
        offline = FakeSource()
        b.controller.offline_source = lambda: offline
        home = {"kind": "home", "server": "srv1"}
        b.nav_stack = [home]

        # Two source OBJECTS, not one with a patched method: HomePage.load
        # captures ``self.ctx.source`` and calls through it, so patching the
        # attribute in place would heal the in-flight load as well and the
        # failure being tested would never happen.
        hung = FakeSource()
        hung.get_libraries = self._boom
        b.source = hung
        b._load_route(home)
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1"})

        # ...and now Home, which puts the SAME dict back and loads it fine.
        b.source = FakeSource()
        b.go_home()
        self.assertIs(b.route, home)
        b._pool.release_last()                     # the good load lands
        self.assertIsNotNone(home.get("_data"), "home did not load")

        b._pool.drain()                            # the hung one finally dies
        self.assertIsNot(b.source, offline,
                         "a stale failure dropped a working session offline")
        self.assertIsNone(home.get("_error"),
                          "a stale failure errored a screen that had loaded")
        self.assertEqual([r["kind"] for r in b.nav_stack], ["home"])

    def test_a_stale_page_failure_does_not_toast_over_another_screen(self):
        b = self._browser()
        b.source.get_library_items = self._boom
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "_items": [{"Id": "x"}], "_total": 99}
        b.nav_stack = [route]
        grid_scroll(b, route, 0, 100)
        b.navigate({"kind": "settings", "server": "srv1", "_tab": "general"})
        b.status = ""
        b._pool.drain()
        self.assertEqual(b.status, "",
                         "toasted about a list the user had left")
        self.assertFalse(route.get("_loading"),
                         "the guard must still be cleared")

    def test_a_stale_success_is_still_discarded(self):
        """Only on_error is ungated. A *successful* late reply must still be
        dropped, or a slow load lands on top of the screen you moved to."""
        b = self._browser()
        route = {"kind": "playlist", "server": "srv1", "item_id": "P",
                 "title": "Mix"}
        b.nav_stack = [route]
        b._load_route(route)
        b.navigate({"kind": "home", "server": "srv1"})
        b._pool.drain()
        self.assertIsNone(route.get("_data"),
                          "a superseded load applied its result anyway")

    def test_a_failed_window_can_be_asked_for_again(self):
        """The in-flight guard is cleared by `always`. Dropping that left the
        grid unable to request that window for the rest of the session."""
        b = self._browser()
        b.source.get_library_items = self._boom
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "_items": [{"Id": "x"}], "_total": 99}
        b.nav_stack = [route]
        grid_scroll(b, route, 10_000, 10_000)
        self.assertTrue(route["_win_load"], "the window was never dispatched")
        b.navigate({"kind": "home", "server": "srv1"})
        b._pool.drain()
        self.assertFalse(route.get("_win_load"),
                         "the in-flight guard survived a failure")


if __name__ == "__main__":
    unittest.main()


class BackToAnUnfinishedLoadRefetchesTest(unittest.TestCase):
    """Going Back to a page whose fetch never landed must re-issue it.

    A page can be left before its load returns -- the always-visible search
    field submits, which bumps the epoch and drops the in-flight result on the
    floor. Nothing then re-issues it: the render path spins on a route with no
    data, no error and no outstanding request, forever.

    `_land_forward` has had this recovery, and its comment argued the Back
    case was impossible -- "going *back* to such a page is impossible (it was
    never below you)". True only when the epoch bump came from *leaving* the
    page; the search field bumps it while the page is still below you.
    """

    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "home", "server": "srv1"}, reset=True)
        return b

    def test_back_to_a_page_that_never_loaded_re_issues_it(self):
        b = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1"})
        # The grid's fetch was dropped by an epoch bump: no data, no items,
        # no error, and no request outstanding.
        for key in ("_data", "_items", "_error", "_loading"):
            b.route.pop(key, None)
        b.navigate({"kind": "search", "server": "srv1"})
        b.go_back()

        self.assertEqual(b.route["kind"], "grid")
        self.assertTrue(
            b.route.get("_items") is not None or b.route.get("_data")
            is not None or b.route.get("_loading") or b.route.get("_error"),
            "Back landed on a page with no data, no error and nothing in "
            "flight -- a spinner that never resolves")

    def test_a_page_holding_an_error_is_left_alone(self):
        """The other direction. `_route_async`'s failure handler is
        deliberately NOT epoch-gated, because an error is a rollback and a
        route you navigated away from must still be holding it when you come
        back. Refetching here would discard exactly that."""
        b = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1"})
        for key in ("_data", "_items", "_loading"):
            b.route.pop(key, None)
        b.route["_error"] = "Failed to load."
        b.navigate({"kind": "search", "server": "srv1"})
        b.go_back()

        self.assertEqual(b.route.get("_error"), "Failed to load.",
                         "Back threw away the error the page was holding")


class TwoLoadsAtOneEpochAreDistinguishableTest(unittest.TestCase):
    """A load that fails late must not write over a newer one that succeeded.

    `_route_async` stamped the ASYNC EPOCH as "which load owns this route's
    outcome". Its own comment explains what that guard is for: a hung server's
    request timing out half a minute later must not write an error over a home
    screen that has since loaded fine, and -- for anyone with downloads --
    drop them onto the offline catalog from a working screen.

    But §4's refresh is deliberately a load, not a *re*load: `refresh_home`
    calls `_load_route` with no `_bump_epoch()`, because bumping would cancel
    everything else in flight. Two such loads therefore stamp the same value,
    the guard compares equal, and it does not fire. Its only other overlap
    check is `route.get("_loading")`, and the sole writer of that in the tree
    is `pagination.py` (infinite scroll) -- Home does not page, so it is
    vacuous. `refresh_live_tv` has its own `_refreshing` marker for exactly
    this.
    """

    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _DeferredPool()
        b.server = "srv1"
        return b

    def test_a_late_failure_does_not_overwrite_a_newer_success(self):
        b = self._browser()
        route = b.route
        epoch = b._epoch

        boom = RuntimeError("the server hung, then gave up")
        # Load A: dispatched first, answers last. Load B: the refresh that
        # follows it at the SAME epoch, which is what refresh_home does.
        b._route_async(route, lambda: (_ for _ in ()).throw(boom),
                       lambda data: route.update(_data=data), epoch)
        b._route_async(route, lambda: {"ok": True},
                       lambda data: route.update(_data=data), epoch)

        b._pool.release(1)          # B lands: the screen is correct
        self.assertEqual(route.get("_data"), {"ok": True})

        b._pool.release(0)          # A finally fails
        self.assertIsNone(
            route.get("_error"),
            "the older load's failure was written over a screen that had "
            "already loaded fine")
        self.assertEqual(route.get("_data"), {"ok": True})

    def test_an_older_SUCCESS_does_not_overwrite_a_newer_one(self):
        """The mirror of the case above, and the half the first fix missed.

        `run_async` gates on_done by EPOCH, and a refresh deliberately shares
        one -- so gating only the failure left the success path open: an older
        load answering after a newer one put its stale rows back. That is
        #560's just-watched episode reappearing in Continue Watching.
        """
        b = self._browser()
        route, epoch = b.route, b._epoch
        b._route_async(route, lambda: {"version": "old"},
                       lambda d: route.update(_data=d), epoch)
        b._route_async(route, lambda: {"version": "new"},
                       lambda d: route.update(_data=d), epoch)
        b._pool.release(1)
        b._pool.release(0)
        self.assertEqual(
            route.get("_data"), {"version": "new"},
            "an older load's result overwrote the newer refresh")

    def test_the_newest_load_can_still_report_its_own_failure(self):
        """The control: distinguishing the two must not stop a genuine
        failure being shown, or the view spins instead of offering a retry."""
        b = self._browser()
        route = b.route
        boom = RuntimeError("nope")
        b._route_async(route, lambda: (_ for _ in ()).throw(boom),
                       lambda data: route.update(_data=data), b._epoch)
        b._pool.release(0)
        self.assertTrue(route.get("_error"),
                        "a failing load reported nothing, so the view has no "
                        "error to show and no retry to offer")
