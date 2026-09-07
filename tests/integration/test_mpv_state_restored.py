"""Play media, do things to it, and put mpv back the way it came in.

**One test shape for a whole class of bug.** mpv is not re-created between
items or between UI modes, so every feature that writes a property is
borrowing something the next feature will inherit. This session found six of
those one at a time -- `image_display_duration`, `keepaspect`, `video-zoom`,
`loop-file`, `background-color`, the geometry option -- each caught by a test
written after somebody noticed the symptom. That does not scale, and it
cannot catch the seventh.

So this asserts the *property* instead of the instances: **after a feature
has had its turn and the library is back, mpv looks the way it did before.**
A new leak fails here without anybody predicting it, which is the whole
point. `ALLOWED` below is the executable form of the "deliberately global"
list -- every entry is a decision with a reason attached, and adding one is
how you say a difference is intended rather than accidental.

**What it cannot see.** It compares PROPERTIES, so a feature that leaves a
*file* loaded rather than a property set is outside its reach -- measured, a
`clear_picture` that returns without taking the page down passes here,
because `on_browse_enter` re-asserts the same window properties on the way
in. `tests/e2e/test_comic_reader.py:_loaded_path` is what covers that half.
Nor does it see anything written outside a property: an input section, an
overlay slot, the stats OSD (`_stats_shown` is a Python flag with no mpv
property behind it -- see `test_picture_options.py`).

Against the FakeMPV rather than a real one: what is under test is what the
SHIM writes, and the fake records exactly that. A real mpv also changes
properties on its own (a VO reconfig, a codec's answer), which would make
the comparison noisy about things nobody wrote.
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import _harness as h  # noqa: E402

player_module = h.import_player_with_fake_mpv()

from test_picture_options import _Base  # noqa: E402


#: Properties that legitimately differ after a round trip, each with the
#: reason. **This is the list to read before "fixing" a diff**: an entry here
#: is a decision somebody made, and several of them are fixes for bugs that
#: a naive restore would re-create.
ALLOWED = {
    # Ratified: a viewer who slows an episode down means the next episode
    # too, so the queue advancing must not fight the choice [iw]. Pinned by
    # tests/e2e/test_type_seams.py.
    "speed": "per session by design, not per item",
    # The title of what is playing, cleared on the way back to the library
    # by `clear_media_title` -- but only once something HAS played, so it is
    # not equal to its pristine value mid-scenario.
    "force_media_title": "cleared by clear_media_title, asserted separately",
    # Written for every item from the user's settings; identical across a
    # round trip only by coincidence of the defaults.
    "deinterlace": "written per item from the setting",
    "hwdec": "written per item, and pinned by the shader-pack tests",
    # The queue's end-of-file behaviour, owned by `upd_player_hide`.
    "keep_open": "owned by the queue, not by the window",
    # Bookkeeping the fake exposes that mpv would not.
    "playback_abort": "the fake's idle flag",
    # A still opened on its own is HELD, deliberately -- "clicking one
    # picture means show me this" -- and `_play_media` unpauses for the next
    # item that is not a still. Pinned by
    # tests/e2e/test_photos.py:test_one_photo_opened_on_its_own_is_held.
    "pause": "a still is held on purpose; the next item unpauses",
    # Reported BY mpv rather than written by the shim: the fake's
    # `fire_property` answers the duration wait, and nothing clears it.
    "duration": "mpv reports it; the shim never writes it",
    # `_sync_window_geometry` arms the option at the window's live size, on
    # purpose and permanently: clearing it is what made the window jump.
    # See tests/test_window_geometry.py.
    "geometry": "armed at the live size deliberately, and kept",
}


class RestoresMpvStateTest(_Base):
    """Each scenario: snapshot, do the thing, come back, compare."""

    def _baseline(self):
        """Snapshot **the library**, not a pristine player.

        The round trip is library -> feature -> library, and arriving at the
        library legitimately paints the browse window (`force_window`, the
        background, the endless still). Comparing against a never-used player
        would report all of that as drift and drown the leak this is for --
        measured, it did.
        """
        self._return_to_the_library()
        return self._snapshot()

    def _snapshot(self):
        """Every public property the fake is holding.

        Read off `__dict__` rather than a hand-listed set, deliberately: a
        property nobody thought to list is exactly the one a new feature
        will leak, and a hand-written list can only ever catch what its
        author already suspected.
        """
        return {k: v for k, v in self.pm._player.__dict__.items()
                if not k.startswith("_") and not callable(v)
                and k not in ("init_options",)}

    def _return_to_the_library(self):
        """Everything the browser does on its way back in, and **nothing the
        scenario does for it**.

        The picture teardown is here rather than in `_show_a_page` on
        purpose. A scenario that cleaned up after itself would be supplying
        the very calls whose absence is the bug -- the mistake this file is
        for. So the two halves are split the way the app splits them: a
        scenario does the FEATURE, this does the RETURN, and it calls what
        `pages/comic.py` and `_release_page_grabs` call, through the gateway
        rather than the player, so a gateway that stopped forwarding is
        caught too.
        """
        from jellyfin_mpv_shim.mpvtk_browser.gateway.picture import (
            PictureMixin)
        from jellyfin_mpv_shim.mpvtk_browser.gateway.playback import (
            PlaybackMixin)

        with mock.patch("jellyfin_mpv_shim.player.playerManager", self.pm):
            picture = PictureMixin()
            picture._act = lambda fn: fn(self.pm)
            picture.clear_picture()
            picture.reset_picture_view()
            PlaybackMixin().on_browse_enter()

    def _assert_restored(self, before, what):
        after = self._snapshot()
        drift = {k: (before.get(k, "<absent>"), after[k])
                 for k in after
                 if k not in ALLOWED and after[k] != before.get(k, "<absent>")}
        self.assertEqual(
            drift, {},
            "%s left mpv changed after the library came back. Each entry is "
            "property: (before, after). Either the feature should put it "
            "back, or it belongs in ALLOWED with the reason why not." % what)

    # -- the scenarios -----------------------------------------------------

    def _play_item(self, media_type="Video", is_photo=False):
        """`_Base.play()` with the item TYPE varied, because most of what
        leaks is written on a branch keyed to it -- the photo duration, the
        audio loop, the audio volume bucket.

        Its own copy of the duration-answering dance rather than a parameter
        on the shared helper: that helper is used by 37 tests in the module
        it lives in, and widening it for one caller is how a fixture starts
        drifting from what those tests think it does.
        """
        from test_picture_options import make_video

        video = make_video()
        video.item = {"MediaType": media_type, "Type": media_type}
        video.is_photo = is_photo
        done = threading.Event()

        def answer():
            while not done.wait(0.02):
                try:
                    self.pm._player.fire_property("duration", 100.0)
                except Exception:
                    return

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self.addCleanup(done.set)
        try:
            with mock.patch.object(player_module.settings,
                                   "playback_timeout", 2):
                self.pm._play_media(video, "http://example.invalid/s.mkv",
                                    is_initial_play=True)
        finally:
            done.set()
            thread.join(timeout=1)
        self.assertIs(self.pm._video, video,
                      "the start did not complete, so this scenario is "
                      "measuring a failed load")
        return video

    def _show_a_page(self, zoom=None):
        """A comic page: `show_picture` loads it straight into the VO, which
        is a path `_play_media` never runs, so nothing it writes is undone by
        the next item."""
        self.assertTrue(self.pm.show_picture("/tmp/page.png"))
        if zoom is not None:
            self.pm.set_picture_view(zoom=zoom, pan_x=0.2, pan_y=-0.3)

    def test_playing_an_item_and_coming_back(self):
        """The baseline round trip. If this drifts, every scenario below is
        measuring that drift as well as its own."""
        before = self._baseline()
        self.play()
        self._return_to_the_library()
        self._assert_restored(before, "playing one item")

    def test_the_gear_menus_aspect_override(self):
        """The HUD's Aspect Ratio control, which corrects a badly flagged
        file -- a per-item judgement if ever there was one."""
        from jellyfin_mpv_shim.mpvtk_browser.gateway.hud import HudMixin

        before = self._baseline()
        self.play()
        with mock.patch("jellyfin_mpv_shim.player.playerManager", self.pm):
            HudMixin().set_aspect("4:3")
        self._return_to_the_library()
        self._assert_restored(before, "forcing a 4:3 aspect from the gear "
                                      "menu")

    def test_the_gear_menus_deinterlace_force(self):
        """The sibling control that got this right, as the contrast: it has
        an override flag, a per-item re-write and two clear doors."""
        before = self._baseline()
        self.play()
        self.pm.set_deinterlace(True)
        self._return_to_the_library()
        self._assert_restored(before, "forcing deinterlace from the gear "
                                      "menu")

    # -- the less-used corners, which is where this pays ------------------

    def test_reading_a_comic_and_coming_back(self):
        """`show_picture` borrows four properties at once -- `keepaspect`,
        `image_display_duration`, `keep_open` and the shader profile -- and
        it is the one load that does not go through `_play_media`, so
        nothing rewrites them for the next item."""
        before = self._baseline()
        self._show_a_page()
        self._return_to_the_library()
        self._assert_restored(before, "reading a comic")

    def test_a_comic_zoomed_and_panned(self):
        """The reading modes write `video-zoom` and `video-pan-*`, which are
        global. This is the leak that made every film after a comic play
        zoomed."""
        before = self._baseline()
        self._show_a_page(zoom=1.25)
        self._return_to_the_library()
        self._assert_restored(before, "zooming and panning a comic page")

    def test_a_comic_opened_while_music_plays(self):
        """The two features together, now that a page stops the track. Both
        halves write mpv, and the stop happens between them."""
        before = self._baseline()
        self._play_item("Audio")
        self._show_a_page(zoom=1.25)
        self._return_to_the_library()
        self._assert_restored(before, "opening a comic over music")

    def test_music_played_on_repeat_one(self):
        """`loop-file` is a music feature and a global option."""
        before = self._baseline()
        self._play_item("Audio")
        self.pm.set_repeat("one")
        self.pm.set_repeat("none")
        self._return_to_the_library()
        self._assert_restored(before, "playing a track on repeat-one")

    def test_music_muted_from_the_now_playing_bar(self):
        before = self._baseline()
        self._play_item("Audio")
        self.pm.set_mute(True)
        self.pm.set_mute(False)
        self._return_to_the_library()
        self._assert_restored(before, "muting a track")

    def test_a_photo_viewed_and_left(self):
        """A still sets `image_display_duration` on a branch of its own."""
        before = self._baseline()
        self._play_item("Photo", is_photo=True)
        self._return_to_the_library()
        self._assert_restored(before, "viewing a photo")

    def test_fullscreen_toggled_during_playback(self):
        before = self._baseline()
        self.play()
        self.pm.set_fullscreen(True)
        self.pm.set_fullscreen(False)
        self._return_to_the_library()
        self._assert_restored(before, "toggling fullscreen during playback")


if __name__ == "__main__":
    unittest.main()
