"""The two book screens, and the reading-position dialog.

A books library holds two unrelated entity types, so the browse rule is not
"books look like X" — it is a question asked per folder: are the things in
here the chapters of an audiobook, or not? Most of what is pinned below is
that question and its edges, because getting it wrong is silent: a folder
that should have been a track list renders as a perfectly reasonable grid.

The dialog tests include the assertion a scene cannot make. ``build_scene``
renders when asked, so it draws a correct tree whether or not the app would
ever have redrawn — a handler that writes state owes a separate check that
something asked for a repaint. See
``test_shell_playlists.test_toggling_private_asks_for_a_repaint``, which is
the shape.
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


def audiobook(i, album="The Divided Account", **extra):
    return {"Id": "ab%d" % i, "Name": "Chapter %02d" % i, "Type": "AudioBook",
            "IndexNumber": i, "Album": album, "Artists": ["Gus Gupta"],
            "RunTimeTicks": 20 * 10000000, **extra}


def book(item_id="bk1", path="/library/A Novel.epub", **extra):
    return {"Id": item_id, "Name": "A Novel", "Type": "Book", "Path": path,
            **extra}


class BooksHarness(unittest.TestCase):
    """A books library open at ``lib-books``, with ``children`` in it."""

    def browser(self, children, folder=None, controller=None):
        src = FakeSource()
        src.libraries = [{"Id": "lib-books", "Name": "Books",
                          "Type": "CollectionFolder",
                          "CollectionType": "books"}]
        src.grid_items = list(children)
        if folder is not None:
            src.items["folder1"] = folder
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.controller = controller or FakeController()
        b.server = "srv1"
        self.src = src
        return b

    def open_folder(self, children, folder=None, controller=None):
        b = self.browser(children, folder, controller)
        b.navigate({"kind": "books", "server": "srv1",
                    "parent_id": "folder1", "item_id": "folder1",
                    "collection_type": "books", "title": "The Divided Account"})
        return b


class TestLibraryIsReachable(BooksHarness):
    """A books library used to be dropped from get_libraries outright, so
    none of the rest of this could be reached at all."""

    def test_a_books_library_is_listed(self):
        from jellyfin_mpv_shim.mpvtk_browser import repository
        self.assertNotIn("books", repository.EXCLUDED_COLLECTION_TYPES)

    def test_opening_a_books_library_lands_on_the_books_page(self):
        b = self.browser([])
        b._open_item({"Id": "lib-books", "Name": "Books",
                      "Type": "CollectionFolder", "CollectionType": "books"})
        self.assertEqual(b.route["kind"], "books")

    def test_a_folder_inside_one_stays_on_the_books_page(self):
        """The books-ness of a nested folder is nowhere in its own DTO — it
        has no CollectionType — so it is carried down the route. Without
        this a folder of chapters opened as an ordinary grid and there was
        no way to play the book."""
        b = self.open_folder([])
        b._open_item({"Id": "sub", "Name": "Sub", "Type": "Folder"})
        self.assertEqual(b.route["kind"], "books")
        self.assertEqual(b.route["collection_type"], "books")

    def test_the_inheritance_does_not_leak_to_other_libraries(self):
        """Propagating any other collection type would make a folder inside
        a movies library run the library's typed, recursive query — and list
        the whole library again inside one of its folders."""
        b = self.browser([])
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        b._open_item({"Id": "sub", "Name": "Sub", "Type": "Folder"})
        self.assertEqual(b.route["kind"], "grid")
        self.assertIsNone(b.route["collection_type"])

    def test_a_book_opens_its_own_page(self):
        b = self.browser([])
        b._open_item(book())
        self.assertEqual(b.route["kind"], "book")

    def test_an_audiobook_opens_a_page_rather_than_playing(self):
        """It plays like a track and it is a *book*.

        A tile that started it left its description, its length, its
        chapters and the place you got to all unreachable — there was
        nowhere in the app they could be seen. A song has none of those,
        which is why a song still plays on click.
        """
        b = self.browser([])
        b._open_item(audiobook(1))
        self.assertEqual(b.route["kind"], "audiobook")
        self.assertFalse(b.controller.played, "it started playing as well")

    def test_a_song_still_plays_on_click(self):
        b = self.browser([])
        was = b.route["kind"]
        b._open_item({"Id": "s1", "Name": "A Song", "Type": "Audio"})
        self.assertEqual(b.route["kind"], was, "a song opened a page")
        self.assertTrue(b.controller.played)

    def test_the_play_chip_still_starts_an_audiobook(self):
        # The same split every other playable type makes: the tile is a
        # door, the chip is the shortcut.
        b = self.browser([])
        self.assertTrue(b._tile_playable(audiobook(1)))

    def test_headless_refuses_both_book_routes(self):
        # The whitelist is what makes this true by default, and this is the
        # test that says so for the two kinds added here.
        from jellyfin_mpv_shim.mpvtk_browser.navigator import HEADLESS_ROUTES
        self.assertNotIn("book", HEADLESS_ROUTES)
        self.assertNotIn("books", HEADLESS_ROUTES)


class TestAudiobookPage(BooksHarness):
    """The destination a loose single-file audiobook needed."""

    def open_audiobook(self, item=None):
        b = self.browser([])
        item = item or dict(audiobook(1, album="The Copper Bell"),
                            Id="ab1", Name="The Copper Bell",
                            Overview="Read by the author.",
                            RunTimeTicks=24 * 60 * 10000000)
        self.src.items["ab1"] = item
        b.navigate({"kind": "audiobook", "server": "srv1", "item_id": "ab1",
                    "title": item["Name"]})
        return b

    def test_the_description_is_reachable(self):
        """The reason the page exists: there was nowhere in the app a loose
        audiobook's description could be read."""
        b = self.open_audiobook()
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("Read by the author.", text)

    def test_it_says_how_long_the_book_is(self):
        b = self.open_audiobook()
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("24:00", text)

    def test_an_untouched_book_offers_play(self):
        b = self.open_audiobook()
        nodes, _h = build_scene(b)
        self.assertIn("ab-play", ids(nodes))
        self.assertNotIn("ab-resume", ids(nodes))

    def test_a_started_book_offers_resume_and_restart(self):
        b = self.open_audiobook(dict(
            audiobook(1), Id="ab1", Name="The Copper Bell",
            RunTimeTicks=24 * 60 * 10000000,
            UserData={"PlaybackPositionTicks": 8 * 60 * 10000000}))
        nodes, handlers = build_scene(b)
        self.assertIn("ab-resume", ids(nodes))
        self.assertIn("Restart", [str(n.get("text", "")) for n in nodes])
        handlers["ab-resume"]["click"]()
        _iid, _srv, offset = b.controller.played[-1]
        self.assertEqual(offset, 8 * 60 * 10000000)

    def test_restart_starts_at_the_beginning(self):
        b = self.open_audiobook(dict(
            audiobook(1), Id="ab1", Name="The Copper Bell",
            RunTimeTicks=24 * 60 * 10000000,
            UserData={"PlaybackPositionTicks": 8 * 60 * 10000000}))
        _n, handlers = build_scene(b)
        handlers["ab-play"]["click"]()
        _iid, _srv, offset = b.controller.played[-1]
        self.assertIsNone(offset)

    def test_its_chapters_are_listed(self):
        b = self.open_audiobook(dict(
            audiobook(1), Id="ab1", Name="The Copper Bell",
            RunTimeTicks=24 * 60 * 10000000,
            Chapters=[{"Name": "One", "StartPositionTicks": 0},
                      {"Name": "Two", "StartPositionTicks": 2400000000}]))
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("One", text)
        self.assertIn("Two", text)

    def test_a_chapter_plays_the_book_from_its_mark(self):
        """These are markers inside ONE file, not queue entries — so a row
        plays the book from that offset rather than playing anything of its
        own. That is the whole difference from a rip's folder."""
        b = self.open_audiobook(dict(
            audiobook(1), Id="ab1", Name="The Copper Bell",
            RunTimeTicks=24 * 60 * 10000000,
            Chapters=[{"Name": "One", "StartPositionTicks": 0},
                      {"Name": "Two", "StartPositionTicks": 2400000000}]))
        _n, handlers = build_scene(b)
        handlers["ab-ch-1"]["click"]()
        iid, _srv, offset = b.controller.played[-1]
        self.assertEqual(iid, "ab1")
        self.assertEqual(offset, 2400000000)

    def test_a_book_with_one_chapter_gets_no_list(self):
        # A single marker is just the start.
        b = self.open_audiobook(dict(
            audiobook(1), Id="ab1", Name="The Copper Bell",
            Chapters=[{"Name": "One", "StartPositionTicks": 0}]))
        nodes, _h = build_scene(b)
        self.assertNotIn("ab-ch-0", ids(nodes))

    def test_headless_refuses_it(self):
        from jellyfin_mpv_shim.mpvtk_browser.navigator import HEADLESS_ROUTES
        self.assertNotIn("audiobook", HEADLESS_ROUTES)


class TestPlayChips(BooksHarness):
    """The floating play button on a hovered tile.

    A folder in a books library is an author or a title, and only one of
    those can be played: a folder of Books has no playable content at all,
    because a Book has no media source. The tile cannot tell which it is —
    a Folder DTO says nothing about its contents and this runs per tile per
    strip — so neither gets a chip.
    """

    def test_a_folder_in_a_books_library_gets_no_chip(self):
        b = self.open_folder([])
        self.assertFalse(b._tile_playable(
            {"Id": "f", "Name": "Ines Imani", "Type": "Folder"}))

    def test_a_book_gets_no_chip(self):
        # It never did — Book is not in PLAYABLE_TYPES — but this is the
        # thing the report was about, so it is worth pinning rather than
        # relying on the absence.
        b = self.open_folder([])
        self.assertFalse(b._tile_playable(book()))

    def test_an_ordinary_folder_still_gets_one(self):
        """The suppression is scoped to books libraries. A Home Videos
        folder is a queue in the order the grid is showing it, and that
        chip is the point of CHIP_CONTAINERS."""
        b = self.browser([])
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "hv",
                    "collection_type": None, "title": "Home Videos"})
        self.assertTrue(b._tile_playable(
            {"Id": "f", "Name": "Holiday", "Type": "Folder"}))

    def test_an_audiobook_tile_still_gets_one(self):
        """The item itself is ordinary audio; only the *folder* is ambiguous.

        This is the assertion that found AudioBook missing from every tile
        menu set — no chip, no Play, no Add to Queue, no favourite and no
        download, for no reason but the type string being longer than
        "Audio"."""
        b = self.open_folder([])
        self.assertTrue(b._tile_playable(audiobook(1)))

    def test_an_audiobook_tile_offers_what_a_track_does(self):
        b = self.open_folder([])
        actions = {key for _label, _icon, key
                   in b._tile_menu_entries(audiobook(1))}
        self.assertEqual(actions & {"play", "queue", "favorite", "download"},
                         {"play", "queue", "favorite", "download"})

    def test_a_book_tile_offers_a_download(self):
        # The one affordance a Book has, and the only way to reach its
        # content at all.
        b = self.open_folder([])
        actions = {key for _label, _icon, key in b._tile_menu_entries(book())}
        self.assertIn("download", actions)
        self.assertNotIn("play", actions)

    def test_a_books_library_tile_gets_no_chip_either(self):
        # It would not have anyway (a CollectionType makes it a door), but
        # via the other branch — so this pins the outcome, not the route.
        b = self.browser([])
        self.assertFalse(b._tile_playable(
            {"Id": "lib-books", "Name": "Books", "Type": "CollectionFolder",
             "CollectionType": "books"}))


class TestAudiobookFolder(BooksHarness):

    def _page(self, b):
        return b._page_for(b.route)

    def test_a_folder_of_chapters_renders_as_a_track_list(self):
        b = self.open_folder([audiobook(i) for i in (1, 2, 3)])
        page = self._page(b)
        tracks = page._tracks()
        self.assertEqual([t["Id"] for t in tracks], ["ab1", "ab2", "ab3"])

    def test_chapters_play_in_index_order_not_load_order(self):
        # A rip is downloaded and listed in whatever order the server got
        # to; "chapter 7" means item 7.
        b = self.open_folder([audiobook(3), audiobook(1), audiobook(2)])
        tracks = self._page(b)._tracks()
        self.assertEqual([t["IndexNumber"] for t in tracks], [1, 2, 3])

    def test_an_untagged_rip_falls_back_to_the_name(self):
        chapters = [dict(audiobook(i), IndexNumber=None) for i in (2, 1)]
        b = self.open_folder(chapters)
        tracks = self._page(b)._tracks()
        self.assertEqual([t["Name"] for t in tracks],
                         ["Chapter 01", "Chapter 02"])

    def test_a_folder_of_books_is_not_a_track_list(self):
        b = self.open_folder([book("b1"), book("b2")])
        self.assertIsNone(self._page(b)._tracks())

    def test_a_folder_mixing_a_book_in_is_not_a_track_list(self):
        """Drawing it as an album would hide the book: there is no row in a
        track list that could open one."""
        b = self.open_folder([audiobook(1), book("b1")])
        self.assertIsNone(self._page(b)._tracks())

    def test_a_folder_that_is_not_fully_loaded_is_not_a_track_list(self):
        """A track list is a claim about a whole book. Playing "from track
        4" out of a windowed list whose later slots are still holes would
        queue blanks — so a folder too big to hold at once falls back to the
        paged grid rather than growing a paged track list."""
        b = self.open_folder([audiobook(i) for i in range(1, 4)])
        page = self._page(b)
        b.route["_total"] = 40          # 3 loaded, 40 in the folder
        self.assertIsNone(page._tracks())

    def test_clicking_a_chapter_plays_the_whole_book_from_there(self):
        b = self.open_folder([audiobook(i) for i in (1, 2, 3)])
        _n, handlers = build_scene(b)
        rows = [k for k in handlers if str(k).startswith("bktrk-")]
        self.assertTrue(rows, "no track rows were drawn")
        handlers["bktrk-1"]["click"]()
        queued, _srv, start = b.controller.played[-1]
        self.assertEqual(queued, ["ab1", "ab2", "ab3"])
        self.assertEqual(start, 1)

    def test_the_action_bar_has_no_shuffle(self):
        """Every other container-of-tracks screen offers it. These are the
        chapters of a book; a random order is not a feature anyone wants,
        and offering it beside Resume invites exactly one misclick."""
        b = self.open_folder([audiobook(i) for i in (1, 2)])
        nodes, _h = build_scene(b)
        drawn = ids(nodes)
        self.assertIn("bk-play", drawn)
        self.assertNotIn("bk-shuffle", drawn)

    def test_resume_starts_at_the_first_unfinished_chapter(self):
        chapters = [
            dict(audiobook(1), UserData={"Played": True}),
            dict(audiobook(2), UserData={"PlaybackPositionTicks": 5000000}),
            audiobook(3),
        ]
        b = self.open_folder(chapters)
        _n, handlers = build_scene(b)
        self.assertIn("bk-resume", handlers)
        handlers["bk-resume"]["click"]()
        _queued, _srv, start = b.controller.played[-1]
        self.assertEqual(start, 1, "resumed at the wrong chapter")

    def test_resume_carries_that_chapters_own_offset(self):
        """A rip's position lives on whichever chapter was playing, not on
        the book — so resuming is two numbers, and dropping the second
        restarts the chapter."""
        chapters = [
            dict(audiobook(1), UserData={"Played": True}),
            dict(audiobook(2), UserData={"PlaybackPositionTicks": 5000000}),
        ]
        b = self.open_folder(chapters)
        _n, handlers = build_scene(b)
        handlers["bk-resume"]["click"]()
        self.assertEqual(b.controller.play_offsets[-1], 5000000)

    def test_a_fresh_book_offers_no_resume(self):
        b = self.open_folder([audiobook(i) for i in (1, 2)])
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-resume", ids(nodes))
        self.assertIn("bk-play", ids(nodes))

    def test_the_second_button_says_restart(self):
        """"Play from Beginning" is what the tile menu calls it, where it
        sits under a Resume in a vertical list with room to spare. On an
        action row it is the longest label on screen and says the same
        thing as the shorter word."""
        chapters = [
            dict(audiobook(1), UserData={"PlaybackPositionTicks": 5000000}),
            audiobook(2),
        ]
        b = self.open_folder(chapters)
        nodes, _h = build_scene(b)
        labels = [str(n.get("text", "")) for n in nodes]
        self.assertIn("Restart", labels)
        self.assertNotIn("Play from Beginning", labels)

    def test_the_resume_label_drops_its_chapter_when_narrow(self):
        """Capping the name bounds how bad it gets; dropping it is what
        makes the row fit. "Resume" alone still says what the button does,
        which is the part that has to survive."""
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        tracks = [{"Name": "The Slow Crossing Part 01"}, {"Name": "b"}]
        wide = BooksPage._resume_label(tracks, 0,
                                       BooksPage.RESUME_NAME_MIN_W)
        narrow = BooksPage._resume_label(tracks, 0,
                                         BooksPage.RESUME_NAME_MIN_W - 1)
        self.assertIn("Slow", wide)
        self.assertEqual(narrow, "Resume")

    def test_the_action_row_fits_down_to_the_threshold(self):
        """The point of the drop, against the laid-out scene. Below ~860px
        the bare six-button row is itself wider than the window — that is a
        property of every action row in the app, not of this one, and is
        not what this test is about."""
        chapters = [
            dict(audiobook(i, album="A"),
                 Name="The Slow Crossing Part %02d" % i,
                 **({"UserData": {"PlaybackPositionTicks": 5000000}}
                    if i == 1 else {}))
            for i in (1, 2, 3)
        ]
        b = self.open_folder(chapters)
        for width in (1600, 1280, 1100, 1060, 1000, 950, 900, 870):
            nodes, _h = build_scene(b, size=(width, 720))
            drawn = [n for n in nodes
                     if n.get("x") is not None and n.get("w")]
            right = max(n["x"] + n["w"] for n in drawn)
            self.assertLessEqual(
                right, width + 1,
                "the action row overflows a %dpx window by %dpx"
                % (width, right - width))

    def test_a_started_book_has_no_plain_play_button(self):
        """On a film "Play" beside "Resume" is harmless. On a book it is a
        trap: the position is hours of listening spread over weeks, and
        starting from chapter one overwrites it as it goes. So the second
        button says out loud that it goes back to the beginning."""
        chapters = [
            dict(audiobook(1), UserData={"PlaybackPositionTicks": 5000000}),
            audiobook(2),
        ]
        b = self.open_folder(chapters)
        nodes, _h = build_scene(b)
        labels = [str(n.get("text", "")) for n in nodes]
        self.assertIn("bk-resume", ids(nodes))
        self.assertNotIn("Play", labels,
                         "a bare Play button is still offered on a book "
                         "that has been started")
        self.assertIn("Restart", labels)

    def test_play_from_beginning_really_does_start_at_zero(self):
        chapters = [
            dict(audiobook(1), UserData={"PlaybackPositionTicks": 5000000}),
            audiobook(2),
        ]
        b = self.open_folder(chapters)
        _n, handlers = build_scene(b)
        handlers["bk-play"]["click"]()
        _queued, _srv, start = b.controller.played[-1]
        self.assertEqual(start, 0)
        self.assertIsNone(b.controller.play_offsets[-1])

    def test_a_finished_book_offers_play_rather_than_resume(self):
        # Every chapter played: there is nothing to resume, and starting
        # over is the only sensible offer.
        chapters = [dict(audiobook(i), UserData={"Played": True})
                    for i in (1, 2)]
        b = self.open_folder(chapters)
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-resume", ids(nodes))
        self.assertIn("bk-play", ids(nodes))

    def test_the_folders_own_item_heads_the_page(self):
        b = self.open_folder(
            [audiobook(1)],
            folder={"Id": "folder1", "Name": "The Divided Account",
                    "Type": "Folder", "AlbumArtist": "Gus Gupta"})
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("The Divided Account", text)
        self.assertIn("Gus Gupta", text)


class TestFolderProgress(BooksHarness):
    """A shelf of audiobooks has to say which ones you have finished.

    Everything here rides on a Folder's own UserData, which the server
    maintains across its children: `Played` when it is marked, and
    `PlayedPercentage` / `UnplayedItemCount` as the chapters are listened
    to. Verified against a live server — marking a folder played cascades
    to its six chapters and back again.
    """

    @staticmethod
    def _folder(**data):
        return {"Id": "folder1", "Name": "The Divided Account",
                "Type": "Folder", "IsFolder": True, "UserData": data}

    def _tile(self, item):
        b = self.open_folder([])
        return b.tiles._tile(item, b.tiles.art.geom, "Primary")

    def test_a_finished_book_gets_a_tick(self):
        self.assertTrue(self._tile(
            self._folder(Played=True, UnplayedItemCount=0)).watched)

    def test_listening_through_every_chapter_counts_as_finished(self):
        """Nothing sets Played on the folder when you simply listen through
        it — only marking it by hand does — so a book you had actually
        finished showed no tick at all, which is the one thing a shelf of
        them needs to say."""
        self.assertTrue(self._tile(
            self._folder(Played=False, UnplayedItemCount=0)).watched)

    def test_an_unstarted_book_gets_no_tick(self):
        self.assertFalse(self._tile(
            self._folder(Played=False, UnplayedItemCount=6)).watched)

    def test_a_part_read_book_gets_a_progress_bar(self):
        """A container has no position and no runtime, so the usual
        position/runtime ratio is always zero for one. PlayedPercentage is
        the server's own answer across the children — which for an
        audiobook folder is exactly how far through the book you are."""
        tile = self._tile(self._folder(PlayedPercentage=40,
                                       UnplayedItemCount=4))
        self.assertAlmostEqual(tile.progress, 0.4)

    def test_an_unstarted_book_has_no_bar(self):
        self.assertEqual(
            self._tile(self._folder(PlayedPercentage=0,
                                    UnplayedItemCount=6)).progress, 0.0)

    def test_the_context_menu_can_mark_a_book_finished(self):
        b = self.open_folder([])
        actions = {key for _l, _i, key in b._tile_menu_entries(
            self._folder(UnplayedItemCount=6))}
        self.assertIn("watched", actions)

    def test_marking_a_folder_flips_the_tick_without_a_reload(self):
        """The optimistic flip has to move the COUNT too. is_watched reads
        it for a container, so setting Played alone leaves the tick
        recomputed from a stale count that still says unfinished."""
        b = self.open_folder([])
        folder = self._folder(Played=False, UnplayedItemCount=6)
        b._actions.toggle_watched(folder, "srv1")
        from jellyfin_mpv_shim.mpvtk_browser import components
        self.assertTrue(components.is_watched(folder))
        self.assertEqual(folder["UserData"]["UnplayedItemCount"], 0)

    def test_un_marking_a_folder_restores_it(self):
        b = self.open_folder([])
        folder = self._folder(Played=True, UnplayedItemCount=0)
        b._actions.toggle_watched(folder, "srv1")
        from jellyfin_mpv_shim.mpvtk_browser import components
        self.assertFalse(components.is_watched(folder))

    def test_marking_survives_several_round_trips(self):
        """On/off/on: the rollback path restores three fields, and a
        partial restore leaves the tile disagreeing with the server in a
        way one toggle cannot show."""
        b = self.open_folder([])
        folder = self._folder(Played=False, UnplayedItemCount=6,
                              PlayedPercentage=0)
        from jellyfin_mpv_shim.mpvtk_browser import components
        for expected in (True, False, True, False):
            b._actions.toggle_watched(folder, "srv1")
            self.assertIs(components.is_watched(folder), expected)


class TestLibraryWidePlayButtons(BooksHarness):
    """A books library gets neither Play All nor Shuffle.

    Half of one cannot be played at all (a Book is dropped from the queue
    silently), and the other half is audiobooks — where a library-wide
    queue is every chapter of every book from the beginning, which
    overwrites hours of position as it plays.
    """

    def _bar(self, collection_type):
        b = self.browser([])
        b.navigate({"kind": "books" if collection_type == "books" else "grid",
                    "server": "srv1", "parent_id": "p",
                    "collection_type": collection_type, "title": "L"})
        nodes, _h = build_scene(b)
        return ids(nodes)

    def test_a_books_library_offers_neither(self):
        drawn = self._bar("books")
        self.assertNotIn("grid-playall", drawn)
        self.assertNotIn("grid-shuffle", drawn)

    def test_a_movies_library_still_offers_both(self):
        drawn = self._bar("movies")
        self.assertIn("grid-playall", drawn)
        self.assertIn("grid-shuffle", drawn)

    def test_a_tv_library_still_offers_shuffle_only(self):
        # The pre-existing rule, which this must not have widened: a random
        # episode is a reasonable ask, "every episode in name order" is not.
        drawn = self._bar("tvshows")
        self.assertNotIn("grid-playall", drawn)
        self.assertIn("grid-shuffle", drawn)


class TestOneBookOrSeveral(BooksHarness):
    """`Books/Kai Kowalski/The Slow Crossing/` holding three parts of one
    book is a chapter list; `Books/Elena Farrow/` holding four different
    novels as one file each is a gallery of four books.

    Both are "a folder of AudioBooks" and nothing about the folder tells
    them apart — `Album` is the only field that ever joins a rip.
    """

    def _page(self, b):
        return b._page_for(b.route)

    def test_one_album_is_one_book(self):
        b = self.open_folder([audiobook(i, album="A Book") for i in (1, 2)])
        self.assertIsNotNone(self._page(b)._tracks())

    def test_several_albums_are_several_books(self):
        b = self.open_folder([audiobook(1, album="Book One"),
                              audiobook(2, album="Book Two")])
        self.assertIsNone(self._page(b)._tracks(),
                          "two different books were drawn as one chapter "
                          "list, so one of them is unreachable")

    def test_an_untagged_folder_is_still_one_book(self):
        """No album at all is the case the folder was *made* the unit for:
        there is genuinely nothing else joining the files, so reading it as
        N separate books would make an untagged rip unplayable as a book."""
        b = self.open_folder([dict(audiobook(i), Album=None)
                              for i in (1, 2, 3)])
        tracks = self._page(b)._tracks()
        self.assertIsNotNone(tracks)
        self.assertEqual(len(tracks), 3)

    def test_a_half_tagged_folder_is_several(self):
        # The tagged one names a book the untagged one is not claiming to
        # be part of, so they are not chapters of the same thing.
        b = self.open_folder([audiobook(1, album="A Book"),
                              dict(audiobook(2), Album=None)])
        self.assertIsNone(self._page(b)._tracks())

    def test_a_lone_audiobook_is_a_book(self):
        b = self.open_folder([audiobook(1, album="A Book")])
        self.assertIsNotNone(self._page(b)._tracks())

    def test_the_rule_is_pure(self):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import one_book

        self.assertTrue(one_book([{"Album": "X"}, {"Album": "X"}]))
        self.assertFalse(one_book([{"Album": "X"}, {"Album": "Y"}]))
        self.assertTrue(one_book([{}, {}]))
        self.assertTrue(one_book([{"Album": "  X  "}, {"Album": "X"}]),
                        "whitespace made two books out of one")


class TestChapterListMarks(BooksHarness):
    """Which chapters are behind you is the whole state of a book — it is
    what Resume is computed from — and marking one had no visible effect
    anywhere before this."""

    #: The tick's size, which is what tells it from the chrome's icons --
    #: the icon PATH is stubbed in the test renderer, so the drawn shape is
    #: not something a scene assertion can read.
    TICK_H = 15

    def _ticks(self, played):
        from jellyfin_mpv_shim.mpvtk_browser import theme

        chapters = []
        for i in (1, 2, 3):
            track = audiobook(i, album="A Book")
            if i in played:
                track["UserData"] = {"Played": True}
            chapters.append(track)
        b = self.open_folder(chapters)
        nodes, _h = build_scene(b)
        return [n for n in nodes
                if n.get("t") == "icon" and n.get("c") == theme.ACCENT
                and n.get("h") == self.TICK_H]

    def test_a_tick_appears_for_each_played_chapter(self):
        """Counted rather than merely present: "some accent icon is on the
        screen" would pass on the chrome, and one tick for three played
        chapters is the bug this is really guarding."""
        self.assertEqual(len(self._ticks(played=())), 0)
        self.assertEqual(len(self._ticks(played=(1,))), 1)
        self.assertEqual(len(self._ticks(played=(1, 2))), 2)
        self.assertEqual(len(self._ticks(played=(1, 2, 3))), 3)

    def test_a_music_playlist_gets_no_tick_column(self):
        """Off by default: nobody tracks which songs they have heard, and a
        dead column on every playlist is worse than the information is
        worth."""
        import inspect
        from jellyfin_mpv_shim.mpvtk_browser import tile_renderer

        sig = inspect.signature(tile_renderer.TileRenderer.track_list)
        self.assertIs(sig.parameters["watched"].default, False)


class TestResumeLabel(BooksHarness):
    """A chapter name is whatever the ripper typed and can be a sentence.
    The action row is a fixed row of buttons, so one long label pushes
    Download off the right edge of the window."""

    def _label(self, tracks, index):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        return BooksPage._resume_label(tracks, index)

    def test_a_long_chapter_name_is_cut(self):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        tracks = [{"Name": "The Slow Crossing Part 01 And Then Some More"},
                  {"Name": "b"}]
        label = self._label(tracks, 0)
        self.assertTrue(label.endswith("…"))
        self.assertLess(len(label), 12 + BooksPage.RESUME_LABEL_MAX)

    def test_a_short_name_is_kept_whole(self):
        tracks = [{"Name": "Chapter 4"}, {"Name": "Chapter 5"}]
        self.assertIn("Chapter 4", self._label(tracks, 0))

    def test_a_single_file_book_names_no_chapter(self):
        # There is one thing to resume, and repeating the page's own title
        # on the button that resumes it is noise.
        self.assertEqual(self._label([{"Name": "The Whole Book"}], 0),
                         "Resume")

    def test_an_unnamed_chapter_says_where_it_is(self):
        tracks = [{"Name": ""}, {"Name": ""}, {"Name": ""}]
        self.assertIn("2/3", self._label(tracks, 1))

    def test_the_action_row_still_fits_a_long_name(self):
        """The point of the cap, asserted against the laid-out scene rather
        than the string."""
        long_name = "The Slow Crossing Part 01 And Then Some More Words"
        b = self.open_folder([
            dict(audiobook(1, album="A"), Name=long_name,
                 UserData={"PlaybackPositionTicks": 5000000}),
            audiobook(2, album="A"),
        ])
        nodes, _h = build_scene(b, size=(1280, 720))
        drawn = [n for n in nodes if n.get("x") is not None and n.get("w")]
        right = max(n["x"] + n["w"] for n in drawn)
        self.assertLessEqual(right, 1281,
                             "the action row overflows by %dpx"
                             % (right - 1280))


class TestAudiobookOverview(BooksHarness):

    def test_the_folders_own_description_wins(self):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        folder = {"Overview": "A folder note."}
        tracks = [{"Overview": "A chapter note."}]
        self.assertEqual(BooksPage._overview(folder, tracks),
                         "A folder note.")

    def test_a_books_description_comes_off_its_files(self):
        """An audiobook's description is written into the FILE's tags, so
        it is on the chapter items and on nothing else — the directory does
        not have it unless someone wrote an .nfo."""
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        self.assertEqual(
            BooksPage._overview({}, [{"Overview": "A chapter note."}]),
            "A chapter note.")

    def test_no_description_anywhere_is_empty(self):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        self.assertEqual(BooksPage._overview({}, [{}]), "")
        self.assertEqual(BooksPage._overview({}, []), "")

    def test_it_is_drawn_on_the_page(self):
        # The folder itself carries none, which is the normal case: an
        # audiobook's description is in the file's tags.
        b = self.open_folder(
            [dict(audiobook(1, album="A"), Overview="Read by the author.")],
            folder={"Id": "folder1", "Name": "A Book", "Type": "Folder"})
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("Read by the author.", text)

    def test_a_books_grid_asks_the_server_for_it(self):
        """GRID_FIELDS drops Overview because a hundred-item grid does not
        draw it. A books folder is a book, and its description is drawn on
        that very screen."""
        self.open_folder([])
        query = self.src.queries[-1]
        self.assertEqual(query["collection_type"], "books")
        from jellyfin_mpv_shim.mpvtk_browser import repository
        self.assertIn("Overview", repository.BOOKS_GRID_FIELDS)
        self.assertNotIn("Overview", repository.GRID_FIELDS)


class TestDownloadingAnAudiobook(BooksHarness):

    def test_a_folder_with_nothing_downloaded_offers_download(self):
        b = self.open_folder([audiobook(i) for i in (1, 2)])
        nodes, _h = build_scene(b)
        self.assertIn("bk-download", ids(nodes))

    def test_a_fully_downloaded_book_offers_removal(self):
        """A folder is never itself a downloads row, so "is this
        downloaded" has to be asked of its chapters."""
        b = self.open_folder([audiobook(i) for i in (1, 2)])
        b.tiles.set_downloaded({"ab1", "ab2"}, set(), set(), set())
        nodes, _h = build_scene(b)
        self.assertIn("bk-undownload", ids(nodes))
        self.assertNotIn("bk-download", ids(nodes))

    def test_a_half_downloaded_book_still_offers_download(self):
        # All of them, not any: a book half on disk is not one you can
        # listen to on a train.
        b = self.open_folder([audiobook(i) for i in (1, 2)])
        b.tiles.set_downloaded({"ab1"}, set(), set(), set())
        nodes, _h = build_scene(b)
        self.assertIn("bk-download", ids(nodes))

    def test_download_still_names_the_folder_if_its_dto_never_arrived(self):
        """The header falls back to the route's title when the folder's own
        DTO has not landed — and the Download button hands that same dict to
        the estimate. Without the id and type on the fallback it would offer
        to download nothing at all, which looks exactly like a download that
        found nothing to do."""
        b = self.open_folder([audiobook(1)])
        b.route.pop("_folder", None)
        _n, handlers = build_scene(b)
        handlers["bk-download"]["click"]()
        self.assertIsNotNone(b._dl)
        self.assertEqual(b._dl["item"]["Id"], "folder1")
        self.assertEqual(b._dl["item"]["Type"], "Folder")

    def test_removing_names_every_chapter(self):
        b = self.open_folder([audiobook(i) for i in (1, 2)])
        b.tiles.set_downloaded({"ab1", "ab2"}, set(), set(), set())
        _n, handlers = build_scene(b)
        handlers["bk-undownload"]["click"]()
        # Confirm first — this is destructive.
        self.assertIsNotNone(b._dialog)
        _n2, h2 = build_scene(b)
        h2["dlg-ok"]["click"]()
        self.assertEqual(b.controller.deleted_downloads, [["ab1", "ab2"]])


class TestAudiobookBar(BooksHarness):
    """What the now-playing bar grows for an audiobook.

    A song is three minutes and the scrubber covers it. A book is hours,
    listened to over weeks and in a single item — a .m4b is ONE file with
    the whole book in it — so the bar needs to say where you are in the
    *book* and to move you around inside it. None of this appears for
    music, which is asserted as hard as its presence is.
    """

    CHAPTERS = [{"title": "One", "time": 0.0},
                {"title": "Two", "time": 300.0},
                {"title": "Three", "time": 600.0}]

    def _playing(self, **extra):
        b = self.browser([])
        state = {"stopped": False, "is_audio": True, "title": "The Book",
                 "position": 310, "duration": 900, "volume": 50,
                 # A rip by default: several entries, so prev/next mean
                 # something. The solo .m4b case sets queue_len=1.
                 "queue_len": 6}
        state.update(extra)
        b.on_playstate(state)
        return b

    # -- skip buttons ------------------------------------------------------

    def test_an_audiobook_gets_skip_buttons(self):
        b = self._playing(is_audiobook=True)
        nodes, _h = build_scene(b)
        self.assertIn("np-back", ids(nodes))
        self.assertIn("np-forward", ids(nodes))

    def test_a_song_gets_none(self):
        b = self._playing()
        nodes, _h = build_scene(b)
        self.assertNotIn("np-back", ids(nodes))
        self.assertNotIn("np-forward", ids(nodes))

    def test_the_skips_are_relative_and_asymmetric(self):
        """Back 10 and forward 30, which every audiobook player uses and
        for a reason: "say that again" and "skip the recap" are different
        sized problems. Relative through the player so it goes round
        SyncPlay, rather than an absolute seek worked out here."""
        b = self._playing(is_audiobook=True)
        _n, handlers = build_scene(b)
        handlers["np-back"]["click"]()
        handlers["np-forward"]["click"]()
        seeks = [(name, args) for name, args in b.controller.transport
                 if name == "seek_relative"]
        self.assertEqual([a[0] for _n, a in seeks], [-10, 30])

    # -- chapter ticks -----------------------------------------------------

    def test_the_scrubber_carries_chapter_ticks(self):
        """On a ten-hour .m4b the bar is the only thing that says where you
        are in the book rather than in the file; without them a scrub is a
        blind drag."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        nodes, _h = build_scene(b)
        seek = [n for n in nodes if n.get("id") == "np-seek"][0]
        self.assertEqual([round(m, 4) for m in seek.get("marks") or []],
                         [round(300 / 900, 4), round(600 / 900, 4)])

    def test_a_mark_at_zero_is_not_drawn(self):
        # The first chapter starts at the left edge, where a tick is a
        # smudge on the track rather than information.
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        nodes, _h = build_scene(b)
        seek = [n for n in nodes if n.get("id") == "np-seek"][0]
        self.assertNotIn(0.0, seek.get("marks") or [])

    def test_a_song_has_no_ticks(self):
        b = self._playing()
        nodes, _h = build_scene(b)
        seek = [n for n in nodes if n.get("id") == "np-seek"][0]
        self.assertFalse(seek.get("marks"))

    # -- layout ------------------------------------------------------------

    #: The transport, in the order it must appear. Stop is NOT here: it
    #: ends playback rather than stepping through it, so it lives with the
    #: right-hand cluster (see test_stop_is_not_in_the_transport).
    TRANSPORT = ["np-prev", "np-chprev", "np-back", "np-pp", "np-forward",
                 "np-chnext", "np-next"]

    def _order(self, b, wanted, size=(1600, 720)):
        nodes, _h = build_scene(b, size=size)
        seen = [n["id"] for n in nodes
                if n.get("id") in wanted and n.get("t") == "rect"]
        # Nodes come out in build order; de-duplicate keeping first sight.
        return list(dict.fromkeys(seen))

    def test_the_transport_nests_around_play_pause(self):
        """The playback HUD's order, matched exactly: the further from the
        centre a button is, the bigger the jump it makes. Anyone who has
        used the video HUD already knows where to press."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        self.assertEqual(self._order(b, self.TRANSPORT), self.TRANSPORT)

    def test_stop_is_not_in_the_transport(self):
        """It ends playback rather than stepping through it, and sitting
        immediately after Next it was one slip from a skip. The playback
        HUD carries no stop button at all for the same reason."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        order = self._order(b, self.TRANSPORT + ["np-stop", "np-fav"])
        self.assertLess(order.index("np-next"), order.index("np-stop"))
        self.assertLess(order.index("np-stop"), order.index("np-fav"),
                        "stop is not with the right-hand cluster")

    def test_the_transport_is_centred_in_the_bar(self):
        """Equal flex on the two outer groups is what centres it. A single
        trailing spacer only left-packs it, and flexing the gaps either
        side of a fixed title and a variable right-hand cluster centres it
        between those two rather than in the bar — so it drifts as controls
        are shed."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        for width in (1600, 1280, 1100):
            nodes, _h = build_scene(b, size=(width, 720))
            pp = [n for n in nodes if n.get("id") == "np-pp"][0]
            middle = pp["x"] + pp["w"] / 2
            self.assertAlmostEqual(
                middle, width / 2, delta=24,
                msg="play/pause sits %.0fpx from the centre of a %dpx bar"
                    % (abs(middle - width / 2), width))

    def test_the_chapter_steps_use_the_huds_glyphs(self):
        """undo/redo, not fast_rewind/fast_forward. The HUD gives chapter
        navigation those two, and the scanning arrows read as scanning —
        which is not what these do."""
        import inspect
        from jellyfin_mpv_shim.mpvtk_browser import music
        src = inspect.getsource(music.MusicMixin._transport)
        self.assertIn('"undo", "np-chprev"', src)
        self.assertIn('"redo", "np-chnext"', src)

    def test_the_chapter_list_sits_with_the_other_pickers(self):
        """Not among the transport buttons. It is a place to choose from,
        like the queue button beside it — and wedged between the scrubber
        and the volume it stranded the two chapter arrows away from the
        play button they step around."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        nodes, _h = build_scene(b)
        order = list(dict.fromkeys(
            n["id"] for n in nodes
            if n.get("id") in ("np-seek", "np-chapters", "np-queue")))
        self.assertEqual(order, ["np-seek", "np-chapters", "np-queue"])

    def _bar_ids(self, width, **state):
        b = self._playing(**state)
        nodes, _h = build_scene(b, size=(width, 720))
        return ids(nodes)

    def test_the_bar_sheds_controls_as_it_narrows(self):
        """Thirteen controls in a fixed row squeezed the seek slider — the
        one thing on the bar that has to be draggable — down to nothing.
        So it sheds, as the playback HUD does."""
        wide = self._bar_ids(1280, is_audiobook=True, chapters=self.CHAPTERS)
        for nid in ("np-chapters", "np-chprev", "np-back", "np-vol",
                    "np-repeat", "np-fav", "np-stop"):
            self.assertIn(nid, wide)

        narrow = self._bar_ids(500, is_audiobook=True,
                               chapters=self.CHAPTERS)
        for nid in ("np-chapters", "np-vol", "np-queue", "np-stop",
                    "np-repeat", "np-fav"):
            self.assertNotIn(nid, narrow, "%s survived a 500px bar" % nid)

    def test_a_book_gives_up_the_heart_before_its_chapters(self):
        """What is worth dropping depends on what is playing, which is why
        this is a priority list and not a table of per-control widths.
        Chapter navigation is the reason the bar is any use on a ten-hour
        item; the favourite heart is not."""
        drawn = self._bar_ids(600, is_audiobook=True,
                              chapters=self.CHAPTERS)
        self.assertIn("np-back", drawn)
        self.assertNotIn("np-fav", drawn)
        self.assertNotIn("np-repeat", drawn)

    def test_the_order_things_are_given_up_in(self):
        """Repeat, then the heart, then the chapter step buttons.

        Repeat and favourite are one-press state you set once and forget,
        and neither has anything to do with getting through what is
        playing. The chapter arrows do, which is why they outlive both —
        and they go before the chapter LIST, because the list can still
        reach every chapter on its own while losing it costs the only way
        to jump.
        """
        first_gone = []
        seen = None
        watch = ("np-repeat", "np-fav", "np-chprev", "np-chapters",
                 "np-stop", "np-queue", "np-vol", "np-back")
        for width in range(1300, 399, -20):
            drawn = self._bar_ids(width, is_audiobook=True,
                                  chapters=self.CHAPTERS)
            now = {nid for nid in watch if nid in drawn}
            if seen is not None:
                first_gone += sorted(seen - now)
            seen = now
        self.assertEqual(first_gone[:3],
                         ["np-repeat", "np-fav", "np-chprev"],
                         "the bar sheds in the wrong order: %s" % first_gone)
        self.assertLess(first_gone.index("np-chprev"),
                        first_gone.index("np-chapters"),
                        "the chapter LIST went before the arrows, which "
                        "leaves no way to jump to a chapter at all")
        self.assertEqual(first_gone[-1], "np-back",
                         "the skip buttons should outlive everything else "
                         "on a book")

    def test_a_song_gives_up_its_transport_last(self):
        # The same shape from the other side: a song has no chapter or skip
        # controls to spend the room on, so what survives at a width that
        # strips a book down to its chapter arrows is the ordinary set.
        drawn = self._bar_ids(600)
        self.assertIn("np-vol", drawn)
        self.assertNotIn("np-chprev", drawn)
        self.assertNotIn("np-back", drawn)

    def test_shedding_is_monotone_in_width(self):
        """The longest PREFIX that fits, not a greedy best fit: narrowing
        must only ever take controls away and widening only ever bring them
        back, or a cheap control pops in and out as an expensive one comes
        and goes.

        This has to hold *across* the two-row boundary too, which is what
        killed the stepped title column: shrinking the title freed more
        room than the cheapest control cost, so dragging the window edge
        inwards past one of its steps made a button appear.
        """
        optional = ("np-chapters", "np-chprev", "np-back", "np-vol",
                    "np-queue", "np-stop", "np-repeat", "np-fav")
        for state in ({"is_audiobook": True, "chapters": self.CHAPTERS},
                      {}):
            seen = None
            for width in range(1600, 399, -20):
                drawn = self._bar_ids(width, **state)
                now = {nid for nid in optional if nid in drawn}
                if seen is not None:
                    self.assertTrue(
                        now <= seen,
                        "%dpx brought back %s that a wider bar had dropped"
                        % (width, sorted(now - seen)))
                seen = now

    def test_the_scrubber_gets_its_own_row_on_a_book(self):
        """A book is one long item and the scrubber is how you move around
        inside it, so it earns the full width rather than whatever is left
        after eleven buttons."""
        from jellyfin_mpv_shim.mpvtk_browser.music import MusicMixin

        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        nodes, _h = build_scene(b, size=(1600, 720))
        seek = [n for n in nodes if n.get("id") == "np-seek"][0]
        self.assertGreater(seek["w"], 1000,
                           "the scrubber is still sharing a row")
        self.assertTrue(MusicMixin.np_two_row(b._now_playing, 1600))

    def test_a_song_keeps_one_row_while_there_is_room(self):
        from jellyfin_mpv_shim.mpvtk_browser.music import MusicMixin

        b = self._playing()
        self.assertFalse(MusicMixin.np_two_row(b._now_playing, 1600))
        self.assertTrue(MusicMixin.np_two_row(b._now_playing, 800),
                        "a narrow music bar still crams one row")

    def test_the_page_is_laid_out_against_the_height_that_is_drawn(self):
        """The bar is two rows for a book, so the content height has to
        follow it — subtracting a constant lays the page out against the
        wrong remainder and the bottom of it goes behind the bar."""
        from jellyfin_mpv_shim.mpvtk_browser import music

        one = music.now_playing_bar_h({"is_audio": True}, 1600)
        two = music.now_playing_bar_h({"is_audio": True,
                                       "is_audiobook": True}, 1600)
        self.assertEqual(one, music.NOW_PLAYING_BAR_H)
        self.assertEqual(two, music.NOW_PLAYING_BAR_H2)
        self.assertGreater(two, one)

    # -- prev/next on a solo book -----------------------------------------

    def test_a_solo_chaptered_book_has_no_track_buttons(self):
        """A single .m4b IS the book, so previous/next track have nowhere
        to go — pressing either can only end playback. They also sit
        immediately outside the chapter arrows, so the two pairs read as a
        set and half of them are traps."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS,
                          queue_len=1)
        nodes, _h = build_scene(b)
        self.assertNotIn("np-prev", ids(nodes))
        self.assertNotIn("np-next", ids(nodes))
        self.assertIn("np-chprev", ids(nodes))

    def test_a_rip_keeps_them(self):
        # There a chapter IS a queue entry, so prev/next are exactly right.
        b = self._playing(is_audiobook=True, queue_len=6)
        nodes, _h = build_scene(b)
        self.assertIn("np-prev", ids(nodes))
        self.assertIn("np-next", ids(nodes))

    def test_a_lone_song_keeps_them(self):
        # No chapters, so nothing has replaced them — and this is what the
        # bar has always done for music.
        b = self._playing(queue_len=1)
        nodes, _h = build_scene(b)
        self.assertIn("np-prev", ids(nodes))
        self.assertIn("np-next", ids(nodes))

    def test_a_chaptered_book_in_a_queue_keeps_them(self):
        # Both conditions are required: several .m4b files queued together
        # still need prev/next to move between the books.
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS,
                          queue_len=3)
        nodes, _h = build_scene(b)
        self.assertIn("np-prev", ids(nodes))

    def test_what_the_bar_never_gives_up(self):
        """Play/pause, the scrubber and prev/next. Those ARE the bar; a
        window too narrow for them is one the bar should not be in."""
        for width in (1280, 900, 700, 520, 400):
            drawn = self._bar_ids(width, is_audiobook=True,
                                  chapters=self.CHAPTERS)
            for nid in ("np-pp", "np-seek", "np-prev", "np-next"):
                self.assertIn(nid, drawn, "%s went at %dpx" % (nid, width))

    def test_the_bar_fits_the_window_at_every_width(self):
        """The actual complaint, asserted against the laid-out scene rather
        than against the tier table: nothing may run off the right edge,
        and the scrubber must keep a draggable width."""
        for width in (1280, 1024, 940, 860, 760, 700, 620, 560, 470, 420):
            b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
            nodes, _h = build_scene(b, size=(width, 720))
            drawn = [n for n in nodes
                     if n.get("x") is not None and n.get("w")]
            right = max(n["x"] + n["w"] for n in drawn)
            self.assertLessEqual(
                right, width + 1,
                "the bar overflows a %dpx window by %dpx"
                % (width, right - width))
            seek = [n for n in nodes if n.get("id") == "np-seek"][0]
            self.assertGreaterEqual(
                seek["w"], 80,
                "the scrubber is only %dpx wide at %dpx"
                % (seek["w"], width))

    # -- the chapter picker ------------------------------------------------

    def test_the_chapter_picker_is_a_button_not_a_hud_glyph(self):
        """It sits in a row of filled square buttons on panel chrome. The
        chromeless treatment is right for the playback HUD, which floats
        over video; among these it reads as a different kind of control."""
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        nodes, _h = build_scene(b)
        picker = [n for n in nodes if n.get("id") == "np-chapters"][0]
        self.assertTrue(picker.get("tchip"),
                        "the chapter picker is still drawn HUD-style")
        self.assertEqual(len(picker["tchip"]), 3)

    def test_the_picker_selects_the_chapter_being_played(self):
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        nodes, _h = build_scene(b)
        picker = [n for n in nodes if n.get("id") == "np-chapters"][0]
        self.assertEqual(picker.get("sel"), 1, "position 310 is chapter two")

    def test_picking_a_chapter_seeks_to_its_start(self):
        b = self._playing(is_audiobook=True, chapters=self.CHAPTERS)
        _n, handlers = build_scene(b)
        handlers["np-chapters"]["select"](2, "Three")
        seeks = [args for name, args in b.controller.transport
                 if name == "seek"]
        self.assertEqual(seeks[-1][0], 600.0)

    def test_no_chapters_means_no_picker(self):
        b = self._playing(is_audiobook=True)
        nodes, _h = build_scene(b)
        self.assertNotIn("np-chapters", ids(nodes))


class BookPageHarness(BooksHarness):

    def open_book(self, item=None, state=(None, None), allowed=True):
        controller = FakeController()
        b = self.browser([], controller=controller)
        item = item or book()
        self.src.items["bk1"] = item
        self.src.download_allowed = allowed
        controller.book_downloads["bk1"] = state
        b.navigate({"kind": "book", "server": "srv1", "item_id": "bk1",
                    "title": "A Novel"})
        return b


class TestBookPage(BookPageHarness):

    def test_there_is_no_play_button(self):
        """And there will not be one. The server serves no page, no archive
        entry and no spine document, so every way of showing a book in this
        window begins by fetching the whole file."""
        b = self.open_book()
        nodes, _h = build_scene(b)
        drawn = ids(nodes)
        self.assertNotIn("btn-play", drawn)
        self.assertNotIn("btn-resume", drawn)
        self.assertIn("bk-read", drawn)

    def test_the_format_and_page_count_are_shown(self):
        b = self.open_book(book(ticks=None, RunTimeTicks=60000,
                                path="/l/A Manual.pdf"))
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("PDF", text)
        self.assertIn("6 pages", text)

    def test_progress_is_shown_where_the_format_has_any(self):
        b = self.open_book(book(RunTimeTicks=60000, path="/l/A Manual.pdf",
                                UserData={"PlaybackPositionTicks": 30000}))
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("Page 4 of 6", text)

    def test_download_fetches_without_opening(self):
        """Download and Read are different buttons now. A Download that also
        launched a reader leaves no way to ask for the copy without the
        window — and is a surprise, since nothing else in the app opens
        anything when you download it."""
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-download"]["click"]()
        self.assertTrue(b.controller.enqueued)
        self.assertEqual(b.controller.opened, [])

    def test_a_download_that_lands_does_not_open_the_book(self):
        """The pending-open set is Read's, not Download's. Sharing it would
        make a Download pop a reader open some seconds later, which is the
        same surprise arriving late."""
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-download"]["click"]()
        b.controller.book_downloads["bk1"] = ("complete", "/store/media.epub")
        b._actions.flush_pending_reads()
        self.assertEqual(b.controller.opened, [])

    def test_downloading_something_already_on_disk_says_so(self):
        # Rather than silently doing nothing, which is what a second press
        # of a button that has not yet re-rendered would otherwise be.
        b = self.open_book(state=("complete", "/store/media.epub"))
        b._actions.download_book(b.route["_data"]["item"], "srv1")
        self.assertIn("already downloaded", b.status or "")
        self.assertEqual(b.controller.opened, [])

    def test_reading_an_undownloaded_book_fetches_it(self):
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        # The enqueue goes through the transport recorder on the fake.
        self.assertTrue(b.controller.enqueued, "Read did not fetch the book")
        self.assertEqual(b.controller.opened, [],
                         "it opened a file it had not downloaded")

    def test_reading_a_downloaded_book_opens_it_straight_away(self):
        b = self.open_book(state=("complete", "/store/media.epub"))
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        self.assertEqual(b.controller.opened, ["bk1"])

    def test_a_download_that_lands_later_opens_the_book(self):
        """Read is one gesture over two steps, and the second is driven by
        the catalog's own change notification rather than by a poller per
        press — a poller would outlive the press."""
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        self.assertEqual(b.controller.opened, [])
        b.controller.book_downloads["bk1"] = ("complete", "/store/media.epub")
        b._actions.flush_pending_reads()
        self.assertEqual(b.controller.opened, ["bk1"])

    def test_a_pending_read_is_only_opened_once(self):
        """The change hook fires for every catalog write, including the ones
        after this book finished. Opening a new reader window on each is the
        multi-pass failure this guards."""
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        b.controller.book_downloads["bk1"] = ("complete", "/store/media.epub")
        for _pass in range(4):
            b._actions.flush_pending_reads()
        self.assertEqual(b.controller.opened, ["bk1"])

    def test_a_download_that_fails_stops_being_waited_on(self):
        # Left in the set it would be retried against a row that is never
        # going to complete, and the user would be told nothing at all.
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        b.controller.book_downloads["bk1"] = ("error", None)
        b._actions.flush_pending_reads()
        b.controller.book_downloads["bk1"] = ("complete", "/store/media.epub")
        b._actions.flush_pending_reads()
        self.assertEqual(b.controller.opened, [],
                         "a failed download still opened later")

    def test_the_buttons_follow_the_catalog_without_a_reload(self):
        """The state is resolved once, in load(), because answering it stats
        a file and render runs per frame. Every button on this page is a
        thing that moves that answer, so it has to be re-read when the
        catalog changes — otherwise it said "Download" through the whole
        download and for as long as the page stayed open."""
        b = self.open_book()
        nodes, _h = build_scene(b)
        self.assertIn("bk-download", ids(nodes))

        b.controller.book_downloads["bk1"] = ("downloading", None)
        b.on_downloads_changed()
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-download", ids(nodes))

        b.controller.book_downloads["bk1"] = ("complete", "/store/media.epub")
        b.on_downloads_changed()
        nodes, _h = build_scene(b)
        self.assertIn("bk-undownload", ids(nodes))

        # And back again, so this is not a one-way latch: removing the
        # download has to restore the Download button.
        b.controller.book_downloads["bk1"] = (None, None)
        b.on_downloads_changed()
        nodes, _h = build_scene(b)
        self.assertIn("bk-download", ids(nodes))
        self.assertNotIn("bk-undownload", ids(nodes))

    def test_pressing_download_updates_the_button_under_the_press(self):
        """Not on the sync worker's next notification: the action re-reads
        the catalog itself when it lands, so the button changes as a result
        of the press that caused it."""
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-download"]["click"]()
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-download", ids(nodes),
                         "the button still offered a download it had just "
                         "started")

    def test_pressing_read_updates_the_button_too(self):
        b = self.open_book()
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-download", ids(nodes))

    def test_a_downloaded_book_offers_removal(self):
        b = self.open_book(state=("complete", "/store/media.epub"))
        nodes, _h = build_scene(b)
        self.assertIn("bk-undownload", ids(nodes))
        self.assertNotIn("bk-download", ids(nodes))

    def test_a_download_in_flight_offers_no_second_one(self):
        b = self.open_book(state=("downloading", None))
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-download", ids(nodes))

    def test_without_the_permission_reading_says_why(self):
        """`/Items/{id}/Download` is the ONLY path to a book's bytes, so a
        user without EnableContentDownloading cannot read one at all. Left
        unsaid it looks like a corrupt file."""
        b = self.open_book(allowed=False)
        _n, handlers = build_scene(b)
        handlers["bk-read"]["click"]()
        self.assertFalse(b.controller.enqueued)
        self.assertIn("not allowed", b.status or "")


class TestProgressDialog(BookPageHarness):

    def _open(self, item=None):
        b = self.open_book(item or book(RunTimeTicks=60000,
                                        path="/l/A Manual.pdf"))
        b.controller.positions["bk1"] = {"PlaybackPositionTicks": 30000}
        _n, handlers = build_scene(b)
        handlers["bk-progress"]["click"]()
        return b

    def test_it_reads_the_position_from_the_server(self):
        """Not off the DTO on screen. The position having moved on another
        device is the entire situation this dialog is for, so showing the
        number the page happened to load would answer the question it was
        opened to ask, wrongly."""
        b = self.open_book(book(RunTimeTicks=60000, path="/l/A Manual.pdf",
                                UserData={"PlaybackPositionTicks": 0}))
        b.controller.positions["bk1"] = {"PlaybackPositionTicks": 40000}
        _n, handlers = build_scene(b)
        handlers["bk-progress"]["click"]()
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("Page 5 of 6", text)

    def test_pushing_a_page_writes_the_index_not_the_page(self):
        # Page 1 is 0 ticks. An off-by-one here puts the shim exactly one
        # page behind every other client, which reads as a rounding quirk.
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("1")
        handlers["bkprog-push"]["click"]()
        self.assertEqual(b.controller.positions_written[-1], ("bk1", 0))

    def test_pushing_a_later_page(self):
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("4")
        handlers["bkprog-push"]["click"]()
        self.assertEqual(b.controller.positions_written[-1], ("bk1", 30000))

    def test_a_page_past_the_end_is_refused_rather_than_sent(self):
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("99")
        handlers["bkprog-push"]["click"]()
        self.assertEqual(b.controller.positions_written, [])
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("between 1 and 6", text)

    def test_nonsense_is_refused_rather_than_sent(self):
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("halfway")
        handlers["bkprog-push"]["click"]()
        self.assertEqual(b.controller.positions_written, [])

    def test_a_push_asks_for_a_repaint(self):
        """The half a rebuilt scene cannot see.

        The dialog is a builder closure over its own state dict, and the
        numbers it shows are read when it BUILDS. Writing new ones changes
        nothing on screen until something asks for a frame — and the harness
        renders on demand, so a scene-based assertion passes either way.
        """
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("2")
        calls = []
        real = b.invalidate
        b.invalidate = lambda *a, **kw: (calls.append(1), real(*a, **kw))
        handlers["bkprog-push"]["click"]()
        self.assertTrue(calls, "pushing a position redrew nothing")

    def test_a_refused_push_asks_for_a_repaint_too(self):
        # This is the case with a message to show, so it is the one where a
        # missing repaint is silent failure rather than a stale number.
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("nope")
        calls = []
        real = b.invalidate
        b.invalidate = lambda *a, **kw: (calls.append(1), real(*a, **kw))
        handlers["bkprog-push"]["click"]()
        self.assertTrue(calls, "a rejected entry said nothing")

    def test_the_dialog_re_reads_after_a_successful_push(self):
        """Rather than assuming its own number landed: a push the server
        clamped would otherwise be shown as accepted."""
        b = self._open()
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("2")
        handlers["bkprog-push"]["click"]()
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("Page 2 of 6", text)

    def test_a_failed_push_says_so(self):
        b = self._open()
        b.controller.set_position_ok = False
        _n, handlers = build_scene(b)
        handlers["bkprog-value"]["change"]("2")
        handlers["bkprog-push"]["click"]()
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("could not be saved", text)

    def test_an_epub_is_offered_no_progress_control(self):
        """The correction: an epub's stored number is an index into epub.js's
        locations array — the text cut into ~1024-character runs, counted
        per spine section — not a percentage of anything a reader displays.
        There is no number to read off your reader and type, and the
        denominator is a property of how the book was sectioned. So the
        control is left out rather than shipping a plausible-looking way to
        record the wrong place."""
        b = self.open_book(book(RunTimeTicks=10_000_000,
                                path="/l/A Novel.epub"))
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-progress", ids(nodes))

    def test_the_epub_figure_is_still_shown(self):
        """Read-only, not hidden: it is the same figure every other client
        shows, and it does say roughly how far in you are. Only *setting*
        it is refused."""
        b = self.open_book(book(RunTimeTicks=10_000_000,
                                path="/l/A Novel.epub",
                                UserData={"PlaybackPositionTicks": 3_700_000}))
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("37% read", text)

    def test_a_paged_book_still_gets_the_control(self):
        # A page IS a number a PDF viewer puts in front of you, which is the
        # whole distinction.
        b = self.open_book(book(RunTimeTicks=60000, path="/l/A Manual.pdf"))
        nodes, _h = build_scene(b)
        self.assertIn("bk-progress", ids(nodes))

    def test_the_epub_dialog_explains_itself_if_it_is_reached(self):
        """The button is gone, but the dialog is a public entry point and a
        Book from another screen could still reach it."""
        b = self.open_book(book(RunTimeTicks=10_000_000,
                                path="/l/A Novel.epub"))
        b._open_book_progress(b.route["_data"]["item"], "srv1")
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("nothing meaningful to set", text)
        self.assertEqual(b.controller.positions_written, [])

    def test_a_format_with_no_progress_gets_an_explanation(self):
        """A mobi resolves as a book and stores no runtime at all, so there
        is no unit to set a position in — and it gets a *different*
        explanation from an epub, because the reasons differ: one has no
        position, the other has one nobody can name."""
        b = self.open_book(book(path="/l/A Kindle Book.azw3"))
        nodes, _h = build_scene(b)
        self.assertNotIn("bk-progress", ids(nodes))
        b._open_book_progress(b.route["_data"]["item"], "srv1")
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("no reading position", text)


if __name__ == "__main__":
    unittest.main()
