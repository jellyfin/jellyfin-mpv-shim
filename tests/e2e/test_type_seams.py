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

**Why these three types, and not the others.** Checked rather than assumed:

* **epub is not a seam producer.** The reader draws with Pillow and pushes a
  bitmap like a tile strip; the only thing `pages/reader.py` asks of the
  player is `book_download_state`, which reads. It mutates no mpv option, so
  "a film after a book" is just "a film after the browser" -- already the
  control row. (Contrast `pages/comic.py`, which calls `show_picture`,
  `set_picture_view` and `clear_picture`.)
* **Live TV is not distinguishable as a predecessor.** There is no
  live-specific mpv state anywhere in `player*.py` -- no `is_live` concept in
  `media.py` at all -- so a channel leaves exactly what a film leaves. A row
  for it would cost a minute of wall clock and assert nothing the video row
  does not.

Both were on the list of gaps until they were looked at. Adding either would
have been a test that cannot fail; this note is here so the next person does
not re-derive it.

**Scope, deliberately.** These are the properties `play()` itself owns.
`keepaspect` and the zoom/pan reset are owned by the browser handoff
(`browse_yield`, `_release_page_grabs`) and not by the player, so asserting
them here would fail for a reason that is not a bug. The comic -> video pair
carries them and lives in
`test_comic_reader.test_a_film_after_a_comic_is_neither_zoomed_nor_stretched`,
which has a browser; `PictureViewHandoffTest` covers the same two
interleavings against fakes. See `reset_picture_view`'s docstring for why
that split is not an accident.
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

    def _loops(self):
        """Whether mpv will loop the current file.

        Asked as a question rather than compared to a literal: the property
        is written as the strings "inf" and "no", and **mpv does not answer
        in the same alphabet it accepts** -- measured, "inf" reads back as
        "inf" and "no" reads back as the boolean False. Asserting
        `loop_file == "no"` fails against correct behaviour, on the backend
        it was written on.
        """
        value = self.pm._player.loop_file
        return value not in (False, "no", 0, "0", None)

    def test_repeat_one_on_a_track_does_not_loop_the_next_film(self):
        """`loop-file` is the seam with the sharpest failure: the film never
        ends.

        Repeat is a MUSIC feature (`set_repeat`: "loop-file is applied only
        while audio plays... so it never makes a video loop"), and `loop-file`
        is a global option that outlives the item it was set for. The write
        lives at the TOP of `_play_media`, before `play()`, and its comment
        records why: it used to run at the very end of the start, past the
        `if not loaded: return`, so a video whose load FAILED kept whatever
        the previous item set -- and if that was a track under repeat-one, it
        stayed "inf".

        Nothing in tests/ touches `loop_file` outside the fakes, so neither
        half of that was pinned.
        """
        self.addCleanup(self.pm.set_repeat, "none")

        self._play("music")
        self.pm.set_repeat("one")
        self.assertTrue(
            self._loops(),
            "repeat-one did not reach mpv, so nothing is staged to leak")

        self._play("video")
        self.assertFalse(
            self._loops(),
            "the film inherited repeat-one from the track before it: it will "
            "play for ever and the queue behind it will never move")
        self.assertTrue(
            self.pm.repeat_mode == "one",
            "the user's repeat preference was silently forgotten rather than "
            "just not applied -- going back to music should still repeat")

        # ...and it comes back for the next track, because the preference was
        # kept. A fix that cleared `repeat_mode` would pass the line above
        # and break the feature.
        self._play("music")
        self.assertTrue(
            self._loops(),
            "repeat-one did not come back for the next track, so the film "
            "turned the setting off for good")

    @staticmethod
    def _rgb(colour):
        """The RGB triplet of an mpv colour.

        **mpv does not answer in the alphabet it accepts**, the same trap as
        `loop-file` below and `image_display_duration` above: the shim writes
        `#141414` and mpv reads it back as `#ff141414`, alpha first. Comparing
        the string it was given fails against correct behaviour.
        """
        text = str(colour).lstrip("#").lower()
        return text[-6:]

    def _bg(self):
        """`background` and `background-color`, as mpv answers them."""
        return (str(self.pm._player.background),
                self._rgb(self.pm._player.background_color))

    def test_a_film_gets_mpvs_background_back_and_music_keeps_the_browse_one(
            self):
        """`background` / `background-color`, the browse -> video seam with
        the longest reach.

        The browser paints its own window rather than decoding a file to hold
        it open, so `set_browse_window` parks `background` at "color" and
        `background-color` at the theme's `#141414`. Nobody put them back:
        **every letterboxed film played after the browser had been on screen
        got #141414 bars instead of black, for the rest of the mpv process's
        life** (`dde0f2a1`). `background` is harmless under opaque video --
        it is `border-background`, which defaults to reading the same colour,
        that carries it to the bars.

        Asserted here and not in the matrix above because the restore belongs
        to `browse_yield`, which `play()` does not call; the gateway's
        `on_browse_leave` does. So the handoff is driven explicitly.

        **And music must NOT get it back.** Audio never yields the window --
        `MpvtkBrowser.on_playstate` calls `enter_browse()` for audio and
        `_yield()` only for video, because the library stays on screen behind
        the now-playing bar -- so a track legitimately keeps the UI-matching
        background. A test that asserted "black after any playback" would be
        asserting a bug into place. Nothing else in tests/ reads these two
        properties off a real mpv at all; the existing assertion is that
        browse SETS them, against a fake.
        """
        from jellyfin_mpv_shim.player_window import (
            BROWSE_BG_HEX, MPV_DEFAULT_BACKGROUND, MPV_DEFAULT_BACKGROUND_HEX)

        self.assertEqual(
            self._bg(), ("color", self._rgb(BROWSE_BG_HEX)),
            "the browse window did not park the background, so nothing is "
            "staged to leak into the film")

        # The handoff, as `gateway.playback.on_browse_leave` runs it.
        self.pm.browse_yield()
        self._play("video")
        self.assertEqual(
            self._bg(),
            (MPV_DEFAULT_BACKGROUND, self._rgb(MPV_DEFAULT_BACKGROUND_HEX)),
            "the film inherited the browser's window background: every "
            "letterboxed film gets %s bars instead of black, and it lasts "
            "the rest of the mpv process's life" % BROWSE_BG_HEX)

        # Music: no yield, by design, so the browse colour stays.
        self.pm.set_browse_window(True)
        self._play("music")
        self.assertEqual(
            self._bg(), ("color", self._rgb(BROWSE_BG_HEX)),
            "music lost the browse background -- the library is still on "
            "screen behind the now-playing bar, so this is the window the "
            "user is looking at")

    def test_repeat_one_does_not_survive_a_video_whose_load_fails(self):
        """The half `loop-file`'s ordering fix was actually about.

        `9d2d35f1` moved the write to before mpv is handed the file
        precisely because it used to sit at the very END of `_play_media`,
        past the `if not loaded: return` that a failed start takes. So the
        item that inherited repeat-one was not the one that played -- it was
        the one that DIDN'T. Nothing asserted that behaviourally;
        `tests/test_playstate_payload.py` pins it as statement order in the
        source, which cannot see whether mpv ended up looping.

        A zero-byte file is the cleanest failing start in the library: there
        is nothing to decode, so the load fails outright rather than
        aborting partway.
        """
        self.addCleanup(self.pm.set_repeat, "none")
        broken = self.session.find(name="Zero-byte file")
        if not broken:
            self.skipTest("no zero-byte fixture to fail a start with")

        self._play("music")
        self.pm.set_repeat("one")
        self.assertTrue(self._loops(), "repeat-one never reached mpv")

        video = _e2e.build_media(self.session, [broken["Id"]]).video
        self.pm.play(video, is_initial_play=True)
        self.assertFalse(
            self._loops(),
            "a video whose load FAILED kept the track's repeat-one. The "
            "write is back at the end of _play_media, past the `if not "
            "loaded: return` -- so the file that inherits the loop is the "
            "one that never played, and the next real video inherits it too")

    def test_playback_speed_is_per_session_and_not_per_item(self):
        """A decision, pinned as one: **speed is deliberately NOT reset
        between items.**

        `speed` is not a construction option and no start path resets it
        (`_play_media`'s `set_speed(1)` is inside the SyncPlay branch), so it
        holds for the life of the mpv process and a fresh mpv starts at 1.0.

        **This asserts the queue does not reset it; it does not assert that
        nothing else writes it.** `gateway/hud.py:set_speed` writes the
        property directly rather than through `PlayerManager.set_speed`, and
        nothing resets it afterwards -- so the HUD's speed control is a
        session-wide change with no owner, reachable only from a film's gear
        menu and not from the library, a photo or the now-playing bar. That reads exactly like
        one of the leaks this module exists to catch -- a global set for one
        item and never put back -- which is why it is written down here
        rather than left for the next person to "fix": a viewer who slows an
        episode down means the next episode too, and re-reading the setting
        per item would fight them every time the queue advanced [iw].

        So this is a change detector on purpose. If speed ever does start
        resetting per item, that is a decision worth making deliberately,
        and this test is the place it gets argued.
        """
        was = self.pm.get_speed()
        self.addCleanup(self.pm.set_speed, was)
        self.assertEqual(float(was), 1.0,
                         "the session did not start at normal speed, so a "
                         "carry-over below would prove nothing")

        self._play("video")
        self.pm.set_speed(1.5)
        self.assertAlmostEqual(float(self.pm.get_speed()), 1.5, places=3,
                               msg="set_speed never reached mpv")

        self._play("music")
        self.assertAlmostEqual(
            float(self.pm.get_speed()), 1.5, places=3,
            msg="the speed the viewer chose was reset by the next item. That "
                "is a behaviour change, not a bug fix: speed is per session "
                "here, so the queue advancing must not fight the choice")

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
