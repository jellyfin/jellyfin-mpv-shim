"""Gaps in a season, against a server that really has some.

The unit suite builds its own Missing and Unaired episodes, which makes every
question about them a question about the fixture. Two halves of this feature
are claims about the server and one is a claim about jellyfin-web, and none of
them can be asked of a dict this repo wrote:

* **the season listing shows the gaps.** `get_episodes` deliberately sends no
  filter, because that is what web does and because seeing the gap is the
  point. The server decides, from the user's `DisplayMissingEpisodes`, and a
  listing that quietly stopped returning them would take the whole feature
  with it -- silently, since an absent row looks like a season without gaps;
* **the play queue does not.** `get_season_queue` sends `IsMissing: false` and
  `IsVirtualUnaired: false` -- web's own filters for this gesture. **A filter
  Jellyfin does not recognise answers exactly as sending nothing does**
  (CLAUDE.md), so a dropped one here is invisible from this side: the queue
  would carry an unplayable episode and the failure would surface as a play
  that does nothing, far from the cause;
* **which word a gap gets is decided from the server's own PremiereDate.**
  Past is Missing, future is Unaired, and the fixture dates are real ones.

Then the three sites of the rule, on the real DTOs rather than on ours: the
tile chip, the tile menu, and the detail page's buttons.

The fixture is four virtual episodes injected straight into `BaseItems`;
nothing else can create one, because the only code path that sets
`IsVirtualItem` is the TMDb provider and stdjflib disables every internet
provider. This skips, saying so, where they are absent.
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


class _GapFixture:
    """The server, a source, and one real gap.

    A mixin rather than a base TestCase: subclassing a class that carries
    cases re-runs every one of them, which is four server queries answered
    twice and a count nobody can reconcile with the file.
    """

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        gaps = cls._virtual_episodes(cls.session)
        if not gaps:
            raise unittest.SkipTest(
                "no virtual episodes on this server; the fixture is a direct "
                "insert into BaseItems -- nothing else can create one, since "
                "the only path that sets IsVirtualItem is the TMDb provider "
                "and stdjflib disables every internet provider")
        cls.gap = gaps[0]
        cls.series_id = cls.gap["SeriesId"]
        cls.season_id = cls.gap["SeasonId"]

    @staticmethod
    def _virtual_episodes(session):
        """Every gap the server holds, asked for explicitly.

        `isMissing=true` rather than paging the whole library and filtering:
        the first draft of this read 400 episodes of 1054, saw none, and
        concluded the library had no gaps at all.
        """
        import json
        import urllib.parse
        import urllib.request

        params = urllib.parse.urlencode({
            "IncludeItemTypes": "Episode", "Recursive": "true",
            "isMissing": "true", "userId": session.user_id,
            "Fields": "LocationType,PremiereDate,SeasonId", "Limit": "50"})
        req = urllib.request.Request(
            session.address + "/Items?" + params,
            headers={"Authorization":
                     'MediaBrowser Token="%s"' % session.token})
        with urllib.request.urlopen(req, timeout=20) as resp:
            got = json.loads(resp.read()) or {}
        return [i for i in (got.get("Items") or [])
                if i.get("LocationType") == "Virtual" and i.get("SeasonId")]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def _ids(self, items):
        return {i.get("Id") for i in items}


@_e2e.require_server
class TheServerHalfTest(_GapFixture, unittest.TestCase):
    """What the two queries come back with. No UI."""

    def test_the_season_listing_shows_the_gap(self):
        """Deliberate, and web's behaviour: the listing sends no filter so the
        gap is visible. Without this the rest of the feature has nothing to
        act on and its absence would look like a season with no gaps."""
        listing = self.source.get_episodes(
            _e2e.SOURCE_UUID, self.series_id, self.season_id)

        self.assertIn(
            self.gap["Id"], self._ids(listing),
            "the season listing did not include the gap, so nothing on the "
            "screen has anything to mark")

    def test_the_play_queue_leaves_it_out(self):
        """The filters `get_season_queue` sends, honoured by the server.

        This is the assertion that cannot be made anywhere else: an
        unrecognised filter answers exactly as no filter does, so a dropped
        `IsMissing` is invisible until an unplayable episode is queued.
        """
        queue = self.source.get_season_queue(
            _e2e.SOURCE_UUID, self.series_id, self.season_id)

        self.assertNotIn(
            self.gap["Id"], self._ids(queue),
            "the queue carries a gap, so the server ignored IsMissing / "
            "IsVirtualUnaired and the filters are doing nothing")

    def test_the_queue_is_the_listing_minus_the_gaps(self):
        """The control, and it is the one that matters.

        A queue that came back empty, or errored into `[]`, would satisfy the
        test above while breaking playing a season entirely.
        """
        listing = self.source.get_episodes(
            _e2e.SOURCE_UUID, self.series_id, self.season_id)
        queue = self.source.get_season_queue(
            _e2e.SOURCE_UUID, self.series_id, self.season_id)

        gaps = {i["Id"] for i in listing
                if i.get("LocationType") == "Virtual"}
        self.assertTrue(gaps, "no gap in this season's listing after all")
        self.assertTrue(queue, "the queue came back empty")
        self.assertEqual(self._ids(listing) - gaps, self._ids(queue))

    def test_the_word_comes_from_the_servers_own_date(self):
        """Past is Missing, future is Unaired -- decided from PremiereDate,
        which here is the server's rather than one this repo chose."""
        from jellyfin_mpv_shim.mpvtk_browser import components

        listing = self.source.get_episodes(
            _e2e.SOURCE_UUID, self.series_id, self.season_id)
        labelled = {i["Id"]: components.virtual_episode_label(i)
                    for i in listing if i.get("LocationType") == "Virtual"}

        self.assertTrue(labelled, "no gap to label")
        for item_id, label in labelled.items():
            self.assertTrue(
                label, "a virtual episode got no label at all: %s" % item_id)


@_e2e.require_server
class TheThreeSitesRefuseARealGapTest(_GapFixture, unittest.TestCase):
    """The rule, on the DTOs the server actually sends.

    The unit tests ask the same three questions of items this repo built, so
    a field the real DTO spells differently -- or does not carry at all --
    would leave all three answering about a dict rather than about an episode.
    """

    def _browser(self):
        from tests._shell_harness import FakeSource

        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        browser = MpvtkBrowser(app=None, source=FakeSource())
        browser.nav_stack = [{"kind": "home", "server": "srv1"}]
        self.addCleanup(browser.shutdown)
        return browser

    def test_the_tile_offers_no_play_chip(self):
        self.assertFalse(self._browser()._tile_playable(self.gap))

    def test_an_ordinary_episode_still_does(self):
        """The control on the real data: a predicate answering False for
        everything would satisfy the test above."""
        listing = self.source.get_episodes(
            _e2e.SOURCE_UUID, self.series_id, self.season_id)
        real = next(i for i in listing
                    if i.get("LocationType") != "Virtual")

        self.assertTrue(self._browser()._tile_playable(real))

    def test_the_tile_menu_offers_no_way_to_play_it(self):
        browser = self._browser()

        actions = {a for _label, _icon, a in browser._tile_menu_entries(self.gap)}

        self.assertFalse(actions & {"play", "restart", "queue", "queuenext"},
                         "the tile menu offers %s for a real gap"
                         % sorted(actions & {"play", "restart", "queue",
                                             "queuenext"}))

    def test_the_detail_page_offers_no_play_buttons(self):
        browser = self._browser()
        browser.nav_stack = [{"kind": "detail", "server": "srv1",
                              "item_id": self.gap["Id"],
                              "title": self.gap.get("Name")}]
        page = browser._page_for(browser.route)

        row = page._play_buttons(self.gap, "srv1")

        ids = []
        stack = [row]
        while stack:
            node = stack.pop()
            if getattr(node, "id", None):
                ids.append(node.id)
            stack.extend(getattr(node, "children", None) or [])
        self.assertNotIn("btn-play", ids)
        self.assertNotIn("btn-resume", ids)


if __name__ == "__main__":
    unittest.main()
