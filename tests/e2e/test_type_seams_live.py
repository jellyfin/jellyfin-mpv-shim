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
import test_playback_lifecycle as _pl  # noqa: E402
from test_playback_lifecycle import LONG_NAME, _PlaybackCase  # noqa: E402

COMIC = "A Test Comic 001"
EPUB = "The Standard Reference"


class _ChainCase(_PlaybackCase):

    def setUp(self):
        super().setUp()
        # The library's background as this app paints it: the theme's, so
        # read at launch rather than spelled here.
        self.assertTrue(_e2e.wait_for(
            lambda: self.p("idle-active") is True
            and self.p("background-color"), timeout=15))
        self.browse_bg = self.p("background-color")

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
        want = {"idle-active": True, "keepaspect": False, "video-zoom": 0.0,
                "background-color": self.browse_bg, "loop-file": False}

        def now():
            return {k: self.p(k) for k in want}
        # Together and eventually: the stop, then the window's repaint, are
        # separate steps, and reading one the instant the other lands is
        # the test racing the app, not a finding.
        _e2e.wait_for(lambda: now() == want, timeout=15)
        self.assertEqual(want, now(), hop)

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
        # press_until: just after a start, jsonipc's HUD mode goes off and
        # on again (the handoff reset) and an ENTER in that gap is lost --
        # a finding in the register, 2026-09-27; a person presses again.
        self.app.press_until("ENTER", lambda f: _app.shown(f, "hud-back"),
                             what="the HUD", retry_after=3)
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
        self.assertEqual(self.browse_bg, self.p("background-color"),
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
        self.assertNotEqual(self.browse_bg, self.p("background-color"),
                            "film: the library's grey letterbox")
        self.assert_not_stretched("film")
        self.leave_by_hud_back()
        self.assert_library("after the film")

    def into_photo_album(self):
        """Home -> Photos -> the "Image Formats" album; returns the album
        id and the ids of the photos in it."""
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
        return album, photos

    def hop_photo(self):
        album, photos = self.into_photo_album()

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


class _PlaylistCase(_ChainCase):
    open_playlist = _pl.StepsAndMarksTest.open_playlist

    def on(self, item_id):
        self.assertTrue(_e2e.wait_for(
            lambda: item_id in (self.p("path") or ""), timeout=30),
            "never reached %s (path %r)" % (item_id, self.p("path")))


class AMixedPlaylistTwiceRoundTest(_PlaylistCase):
    """Row 24: video, song, video, song, and round again. Each video owns
    the window and its HUD comes back on the pointer; each song leaves the
    library up with its bar (the playlist that blanked the library,
    CL0907:20-22)."""

    before_login_films = (LONG_NAME, _pl.SECOND_LONG_NAME)

    def before_login(self):
        films = [self.movie(n) for n in self.before_login_films]
        songs = []
        for a in self.session.find_all(library="Music",
                                       item_type="MusicAlbum"):
            got = self.session.find_all(item_type="Audio", parent_id=a["Id"])
            if len(got) >= 2:
                songs = [s["Id"] for s in got[:2]]
                break
        self.assertEqual(2, len(songs), "no album with two tracks")
        self.fresh(*films)
        self.order = [(films[0], "video"), (songs[0], "audio"),
                      (films[1], "video"), (songs[1], "audio")]
        made = self.session._request("/Playlists", method="POST", body={
            "Name": "jms-e2e-mixed", "Ids": [i for i, _ in self.order],
            "UserId": self.session.user_id})
        self.playlist = made["Id"]
        self.addCleanup(self.session._request, "/Items/%s" % self.playlist,
                        "DELETE")

    def test_video_song_video_song_twice(self):
        self.open_playlist()
        for round_ in (1, 2):
            self.app.move_to("pl-play")
            self.app.key("ENTER")
            for n, (item_id, kind) in enumerate(self.order):
                hop = "round %d, item %d (%s)" % (round_, n + 1, kind)
                self.on(item_id)
                if kind == "video":
                    self.app.wait_for(lambda f: f.get("phud_mode"),
                                      timeout=15, what="%s: HUD mode" % hop)
                    self.app.summon_hud()
                    self.app.key(">")
                else:
                    f = self.app.wait_for(
                        lambda f: _app.shown(f, "np-next")
                        and _app.shown(f, "nav-settings")
                        and not f.get("phud_mode"),
                        timeout=15, what="%s: the library + bar" % hop)
                    self.assertEqual(self.browse_bg, self.p("background-color"),
                                     hop)
                    # Next from the last entry does not end the queue, so
                    # the round ends with Stop.
                    last = n == len(self.order) - 1
                    self.app.move_to("np-stop" if last else "np-next")
                    self.app.key("ENTER")
            self.assert_library("end of round %d" % round_)
            self.app.wait_for(lambda f: _app.shown(f, "pl-play"),
                              timeout=15, what="the playlist page again")
        self.assertEqual(0, self.app.quit(timeout=30))


class AMusicPlaylistFromItsTileTest(_PlaylistCase):
    """Row 24 / audit A2:67: a playlist of songs, launched from its tile,
    launches as audio -- the library stays, with its bar. What decides it
    in production is the playlist's FIRST entry (`launches_as_audio(item,
    first=items[0])`), which the LYING unit-level test never passed."""

    def before_login(self):
        songs = []
        for a in self.session.find_all(library="Music",
                                       item_type="MusicAlbum"):
            got = self.session.find_all(item_type="Audio", parent_id=a["Id"])
            if len(got) >= 2:
                songs = [s["Id"] for s in got[:2]]
                break
        self.assertEqual(2, len(songs), "no album with two tracks")
        self.order = [(songs[0], "audio"), (songs[1], "audio")]
        made = self.session._request("/Playlists", method="POST", body={
            "Name": "jms-e2e-songs", "Ids": songs,
            "UserId": self.session.user_id, "MediaType": "Audio"})
        self.playlist = made["Id"]
        self.addCleanup(self.session._request, "/Items/%s" % self.playlist,
                        "DELETE")

    def test_it_launches_as_music(self):
        self.open_playlist()
        self.app.move_to("pl-play")
        self.app.key("ENTER")
        self.on(self.order[0][0])
        f = self.app.wait_for(lambda f: _app.shown(f, "np-stop"),
                              timeout=15, what="the now-playing bar")
        self.assertFalse(f.get("phud_mode"), "a song playlist yielded")
        self.assertTrue(_app.shown(f, "nav-settings"))
        self.assertEqual(self.browse_bg, self.p("background-color"))
        self.assertEqual(0, self.app.quit(timeout=30))


class AnAudiobookKeepsTheLibraryTest(_ChainCase):
    """Row 21: launched by keys, an audiobook never takes the window (no
    frame yields to video) and resumes where it stopped, a book's length
    in (about 12 minutes; the server keeps an AudioBook's place in
    minutes)."""

    BOOK = "The Overnight Vigil"          # 24 minutes
    AT = 720

    def open_book(self):
        _flows.open_by_search(self.app, self.BOOK, self.book,
                              section="Audiobooks", landed="ab-play")

    def test_launch_keeps_the_library_and_resume_lands_in_place(self):
        self.book = self.named(self.BOOK, library="Books")
        self.fresh(self.book)
        self.open_book()
        self.app.move_to("ab-play")
        self.app.key("ENTER")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            f = self.app.frame()
            self.assertFalse(f.get("phud_mode"), "an audiobook yielded")
            self.assertTrue(_app.shown(f, "nav-settings"),
                            "the library left during an audiobook launch")
            time.sleep(0.1)
        self.app.playing_path()
        # Setup, not the claim: get to 12 minutes without 24 presses.
        self.app.mpv.command("seek", str(self.AT), "absolute")
        self.assertTrue(_e2e.wait_for(
            lambda: (self.p("time-pos") or 0) >= self.AT - 2, timeout=15))
        self.app.move_to("np-stop")
        self.app.key("ENTER")
        self.assert_library("after stopping the book")
        held = _e2e.wait_for(lambda: (self.session.user_data(self.book) or {})
                             .get("PlaybackPositionTicks"), timeout=20)
        self.assertTrue(held, "no position kept for the book")
        # With a position kept, the page offers Resume and Play becomes
        # Restart (books.py) -- on a page loaded AFTER the stop: the one
        # left open is not refreshed by playback (the pending USERDATA_KINDS
        # decision, register 2026-09-27), so open it again.
        self.open_book()
        self.app.wait_for(lambda f: _app.shown(f, "ab-resume"), timeout=15,
                          what="Resume on the book's page")
        self.app.move_to("ab-resume")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(
            lambda: (self.p("time-pos") or 0) > 5, timeout=30),
            "the book did not play again")
        self.assertLess(abs(self.p("time-pos") - self.AT), 75,
                        "resumed at %.0f s, stopped at %d s"
                        % (self.p("time-pos"), self.AT))
        self.assertEqual(0, self.app.quit(timeout=30))


class _MusicUp(_ChainCase):
    def play_album(self):
        album = self.album()
        self.tracks = [s["Id"] for s in self.session.find_all(
            item_type="Audio", parent_id=album["Id"])]
        _flows.open_by_search(self.app, album["Name"], album["Id"],
                              section="Albums", landed="album-play")
        self.app.move_to("album-play")
        self.app.key("ENTER")
        self.app.playing_path()
        self.app.wait_for(lambda f: _app.shown(f, "np-stop"), timeout=30,
                          what="the now-playing bar")

    def press(self, node_id):
        self.app.move_to(node_id)
        self.app.key("ENTER")

    def track(self):
        path = self.p("path") or ""
        return next((i for i in self.tracks if i in path), None)


class APhotoSlideshowRunsTest(_ChainCase):
    """Row 19, Play All: from a photo album the slideshow advances by
    itself (a single photo opened from its tile is held -- the chain)."""

    def test_play_all_moves_on(self):
        album, photos = self.into_photo_album()
        self.app.press_until("ENTER", lambda f: _app.shown(f, "grid-playall"),
                             what="the album's page")
        self.app.move_to("grid-playall")
        self.app.key("ENTER")
        seen = [self.app.playing_path()]
        # Two advances by themselves. (pause blinks true for a moment at
        # each change, so it is not the witness.)
        for _ in range(2):
            self.assertTrue(_e2e.wait_for(
                lambda: self.p("path") not in (None,) + tuple(seen),
                timeout=15), "the slideshow stopped at %d photos" % len(seen))
            seen.append(self.p("path"))
        self.assertEqual(0, self.app.quit(timeout=30))


class TheThemeLetsThePictureThroughTest(_ChainCase):
    """Row 26 (PM 9fd940bb): under jf-wmc, whose window gradient is a
    bitmap -- and bitmaps composite ABOVE mpv's video output -- a comic
    page and a film show untinted. Measured as mpv's bare picture
    (screenshot "video") against what the window shows ("window") over the
    middle of the picture: equal, give or take scaling."""

    CONF = {"theme": "jf-wmc"}

    def tint(self, name):
        from PIL import Image, ImageChops, ImageStat
        base = os.path.join(self.app.config_dir, name)
        self.app.mpv.command("screenshot-to-file", base + "-v.png", "video")
        self.app.mpv.command("screenshot-to-file", base + "-w.png", "window")
        d = self.p("osd-dimensions")
        box = (d["ml"], d["mt"], d["w"] - d["mr"], d["h"] - d["mb"])
        bw, bh = box[2] - box[0], box[3] - box[1]
        win = Image.open(base + "-w.png").convert("RGB").crop(box)
        vid = Image.open(base + "-v.png").convert("RGB").resize((bw, bh))
        mid = (bw // 4, bh // 4, 3 * bw // 4, 3 * bh // 4)
        diff = ImageChops.difference(win.crop(mid), vid.crop(mid))
        return max(ImageStat.Stat(diff).mean)

    def test_a_comic_and_a_film_are_not_tinted(self):
        comic = self.named(COMIC, library="Books")
        self.fresh(comic)
        _flows.open_by_search(self.app, COMIC, comic, section="Books",
                              landed="bk-read")
        self.app.move_to("bk-read")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "cm-page"), timeout=30,
                          what="the comic reader")
        self.app.playing_path()
        time.sleep(1.5)
        self.assertLess(self.tint("comic"), 12,
                        "the theme's gradient is drawn over the comic page")
        self.app.key("ESC")
        self.assert_library("after the comic")
        film = self.movie(LONG_NAME)
        self.fresh(film)
        _flows.open_by_search(self.app, LONG_NAME, film)
        self.play()
        self.assertTrue(_e2e.wait_for(lambda: (self.p("time-pos") or 0) > 3,
                                      timeout=30))
        self.app.mpv.command("set", "pause", "yes")   # a still frame to shoot
        time.sleep(1)
        self.assertLess(self.tint("film"), 12,
                        "the theme's gradient is drawn over the film")
        self.assertEqual(0, self.app.quit(timeout=30))


class TheQueueScreenTest(_MusicUp):
    """The queue screen on a REAL queue (the route walk's could only ever
    be empty, audit A2:145): opened from the bar during an album, it lists
    every track, and a row's play button sends playback there."""

    def test_it_lists_the_queue_and_a_row_plays(self):
        self.play_album()
        self.press("np-queue")
        n = len(self.tracks)
        self.app.wait_for(lambda f: all(_app.node(f, "q-%d" % i)
                                        for i in range(n)), timeout=15,
                          what="all %d tracks on the queue screen" % n)
        self.assertIsNone(_app.node(self.app.frame(), "q-%d" % n),
                          "the queue lists more than the album")
        # A queue row SELECTS (it is a multi-select list); its play button
        # is the one-press jump (tile_renderer.track_list).
        self.press("q-play-1")
        self.assertTrue(_e2e.wait_for(lambda: self.track() == self.tracks[1],
                                      timeout=15),
                        "the second row's play button did not play the "
                        "second track")
        self.assertEqual(0, self.app.quit(timeout=30))


class TheReaderMakesRoomForTheBarTest(_MusicUp):
    """Row 20: with music playing, the epub's page area ends above the
    now-playing bar, and when the music stops it grows back (the
    measurement that had never once succeeded, CL0907-fix section 2)."""

    def area_bottom(self):
        n = _app.node(self.app.frame(), "rd-area")
        return n["y"] + n["h"]

    def test_the_page_shrinks_for_the_bar_and_grows_back(self):
        self.play_album()
        epub = self.named(EPUB, library="Books")
        self.fresh(epub)
        _flows.open_by_search(self.app, EPUB, epub, section="Books",
                              landed="bk-read")
        self.press("bk-read")
        f = self.app.wait_for(lambda f: _app.shown(f, "rd-area")
                              and _app.shown(f, "np-stop"), timeout=30,
                              what="the reader with the bar")
        bar_top = min(n["y"] for n in f["nodes"]
                      if (n.get("id") or "").startswith("np-")
                      and n.get("vis"))
        with_bar = self.area_bottom()
        self.assertLessEqual(with_bar, bar_top + 1,
                             "the page runs under the now-playing bar")
        self.press("np-stop")
        self.app.wait_for(lambda f: not _app.shown(f, "np-stop")
                          and _app.shown(f, "rd-area"), timeout=15,
                          what="the bar gone, the reader still up")
        self.app.wait_for(lambda f: self.area_bottom() > with_bar,
                          timeout=10, what="the page to grow back")
        self.assertEqual(0, self.app.quit(timeout=30))


class MusicTransportAndRepeatTest(_MusicUp):
    """Row 25: the bar's buttons, pressed. Next and previous move the
    track, play/pause pauses, repeat cycles none -> all -> one where one is
    mpv looping the file -- and repeat is music-only: a film never loops.
    The favourite reaches the server."""

    def test_the_bar_drives_the_music(self):
        self.play_album()
        first = self.track()
        self.assertIsNotNone(first)
        self.press("np-next")
        self.assertTrue(_e2e.wait_for(lambda: self.track() not in
                                      (None, first), timeout=15),
                        "np-next did not move the track")
        self.press("np-prev")
        self.assertTrue(_e2e.wait_for(lambda: self.track() == first,
                                      timeout=15), "np-prev did not go back")
        self.press("np-pp")
        self.assertTrue(_e2e.wait_for(lambda: self.p("pause") is True,
                                      timeout=5), "np-pp did not pause")
        self.press("np-pp")
        seen = []
        for _ in range(3):
            self.press("np-repeat")
            time.sleep(0.8)
            seen.append(self.p("loop-file"))
        self.assertEqual(1, sum(1 for s in seen if s in ("inf", True)),
                         "repeat one is not a looping file, once per cycle "
                         "(loop-file over three presses: %r)" % (seen,))
        # Leave it on "one", then a film: it must not loop.
        while self.p("loop-file") not in ("inf", True):
            self.press("np-repeat")
            time.sleep(0.8)
        self.assertIsNone(self.session.user_data(first).get("IsFavorite")
                          or None)
        self.addCleanup(self.session._request,
                        "/UserFavoriteItems/%s" % first, "DELETE")
        self.press("np-fav")
        self.assertTrue(_e2e.wait_for(
            lambda: (self.session.user_data(self.track()) or {})
            .get("IsFavorite"), timeout=15), "the favourite never reached "
                                             "the server")
        film = self.movie(LONG_NAME)
        self.fresh(film)
        _flows.open_by_search(self.app, LONG_NAME, film)
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        # The FILM's path: the song's is still there when the key goes in.
        self.assertTrue(_e2e.wait_for(
            lambda: film in (self.p("path") or "")
            and (self.p("time-pos") or 0) > 1, timeout=30),
            "the film never started")
        self.assertIn(self.p("loop-file"), (False, "no"),
                      "a film loops because music's repeat-one was on")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
