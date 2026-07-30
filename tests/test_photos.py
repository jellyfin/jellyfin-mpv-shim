"""Photos, which are videos that happen to be still.

mpv is already a photo viewer: it holds an image for
``--image-display-duration`` (5s by default) and moves on. So the feature is
not a viewer, it is four small decisions about everything *around* playback
-- how the file is fetched, that it starts paused, that the seek controls go
away, and that a folder of them behaves like an album.
"""

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.media import (  # noqa: E402
    _PHOTO_MAX_WIDTH, _SERVER_CONVERTED_IMAGES, Video)
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components.labels import (  # noqa: E402
    placeholder_glyph)
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    FOLDER_TYPES, PHOTO_TYPE, PLAYABLE_TYPES)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene)


class _Jellyfin:
    def __init__(self, item):
        self._item = item
        self.calls = []

    def get_item(self, item_id):
        return dict(self._item, Id=item_id)

    def download_url(self, item_id):
        self.calls.append(("download", item_id))
        return "http://srv/Items/%s/Download?api_key=k" % item_id

    def artwork(self, item_id, art, max_width, ext="jpg", index=None):
        self.calls.append(("artwork", item_id, art, max_width))
        return "http://srv/Items/%s/Images/%s?MaxWidth=%d" % (
            item_id, art, max_width)

    def get_play_info(self, *a, **kw):        # must never be reached
        raise AssertionError("a photo asked PlaybackInfo for a media source")


class _Client:
    def __init__(self, item):
        self.jellyfin = _Jellyfin(item)


class _Parent:
    is_local = True

    def __init__(self, item):
        self.client = _Client(item)


class PhotoUrlTest(unittest.TestCase):
    """A photo does not go through PlaybackInfo at all.

    That endpoint answers about MediaSources and a Photo has none, so there
    is nothing to negotiate, no play-session id and nothing to transcode.
    Confirmed against a live server: web fetches the file itself.
    """

    def _video(self, **item):
        item.setdefault("Type", PHOTO_TYPE)
        parent = _Parent(item)
        v = Video("ph1", parent)
        return v, parent.client.jellyfin

    def test_a_photo_is_marked_as_one(self):
        v, _jf = self._video()
        self.assertTrue(v.is_photo)

    def test_an_ordinary_video_is_not(self):
        v, _jf = self._video(Type="Movie")
        self.assertFalse(v.is_photo)

    def test_a_jpeg_is_downloaded_whole(self):
        v, jf = self._video(Container="jpg", Path="/pics/a.jpg")
        url = v.get_playback_url()
        self.assertIn("/Download", url)
        self.assertEqual(jf.calls[-1][0], "download")

    def test_heic_goes_through_the_server_converter(self):
        """mpv's ffmpeg often cannot decode HEIC, and finding out at decode
        time gives a black window rather than a fallback."""
        v, jf = self._video(Container="heic", Path="/pics/a.HEIC")
        url = v.get_playback_url()
        self.assertIn("/Images/Primary", url)
        self.assertEqual(jf.calls[-1],
                         ("artwork", "ph1", "Primary", _PHOTO_MAX_WIDTH))

    def test_the_extension_is_enough_when_the_container_is_missing(self):
        v, jf = self._video(Path="/pics/IMG_0001.HEIC")
        self.assertIn("/Images/Primary", v.get_playback_url())

    def test_raw_formats_go_the_same_way(self):
        for ext in ("cr2", "nef", "dng"):
            with self.subTest(ext=ext):
                v, _jf = self._video(Container=ext, Path="/p/a." + ext)
                self.assertIn("/Images/Primary", v.get_playback_url())

    def test_heic_is_in_the_converted_set(self):
        self.assertIn("heic", _SERVER_CONVERTED_IMAGES)


class PhotoTypeSetsTest(unittest.TestCase):
    def test_a_photo_album_is_a_folder(self):
        """A Home Videos directory holding both clips and images comes back
        as PhotoAlbum with IsFolder true. Without this it dead-ended."""
        self.assertIn("PhotoAlbum", FOLDER_TYPES)

    def test_photos_are_not_in_playable_types(self):
        """That set also drives the tile context menu, where Download,
        Add to Playlist and Mark Watched are meaningless or broken for a
        still image. Photos are routed on their own instead."""
        self.assertNotIn(PHOTO_TYPE, PLAYABLE_TYPES)


class PhotoGlyphTest(unittest.TestCase):
    """A first initial is useless where the name does not distinguish
    things: a Home Videos library is folders named 2019, 2020, Holiday."""

    def test_a_folder_says_folder(self):
        self.assertEqual(placeholder_glyph({"Type": "Folder",
                                            "Name": "2019"}), "▸")

    def test_a_photo_album_says_album(self):
        self.assertEqual(placeholder_glyph({"Type": "PhotoAlbum",
                                            "Name": "2019"}), "▣")

    def test_an_ordinary_item_still_gets_its_initial(self):
        self.assertEqual(placeholder_glyph({"Type": "Movie",
                                            "Name": "Arrival"}), "A")

    def test_music_keeps_its_note(self):
        self.assertEqual(placeholder_glyph({"Type": "MusicAlbum",
                                            "Name": "Kid A"}), "♪")


class PhotoOpensTheAlbumTest(unittest.TestCase):
    """Clicking a photo plays it with the rest of the album queued, so
    next/prev walk the folder and unpausing is a slideshow."""

    def _browser(self, items):
        src = FakeSource()
        src.grid_items = items
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "al1",
                    "title": "Album"})
        build_scene(b)
        return b

    ALBUM = [
        {"Id": "p1", "Name": "One", "Type": PHOTO_TYPE},
        {"Id": "v1", "Name": "Clip", "Type": "Video"},
        {"Id": "p2", "Name": "Two", "Type": PHOTO_TYPE},
        {"Id": "p3", "Name": "Three", "Type": PHOTO_TYPE},
    ]

    def test_opening_a_photo_queues_the_album_from_that_photo(self):
        b = self._browser(self.ALBUM)
        b._open_item(self.ALBUM[2])
        ids, _srv, start = b.controller.played[-1]
        self.assertEqual(ids, ["p1", "p2", "p3"])
        self.assertEqual(start, 1, "the queue did not start at the click")

    def test_videos_in_the_album_are_not_queued_with_the_photos(self):
        """They play as videos when clicked; a slideshow that stopped on a
        clip and waited for it would not be one."""
        b = self._browser(self.ALBUM)
        b._open_item(self.ALBUM[0])
        ids, _srv, _start = b.controller.played[-1]
        self.assertNotIn("v1", ids)

    def test_a_photo_with_no_list_behind_it_still_opens(self):
        b = self._browser([])
        b._open_item({"Id": "solo", "Name": "S", "Type": PHOTO_TYPE})
        ids, _srv, start = b.controller.played[-1]
        self.assertEqual((ids, start), (["solo"], 0))

    def test_a_photo_does_not_open_a_detail_page(self):
        b = self._browser(self.ALBUM)
        b._open_item(self.ALBUM[0])
        self.assertNotEqual(b.route.get("kind"), "detail")

    def test_a_photo_album_drills_into_a_grid(self):
        b = self._browser(self.ALBUM)
        b._open_item({"Id": "al2", "Name": "2020", "Type": "PhotoAlbum"})
        self.assertEqual(b.route.get("kind"), "grid")
        self.assertEqual(b.route.get("parent_id"), "al2")


# NOT covered here: that _play_media pauses once a photo has loaded.
# Reaching it needs a load to complete, which the fake mpv does not drive on
# its own -- and a test that scrapes player.py for the word "is_photo"
# asserts nothing about behaviour while breaking on any refactor. It is four
# lines, immediately after the success point, and it is manually verified.


if __name__ == "__main__":
    unittest.main()
