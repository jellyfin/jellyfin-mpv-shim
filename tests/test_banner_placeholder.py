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

The fix is to ask the DTO instead (`has_backdrop`, which is `backdrop_spec`
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

        def spy(image, box, title=None, meta=None, context=None):
            composed.append(title)
            return real(image, box, title, meta, context)

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
            "`tiles.has_backdrop(item)` instead: %s" % offenders)

    def test_every_page_that_draws_a_backdrop_asks_the_item(self):
        missing = []
        for name in sorted(os.listdir(self.PAGES_DIR)):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(self.PAGES_DIR, name),
                       encoding="utf-8").read()
            if "backdrop_node" in src and "has_backdrop" not in src:
                missing.append(name)
        self.assertEqual(
            missing, [],
            "these pages draw a backdrop header without asking whether the "
            "item has one, so their heading placement cannot be right in "
            "both states: %s" % missing)


if __name__ == "__main__":
    unittest.main()
