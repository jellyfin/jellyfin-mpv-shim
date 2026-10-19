"""Special Features (#759) and the Trailer button, on every page that loads
an item.

Both are gated on the server's counts rather than on the item's type, which
is jellyfin-web's rule (itemDetails/index.js:862, :508-514), and both rules
live in ``components/detail.py`` so the five pages cannot drift apart. The
page tests here are what pins that each page actually calls them.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import components  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components import (  # noqa: E402
    detail as detail_components)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene, ids)

TICKS_PER_MIN = 600000000


def extra(iid, extra_type, played=False):
    """An extra as `/Items/{id}/SpecialFeatures` sends one."""
    return {"Id": iid, "Name": "Extra %s" % iid, "Type": "Video",
            "ExtraType": extra_type, "RunTimeTicks": 5 * TICKS_PER_MIN,
            "UserData": {"Played": played, "PlaybackPositionTicks": 0}}


EXTRAS = [extra("x1", "BehindTheScenes"), extra("x2", "DeletedScene"),
          extra("x3", "Unknown")]


def browser(route, items=None, extras=None, trailers=None):
    src = FakeSource()
    src.items.update(items or {})
    src.special_features = extras or {}
    src.trailers = trailers or {}
    b = MpvtkBrowser(app=None, source=src, controller=FakeController())
    b._pool = _SyncPool()
    b.server = "srv1"
    b.nav_stack = [dict(route, server="srv1")]
    b._load_route(b.route)
    return b


def with_counts(item_id, kind="Movie", extras=0, trailers=0, remote=None):
    item = FakeSource().get_item("srv1", item_id)
    item["Type"] = kind
    if extras:
        item["SpecialFeatureCount"] = extras
    if trailers:
        item["LocalTrailerCount"] = trailers
    if remote is not None:
        item["RemoteTrailers"] = remote
    return {item_id: item}


def order(nodes):
    return [n.get("id") for n in nodes if n.get("id")]


DETAIL = {"kind": "detail", "item_id": "m1"}
TALL = (1280, 3000)     # every row on one screen, so none is virtualized away


class TheRowTest(unittest.TestCase):
    def test_a_counted_item_draws_its_extras_in_the_servers_order(self):
        b = browser(DETAIL, with_counts("m1", extras=3), {"m1": EXTRAS})
        tiles = [i for i in order(build_scene(b, TALL)[0])
                 if i.startswith("detail-extras-")]
        self.assertEqual(tiles, ["detail-extras-x1", "detail-extras-x2",
                                 "detail-extras-x3"])

    def test_no_count_means_no_row_and_no_request(self):
        """The control, and the half of the gate a scene cannot show: the
        server says it has none, so nothing is asked."""
        b = browser(DETAIL, extras={"m1": EXTRAS})
        self.assertNotIn("detail-extras", ids(build_scene(b, TALL)[0]))
        self.assertEqual(getattr(b.source, "extras_calls", []), [])

    def test_a_count_with_an_empty_list_draws_no_row(self):
        """12.x counts extras the parental filter then hides
        (DtoService.cs:1501 vs UserLibraryController.cs:490)."""
        b = browser(DETAIL, with_counts("m1", extras=2), {"m1": []})
        self.assertEqual(b.source.extras_calls, ["m1"])
        self.assertNotIn("detail-extras", ids(build_scene(b, TALL)[0]))

    def test_a_failed_fetch_costs_the_row_and_not_the_page(self):
        b = browser(DETAIL, with_counts("m1", extras=2))

        def boom(*_a):
            raise RuntimeError("500")
        b.source.get_special_features = boom
        b._load_route(b.route)
        nodes = build_scene(b, TALL)[0]
        self.assertIn("btn-play", ids(nodes))
        self.assertNotIn("detail-extras", ids(nodes))

    def test_it_sits_after_cast_and_before_more_like_this(self):
        b = browser(DETAIL, with_counts("m1", extras=3), {"m1": EXTRAS})
        seq = order(build_scene(b, TALL)[0])
        self.assertLess(seq.index("detail-people"),
                        seq.index("detail-extras"))
        self.assertLess(seq.index("detail-extras"),
                        seq.index("detail-similar"))

    def test_a_tile_click_opens_the_extras_own_page(self):
        """The owner's ruling: like every other tile. Web plays instead."""
        b = browser(DETAIL, with_counts("m1", extras=3), {"m1": EXTRAS})
        build_scene(b, TALL)[1]["detail-extras-x2"]["click"]()
        self.assertEqual((b.route["kind"], b.route["item_id"]),
                         ("detail", "x2"))

    def test_a_userdata_refresh_re_reads_the_row_without_it_walking(self):
        """A watched extra's check and progress come from re-reading, so
        each refresh asks again -- and the row stays the same row."""
        b = browser(DETAIL, with_counts("m1", extras=3), {"m1": EXTRAS})
        b._browsing = True
        for n in range(3):
            b.source.special_features["m1"] = [
                dict(e, UserData={"Played": n >= 1}) for e in EXTRAS]
            self.assertTrue(b._refresh_current({"detail"}))
            self.assertEqual(len(b.source.extras_calls), n + 2)
            data = b.route["_data"]["extras"]
            self.assertEqual([e["Id"] for e in data], ["x1", "x2", "x3"])
            self.assertEqual(data[0]["UserData"]["Played"], n >= 1)
        tiles = [i for i in order(build_scene(b, TALL)[0])
                 if i.startswith("detail-extras-")]
        self.assertEqual(len(tiles), 3)


class TheCaptionTest(unittest.TestCase):
    def test_an_extra_is_captioned_with_its_kind(self):
        self.assertEqual(components.tile_lines(extra("x", "Featurette")),
                         ("Extra x", "Featurette"))

    def test_unknown_says_nothing_rather_than_unknown(self):
        self.assertEqual(components.tile_lines(extra("x", "Unknown"))[1], "")

    def test_its_borrowed_year_is_not_the_caption(self):
        """The server copies the owner's metadata onto an extra; a year is
        what an ordinary Video's caption would be."""
        item = dict(extra("x", "Interview"), ProductionYear=2020)
        self.assertEqual(components.tile_lines(item)[1], "Interview")

    def test_a_plain_video_keeps_its_year(self):
        item = {"Id": "v", "Name": "Holiday", "Type": "Video",
                "ProductionYear": 2020}
        self.assertEqual(components.tile_lines(item)[1], "2020")


class OtherPagesTest(unittest.TestCase):
    def test_series(self):
        b = browser({"kind": "series", "item_id": "sh1", "title": "Show"},
                    with_counts("sh1", "Series", extras=1),
                    {"sh1": EXTRAS[:1]})
        self.assertIn("series-extras-x1", ids(build_scene(b, TALL)[0]))

    def test_season(self):
        """The season page loads its own item for this: its sibling list
        carries no counts."""
        b = browser({"kind": "season", "item_id": "se1", "series_id": "sh1",
                     "title": "Season 1"},
                    with_counts("se1", "Season", extras=1),
                    {"se1": EXTRAS[:1]})
        self.assertIn("season-extras-x1", ids(build_scene(b, TALL)[0]))

    def test_album(self):
        b = browser({"kind": "album", "item_id": "al1", "title": "Album"},
                    with_counts("al1", "MusicAlbum", extras=1),
                    {"al1": EXTRAS[:1]})
        self.assertIn("album-extras-x1", ids(build_scene(b, TALL)[0]))

    def test_artist(self):
        b = browser({"kind": "artist", "item_id": "ar1", "title": "Artist"},
                    with_counts("ar1", "MusicArtist", extras=1),
                    {"ar1": EXTRAS[:1]})
        self.assertIn("artist-extras-x1", ids(build_scene(b, TALL)[0]))

    def test_none_of_them_ask_without_a_count(self):
        for route in ({"kind": "series", "item_id": "sh1"},
                      {"kind": "season", "item_id": "se1",
                       "series_id": "sh1"},
                      {"kind": "album", "item_id": "al1"},
                      {"kind": "artist", "item_id": "ar1"}):
            b = browser(route, extras={route["item_id"]: EXTRAS})
            build_scene(b, TALL)
            self.assertEqual(getattr(b.source, "extras_calls", []), [],
                             route["kind"])


class DownloadIsNotOfferedTest(unittest.TestCase):
    def test_not_on_an_extras_own_page(self):
        b = browser({"kind": "detail", "item_id": "x1"},
                    {"x1": dict(FakeSource().get_item("srv1", "x1"),
                                Type="Video", ExtraType="Featurette")})
        self.assertNotIn("act-download", ids(build_scene(b)[0]))

    def test_the_control_a_plain_video_still_offers_it(self):
        b = browser({"kind": "detail", "item_id": "v1"},
                    {"v1": dict(FakeSource().get_item("srv1", "v1"),
                                Type="Video")})
        self.assertIn("act-download", ids(build_scene(b)[0]))

    def test_not_in_an_extras_tile_menu(self):
        b = browser(DETAIL)
        keys = [k for _l, _i, k in b._tile_menu_entries(extra("x", "Clip"))]
        self.assertNotIn("download", keys)
        # Kept, as on web: the playlist branch of the sync door drops it.
        self.assertIn("addto", keys)
        plain = {"Id": "v", "Type": "Video"}
        self.assertIn("download", [k for _l, _i, k in
                                   b._tile_menu_entries(plain)])

    def test_a_door_the_menus_missed_says_why_not_nothing_left(self):
        """`_expand` refuses an extra with its own verdict, which reaches
        the dialog as a failed estimate. "Nothing left to download" would
        say it is already on disk."""
        from jellyfin_mpv_shim.sync.manager import NotDownloadable

        b = browser(DETAIL)

        def refuse(server, item_id, item_type):
            raise NotDownloadable(item_id)
        b.controller.download_estimate = refuse
        b._open_download(extra("x1", "Featurette"))
        texts = [n.get("text") for n in build_scene(b)[0]
                 if n.get("t") == "text"]
        self.assertIn("Special features cannot be downloaded.", texts)
        self.assertNotIn("Nothing left to download.", texts)


class TrailerTargetTest(unittest.TestCase):
    REMOTE = [{"Url": "https://video.example/t1"},
              {"Url": "https://video.example/t2"}]

    def test_local_wins(self):
        self.assertEqual(detail_components.trailer_target(
            {"RemoteTrailers": self.REMOTE}, [{"Id": "t"}], False),
            ("local", ["t"]))

    def test_remote_only_opens_the_first(self):
        self.assertEqual(detail_components.trailer_target(
            {"RemoteTrailers": self.REMOTE}, [], False),
            ("remote", "https://video.example/t1"))

    def test_neither_is_no_button(self):
        self.assertIsNone(detail_components.trailer_target({}, [], False))

    def test_offline_never_offers_a_remote_one(self):
        self.assertIsNone(detail_components.trailer_target(
            {"RemoteTrailers": self.REMOTE}, [], True))

    def test_an_entry_without_a_url_is_skipped(self):
        self.assertEqual(detail_components.trailer_target(
            {"RemoteTrailers": [{"Name": "x"}, "junk",
                                {"Url": "https://v.example/ok"}]}, [], False),
            ("remote", "https://v.example/ok"))


class TrailerButtonTest(unittest.TestCase):
    """One per page: placed, and wired to what the target says."""

    PAGES = [
        ({"kind": "detail", "item_id": "m1"}, "Movie", "btn-trailer"),
        ({"kind": "series", "item_id": "sh1"}, "Series", "sa-trailer"),
        ({"kind": "season", "item_id": "se1", "series_id": "sh1"},
         "Season", "se-trailer"),
        ({"kind": "album", "item_id": "al1"}, "MusicAlbum", "album-trailer"),
        ({"kind": "artist", "item_id": "ar1"}, "MusicArtist", "art-trailer"),
    ]

    def test_a_local_trailer_plays(self):
        for route, kind, node in self.PAGES:
            iid = route["item_id"]
            b = browser(route, with_counts(iid, kind, trailers=1),
                        trailers={iid: [{"Id": "tr-" + iid}]})
            played = []
            b._actions.play_list = lambda ids_, srv, i, **kw: played.append(
                list(ids_))
            handlers = build_scene(b, TALL)[1]
            self.assertIn(node, handlers, kind)
            handlers[node]["click"]()
            self.assertEqual(played, [["tr-" + iid]], kind)

    def test_a_remote_trailer_opens_in_the_browser(self):
        for route, kind, node in self.PAGES:
            iid = route["item_id"]
            url = "https://video.example/" + iid
            b = browser(route, with_counts(iid, kind, remote=[{"Url": url}]))
            handlers = build_scene(b, TALL)[1]
            self.assertIn(node, handlers, kind)
            handlers[node]["click"]()
            self.assertEqual(b.controller.opened_urls, [url], kind)
            # No local count, so the local trailers were never asked for.
            self.assertEqual(getattr(b.source, "trailer_calls", []), [], kind)

    def test_no_counts_no_button(self):
        for route, kind, node in self.PAGES:
            b = browser(route, with_counts(route["item_id"], kind),
                        trailers={route["item_id"]: [{"Id": "tr"}]})
            self.assertNotIn(node, build_scene(b, TALL)[1], kind)


if __name__ == "__main__":
    unittest.main()
