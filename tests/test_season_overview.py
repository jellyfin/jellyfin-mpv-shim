"""A season page shows the season's own description.

Reported as broken, and it was: `pages/season.py` contained the string
`Overview` zero times, while the series and detail pages both render one.
Nothing about the data was missing -- `repository.get_seasons` asks for
`info()`, which includes `Overview`, so the DTO has carried it all along.
jellyfin-web draws it too (`itemDetails/index.js` adds `Overview` to its
Fields when the type is Season).

The ruled surface is the season **page**, not the season tiles on the series
page: "someone said that was broken" fitted both, and the owner picked one.

The second test is the one that stops this from being satisfied by a page that
always draws a paragraph: `FakeSource` gives season 1 an overview and season 2
none, which is what a real library looks like.
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

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource, build_scene          # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser      # noqa: E402


def _texts(browser, size=(1280, 720)):
    nodes, _h = build_scene(browser, size)
    return [n.get("text") for n in nodes if n.get("t") == "text"]


def _browser(item_id, title):
    b = MpvtkBrowser(app=None, source=FakeSource())
    b.nav_stack = [{"kind": "season", "item_id": item_id, "series_id": "sh1",
                    "server": "s1", "title": title, "bar_title": "A Show"}]
    b._load_route(b.route)
    b._pool.shutdown(wait=True)
    return b


class ASeasonDescriptionIsDrawnTest(unittest.TestCase):
    def test_the_seasons_overview_is_on_the_page(self):
        overview = FakeSource().get_seasons("sh1", "s1")[0]["Overview"]

        self.assertIn(overview, _texts(_browser("se1", "Season 1")),
                      "the season page does not draw its own description")

    def test_a_season_with_no_overview_draws_no_empty_paragraph(self):
        """The control. A page that unconditionally drew `Overview` would put
        an empty Text in the header -- which costs `header_offset` a line it
        cannot see the reason for, and shifts the grid down on every season
        nobody has described."""
        texts = _texts(_browser("se2", "Season 2"))

        self.assertEqual([], [t for t in texts if t == ""],
                         "an empty paragraph was drawn for a season with no "
                         "description")

    def test_it_wraps_to_the_grids_own_column(self):
        """Not to the window: this page centres its grid, so with
        `grid_fill: center` the column is up to ~94px narrower per side, and
        the scrollbar takes 10 more. Asserted as the node's width rather than
        as a pixel count, because the number moves with the type scale."""
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        b = _browser("se1", "Season 1")
        overview = FakeSource().get_seasons("sh1", "s1")[0]["Overview"]
        nodes, _h = build_scene(b, (1280, 720))
        para = [n for n in nodes
                if n.get("t") == "text" and n.get("text") == overview]

        self.assertTrue(para, "the description is not in the scene at all")
        self.assertLessEqual(
            para[0]["w"], chrome.body_width(1280, chrome.CONTENT_PAD),
            "the description is wrapped wider than a padded column, so its "
            "tail runs under the scrollbar")


if __name__ == "__main__":
    unittest.main()
