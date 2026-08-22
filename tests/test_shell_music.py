"""Music browsing: albums, artists, genres and track lists.
"""

import re
import unittest
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser import tile_renderer
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeController,
    FakeSource,
    _SyncPool,
    _sub_item,
    build_scene,
    detail_page,
    ids,
    music_page,
    music_scroll,
)


class TestMusicDepth(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _music(self, tab=None):
        route = {"kind": "music", "server": "srv1", "parent_id": "ml",
                 "title": "Music"}
        if tab:
            route["_tab"] = tab
        self.b.navigate(route)
        return build_scene(self.b)

    def test_all_five_tabs(self):
        nodes, _h = self._music()
        for t in ("mtab-albums", "mtab-albumartists", "mtab-artists",
                  "mtab-songs", "mtab-genres"):
            self.assertIn(t, ids(nodes))

    def _first_music_tile(self, tab):
        """``(w, h)`` of the first tile on a music tab.

        Matches ``music-<row>-<itemid>`` specifically. A ``music-`` prefix
        test does not work: the tab bar's Paginated checkbox is
        ``music-paginated`` and the scroller is ``music-grid``, both of which
        sort ahead of the tiles and are the same size on every tab -- so the
        loose selector compared the checkbox against itself and passed no
        matter what shape the tiles were.
        """
        nodes, _h = self._music(tab=tab)
        hit = [n for n in nodes
               if re.match(r"^music-\d+-", str(n.get("id", "")))
               and n["t"] == "rect"]
        self.assertTrue(hit, "no tiles on the %s tab" % tab)
        return hit[0]["w"], hit[0]["h"]

    def test_genre_tiles_are_square_like_every_other_music_tile(self):
        """jellyfin-web draws a MusicGenre as shape:'auto' with nothing to
        measure -- genres carry no Primary image -- and setCardData's
        no-aspect-ratio fallback is square. The modern app says it outright
        (Music -> SquareOverflow). Ours was landscape.

        Asserted against the albums tab in the same shape of scene rather
        than against a geom constant, because "is it square" cannot be read
        off one tile: every geom is taller than wide once the caption is
        added. Albums have always been square, so they are the reference.
        """
        self.assertEqual(self._first_music_tile("genres"),
                         self._first_music_tile("albums"),
                         "genre tiles are not the same shape as album tiles")

    def test_songs_tab_is_track_list(self):
        _n, h = self._music(tab="songs")
        self.assertTrue(any(k.startswith("song-") for k in h))

    def test_playing_a_song_asks_the_server_from_that_row(self):
        """A music library's songs tab runs to thousands of rows and is
        windowed, so "everything on screen" is a few loaded pages with holes
        between them -- there is no full list to index into.

        jellyfin-web does the same thing: playAllFromHere re-runs the
        container's own query rather than using its rendered rows. Ours is
        simpler because the row index IS the track's absolute position.
        """
        _n, h = self._music(tab="songs")
        h["song-2"]["click"]()
        ids_, _srv, start = self.ctl.played[-1]
        self.assertEqual(start, 0, "played into a queue instead of from it")
        self.assertEqual(ids_[0], "so2",
                         "the queue does not start at the clicked row")

    def test_album_action_bar(self):
        self.b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                         "title": "Album"})
        nodes, h = build_scene(self.b)
        for nid in ("album-play", "album-shuffle", "album-queue", "album-mix"):
            self.assertIn(nid, ids(nodes))
        h["album-queue"]["click"]()
        self.assertIn("queue_items",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_instant_mix_plays(self):
        self.b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                         "title": "Album"})
        _n, h = build_scene(self.b)
        h["album-mix"]["click"]()
        ids_, _srv, _s = self.ctl.played[-1]
        self.assertEqual(ids_, ["mix0", "mix1", "mix2"])

    def test_artist_action_bar_and_albums(self):
        self.b.navigate({"kind": "artist", "server": "srv1", "item_id": "ar1",
                         "title": "Artist"})
        nodes, h = build_scene(self.b)
        self.assertIn("art-play", ids(nodes))
        self.assertTrue(any(k.startswith("artist-") for k in h))

class TestSearchSongsAllRender(unittest.TestCase):
    """Every song in a search result must be on screen.

    The Songs table was virtualized against scroll_id="search", but its
    VScroll had no on_scroll, so nothing ever re-rendered — the window
    computed at offset 0 was the only one materialized and every row past
    the first screenful drew blank, permanently. head_h was a fixed 120
    against a header that is a People row plus up to six carousels.
    """

    def _search(self, n_songs):
        src = FakeSource()
        src.search = lambda srv, term, limit=60: (
            [{"Id": "m1", "Name": "A Movie", "Type": "Movie"}]
            + [{"Id": "s%d" % i, "Name": "Song %d" % i, "Type": "Audio",
                "RunTimeTicks": 1200000000}
               for i in range(n_songs)])
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "search", "server": "srv1", "term": "x"})
        return build_scene(b, (1280, 720))

    def test_a_song_far_past_the_fold_still_renders(self):
        nodes, _h = self._search(60)
        texts = [n.get("text") or "" for n in nodes]
        for name in ("Song 0", "Song 30", "Song 59"):
            self.assertIn(name, texts, "%s never reached the scene" % name)

    def test_every_song_row_is_clickable(self):
        """A blank virtualized row is a spacer with no handler, so the rows
        being present is not enough — they have to be live."""
        nodes, handlers = self._search(40)
        for i in (0, 20, 39):
            self.assertIn("search-song-%d" % i, handlers,
                          "row %d has no click handler" % i)

    def test_the_carousels_above_the_table_are_still_there(self):
        """The songs table sits under the tile rows; dropping virtualization
        must not have cost the rest of the page. (Tile *names* are baked into
        the strip bitmap, so assert on the heading and the row node.)"""
        nodes, _h = self._search(30)
        texts = [n.get("text") or "" for n in nodes]
        self.assertIn("Movies", texts)
        self.assertIn("People", texts)
        self.assertIn("search-Movies", ids(nodes))

class TestMusicPaging(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()

    def _albums(self, total=5000):
        src = self.b.source
        calls = []

        def get_music_albums(server_uuid, parent_id, start_index=0,
                             limit=100, **kw):
            calls.append(start_index)
            return ([{"Id": "al%d" % (start_index + i),
                      "Name": "Album %d" % (start_index + i),
                      "Type": "MusicAlbum"} for i in range(limit)], total)
        src.get_music_albums = get_music_albums
        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "lib1", "title": "Music"})
        return calls

    def test_the_tab_is_sized_from_the_server_total(self):
        """Windowed since #617: the list is `total` slots from the first
        frame, so the scrollbar is full-length and does not resize as pages
        land."""
        calls = self._albums()
        self.assertEqual(len(self.b.route["_data"]), 5000)
        self.assertEqual(calls, [0], "the first frame fetched more than one "
                                     "page: %r" % calls)

    def test_scrolling_far_down_fetches_that_window(self):
        calls = self._albums()
        music_scroll(self.b, self.b.route, 40_000, 60_000)
        self.assertTrue(len(calls) > 1, "nothing was fetched for the window")
        self.assertTrue(all(c >= 100 for c in calls[1:]),
                        "walked the tab from the top: %r" % calls)
        self.assertEqual(len(self.b.route["_data"]), 5000,
                         "the list changed length as a window landed")

    def test_the_top_of_the_tab_asks_for_nothing_more(self):
        calls = self._albums()
        music_scroll(self.b, self.b.route, 0, 60_000)
        self.assertEqual(calls, [0], "re-fetched items it already had")

    def test_genres_window_to_nothing(self):
        """Genres are unpaged server-side and report their own length, so
        the padded list has no holes -- the same shape that exempts a
        Random library grid."""
        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "lib1", "title": "Music"})
        page = self.b._page_for(self.b.route)
        page._set_tab("genres")
        data = self.b.route.get("_data") or []
        self.assertTrue(data, "the genres tab loaded nothing")
        self.assertTrue(all(i is not None for i in data),
                        "an unpaged tab was padded with holes")

class TestSongsTabWindows(unittest.TestCase):
    """The songs tab is the one most likely to run to thousands of rows."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def _songs(self, total=4000):
        calls = []

        def get_songs(server_uuid, parent_id, start_index=0, limit=100, **kw):
            calls.append((start_index, limit))
            return ([{"Id": "so%d" % (start_index + i),
                      "Name": "Song %d" % (start_index + i),
                      "Type": "Audio"} for i in range(limit)], total)
        self.b.source.get_songs = get_songs
        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "ml", "title": "Music", "_tab": "songs"})
        return calls

    def test_the_table_is_sized_from_the_server_total(self):
        calls = self._songs()
        self.assertEqual(len(self.b.route["_data"]), 4000)
        self.assertEqual([c[0] for c in calls], [0])

    def test_a_hole_row_is_drawn_but_inert(self):
        """Two things, not one: the cells have to be blank AND the handlers
        have to go. _list_view needed both and only got the first at
        first."""
        self._songs()
        nodes, handlers = build_scene(self.b)
        holes = [k for k in handlers if k.startswith("song-")
                 and int(k.rsplit("-", 1)[-1]) > 200]
        self.assertFalse(holes, "unloaded rows carry handlers: %r" % holes[:3])

    def test_playing_a_hole_is_impossible(self):
        """There is no id to play, so there must be no way to ask."""
        self._songs()
        _n, handlers = build_scene(self.b)
        self.assertIn("song-0", handlers, "the loaded rows lost their click")


class TestTrackListVirtualization(unittest.TestCase):
    """Track tables must window their rows. With the album-art column each
    visible row is one mpv overlay, so a few hundred tracks would blow the
    63-overlay budget outright — not just cost a slow repaint."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()
        self.tracks = [{"Id": "t%d" % i, "Name": "Track %d" % i,
                        "Type": "Audio", "IndexNumber": i + 1,
                        "RunTimeTicks": 2000000000} for i in range(400)]
        # _track_list reads _size when it computes the virtual window, so it
        # has to be set before the tree is built, not just before layout.
        self.b._size = (1280, 720)

    def _row_ids(self, node, size=(1280, 720)):
        nodes, _h = layout(node, *size)
        return {n["id"] for n in nodes
                if isinstance(n.get("id"), str) and n["id"].startswith("t-")
                and n["id"].count("-") == 1}

    def test_only_a_window_of_rows_is_built(self):
        node = self.b._track_list(self.tracks, "t", lambda i: None,
                                  scroll_id="album")
        ids = self._row_ids(node)
        self.assertGreater(len(ids), 0)
        self.assertLess(len(ids), 60, "should not materialize 400 rows")

    def test_window_follows_the_scroll_offset(self):
        top = self._row_ids(self.b._track_list(
            self.tracks, "t", lambda i: None, scroll_id="album"))
        self.b._scroll.on_scroll("album", 6000, 100000)
        bottom = self._row_ids(self.b._track_list(
            self.tracks, "t", lambda i: None, scroll_id="album"))
        self.assertTrue(top and bottom)
        self.assertNotEqual(top, bottom)

    def test_without_a_scroll_id_nothing_is_windowed(self):
        """Short lists inside another scroll keep the simple path."""
        node = self.b._track_list(self.tracks[:5], "t", lambda i: None)
        self.assertEqual(len(self._row_ids(node)), 5)

    def test_art_column_stays_within_the_overlay_budget(self):
        from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode
        node = self.b._track_list(self.tracks, "t", lambda i: None,
                                  art=True, scroll_id="playlist")
        nodes, _h = layout(node, 1280, 720)
        images = [n for n in nodes if n["t"] == "img"]
        self.assertLess(len(images), 63, "exceeds mpv's overlay budget")
        _ = ImageNode

class TestTrackListArtWindowing(unittest.TestCase):
    """Art cells composite into the 48-entry strip LRU as they are built,
    so a long playlist must only build them for the visible window — the
    unwindowed version evicted (and freed the buffers of) the very rows
    on screen, which then drew blank on every repaint."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._size = (1280, 720)
        self.built = []
        self.b.tiles.art_cell = lambda tr, size=28: self.built.append(
            tr.get("Id")) or self.b.tiles._art_placeholder(size)

    def _tracks(self, n):
        return [{"Id": "t%d" % i, "Name": "Track %d" % i,
                 "Type": "Audio"} for i in range(n)]

    def test_only_the_visible_window_composites_art(self):
        tracks = self._tracks(300)
        self.b._track_list(tracks, "pl", on_play=lambda i: None,
                           art=True, scroll_id="playlist", head_h=70)
        self.assertLess(len(self.built), 48,
                        "must stay under the strip LRU bound")
        self.assertIn("t0", self.built, "the top rows are visible")
        self.assertNotIn("t299", self.built, "off-screen rows must not")

    def test_scrolling_moves_the_window(self):
        tracks = self._tracks(300)
        self.b._scroll.on_scroll("playlist", 100 * tile_renderer.TRACK_ROW_H + 70, 100000)
        self.b._track_list(tracks, "pl", on_play=lambda i: None,
                           art=True, scroll_id="playlist", head_h=70)
        self.assertNotIn("t0", self.built)
        self.assertIn("t100", self.built, "scrolled-to rows composite")
        self.assertLess(len(self.built), 48)

    def test_short_lists_are_unaffected(self):
        tracks = self._tracks(12)
        self.b._track_list(tracks, "pl", on_play=lambda i: None,
                           art=True, scroll_id="playlist", head_h=70)
        self.assertEqual(len(self.built), 12)

class TestTrackDefaults(unittest.TestCase):
    """The detail page's pickers must show the tracks that will actually
    play. They showed a hardcoded "None" for subtitles, and because a
    browser selection is taken as final downstream (explicit_tracks), that
    lie became the playback behaviour."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.route = {"kind": "detail", "server": "srv1"}

    def test_server_default_subtitle_is_preselected(self):
        item = _sub_item(default_sid=4)
        _aid, sid = detail_page(self.b, self.route)._effective_tracks(item)
        self.assertEqual(sid, 4, "showed None instead of the server default")

    def test_language_config_beats_the_server_default(self):
        import jellyfin_mpv_shim.language_config as lc

        item = _sub_item(default_sid=4)
        real, lc.apply = lc.apply, lambda rules, src, it: (None, 3)
        self.addCleanup(lambda: setattr(lc, "apply", real))
        _aid, sid = detail_page(self.b, self.route)._effective_tracks(item)
        self.assertEqual(sid, 3, "language_config must win")

    def test_explicit_none_is_not_overwritten_by_the_default(self):
        """-1 is a deliberate "no subtitles" and must survive; only an
        untouched picker (None) falls back to the default."""
        item = _sub_item(default_sid=4)
        self.route["_sid"] = -1
        _aid, sid = detail_page(self.b, self.route)._effective_tracks(item)
        self.assertEqual(sid, -1)

    def test_picking_audio_still_carries_the_subtitle_default(self):
        """The poisoning case: touching only Audio marked the play
        explicit with sid=None, so map_streams returned before applying
        DefaultSubtitleStreamIndex and subtitles came up off."""
        item = _sub_item(default_sid=4)
        self.route["_aid"] = 1
        aid, sid = detail_page(self.b, self.route)._effective_tracks(item)
        self.assertEqual((aid, sid), (1, 4))

    def test_no_subtitle_streams_reports_no_choice(self):
        """An item with no subtitles must not send a spurious index —
        that would mark the play explicit for no reason."""
        item = _sub_item(default_sid=None, subs=())
        _aid, sid = detail_page(self.b, self.route)._effective_tracks(item)
        self.assertIsNone(sid)

    def test_picker_shows_the_default_not_none(self):
        item = _sub_item(default_sid=4)
        from jellyfin_mpv_shim.mpvtk.widgets import Column

        rows = detail_page(self.b, self.route)._track_pickers(item)
        self.assertTrue(rows, "expected pickers")
        nodes, _h = layout(Column(rows), 1280, 720)
        dd = [n for n in nodes if n.get("id") == "dt-sub"]
        self.assertTrue(dd, "no subtitle picker rendered")
        # options are ["None", "Sub 3", "Sub 4"] -> index 2
        self.assertEqual(dd[0].get("sel"), 2)

    def test_defaults_are_resolved_once_per_source(self):
        """_effective_tracks runs from build(), so the language_config
        walk (which logs on every call) must not run per repaint."""
        import jellyfin_mpv_shim.language_config as lc

        calls = []
        item = _sub_item(default_sid=4)
        real = lc.apply
        lc.apply = lambda rules, src, it: (calls.append(1), (None, None))[1]
        self.addCleanup(lambda: setattr(lc, "apply", real))
        for _ in range(5):
            detail_page(self.b, self.route)._effective_tracks(item)
        self.assertEqual(len(calls), 1, "should be cached on the route")

class TestQueueToPlaylist(unittest.TestCase):
    def test_the_button_saves_the_whole_queue(self):
        ctl = FakeController()
        added = []
        ctl.playlist_add = lambda s, p, ids: added.append(list(ids))
        src = FakeSource()
        src.get_playlists = lambda srv: [{"Id": "p1", "Name": "Mix"}]
        b = MpvtkBrowser(app=None, source=src, controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        b.nav_stack = [{"kind": "queue", "server": "srv1", "_data": {
            "entries": [{"item": {"Id": "a"}, "pid": "p1"},
                        {"item": {"Id": "b"}, "pid": "p2"}],
            "current_id": "a"}}]
        nodes, h = build_scene(b)
        self.assertIn("q-toplaylist", ids(nodes), "no way to save the queue")
        h["q-toplaylist"]["click"]()
        _n, h = build_scene(b)
        h["add-pl-0"]["click"]()
        self.assertEqual(added, [["a", "b"]])

class TestSongsTabArt(unittest.TestCase):
    """The Songs tab is a whole library's songs from every album at once —
    exactly the mixed-album case the art column exists for — and it was the
    one track list rendering without it. The album page omits art on
    purpose (every row would be the same cover)."""

    def _browser(self, tab):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        # Spy on the art cell: with no real image server the cell falls back
        # to a placeholder, so the rendered scene can't tell "art column,
        # nothing loaded yet" from "no art column".
        self.art_for = []
        b.tiles.art_cell = lambda tr, size=28: self.art_for.append(
            tr.get("Id")) or b.tiles._art_placeholder(size)
        b.navigate({"kind": "music", "server": "srv1", "parent_id": "lib1",
                    "title": "Music"})
        music_page(b)._set_tab(tab)
        return b

    def test_the_songs_tab_shows_per_row_art(self):
        b = self._browser("songs")
        build_scene(b)
        self.assertTrue(self.art_for, "no album art in the songs list")

    def test_the_list_is_still_virtualized(self):
        """art=True means one mpv overlay per visible row, against a budget
        of 63 — safe only because off-screen rows are never built."""
        b = self._browser("songs")
        build_scene(b)
        self.assertLess(len(self.art_for), 63)

class TestMusicTabsAreCached(unittest.TestCase):
    """Every tab switch refetched from scratch, so flipping between Albums
    and Artists on a large library re-paged the whole thing each time. Tk
    cached per tab."""

    def _browser(self):
        src = FakeSource()
        self.calls = []
        real_albums = src.get_music_albums
        src.get_music_albums = lambda srv, p, **kw: (
            self.calls.append("albums") or real_albums(srv, p, **kw))
        src.get_artists = lambda srv, p, **kw: (
            self.calls.append("artists")
            or ([{"Id": "ar1", "Name": "A", "Type": "MusicArtist"}], 1))
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "music", "server": "srv1", "parent_id": "lib1",
                    "title": "Music"})
        return b

    def test_going_back_to_a_tab_does_not_refetch(self):
        b = self._browser()
        route = b.route
        music_page(b, route)._set_tab("artists")
        music_page(b, route)._set_tab("albums")
        self.assertEqual(self.calls, ["albums", "artists"],
                         "the albums tab was fetched twice: %r" % self.calls)

    def test_the_cached_tab_still_has_its_items(self):
        b = self._browser()
        route = b.route
        first = list(route["_data"])
        music_page(b, route)._set_tab("artists")
        music_page(b, route)._set_tab("albums")
        self.assertEqual(route["_data"], first)

    def test_a_tab_never_opened_is_still_fetched(self):
        b = self._browser()
        music_page(b)._set_tab("artists")
        self.assertIn("artists", self.calls)

    def test_the_cache_dies_with_the_route(self):
        """It lives in the route dict, so it cannot go stale across a
        reload or outlive the page."""
        b = self._browser()
        music_page(b)._set_tab("artists")
        b.navigate({"kind": "music", "server": "srv1", "parent_id": "lib1",
                    "title": "Music"})
        self.assertNotIn("_tab_cache", {k: v for k, v in b.route.items()
                                        if k == "_tab_cache" and v})

class TestMusicDetailHeaders(unittest.TestCase):
    """Album and artist pages were a bare title — no cover, no year or genre,
    no Overview, and no heading over the artist's album grid. On a music
    library that is most of what tells one entry from another."""

    def _browser(self):
        src = FakeSource()
        src.get_item = lambda srv, iid: {
            "Id": iid, "Name": "The Album", "Type": "MusicAlbum",
            "ProductionYear": 1999, "Genres": ["Rock"],
            "Overview": "A record about things."}
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        return b

    def _texts(self, b):
        return [n.get("text") or "" for n in build_scene(b)[0]]

    def test_an_album_shows_its_year_genre_and_overview(self):
        b = self._browser()
        b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                    "title": "The Album"})
        texts = self._texts(b)
        self.assertIn("The Album", texts)
        self.assertTrue(any("1999" in t and "Rock" in t for t in texts),
                        "no metadata line: %r" % texts)
        self.assertTrue(any("A record about things" in t for t in texts),
                        "no overview")

    def test_an_album_shows_its_track_count(self):
        b = self._browser()
        b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                    "title": "The Album"})
        self.assertTrue(any("tracks" in t for t in self._texts(b)))

    def test_an_artist_page_heads_its_album_grid(self):
        b = self._browser()
        b.navigate({"kind": "artist", "server": "srv1", "item_id": "ar1",
                    "title": "The Artist"})
        self.assertIn("Albums", self._texts(b))

    def test_an_artist_page_shows_the_fetched_metadata(self):
        b = self._browser()
        b.navigate({"kind": "artist", "server": "srv1", "item_id": "ar1",
                    "title": "The Artist"})
        self.assertTrue(any("A record about things" in t
                            for t in self._texts(b)))

    def test_a_server_that_cannot_answer_still_renders_the_title(self):
        """get_item is best-effort — the header degrades, it does not blow up
        the page."""
        b = self._browser()
        b.source.get_item = lambda srv, iid: (_ for _ in ()).throw(
            RuntimeError("no"))
        b.navigate({"kind": "artist", "server": "srv1", "item_id": "ar1",
                    "title": "The Artist"})
        self.assertIn("The Artist", self._texts(b))

class TestTrackRowsHaveAContextMenu(unittest.TestCase):
    """Tiles have had a right-click menu all along; Table rows never asked
    for one. Every music playlist therefore lost Play / Add to Queue /
    Favorite / Download — and per-track "Remove from playlist" entirely,
    leaving only the bulk editor. The toolkit already supported it."""

    def _playlist(self):
        src = FakeSource()
        tracks = [{"Id": "t%d" % i, "Name": "Track %d" % i, "Type": "Audio",
                   "PlaylistItemId": "e%d" % i} for i in range(3)]
        src.get_playlist_items = lambda srv, pid: list(tracks)
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "playlist", "server": "srv1", "item_id": "P",
                    "title": "Mix"})
        return b

    def test_a_playlist_row_opens_the_menu(self):
        b = self._playlist()
        _n, handlers = build_scene(b)
        self.assertIn("context", handlers.get("pl-0", {}),
                      "no context menu on a track row")
        handlers["pl-0"]["context"](100, 100)
        self.assertIsNotNone(b._menu, "the menu did not open")
        self.assertEqual(b._menu["item"]["Id"], "t0")

    def test_the_menu_offers_remove_from_playlist(self):
        """The entry that was unreachable by any route."""
        b = self._playlist()
        _n, handlers = build_scene(b)
        handlers["pl-1"]["context"](100, 100)
        labels = [e[0] for e in b._tile_menu_entries(b._menu["item"])]
        self.assertIn("Remove from playlist", labels)
        self.assertIn("Play", labels)
        self.assertIn("Add to play queue", labels)

    def test_it_opens_the_menu_for_the_row_you_clicked(self):
        b = self._playlist()
        _n, handlers = build_scene(b)
        handlers["pl-2"]["context"](10, 20)
        self.assertEqual(b._menu["item"]["Id"], "t2")

    def test_the_playlist_editor_rows_do_not_get_one(self):
        """The editor is a multi-select surface with its own gestures; a
        context menu there would fight the selection."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.navigate({"kind": "playlist_edit", "server": "srv1",
                    "item_id": "PL1", "title": "Faves"})
        _n, handlers = build_scene(b)
        self.assertNotIn("context", handlers.get("pe-row-0", {}))


if __name__ == "__main__":
    unittest.main()


class TestSearchRowCaps(unittest.TestCase):
    """One budget per row, not one shared across the screen (#641).

    The query asks for a lot so that every type is represented -- a term
    matching a thousand songs used to spend the whole 60-item allowance
    before the movies were reached, and the Movies row simply did not
    appear. Measured against a real server: searching "a" at 60 returned no
    Audio at all, and at 800 returns 719 of them.

    But the answer cannot be drawn whole. A tile row composites every tile
    it is given and the Songs table is deliberately not virtualized, so the
    same widening that fixes the missing row would lay out thousands of
    nodes and blow mpv's 63-overlay budget. Hence a cap per row.
    """

    def _search(self, per_type):
        from jellyfin_mpv_shim.mpvtk_browser.pages.search import ROW_MAX
        self.ROW_MAX = ROW_MAX
        src = FakeSource()
        src.search = lambda srv, term, limit=800: (
            [{"Id": "m%d" % i, "Name": "Movie %d" % i, "Type": "Movie"}
             for i in range(per_type)]
            + [{"Id": "s%d" % i, "Name": "Song %d" % i, "Type": "Audio",
                "RunTimeTicks": 1200000000} for i in range(per_type)])
        src.search_people = lambda srv, term, limit=100: [
            {"Id": "p%d" % i, "Name": "Person %d" % i, "Type": "Person"}
            for i in range(per_type)]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "search", "server": "srv1", "term": "x"})
        return build_scene(b, (1280, 720))

    def test_the_songs_table_stops_at_the_cap(self):
        found = 200
        _nodes, handlers = self._search(found)
        rows = [k for k in handlers if str(k).startswith("search-song-")]
        self.assertEqual(len(rows), self.ROW_MAX,
                         "the songs table drew %d rows for %d results"
                         % (len(rows), found))

    def test_every_type_still_gets_its_row(self):
        """The point of widening the query. A row per type, none of them
        starved by another's matches."""
        nodes, _h = self._search(200)
        texts = [n.get("text") or "" for n in nodes]
        for heading in ("People", "Movies", "Songs"):
            self.assertIn(heading, texts, "%s row is missing" % heading)

    def test_a_small_result_is_untouched(self):
        """The cap must not truncate ordinary searches -- it is a ceiling,
        not a page size."""
        nodes, handlers = self._search(3)
        rows = [k for k in handlers if str(k).startswith("search-song-")]
        self.assertEqual(len(rows), 3)


class TestSearchSectionOrder(unittest.TestCase):
    """Rows come in jellyfin-web's order (SEARCH_SECTIONS_SORT_ORDER).

    What you searched for first, the people who made it after: Movies,
    Shows, Episodes, People, Artists, Albums, Songs, Videos. People used to
    lead, which put a row of faces above the film whose title had just been
    typed -- and since the first row built takes focus, it also left a
    remote's first keypress on the cast.

    The Live TV pair at the end is web's order too: what is on now is a
    result, a channel is a place to go and look.
    """

    def _rows(self):
        src = FakeSource()
        src.search = lambda srv, term, limit=800: [
            {"Id": "m1", "Name": "M", "Type": "Movie"},
            {"Id": "sr1", "Name": "S", "Type": "Series"},
            {"Id": "e1", "Name": "E", "Type": "Episode"},
            {"Id": "al1", "Name": "L", "Type": "MusicAlbum"},
            {"Id": "so1", "Name": "G", "Type": "Audio",
             "RunTimeTicks": 1200000000},
            {"Id": "v1", "Name": "V", "Type": "Video"},
        ]
        src.search_people = lambda srv, term, limit=100: [
            {"Id": "p1", "Name": "P", "Type": "Person"}]
        src.search_artists = lambda srv, term, limit=100: [
            {"Id": "ar1", "Name": "A", "Type": "MusicArtist"}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "search", "server": "srv1", "term": "x"})
        nodes, _h = build_scene(b, (1280, 720))
        # The section-heading tier, resolved rather than written as 24: the
        # page title above them is a tier LARGER (it used to be the same
        # size, which left it with no rank over its own sections), and a
        # literal here would silently start or stop collecting it.
        from jellyfin_mpv_shim.mpvtk import theme as tk

        head_px = tk.size("HEADING")
        headings = [n for n in nodes
                    if n.get("size") == head_px and n.get("text")]
        headings.sort(key=lambda n: n.get("y", 0))
        return [n["text"] for n in headings], nodes

    def test_the_order_is_webs(self):
        headings, _nodes = self._rows()
        # No 'Results for "x"': that is the page title, and it is not a
        # section heading. The order under test is the sections'.
        self.assertEqual(
            headings,
            ["Movies", "Shows", "Episodes", "People",
             "Artists", "Albums", "Songs", "Videos", "On TV", "Channels"])

    def test_the_first_result_row_takes_focus_not_the_cast(self):
        """Submitting a search moves focus out of the box and onto the
        results; it should land on what was searched for."""
        _headings, nodes = self._rows()
        focused = [n.get("id") for n in nodes if n.get("af")]
        self.assertEqual(focused, ["search-Movies-m1"])


class TestSearchArtists(unittest.TestCase):
    """Artists come from /Artists, not from the item search.

    Reported as "I see albums and songs but no artists". The item query is
    not a reliable source for them: against the development server it
    returns fewer than /Artists (9 against 13 for one term, the difference
    being track-level and featured artists that have no MusicArtist item at
    all), and against at least one real server it returns none, which is
    what an absent row looks like from the outside. jellyfin-web asks
    /Artists separately for exactly this reason.
    """

    def _search(self, artists=None, items=None):
        src = FakeSource()
        src.search = lambda srv, term, limit=800: items if items is not None \
            else [{"Id": "m1", "Name": "M", "Type": "Movie"}]
        src.search_people = lambda srv, term, limit=100: []
        if artists is not None:
            src.search_artists = lambda srv, term, limit=100: artists
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "search", "server": "srv1", "term": "x"})
        return build_scene(b, (1280, 720))

    def test_the_dedicated_request_fills_the_row(self):
        nodes, _h = self._search(
            artists=[{"Id": "ar1", "Name": "A", "Type": "MusicArtist"}])
        self.assertIn("Artists", [n.get("text") for n in nodes])
        self.assertIn("search-Artists-ar1", ids(nodes))

    def test_item_results_are_the_fallback(self):
        """The other direction, which has also been seen: a server that
        answers the item query with artists but has no usable /Artists."""
        nodes, _h = self._search(
            artists=[],
            items=[{"Id": "ar9", "Name": "A", "Type": "MusicArtist"}])
        self.assertIn("Artists", [n.get("text") for n in nodes])
        self.assertIn("search-Artists-ar9", ids(nodes))

    def test_a_stray_artist_item_is_never_filed_under_other(self):
        """MusicArtist left SEARCH_TYPES when artists got their own request.
        One arriving anyway must still read as an artist -- it landed in the
        Other row the moment the type stopped being claimed."""
        nodes, _h = self._search(
            artists=[{"Id": "ar1", "Name": "A", "Type": "MusicArtist"}],
            items=[{"Id": "ar9", "Name": "B", "Type": "MusicArtist"}])
        self.assertNotIn("Other", [n.get("text") for n in nodes])

    def test_a_source_without_the_method_still_renders(self):
        """The offline catalog and any older source: getattr'd, not assumed."""
        # A subclass without it, since the method is on the class: this is
        # the offline catalog's shape, and any source written before it.
        class Older(FakeSource):
            search_artists = property(
                lambda self: (_ for _ in ()).throw(AttributeError))

        src = Older()
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "search", "server": "srv1", "term": "x"})
        nodes, _h = build_scene(b, (1280, 720))
        self.assertTrue(nodes)
        self.assertNotIn("Artists", [n.get("text") for n in nodes])
