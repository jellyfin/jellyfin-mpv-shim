"""Browsing downloaded books with the server away.

The catalog held the files and the readers could open them, and there was
no way to *reach* one: ``OfflineLibrarySource`` synthesized libraries for
Movies, Videos, Shows and Playlists and nothing at all for books, so a
downloaded book was invisible the moment the server was.

**A books library browses by folder and the catalog holds no folders.**
``sync.manager._expand`` lists one and downloads its children, so what is
on disk is leaves — and the two halves of a books library do not agree
about which field would put them back together. Measured against a real
server (stdjflib), with the ``Fields`` the downloader actually asks for:

    Book       SeriesName="Long Form"   Album=None     ParentId absent
    AudioBook  SeriesName=None          Album="..."    ParentId absent

So the shelf is rebuilt from what is there, and the tests below are about
the three cases that produces rather than about the rebuilding.
"""

import json
import os
import sys
import tempfile
import unittest

sys.argv = [sys.argv[0]]      # importing the browser reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    BOOKS_COLLECTION, OfflineLibrarySource)
from jellyfin_mpv_shim.sync.db import (COLUMNS, STATUS_COMPLETE,  # noqa: E402
                                       SyncDB)


def row(item_id, dto, **overrides):
    record = {c: None for c in COLUMNS}
    record["item_id"] = item_id
    record["status"] = STATUS_COMPLETE
    record["type"] = dto["Type"]
    record["name"] = dto["Name"]
    record["file_path"] = "%s/%s" % (item_id, dto.get("_file", "file.bin"))
    record["item_json"] = json.dumps(dto)
    record.update(overrides)
    return record


def book(item_id, name, **extra):
    return {"Id": item_id, "Name": name, "Type": "Book",
            "Path": "/library/%s.epub" % name, "UserData": {}, **extra}


def chapter(item_id, name, album, index=None, **extra):
    dto = {"Id": item_id, "Name": name, "Type": "AudioBook",
           "Album": album, "AlbumArtist": "A Reader",
           "Path": "/library/%s.m4b" % name, "UserData": {}, **extra}
    if index is not None:
        dto["IndexNumber"] = index
    return dto


class BooksShelfTest(unittest.TestCase):
    def source(self, dtos):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = os.path.join(tmp.name, "catalog.db")
        db = SyncDB(catalog)
        for dto in dtos:
            db.upsert(row(dto["Id"], dto))
        db.close()
        return OfflineLibrarySource(catalog)

    def shelf(self, dtos):
        src = self.source(dtos)
        items, total = src.get_library_items("offline", "offline:books")
        self.assertEqual(total, len(items))
        return src, items

    # -- the library itself ------------------------------------------------

    def test_downloaded_books_get_a_library(self):
        src = self.source([book("b1", "A Novel")])
        ids = [lib["Id"] for lib in src.get_libraries("offline")]
        self.assertIn("offline:books", ids)

    def test_the_library_says_it_is_a_books_library(self):
        """Not decoration: the shell routes on CollectionType, and books is
        the one collection type it INHERITS down the tree — which is what
        carries a synthesized container to the page that draws an album."""
        src = self.source([book("b1", "A Novel")])
        lib = next(l for l in src.get_libraries("offline")
                   if l["Id"] == "offline:books")
        self.assertEqual(lib.get("CollectionType"), BOOKS_COLLECTION)

    def test_no_books_no_library(self):
        movie = {"Id": "m1", "Name": "A Film", "Type": "Movie"}
        src = self.source([movie])
        ids = [lib["Id"] for lib in src.get_libraries("offline")]
        self.assertNotIn("offline:books", ids)

    def test_the_home_screen_offers_a_row_of_them(self):
        src = self.source([book("b1", "A Novel")])
        rows = src.get_home_rows("offline")
        titles = {r["title"]: r for r in rows}
        self.assertIn("Downloaded Books", titles)
        self.assertEqual(titles["Downloaded Books"]["collection_type"],
                         BOOKS_COLLECTION)

    # -- what is on the shelf ---------------------------------------------

    def test_a_book_stands_on_its_own(self):
        """One file, one thing to read — exactly as in a folder online."""
        _src, items = self.shelf([book("b1", "A Novel")])
        self.assertEqual([(i["Id"], i["Type"]) for i in items],
                         [("b1", "Book")])

    def test_chapters_of_one_recording_become_one_tile(self):
        """The case the container exists for: without one a twelve-part
        recording is twelve tiles, and starting it means picking a chapter.
        """
        _src, items = self.shelf([
            chapter("c1", "Part One", "The Crossing", index=1),
            chapter("c2", "Part Two", "The Crossing", index=2),
            chapter("c3", "Part Three", "The Crossing", index=3),
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["Name"], "The Crossing")
        self.assertEqual(items[0]["Type"], "Folder")

    def test_a_lone_audiobook_is_not_wrapped(self):
        """A container around one chapter is a click that leads to the same
        thing — the reason the online screens give a loose audiobook a page
        of its own."""
        _src, items = self.shelf([chapter("c1", "The Vigil", "The Vigil")])
        self.assertEqual([(i["Id"], i["Type"]) for i in items],
                         [("c1", "AudioBook")])

    def test_an_untagged_recording_is_left_loose(self):
        """Nothing joins those files, and inventing a container would put
        two unrelated rips in one book."""
        _src, items = self.shelf([
            chapter("c1", "Track 01", ""),
            chapter("c2", "Track 02", ""),
        ])
        self.assertEqual(sorted(i["Type"] for i in items),
                         ["AudioBook", "AudioBook"])

    def test_two_recordings_do_not_merge(self):
        _src, items = self.shelf([
            chapter("a1", "One", "Book A", index=1),
            chapter("a2", "Two", "Book A", index=2),
            chapter("b1", "One", "Book B", index=1),
            chapter("b2", "Two", "Book B", index=2),
        ])
        self.assertEqual(sorted(i["Name"] for i in items),
                         ["Book A", "Book B"])

    def test_an_album_id_groups_across_a_retag(self):
        """The id is what the server means; the name is all an untagged rip
        has. Two chapters whose album *name* drifted still belong together.
        """
        _src, items = self.shelf([
            chapter("c1", "One", "The Crossing", index=1, AlbumId="alb"),
            chapter("c2", "Two", "the crossing ", index=2, AlbumId="alb"),
        ])
        self.assertEqual(len(items), 1)

    # -- opening one -------------------------------------------------------

    def test_opening_a_container_lists_its_chapters_in_order(self):
        """IndexNumber first: a rip's files are routinely "Track 01" to
        "Track 10", which sort wrong as text."""
        src, items = self.shelf([
            chapter("c10", "Track 10", "The Crossing", index=10),
            chapter("c2", "Track 02", "The Crossing", index=2),
            chapter("c1", "Track 01", "The Crossing", index=1),
        ])
        chapters, total = src.get_library_items("offline", items[0]["Id"])
        self.assertEqual([c["Id"] for c in chapters], ["c1", "c2", "c10"])
        self.assertEqual(total, 3)

    def test_a_container_has_a_dto_of_its_own(self):
        """BooksPage asks for the folder's own DTO to draw the album
        header; without one the album renders with no title and no actions.
        """
        src, items = self.shelf([
            chapter("c1", "One", "The Crossing", index=1),
            chapter("c2", "Two", "The Crossing", index=2),
        ])
        dto = src.get_item("offline", items[0]["Id"])
        self.assertIsNotNone(dto)
        self.assertEqual(dto["Name"], "The Crossing")

    def test_a_container_is_square_not_a_poster(self):
        """A container with no opinion is drawn as a poster, and an
        audiobook cover is square — the same question the synthesized
        Series answers the other way."""
        _src, items = self.shelf([
            chapter("c1", "One", "The Crossing", index=1),
            chapter("c2", "Two", "The Crossing", index=2),
        ])
        self.assertEqual(items[0]["PrimaryImageAspectRatio"], 1.0)

    def test_a_book_keeps_the_fields_its_reader_needs(self):
        """Path is the only statement of a Book's format, and the readers
        route on it. A DTO that lost it offline opens nothing."""
        src, _items = self.shelf([book("b1", "A Novel")])
        dto = src.get_item("offline", "b1")
        self.assertEqual(dto["Path"], "/library/A Novel.epub")

    def test_a_finished_recording_reads_as_finished_on_its_container(self):
        """A container has no UserData of its own, and a missing one reads
        as never-opened — the same reason the synthesized Series aggregates.
        """
        _src, items = self.shelf([
            chapter("c1", "One", "The Crossing", index=1,
                    UserData={"Played": True}),
            chapter("c2", "Two", "The Crossing", index=2,
                    UserData={"Played": True}),
        ])
        self.assertTrue(items[0]["UserData"]["Played"])
        self.assertEqual(items[0]["UserData"]["UnplayedItemCount"], 0)


if __name__ == "__main__":
    unittest.main()
