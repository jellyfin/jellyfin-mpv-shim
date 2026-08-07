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

    def test_an_audiobook_plays_rather_than_opening_a_page(self):
        # It is an ordinary Audio item; a detail page for one would be a
        # heading and no reason to be there.
        b = self.browser([])
        was = b.route["kind"]
        b._open_item(audiobook(1))
        self.assertEqual(b.route["kind"], was, "it navigated somewhere")
        self.assertTrue(b.controller.played)

    def test_headless_refuses_both_book_routes(self):
        # The whitelist is what makes this true by default, and this is the
        # test that says so for the two kinds added here.
        from jellyfin_mpv_shim.mpvtk_browser.navigator import HEADLESS_ROUTES
        self.assertNotIn("book", HEADLESS_ROUTES)
        self.assertNotIn("books", HEADLESS_ROUTES)


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

    def test_the_folders_own_item_heads_the_page(self):
        b = self.open_folder(
            [audiobook(1)],
            folder={"Id": "folder1", "Name": "The Divided Account",
                    "Type": "Folder", "AlbumArtist": "Gus Gupta"})
        nodes, _h = build_scene(b)
        text = " ".join(str(n.get("text", "")) for n in nodes)
        self.assertIn("The Divided Account", text)
        self.assertIn("Gus Gupta", text)


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
