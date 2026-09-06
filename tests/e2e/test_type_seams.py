"""What one content type leaves behind for the next — the seam matrix.

Every playback test in this suite is *vertical*: one content type, one
session, its own class with disjoint fixtures. That isolation is deliberate
(`tests/e2e/README.md`: "Test classes own disjoint fixtures... pick an unused
series rather than sharing one") and it is why exactly one test in the whole
tier crosses a feature boundary --
`test_photos.test_the_browsers_endless_still_does_not_survive_into_a_photo`.

**Vertical is the wrong axis for this bug class.** mpv is not re-created
between queue items, so any global written for one item is still set for the
next, and the shipped failures all read as a defect in the *second* feature
while belonging to the first:

* the browser parks `image_display_duration` at `inf`; a photo that inherits
  it never reaches end-of-file and the slideshow behind it never moves;
* a comic leaves `video-zoom` and `video-pan-*` set, and every film after one
  played zoomed (`reset_picture_view`);
* `keepaspect` restored at the wrong moment stretched **every** film, with no
  comic anywhere in the session (`docs/mpv-backends.md`, and
  `tests/test_window_geometry.py:PictureViewHandoffTest`).

So this file runs the ordered pairs and asks, after each B, whether B's own
state is right — regardless of which A preceded it. A per-type expectation is
declared once in `EXPECTED` and checked after every predecessor, which is the
part a vertical test cannot do: `test_photos` proves a photo is correct after
the BROWSER, and says nothing about a photo after a film or after a track.

**Scope, deliberately.** These are the properties `play()` itself owns.
`keepaspect` and the zoom/pan reset are owned by the browser handoff
(`browse_yield`, `_release_page_grabs`) and not by the player, so asserting
them in a browser-less harness would fail for a reason that is not a bug;
they are covered by `test_comic_reader` and `PictureViewHandoffTest`, which
have a browser. See `reset_picture_view`'s docstring for why that split is
not an accident.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

PHOTO_LIBRARY = "Photos"
DISPLAY_SECS = 2

#: What each type requires of the player once it is the thing playing.
#: ``None`` means "not this type's business" and is not asserted.
#:
#: `library_showing` is the seam that killed BACK and the mouse's back button
#: for the whole of music playback: audio keeps `_video` set AND keeps the
#: browser up, so it is the one type that plays without taking the library
#: away. `display_secs` is the one #: `test_photos` already pins after the
#: browser and nothing pins after a film.
EXPECTED = {
    "video": {"library_showing": False, "is_audio": False,
              "display_secs": None},
    "music": {"library_showing": True, "is_audio": True,
              "display_secs": None},
    "photo": {"library_showing": False, "is_audio": False,
              "display_secs": float(DISPLAY_SECS)},
}


@_e2e.require_server_and_mpv
class TypeSeamMatrixTest(_e2e.E2ETestCase):

    def setUp(self):
        super().setUp()
        from jellyfin_mpv_shim.conf import settings

        eps = self.session.episodes("The Standard Show", season=1)
        tracks = None
        for album in self.session.find_all(library="Music",
                                           item_type="MusicAlbum"):
            found = self.session.find_all(item_type="Audio",
                                          parent_id=album["Id"])
            if found:
                tracks = found
                break
        photos = [p for p in self.session.find_all(
            library=PHOTO_LIBRARY, item_type="Photo", Limit=50)
            if p["Name"].startswith("orientation-")]
        if not (eps and tracks and photos):
            self.skipTest("need an episode, a track and a photo (%d/%d/%d)"
                          % (len(eps or ()), len(tracks or ()), len(photos)))
        self.items = {"video": eps[0], "music": tracks[0],
                      "photo": sorted(photos, key=lambda p: p["Name"])[0]}

        self._was = settings.photo_display_secs
        settings.photo_display_secs = DISPLAY_SECS
        self.addCleanup(setattr, settings, "photo_display_secs", self._was)
        # The browser owns the window before anything plays, and parking
        # `image_display_duration` at inf is what it does there. Without
        # this the matrix starts from a clean value and the inheritance it
        # exists to catch cannot happen.
        self.pm.set_browse_window(True)
        self.addCleanup(self.pm.set_browse_window, False)

        ids = [i["Id"] for i in self.items.values()]
        self.session.reset_played(*ids)
        self.addCleanup(self.session.reset_played, *ids)

    def _play(self, kind):
        media = _e2e.build_media(self.session, [self.items[kind]["Id"]])
        video = media.video
        self.assertIsNotNone(video, "Media built nothing for %s" % kind)
        # A still opened on its own is held rather than advanced; that is
        # `_play_media`'s pause_stills branch and not what this file is
        # about, so the slideshow form is used for photos throughout.
        self.pm.play(video, is_initial_play=True, pause_stills=False)
        self.assertIs(self.pm._video, video, "%s never started" % kind)
        return video

    def _display_secs(self):
        """`image_display_duration` as a float on either backend."""
        return float(self.pm._player.image_display_duration)

    def _assert_state(self, kind, after):
        want = EXPECTED[kind]
        self.assertIs(
            self.pm._library_showing(), want["library_showing"],
            "%s after %s: _library_showing() is wrong. For music this is the "
            "seam that kills BACK and the mouse back button for the whole of "
            "playback; for a picture it hands the library input it cannot "
            "use." % (kind, after))
        self.assertIs(
            bool(self.pm._current_is_audio()), want["is_audio"],
            "%s after %s: _current_is_audio() disagrees, so every predicate "
            "built on it answers for the wrong type" % (kind, after))
        if want["display_secs"] is not None:
            self.assertEqual(
                self._display_secs(), want["display_secs"],
                "%s after %s: image_display_duration is %r, not %r -- it was "
                "inherited rather than set, and mpv will never send the EOF "
                "the queue is waiting for"
                % (kind, after, self._display_secs(), want["display_secs"]))

    def test_the_matrix(self):
        """Every ordered pair, plus each type from the browser as the
        control row -- if a type is already wrong with no predecessor, the
        pair rows below are measuring the wrong thing."""
        for kind in EXPECTED:
            with self.subTest(first=kind, after="the browser"):
                self._play(kind)
                self._assert_state(kind, "the browser")

        for first in EXPECTED:
            for second in EXPECTED:
                if first == second:
                    continue
                with self.subTest(first=first, then=second):
                    self.pm.set_browse_window(True)
                    self._play(first)
                    self._play(second)
                    self._assert_state(second, first)


if __name__ == "__main__":
    unittest.main()
