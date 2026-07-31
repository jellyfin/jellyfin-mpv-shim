"""What Play All and Shuffle actually queue.

The bug these exist for: Play All on a Home Videos album holding photos and
clips reported "there is nothing here to play" while the clips were on
screen. It asked the server a *different* question from the one the grid
had asked -- ``Recursive`` plus ``IncludeItemTypes=Movie,Episode,Video`` --
and got nothing back.

So the tests here are mostly about one property: **what is drawn is what is
queued.** The type filter is on MediaType rather than Type for the same
reason, since Type is whichever concrete entity the scanner's resolver
chose and MediaType is the question being asked.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    QUEUEABLE_MEDIA,
    LibrarySource,
)

#: A Home Videos album exactly as the user reported it: a folder Jellyfin
#: typed PhotoAlbum, holding pictures and clips side by side.
MIXED_ALBUM = [
    {"Id": "p1", "Name": "IMG_1.jpg", "Type": "Photo", "MediaType": "Photo"},
    {"Id": "v1", "Name": "clip.mp4", "Type": "Video", "MediaType": "Video"},
    {"Id": "p2", "Name": "IMG_2.jpg", "Type": "Photo", "MediaType": "Photo"},
    {"Id": "v2", "Name": "clip2.mp4", "Type": "Video", "MediaType": "Video"},
]


class FakeApi:
    def __init__(self, items=None, recursive_items=None):
        self.items = items if items is not None else []
        self.recursive_items = recursive_items or []
        self.calls = []

    def get_user_items(self, **kw):
        self.calls.append(kw)
        if kw.get("recursive"):
            return {"Items": list(self.recursive_items),
                    "TotalRecordCount": len(self.recursive_items)}
        return {"Items": list(self.items),
                "TotalRecordCount": len(self.items)}

    def get_random_items(self, **kw):
        self.calls.append(dict(kw, _random=True))
        return {"Items": list(self.recursive_items or self.items)}


class Harness(unittest.TestCase):
    def _source(self, api):
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        return src


class PlayAllQueriesTest(Harness):
    def test_a_mixed_album_queues_its_contents(self):
        """The reported bug. Every one of these is on screen, so every one
        of them has to be queueable."""
        api = FakeApi(MIXED_ALBUM)
        ids = self._source(api).get_play_all_ids("srv", "album")
        self.assertEqual(ids, ["p1", "v1", "p2", "v2"])

    def test_it_asks_the_grids_own_question(self):
        """Not a second, differently-filtered one. A Play All that can
        disagree with the grid is a Play All that will."""
        api = FakeApi(MIXED_ALBUM)
        self._source(api).get_play_all_ids(
            "srv", "album", sort_by="DateCreated", sort_order="Descending",
            filters={"unplayed": True})
        (call,) = api.calls
        self.assertEqual(call["parent_id"], "album")
        self.assertEqual(call["sort_by"], "DateCreated")
        self.assertEqual(call["sort_order"], "Descending")
        self.assertEqual(call["filters"], "IsUnplayed")
        self.assertNotIn("recursive", call)
        self.assertNotIn("include_item_types", call)

    def test_the_order_is_the_grids_order(self):
        api = FakeApi(list(reversed(MIXED_ALBUM)))
        self.assertEqual(self._source(api).get_play_all_ids("srv", "a"),
                         ["v2", "p2", "v1", "p1"])

    def test_containers_are_left_out(self):
        api = FakeApi([
            {"Id": "f1", "Type": "Folder", "IsFolder": True},
            {"Id": "v1", "Type": "Video", "MediaType": "Video"},
            {"Id": "b1", "Type": "BoxSet", "IsFolder": True},
        ])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "a"),
                         ["v1"])

    def test_the_filter_is_mediatype_not_type(self):
        """A clip is a `Video` under a Home Videos library and a `Movie`
        under a movies one -- same file, different resolver. MediaType is
        the same either way."""
        api = FakeApi([
            {"Id": "a", "Type": "Movie", "MediaType": "Video"},
            {"Id": "b", "Type": "Video", "MediaType": "Video"},
            {"Id": "c", "Type": "MusicVideo", "MediaType": "Video"},
            {"Id": "d", "Type": "SomethingAPluginInvented",
             "MediaType": "Video"},
        ])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "a"),
                         ["a", "b", "c", "d"])

    def test_an_item_with_no_mediatype_is_not_queued(self):
        api = FakeApi([{"Id": "x", "Type": "Genre"},
                       {"Id": "v", "MediaType": "Video"}])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "a"), ["v"])

    def test_photos_are_queued(self):
        """"Play all" in an album of pictures means the slideshow. Leaving
        them out made the button dead in the folders it was added for."""
        self.assertIn("Photo", QUEUEABLE_MEDIA)
        api = FakeApi([{"Id": "p", "Type": "Photo", "MediaType": "Photo"}])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "a"), ["p"])


class PlayAllFolderFallbackTest(Harness):
    def test_a_level_of_only_folders_recurses(self):
        """The top of a Home Videos library, where a flat listing genuinely
        cannot answer the question."""
        api = FakeApi(
            items=[{"Id": "f1", "Type": "Folder", "IsFolder": True},
                   {"Id": "f2", "Type": "PhotoAlbum", "IsFolder": True}],
            recursive_items=[{"Id": "v1"}, {"Id": "v2"}])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "lib"),
                         ["v1", "v2"])
        self.assertTrue(api.calls[-1]["recursive"])
        self.assertEqual(api.calls[-1]["media_types"], "Audio,Photo,Video")

    def test_one_playable_item_is_enough_to_stay_flat(self):
        """A recursive second query is the expensive path, and a folder that
        has anything of its own is answering for itself."""
        api = FakeApi(
            items=[{"Id": "f1", "Type": "Folder", "IsFolder": True},
                   {"Id": "v1", "MediaType": "Video"}],
            recursive_items=[{"Id": "deep"}])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "lib"),
                         ["v1"])
        self.assertEqual(len(api.calls), 1)

    def test_a_genuinely_empty_folder_does_not_recurse(self):
        """Nothing playable AND no folders: there is nothing under it to
        find, and the caller says so rather than asking twice."""
        api = FakeApi(items=[], recursive_items=[{"Id": "surprise"}])
        self.assertEqual(self._source(api).get_play_all_ids("srv", "lib"), [])
        self.assertEqual(len(api.calls), 1)


class ShuffleTest(Harness):
    def test_shuffle_filters_on_the_same_axis(self):
        """It had the same IncludeItemTypes filter, so it had the same bug;
        the two must not drift apart again."""
        api = FakeApi(recursive_items=[{"Id": "a"}, {"Id": "b"}])
        self.assertEqual(self._source(api).get_shuffle_ids("srv", "lib"),
                         ["a", "b"])
        (call,) = api.calls
        self.assertEqual(call["media_types"], "Audio,Photo,Video")
        self.assertIsNone(call.get("include_item_types"))


class ApiclientSupportTest(unittest.TestCase):
    def test_get_random_items_takes_media_types(self):
        """Added additively to jellyfin-apiclient-python, which has a
        no-breaking-changes policy -- so it defaults to None and an existing
        caller's query is byte-identical."""
        import inspect

        from jellyfin_apiclient_python.api import API

        sig = inspect.signature(API.get_random_items)
        self.assertIn("media_types", sig.parameters)
        self.assertIsNone(sig.parameters["media_types"].default)


if __name__ == "__main__":
    unittest.main()
