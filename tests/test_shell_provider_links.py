"""The detail screen's metadata-provider links (IMDb, TMDB, TheTVDB, AniDB).

Two properties, and only one of them is about the row being drawn.

The other is the one this kind of row gets wrong: a button built in a loop
over links, whose handler closes over the loop variable, opens whichever
provider the loop ended on -- from *every* button. It is invisible in a
scene assertion, because the labels are right and the layout is right; the
only thing wrong is where pressing goes. So every test here that cares about
that presses a button and asserts the url, rather than reading the tree.

The scheme allowlist has its own tests in ``test_system_open_urls.py``:
these links are composed by the *server*, and a desktop opener hands
``file://`` or a registered application scheme to whatever claims it.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components import (  # noqa: E402
    detail as detail_components)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene, ids)

DETAIL = {"kind": "detail", "item_id": "m1", "server": "srv1"}


class LinkSource(FakeSource):
    """A FakeSource whose detail item carries whatever links a test wants."""

    def __init__(self, links):
        super().__init__()
        self.links = links

    def get_item(self, server_uuid, item_id):
        item = super().get_item(server_uuid, item_id)
        if self.links is None:
            item.pop("ExternalUrls", None)
        else:
            # Copied only when copyable: a test feeds junk here on
            # purpose, and the fixture must deliver it unchanged.
            item["ExternalUrls"] = [
                dict(link) if isinstance(link, dict) else link
                for link in self.links]
        return item


class ManyLinkSeasons(FakeSource):
    """Seasons tagged by every database at once, for the wrapping case."""

    def get_seasons(self, server_uuid, series_id):
        seasons = super().get_seasons(server_uuid, series_id)
        for season in seasons:
            season["ExternalUrls"] = [
                {"Name": "Provider %d" % i, "Url": "https://p%d.example/x" % i}
                for i in range(8)]
        return seasons


class DeadProviderTest(unittest.TestCase):
    """Links we refuse to offer because the site is gone.

    Jellyfin still ships a Zap2It external id and still composes a
    tvlistings.zap2it.com url for anything carrying one, years after that
    listings site was retired [iw]. A button that leaves the app for a page
    that does not exist is worse than no button.
    """

    def _names(self, urls):
        from jellyfin_mpv_shim.mpvtk_browser.components import controls

        item = {"ExternalUrls": [{"Name": "P%d" % i, "Url": u}
                                 for i, u in enumerate(urls)]}
        buttons = detail_components.provider_link_buttons(item, lambda _u: None)
        del controls
        return len(buttons)

    def test_a_zap2it_link_is_not_offered(self):
        self.assertEqual(self._names([
            "https://tvlistings.zap2it.com/overview.html?programSeriesId=EP1"
        ]), 0)

    def test_the_whole_domain_goes_not_just_the_listings_host(self):
        self.assertEqual(self._names(["https://zap2it.com/anything"]), 0)

    def test_it_does_not_take_its_neighbours_with_it(self):
        self.assertEqual(self._names([
            "https://tvlistings.zap2it.com/overview.html?programSeriesId=EP1",
            "https://www.themoviedb.org/tv/1",
            "https://www.imdb.com/title/tt1/"]), 2)

    def test_the_match_is_on_the_host_and_not_the_whole_url(self):
        """A provider whose query string happens to name the dead one must
        survive -- matching the raw string would take it out."""
        self.assertEqual(
            self._names(["https://example.test/x?src=tvlistings.zap2it.com"]),
            1)

    def test_a_host_that_merely_ends_in_the_same_letters_survives(self):
        self.assertEqual(self._names(["https://notzap2it.com/x"]), 1)

    def test_an_unparseable_url_is_dropped_rather_than_drawn(self):
        self.assertEqual(self._names(["https://[oops"]), 0)


class ProviderLinkTest(unittest.TestCase):
    def _browser(self, links=None):
        source = FakeSource() if links == "default" else LinkSource(links)
        b = MpvtkBrowser(app=None, source=source)
        b._pool = _SyncPool()
        b.controller = FakeController()
        b.server = "srv1"
        b.nav_stack = [dict(DETAIL)]
        b._load_route(b.route)
        return b

    def _scene(self, links=None):
        b = self._browser(links)
        nodes, handlers = build_scene(b)
        return b, nodes, handlers

    def _labels(self, nodes):
        """Link button captions, in drawn order.

        Read off the row rather than off the DTO, so a test that says
        "three links" is talking about three buttons.
        """
        out = []
        for index, node in enumerate(nodes):
            if (node.get("id") or "").startswith("detail-link-"):
                # The caption is the text node inside the button.
                for after in nodes[index:index + 4]:
                    if after.get("t") == "text" and after.get("text"):
                        out.append(after["text"])
                        break
        return out

    # -- the row -----------------------------------------------------------

    def test_the_links_are_drawn_in_the_servers_order(self):
        _b, nodes, _h = self._scene([
            {"Name": "IMDb", "Url": "https://imdb.example/1"},
            {"Name": "TheTVDB", "Url": "https://tvdb.example/2"},
            {"Name": "AniDB", "Url": "https://anidb.example/3"}])
        self.assertEqual(self._labels(nodes), ["IMDb", "TheTVDB", "AniDB"])

    def test_an_item_with_no_links_draws_no_provider_buttons(self):
        """The Jellyfin Web link is not a provider and stays -- it is
        composed from the item's own id rather than matched by anybody."""
        _b, nodes, _h = self._scene(None)
        self.assertEqual([i for i in ids(nodes)
                          if (i or "").startswith("detail-link-")], [])
        self.assertIn("detail-web-link", ids(nodes))

    def test_an_empty_list_draws_no_provider_buttons(self):
        """Not the same input as a missing key, and the server sends it --
        an item nothing has matched answers ``ExternalUrls: []``."""
        _b, nodes, _h = self._scene([])
        self.assertEqual([i for i in ids(nodes)
                          if (i or "").startswith("detail-link-")], [])
        self.assertIn("detail-web-link", ids(nodes))

    # -- pressing them -----------------------------------------------------

    def test_each_button_opens_its_own_provider(self):
        """The late-binding trap, and the reason this file exists.

        Every button is asserted, not just one: the bug draws a correct row
        and sends all of them to the last url, so checking the first button
        alone passes against it.
        """
        b, _nodes, handlers = self._scene([
            {"Name": "IMDb", "Url": "https://imdb.example/1"},
            {"Name": "TheTVDB", "Url": "https://tvdb.example/2"},
            {"Name": "AniDB", "Url": "https://anidb.example/3"}])
        for i in range(3):
            handlers["detail-link-%d" % i]["click"]()
        self.assertEqual(b.controller.opened_urls,
                         ["https://imdb.example/1",
                          "https://tvdb.example/2",
                          "https://anidb.example/3"])

    def test_a_refused_link_says_so_rather_than_doing_nothing(self):
        """A press with no visible effect is indistinguishable from a dead
        UI, and that is the outcome on a box with no browser -- or for a
        scheme ``system_open`` refuses."""
        b, _nodes, handlers = self._scene(
            [{"Name": "IMDb", "Url": "https://imdb.example/1"}])
        b.controller.open_url_result = (False, None)
        b.status = ""
        handlers["detail-link-0"]["click"]()
        # b.status, not a patched set_status: PageContext binds the method at
        # construction, so a stub installed after the page exists is never
        # the one called -- which is a test that passes against silence.
        self.assertTrue(b.status,
                        "a refused link reported nothing to the user")

    def test_a_gateway_that_raises_does_not_kill_the_render_loop(self):
        b, _nodes, handlers = self._scene(
            [{"Name": "IMDb", "Url": "https://imdb.example/1"}])

        def boom(_url):
            raise RuntimeError("no desktop here")

        b.controller.open_url = boom
        b.status = ""
        handlers["detail-link-0"]["click"]()      # must not raise
        self.assertTrue(b.status)

    # -- what gets filtered out --------------------------------------------

    def test_entries_missing_a_name_or_a_url_are_dropped(self):
        """A nameless link is a button captioned with nothing, and the name
        is the only thing on it that says where it goes."""
        _b, nodes, _h = self._scene([
            {"Name": "IMDb", "Url": "https://imdb.example/1"},
            {"Name": "Nameless"},
            {"Url": "https://no-name.example/x"},
            {"Name": "", "Url": "https://blank.example/x"},
            {"Name": "Trakt", "Url": "https://trakt.example/2"}])
        self.assertEqual(self._labels(nodes), ["IMDb", "Trakt"])

    def test_the_same_url_twice_is_drawn_once(self):
        """Two server plugins for one database answer with the same link."""
        _b, nodes, _h = self._scene([
            {"Name": "TMDb", "Url": "https://tmdb.example/1"},
            {"Name": "TheMovieDb", "Url": "https://tmdb.example/1"}])
        self.assertEqual(self._labels(nodes), ["TMDb"])

    def test_a_heavily_tagged_item_is_capped(self):
        """Anime is tagged by half a dozen databases at once; past a couple
        of rows this stops being a reference and becomes the page."""
        many = [{"Name": "P%d" % i, "Url": "https://p%d.example/x" % i}
                for i in range(20)]
        _b, nodes, _h = self._scene(many)
        drawn = self._labels(nodes)
        self.assertEqual(len(drawn),
                         detail_components.MAX_PROVIDER_LINKS)
        self.assertEqual(drawn[0], "P0", "the cap dropped from the wrong end")

    def test_junk_in_the_list_does_not_break_the_page(self):
        """``ExternalUrls`` is server-composed and reaches us as parsed
        JSON, so a plugin writing something odd must cost a link and not
        the screen."""
        _b, nodes, _h = self._scene([
            "not a dict", None, 42,
            {"Name": "IMDb", "Url": "https://imdb.example/1"}])
        self.assertEqual(self._labels(nodes), ["IMDb"])

    # -- placement ---------------------------------------------------------

    def test_the_links_sit_between_the_synopsis_and_the_cast(self):
        """Where jellyfin-web puts them: the tail of the metadata, not an
        action on the item. Asserted by y, because "after the overview" is
        a claim about the laid-out page rather than about list order.
        """
        _b, nodes, _h = self._scene([
            {"Name": "IMDb", "Url": "https://imdb.example/1"}])
        link_y = [n["y"] for n in nodes
                  if (n.get("id") or "").startswith("detail-link-")]
        cast_y = [n["y"] for n in nodes
                  if (n.get("id") or "") == "detail-people"]
        play_y = [n["y"] for n in nodes
                  if (n.get("id") or "") == "btn-play"]
        self.assertTrue(link_y and cast_y and play_y)
        self.assertLess(max(play_y), min(link_y), "links drew above Play")
        self.assertLess(max(link_y), min(cast_y), "links drew below the cast")

    def test_many_links_wrap_instead_of_running_off_the_page(self):
        """A Row does not wrap. Eight providers at a narrow width is more
        than one row of buttons, and the tail would simply leave the window.
        """
        many = [{"Name": "Provider %d" % i,
                 "Url": "https://p%d.example/x" % i} for i in range(8)]
        b = self._browser(many)
        nodes, _h = build_scene(b, size=(900, 700))
        rows = {n["y"] for n in nodes
                if (n.get("id") or "").startswith("detail-link-")}
        self.assertGreater(len(rows), 1, "eight links drew on one row")
        widest = max(n["x"] + n["w"] for n in nodes
                     if (n.get("id") or "").startswith("detail-link-"))
        self.assertLessEqual(widest, 900, "a link ran off the window")


if __name__ == "__main__":
    unittest.main()


class SeriesAndSeasonLinkTest(unittest.TestCase):
    """The same row on the other two screens a show is browsed through.

    Both were missing when the detail screen got it, and they fail
    differently: a series page loads through ``get_item`` and therefore has
    the links for free, while a season page loads through a *list* query,
    which omits ``ExternalUrls`` unless it is asked for. So the season case
    is as much a test of ``LibrarySource.get_seasons`` as of the row.
    """

    def _scene(self, route, size=(1280, 720)):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.controller = FakeController()
        b.server = "srv1"
        b.nav_stack = [dict(route)]
        b._load_route(b.route)
        nodes, handlers = build_scene(b, size=size)
        return b, nodes, handlers

    def test_a_series_page_draws_its_links(self):
        _b, nodes, _h = self._scene(
            {"kind": "series", "item_id": "sh1", "server": "srv1"})
        self.assertTrue([i for i in ids(nodes)
                         if (i or "").startswith("detail-link-")])

    def test_a_series_link_opens_that_provider(self):
        b, _nodes, handlers = self._scene(
            {"kind": "series", "item_id": "sh1", "server": "srv1"})
        handlers["detail-link-0"]["click"]()
        self.assertEqual(b.controller.opened_urls,
                         ["https://www.imdb.com/title/tt1/"])

    def test_a_season_page_draws_the_seasons_own_links(self):
        """The season's, not the show's -- a season carries its own TMDB
        and TVDB urls, and drawing the series' would send the user to the
        wrong page while looking right."""
        b, _nodes, handlers = self._scene(
            {"kind": "season", "item_id": "se2", "series_id": "sh1",
             "server": "srv1"})
        handlers["detail-link-0"]["click"]()
        self.assertEqual(
            b.controller.opened_urls,
            ["https://thetvdb.example/series/1/seasons/2"])

    def test_the_season_links_are_above_the_episode_grid(self):
        """They ride in the header, which is also what keeps ``head_h``
        honest -- the virtualizer sizes the header to decide which grid
        rows are near the viewport."""
        _b, nodes, _h = self._scene(
            {"kind": "season", "item_id": "se1", "series_id": "sh1",
             "server": "srv1"})
        link_y = [n["y"] for n in nodes
                  if (n.get("id") or "").startswith("detail-link-")]
        cell_y = [n["y"] for n in nodes
                  if (n.get("id") or "").startswith("ep-")]
        self.assertTrue(link_y and cell_y)
        self.assertLess(max(link_y), min(cell_y))
    def test_the_season_links_share_the_title_row(self):
        """Beside To Series, not on a band of their own [iw].

        This screen already spends two rows above the grid; a third pushes
        the first episode off the fold. Asserted by shared centre-line, the
        way `test_action_rows` does -- "same row" is a fact about the laid
        out page, not about which list the buttons were appended to.
        """
        _b, nodes, _h = self._scene(
            {"kind": "season", "item_id": "se1", "series_id": "sh1",
             "server": "srv1"})
        mid = {n["id"]: round(n["y"] + n["h"] / 2.0) for n in nodes
               if n.get("id") in ("season-to-series", "detail-link-0")}
        self.assertEqual(len(mid), 2, "the season header lost a button")
        self.assertEqual(len(set(mid.values())), 1,
                         "the links dropped onto their own row: %r" % mid)


class OneSeasonSource(FakeSource):
    """A show with one season, so the header row is nothing but buttons.

    The configuration the spacing bug was reported in: with a banner and no
    picker there is no other KIND of control on that row, so a gap chosen to
    separate kinds is the only odd spacing on the screen.
    """

    def get_seasons(self, server_uuid, series_id):
        return super().get_seasons(server_uuid, series_id)[:1]


class SeasonHeaderSpacingTest(unittest.TestCase):
    """One gap between adjacent buttons on the season header, at any width.

    Measured against a *sibling* row on the same rendered page -- the
    actions row directly below -- rather than against a hardcoded 8, so it
    cannot pass by both drifting together.

    This was got wrong twice, the same way each time: the group being spaced
    was drawn too narrowly. First the links were grouped and To Series left
    outside; then the group could not break, so a narrow window fell back to
    the outer row's gap. Hence the width sweep rather than one render.
    """

    WIDTHS = ((1280, 720), (1100, 720), (900, 700), (700, 700))

    def _scene(self, source, size):
        b = MpvtkBrowser(app=None, source=source)
        b._pool = _SyncPool()
        b.controller = FakeController()
        b.server = "srv1"
        b.nav_stack = [{"kind": "season", "item_id": "se1",
                        "series_id": "sh1", "server": "srv1"}]
        b._load_route(b.route)
        return build_scene(b, size=size)[0]

    @staticmethod
    def _button_gaps(nodes):
        """Gaps between horizontally adjacent header buttons, rounded.

        To Series counts as one of them -- that is the whole point. Only
        pairs sharing a row are compared; a pair split by a wrap is not
        adjacent.
        """
        def is_button(node):
            nid = node.get("id") or ""
            # detail-web-link included deliberately: it draws in the middle
            # of this group, so a helper that skipped it would measure a gap
            # straight over it and report the whole row as unevenly spaced.
            return (nid in ("season-to-series", "detail-web-link")
                    or nid.startswith("detail-link-"))

        buttons = sorted((n for n in nodes if is_button(n)),
                         key=lambda n: (round(n["y"]), n["x"]))
        return {round(b_["x"] - (a["x"] + a["w"]))
                for a, b_ in zip(buttons, buttons[1:])
                if round(a["y"]) == round(b_["y"])}

    @staticmethod
    def _actions_gap(nodes):
        acts = sorted((n for n in nodes
                       if n.get("id") in ("se-nextup", "se-watched")),
                      key=lambda n: n["x"])
        return round(acts[1]["x"] - (acts[0]["x"] + acts[0]["w"]))

    def test_to_series_sits_at_the_same_gap_as_the_row_below(self):
        """The reported case, exactly: a single season, so the header row is
        only To Series and the provider button."""
        nodes = self._scene(OneSeasonSource(), (1280, 720))
        self.assertEqual(self._button_gaps(nodes),
                         {self._actions_gap(nodes)})

    def test_one_gap_at_every_width(self):
        for source, label in ((FakeSource(), "one link"),
                              (ManyLinkSeasons(), "eight links")):
            for size in self.WIDTHS:
                with self.subTest(source=label, size=size):
                    nodes = self._scene(source, size)
                    gaps = self._button_gaps(nodes)
                    self.assertTrue(gaps, "no adjacent header buttons to check")
                    self.assertEqual(gaps, {self._actions_gap(nodes)})

    def test_nothing_runs_off_the_window(self):
        for size in self.WIDTHS:
            with self.subTest(size=size):
                nodes = self._scene(ManyLinkSeasons(), size)
                links = [n for n in nodes
                         if (n.get("id") or "").startswith("detail-link-")]
                self.assertTrue(links)
                self.assertLessEqual(max(n["x"] + n["w"] for n in links),
                                     size[0])



class SeasonsQueryTest(unittest.TestCase):
    """``get_seasons`` asks for the field its screen needs.

    Pinned because the failure is silent and remote: the stock apiclient
    query hardcodes ``Fields=info()``, which has no ``ExternalUrls``, and a
    list route answers exactly as it does when nothing matched -- an empty
    row, on a screen that looks otherwise correct.
    """

    class _Api:
        def __init__(self):
            self.params = None

        def shows(self, handler, params):
            self.params = dict(params)
            self.handler = handler
            return {"Items": [{"Id": "se1"}]}

        def get_seasons(self, series_id):        # the fallback path
            self.params = {"Fields": "stock"}
            return {"Items": [{"Id": "fallback"}]}

    def _source(self, api):
        from types import SimpleNamespace

        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource

        source = LibrarySource.__new__(LibrarySource)
        source._conn = lambda _uuid: SimpleNamespace(api=api)
        return source

    def test_external_urls_is_requested(self):
        api = self._Api()
        self._source(api).get_seasons("srv1", "sh1")
        self.assertIn("ExternalUrls", api.params["Fields"])

    def test_the_stock_field_set_is_kept(self):
        """Added to, not replaced: the screen reads UserData, artwork tags
        and item counts off these same DTOs."""
        from jellyfin_apiclient_python.api import info

        api = self._Api()
        self._source(api).get_seasons("srv1", "sh1")
        for field in info().split(","):
            self.assertIn(field, api.params["Fields"])

    def test_an_apiclient_without_the_helper_still_lists_seasons(self):
        """The links are worth a field, not a screen."""
        class NoShows(self._Api):
            def shows(self, handler, params):
                raise AttributeError("this apiclient has no shows()")

        api = NoShows()
        seasons = self._source(api).get_seasons("srv1", "sh1")
        self.assertEqual([s["Id"] for s in seasons], ["fallback"])


class NoAddressSource(FakeSource):
    """A source that cannot name a server address — what the offline one is.

    ``OfflineLibrarySource.server_address`` answers None by construction:
    there is no server to send anybody to. Modelled as its own class rather
    than by patching, because the offline case is the one where a link that
    still drew would be an offer to open a page that cannot load.
    """

    def server_address(self, server_uuid):
        return None


class JellyfinWebLinkTest(unittest.TestCase):
    """#714 — a link back to the item's own page in jellyfin-web.

    Every test that cares about *where* the button goes presses it, for the
    reason the file exists: a row whose captions and layout are right can
    still send every button to the wrong url.
    """

    def _scene(self, route, source=None, size=(1280, 720)):
        b = MpvtkBrowser(app=None, source=source or FakeSource())
        b._pool = _SyncPool()
        b.controller = FakeController()
        b.server = "srv1"
        b.nav_stack = [dict(route)]
        b._load_route(b.route)
        nodes, handlers = build_scene(b, size=size)
        return b, nodes, handlers

    # -- the url -----------------------------------------------------------

    def test_the_detail_button_opens_this_items_web_page(self):
        b, _nodes, handlers = self._scene(DETAIL)
        handlers["detail-web-link"]["click"]()
        self.assertEqual(
            b.controller.opened_urls,
            ["https://home.example/web/#/details?id=m1&serverId=SRVID"])

    def test_it_names_the_server_the_item_came_from(self):
        """Not "a server": with two connected, a link composed from the
        wrong address looks right and opens somebody else's library."""
        from tests._shell_harness import MultiServerSource

        b, _nodes, handlers = self._scene(
            {"kind": "detail", "item_id": "m1", "server": "srv2"},
            source=MultiServerSource())
        handlers["detail-web-link"]["click"]()
        self.assertEqual(
            b.controller.opened_urls,
            ["https://remote.example/web/#/details?id=m1&serverId=SRVID"])

    def test_a_source_with_no_address_draws_no_button(self):
        _b, nodes, _h = self._scene(DETAIL, source=NoAddressSource())
        self.assertNotIn("detail-web-link", ids(nodes))
        self.assertTrue([i for i in ids(nodes)
                         if (i or "").startswith("detail-link-")],
                        "the provider links went with it")

    # -- the other two screens --------------------------------------------

    def test_a_series_page_carries_it(self):
        b, _nodes, handlers = self._scene(
            {"kind": "series", "item_id": "sh1", "server": "srv1"})
        handlers["detail-web-link"]["click"]()
        self.assertEqual(len(b.controller.opened_urls), 1)
        self.assertIn("id=sh1", b.controller.opened_urls[0])

    def test_a_season_page_links_to_the_season_not_the_show(self):
        """The same trap the provider links have on this screen: the season
        is browsed through the series, and the id in scope is the wrong one
        by default."""
        b, _nodes, handlers = self._scene(
            {"kind": "season", "item_id": "se2", "series_id": "sh1",
             "server": "srv1"})
        handlers["detail-web-link"]["click"]()
        self.assertEqual(
            b.controller.opened_urls,
            ["https://home.example/web/#/details?id=se2&serverId=SRVID"])

    # -- where it sits -----------------------------------------------------

    def test_it_leads_the_row_rather_than_joining_the_tail(self):
        _b, nodes, _h = self._scene(DETAIL)
        web = [n for n in nodes if n.get("id") == "detail-web-link"]
        first = [n for n in nodes if n.get("id") == "detail-link-0"]
        self.assertTrue(web and first)
        self.assertEqual(round(web[0]["y"]), round(first[0]["y"]),
                         "it dropped onto a row of its own")
        self.assertLess(web[0]["x"], first[0]["x"])

    def test_it_is_captioned_like_its_neighbours(self):
        """One word, so it reads as another entry in a row of links rather
        than as a different kind of control [iw]. Asserted because the
        caption is the whole of what tells the user where it goes."""
        _b, nodes, _h = self._scene(DETAIL)
        web = next(i for i, n in enumerate(nodes)
                   if n.get("id") == "detail-web-link")
        caption = next(n["text"] for n in nodes[web:web + 4]
                       if n.get("t") == "text" and n.get("text"))
        self.assertEqual(caption, "Web")

    def test_it_does_not_renumber_the_provider_buttons(self):
        """``detail-link-0`` still means the server's first provider. The
        ids are read by the remote-control tests and by anything that
        clicks one, so shifting them by one is a silent rename."""
        b, _nodes, handlers = self._scene(DETAIL)
        handlers["detail-link-0"]["click"]()
        self.assertEqual(b.controller.opened_urls,
                         ["https://www.imdb.com/title/tt1/"])


class JellyfinWebUrlTest(unittest.TestCase):
    """The composition on its own, including the shapes a page cannot make."""

    def url(self, address, item):
        return detail_components.jellyfin_web_url(address, item)

    def test_the_route_web_actually_uses(self):
        self.assertEqual(
            self.url("https://jf.example", {"Id": "abc", "ServerId": "s1"}),
            "https://jf.example/web/#/details?id=abc&serverId=s1")

    def test_a_trailing_slash_does_not_double_up(self):
        self.assertEqual(
            self.url("https://jf.example/", {"Id": "abc"}),
            "https://jf.example/web/#/details?id=abc")

    def test_a_dto_without_a_server_id_leaves_it_off(self):
        """Rather than sending ``serverId=None``. web falls back to the
        server the browser is signed in to, which is the right answer for a
        synthesized offline DTO."""
        self.assertEqual(self.url("https://jf.example", {"Id": "abc"}),
                         "https://jf.example/web/#/details?id=abc")

    def test_no_address_and_no_id_are_both_no_link(self):
        self.assertIsNone(self.url(None, {"Id": "abc"}))
        self.assertIsNone(self.url("", {"Id": "abc"}))
        self.assertIsNone(self.url("https://jf.example", {}))
        self.assertIsNone(self.url("https://jf.example", None))
