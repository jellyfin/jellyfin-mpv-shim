"""The detail header must not move when its banner arrives.

`backdrop_node` bakes the heading *into* the banner bitmap, for a reason that
is not negotiable: mpv composites overlay bitmaps above all script ASS, so a
title drawn as a node would sit under the artwork it labels (mpvtk GUIDE §6).
The consequence is what this module is about. If the heading lives inside the
banner's fixed box when the artwork is there, it has to live there when the
artwork is *not* there yet — otherwise the waiting state draws the heading
somewhere else, and everything below it moves the moment the image lands.

It did. All three headers chose by asking `isinstance(banner, Box)`, which
cannot answer the question they were asking. A placeholder Box means either
"this item has no artwork" or "the artwork has not arrived", and on a first
paint nothing is cached, so *every* header with a backdrop drew its heading
below the banner and then dropped up to three text blocks when the bitmap
came back — with the play buttons under them.

The fix is to ask the DTO instead (`header_bakes_heading`, which is the specs
and needs no image), and to compose the waiting state through the same
`compose_banner` over a flat panel. That last half is what makes the title
readable while loading and, more importantly, keeps it readable when the
fetch never succeeds — `_request_image` gives up after `IMG_MAX_ATTEMPTS`,
and a header that merely *reserved* the space would be an anonymous grey
panel for the life of the page.

**This could not have been caught before**, and not because the fakes were
sloppy: `FakeSource.backdrop_spec` returned None unconditionally, so no shell
test had ever rendered a header that *has* artwork. The path was not
uncovered, it was unreachable while every header test reported a pass.
`tools/audit_fake_contracts.py` is blind to it too — `backdrop_spec` is
provided, just never honestly — which is the limit that file documents about
itself.
"""

import os
import re
import unittest

from PIL import Image as PILImage

from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser import tile_renderer
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from jellyfin_mpv_shim.mpvtk.widgets import Box, Image

from tests._shell_harness import FakeSource, FakeThumbs, _SyncPool

SIZE = (1280, 720)

#: The pages that draw a backdrop header. Each is (module, route, the node
#: whose position must not move). The button differs per screen because what
#: sits under the header differs — that is the point of checking all three
#: rather than trusting that they share a shape.
HEADERS = (
    ("detail", {"kind": "detail", "server": "srv1", "item_id": "m1"},
     "btn-play"),
    ("series", {"kind": "series", "server": "srv1", "item_id": "sr1",
                "title": "Show"}, "sa-nextup"),
)


def _art():
    """A decoded backdrop, the shape the server would hand back."""
    return PILImage.new("RGB", (640, 240), (10, 80, 160))


class _Case(unittest.TestCase):

    def browser(self, has_backdrop):
        src = FakeSource()
        src.has_backdrop = has_backdrop
        thumbs = FakeThumbs()
        b = MpvtkBrowser(app=None, source=src, thumbs=thumbs)
        # Inline, so the item is loaded before anything renders. Without it
        # a render can land on `chrome.busy()` and the assertions measure a
        # spinner — which passes some of them for entirely the wrong reason.
        b._pool = _SyncPool()
        return b, thumbs

    def nodes(self, b):
        return layout(b.build(SIZE), *SIZE)[0]

    def y_of(self, b, node_id):
        for node in self.nodes(b):
            if node.get("id") == node_id:
                return node["y"]
        return None

    def deliver(self, b, thumbs):
        """Hand back every image the last render asked for.

        Returns how many landed, so a test cannot quietly assert stability
        across a transition that never happened — which is the way this
        whole class of test passes for the wrong reason.
        """
        pending = list(thumbs.requests)
        for key, _url in pending:
            if key in thumbs._cbs:
                thumbs.resolve(key, _art())
        return len(pending)


class BannerDoesNotShiftTest(_Case):
    """The property, over the load transition rather than in one state."""

    def test_the_header_does_not_move_when_the_banner_arrives(self):
        for kind, route, button in HEADERS:
            with self.subTest(page=kind):
                b, thumbs = self.browser(has_backdrop=True)
                b.navigate(dict(route))

                before = self.y_of(b, button)
                self.assertIsNotNone(
                    before, "%s never drew %r, so this test is measuring "
                            "nothing" % (kind, button))
                self.assertTrue(
                    self.deliver(b, thumbs),
                    "%s asked for no artwork at all — the transition under "
                    "test did not happen" % kind)

                after = self.y_of(b, button)
                self.assertEqual(
                    before, after,
                    "%s moved %r by %.1fpx when its banner loaded" %
                    (kind, button, abs((after or 0) - before)))

    def test_the_heading_is_not_also_drawn_below_the_banner(self):
        """The mechanism behind the shift, asserted directly.

        A duplicated heading is what the height came from, and it is worth
        its own assertion: an equal-position test alone would also pass if
        both states drew the text.
        """
        b, _thumbs = self.browser(has_backdrop=True)
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1"})
        item = b.route["_data"]["item"]
        texts = [n.get("text") for n in self.nodes(b) if n.get("text")]
        self.assertNotIn(
            item["Name"], texts,
            "the title is drawn as a text node below the banner as well as "
            "baked into it — that block is exactly what collapses when the "
            "image lands")

    def test_a_header_with_no_artwork_still_draws_its_heading(self):
        """The other half, and the one a careless fix breaks.

        Suppressing the text whenever a banner *might* exist is not the fix;
        suppressing it when the heading is baked somewhere else is. An item
        with no artwork has no baked heading, so the text is all there is.
        """
        b, _thumbs = self.browser(has_backdrop=False)
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1"})
        item = b.route["_data"]["item"]
        texts = [n.get("text") for n in self.nodes(b) if n.get("text")]
        self.assertIn(item["Name"], texts,
                      "an item with no backdrop lost its title entirely")


class WaitingBannerTest(_Case):
    """What the placeholder *is*, which is what survives a failed fetch."""

    def test_the_waiting_banner_carries_the_heading(self):
        composed = []
        real = tile_renderer.components.compose_banner

        def spy(image, box, title=None, meta=None, context=None,
                poster=None):
            composed.append(title)
            return real(image, box, title, meta, context, poster=poster)

        tile_renderer.components.compose_banner = spy
        self.addCleanup(setattr, tile_renderer.components, "compose_banner",
                        real)

        b, _thumbs = self.browser(has_backdrop=True)
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1"})
        item = b.route["_data"]["item"]
        node = self._banner(b)

        self.assertIsInstance(
            node, Image,
            "the waiting banner is a bare Box — nothing is baked into it, so "
            "a fetch that never succeeds leaves the header anonymous")
        self.assertIn(
            item["Name"], composed,
            "the placeholder was composed without the heading")

    def test_the_placeholder_is_not_served_once_the_real_one_exists(self):
        """The cache key has to separate them.

        Both are `strips.bitmap` entries built from the same item at the same
        size. Sharing a key would mean the header renders the flat panel
        forever, because the first thing composed wins and the placeholder is
        always first.
        """
        b, thumbs = self.browser(has_backdrop=True)
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1"})
        waiting = self._banner(b)
        self.assertTrue(self.deliver(b, thumbs))
        loaded = self._banner(b)

        self.assertIsInstance(loaded, Image)
        self.assertNotEqual(
            (waiting.src, waiting.v), (loaded.src, loaded.v),
            "the loaded banner is byte-identical to the placeholder — the "
            "two share a cache key, so the artwork never appears")

    def test_an_item_with_no_artwork_gets_a_plain_placeholder(self):
        """No baked heading to match, so there is nothing to compose."""
        b, _thumbs = self.browser(has_backdrop=False)
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1"})
        self.assertIsInstance(
            self._banner(b), Box,
            "an item with no artwork composed a banner bitmap for artwork "
            "that does not exist")

    def _banner(self, b):
        """The banner node itself, off the built tree rather than the scene.

        The laid-out scene flattens an Image to a paint node and loses the
        widget type, which is the thing under test here.
        """
        page = b._page_for(b.route)
        tree = page.render(SIZE)
        found = []

        def walk(node):
            if getattr(node, "id", None) == "detail-bd":
                found.append(node)
            for child in (getattr(node, "children", None) or []):
                walk(child)
            for attr in ("child", "content"):
                inner = getattr(node, attr, None)
                if inner is not None and inner is not node:
                    walk(inner)

        walk(tree)
        self.assertTrue(found, "no banner node in the tree")
        return found[0]


class NoPageAsksTheNodeTest(unittest.TestCase):
    """A fourth header must not reintroduce the shape.

    The bug was not a typo — it was the obvious way to write it, and it was
    written three times independently. An enumeration maintained by hand
    rots, so this reads the source: any page that draws a backdrop has to
    decide from the item, and testing the returned node's type is the
    specific mistake.
    """

    PAGES_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "jellyfin_mpv_shim", "mpvtk_browser", "pages")

    def test_no_header_decides_from_the_banner_nodes_type(self):
        offenders = []
        for name in sorted(os.listdir(self.PAGES_DIR)):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(self.PAGES_DIR, name),
                       encoding="utf-8").read()
            if "backdrop_node" not in src:
                continue
            if re.search(r"isinstance\(\s*banner\s*,", src):
                offenders.append(name)
        self.assertEqual(
            offenders, [],
            "these headers choose their heading from the banner node's type, "
            "which cannot tell 'no artwork' from 'not yet' — ask "
            "`tiles.header_bakes_heading(item)` instead: %s" % offenders)

    def test_every_page_that_draws_a_backdrop_asks_the_item(self):
        missing = []
        for name in sorted(os.listdir(self.PAGES_DIR)):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(self.PAGES_DIR, name),
                       encoding="utf-8").read()
            if ("backdrop_node" in src
                    and "header_bakes_heading" not in src):
                missing.append(name)
        self.assertEqual(
            missing, [],
            "these pages draw a backdrop header without asking whether it "
            "bakes its own heading, so their heading placement cannot be "
            "right in both states: %s" % missing)


if __name__ == "__main__":
    unittest.main()


class HeaderPosterTest(unittest.TestCase):
    """The poster inset into a detail header (#7).

    It is baked into the banner bitmap rather than drawn as its own node,
    for the same reason the heading is: overlay bitmaps composite above all
    script ASS, so a second node here would be a second overlay fighting
    this one for z-order — and the heading has to sit *over* the artwork.

    That makes the arrival time the whole problem. The poster is a second
    fetch, so the composition that lands first must not be what the cache
    serves for ever.
    """

    SIZE = (1280, 720)

    def _renderer(self, poster_ready):
        """A TileRenderer whose poster fetch either has landed or has not."""
        from types import SimpleNamespace
        from PIL import Image as PILImage
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        art = PILImage.new("RGB", (40, 60), (200, 40, 40))
        composed = []

        class _Source:
            @staticmethod
            def backdrop_spec(_item):
                return ("m1", "Backdrop", "bt")

            @staticmethod
            def backdrop_url(*_a, **_k):
                return "http://srv/bd.jpg"

            @staticmethod
            def image_spec(_item, _t="Primary", _w=280, inherit=True):
                return ("m1", "Primary", "pt")

            @staticmethod
            def image_url(*_a, **_k):
                return "http://srv/po.jpg"

        class _Strips:
            @staticmethod
            def bitmap(key, image, lsize=None):
                composed.append((key, callable(image)))
                return {"src": "s", "iw": 1, "ih": 1, "lw": 1, "lh": 1,
                        "v": 0}

        r = TileRenderer.__new__(TileRenderer)
        r.art = SimpleNamespace(server="srv1", source=_Source(),
                                thumbs=None, strips=_Strips())
        r._requested, r._img_retry = set(), {}
        # The backdrop is always there; the poster is the variable.
        # Discriminated by URL, not by key: make_key returns a HASH, so a
        # stub testing the key for "Primary" never matches and every fetch
        # looks like the backdrop.
        r._request_image = lambda key, url, box: (
            art if ("po.jpg" not in url or poster_ready) else None)
        return r, composed

    def _key(self, poster_ready):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r, composed = self._renderer(poster_ready)
        box = TileRenderer.banner_box(r, self.SIZE[0])
        r.backdrop_node({"Id": "m1"}, box, "detail-bd", title="A Film")
        self.assertTrue(composed, "nothing was composed")
        return composed[-1][0]

    def test_the_poster_is_part_of_the_cache_key(self):
        """Without this the first composition — backdrop here, poster still
        loading — is what the cache serves for ever, and the poster never
        appears however many repaints follow."""
        self.assertNotEqual(self._key(poster_ready=False),
                            self._key(poster_ready=True))

    def test_its_absence_is_keyed_too(self):
        # Not "no suffix when absent": the waiting state and the finished
        # one would then collide on one key, which is the same bug.
        self.assertIn("nopo", self._key(poster_ready=False))
        self.assertNotIn("nopo", self._key(poster_ready=True))

    def _no_backdrop_renderer(self, has_poster=True):
        """A renderer for an item with NO backdrop, with or without a
        poster to fall back to."""
        from types import SimpleNamespace
        from PIL import Image as PILImage
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        art = PILImage.new("RGB", (40, 60), (200, 40, 40))
        composed = []

        class _Source:
            @staticmethod
            def backdrop_spec(_item):
                return None

            @staticmethod
            def image_spec(_item, _t="Primary", _w=280, inherit=True):
                return ("m1", "Primary", "pt") if has_poster else None

            @staticmethod
            def image_url(*_a, **_k):
                return "http://srv/po.jpg"

        class _Strips:
            @staticmethod
            def bitmap(key, image, lsize=None):
                composed.append((key, callable(image)))
                return {"src": "s", "iw": 1, "ih": 1, "lw": 1, "lh": 1,
                        "v": 0}

        r = TileRenderer.__new__(TileRenderer)
        r.art = SimpleNamespace(server="srv1", source=_Source(),
                                thumbs=None, strips=_Strips())
        r._requested, r._img_retry = set(), {}
        r._request_image = lambda key, url, box: art
        return r, composed

    def test_no_backdrop_still_shows_the_poster(self):
        """[iw]: with no backdrop the header short-circuited to a bare grey
        box, when the item has a perfectly good poster to show.

        It is NOT stretched across the banner -- backdrop_spec rejects that
        for looking like a rendering fault -- it gets the composition the
        *waiting* state already used: flat panel, heading baked in, artwork
        inset at its own aspect.
        """
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode

        r, composed = self._no_backdrop_renderer()
        box = TileRenderer.banner_box(r, self.SIZE[0])
        node = r.backdrop_node({"Id": "m1"}, box, "detail-bd", title="A Film")
        self.assertIsInstance(
            node, ImageNode,
            "a header with a poster and no backdrop drew a plain grey box")
        self.assertTrue(composed, "nothing was composed")
        self.assertTrue(composed[-1][1],
                        "the compose must be deferred to a cache miss")

    def test_no_backdrop_draws_the_heading_before_the_poster_lands(self):
        """The regression the review caught, and the one this method's own
        docstring says the pending composition exists to prevent.

        header_bakes_heading answers from the SPEC, which is known on the
        first paint; the poster is a fetch. Gating the banner on the decoded
        image meant that in between, the caller suppressed its heading and
        the banner had none — so the page drew no title at all, and if the
        fetch never succeeded, no title ever.
        """
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode

        r, composed = self._no_backdrop_renderer()
        r._request_image = lambda *a, **k: None       # never lands
        box = TileRenderer.banner_box(r, self.SIZE[0])
        node = r.backdrop_node({"Id": "m1"}, box, "detail-bd", title="A Film")
        self.assertTrue(
            TileRenderer.header_bakes_heading(r, {"Id": "m1"}),
            "test premise: the caller is suppressing its own heading")
        self.assertIsInstance(
            node, ImageNode,
            "no heading anywhere while the poster is still loading")
        self.assertIn("nopo", composed[-1][0],
                      "the waiting composition must be keyed apart, or the "
                      "cache serves the poster-less one for ever")

    def test_no_backdrop_and_no_poster_is_still_a_plain_box(self):
        # The genuinely artwork-less case keeps the placeholder, because
        # then there is no baked heading and the caller draws its own.
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode

        r, _composed = self._no_backdrop_renderer(has_poster=False)
        box = TileRenderer.banner_box(r, self.SIZE[0])
        node = r.backdrop_node({"Id": "m1"}, box, "detail-bd", title="A Film")
        self.assertNotIsInstance(node, ImageNode)

    def test_the_caller_is_told_which_of_the_two_it_got(self):
        """The heading is baked into the poster fallback, so a page that
        drew its own underneath would draw it twice -- which is exactly the
        bug header_bakes_heading exists to prevent, in its other direction.
        """
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        with_poster, _c = self._no_backdrop_renderer(has_poster=True)
        without, _c2 = self._no_backdrop_renderer(has_poster=False)
        self.assertTrue(
            TileRenderer.header_bakes_heading(with_poster, {"Id": "m1"}))
        self.assertFalse(
            TileRenderer.header_bakes_heading(without, {"Id": "m1"}))

    def test_a_poster_that_is_the_backdrop_is_not_drawn_twice(self):
        """A home video whose landscape Primary is already the banner (see
        backdrop_spec's Primary step). Inset over itself looks like a
        rendering fault rather than a feature."""
        from types import SimpleNamespace
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        class _Same:
            @staticmethod
            def backdrop_spec(_item):
                return ("v1", "Primary", "same")

            @staticmethod
            def image_spec(_item, _t="Primary", _w=280, inherit=True):
                return ("v1", "Primary", "same")

            @staticmethod
            def image_url(*_a, **_k):
                return "http://srv/x.jpg"

        r = TileRenderer.__new__(TileRenderer)
        r.art = SimpleNamespace(server="srv1", source=_Same(), thumbs=None)
        # A real image, not a sentinel: _banner_poster plates the
        # artwork now (transparent channel logos), so a stand-in
        # without pixels does not leave that untested -- it makes
        # this path raise where nothing is looking.
        r._request_image = lambda *a, **k: PILImage.new(
            "RGBA", (40, 60), (200, 40, 40, 255))
        box = TileRenderer.banner_box(r, self.SIZE[0])
        img, key = r._banner_poster({"Id": "v1"}, box, ("v1", "Primary",
                                                        "same"))
        self.assertIsNone(img)
        self.assertEqual(key, "")

    def test_the_poster_is_the_items_own_never_the_series(self):
        """An episode's banner is already the *series* backdrop, so a poster
        that inherited would draw the same series twice and the episode not
        at all. `inherit=False` is what makes the slot the episode still —
        which is the thing the user asked to see."""
        from types import SimpleNamespace
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        asked = {}

        class _Inheriting:
            @staticmethod
            def backdrop_spec(_item):
                return ("series1", "Backdrop", "sbt")

            @staticmethod
            def image_spec(_item, _t="Primary", _w=280, inherit=True):
                asked["inherit"] = inherit
                # What a real source does: with inheritance the chain walks
                # up to the series, without it it stops at the episode.
                return (("series1", "Primary", "sp") if inherit
                        else ("ep1", "Primary", "ep"))

            @staticmethod
            def image_url(*_a, **_k):
                return "http://srv/po.jpg"

        r = TileRenderer.__new__(TileRenderer)
        r.art = SimpleNamespace(server="srv1", source=_Inheriting(),
                                thumbs=None)
        # A real image, not a sentinel: _banner_poster plates the
        # artwork now (transparent channel logos), so a stand-in
        # without pixels does not leave that untested -- it makes
        # this path raise where nothing is looking.
        r._request_image = lambda *a, **k: PILImage.new(
            "RGBA", (40, 60), (200, 40, 40, 255))
        box = TileRenderer.banner_box(r, self.SIZE[0])
        _img, key = r._banner_poster({"Id": "ep1"}, box,
                                     ("series1", "Backdrop", "sbt"))
        self.assertFalse(asked["inherit"],
                         "the header poster inherited from the series")
        self.assertTrue(key)

    def test_a_still_keeps_its_own_shape(self):
        """The reported bug [iw]: "thumbnails are drawing inside a poster
        with rounded corners and black letterboxing". A 16:9 still boxed
        into a 2:3 slot reads as a poster *of* a photograph rather than as
        the frame it is. Both shapes are drawn at their own aspect now,
        fitted inside one bounding box.
        """
        from PIL import Image as PILImage
        from jellyfin_mpv_shim.mpvtk_browser.components import banner

        box = (1100, 412)
        back = PILImage.new("RGB", (1600, 600), (40, 40, 60))
        slot = banner.poster_box(box)
        self.assertIsNotNone(slot)

        def drawn_width(art):
            """Where the heading starts tells us how wide the art came out."""
            canvas = PILImage.new("RGBA", box, (0, 0, 0, 255))
            return banner._paste_poster(canvas, art, slot) - slot[0]

        poster_w = drawn_width(PILImage.new("RGB", (400, 600), (200, 150, 90)))
        still_w = drawn_width(PILImage.new("RGB", (640, 360), (90, 200, 150)))
        # A 2:3 poster is height-limited and narrow; a 16:9 still is
        # width-limited and wide. If either were letterboxed into a fixed
        # slot they would come out the same width.
        self.assertNotEqual(poster_w, still_w)
        self.assertGreater(still_w, poster_w)
        self.assertLessEqual(still_w, slot[2])

    def test_nothing_is_drawn_beyond_the_artwork(self):
        """No plate: the pixels outside the fitted artwork must still be
        the backdrop (darkened by the shadow), never a black letterbox."""
        from PIL import Image as PILImage
        from jellyfin_mpv_shim.mpvtk_browser.components import banner

        box = (1100, 412)
        slot = banner.poster_box(box)
        canvas = PILImage.new("RGBA", box, (0, 200, 0, 255))   # vivid green
        still = PILImage.new("RGB", (640, 360), (255, 0, 0))   # vivid red
        right = banner._paste_poster(canvas, still, slot)
        # Above the bottom-aligned art, inside the slot's own bounds: this
        # is where a letterbox plate would have been.
        probe = canvas.getpixel((slot[0] + 4, slot[1] + 4))
        self.assertGreater(probe[1], probe[0],
                           "the slot was filled with a plate rather than "
                           "left as backdrop (pixel %r)" % (probe,))
        self.assertLess(right - slot[0], slot[2] + 1)

    def _poster_for(self, item_type):
        from types import SimpleNamespace
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        class _Source:
            @staticmethod
            def image_spec(_item, _t="Primary", _w=280, inherit=True):
                return ("m1", "Primary", "pt")

            @staticmethod
            def image_url(*_a, **_k):
                return "http://srv/po.jpg"

        r = TileRenderer.__new__(TileRenderer)
        r.art = SimpleNamespace(server="srv1", source=_Source(), thumbs=None)
        # A real image, not a sentinel: _banner_poster plates the
        # artwork now (transparent channel logos), so a stand-in
        # without pixels does not leave that untested -- it makes
        # this path raise where nothing is looking.
        r._request_image = lambda *a, **k: PILImage.new(
            "RGBA", (40, 60), (200, 40, 40, 255))
        box = TileRenderer.banner_box(r, self.SIZE[0])
        return r._banner_poster({"Id": "m1", "Type": item_type}, box,
                                ("m1", "Backdrop", "b"))[0]

    def test_the_two_settings_are_independent(self):
        """Split because the objections are unrelated and only one is about
        taste [iw]: an episode still is a frame of something the user may
        not have watched, on the page they opened to decide whether to.
        Somebody avoiding spoilers wants that off with posters left alone —
        which one combined setting cannot express."""
        from jellyfin_mpv_shim.conf import settings

        for key in ("detail_poster", "detail_episode_image"):
            self.addCleanup(setattr, settings, key, getattr(settings, key))

        settings.detail_poster = True
        settings.detail_episode_image = False
        self.assertIsNotNone(self._poster_for("Movie"),
                             "the poster went with the episode still")
        self.assertIsNone(self._poster_for("Episode"))

        settings.detail_poster = False
        settings.detail_episode_image = True
        self.assertIsNone(self._poster_for("Movie"))
        self.assertIsNotNone(self._poster_for("Episode"),
                             "the episode still went with the poster")

    def test_both_on_is_the_default(self):
        from jellyfin_mpv_shim.conf import settings
        self.assertTrue(settings.detail_poster)
        self.assertTrue(settings.detail_episode_image)

    def test_the_banner_is_composed_only_on_a_cache_miss(self):
        """`bitmap` takes a callable and calls it only on a miss. Composing
        eagerly re-cropped the backdrop, re-drew the heading and re-blurred
        a full-canvas drop shadow on *every repaint* of a detail page, to
        hand the answer to a cache that already had it."""
        from types import SimpleNamespace
        from PIL import Image as PILImage
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        art = PILImage.new("RGB", (40, 60), (200, 40, 40))
        passed = []

        class _Strips:
            @staticmethod
            def bitmap(key, image, lsize=None):
                passed.append(image)
                return {"src": "s", "iw": 1, "ih": 1, "lw": 1, "lh": 1,
                        "v": 0}

        class _Source:
            @staticmethod
            def backdrop_spec(_item):
                return ("m1", "Backdrop", "bt")

            @staticmethod
            def backdrop_url(*_a, **_k):
                return "http://srv/bd.jpg"

            @staticmethod
            def image_spec(_item, _t="Primary", _w=280, inherit=True):
                return None

        r = TileRenderer.__new__(TileRenderer)
        r.art = SimpleNamespace(server="srv1", source=_Source(), thumbs=None,
                                strips=_Strips())
        r._request_image = lambda *a, **k: art
        box = TileRenderer.banner_box(r, self.SIZE[0])
        r.backdrop_node({"Id": "m1"}, box, "detail-bd", title="A Film")
        self.assertTrue(passed)
        self.assertTrue(callable(passed[-1]),
                        "the banner was composed before the cache was asked")


class FullBleedHeaderTest(_Case):
    """`backdrop_full_width`: the header runs to the edges of the viewport.

    Driven through the real page rather than through `banner_box` alone,
    because the box is only half of it -- the other half is that the banner
    has to come *out* of the content column's padding, and a wide box inside
    a padded column is a header that overhangs its own scrollbar.

    `has_backdrop=True` throughout. The mode is deliberately off for an item
    with no artwork at all (there is nothing to bleed, and the placeholder
    panel run edge to edge is a grey band across the page), so a fixture
    without artwork would test the padded path under a full-bleed name --
    which is the shape of green-but-worthless this file's siblings document.
    """

    def _bd(self, b):
        for node in self.nodes(b):
            if node.get("id") == "detail-bd":
                return node
        return None

    def _open(self, has_backdrop=True, full=True):
        from jellyfin_mpv_shim.conf import settings

        was = settings.backdrop_full_width
        settings.backdrop_full_width = full
        self.addCleanup(setattr, settings, "backdrop_full_width", was)
        b, thumbs = self.browser(has_backdrop=has_backdrop)
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1"})
        self.deliver(b, thumbs)
        return b

    def test_the_header_starts_at_the_left_edge(self):
        node = self._bd(self._open())
        self.assertIsNotNone(node, "no header drawn at all")
        self.assertEqual(node["x"], 0)

    def test_the_header_stops_at_the_scrollbar(self):
        """"Full width" is the scroll VIEWPORT's width. The view reserves
        SCROLLBAR_W whether or not it is scrolling, so a header taking the
        window's own width paints its last 10px underneath the bar."""
        from jellyfin_mpv_shim.mpvtk.layout import SCROLLBAR_W

        node = self._bd(self._open())
        self.assertEqual(node["x"] + node["w"], SIZE[0] - SCROLLBAR_W)

    def test_the_content_below_it_keeps_its_padding(self):
        """Only the banner leaves the column. Everything else is still inset,
        or the overview runs into the window frame."""
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        b = self._open()
        # The FIRST control of each row -- a later one is offset by whatever
        # precedes it and would agree with any padding at all.
        for node_id in ("btn-resume", "act-watched"):
            with self.subTest(node=node_id):
                hit = [n for n in self.nodes(b) if n.get("id") == node_id]
                self.assertTrue(hit, "the page never drew %s" % node_id)
                self.assertEqual(hit[0]["x"], chrome.CONTENT_PAD)

    def test_it_costs_no_vertical_space(self):
        """The constraint the option is subject to. It may spend horizontal
        space, which is free; it may not push the page down, because what is
        below the header is what the user came for.

        Measured at the first control below it, not at the header's own
        height -- the height being equal is necessary and not sufficient,
        since the padding *around* it is the other way a header takes space.
        """
        padded = self.y_of(self._open(full=False), "btn-play")
        bleed = self.y_of(self._open(full=True), "btn-play")
        self.assertIsNotNone(padded)
        self.assertLessEqual(bleed, padded)

    def test_an_item_with_no_artwork_stays_padded(self):
        """There is nothing to bleed: the header is a placeholder panel, and
        running that to both edges is a full-width empty grey band."""
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        node = self._bd(self._open(has_backdrop=False))
        self.assertIsNotNone(node)
        self.assertEqual(node["x"], chrome.CONTENT_PAD)

    def test_turning_it_off_restores_the_padded_header(self):
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        node = self._bd(self._open(full=False))
        self.assertEqual(node["x"], chrome.CONTENT_PAD)
