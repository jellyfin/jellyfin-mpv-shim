import unittest
from unittest import mock

from jellyfin_mpv_shim import menu as menu_module
from jellyfin_mpv_shim.media import Media
from jellyfin_mpv_shim.menu import OSDMenu


def make_media(queue_ids, seq):
    """Build a Media without running __init__ (which would hit the network)."""
    media = Media.__new__(Media)
    media.queue = [
        {"PlaylistItemId": "orig{0}".format(i), "Id": id_num}
        for i, id_num in enumerate(queue_ids)
    ]
    media.seq = seq
    media.has_next = seq < len(media.queue) - 1
    media.has_prev = seq > 0
    return media


class InsertItemsOrderingTest(unittest.TestCase):
    def test_append_adds_at_end_and_updates_has_next(self):
        media = make_media(["a", "b", "c"], seq=2)  # playing last item
        self.assertFalse(media.has_next)
        media.insert_items(["d"], append=True)
        self.assertEqual([q["Id"] for q in media.queue], ["a", "b", "c", "d"])
        self.assertTrue(media.has_next)

    def test_append_does_not_mutate_in_place(self):
        media = make_media(["a", "b"], seq=0)
        old_queue = media.queue
        media.insert_items(["c"], append=True)
        # Reader threads may hold a reference to the previous list object; it
        # must not be mutated out from under them.
        self.assertEqual([q["Id"] for q in old_queue], ["a", "b"])
        self.assertIsNot(media.queue, old_queue)

    def test_play_next_inserts_after_current(self):
        media = make_media(["a", "b", "c"], seq=1)  # playing "b"
        media.insert_items(["x"], append=False)
        self.assertEqual([q["Id"] for q in media.queue], ["a", "b", "x", "c"])
        self.assertTrue(media.has_next)
        self.assertEqual(media.seq, 1)

    def test_has_next_consistent_with_queue(self):
        # After any insert, has_next must agree with the published queue so a
        # reader that observes has_next=True can safely index get_next().
        media = make_media(["a"], seq=0)
        media.insert_items(["b"], append=True)
        self.assertEqual(media.has_next, media.seq < len(media.queue) - 1)
        self.assertTrue(media.has_next)


class FakeVideo:
    def __init__(self, aid, sid, streams):
        self.aid = aid
        self.sid = sid
        self.media_source = {"MediaStreams": streams}


class FakePlayer:
    def __init__(self, video):
        self._video = video

    def get_video(self):
        return self._video


def make_menu(video):
    menu = OSDMenu.__new__(OSDMenu)
    menu.playerManager = FakePlayer(video)
    menu.menu_list = []
    menu.menu_selection = 0
    menu.menu_title = ""

    def fake_put_menu(title, entries=None, selected=0):
        menu.menu_title = title
        menu.menu_list = entries if entries is not None else []
        menu.menu_selection = selected

    menu.put_menu = fake_put_menu
    return menu


class MenuLangFilterIndexTest(unittest.TestCase):
    def test_audio_selection_index_accounts_for_filtered_entries(self):
        streams = [
            {"Type": "Audio", "Index": 10, "Language": "jpn",
             "DisplayTitle": "Jpn", "Title": "t"},
            {"Type": "Audio", "Index": 11, "Language": "fre",
             "DisplayTitle": "Fre", "Title": "t"},  # filtered out
            {"Type": "Audio", "Index": 12, "Language": "eng",
             "DisplayTitle": "Eng", "Title": "t"},  # selected
        ]
        video = FakeVideo(aid=12, sid=-1, streams=streams)
        menu = make_menu(video)
        with mock.patch.object(menu_module.settings, "lang_filter_audio", True), \
                mock.patch.object(menu_module, "lang_filter", {"jpn", "eng"}):
            menu.change_audio_menu()
        # "fre" (Index 11) is dropped, so the selected track is at menu row 1,
        # not enumerate index 2 over the unfiltered stream list.
        self.assertEqual([row[2] for row in menu.menu_list], [10, 12])
        self.assertEqual(menu.menu_selection, 1)
        self.assertEqual(menu.menu_list[menu.menu_selection][2], 12)

    def test_subtitle_selection_index_accounts_for_none_and_filter(self):
        streams = [
            {"Type": "Subtitle", "Index": 20, "Language": "fre",
             "Title": "t", "Codec": "srt"},  # filtered out
            {"Type": "Subtitle", "Index": 21, "Language": "eng",
             "Title": "t", "Codec": "srt"},  # selected
        ]
        video = FakeVideo(aid=-1, sid=21, streams=streams)
        menu = make_menu(video)
        with mock.patch.object(menu_module.settings, "lang_filter_sub", True), \
                mock.patch.object(menu_module, "lang_filter", {"eng"}):
            menu.change_subtitle_menu()
        # menu_list[0] is the prepended "None" (-1); "fre" is filtered out, so
        # the selected sub lands at row 1.
        self.assertEqual([row[2] for row in menu.menu_list], [-1, 21])
        self.assertEqual(menu.menu_selection, 1)
        self.assertEqual(menu.menu_list[menu.menu_selection][2], 21)


if __name__ == "__main__":
    unittest.main()
