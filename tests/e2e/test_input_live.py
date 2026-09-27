"""Slice S3, `input_live`: keys and the mouse on the app's own mpv.

Critical-path inventory rows 33-41. What these replace (`test_input_routing`,
`test_keyboard_nav`, `test_mouse_routing`) hand-scripted the transitions, or
ran on a separate mpv with mpvtk's options -- no OSC, no default bindings, no
scripts -- where the bugs in this area cannot happen (audit A1:148-159,
A2:34-35). Here the transitions are real: play by ENTER, leave by the HUD's
Back, and every key goes through the app's own section stack.
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


class _InputCase(_PlaybackCase):

    def open_film(self):
        self.film = self.movie(LONG_NAME)
        self.fresh(self.film)
        _flows.open_by_search(self.app, LONG_NAME, self.film)

    def playing_for(self, seconds=2):
        self.assertTrue(_e2e.wait_for(lambda: self.time_pos() > seconds,
                                      timeout=30), "playback never started")

    def toggles(self, key, prop):
        """``key`` flips mpv's ``prop`` and flips it back."""
        for _ in range(2):
            was = self.app.prop(prop)
            self.app.key(key)
            self.assertTrue(
                _e2e.wait_for(lambda: self.app.prop(prop) is (not was),
                              timeout=10),
                "%s did not toggle %s (still %r)" % (key, prop, was))

    def leave_by_hud_back(self):
        """ENTER wakes the HUD (and toggles pause), TAB to Back, ENTER."""
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "hud-back"), timeout=15,
                          what="the HUD")
        self.app.move_to("hud-back")
        self.app.key("ENTER")
        try:
            self.app.wait_for(
                lambda f: self.app.prop("idle-active") is True
                and _app.shown(f, "nav-settings"),
                timeout=30, what="the library after HUD Back")
        except _app.AppError as exc:
            trail = [(h.get("rev"), h.get("nav"), h.get("top"))
                     for h in self.app.history()[-40:]]
            raise _app.AppError("%s\ntrail %r\nclicks %r navs %r pause %r"
                                % (exc, trail, self.app.frame().get("clicks"),
                                   self.app.frame().get("navs"),
                                   self.app.prop("pause")))


class KeysAcrossAFilmTest(_InputCase):
    """Row 33, the video half: arrows and TAB from launch, then a film played
    by ENTER, where RIGHT seeks and SPACE/`m` answer, left by the HUD's Back
    -- after which the library's keys still work."""

    def test_the_keys_answer_before_during_and_after_a_film(self):
        # From launch: TAB and the arrows move focus.
        self.app.move_to("nav-home")
        was = self.app.frame().get("nav")
        self.app.key("DOWN")
        self.app.wait_for(lambda f: f.get("nav") not in (None, was),
                          timeout=5, what="DOWN to move focus from launch")

        self.open_film()
        self.play()
        self.playing_for()
        before = self.time_pos()
        self.app.key("RIGHT")
        self.assertTrue(
            _e2e.wait_for(lambda: self.time_pos() >= before + 3, timeout=10),
            "RIGHT did not seek (%.1f -> %.1f)" % (before, self.time_pos()))
        self.toggles("SPACE", "pause")
        self.toggles("m", "mute")

        self.leave_by_hud_back()
        self.app.move_to("nav-settings")
        self.app.press_until("ENTER", lambda f: _app.shown(f, "stab-general"),
                             what="Settings after a film")
        self.app.key("ESC")
        self.app.wait_for(lambda f: not _app.shown(f, "stab-general"),
                          timeout=10, what="ESC to close Settings")
        self.assertEqual([], self.app.lost_keys)
        self.assertEqual(0, self.app.quit(timeout=30))


class SpaceAndMuteAfterMusicTest(_InputCase):
    """Row 33, PM C2: after music, stopped, then a film, SPACE and `m` still
    reach the film. The music half's key claims outlived the music."""

    def album(self):
        for a in self.session.find_all(library="Music",
                                       item_type="MusicAlbum"):
            if len(self.session.find_all(item_type="Audio",
                                         parent_id=a["Id"])) >= 2:
                return a
        self.skipTest("no album with two tracks on this server")

    def test_space_and_m_reach_the_film_after_music(self):
        album = self.album()
        _flows.open_by_search(self.app, album["Name"], album["Id"],
                              section="Albums", landed="album-play")
        self.app.move_to("album-play")
        self.app.key("ENTER")
        self.app.playing_path()
        self.app.wait_for(lambda f: _app.shown(f, "np-stop"), timeout=30,
                          what="the now-playing bar")
        self.toggles("SPACE", "pause")         # the library is up: music
        self.app.move_to("np-stop")
        self.app.key("ENTER")
        self.app.wait_for(
            lambda f: self.app.prop("idle-active") is True
            and not _app.shown(f, "np-stop"),
            timeout=30, what="the music to stop")

        self.open_film()
        self.play()
        self.playing_for()
        self.toggles("SPACE", "pause")
        self.toggles("m", "mute")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
