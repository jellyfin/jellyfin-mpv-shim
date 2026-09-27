"""Slice S3, `type_seams_live`: one session across every media type.

Critical-path inventory rows 22 and 23 (19-26 are this module's). mpv is not
re-created between items (CLAUDE.md), so a global one type leaves behind
only shows up at the hop AFTER the next -- which pairwise tests cannot see
(`test_type_seams` covers pairs, and its music half is LYING, audit A2:224).
Here one scripted chain goes library -> music -> comic -> film -> photo ->
book by keys, and every hop asserts every type's invariants, then the whole
chain again after a relaunch.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
from test_playback_lifecycle import LONG_NAME, _PlaybackCase  # noqa: E402

COMIC = "A Test Comic 001"
EPUB = "The Standard Reference"
BROWSE_BG = "#FF141414"


class _ChainCase(_PlaybackCase):

    def named(self, name, **kw):
        found = [i for i in self.session.find_all(**kw)
                 if i.get("Name") == name]
        self.assertTrue(found, "no %r on this server" % name)
        return found[0]["Id"]

    def album(self):
        for a in self.session.find_all(library="Music",
                                       item_type="MusicAlbum"):
            if len(self.session.find_all(item_type="Audio",
                                         parent_id=a["Id"])) >= 2:
                return a
        self.skipTest("no album with two tracks on this server")

    def photos_view(self):
        views = self.session._request("/Users/%s/Views"
                                      % self.session.user_id)["Items"]
        # A "homevideos" view like several others: by name.
        got = [v["Id"] for v in views if v.get("Name") == "Photos"]
        if not got:
            self.skipTest("no photo library on this server")
        return got[0]

    # -- what each state owes, asserted at every hop ----------------------

    def p(self, name):
        return self.app.prop(name)

    def assert_library(self, hop):
        """Nothing playing, and the window is the library's: free aspect,
        no zoom, the UI's background."""
        self.assertTrue(_e2e.wait_for(lambda: self.p("idle-active") is True,
                                      timeout=15), "%s: still playing" % hop)
        self.assertEqual(False, self.p("keepaspect"), hop)
        self.assertEqual(0.0, self.p("video-zoom"), hop)
        self.assertEqual(BROWSE_BG, self.p("background-color"), hop)
        self.assertEqual(False, self.p("loop-file"), hop)

    def assert_not_stretched(self, hop):
        """The drawn picture has the video's shape. mpv's own geometry: the
        OSD box minus the margins it letterboxed with."""
        def ratio():
            d = self.p("osd-dimensions") or {}
            vp = self.p("video-params") or {}
            w = d.get("w", 0) - d.get("ml", 0) - d.get("mr", 0)
            h = d.get("h", 0) - d.get("mt", 0) - d.get("mb", 0)
            if not (w > 0 and h > 0 and vp.get("aspect")):
                return None
            return (w / h) / vp["aspect"]
        self.assertTrue(_e2e.wait_for(lambda: ratio() is not None,
                                      timeout=10), "%s: no geometry" % hop)
        self.assertAlmostEqual(1.0, ratio(), delta=0.02,
                               msg="%s: picture stretched" % hop)

    def leave_by_hud_back(self):
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "hud-back"), timeout=15,
                          what="the HUD")
        self.app.move_to("hud-back")
        self.app.key("ENTER")

    # -- the hops ----------------------------------------------------------

    def hop_music(self):
        album = self.album()
        _flows.open_by_search(self.app, album["Name"], album["Id"],
                              section="Albums", landed="album-play")
        self.app.move_to("album-play")
        self.app.key("ENTER")
        self.app.playing_path()
        f = self.app.wait_for(lambda f: _app.shown(f, "np-stop"), timeout=30,
                              what="the now-playing bar")
        self.assertTrue(_app.shown(f, "nav-settings") and not
                        f.get("phud_mode"),
                        "music took the library away (the #C seam)")
        self.assertEqual(BROWSE_BG, self.p("background-color"),
                         "music painted a video background")
        self.app.move_to("np-stop")
        self.app.key("ENTER")
        self.assert_library("after music")

    def hop_comic(self):
        comic = self.named(COMIC, library="Books")
        # From page one: reading stores a position, and a comic reopened on
        # its last page has nothing for RIGHT to turn to.
        self.fresh(comic)
        _flows.open_by_search(self.app, COMIC, comic, section="Books",
                              landed="bk-read")
        self.app.move_to("bk-read")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "cm-page"), timeout=30,
                          what="the comic reader")
        first = self.app.playing_path()
        self.assertEqual(True, self.p("keepaspect"), "a page is stretched")
        for _ in range(3):
            was = self.p("path")
            self.app.key("RIGHT")
            self.assertTrue(_e2e.wait_for(
                lambda: self.p("path") not in (None, was), timeout=10),
                "RIGHT did not turn the page (row 22, A1:98)")
        self.assertNotEqual(first, self.p("path"))
        self.app.key("ESC")
        self.app.wait_for(lambda f: _app.shown(f, "bk-read"), timeout=15,
                          what="the comic's page after ESC")
        self.assert_library("after the comic")

    def hop_film(self):
        film = self.movie(LONG_NAME)
        self.fresh(film)
        _flows.open_by_search(self.app, LONG_NAME, film)
        self.play()
        self.assertTrue(_e2e.wait_for(lambda: (self.p("time-pos") or 0) > 1,
                                      timeout=30), "the film never played")
        self.assertEqual(True, self.p("keepaspect"), "film: keepaspect off")
        self.assertEqual(0.0, self.p("video-zoom"),
                         "film: zoomed (a comic's fit leaked, R7/F15)")
        self.assertEqual(0.0, self.p("panscan"))
        self.assertNotEqual(BROWSE_BG, self.p("background-color"),
                            "film: the library's grey letterbox")
        self.assert_not_stretched("film")
        self.leave_by_hud_back()
        self.assert_library("after the film")

    def hop_photo(self):
        view = self.photos_view()
        self.app.move_to("nav-home")
        tile = "row-libs-" + view
        self.app.press_until("ENTER", lambda f: _app.node(f, tile),
                             what="Home")
        f = self.app.frame()
        first = next(n["id"] for n in f["nodes"]
                     if (n.get("id") or "").startswith("row-libs-"))
        self.app.move_to(first)
        self.app.move_to(tile, key="RIGHT")
        # The grid's top level is the photo albums; one of them, then a
        # tile that IS a photo inside it.
        album = next(i["Id"] for i in self.session.find_all(parent_id=view)
                     if i.get("Name") == "Image Formats")
        album_tile = "grid-0-" + album
        self.app.press_until("ENTER", lambda f: _app.node(f, album_tile),
                             what="the photo library")
        self.app.move_to(album_tile)
        photos = {i["Id"] for i in self.session.find_all(
            parent_id=album, item_type="Photo")}

        def a_photo(f):
            return next((n["id"] for n in f.get("nodes", [])
                         if (n.get("id") or "").startswith("grid-")
                         and n["id"].rsplit("-", 1)[-1] in photos), None)
        f = self.app.press_until("ENTER", a_photo, what="a photo's tile")
        photo = a_photo(f)
        self.app.move_to(photo)
        self.app.key("ENTER")
        shown = self.app.playing_path()
        # Held by PAUSING (pause_stills, "show me this picture"); the
        # display duration stays the slideshow's, for Play All.
        self.assertTrue(_e2e.wait_for(lambda: self.p("pause") is True,
                                      timeout=10),
                        "a photo opened from its tile is not held (row 19)")
        time.sleep(3)
        self.assertEqual(shown, self.p("path"), "the photo moved on by itself")
        self.leave_by_hud_back()
        self.assert_library("after the photo")

    def hop_book(self):
        epub = self.named(EPUB, library="Books")
        self.fresh(epub)
        _flows.open_by_search(self.app, EPUB, epub, section="Books",
                              landed="bk-read")
        self.app.move_to("bk-read")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "rd-area"), timeout=30,
                          what="the epub reader")
        self.assertTrue(self.p("idle-active"), "a book set mpv playing")
        self.app.key("ESC")
        self.app.wait_for(lambda f: _app.shown(f, "bk-read"), timeout=15,
                          what="the book's page after ESC")
        self.assert_library("after the book")

    def chain(self):
        self.assert_library("at launch")
        self.hop_music()
        self.hop_comic()
        self.hop_film()
        self.hop_photo()
        self.hop_book()


class OneSessionAcrossEveryTypeTest(_ChainCase):
    """Row 23, with row 22's film-after-comic and page turn inside it."""

    def test_the_chain_twice_with_a_relaunch_between(self):
        self.chain()
        self.app = _flows.relaunch(self.app, self.relay)
        self.app.wait_for(lambda f: _app.shown(f, "nav-settings"),
                          timeout=60, what="Home after the relaunch")
        self.chain()
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
