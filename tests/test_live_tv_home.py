"""The Live TV "On Now" home row, and — mostly — its gate.

Live TV is a fringe feature: the shim's users overwhelmingly have no tuner.
But `livetv` sits in jellyfin-web's stock home layout, so it is present in the
resolved layout of nearly every user whether or not their server can serve it.
Most of what follows therefore pins the gate rather than the feature: a server
without Live TV must issue no Live TV request, ever, on any home load.

The gate is the presence of a `livetv` view in /Views, which get_libraries
already fetches. The server adds that view only when the user may use Live TV
AND a tuner host is configured, so it answers both halves at no extra cost.
"""

import sys
import threading
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import home_sections as hs  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    LIVE_TYPES,
    LibrarySource,
)

LIVE_VIEW = {"Id": "lt", "Name": "Live TV", "CollectionType": "livetv"}
MOVIES_VIEW = {"Id": "l1", "Name": "Movies", "CollectionType": "movies"}


class FakeApi:
    """Records every call. _get is what the On Now row uses."""

    def __init__(self, views=(), programs=None):
        self.calls = []
        self.params = []
        self._lock = threading.Lock()
        self._views = list(views)
        self._programs = (programs if programs is not None
                          else [{"Id": "p1", "Name": "The News",
                                 "Type": "Program", "ChannelId": "c1"}])

    def _enter(self, name, params=None):
        with self._lock:
            self.calls.append(name)
            self.params.append(params or {})

    def get_views(self):
        self._enter("get_views")
        return {"Items": self._views}

    def user_items(self, handler="", params=None):
        self._enter("user_items%s" % (handler or ""), params)
        if handler == "/Latest":
            return []
        return {"Items": []}

    def get_next(self, limit=1, fields=None, enable_image_types=None):
        self._enter("get_next")
        return {"Items": []}

    def _get(self, handler, params=None):
        self._enter(handler, params)
        return {"Items": list(self._programs)}

    @property
    def live_calls(self):
        return [c for c in self.calls if "LiveTv" in c]


def source_for(api):
    src = LibrarySource.__new__(LibrarySource)
    src._conn = lambda _uuid: type("C", (), {"api": api})()
    src._has_live_tv = {}
    return src


class GateTest(unittest.TestCase):
    """The 98%-of-users path: no tuner, no cost."""

    def test_no_live_tv_view_means_no_live_tv_request(self):
        api = FakeApi(views=[MOVIES_VIEW])
        src = source_for(api)
        libs = src.get_libraries("srv")
        # livetv is in the stock layout, so this is the realistic case.
        src.get_home_rows("srv", libraries=libs,
                          layout=list(hs.DEFAULT_LAYOUT))
        self.assertEqual(api.live_calls, [],
                         "a server with no tuner was asked for Live TV")

    def test_the_gate_costs_no_extra_request(self):
        # It is derived from /Views, which the home screen fetches anyway.
        api = FakeApi(views=[MOVIES_VIEW, LIVE_VIEW])
        src = source_for(api)
        src.get_libraries("srv")
        self.assertEqual(api.calls, ["get_views"])
        self.assertTrue(src.has_live_tv("srv"))

    def test_live_tv_view_is_still_hidden_from_the_library_list(self):
        # Noting it must not resurrect it as a library tile.
        api = FakeApi(views=[MOVIES_VIEW, LIVE_VIEW])
        src = source_for(api)
        libs = src.get_libraries("srv")
        self.assertEqual([lib["Id"] for lib in libs], ["l1"])

    def test_unknown_until_views_are_read(self):
        # Wrong in the cheap direction: a missing row until the next load,
        # rather than a doomed request on every load.
        self.assertFalse(source_for(FakeApi()).has_live_tv("srv"))

    def test_survives_a_source_built_without_init(self):
        src = LibrarySource.__new__(LibrarySource)
        self.assertFalse(src.has_live_tv("srv"))

    def test_gate_reflects_the_latest_views_read(self):
        api = FakeApi(views=[MOVIES_VIEW, LIVE_VIEW])
        src = source_for(api)
        src.get_libraries("srv")
        self.assertTrue(src.has_live_tv("srv"))
        # Tuner removed server-side; the next views read must clear it.
        api._views = [MOVIES_VIEW]
        src.get_libraries("srv")
        self.assertFalse(src.has_live_tv("srv"))


class OnNowRowTest(unittest.TestCase):

    def _rows(self, api):
        src = source_for(api)
        libs = src.get_libraries("srv")
        return src.get_home_rows("srv", libraries=libs,
                                 layout=[hs.LIVE_TV])

    def test_row_is_fetched_when_the_server_has_live_tv(self):
        api = FakeApi(views=[LIVE_VIEW])
        rows = self._rows(api)
        self.assertEqual(api.live_calls, ["LiveTv/Programs/Recommended"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["collection_type"], "livetv")
        self.assertEqual(rows[0]["kind"], hs.LIVE_TV)
        self.assertEqual([i["Id"] for i in rows[0]["items"]], ["p1"])

    def test_asks_only_for_airing_programs(self):
        api = FakeApi(views=[LIVE_VIEW])
        self._rows(api)
        params = api.params[api.calls.index("LiveTv/Programs/Recommended")]
        self.assertIs(params["IsAiring"], True)

    def test_asks_for_channel_info(self):
        # ChannelInfo is what carries ChannelName/ChannelPrimaryImageTag, which
        # are the tile's subtitle and (usually) its only art.
        api = FakeApi(views=[LIVE_VIEW])
        self._rows(api)
        params = api.params[api.calls.index("LiveTv/Programs/Recommended")]
        self.assertIn("ChannelInfo", params["Fields"])

    def test_skips_the_total_record_count(self):
        api = FakeApi(views=[LIVE_VIEW])
        self._rows(api)
        params = api.params[api.calls.index("LiveTv/Programs/Recommended")]
        self.assertIs(params["EnableTotalRecordCount"], False)

    def test_an_empty_row_is_dropped(self):
        # Which is why there is no separate limit=1 probe like jellyfin-web's:
        # nothing airing renders nothing, for one request rather than two.
        api = FakeApi(views=[LIVE_VIEW], programs=[])
        self.assertEqual(self._rows(api), [])

    def test_a_failing_row_does_not_take_the_home_screen_with_it(self):
        api = FakeApi(views=[LIVE_VIEW, MOVIES_VIEW])

        def explode(handler, params=None):
            raise RuntimeError("no tuner attached")
        api._get = explode
        src = source_for(api)
        libs = src.get_libraries("srv")
        rows = src.get_home_rows("srv", libraries=libs,
                                 layout=[hs.LIVE_TV, hs.NEXT_UP, hs.LATEST])
        # Latest still produced its rows; only Live TV is missing.
        self.assertNotIn(hs.LIVE_TV, [r["kind"] for r in rows])


class ProgramItemTest(unittest.TestCase):
    """A Program is not like the items every other row carries."""

    def test_live_types_cover_programs_and_channels(self):
        self.assertEqual(LIVE_TYPES, {"Program", "TvChannel"})

    def test_program_art_falls_back_to_the_channel_logo(self):
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "p1", "Type": "Program", "ChannelId": "c1",
                "ChannelPrimaryImageTag": "tag1"}
        self.assertEqual(src.image_spec(item, "Thumb"),
                         ("c1", "Primary", "tag1"))

    def test_program_art_prefers_its_own_thumb(self):
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "p1", "Type": "Program", "ImageTags": {"Thumb": "own"},
                "ChannelId": "c1", "ChannelPrimaryImageTag": "tag1"}
        self.assertEqual(src.image_spec(item, "Thumb"), ("p1", "Thumb", "own"))

    def test_parent_thumb_beats_the_channel_logo(self):
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "p1", "Type": "Program",
                "ParentThumbItemId": "c1", "ParentThumbImageTag": "pt",
                "ChannelId": "c1", "ChannelPrimaryImageTag": "tag1"}
        self.assertEqual(src.image_spec(item, "Thumb"), ("c1", "Thumb", "pt"))

    def test_no_art_at_all_is_still_none(self):
        src = LibrarySource.__new__(LibrarySource)
        self.assertIsNone(
            src.image_spec({"Id": "p1", "Type": "Program"}, "Thumb"))

    def test_channel_fallback_does_not_disturb_ordinary_items(self):
        # An Episode must still resolve through the series, not a channel.
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "e1", "Type": "Episode", "SeriesId": "s1",
                "SeriesPrimaryImageTag": "st"}
        self.assertEqual(src.image_spec(item, "Thumb"),
                         ("s1", "Primary", "st"))


if __name__ == "__main__":
    unittest.main()
