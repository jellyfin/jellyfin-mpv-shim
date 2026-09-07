"""Photograph the states where two features share the window.

**Optional.** Skipped unless ``JMS_E2E_SHOTS`` names an output directory:

    JMS_E2E_SHOTS=/tmp/shots JMS_E2E_SERVER=http://127.0.0.1:8096 \\
        xvfb-run -a python3 -m unittest tests.e2e.test_composite_shots

It asserts almost nothing itself, on purpose. What a user sees is the
renderer's **bitmaps drawn over mpv's video output**, and no existing test can
see that composition: `tests/test_scene_snapshots.py` sees the scene layer and
says so, `tests/integration/test_mpv_state_restored.py` sees mpv's properties,
and a comic page is in neither — it is a VO frame. Every cross-feature visual
break in the 2026-09 sweep lived exactly there: the theme gradient and Custom
OSC's backdrop were each a correct scene node, the page underneath was a
correct VO frame, and only the composition was wrong.

**Why a shot rather than a diff.** A golden-image baseline needs one blessed
platform and a tolerance, because font stacks, DPI and the GPU (WARP on the
Windows VM, software on Xvfb) all move pixels for reasons that are not bugs.
The judgement wanted here is semantic — *"is a gradient covering the page"* —
so the oracle is a reader, human or model, given the shot and the intent.
`shots.json` carries that intent per shot so the question is answerable
without reading this file.

The list below is the risk register for feature interactions, not a sample.
Add a row when a feature starts sharing the window with another one.

**A shot only proves a composite when BOTH layers are visibly present.**
Measured on the first good run: with the backdrop correctly suppressed and the
reader's own bars not on screen, `comic-plain`, `comic-theme-wmc` and
`comic-osc-custom` came back byte-identical — the right answer, and a weak
one, because a VO-only frame cannot tell "the backdrop was correctly
suppressed" from "the renderer drew nothing at all", which is exactly the
failure this file had on its first run. `comic-with-music` differs because the
now-playing bar really is drawn over the page. Prefer states with guaranteed
chrome, and read an all-VO shot as a control rather than as evidence.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "integration"))
from test_mpvtk_browser import _spawn_handle  # noqa: E402
import test_comic_reader as _comic_mod  # noqa: E402
# the module, not `from … import ComicReaderTest`: importing the class
# puts a TestCase in this namespace and unittest then runs ITS tests here

SHOT_DIR = os.environ.get("JMS_E2E_SHOTS")
SIZE = (1280, 720)


def _page(path, w=1400, h=2100):
    """A comic-shaped page: portrait, so a landscape window letterboxes it
    and whatever is drawn over the bars is obvious."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (245, 240, 230))
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, w - 8, h - 8], outline=(20, 20, 20), width=6)
    for i in range(6):
        y = 80 + i * 330
        d.rectangle([60, y, w - 60, y + 300], outline=(20, 20, 20), width=4)
        d.text((90, y + 20), "PANEL %d" % (i + 1), fill=(20, 20, 20))
    img.save(path)
    return path


@unittest.skipUnless(SHOT_DIR, "set JMS_E2E_SHOTS=<dir> to capture")
@_e2e.require_server_and_mpv
class CompositeShotsTest(_e2e.E2ETestCase):
    """One test, many shots: the states are a list, not a suite, because the
    thing under review is the set of them side by side."""

    def setUp(self):
        super().setUp()
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        os.makedirs(SHOT_DIR, exist_ok=True)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)
        _e2e.normalise_home_layout(self.session)

        self.comic = _comic_mod.ComicReaderTest._comic(self)
        _comic_mod.ComicReaderTest._download_into_a_temp_store(
            self, self.comic)

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-shots-")))
        # **With a controller.** The comic page finds its download through
        # `ctx.player.book_download_state`; with none it never opens the
        # archive and the shot is the "Getting the comic…" placeholder.
        self.browser = MpvtkBrowser(self.app, self.source,
                                    server_uuid=_e2e.SOURCE_UUID,
                                    strips=strips,
                                    controller=PlayerGateway())
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.addCleanup(self._teardown)
        self.assertTrue(self.app.ready.wait(20), "renderer never came up")
        self.manifest = []

    def _teardown(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        try:
            theme.apply("default")
            self.pm.clear_picture()
            self.pm.reset_picture_view()
            self.pm.set_browse_window(False)
        except Exception:
            pass
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.browser.shutdown(free_bitmaps=False)
            except Exception:
                pass
            try:
                self.handle.terminate()
            except Exception:
                pass

    # -- capture -----------------------------------------------------------

    def _shoot(self, name, intent):
        """mpv's own screenshot first, the X root second.

        Same order and the same reason as `tools/shoot_browser.py:_capture`:
        with a frame on the VO mpv's is the higher-fidelity one and is the
        only one that proves the composition; with an idle player it has
        nothing to write and the root window is what a user's eye would see.
        """
        path = os.path.join(SHOT_DIR, "%s.png" % name)
        got = False
        try:
            self.app.screenshot(path)
            got = os.path.exists(path)
        except Exception:
            got = False
        if not got:
            subprocess.run(["import", "-window", "root", path],
                           check=False, timeout=20)
            got = os.path.exists(path)
        self.manifest.append({"shot": "%s.png" % name, "intent": intent,
                              "captured": got})
        return got

    def _repaint(self, settle=1.2):
        self.app.invalidate()
        time.sleep(settle)

    def _theme(self, name):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        theme.apply(name)
        theme.apply_to_toolkit()
        self.app.push_theme()
        self._repaint()

    def _show_page(self):
        """The comic ROUTE, not `show_picture` directly.

        The shortcut cost this file its first run: calling `show_picture`
        puts a page on the VO without ever setting `route["_showing"]` or
        drawing the reader, so the renderer contributed nothing and all four
        comic shots came back byte-identical. A composite needs both layers,
        which means the browser has to actually be on the screen.
        """
        self.browser.navigate({"kind": "comic", "server": _e2e.SOURCE_UUID,
                               "item_id": self.comic["Id"],
                               "title": self.comic.get("Name", "")})
        route = self.browser.route
        self.assertEqual(route.get("kind"), "comic")
        self.assertIsNone(route.get("_error"),
                          "the comic failed to open: %s" % route.get("_error"))
        deadline = time.time() + 20
        while time.time() < deadline and route.get("_comic") is None:
            time.sleep(0.25)
        self.assertIsNotNone(route.get("_comic"),
                             "the route built with no open archive, so the "
                             "shot would be the placeholder. This harness runs "
                             "the REAL pool and a live render loop, unlike "
                             "test_comic_reader, so the open is asynchronous")
        self._repaint(2.0)

    # -- the register ------------------------------------------------------

    def test_capture_the_at_risk_composites(self):
        from jellyfin_mpv_shim.conf import settings

        # 1. Browse under the themes that draw their own background. The
        #    light ones are the logo-plate risk: white artwork on near-white.
        for name in ("default", "jf-wmc", "jf-light"):
            self._theme(name)
            self._shoot("browse-%s" % name,
                        "The library, theme %s. Rows of tiles, readable "
                        "titles, artwork not lost against the background, "
                        "nothing overlapping." % name)

        # 2. A comic page, plain. The control for everything below it.
        self._theme("default")
        self._show_page()
        self._shoot("comic-plain",
                    "A comic page (portrait, panelled, cream) fills the "
                    "window height with black bars left and right. Reader "
                    "bars may sit at top and bottom. NOTHING may cover the "
                    "page itself.")

        # 3. The two features that were painting over it.
        self._theme("jf-wmc")
        self._shoot("comic-theme-wmc",
                    "The same page under the Windows Media Centre theme, "
                    "which draws a blue gradient background. The gradient "
                    "must NOT be drawn over the page — if the page is "
                    "tinted blue or replaced by a gradient, that is the bug.")
        self._theme("default")

        was = settings.osc_style
        settings.osc_style = "custom"
        self.addCleanup(setattr, settings, "osc_style", was)
        self._repaint()
        self._shoot("comic-osc-custom",
                    "The same page with Custom OSC selected, which makes the "
                    "library paint an opaque backdrop. That backdrop must "
                    "NOT cover the page.")
        settings.osc_style = was
        self._repaint()

        # 4. A page while music plays — two features sharing the window.
        tracks = None
        for album in self.session.find_all(library="Music",
                                           item_type="MusicAlbum"):
            found = self.session.find_all(item_type="Audio",
                                          parent_id=album["Id"])
            if found:
                tracks = found
                break
        if tracks:
            media = _e2e.build_media(self.session, [tracks[0]["Id"]])
            self.pm.play(media.video, is_initial_play=True)
            self._show_page()
            self._shoot("comic-with-music",
                        "A comic page with a track playing. The page must be "
                        "visible AND the now-playing bar may be drawn over "
                        "it at the bottom. A reader with bars but no page is "
                        "the bug this shot exists for.")
            self.pm.stop()
            time.sleep(0.5)

        # 5. A film started after a comic: zoom, pan and aspect residue.
        eps = self.session.episodes("The Standard Show", season=1)
        if eps:
            self.pm.set_picture_view(zoom=1.25, pan_x=0.2, pan_y=-0.3)
            self._repaint(0.6)
            self.browser._yield()
            media = _e2e.build_media(self.session, [eps[0]["Id"]])
            self.pm.play(media.video, is_initial_play=True)
            time.sleep(2.0)
            self._shoot("video-after-comic",
                        "An episode playing, started straight after a comic "
                        "page that was zoomed and panned. The picture must "
                        "be centred, unzoomed and correctly proportioned — "
                        "a cropped, off-centre or stretched frame is the bug.")
            self.pm.stop()

        with open(os.path.join(SHOT_DIR, "shots.json"), "w", encoding="utf-8") as fh:
            json.dump(self.manifest, fh, indent=2)

        missed = [m["shot"] for m in self.manifest if not m["captured"]]
        self.assertEqual(missed, [],
                         "no image was written for: %s" % ", ".join(missed))


if __name__ == "__main__":
    unittest.main()
