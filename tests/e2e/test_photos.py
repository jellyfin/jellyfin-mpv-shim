"""One picture is a picture; an album is a slideshow.

A photo is a video that happens to be still, which makes the whole feature a
matter of two mpv properties agreeing with two decisions the shim makes:

* `image_display_duration` — how long mpv holds a still before it reaches
  end-of-file. The in-window browser parks it at **inf** while it owns the
  window (`set_browse_window`), and `browse_yield` deliberately does not undo
  that, so a photo opened out of the library inherited "inf" and displayed
  forever. The queue was waiting on an EOF mpv had been told never to send:
  that is the whole of "photo auto-advance is broken".
* `pause` — a photo opened on its own is held, and one reached by Play All or
  by the queue advancing is not.

Neither is visible without a real mpv. `image_display_duration` is a property
of the demuxer, and a fake accepts "inf" as cheerfully as it accepts 5;
`tests/test_photos.py` covers the pause decision as a truth table over an
**excerpt** of `_play_media`, and an excerpt cannot see that the same method
went on to unpause it seventy lines later — which it did, for the whole life
of the feature, until this suite ran (`02d3329e`).

The fixtures are the `orientation-*` JPEGs. Deliberately not `format-gif`:
an animated GIF is a video as far as mpv is concerned, with a real duration
and no `image_display_duration` at all, so a slideshow test written on it
would pass whatever these properties said.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

LIBRARY = "Photos"
#: Short enough that a slideshow test is quick, and >= mpv's own floor of 1.
DISPLAY_SECS = 2


@_e2e.require_server_and_mpv
class PhotoTest(_e2e.E2ETestCase):

    def setUp(self):
        super().setUp()
        from jellyfin_mpv_shim.conf import settings

        photos = [p for p in self.session.find_all(
            library=LIBRARY, item_type="Photo", Limit=50)
            if p["Name"].startswith("orientation-")]
        if len(photos) < 3:
            self.skipTest("need three still photos in %r; found %d"
                          % (LIBRARY, len(photos)))
        self.photos = sorted(photos, key=lambda p: p["Name"])[:3]
        self.ids = [p["Id"] for p in self.photos]

        self.settings = settings
        self._was = settings.photo_display_secs
        settings.photo_display_secs = DISPLAY_SECS
        self.addCleanup(setattr, settings, "photo_display_secs", self._was)
        # The library owns the window before anything is played, and that is
        # where the "inf" comes from. Entering it here is not scene-setting:
        # it is the precondition the auto-advance bug needed.
        self.pm.set_browse_window(True)
        self.addCleanup(self.pm.set_browse_window, False)

    def _play(self, pause_stills):
        media = _e2e.build_media(self.session, self.ids)
        video = media.video
        self.assertTrue(video.is_photo,
                        "%r did not come back as a Photo"
                        % self.photos[0]["Name"])
        self.pm.play(video, is_initial_play=True, pause_stills=pause_stills)
        self.assertIs(self.pm._video, video, "the photo never started")
        return video

    def _current_id(self):
        return self.pm._video.item_id if self.pm._video else None

    def _display_duration(self):
        """`image_display_duration`, as a number, on either backend.

        The two disagree about its type: libmpv hands back a float and
        jsonipc the string mpv printed, so an endless still is `inf` on one
        and `'inf'` on the other. Comparing the raw value passed on libmpv
        and failed on jsonipc for a reason that had nothing to do with the
        app — which is what the backend matrix is for.
        """
        return float(self.pm._player.image_display_duration)

    # -- the tests ---------------------------------------------------------

    def test_the_browsers_endless_still_does_not_survive_into_a_photo(self):
        """`set_browse_window` parks the duration at inf and `browse_yield`
        leaves it there, so the value has to be re-set on the way into
        playback. Inherit it and the picture never reaches end-of-file, and
        the queue behind it never moves."""
        self.assertEqual(
            self._display_duration(), float("inf"),
            "the browser no longer parks image_display_duration at inf, so "
            "this test is no longer staging the condition it exists for")
        self._play(pause_stills=False)
        self.assertEqual(
            self._display_duration(), float(DISPLAY_SECS),
            "the photo inherited the browser's endless display duration, so "
            "mpv will never send the EOF the queue is waiting for")

    def test_one_photo_opened_on_its_own_is_held(self):
        """Clicking a picture means "show me this". It must not run on."""
        self._play(pause_stills=True)
        self.assertTrue(self.pm._player.pause,
                        "the photo started playing instead of being held")
        moved = self.pump_until(
            lambda: self._current_id() != self.ids[0],
            timeout=DISPLAY_SECS * 3 + 4)
        self.assertFalse(
            moved,
            "a photo opened on its own advanced to the next one after about "
            "%d seconds — the viewer turned into a slideshow" % DISPLAY_SECS)
        self.assertEqual(self._current_id(), self.ids[0])

    def test_play_all_runs_the_slideshow(self):
        """The other half of the same decision: Play All on an album must
        not pause on frame one, or the queue never starts."""
        self._play(pause_stills=False)
        self.assertFalse(self.pm._player.pause,
                         "Play All paused on the first picture")
        advanced = self.pump_until(
            lambda: self._current_id() == self.ids[1],
            timeout=DISPLAY_SECS * 4 + 10)
        self.assertTrue(
            advanced,
            "the slideshow never advanced past the first picture (still on "
            "%r after %d seconds)" % (self._current_id(), DISPLAY_SECS * 4))

    def test_the_slideshow_does_not_stop_after_one_frame(self):
        """The regression `is_initial_play` exists for. Pausing on every
        load meant the queue advanced onto the second picture and paused
        there too, so the slideshow moved exactly one frame and stopped —
        which looks far more like "auto-advance is broken" than like a
        deliberate pause."""
        self._play(pause_stills=False)
        self.assertTrue(
            self.pump_until(lambda: self._current_id() == self.ids[1],
                            timeout=DISPLAY_SECS * 4 + 10),
            "never reached the second picture")
        self.assertFalse(
            self.pm._player.pause,
            "the slideshow paused on the picture it advanced onto")
        self.assertTrue(
            self.pump_until(lambda: self._current_id() == self.ids[2],
                            timeout=DISPLAY_SECS * 4 + 10),
            "the slideshow advanced one frame and stopped")

    def test_a_photo_needs_no_playback_session(self):
        """Photos do not go through PlaybackInfo — that endpoint answers
        about MediaSources and a Photo has none. Asking anyway yields no
        source to negotiate and, worse, a play-session id that the reporting
        path would then carry for an item the server has no session for."""
        video = self._play(pause_stills=True)
        self.assertIsNone(video.playback_info,
                          "a photo went through PlaybackInfo")
        self.assertIsNone(video.media_source)
        url = video.get_playback_url()
        host = _e2e.SERVER.split("//")[-1].split("/")[0]
        self.assertIn(host, url)
        self.assertIn("/Items/", url,
                      "a photo is fetched from the item endpoint, not from a "
                      "video stream url: %s" % url)


if __name__ == "__main__":
    unittest.main()
