"""The cast screen: metadata build, backdrop+text compositing into a
full-window bitmap, and the route renderer.

This was display_mirror.DisplayMirror, a second UI that owned the mpv window
itself; it is a route on the browser now (mpvtk_browser/cast.py). The
compositing is unchanged and so are these tests — that is the point: the
screen has to look and behave exactly as it did, only hosted differently.

Runs headless (PIL only, no network — the backdrop url is omitted so the
solid-canvas path is used).
"""

import unittest

import sys

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk.rawimage import cache_dir  # noqa: E402
from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.cast import _wrap  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore  # noqa: E402

from tests.test_mpvtk_browser_shell import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


ITEM_DATA = {"title": "The Movie", "overview": "A long overview. " * 20,
             "misc": "2020    118min", "rating": "★ 8.1",
             "backdrop_url": None}


class TestCastScreen(unittest.TestCase):
    def _cast(self):
        b = MpvtkBrowser(
            app=None, source=FakeSource(),
            strips=StripStore(cache_dir=cache_dir("mpvtk-cast-test-")))
        b._pool = _SyncPool()
        b._cast_size = (800, 600)
        return b   # app is None -> invalidate() is a guarded no-op

    def test_build_item_data(self):
        d = MpvtkBrowser._build_item_data(
            {"Name": "Movie", "Overview": "o", "Type": "Movie",
             "ProductionYear": 2020}, "http://srv")
        self.assertEqual(d["title"], "Movie")
        self.assertEqual(d["overview"], "o")
        self.assertIn("2020", d["misc"])

    def test_composite_bakes_full_window_bitmap(self):
        m = self._cast()
        m._composite(ITEM_DATA, (800, 600))
        self.assertIsNotNone(m._cast_entry)
        self.assertEqual(m._cast_entry["iw"], 800)
        self.assertEqual(m._cast_entry["ih"], 600)

    def test_the_route_renders_the_baked_bitmap(self):
        m = self._cast()
        m._composite(ITEM_DATA, (800, 600))
        node = m._render_cast({"kind": "cast"}, (800, 600))
        self.assertIsInstance(node, ImageNode)
        self.assertEqual(node.w, 800)
        self.assertEqual(node.h, 600)

    def test_nothing_baked_yet_is_an_empty_scene(self):
        """Rather than a flash of whatever the previous page left up."""
        m = self._cast()
        node = m._render_cast({"kind": "cast"}, (800, 600))
        self.assertEqual(getattr(node, "children", None), [])

    def test_backdrop_is_fetched_once_per_data_change(self):
        """A window resize must re-composite from the cached image, not go
        back to the network (and, when idle, not re-roll the random
        backdrop)."""
        m = self._cast()
        calls = []

        def fake_fetch(url, timeout=10):
            calls.append(url)
            return None
        import jellyfin_mpv_shim.mpvtk_browser.cast as dm
        real, dm._fetch_image = dm._fetch_image, fake_fetch
        try:
            data = dict(ITEM_DATA, backdrop_url="http://srv/bd.jpg")
            m._cast = data
            m._composite(data, (800, 600))
            m._composite(data, (1024, 768))
            m._composite(data, (1280, 720))
            self.assertEqual(len(calls), 1)
            m._set_cast_data(dict(data, title="Other"))   # new item -> refetch
            m._composite(m._cast, (800, 600))
            self.assertEqual(len(calls), 2)
        finally:
            dm._fetch_image = real

    def test_bitmap_key_is_content_addressed(self):
        """A monotonic key was a guaranteed cache miss, so each resize tick
        retained another full-window buffer."""
        m = self._cast()
        m._composite(ITEM_DATA, (800, 600))
        first = m._cast_entry["src"]
        m._composite(ITEM_DATA, (800, 600))
        self.assertEqual(m._cast_entry["src"], first)
        self.assertEqual(len(m.strips._cache), 1)

    def test_wrap_breaks_long_text(self):
        from PIL import Image as PILImage, ImageDraw
        from jellyfin_mpv_shim.imageutil import pil_font
        draw = ImageDraw.Draw(PILImage.new("RGBA", (10, 10)))
        lines = _wrap(draw, "word " * 100, pil_font(24), 300)
        self.assertGreater(len(lines), 1)


if __name__ == "__main__":
    unittest.main()


class TestTheBackdropActuallyRenders(unittest.TestCase):
    """Every test above uses backdrop_url=None, so the entire fetch-and-
    composite-onto-an-image path was untested — and it broke: the cast
    screen showed white text on black with no picture.

    The cause was ordering, not compositing. The idle screen picks a random
    backdrop from a library item, so it needs a connected server; at startup
    it composites BEFORE the connect finishes, finds no clients, and caches
    "no backdrop". That cache is deliberate (it stops the picture re-rolling
    on every resize tick) so it never retried.
    """

    def setUp(self):
        """No test in this file may touch the network. One leaked patch
        already let a real HTTP request escape — slow, flaky, and exactly
        what a headless unit suite must not do."""
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod

        def forbidden(*a, **k):
            raise AssertionError("a cast test tried to reach the network")

        for name in ("_fetch_image", "_random_backdrop_url"):
            real = getattr(cast_mod, name)
            self.addCleanup(lambda n=name, r=real: setattr(cast_mod, n, r))
            setattr(cast_mod, name, forbidden)

    def _browser(self, fetched=None):
        from PIL import Image as PILImage
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod

        self.fetches = []

        def fake_fetch(url, timeout=10):
            self.fetches.append(url)
            return PILImage.new("RGBA", (1920, 1080), (200, 60, 60, 255))

        real, cast_mod._fetch_image = cast_mod._fetch_image, fake_fetch
        self.addCleanup(lambda: setattr(cast_mod, "_fetch_image", real))

        b = MpvtkBrowser(
            app=None, source=FakeSource(), controller=None,
            strips=StripStore(cache_dir=cache_dir("mpvtk-cast-bd-")))
        b._pool = _SyncPool()
        # Headless: the cast screen is only ever the current page there, and
        # set_source would otherwise reset a normal browser to home — which
        # is correct for it, and would make this assert nothing.
        b.headless = True
        b.server = "srv1"
        b._cast_size = (800, 600)
        return b

    def test_a_backdrop_is_composited_when_one_is_available(self):
        b = self._browser()
        b._composite(dict(ITEM_DATA, backdrop_url="http://srv/bd.jpg"),
                     (800, 600))
        self.assertEqual(self.fetches, ["http://srv/bd.jpg"])
        self.assertIsNotNone(b._cast_entry)
        self.assertIsNotNone(b._cast_backdrop,
                             "the decoded backdrop was not retained")

    def test_the_idle_screen_retries_once_a_server_is_reachable(self):
        """The reported bug: composited at startup with no clients, then
        stuck on a blank background for the rest of the session."""
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod

        b = self._browser()
        b.show_cast()

        # No servers yet — exactly the startup state.
        rolls = []
        real_roll = cast_mod._random_backdrop_url
        self.addCleanup(
            lambda: setattr(cast_mod, "_random_backdrop_url", real_roll))
        cast_mod._random_backdrop_url = lambda: (
            rolls.append(1) or (None if len(rolls) == 1
                                else "http://srv/random.jpg"))
        b._composite({"idle": True}, (800, 600))
        self.assertEqual(self.fetches, [], "fetched with no server available")
        self.assertEqual(b._cast_backdrop_key, "",
                         "the no-backdrop result was not cached")

        # The connect lands. The screen must ask again.
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(self.fetches, ["http://srv/random.jpg"],
                         "the idle backdrop never retried after connecting")

    def test_a_displayed_item_is_not_thrown_away_by_a_reconnect(self):
        """The re-roll must be gated on actually being idle, or a
        reconnect would wipe whatever a phone just cast."""
        b = self._browser()
        b.show_cast()
        b._set_cast_data(dict(ITEM_DATA, backdrop_url="http://srv/item.jpg"))
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertFalse((b._cast or {}).get("idle"),
                         "a reconnect reset the cast screen to idle")


class TestTheCastScreenLeavesRoomForTheBar(unittest.TestCase):
    """The cast page is full-bleed: unlike every other view it sizes itself
    to the window rather than flexing. So it has to account for the
    now-playing bar, and it did not — the bar was laid out BELOW the visible
    area, so casting music to a headless box showed no transport at all on
    the only page there is."""

    def _browser(self):
        b = MpvtkBrowser(
            app=None, source=FakeSource(), controller=FakeController(),
            strips=StripStore(cache_dir=cache_dir("mpvtk-cast-bar-")))
        b._pool = _SyncPool()
        b.headless = True
        b.server = "srv1"
        b.show_cast()
        return b

    def _play_audio(self, b):
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "title": "Song", "position": 1, "duration": 100})

    def test_the_transport_is_on_screen(self):
        from tests.test_mpvtk_browser_shell import build_scene
        b = self._browser()
        self._play_audio(b)
        nodes, _h = build_scene(b, size=(1280, 720))
        bar = [n for n in nodes if n.get("id") == "np-pp"]
        self.assertTrue(bar, "no transport button at all")
        node = bar[0]
        self.assertLessEqual(
            node["y"] + node["h"], 720,
            "the now-playing bar is off the bottom of the screen (y=%s)"
            % node["y"])

    def test_the_backdrop_shrinks_rather_than_overlapping(self):
        b = self._browser()
        from jellyfin_mpv_shim.mpvtk_browser.music import NOW_PLAYING_BAR_H
        self._play_audio(b)
        b._render_cast({"kind": "cast"}, (1280, 720))
        self.assertEqual(b._cast_size, (1280, 720 - NOW_PLAYING_BAR_H))

    def test_with_nothing_playing_it_uses_the_whole_window(self):
        b = self._browser()
        b._render_cast({"kind": "cast"}, (1280, 720))
        self.assertEqual(b._cast_size, (1280, 720))


class TestFetchImageItself(unittest.TestCase):
    """The one function every other cast test stubs out.

    That is exactly why a NameError in it shipped: the "backdrop path" tests
    replace `_fetch_image` wholesale, so they proved the compositing around
    it worked while the function itself could not run at all. It had lost
    `from io import BytesIO` when its imports were moved, and every test
    passed.

    So these drive the real body. Still no network — `requests.get` is
    replaced, but everything after it is the production code path.
    """

    def _response(self, fmt="PNG", size=(64, 48)):
        from io import BytesIO
        from PIL import Image as PILImage

        buf = BytesIO()
        PILImage.new("RGB", size, (10, 20, 30)).save(buf, fmt)
        payload = buf.getvalue()

        class Resp:
            status_code = 200
            content = payload

            def raise_for_status(self):
                pass

        return Resp()

    def _patch_get(self, fn):
        import requests
        real = requests.get
        self.addCleanup(lambda: setattr(requests, "get", real))
        requests.get = fn

    def test_it_decodes_a_real_response(self):
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod
        self._patch_get(lambda *a, **k: self._response())
        img = cast_mod._fetch_image("http://srv/bd.png")
        self.assertIsNotNone(img, "the backdrop did not decode")
        self.assertEqual(img.size, (64, 48))
        self.assertEqual(img.mode, "RGBA",
                         "the compositor needs an alpha channel")

    def test_a_jpeg_works_too(self):
        """Jellyfin serves JPEG backdrops; RGB->RGBA is a real conversion."""
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod
        self._patch_get(lambda *a, **k: self._response(fmt="JPEG"))
        img = cast_mod._fetch_image("http://srv/bd.jpg")
        self.assertIsNotNone(img)
        self.assertEqual(img.mode, "RGBA")

    def test_no_url_is_none_without_calling_out(self):
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod

        def boom(*a, **k):
            raise AssertionError("fetched with no url")

        self._patch_get(boom)
        self.assertIsNone(cast_mod._fetch_image(None))

    def test_a_network_error_is_none_rather_than_a_crash(self):
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod

        def boom(*a, **k):
            raise OSError("no route to host")

        self._patch_get(boom)
        self.assertIsNone(cast_mod._fetch_image("http://srv/bd.jpg"))

    def test_garbage_bytes_are_none_rather_than_a_crash(self):
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast_mod

        class Resp:
            status_code = 200
            content = b"this is not an image"

            def raise_for_status(self):
                pass

        self._patch_get(lambda *a, **k: Resp())
        self.assertIsNone(cast_mod._fetch_image("http://srv/bd.jpg"))
