"""Text has to fit the window at every scale, not only at 1x.

Everything in view code is logical (see ``mpvtk/scaling.py``), so a screen is
laid out against ``physical / ui_scale``. That makes the UI scale a *width*
problem rather than a font problem: at 200% a 1280px window is a 640px page,
and every label, button row and heading has to survive it. Nothing in the
suite was checking that, because everything is developed and tested at 1x
where the logical size and the window are the same number.

So: build the representative screens across a matrix of scales and window
sizes, and assert that no text run is drawn outside the window.

Two things this deliberately does not flag:

* **Anything inside a horizontal scroll container.** A carousel's content is
  meant to extend past the window — that is what scrolling it means.
* **Ellipsis.** ``layout`` truncates a label to its assigned box, so a
  cramped screen degrades to "Continue Watchi…" rather than to overflow.
  That is the intended behaviour, and it is why the check below measures the
  *drawn* run rather than the node's box: an over-wide box that ellipsizes is
  fine, an over-wide box whose text reaches past the window is not.

The matrix is (scale, window) pairs, not a cross product of everything: the
combinations below are the ones a user can actually produce. ``ui_scale``
offers 100/125/150/200% (config.py), and "Follow display" can hand back
anything the compositor reports.

Four things it found on the way in, none of them scale-specific once you
look — every one is a Row or a paragraph that assumed a wide page, and every
one is equally reachable at 100% on a small window:

* the settings tab bar, whose last tab ("Logs") drew off the right edge;
* the grid's A-Z filter bar, 754px of fixed cells;
* the album and artist headers, whose overview wrapped to the *page* width
  while being drawn in a column that starts 148px in;
* the play queue's and playlist editor's toolbars.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from tests._scene_snapshot import frozen_clock                # noqa: E402
from tests._shell_harness import FakeSource                   # noqa: E402

from jellyfin_mpv_shim.mpvtk import scaling                   # noqa: E402
from jellyfin_mpv_shim.mpvtk.layout import (                  # noqa: E402
    SCROLLBAR_W, layout, text_width)
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402


class WordySource(FakeSource):
    """The fake library, with the text a real one has.

    The stock fixture's overview is one repeated sentence and its items have
    no genres, so the paragraph and meta-line paths — the ones with the most
    room to get a width wrong — were laid out against text too tame to reach
    an edge. A matrix that measures short strings measures very little.
    """

    OVERVIEW = (
        "When an ordinary man is drawn into a conspiracy he does not "
        "understand, he has to decide how much of himself he is willing to "
        "lose to see it through. Shot over four years on three continents, "
        "it is a film about the cost of certainty.\n\n"
        "Restored in 4K from the original camera negative."
    )

    #: Several rows with names of real length, for the three row-per-thing
    #: screens. The harness ships one row of one item, which draws two
    #: labels -- below MIN_TEXTS, and nowhere near enough page to measure.
    #: The captions are not labels at any width (they are baked into the
    #: strip bitmap), so what these screens put on the page is their row
    #: HEADINGS, and a screen with one heading cannot overflow.
    _ROW_TITLES = ("Science Fiction & Fantasy", "Documentary",
                   "Action & Adventure", "Mystery & Thriller")

    @staticmethod
    def _row_items(prefix, n=10):
        return [{"Id": "%s-%d" % (prefix, i), "Name": "A Film of Some Name",
                 "Type": "Movie", "PrimaryImageAspectRatio": 2 / 3}
                for i in range(n)]

    @property
    def genre_rows(self):
        return [{"key": "g%d" % i, "title": t, "types": "Movie",
                 "items": self._row_items("g%d" % i)}
                for i, t in enumerate(self._ROW_TITLES)]

    @property
    def favorite_rows(self):
        return [{"key": "f%d" % i, "title": t, "types": "Movie",
                 "items": self._row_items("f%d" % i)}
                for i, t in enumerate(self._ROW_TITLES)]

    @property
    def byname_rows(self):
        return [{"key": "b%d" % i, "title": t, "types": "Movie",
                 "total": 40, "items": self._row_items("b%d" % i)}
                for i, t in enumerate(self._ROW_TITLES)]

    def get_item(self, server_uuid, item_id, **kw):
        item = super().get_item(server_uuid, item_id, **kw)
        if not item:
            return item
        item = dict(item)
        # Replaced, not defaulted: the fixture's own overview is one short
        # sentence repeated, with no paragraph break and no long word in it.
        item["Overview"] = self.OVERVIEW
        item.setdefault("Genres", ["Drama", "Thriller", "Science Fiction",
                                   "Mystery", "Adventure"])
        return item


#: The scales Settings offers, plus the ones "Follow display" and ``--scale``
#: can produce — the compositor reports whatever it likes and is under no
#: obligation to pick one of ours, and sub-1x is a real setting on a small
#: high-density panel.
#:
#: The fractional ones are the interesting half, and **the sub-1x ones are
#: the sharpest**: they are where ``px()``'s rounding disagreed most with the
#: width layout fitted the text to. At 0.75x an 18px run was drawn at 14px,
#: i.e. 18.67 logical — 3.7% wide, which on a full-width paragraph is tens of
#: pixels out and under the scrollbar. See ``scaling._EXACT_KEYS``.
SCALES = (0.75, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0)

#: Physical window sizes. The small end is a windowed player on a laptop; the
#: large end is what a 4K display hands back before scaling.
WINDOWS = ((1024, 576), (1280, 720), (1600, 900), (1920, 1080), (2560, 1440),
           (3840, 2160))

#: Logical widths below this are not a supported configuration — 200% on a
#: 1024px window is a 512px page, which is narrower than the chrome's own
#: buttons. Skipping them keeps the matrix honest instead of pinning failures
#: nobody can hit.
MIN_LOGICAL_W = 640

#: The screens, by route. The scene snapshots' set (distinct layout shapes)
#: plus the ones carrying dense chrome, which is where a row of controls runs
#: out of page: tab bars, filter bars, track tables and the queue's toolbar.
#:
#: ``favorites``, ``genres`` and ``byname`` used to be excluded here, on the
#: grounds that the fake source had no data for them and they would render an
#: empty state that passes at any width. That has not been true since the
#: harness grew ``genre_rows`` / ``favorite_rows`` / ``byname_rows`` -- they
#: drew a real screen and nobody was measuring it, which is how a screen
#: reported as misbehaving on a scaled display turned out never to have been
#: laid out at a scale here. They are in, over ``RowSource``'s several rows:
#: one row of one item is a screen too thin to run out of anything.
#:
#: Still absent, and each for the same honest reason -- no fixture that would
#: draw the real thing: ``reader``, ``comic``, ``book``/``audiobook``/
#: ``books``, ``playlist``/``playlist_edit``, ``person``, ``music_genre``,
#: and Live TV's ``program`` / ``channel``. ``test_every_screen_has_a_test``
#: guards the table below, not the set of routes that exist, so adding a
#: screen here is the only thing that starts measuring it.
SCREENS = {
    "home": {"kind": "home", "server": "s1"},
    "grid": {"kind": "grid", "parent_id": "lib1", "server": "s1",
             "title": "Movies"},
    "list": {"kind": "list", "parent_id": "lib1", "server": "s1",
             "title": "Movies"},
    "search": {"kind": "search", "server": "s1", "term": "a"},
    "settings": {"kind": "settings", "server": "s1"},
    "detail": {"kind": "detail", "item_id": "m1", "server": "s1"},
    "series": {"kind": "series", "item_id": "sh1", "server": "s1"},
    "season": {"kind": "season", "item_id": "se1", "series_id": "sh1",
               "server": "s1"},
    "music": {"kind": "music", "parent_id": "lib2", "server": "s1",
              "collection_type": "music"},
    "album": {"kind": "album", "item_id": "al1", "server": "s1"},
    "artist": {"kind": "artist", "item_id": "ar1", "server": "s1"},
    "livetv": {"kind": "livetv", "server": "s1"},
    "queue": {"kind": "queue", "server": "s1"},
    "genres": {"kind": "genres", "parent_id": "lib1", "server": "s1",
               "collection_type": "movies", "title": "Genres"},
    "favorites": {"kind": "favorites", "server": "s1", "title": "Favorites"},
    "byname": {"kind": "byname", "server": "s1", "parent_id": "lib1",
               "title": "People"},
}

#: A screen with fewer drawn labels than this is a spinner or an error state,
#: which fits every window there has ever been. The threshold is a smoke
#: alarm, not a measurement -- the thinnest real screen here draws four.
MIN_TEXTS = 4

#: One pixel. Everything here is measured on the physical scene, where each
#: box edge has been through ``px()`` once — so half a pixel of rounding on
#: the position is expected and means nothing. What this file is looking for
#: is tens of pixels.
SLOP = 1.0


def _loaded(route):
    """A browser with ``route`` loaded and its async work settled.

    Rendering before the pool drains snapshots a spinner, and a spinner fits
    every window there has ever been.
    """
    browser = MpvtkBrowser(app=None, source=WordySource())
    browser.nav_stack = [dict(route)]
    browser._load_route(browser.route)
    browser._pool.shutdown(wait=True)
    return browser


def _x_scrolled(nodes):
    """``id -> bool``: is this scroll container, or any container above it,
    scrolled horizontally? Content inside one is *meant* to run past the
    window."""
    axis = {n["id"]: n.get("axis") for n in nodes if n.get("t") == "scroll"}
    up = {n["id"]: n.get("sc") for n in nodes if n.get("t") == "scroll"}

    def walk(sc):
        seen = set()
        while sc and sc not in seen:
            seen.add(sc)
            if axis.get(sc) == "x":
                return True
            sc = up.get(sc)
        return False

    return walk


def right_limit(node, scrolls, win_w):
    """How far right this run may reach.

    The window, except inside a vertical scroll container that reserves a
    scrollbar — there the limit is the viewport's inner edge, because the
    scrollbar is drawn *over* the content and text run under it is text you
    cannot read. ``chrome.body_width`` exists to keep wrapped text inside
    exactly this line; the whole point of checking it here is that any
    caller can forget to use it, or use it against the wrong width.
    """
    sc = scrolls.get(node.get("sc"))
    if sc is None or sc.get("axis") != "y":
        return float(win_w)
    bar = scaling.px(SCROLLBAR_W) if sc.get("bar") else 0
    return min(float(win_w), float(sc["x"] + sc["w"] - bar))


def text_span(node):
    """(left, right) of the run as libass will draw it.

    ``layout`` has already ellipsized the string to the node's box, so this
    is the real ink extent and not the box's.
    """
    tw = text_width(node["text"], node["size"], node.get("bold", False))
    align = node.get("align") or "left"
    if align == "center":
        left = node["x"] + node["w"] / 2.0 - tw / 2.0
    elif align == "right":
        left = node["x"] + node["w"] - tw
    else:
        left = node["x"]
    return left, left + tw


def overflows(nodes, win_w):
    """Text runs that escape the space they are allowed."""
    scrolled = _x_scrolled(nodes)
    scrolls = {n["id"]: n for n in nodes if n.get("t") == "scroll"}
    out = []
    for node in nodes:
        if node.get("t") != "text" or not node.get("text"):
            continue
        if scrolled(node.get("sc")):
            continue
        left, right = text_span(node)
        limit = right_limit(node, scrolls, win_w)
        if left < -SLOP or right > limit + SLOP:
            out.append((node.get("id"), node["text"], round(left, 1),
                        round(right, 1), round(limit, 1)))
    return out


def _cases():
    """(scale, (w, h)) pairs worth building."""
    for scale in SCALES:
        for win in WINDOWS:
            if win[0] / scale >= MIN_LOGICAL_W:
                yield scale, win


class DpiMatrixTest(unittest.TestCase):
    """One test per screen; every (scale, window) is a subTest, so a failure
    names the exact configuration rather than "the matrix failed"."""

    def tearDown(self):
        scaling.set_scale(1.0)

    def _walk(self, name, check):
        with frozen_clock():
            for scale in SCALES:
                # BEFORE the browser exists, and one browser per scale.
                # Production resolves the scale once at startup (app.py's
                # ready event) and never moves it; a strip composited at one
                # scale and drawn at another is caught by widgets._check_raster
                # long before it is a layout question.
                scaling.set_scale(scale)
                browser = _loaded(SCREENS[name])
                for win in WINDOWS:
                    lsize = scaling.logical_size(win)
                    if lsize[0] < MIN_LOGICAL_W:
                        continue
                    with self.subTest(scale=scale, window="%dx%d" % win):
                        nodes, _h = layout(browser.build(lsize), *lsize)
                        texts = [n for n in nodes if n.get("t") == "text"]
                        self.assertGreaterEqual(
                            len(texts), MIN_TEXTS,
                            "%s drew %d labels -- a spinner or an error "
                            "state passes this file trivially"
                            % (name, len(texts)))
                        # Measured on the PHYSICAL scene, exactly as pushed
                        # to the renderer. Checking the logical one compares
                        # layout against itself and can only ever agree — it
                        # is blind to the whole class of bug where the size
                        # the text is *drawn* at is not the size it was
                        # fitted at.
                        scaling.scale_scene(nodes)
                        check(nodes, win)

    def _no_overflow(self, name):
        def check(nodes, lsize):
            bad = overflows(nodes, lsize[0])
            self.assertEqual(
                bad, [],
                "text drawn outside the space it has, on a %.0f-wide page:\n%s"
                % (lsize[0], "\n".join(
                    "  %s %r spans %s..%s, limit %s" % b for b in bad)))
        self._walk(name, check)

    def test_home(self):
        self._no_overflow("home")

    def test_grid(self):
        self._no_overflow("grid")

    def test_list(self):
        self._no_overflow("list")

    def test_search(self):
        self._no_overflow("search")

    def test_settings(self):
        self._no_overflow("settings")

    def test_detail(self):
        self._no_overflow("detail")

    def test_series(self):
        self._no_overflow("series")

    def test_season(self):
        self._no_overflow("season")

    def test_music(self):
        self._no_overflow("music")

    def test_album(self):
        self._no_overflow("album")

    def test_artist(self):
        self._no_overflow("artist")

    def test_livetv(self):
        self._no_overflow("livetv")

    def test_queue(self):
        self._no_overflow("queue")

    def test_genres(self):
        self._no_overflow("genres")

    def test_favorites(self):
        self._no_overflow("favorites")

    def test_byname(self):
        self._no_overflow("byname")

    def test_every_screen_has_a_test(self):
        """A SCREENS entry with no test above is a screen nobody checks."""
        covered = {name[len("test_"):] for name in dir(self)
                   if name.startswith("test_")}
        self.assertEqual(sorted(set(SCREENS) - covered), [])

    def test_the_matrix_is_not_empty(self):
        """A guard on the guards: a bad MIN_LOGICAL_W would silently reduce
        every test above to nothing."""
        cases = list(_cases())
        self.assertGreater(len(cases), 20)
        self.assertIn((2.0, (3840, 2160)), cases)
        self.assertIn((1.0, (1024, 576)), cases)


class OverflowCheckTest(unittest.TestCase):
    """The measurement itself.

    A matrix whose check is wrong is worse than no matrix: it is a green
    light nobody looks at again. These are synthetic scenes with the answer
    known in advance.
    """

    WIN = 1000

    def _scene(self, ends_at, axis="y", bar=True):
        """A scroll container and one text run whose ink ends exactly at
        ``ends_at``."""
        text = "overflow " * 4
        tw = text_width(text, 15, False)
        return [
            {"t": "scroll", "id": "page", "axis": axis, "x": 0, "y": 0,
             "w": self.WIN, "h": 700, "cw": self.WIN, "ch": 3000,
             **({"bar": True} if bar else {})},
            {"t": "text", "id": "t", "sc": "page", "x": ends_at - tw,
             "y": 10, "w": tw, "h": 20, "size": 15, "text": text},
        ]

    def test_text_stopping_at_the_scrollbar_is_clean(self):
        self.assertEqual(overflows(self._scene(self.WIN - SCROLLBAR_W),
                                   self.WIN), [])

    def test_text_running_under_the_scrollbar_is_caught(self):
        """Inside the window, and still unreadable: the scrollbar is drawn
        over the content. This is the check the window-edge test misses."""
        bad = overflows(self._scene(self.WIN - SCROLLBAR_W + 2), self.WIN)
        self.assertEqual(len(bad), 1, bad)
        self.assertEqual(bad[0][-1], float(self.WIN - SCROLLBAR_W))

    def test_a_container_with_no_scrollbar_gets_the_whole_width(self):
        self.assertEqual(
            overflows(self._scene(self.WIN, bar=False), self.WIN), [])

    def test_a_horizontal_scroller_is_never_flagged(self):
        """Its content is meant to run past the window — that is scrolling."""
        self.assertEqual(
            overflows(self._scene(self.WIN * 3, axis="x", bar=False),
                      self.WIN), [])

    def test_text_off_the_left_edge_is_caught_too(self):
        bad = overflows(self._scene(-5, bar=False), self.WIN)
        self.assertEqual(len(bad), 1, bad)

    def test_the_real_detail_page_is_measured_against_the_scrollbar(self):
        """Not synthetic: the screen this was reported on must actually be
        reaching the stricter limit, or the check above is inert here."""
        browser = _loaded(SCREENS["detail"])
        with frozen_clock():
            nodes, _h = layout(browser.build((1280, 720)), 1280, 720)
        scrolls = {n["id"]: n for n in nodes if n.get("t") == "scroll"}
        para = [n for n in nodes if n.get("t") == "text"
                and "conspiracy" in n.get("text", "")]
        self.assertTrue(para, "the overview did not reach the scene")
        self.assertEqual(right_limit(para[0], scrolls, 1280),
                         1280 - SCROLLBAR_W)
