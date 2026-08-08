"""Books and audiobooks against a real server.

Books are the part of Jellyfin with the widest gap between what the DTO
*looks* like and what it *is*, and every one of those gaps is invisible to a
fake — because a fake is written from the same reading of the API that the
code is. The specific claims the shim now depends on, each of which would be
a silent wrong answer rather than an error if it changed:

* **A `Book` carries no size and no container, under any `Fields` value.**
  The download pipeline reads its extension out of `Path` for that reason
  alone, and reports its size as unknown rather than as zero. If a future
  server started answering `Fields=Size`, the estimate should stop lying —
  and if `Path` ever stopped being served to a non-administrator, downloads
  would start writing files nothing could open. Both are pinned.
* **`RunTimeTicks` means three different things by format**, and the
  position that pairs with it is an index for two of them and a proportion
  for the third. Pinned by a real round trip through the server, because
  the encoding is a placeholder the server's own comments apologise for and
  is exactly the sort of thing a release changes.
* **`SeriesName` is populated for books and null for audiobooks; `Album` is
  the reverse.** That asymmetry is why the downloads panel groups
  audiobooks by `Album` and why the folder — not any metadata field — is
  the download unit. It has already been measured once and written into a
  memo; this is where it stops being a memo.
* **A single-file audiobook has real chapters and a rip does not.** Same
  user gesture, two code paths, and only the first reuses the chapter
  machinery.

Fixtures come from stdjflib's `Books` library. Every lookup skips rather
than fails when a fixture is absent, so a library built before the book
fixtures existed does not report a defect it cannot have.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from jellyfin_mpv_shim import books  # noqa: E402

LIBRARY = "Books"

#: One fixture per claim, named rather than looked up by shape — a test that
#: takes "the first PDF it finds" passes for the wrong reasons the moment the
#: library grows.
EPUB = "The Standard Reference"          # epub: progress is a percentage
PDF = "The Standard Manual"              # pdf: progress is a page index
COMIC = "A Test Comic 001"               # cbz: pages counted from the archive
KINDLE = "A Kindle Format Book"          # azw3: resolves, but has no runtime
SERIES_BOOK = "Ascent"                   # carries a real SeriesName

#: The two audiobook shapes. `The Divided Account` is a folder of six mp3s
#: joined only by their Album tag; `The Lantern Keeper` is one .m4b whose
#: chapters live inside the file.
RIP_FOLDER = "The Divided Account"
SINGLE_FILE = "The Lantern Keeper"


class _BookCase(unittest.TestCase):
    """A session and a real `LibrarySource`, plus fixture lookup by name."""

    ACCOUNT = "qa-user"

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session(cls.ACCOUNT)
        cls.source = cls.session.library_source()
        cls.source.get_libraries(_e2e.SOURCE_UUID)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    @property
    def uuid(self):
        return _e2e.SOURCE_UUID

    # -- fixtures ----------------------------------------------------------

    def items(self, item_type,
              fields="Path,MediaSources,SeriesName,Album,ParentId"):
        return {i["Name"]: i for i in self.session.find_all(
            library=LIBRARY, item_type=item_type, fields=fields)}

    def book(self, name, fields="Path,MediaSources,SeriesName"):
        found = self.items("Book", fields).get(name)
        if found is None:
            self.skipTest(
                "no book named %r — this library predates stdjflib's book "
                "fixtures; rebuild it with `stdjflib build`" % name)
        return found

    def audiobook(self, name, fields="Path,MediaSources,Album,ParentId"):
        found = self.items("AudioBook", fields).get(name)
        if found is None:
            self.skipTest("no audiobook named %r — rebuild the library" % name)
        return found


@_e2e.require_server
class BookDtoShapeTest(_BookCase):
    """What a `Book` does and does not carry.

    Everything the download pipeline had to work around, asserted against
    the server rather than against the reading of the source that produced
    the workaround.
    """

    def test_a_book_has_no_media_sources(self):
        """`Book` is not `IHasMediaSources`. This is why PlaybackInfo,
        subtitles, trickplay and segments are all skipped for one — each
        would be a request that can only come back empty."""
        item = self.book(EPUB, fields="Path,MediaSources,MediaStreams")
        self.assertFalse(item.get("MediaSources"),
                         "a Book now has media sources — the download "
                         "pipeline could use them instead of Path")
        self.assertFalse(item.get("MediaStreams"))

    def test_a_book_states_no_container(self):
        item = self.book(EPUB, fields="Path,Container")
        self.assertIsNone(item.get("Container"))

    def test_a_book_states_no_size_under_any_fields(self):
        """Which is why the download estimate reports one as unsized rather
        than as 0 B. If this starts failing, the honest thing is to use the
        real number, not to keep saying "unknown"."""
        for fields in ("Size", "MediaSources", "Path,Size,Container"):
            item = {i["Name"]: i for i in self.session.find_all(
                library=LIBRARY, item_type="Book", fields=fields)}.get(EPUB)
            if item is None:
                self.skipTest("no book named %r" % EPUB)
            self.assertIsNone(item.get("Size"),
                              "Fields=%s now yields a size" % fields)

    def test_path_reaches_an_ordinary_user(self):
        """The load-bearing one.

        `Path` is the only statement of a book's format anywhere in the DTO,
        and the account here is deliberately not an administrator. If this
        ever stops being served, every download starts writing a file with
        the wrong extension and nothing on the desktop opens it — a failure
        that looks like a corrupt book.
        """
        item = self.book(EPUB)
        self.assertTrue(item.get("Path"),
                        "Path is no longer served to a non-admin; the "
                        "download pipeline has no other source of format")
        self.assertEqual(books.book_format(item), "epub")

    def test_the_fixture_formats_are_classified_as_expected(self):
        formats = {EPUB: "epub", PDF: "pdf", COMIC: "cbz", KINDLE: "azw3"}
        for name, expected in formats.items():
            self.assertEqual(books.book_format(self.book(name)), expected,
                             "%s is not a .%s any more" % (name, expected))

    def test_a_book_carries_a_series_name(self):
        """The half of the asymmetry that makes `SeriesName` useless for
        audiobooks: only `BookResolver` populates it, and it runs for
        `Book`."""
        self.assertTrue(self.book(SERIES_BOOK).get("SeriesName"))


@_e2e.require_server
class ProgressEncodingTest(_BookCase):
    """`RunTimeTicks` as a progress unit, round-tripped through the server.

    The push and the pull are the shim's only way to record where a reader
    got to — nothing observes an external application — so an encoding that
    drifted would put every book at the wrong place on every other client,
    silently.
    """

    def _reset(self, item_id):
        self.addCleanup(self._set_position, item_id, 0)

    def _set_position(self, item_id, ticks):
        self.session.api.update_userdata_for_item(
            item_id, {"PlaybackPositionTicks": int(ticks)})

    def _userdata(self, item_id):
        return self.session.api.get_userdata_for_item(item_id) or {}

    def test_a_paged_book_reports_its_page_count(self):
        """Comics count archive entries and PDFs are rendered to be counted;
        both store `pages * 10000`. Page counts need the probe that landed
        in Jellyfin 12.0, so an older server has none at all."""
        comic = self.book(COMIC)
        pages = books.page_count(comic)
        if pages is None:
            self.skipTest("this server counts no pages (pre-12.0)")
        self.assertGreater(pages, 0)
        self.assertEqual(comic["RunTimeTicks"], pages * 10000)

    def test_an_epub_stores_exactly_one_second(self):
        item = self.book(EPUB)
        self.assertEqual(item.get("RunTimeTicks"), books.EPUB_FULL_TICKS,
                         "an epub's runtime is the denominator its progress "
                         "is a proportion of")

    def test_a_kindle_format_has_no_runtime_at_all(self):
        """Which is why the progress dialog refuses one rather than offering
        to set a page number that means nothing."""
        item = self.book(KINDLE)
        self.assertIsNone(item.get("RunTimeTicks"))
        self.assertEqual(books.progress_mode(item), books.PROGRESS_NONE)

    def test_a_page_pushed_reads_back_as_the_same_page(self):
        item = self.book(PDF)
        pages = books.page_count(item)
        if not pages or pages < 3:
            self.skipTest("the PDF fixture has no usable page count")
        self._reset(item["Id"])
        for page in (1, 2, pages):
            self._set_position(item["Id"], books.ticks_for_page(page))
            fresh = dict(item, UserData=self._userdata(item["Id"]))
            mode, value, total = books.progress_of(fresh)
            self.assertEqual((mode, value, total),
                             (books.PROGRESS_PAGES, page, pages),
                             "page %d did not survive the round trip" % page)

    def test_page_one_stores_nothing(self):
        """The off-by-one, against the server. The stored value is a
        zero-based index, so being wrong here puts the shim exactly one page
        behind every other client — which reads as a rounding quirk."""
        item = self.book(PDF)
        self._reset(item["Id"])
        self._set_position(item["Id"], books.ticks_for_page(1))
        self.assertEqual(
            self._userdata(item["Id"]).get("PlaybackPositionTicks") or 0, 0)

    def test_an_epub_position_is_read_but_never_written(self):
        """The correction that cost the epub half of this feature.

        The stored number is ``location / total`` over epub.js's locations
        index — the text cut into ~1024-character runs, counted per spine
        section — so it is neither a percentage of the book nor anything a
        reader puts a number in front of you for. It is still *read* (the
        server round trip below is what every client shows), and the shim
        refuses to offer a control that sets it.

        The round trip is still asserted, because the read half is real and
        the encoding is still a placeholder the server may change.
        """
        item = self.book(EPUB)
        self.assertFalse(books.progress_settable(item),
                         "an epub is being offered a settable position "
                         "again — there is no unit the user can name")
        self._reset(item["Id"])
        self._set_position(item["Id"], books.EPUB_FULL_TICKS * 37 // 100)
        fresh = dict(item, UserData=self._userdata(item["Id"]))
        self.assertEqual(books.progress_of(fresh)[1], 37)

    def test_a_paged_book_is_the_only_settable_one(self):
        """A page IS a number a PDF viewer and a comic reader both put in
        front of you, which is the whole of the distinction."""
        self.assertTrue(books.progress_settable(self.book(PDF)))
        self.assertTrue(books.progress_settable(self.book(COMIC)))
        self.assertFalse(books.progress_settable(self.book(KINDLE)))

    def test_a_position_does_not_clear_the_favourite_flag(self):
        """`UpdateItemUserData` merges, and the shim relies on it: a
        progress push names one field and must leave the rest alone."""
        item = self.book(PDF)
        self._reset(item["Id"])
        self.addCleanup(self.session.api.favorite, item["Id"], False)
        self.session.api.favorite(item["Id"], True)
        self._set_position(item["Id"], books.ticks_for_page(2))
        self.assertTrue(self._userdata(item["Id"]).get("IsFavorite"),
                        "pushing a position cleared the favourite heart")

    def test_a_book_with_a_position_appears_in_continue_reading(self):
        """The home row's query is `MediaTypes=Book`, which is the only
        filter that works: the two book entity types are unrelated, and an
        AudioBook would land in Continue Listening instead."""
        item = self.book(EPUB)
        self._reset(item["Id"])
        self._set_position(item["Id"], books.ticks_for_percent(30))
        resume = self.session.api.get_resume_items(media_types="Book",
                                                   limit=20) or {}
        names = {i["Name"] for i in resume.get("Items", [])}
        self.assertIn(item["Name"], names,
                      "a part-read book is not resumable — Continue Reading "
                      "would always be empty")
        self.assertNotIn(
            "AudioBook", {i.get("Type") for i in resume.get("Items", [])},
            "MediaTypes=Book caught an audiobook, which belongs in "
            "Continue Listening")


@_e2e.require_server
class AudiobookShapeTest(_BookCase):
    """The two shapes an audiobook comes in, and what joins a rip."""

    def test_an_audiobook_is_an_ordinary_audio_item(self):
        """Real MediaSources, real duration, the whole ffmpeg pipeline —
        which is why audiobooks need no playback work at all."""
        item = self.audiobook(SINGLE_FILE)
        self.assertEqual(item.get("MediaType"), "Audio")
        sources = item.get("MediaSources") or []
        self.assertTrue(sources, "an audiobook has no media source")
        self.assertTrue(sources[0].get("Container"))
        self.assertGreater(item.get("RunTimeTicks") or 0, 0)

    def test_a_single_file_audiobook_has_chapters_in_the_file(self):
        """`ExtractChapters = item is AudioBook`, added to the audio prober
        in 2026. This is the shape that reuses the chapter machinery."""
        item = {i["Name"]: i for i in self.session.find_all(
            library=LIBRARY, item_type="AudioBook",
            fields="Chapters")}.get(SINGLE_FILE)
        if item is None:
            self.skipTest("no audiobook named %r" % SINGLE_FILE)
        chapters = item.get("Chapters") or []
        self.assertGreater(len(chapters), 1,
                           "the .m4b fixture has no chapters — the chapter "
                           "controls in the audio bar have nothing to drive")
        starts = [c.get("StartPositionTicks") or 0 for c in chapters]
        self.assertEqual(starts, sorted(starts))

    def test_series_name_is_null_on_audiobooks(self):
        """The measured asymmetry, pinned. `AudioBook` implements
        `IHasSeries` and carries the field, but only `BookResolver` ever
        populates it and that runs for `Book` — so grouping a rip by
        SeriesName would group everything under nothing."""
        for name in (SINGLE_FILE,):
            self.assertFalse(self.audiobook(name).get("SeriesName"),
                             "audiobooks now carry a SeriesName; the "
                             "downloads panel could group by it")

    def test_a_rip_is_joined_only_by_its_album(self):
        chapters = [i for i in self.items("AudioBook").values()
                    if (i.get("Album") or "") == RIP_FOLDER]
        if len(chapters) < 2:
            self.skipTest("no multi-file audiobook fixture")
        self.assertFalse(any(c.get("SeriesName") for c in chapters))
        # Same parent folder, which is the fallback when even Album is
        # absent — and the reason the folder is the download unit.
        self.assertEqual(len({c.get("ParentId") for c in chapters}), 1,
                         "a rip's files are not in one folder, so nothing "
                         "at all would join an untagged one")

    def test_a_rip_is_n_separate_items(self):
        """Not one item with chapters. Same gesture, different code path:
        here "chapter 7" means *item* 7."""
        chapters = [i for i in self.items("AudioBook").values()
                    if (i.get("Album") or "") == RIP_FOLDER]
        if not chapters:
            self.skipTest("no multi-file audiobook fixture")
        self.assertGreater(len(chapters), 1)
        indexes = sorted(c.get("IndexNumber") for c in chapters
                         if c.get("IndexNumber") is not None)
        self.assertEqual(indexes, list(range(1, len(chapters) + 1)),
                         "the chapters are not numbered 1..N, so the track "
                         "list would order them by name alone")


@_e2e.require_server
class BookBrowseTest(_BookCase):
    """What the browser's own source returns for a books library."""

    def library(self):
        for lib in self.source.get_libraries(self.uuid):
            if lib.get("CollectionType") == "books":
                return lib
        self.skipTest("no books library on this server")

    def test_a_books_library_is_offered_at_all(self):
        """It was excluded outright until book support existed, which made
        every other test here unreachable through the UI."""
        self.assertTrue(self.library())

    def test_a_books_library_lists_folders_not_a_flattened_grid(self):
        """`LIBRARY_ITEM_TYPES` has no entry for books, so the query goes
        out untyped and non-recursive — which is jellyfin-web's default
        Folders tab, and the only structure these libraries have.

        Send a type and recursion instead and the folder tree collapses:
        every chapter of every rip becomes a loose tile with no way to play
        the book it belongs to.
        """
        lib = self.library()
        items, total = self.source.get_library_items(
            self.uuid, lib["Id"], limit=100, collection_type="books")
        self.assertTrue(items)
        self.assertEqual(total, len(items))
        self.assertIn("Folder", {i.get("Type") for i in items},
                      "the books library listed no folders — the query was "
                      "flattened")

    def test_a_folder_of_chapters_lists_only_audiobooks(self):
        """Which is what the browser's track-list rendering keys on: every
        child an AudioBook, and all of them loaded."""
        chapters = [i for i in self.items("AudioBook").values()
                    if (i.get("Album") or "") == RIP_FOLDER]
        if not chapters:
            self.skipTest("no multi-file audiobook fixture")
        parent = chapters[0]["ParentId"]
        items, total = self.source.get_library_items(
            self.uuid, parent, limit=100, collection_type="books")
        self.assertEqual(total, len(items))
        self.assertEqual({i.get("Type") for i in items}, {"AudioBook"})

    def test_books_are_searchable(self):
        """Both types are in `SEARCH_TYPES` now; web's search returns them
        too, in its own last-but-one section."""
        results = self.source.search(self.uuid, "Standard") or []
        types = {i.get("Type") for i in results}
        self.assertTrue(
            {"Book"} & types,
            "a search that matches a book returned no books; found %s"
            % sorted(t for t in types if t))


@_e2e.require_server
class BookDownloadTest(_BookCase):
    """A real book, fetched over the real endpoint, into a real store.

    This is the only path to a book's bytes and it is the whole feature, so
    it is worth taking end to end: the estimate, the file, its name, and the
    catalog row that lets the Read button find it again.
    """

    def _manager(self):
        import shutil
        import tempfile
        from jellyfin_mpv_shim.sync.db import SyncDB
        from jellyfin_mpv_shim.sync.manager import SyncManager

        root = tempfile.mkdtemp(prefix="jms-e2e-books-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        manager = SyncManager()
        manager.root = root
        manager.db = SyncDB(os.path.join(root, "catalog.db"))
        self.addCleanup(manager.db.close)
        manager.get_client = lambda uuid: self.session.client
        return manager

    def test_an_estimate_admits_it_cannot_size_a_book(self):
        manager = self._manager()
        item = self.book(EPUB)
        est = manager.estimate("e2e", item["Id"], "Book")
        self.assertEqual(est["count"], 1)
        self.assertEqual(est["unsized_count"], 1)
        self.assertEqual(est["total_bytes"], 0)

    def test_a_book_downloads_and_lands_under_its_own_extension(self):
        manager = self._manager()
        item = self.book(EPUB)
        self.assertEqual(manager.enqueue("e2e", item["Id"], "Book",
                                         include_watched=True), 1)
        row = manager.db.get(item["Id"])
        self.assertEqual(row["ext"], "epub")
        manager._download(row)

        row = manager.db.get(item["Id"])
        self.assertEqual(row["status"], "complete",
                         "the book did not download")
        path = os.path.join(manager.root, row["file_path"])
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(".epub"),
                        "the file has no extension the desktop can route on")
        # The size is learned from the response, since the DTO has none.
        self.assertGreater(row["size_bytes"], 0)
        self.assertEqual(os.path.getsize(path), row["size_bytes"])
        # And it really is an epub: a zip, whatever the metadata claimed.
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(2), b"PK")

    def test_the_served_filename_agrees_with_the_path(self):
        """They normally do, which is what makes `Path` usable before the
        download starts. The correction in `_download` is for when they do
        not — and this is what would tell us the two had drifted apart in
        general rather than in one case."""
        import requests

        item = self.book(PDF)
        url = self.session.api.download_url(item["Id"], include_apikey=False)
        header = self.session.client.http._get_authenication_header()
        resp = requests.get(url, stream=True, timeout=(10, 30),
                            headers={"Authorization": header})
        try:
            resp.raise_for_status()
            from jellyfin_mpv_shim.sync.manager import _disposition_ext
            self.assertEqual(_disposition_ext(resp.headers),
                             books.book_format(item))
        finally:
            resp.close()

    def test_a_folder_of_chapters_downloads_as_one_unit(self):
        """The folder is the download unit because nothing else joins a
        rip. Enqueuing one has to produce a row per chapter — and only for
        the chapters, not for anything else that happens to sit alongside
        them."""
        chapters = [i for i in self.items("AudioBook").values()
                    if (i.get("Album") or "") == RIP_FOLDER]
        if not chapters:
            self.skipTest("no multi-file audiobook fixture")
        manager = self._manager()
        parent = chapters[0]["ParentId"]
        added = manager.enqueue("e2e", parent, "Folder", include_watched=True)
        self.assertEqual(added, len(chapters))
        rows = manager.db.list()
        self.assertEqual({r["type"] for r in rows}, {"AudioBook"})
        self.assertEqual({r["item_id"] for r in rows},
                         {c["Id"] for c in chapters})

    def test_a_downloaded_rip_groups_under_its_book(self):
        """End to end into the downloads panel's tree, which is where the
        Album fallback actually has to hold."""
        from jellyfin_mpv_shim.mpvtk_browser.downloads import group_downloads

        chapters = [i for i in self.items("AudioBook").values()
                    if (i.get("Album") or "") == RIP_FOLDER]
        if not chapters:
            self.skipTest("no multi-file audiobook fixture")
        manager = self._manager()
        manager.enqueue("e2e", chapters[0]["ParentId"], "Folder",
                        include_watched=True)
        tree = group_downloads(manager.db.list(), [], lambda pid: [], {})
        self.assertEqual([g["kind"] for g in tree], ["audiobooks"])
        self.assertEqual([b["title"] for b in tree[0]["children"]],
                         [RIP_FOLDER])
        self.assertEqual(tree[0]["count"], len(chapters))


if __name__ == "__main__":
    unittest.main()
