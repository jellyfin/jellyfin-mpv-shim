"""A tile with no artwork draws jellyfin-web's icon for its type.

The placeholders used to be literal characters -- a musical note, a
couple of geometric marks, otherwise the title's first initial -- which
looked off, the bookshelf especially [iw].

A comment in `labels.py` said they had to be characters "because this is
baked into the tile bitmap by the strip compositor, which draws text, not
icon fonts". **[iw]: "that comment is a lie."** It was, and not merely
stale: `strips._paint_glyph_badge` has rasterized Material paths through
`vector.icon_image` since the home-video type badges landed, several
hundred lines below the comment claiming it could not. A stale comment
describes something that used to be true; this one would have talked the
next person out of a change that was already possible.

The map is jellyfin-web's own (`getItemTypeIcon` / `getLibraryIcon`,
`src/utils/image.ts`), so a library with no artwork looks like the same
library does in every other client.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk import vector                        # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components import labels     # noqa: E402


class GlyphMapTest(unittest.TestCase):
    def test_every_mapped_glyph_is_an_icon_we_ship(self):
        """A name with no path draws nothing at all -- a blank tile, which
        is worse than the letter it replaced."""
        for table in (labels._TYPE_GLYPHS, labels._LIBRARY_GLYPHS):
            for key, name in table.items():
                with self.subTest(key):
                    self.assertIn(name, vector.ICON_PATHS)

    def test_it_matches_jellyfin_webs_map(self):
        # Spot-checked against src/utils/image.ts rather than invented.
        for item_type, want in (("Movie", "movie"), ("Series", "tv"),
                                ("Episode", "tv"), ("MusicAlbum", "album"),
                                ("MusicArtist", "person"),
                                ("Audio", "audiotrack"), ("Book", "book"),
                                ("BoxSet", "video_library"),
                                ("Playlist", "queue"), ("Photo", "photo"),
                                ("PhotoAlbum", "photo_album")):
            with self.subTest(item_type):
                self.assertEqual(
                    labels.placeholder_glyph({"Type": item_type}), want)

    def test_a_library_is_answered_by_collection_type(self):
        """Every library is `CollectionFolder`, so the type map alone drew
        a folder on all of them."""
        for ctype, want in (("movies", "movie"), ("music", "music_note"),
                            ("books", "book"), ("tvshows", "tv"),
                            ("boxsets", "video_library"),
                            ("photos", "photo")):
            with self.subTest(ctype):
                self.assertEqual(
                    labels.placeholder_glyph(
                        {"Type": "CollectionFolder",
                         "CollectionType": ctype}), want)

    def test_an_audiobook_gets_the_audio_icon(self):
        """web names no icon for AudioBook, but one IS an Audio item and
        this is the case the table was written for: an author folder of
        three books called "The ..." drew three tiles all reading "T"."""
        self.assertEqual(
            labels.placeholder_glyph({"Type": "AudioBook",
                                      "Name": "The Hobbit"}), "audiotrack")

    def test_an_unknown_type_still_falls_back(self):
        self.assertEqual(
            labels.placeholder_glyph({"Type": "Nonesuch", "Name": "Zebra"}),
            "Z")
        self.assertEqual(labels.placeholder_glyph({"Type": "Nonesuch"}), "?")

    def test_a_letter_is_never_mistaken_for_an_icon(self):
        # The compositor branches on `glyph in ICON_PATHS`, so a
        # single-character answer must not collide with an icon name.
        for ch in "ABCXYZ?":
            with self.subTest(ch):
                self.assertNotIn(ch, vector.ICON_PATHS)


class CompositorTest(unittest.TestCase):
    def _compose(self, items):
        import tempfile

        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            POSTER_GEOM, StripStore, Tile)

        store = StripStore(cache_dir=tempfile.mkdtemp())
        try:
            tiles = [Tile(key=str(i), title=it.get("Name", ""),
                          glyph=labels.placeholder_glyph(it))
                     for i, it in enumerate(items)]
            return store._compose(tiles, POSTER_GEOM.physical()), tiles
        finally:
            store.shutdown()

    def test_an_artless_strip_composites(self):
        entry, tiles = self._compose(
            [{"Type": "Movie", "Name": "A Film"},
             {"Type": "Book", "Name": "A Book"},
             {"Type": "AudioBook", "Name": "The Hobbit"}])
        self.assertEqual(len(entry["regions"]), 3)
        self.assertEqual([t.glyph for t in tiles],
                         ["movie", "book", "audiotrack"])

    def test_an_icon_glyph_is_actually_drawn_as_an_icon(self):
        """The branch, not just the mapping.

        Both of the tests above pass with the icon path disabled -- the
        map is still right and a strip still composites, it just falls
        back to text. So spy on the rasterizer: an icon name must reach
        `vector.icon_image`, and a letter must not.
        """
        from unittest import mock

        from jellyfin_mpv_shim.mpvtk import vector

        real = vector.icon_image
        for glyph, expect_icon in (("movie", True), ("T", False),
                                   ("?", False)):
            with self.subTest(glyph):
                with mock.patch.object(vector, "icon_image",
                                       side_effect=real) as spy:
                    self._compose([{"Type": "x", "Name": glyph}]
                                  if not expect_icon else
                                  [{"Type": "Movie", "Name": "A Film"}])
                names = [c.args[0] for c in spy.call_args_list]
                if expect_icon:
                    self.assertIn("movie", names)
                else:
                    self.assertNotIn(glyph, names)

    def test_the_glyph_is_part_of_the_cache_key(self):
        """Otherwise a tile that gained artwork -- or a map that changed --
        would be served the old bitmap."""
        import tempfile

        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile

        store = StripStore(cache_dir=tempfile.mkdtemp())
        try:
            a = store._tile_key(Tile(key="1", title="x", glyph="movie"))
            b = store._tile_key(Tile(key="1", title="x", glyph="book"))
            self.assertNotEqual(a, b)
        finally:
            store.shutdown()


if __name__ == "__main__":
    unittest.main()
