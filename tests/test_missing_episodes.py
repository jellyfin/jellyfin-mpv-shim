"""An episode the server has no file for is shown, marked, and not playable.

A **virtual** episode is one Jellyfin lists out of the series metadata: it is in
the season because the metadata says it aired, and there is nothing to play. The
server only sends them when the user's own `DisplayMissingEpisodes` is on
(`TvShowsController.cs`, both supported majors), so seeing them is that setting
working -- which is why nothing here hides them.

jellyfin-web's behaviour, which is the ruling, is two things and this file is
about both:

* `indicators.js` marks the card **Missing**, or **Unaired** when the air date
  is still ahead;
* `cardBuilder.js` gives it **no play button** -- and neither does our tile's
  play chip nor the detail page's Play, which is one rule at two sites. A rule
  applied at N-1 of N sites is this repository's most-repeated defect shape, so
  both are asserted here rather than in two files.

And a third site the review never saw, found while reading the server source:
the **cross-season** play queue sent no filter at all, so for a user with the
setting on it could hand the player an episode with no file. The sharpest case
is the Next Up fallback, `limit=1` on a series nobody has started: the server
applies `isMissing` *before* `limit`, so a missing S01E01 was exactly what came
back.
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
from types import SimpleNamespace as NS
from unittest import mock

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource                        # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser import components             # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import strips                 # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import theme                  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser       # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (             # noqa: E402
    LibrarySource)


def _button_ids(widget):
    """Every widget id in a built row. The buttons are widgets, not scene
    nodes -- `_play_buttons` is called directly here rather than through a
    render, because a detail route would need the fake source to serve a
    virtual episode and the question is about the button list."""
    out = []
    stack = [widget]
    while stack:
        node = stack.pop()
        wid = getattr(node, "id", None)
        if wid:
            out.append(wid)
        stack.extend(getattr(node, "children", None) or [])
    return out


FUTURE = "2099-04-01T00:00:00.0000000Z"
PAST = "2001-04-01T00:00:00.0000000Z"


def _episode(**kw):
    item = {"Id": "e1", "Type": "Episode", "Name": "An Episode",
            "SeriesName": "A Show", "MediaType": "Video",
            "IndexNumber": 1, "ParentIndexNumber": 1}
    item.update(kw)
    return item


MISSING = _episode(Id="gap", Name="A Gap", LocationType="Virtual",
                   PremiereDate=PAST)
UNAIRED = _episode(Id="soon", Name="Coming Soon", LocationType="Virtual",
                   PremiereDate=FUTURE)
NORMAL = _episode(Id="real", Name="A Real One", LocationType="FileSystem")


class TheLabelTest(unittest.TestCase):
    """web's rule, and the rows that make it a rule rather than a lookup."""

    def test_the_table(self):
        cases = [
            ("a past-dated virtual episode", MISSING, "Missing"),
            ("a future-dated virtual episode", UNAIRED, "Unaired"),
            # An air date is the only evidence that something is coming
            # rather than absent, so no date means Missing.
            ("a virtual episode with no air date",
             _episode(LocationType="Virtual"), "Missing"),
            ("an ordinary episode", NORMAL, None),
            ("an episode with no LocationType at all", _episode(), None),
            # LocationType: Virtual exists on other types (a Series whose
            # files are gone), and web's indicator is Episode-only.
            ("a virtual SERIES", {"Type": "Series", "LocationType": "Virtual"},
             None),
        ]
        for label, item, expected in cases:
            with self.subTest(label):
                self.assertEqual(expected,
                                 components.virtual_episode_label(item))

    def test_the_two_words_are_different_words(self):
        """The premise of the rows above. If the dated cases ever agree, this
        file stops testing web's distinction and starts testing that virtual
        episodes get *a* label."""
        self.assertNotEqual(components.virtual_episode_label(MISSING),
                            components.virtual_episode_label(UNAIRED))


class TheTagChipFitsTest(unittest.TestCase):
    """The tag is baked into the strip bitmap, beside its neighbours.

    Nothing clips it back: several tiles share one image, so a chip wider than
    its tile draws over the tile to its right rather than being cut off. The
    captions below it already ellipsize to `g.tile_w` for exactly this reason
    (`_paint_caption`); the chip is the fourth baked-text site and did not ask.

    "Missing" and "Unaired" fit at every locale measured, so this is a guard
    against a translation nobody has read yet, not a reproduction of one.
    """

    def _chip(self, text, max_w):
        from PIL import Image, ImageDraw

        tile_w = strips._px(240)
        img = Image.new("RGBA", (tile_w * 3, tile_w), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        strips.StripStore._paint_text_chip(
            img, dr, strips._px(17), strips._px(17), text, 14, max_w=max_w)
        return tile_w, img.getbbox()

    def test_a_long_tag_stays_inside_its_tile(self):
        text = "Nicht ausgestrahlt und noch nicht verfügbar"
        tile_w, unclamped = self._chip(text, None)
        # Pinned so the case cannot quietly stop being one: if this string
        # ever fits unclamped, the test below proves nothing and says so.
        self.assertGreater(
            unclamped[2], tile_w,
            "the sample no longer overflows a tile unclamped -- pick a "
            "longer one, or this test has stopped testing anything")

        _tw, box = self._chip(text, tile_w - strips._px(8))

        self.assertLessEqual(
            box[2], tile_w,
            "the tag chip drew %dpx past its own tile, into the next one"
            % (box[2] - tile_w))

    def test_a_tag_that_fits_is_left_alone(self):
        """The control: a clamp that shortened everything would pass the test
        above and ellipsize "Unaired" on every tile in the library."""
        _tw, clamped = self._chip("Unaired", strips._px(240) - strips._px(8))
        _tw, unclamped = self._chip("Unaired", None)

        self.assertEqual(unclamped, clamped)


class TheTagChipFitsOnShadowedThemesTest(unittest.TestCase):
    """The same claim on the other branch of `_paint_text_chip`.

    Two things had to be wrong at once for this to ship, and the class above
    could not see either. It draws on the FIRST tile, where a left overflow
    lands at a negative x that PIL simply clips -- `getbbox()` never reports
    it -- and it asserts only the right edge. And it runs on the default
    theme, which takes the filled-chip branch. No test in the tree rendered
    this function under `badge_shadow` at all.

    Both of this round's defects were on the other side of that: the clamp
    sat below the shadowed branch's early return so it never ran, and the
    shadowed text was centred on `cx` while the function's own summary line
    says the chip is pinned by its LEFT edge there. Neither fix alone is
    enough -- a centred word that fits still starts half a word too far left,
    and a left-pinned word with no clamp still runs off the right.

    So: a MIDDLE tile, where both edges are real, and both edges asserted.
    """

    #: Tile 1 of 3. On tile 0 a left bleed is clipped at x=0 and invisible to
    #: `getbbox()`, which is how 161px of it went unnoticed.
    TILE = 1

    def _chip(self, text, shadow=True):
        from PIL import Image, ImageDraw

        tile_w = strips._px(240)
        img = Image.new("RGBA", (tile_w * 3, tile_w), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        x0 = tile_w * self.TILE
        with mock.patch.object(strips.StripStore, "shadowed_badges",
                               staticmethod(lambda: shadow)):
            strips.StripStore._paint_text_chip(
                img, dr, x0 + strips._px(17), strips._px(17), text, 14,
                max_w=tile_w - strips._px(8))
        return x0, tile_w, img.getbbox()

    def _assert_inside(self, text):
        x0, tile_w, box = self._chip(text)
        self.assertIsNotNone(box, "nothing was drawn for %r" % text)
        self.assertGreaterEqual(
            box[0], x0,
            "%r drew %dpx past the LEFT of its own tile, into the one before "
            "it. Several tiles share one strip bitmap, so this is not "
            "clipped -- it is painted over a neighbour."
            % (text, x0 - box[0]))
        self.assertLessEqual(
            box[2], x0 + tile_w,
            "%r drew %dpx past the RIGHT of its own tile"
            % (text, box[2] - (x0 + tile_w)))

    def test_the_english_words_fit(self):
        """Not a guard against an unread translation: these bleed 15px left
        at any length, so every Missing and Unaired tile on a shadowed theme
        was painting over the tile beside it."""
        for text in ("Missing", "Unaired"):
            with self.subTest(text=text):
                self._assert_inside(text)

    def test_a_long_tag_fits(self):
        text = "Nicht ausgestrahlt und noch nicht verfügbar"
        _x0, tile_w, unclamped = self._chip(text)
        self.assertGreater(
            unclamped[2] - unclamped[0], 0,
            "the sample drew nothing")

        self._assert_inside(text)

    def test_the_word_sits_where_the_filled_theme_puts_it(self):
        """The other half of the left pin, and the reason it is the GLYPHS'
        edge rather than the layer's.

        A shadow pad is invisible halo. Pinning the padded layer would fit
        the tile just as well and move the word a few px right of where the
        same word sits on a filled theme -- visible as a twitch when the
        theme changes, and not something a "does it fit" assertion can see.
        """
        self.assertEqual(self._ink_x("Missing", shadow=False),
                         self._ink_x("Missing", shadow=True),
                         "the word moved when the theme changed")

    def _ink_x(self, text, shadow):
        """The leftmost column holding white glyph ink.

        White specifically: the filled branch draws a grey chip behind the
        word and the shadowed one a dark halo around it, so a bounding box
        measures a different thing on each branch and cannot be compared
        across them. The glyphs are the only thing both branches draw.
        """
        from PIL import Image, ImageDraw

        tile_w = strips._px(240)
        img = Image.new("RGBA", (tile_w * 3, tile_w), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        x0 = tile_w * self.TILE
        with mock.patch.object(strips.StripStore, "shadowed_badges",
                               staticmethod(lambda: shadow)):
            strips.StripStore._paint_text_chip(
                img, dr, x0 + strips._px(17), strips._px(17), text, 14,
                max_w=tile_w - strips._px(8))
        px = img.load()
        for x in range(img.width):
            for y in range(img.height):
                r, g, b, a = px[x, y]
                if a > 200 and r > 230 and g > 230 and b > 230:
                    return x - x0
        self.fail("no glyph ink found for %r (shadow=%s)" % (text, shadow))


class NoPlayAffordanceTest(unittest.TestCase):
    """All THREE sites of one rule, in one test class on purpose.

    The count was two when this was written, and two was wrong: the browser
    offers to play an item from the tile chip, from the detail page's buttons,
    and from the tile's context menu. Missing the third is what the comments
    here call the N-1-of-N shape, arrived at by enumerating the sites by hand.
    """

    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b.nav_stack = [{"kind": "home", "server": "srv1"}]
        return b

    def test_a_tile_gets_no_play_chip(self):
        b = self._browser()

        self.assertFalse(b._tile_playable(MISSING))
        self.assertFalse(b._tile_playable(UNAIRED))

    def test_an_ordinary_episode_still_gets_one(self):
        """The control, and the half that matters: a predicate returning False
        for everything passes the test above."""
        b = self._browser()

        self.assertTrue(b._tile_playable(NORMAL))

    def test_the_detail_page_offers_no_play(self):
        b = self._browser()
        b.nav_stack = [{"kind": "detail", "server": "srv1", "item_id": "gap",
                        "title": "A Gap"}]
        page = b._page_for(b.route)
        with_position = dict(MISSING)
        with_position["UserData"] = {"PlaybackPositionTicks": 60 * 10000000}
        row = page._play_buttons(with_position, "srv1")

        self.assertNotIn("btn-play", _button_ids(row),
                         "a missing episode has a Play button")
        self.assertNotIn(
            "btn-resume", _button_ids(row),
            "a missing episode has a Resume button -- a position on one is "
            "possible, since the file was there once")

    def test_an_ordinary_episode_still_has_play_and_resume(self):
        """The same control on the second site. Resume needs a position, so
        this is the item with one."""
        b = self._browser()
        b.nav_stack = [{"kind": "detail", "server": "srv1", "item_id": "real",
                        "title": "A Real One"}]
        page = b._page_for(b.route)
        started = dict(NORMAL)
        started["UserData"] = {"PlaybackPositionTicks": 60 * 10000000}
        row = page._play_buttons(started, "srv1")

        found = _button_ids(row)
        self.assertIn("btn-play", found)
        self.assertIn("btn-resume", found)

    #: Every menu action that starts playback, now or later. A gap must offer
    #: none of them: `get_playback_url` declines a sourceless item, so each one
    #: ends in `play()` logging "no URL found" and returning with the screen
    #: unchanged and nothing said.
    PLAYING_ACTIONS = {"play", "restart", "queue", "queuenext"}

    def _menu_actions(self, item):
        b = self._browser()
        return {action for _label, _icon, action
                in b._tile_menu_entries(item)}

    def test_the_tile_menu_offers_no_way_to_play_it(self):
        """The third site. A position is possible on a gap -- the file was
        there once -- so this is the item that would otherwise get Resume and
        Play from beginning as well."""
        with_position = dict(MISSING)
        with_position["UserData"] = {"PlaybackPositionTicks": 60 * 10000000}

        actions = self._menu_actions(with_position)

        self.assertFalse(
            actions & self.PLAYING_ACTIONS,
            "a missing episode's tile menu offers %s"
            % sorted(actions & self.PLAYING_ACTIONS))

    def test_an_ordinary_episodes_tile_menu_still_plays(self):
        """The control on the third site."""
        actions = self._menu_actions(NORMAL)

        self.assertIn("play", actions)
        self.assertIn("queue", actions)

    def test_a_gap_keeps_the_entries_that_are_not_about_playing(self):
        """The gate is on playback, not on the item. Go to Series is the one
        thing there IS to do with a gap, and suppressing the whole menu would
        take it away."""
        actions = self._menu_actions(dict(MISSING, SeriesId="sh1"))

        self.assertIn("goseries", actions)
        self.assertIn("watched", actions)


class TheGapsCallToActionTest(unittest.TestCase):
    """Suppressing both play buttons took the page's only ``autofocus`` with
    them, so a Missing episode opened with nothing nominated and the first
    arrow press had to hunt for focus.

    [iw]: "Go to Series should be the focused option, brought to the begining
    of the list and made to be blue."
    """

    def _actions_row(self, item):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b.nav_stack = [{"kind": "detail", "server": "srv1",
                        "item_id": item["Id"], "title": item.get("Name")}]
        page = b._page_for(b.route)
        return page._detail_actions(item, "srv1")

    def test_a_gap_leads_with_go_to_series(self):
        row = self._actions_row(dict(MISSING, SeriesId="sh1"))

        first = row.children[0]
        self.assertEqual("act-series", first.id,
                         "the row leads with %s" % first.id)
        self.assertTrue(first.autofocus,
                        "nothing on the page is nominated for a remote")
        self.assertEqual(theme.ACCENT, first.bg,
                         "Go to Series is not the accented call to action")

    def test_an_ordinary_episode_is_unchanged(self):
        """The control. Play is still this page's call to action when there
        is one, and Go to Series stays a secondary action at the end."""
        row = self._actions_row(dict(NORMAL, SeriesId="sh1"))

        ids = [c.id for c in row.children]
        self.assertEqual("act-series", ids[-1],
                         "Go to Series moved for an episode that can play")
        series = row.children[-1]
        self.assertFalse(series.autofocus)
        self.assertNotEqual(theme.ACCENT, series.bg)


#: What the server has for this series: a missing first episode and two real
#: ones. The order is the point -- `limit=1` is what the Next Up fallback
#: sends, and the gap is what it used to get.
SERIES_EPISODES = [MISSING, NORMAL, _episode(Id="real2", Name="Another",
                                             LocationType="FileSystem")]


class _ServerLikeApi:
    """Answers `Shows/{id}/Episodes` the way `TvShowsController` does.

    The ORDER of operations is the fixture's whole value and it is the
    server's: the include decision first, then `isMissing`, and `startItemId`
    and `limit` **after that** -- which is why `limit=1` on a series whose
    first episode is missing returned the missing one rather than skipping it.
    A fake that filtered after limiting would make the fix look unnecessary.
    """

    def __init__(self):
        self.calls = []

    def shows(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        items = list(SERIES_EPISODES)
        if params.get("IsMissing") is False:
            items = [i for i in items if i.get("LocationType") != "Virtual"]
        limit = params.get("Limit")
        return {"Items": items[:limit] if limit else items}

    def get_season(self, show_id, season_id):
        """The apiclient's season LISTING helper: `UserId` and `SeasonId`, and
        no filters at all -- which is already web's shape for the screen."""
        return self.shows("/%s/Episodes" % show_id,
                          {"UserId": "{UserId}", "SeasonId": season_id})

    def get_episodes(self, series_id, season_id=None, start_item_id=None,
                     fields=None, limit=None):
        """The apiclient helper, which takes no filter arguments -- which is
        the reason both queue methods bypass it."""
        params = {"UserId": "{UserId}"}
        if season_id is not None:
            params["SeasonId"] = season_id
        if start_item_id is not None:
            params["StartItemId"] = start_item_id
        if fields is not None:
            params["Fields"] = fields
        if limit is not None:
            params["Limit"] = limit
        return self.shows("/%s/Episodes" % series_id, params)


class TheCrossSeasonQueueTest(unittest.TestCase):
    def _repo(self):
        repo = LibrarySource.__new__(LibrarySource)
        api = _ServerLikeApi()
        repo._conns = {"s1": NS(api=api)}
        return repo, api

    def test_the_next_up_fallback_does_not_resolve_to_the_gap(self):
        """`limit=1` on a series nobody has started. This is the call
        `item_actions` makes behind Play on a Series tile, and the episode it
        used to return does not exist."""
        repo, _api = self._repo()

        items = repo.get_series_queue("s1", "sh1", limit=1)

        self.assertEqual(["real"], [i["Id"] for i in items])

    def test_a_whole_series_queue_carries_no_gaps(self):
        repo, _api = self._repo()

        items = repo.get_series_queue("s1", "sh1")

        self.assertEqual(["real", "real2"], [i["Id"] for i in items])

    def test_it_asks_with_both_filters(self):
        """Named as well as observed: the server applies them, so the params
        are the contract and the behaviour above is downstream of it."""
        repo, api = self._repo()

        repo.get_series_queue("s1", "sh1", start_item_id="real", limit=50)

        _path, params = api.calls[-1]
        self.assertIs(False, params.get("IsMissing"))
        self.assertIs(False, params.get("IsVirtualUnaired"))
        self.assertEqual("real", params.get("StartItemId"))
        self.assertEqual(50, params.get("Limit"))

    def test_the_season_queue_still_does_too(self):
        """Its sibling, which had the filters all along -- here so that the
        pair is visible in one place, since the defect was one of them having
        them and the other not."""
        repo, api = self._repo()

        repo.get_season_queue("s1", "sh1", "se1")

        _path, params = api.calls[-1]
        self.assertIs(False, params.get("IsMissing"))
        self.assertIs(False, params.get("IsVirtualUnaired"))
        self.assertEqual("se1", params.get("SeasonId"))

    def test_the_season_listing_is_unfiltered(self):
        """The other direction, and it is deliberate: the SCREEN shows the
        gaps -- that is what the badge is for, and what web does -- and only
        the queue refuses them."""
        repo, api = self._repo()

        repo.get_episodes("s1", "sh1", "se1")

        _path, params = api.calls[-1]
        self.assertNotIn("IsMissing", params)
        self.assertNotIn("IsVirtualUnaired", params)


if __name__ == "__main__":
    unittest.main()
