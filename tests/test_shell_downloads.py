"""Offline downloads: the panel, the dialog, and the state behind them.
"""

import unittest
import threading
import time
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    DownloadsController,
    FakeConfig,
    FakeController,
    FakeSource,
    _NeverPool,
    _RecordingPool,
    _SyncPool,
    build_scene,
    ids,
    menu_pick,
)


class TestDownloadDialog(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_download_dialog_shows_estimate_and_enqueues(self):
        self.b._open_download({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        nodes, handlers = build_scene(self.b)
        self.assertIn("dl-ok", ids(nodes))
        self.assertEqual(self.b._dl["est"]["count"], 3)   # estimate fetched
        handlers["dl-ok"]["click"]()
        # `enqueued`, not the catch-all recorder: download_enqueue is a
        # declared method on the fake now, because it has to WRITE a row
        # (books read the catalog back to decide what their buttons say).
        self.assertTrue(self.ctl.enqueued)
        self.assertIsNone(self.b._dl)

    def test_a_single_item_never_filters_out_watched(self):
        """include_watched is a container filter. Applied to one item it
        means "enqueue nothing" whenever that item is already played —
        which it did, silently."""
        self.b._open_download({"Id": "m1", "Type": "Movie"})
        self.assertTrue(self.b._dl["watched"])
        nodes, _h = build_scene(self.b)
        self.assertNotIn("dl-watched", ids(nodes),
                         "offered a filter that can only break the download")

    def test_a_container_offers_the_filter(self):
        self.b._open_download({"Id": "s1", "Type": "Series"})
        nodes, _h = build_scene(self.b)
        self.assertIn("dl-watched", ids(nodes))

    def test_download_include_watched_toggles(self):
        self.b._open_download({"Id": "s1", "Type": "Series"})
        self.assertFalse(self.b._dl["watched"])
        self.b._dl_toggle_watched()
        self.assertTrue(self.b._dl["watched"])

    def test_confirm_is_withheld_until_the_estimate_lands(self):
        """Confirming during "Estimating…" loses the audio_only default."""
        self.b._pool = _NeverPool()
        self.b._open_download({"Id": "s1", "Type": "Series"})
        nodes, _h = build_scene(self.b)
        self.assertNotIn("dl-ok", ids(nodes))

    def test_download_cancel_clears_state(self):
        self.b._open_download({"Id": "m1", "Type": "Movie"})
        self.b._close_download()
        self.assertIsNone(self.b._dl)
        self.assertIsNone(self.b._dialog)

    def test_menu_download_opens_dialog(self):
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        menu_pick(self.b, "download")
        self.assertIsNone(self.b._menu)
        self.assertIsNotNone(self.b._dl)

class TestDownloadsPanel(unittest.TestCase):
    def setUp(self):
        self.ctl = DownloadsController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()
        self.b.open_settings("downloads")
        # First build kicks off the (inline) catalog load and shows a
        # spinner; the second renders the tree.
        build_scene(self.b)

    def test_tree_is_indented_by_level(self):
        """Series > season > episode each start further right."""
        nodes, _h = build_scene(self.b)
        text_x = {n["text"]: n["x"] for n in nodes if n["t"] == "text"}
        self.assertIn("The Show", text_x)
        self.assertIn("Season 1", text_x)
        self.assertIn("1. Pilot", text_x)
        self.assertLess(text_x["The Show"], text_x["Season 1"])
        self.assertLess(text_x["Season 1"], text_x["1. Pilot"])
        self.assertEqual(text_x["Season 1"] - text_x["The Show"],
                         self.b.INDENT)

    def test_every_level_can_be_deleted(self):
        _n, h = build_scene(self.b)
        for nid in ("dl-g1-rm", "dl-g1-s0-rm", "dl-g1-s0-e0-rm"):
            self.assertIn(nid, h, nid)

    def test_deleting_a_series_passes_series_id(self):
        _n, h = build_scene(self.b)
        h["dl-g1-rm"]["click"]()          # opens the confirm dialog
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.ctl.deleted, [(None, "sh1", None, None)])

    def test_deleting_an_episode_passes_item_id(self):
        _n, h = build_scene(self.b)
        h["dl-g1-s0-e0-rm"]["click"]()
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.ctl.deleted, [("e1", None, None, None)])

    def test_loose_movies_group_renders(self):
        """Items with no series land in one flat group at the end."""
        nodes, _h = build_scene(self.b)
        texts = [n["text"] for n in nodes if n["t"] == "text"]
        self.assertIn("Movies & Videos", texts)
        self.assertIn("A Movie", texts)
        self.assertIn("dl-g2-i0-rm", ids(nodes))

    def test_pending_items_show_their_status_in_words(self):
        """The raw catalog values were rendered verbatim and untranslated —
        "pending", "downloading". Tk turned them into "Queued" and
        "Downloading 42%", which is the difference between a status column
        and a debug dump."""
        nodes, _h = build_scene(self.b)
        texts = [n["text"] for n in nodes if n["t"] == "text"]
        self.assertTrue(any("Queued" in t for t in texts),
                        "no friendly status: %r" % texts)
        self.assertFalse(any("pending" in t for t in texts),
                         "raw catalog value on screen")
        # A completed item shows only its size, not "complete".
        self.assertFalse(any("complete" in t for t in texts))

class TestDownloadsGrouping(unittest.TestCase):
    """The controller's grouping is where the 0 B / music-spam problems were,
    so exercise it against a fake catalog rather than only the view."""

    def _controller(self, rows, playlists=(), owned=None):
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        class FakeDB:
            def list(self_inner):
                return list(rows)

            def list_playlists(self_inner):
                return list(playlists)

            def playlist_item_rows(self_inner, pid):
                return [r for r in rows if r.get("_pl") == pid]

            def playlist_ownership(self_inner):
                return dict(owned or {})

        class FakeSync:
            db = FakeDB()

        import jellyfin_mpv_shim.sync.manager as mgr
        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))
        return PlayerGateway()

    def test_size_comes_from_the_real_columns(self):
        """The catalog stores size_bytes/downloaded_bytes; reading a "size"
        key showed 0 B for everything."""
        ctl = self._controller([
            {"item_id": "m1", "name": "A Movie", "status": "complete",
             "downloaded_bytes": 1024 * 1024, "size_bytes": 2 * 1024 * 1024},
        ])
        groups = ctl.list_downloads()
        self.assertEqual(groups[0]["size"], 1024 * 1024)

    def test_falls_back_to_expected_size_before_download_starts(self):
        ctl = self._controller([
            {"item_id": "m1", "name": "Queued", "status": "pending",
             "downloaded_bytes": 0, "size_bytes": 4096},
        ])
        self.assertEqual(ctl.list_downloads()[0]["size"], 4096)

    def test_playlists_are_collapsed_and_own_their_items(self):
        rows = [{"item_id": "t%d" % i, "name": "Track %d" % i,
                 "type": "Audio", "status": "complete",
                 "downloaded_bytes": 100, "_pl": "PL1"} for i in range(200)]
        ctl = self._controller(
            rows, playlists=[{"playlist_id": "PL1", "name": "Road Trip"}],
            owned={r["item_id"]: "PL1" for r in rows})
        groups = ctl.list_downloads()
        self.assertEqual(len(groups), 1, "tracks must not also list loose")
        pl = groups[0]
        self.assertEqual(pl["kind"], "playlist")
        self.assertEqual(pl["count"], 200)
        self.assertEqual(pl["size"], 200 * 100)
        self.assertEqual(pl["children"], [], "collapsed, not 200 rows")

    def test_video_playlists_list_their_items(self):
        """A playlist of films is a handful of rows, and the whole point of
        having it in the manager is removing one of them."""
        rows = [{"item_id": "m1", "name": "First", "type": "Movie",
                 "status": "complete", "downloaded_bytes": 100, "_pl": "PL1"},
                {"item_id": "m2", "name": "Second", "type": "Video",
                 "status": "complete", "downloaded_bytes": 200, "_pl": "PL1"}]
        ctl = self._controller(
            rows, playlists=[{"playlist_id": "PL1", "name": "Movie Night"}],
            owned={r["item_id"]: "PL1" for r in rows})
        groups = ctl.list_downloads()
        self.assertEqual(len(groups), 1, "items must not also list loose")
        pl = groups[0]
        self.assertEqual(pl["count"], 2)
        self.assertEqual([c["title"] for c in pl["children"]],
                         ["First", "Second"])
        self.assertEqual([c["id"] for c in pl["children"]], ["m1", "m2"])

    def test_mixed_and_untyped_playlists_stay_collapsed(self):
        """One video among the tracks doesn't make it a video playlist, and
        a row with no type must not be guessed into one."""
        mixed = [{"item_id": "a1", "name": "Track", "type": "Audio",
                  "status": "complete", "downloaded_bytes": 1, "_pl": "PL1"},
                 {"item_id": "v1", "name": "Clip", "type": "Video",
                  "status": "complete", "downloaded_bytes": 1, "_pl": "PL1"}]
        ctl = self._controller(
            mixed, playlists=[{"playlist_id": "PL1", "name": "Mixed"}],
            owned={r["item_id"]: "PL1" for r in mixed})
        self.assertEqual(ctl.list_downloads()[0]["children"], [])

        untyped = [{"item_id": "u1", "name": "?", "status": "complete",
                    "downloaded_bytes": 1, "_pl": "PL2"}]
        ctl = self._controller(
            untyped, playlists=[{"playlist_id": "PL2", "name": "Old"}],
            owned={r["item_id"]: "PL2" for r in untyped})
        self.assertEqual(ctl.list_downloads()[0]["children"], [])

    def test_series_nest_seasons_and_episodes(self):
        ctl = self._controller([
            {"item_id": "e1", "name": "Pilot", "series_id": "sh1",
             "series_name": "Show", "season_id": "s1", "parent_index": 1,
             "index_number": 1, "downloaded_bytes": 10, "status": "complete"},
            {"item_id": "e2", "name": "Two", "series_id": "sh1",
             "series_name": "Show", "season_id": "s1", "parent_index": 1,
             "index_number": 2, "downloaded_bytes": 20, "status": "complete"},
        ])
        show = ctl.list_downloads()[0]
        self.assertEqual(show["kind"], "series")
        self.assertEqual(show["size"], 30)
        self.assertEqual(show["count"], 2)
        self.assertEqual(len(show["children"]), 1)
        self.assertEqual(len(show["children"][0]["children"]), 2)

class TestDownloadStatusBar(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def test_hidden_when_nothing_is_downloading(self):
        self.b.set_download_status(None)
        nodes, _h = build_scene(self.b)
        self.assertNotIn("dlbar-view", ids(nodes))

    def test_shows_progress_and_a_way_into_the_manager(self):
        self.b.set_download_status({"pending": 3, "name": "Pilot",
                                    "percent": 42})
        nodes, h = build_scene(self.b)
        self.assertIn("dlbar-view", ids(nodes))
        texts = " ".join(n.get("text", "") for n in nodes if n["t"] == "text")
        self.assertIn("Pilot", texts)
        self.assertIn("42%", texts)
        h["dlbar-view"]["click"]()
        self.assertEqual(self.b.route["kind"], "settings")
        self.assertEqual(self.b.route["_tab"], "downloads")

    def test_unknown_percentage_still_shows_the_bar(self):
        self.b.set_download_status({"pending": 1, "name": "X",
                                    "percent": None})
        nodes, _h = build_scene(self.b)
        self.assertIn("dlbar-view", ids(nodes))

class TestDownloadsGroupDelete(unittest.TestCase):
    """The flat "Movies & Videos" group has no server-side id, so its
    Remove button must enumerate its own rows. Passing no scope reached
    syncManager.delete() with every id None — the whole catalog."""

    def test_a_group_without_an_id_deletes_only_its_own_rows(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        group = {"kind": "movies", "id": None, "title": "Movies & Videos",
                 "children": [{"kind": "item", "id": "m1"},
                              {"kind": "item", "id": "m2"}]}
        self.assertEqual(b._dl_group_item_ids(group), ["m1", "m2"])

    def test_season_rows_are_collected_from_nested_children(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        group = {"kind": "series", "id": "sh1", "children": [
            {"kind": "season", "id": "s1", "children": [
                {"kind": "item", "id": "e1"}, {"kind": "item", "id": "e2"}]}]}
        self.assertEqual(b._dl_group_item_ids(group), ["e1", "e2"])

    def test_an_empty_group_yields_no_ids(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        self.assertEqual(b._dl_group_item_ids({"kind": "movies"}), [])

class TestRemoveWatchedDownloads(unittest.TestCase):
    """"Reclaim space on a finished show" — delete only the watched
    downloads in a scope, keeping what hasn't been watched. The sync
    manager supported watched_only all along; mpvtk never offered it."""

    def setUp(self):
        self.ctl = DownloadsController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.open_settings("downloads")
        build_scene(self.b)

    def test_a_series_offers_remove_watched(self):
        _n, h = build_scene(self.b)
        self.assertIn("dl-g1-rmw", h, "no Remove Watched on a series")

    def test_it_deletes_only_the_watched_ones(self):
        _n, h = build_scene(self.b)
        h["dl-g1-rmw"]["click"]()
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.ctl.deleted, [(None, "sh1", None, None)])
        self.assertEqual(self.ctl.deleted_watched_only, [True])

    def test_plain_remove_is_unaffected(self):
        _n, h = build_scene(self.b)
        h["dl-g1-rm"]["click"]()
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.ctl.deleted_watched_only, [False])

    def test_a_flat_group_has_no_watched_sweep(self):
        """The Movies group has no server-side scope, so a watched sweep
        there would have to enumerate — not offered rather than wrong."""
        _n, h = build_scene(self.b)
        self.assertNotIn("dl-g2-rmw", h)

    def test_individual_items_have_no_watched_sweep(self):
        _n, h = build_scene(self.b)
        self.assertNotIn("dl-g1-s0-e0-rmw", h)

class TestOrphanedDownloadOwnership(unittest.TestCase):
    """An ownership row can outlive its playlist. Skipping owned rows
    unconditionally made those downloads invisible AND undeletable —
    disk used with no UI path to reclaim it."""

    def _controller(self, rows, playlists=(), owned=None):
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        class FakeDB:
            def list(self_inner):
                return list(rows)

            def list_playlists(self_inner):
                return list(playlists)

            def playlist_item_rows(self_inner, pid):
                return [r for r in rows if r.get("_pl") == pid]

            def playlist_ownership(self_inner):
                return dict(owned or {})

        class FakeSync:
            db = FakeDB()

        import jellyfin_mpv_shim.sync.manager as mgr
        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))
        return PlayerGateway()

    def test_an_orphaned_row_still_appears(self):
        rows = [{"item_id": "m1", "name": "A Movie", "type": "Movie",
                 "status": "complete", "downloaded_bytes": 100}]
        ctl = self._controller(rows, playlists=[],
                               owned={"m1": "GONE"})
        groups = ctl.list_downloads()
        titles = [c.get("title") for g in groups
                  for c in (g.get("children") or [])]
        self.assertIn("A Movie", titles,
                      "download vanished with its playlist record")

    def test_a_live_playlist_still_owns_its_rows(self):
        rows = [{"item_id": "t1", "name": "Track", "type": "Audio",
                 "status": "complete", "downloaded_bytes": 100,
                 "_pl": "PL1"}]
        ctl = self._controller(
            rows, playlists=[{"playlist_id": "PL1", "name": "Mix"}],
            owned={"t1": "PL1"})
        groups = ctl.list_downloads()
        self.assertEqual([g["kind"] for g in groups], ["playlist"],
                         "owned row also listed loose")

class TestRemoveDownloadButton(unittest.TestCase):
    """The action row always said "Download", so pressing it on a complete
    item did nothing visible and there was no way to reclaim the space
    outside Settings -> Downloads."""

    def setUp(self):
        self.ctl = FakeController()
        self.deleted = []
        self.ctl.delete_download = lambda **kw: self.deleted.append(kw)
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _btns(self, item):
        from jellyfin_mpv_shim.mpvtk.widgets import Row

        row = self.b._common_actions(item, "srv1", "act")
        nodes, handlers = layout(Row(row), 1280, 720)
        return ids(nodes), handlers

    def test_an_undownloaded_item_offers_download(self):
        node_ids, _h = self._btns({"Id": "m1", "Type": "Movie"})
        self.assertIn("act-download", node_ids)
        self.assertNotIn("act-undownload", node_ids)

    def test_a_downloaded_item_offers_removal(self):
        self.b.tiles._downloaded = {"m1"}
        node_ids, _h = self._btns({"Id": "m1", "Type": "Movie"})
        self.assertIn("act-undownload", node_ids)
        self.assertNotIn("act-download", node_ids)

    def test_removing_deletes_by_item_id(self):
        self.b.tiles._downloaded = {"m1"}
        _n, h = self._btns({"Id": "m1", "Name": "A", "Type": "Movie"})
        h["act-undownload"]["click"]()
        _n2, h2 = build_scene(self.b)
        h2["dlg-ok"]["click"]()
        self.assertEqual(self.deleted, [{"item_id": "m1"}])

    def test_removing_a_series_deletes_by_series_id(self):
        self.b.tiles._downloaded_series = {"sh1"}
        _n, h = self._btns({"Id": "sh1", "Name": "Show", "Type": "Series"})
        h["act-undownload"]["click"]()
        _n2, h2 = build_scene(self.b)
        h2["dlg-ok"]["click"]()
        self.assertEqual(self.deleted, [{"series_id": "sh1"}])

class TestMoveDownloadsIsNotOnThePool(unittest.TestCase):
    """Relocating the download store copies the whole thing, possibly across
    drives. On the 4-worker pool it holds a worker for minutes while route
    loads queue behind it, so it gets its own thread."""

    def _browser(self, relocate):
        cfg = FakeConfig()
        cfg.relocate_downloads = relocate
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController(), config=cfg)
        b._pool = _RecordingPool()
        return b

    def _settle(self, b, timeout=5):
        t = b._long_thread
        if t is not None:
            t.join(timeout)

    def test_it_does_not_take_a_pool_worker(self):
        done = threading.Event()

        def relocate(path, progress=None):
            done.set()
            return True, "moved"

        b = self._browser(relocate)
        b._move_downloads("/new")
        self.assertTrue(done.wait(5), "the move never ran")
        self._settle(b)
        self.assertEqual(b._pool.submitted, 0,
                         "the multi-GB copy went to the shared pool")

    def test_a_second_move_says_so_instead_of_doing_nothing(self):
        release = threading.Event()
        calls = []

        def relocate(path, progress=None):
            calls.append(path)
            release.wait(5)
            return True, "moved"

        b = self._browser(relocate)
        b._move_downloads("/new")
        for _ in range(500):          # wait for the first to be in flight
            if calls:
                break
            time.sleep(0.005)
        b._move_downloads("/other")
        self.assertIn("already", b.status.lower(),
                      "a second press looked like a dead button")
        release.set()
        self._settle(b)
        self.assertEqual(calls, ["/new"], "two copies ran at once")

    def test_the_outcome_reaches_the_status_line(self):
        b = self._browser(lambda path, progress=None: (False, ""))
        b._move_downloads("/new")
        self._settle(b)
        self.assertIn("failed", b.status.lower())

    def test_a_crash_in_the_move_is_logged_not_swallowed_silently(self):
        def relocate(path, progress=None):
            raise OSError("disk full")

        b = self._browser(relocate)
        b._move_downloads("/new")
        self._settle(b)
        self.assertIn("failed", b.status.lower())
        self.assertIsNone(b._long_thread, "the slot was never released")

class TestDownloadsWatchedMarker(unittest.TestCase):
    """"Remove Watched" rendered unconditionally, and no row said which items
    it meant — so it read as a destructive guess, and on a show with nothing
    watched it silently deleted nothing."""

    def _panel(self, tree):
        ctl = FakeController()
        ctl.list_downloads = lambda: tree
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=FakeConfig())
        b._pool = _SyncPool()
        route = {"kind": "settings", "server": "srv1", "_tab": "downloads",
                 "_dl_open": {"g0"}}
        b.nav_stack = [route]
        b._load_downloads(route)
        return build_scene(b)

    @staticmethod
    def _series(watched_count, children_watched):
        return [{"kind": "series", "id": "s1", "title": "Show", "size": 10,
                 "count": len(children_watched),
                 "watched_count": watched_count,
                 "children": [{"kind": "season", "id": "a", "series_id": "s1",
                               "title": "Season 1", "size": 10,
                               "count": len(children_watched),
                               "watched_count": watched_count,
                               "children": [
                                   {"kind": "item", "id": "e%d" % i,
                                    "title": "Ep %d" % i, "status": "complete",
                                    "size": 5, "index": i, "done": 5,
                                    "total": 5, "watched": w}
                                   for i, w in enumerate(children_watched)]}]}]

    def test_a_watched_row_is_marked(self):
        nodes, _h = self._panel(self._series(1, [True, False]))
        texts = [n.get("text") or "" for n in nodes]
        self.assertTrue(any("watched" in t for t in texts),
                        "no watched marker: %r" % texts)

    def test_remove_watched_is_offered_when_something_is_watched(self):
        nodes, _h = self._panel(self._series(1, [True, False]))
        self.assertTrue(any(i.endswith("-rmw") for i in ids(nodes)),
                        "no Remove Watched despite a watched episode")

    def test_it_is_not_offered_when_nothing_is_watched(self):
        """It would delete nothing, silently."""
        nodes, _h = self._panel(self._series(0, [False, False]))
        self.assertFalse(any(i.endswith("-rmw") for i in ids(nodes)),
                         "Remove Watched offered with nothing to remove")

    def test_plain_remove_is_always_there(self):
        nodes, _h = self._panel(self._series(0, [False]))
        self.assertTrue(any(i.endswith("-rm") for i in ids(nodes)))

class TestDownloadDialogGuardsAnEmptyEstimate(unittest.TestCase):
    """An estimate of nothing means everything is already downloaded, so
    offering Download is a dead click. Tk guarded on the count."""

    def _dialog(self, est):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b._dl = {"server": "srv1", "item": {"Id": "s1", "Name": "Show"},
                 "est": est, "watched": False, "container": True}
        b._show_download()
        return build_scene(b)

    def test_zero_items_offers_no_download_button(self):
        nodes, handlers = self._dialog({"count": 0, "total_bytes": 0,
                                        "already_count": 12})
        self.assertNotIn("dl-ok", handlers, "a dead Download button")
        texts = [n.get("text") or "" for n in nodes]
        self.assertTrue(any("nothing left" in t.lower() for t in texts),
                        "it does not say why: %r" % texts)

    def test_a_real_estimate_still_offers_it(self):
        _n, handlers = self._dialog({"count": 3, "total_bytes": 100})
        self.assertIn("dl-ok", handlers)

    def test_a_pending_estimate_still_says_estimating(self):
        nodes, handlers = self._dialog(None)
        self.assertNotIn("dl-ok", handlers)
        texts = [n.get("text") or "" for n in nodes]
        self.assertTrue(any("estimating" in t.lower() for t in texts))

class TestDownloadsPollerShowsCompletion(unittest.TestCase):
    """The poller stopped as soon as nothing was pending — without reading
    the catalog one last time. The transition that took pending to zero is
    the one the list has not drawn yet, so the item that had just finished
    still read "downloading" until someone pressed Refresh."""

    def _browser(self, activity):
        ctl = FakeController()
        self.reads = []
        ctl.download_activity = lambda: activity.pop(0) if activity else (0, 0)
        ctl.list_downloads = lambda: (self.reads.append(1) or [])
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=FakeConfig())
        b._pool = _SyncPool()
        b.DL_POLL_SECS = 0.01      # don't sleep 3s per tick in a test
        b._browsing = True
        return b

    def _run_poller(self, b, route):
        b.nav_stack = [route]
        b._poll_downloads(route)
        t = b._dl_thread
        if t is not None:
            t.join(5)
            self.assertFalse(t.is_alive(), "the poller never stopped")

    def test_it_reads_once_more_when_the_queue_drains(self):
        b = self._browser([(1, 1), (0, 1)])
        route = {"kind": "settings", "server": "srv1", "_tab": "downloads"}
        self._run_poller(b, route)
        self.assertGreaterEqual(
            len(self.reads), 2,
            "the finished download was never re-read: %d" % len(self.reads))

    def test_it_still_stops(self):
        """The extra read must not turn the break into a spin."""
        b = self._browser([(1, 1), (0, 1)])
        route = {"kind": "settings", "server": "srv1", "_tab": "downloads"}
        self._run_poller(b, route)
        self.assertIsNone(b._dl_thread, "the poller slot was never released")

    def test_leaving_the_tab_does_not_trigger_a_final_read(self):
        """Only a drained queue gets the last read; walking away should not
        cost a catalog scan."""
        b = self._browser([(1, 1)])
        route = {"kind": "settings", "server": "srv1", "_tab": "downloads"}
        b.nav_stack = [{"kind": "home", "server": "srv1"}]   # not on the tab
        b._poll_downloads(route)
        t = b._dl_thread
        if t is not None:
            t.join(5)
        self.assertEqual(self.reads, [])

class TestEmptyDownloadFolderAsksFirst(unittest.TestCase):
    """Clearing the folder field and pressing Enter used to relocate the
    whole download store to the default location, silently — no confirm, and
    nothing on screen saying that is what an empty box means."""

    def _browser(self, relocate=None):
        cfg = FakeConfig()
        cfg.relocate_downloads = relocate or (
            lambda path, progress=None: (True, "moved"))
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController(), config=cfg)
        b._pool = _SyncPool()
        return b

    def _settle(self, b):
        t = b._long_thread
        if t is not None:
            t.join(5)

    def test_an_empty_field_does_not_move_anything_yet(self):
        moved = []
        b = self._browser(lambda path, progress=None: (
            moved.append(path) or (True, "moved")))
        for empty in ("", "   ", None):
            with self.subTest(value=empty):
                del moved[:]
                b._dialog = None
                b._move_downloads(empty)
                self._settle(b)
                self.assertEqual(moved, [], "relocated without asking")
                self.assertIsNotNone(b._dialog, "no confirmation was shown")

    def test_confirming_then_moves_to_the_default(self):
        moved = []
        b = self._browser(lambda path, progress=None: (
            moved.append(path) or (True, "moved")))
        b._move_downloads("", confirmed=True)
        self._settle(b)
        self.assertEqual(moved, [""],
                         "confirming did not reach the default folder")

    def test_a_real_path_still_moves_without_a_prompt(self):
        moved = []
        b = self._browser(lambda path, progress=None: (
            moved.append(path) or (True, "moved")))
        b._move_downloads("/somewhere/else")
        self._settle(b)
        self.assertEqual(moved, ["/somewhere/else"])
        self.assertIsNone(b._dialog, "prompted for an ordinary move")

class TestPlaylistPageDownloadButton(unittest.TestCase):
    def test_it_swaps_to_remove_when_downloaded(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.tiles._downloaded = {"P"}
        b.nav_stack = [{"kind": "playlist", "server": "srv1", "item_id": "P",
                        "title": "Mix", "_data": []}]
        nodes, _h = build_scene(b)
        self.assertIn("pl-undownload", ids(nodes),
                      "playlist page still hardcodes Download")

class TestDownloadStateAndPush(unittest.TestCase):
    """A playlist's id is never a downloads row, and nothing refreshed the
    badges when a download finished while browsing."""

    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_a_downloaded_playlist_reads_as_downloaded(self):
        self.b.tiles._downloaded_playlists = {"P"}
        self.assertTrue(self.b._is_downloaded({"Id": "P",
                                               "Type": "Playlist"}))

    def test_an_undownloaded_playlist_does_not(self):
        self.assertFalse(self.b._is_downloaded({"Id": "P",
                                                "Type": "Playlist"}))

    def test_a_downloaded_season_reads_as_downloaded(self):
        """A season is never itself a downloads row — manager.download
        expands it into its episodes — so _is_downloaded had no branch that
        could ever return True for one."""
        self.b.tiles._downloaded_seasons = {"sea1"}
        self.assertTrue(self.b._is_downloaded({"Id": "sea1",
                                               "Type": "Season"}))

    def test_an_undownloaded_season_does_not(self):
        self.assertFalse(self.b._is_downloaded({"Id": "sea1",
                                                "Type": "Season"}))

    def test_a_downloaded_season_offers_remove_not_download(self):
        """The visible consequence: se-undownload was unrenderable, so a
        fully downloaded season showed "Download" forever."""
        self.b.tiles._downloaded_seasons = {"sea1"}
        item = {"Id": "sea1", "Type": "Season", "SeriesId": "sh1"}
        btn = self.b._download_btn(item, "srv1", "se")
        _n, h = layout(btn, 1280, 720)
        self.assertIn("se-undownload", h,
                      "a downloaded season still offers Download")

    def test_removing_a_season_download_passes_both_ids(self):
        """The Season branch of _remove_download was unreachable, so this
        call had never once been made."""
        got = {}
        self.ctl.delete_download = lambda **kw: got.update(kw)
        self.b.tiles._downloaded_seasons = {"sea1"}
        self.b.nav_stack = [{"kind": "season", "server": "srv1"}]
        self.b._remove_download({"Id": "sea1", "Type": "Season",
                                 "SeriesId": "sh1"})
        self.assertEqual(got, {"series_id": "sh1", "season_id": "sea1"})

    def test_the_push_hook_refreshes_the_badges(self):
        self.ctl.downloaded_ids = lambda: ({"m1"}, set(), {"sea1"}, {"P"})
        self.b.on_downloads_changed()
        self.assertEqual(self.b.tiles._downloaded, {"m1"})
        self.assertEqual(self.b.tiles._downloaded_seasons, {"sea1"})
        self.assertEqual(self.b.tiles._downloaded_playlists, {"P"})

    def test_the_controller_reports_playlists(self):
        import jellyfin_mpv_shim.sync.manager as mgr
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        class FakeDB:
            def list_playlists(self):
                return [{"playlist_id": "P"}]

        class FakeSync:
            db = FakeDB()

            @staticmethod
            def downloaded_item_ids():
                return {"m1"}

            @staticmethod
            def downloaded_series_ids():
                return {"sh1"}

            @staticmethod
            def downloaded_season_ids():
                return {"sea1"}

        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))
        got = PlayerGateway().downloaded_ids()
        self.assertEqual(got, ({"m1"}, {"sh1"}, {"sea1"}, {"P"}))


if __name__ == "__main__":
    unittest.main()
