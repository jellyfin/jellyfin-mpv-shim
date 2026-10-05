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
import test_playback_lifecycle as _pl  # noqa: E402
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
        """ENTER wakes the HUD (and toggles pause), TAB to Back, ENTER.
        Pressed again if nothing came: see type_seams_live's copy."""
        self.app.press_until("ENTER", lambda f: _app.shown(f, "hud-back"),
                             what="the HUD", retry_after=3)
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


def _centre(frame):
    return frame.get("w", 1280) / 2, frame.get("h", 720) / 2


class _MusicMixin:
    def album(self):
        for a in self.session.find_all(library="Music",
                                       item_type="MusicAlbum"):
            if len(self.session.find_all(item_type="Audio",
                                         parent_id=a["Id"])) >= 2:
                return a
        self.skipTest("no album with two tracks on this server")

    def play_album(self):
        album = self.album()
        _flows.open_by_search(self.app, album["Name"], album["Id"],
                              section="Albums", landed="album-play")
        self.app.move_to("album-play")
        self.app.key("ENTER")
        self.app.playing_path()
        self.app.wait_for(lambda f: _app.shown(f, "np-stop"), timeout=30,
                          what="the now-playing bar")
        return album


class TheHudComesAndGoesTest(_InputCase):
    """Row 38: summoned by the pointer, gone after `hud_hide_secs` with no
    input, three times over; and after a queue advance it comes back for
    the NEW item (F41: the HUD's state outlived the advance)."""

    CONF = {"hud_hide_secs": 1.5}
    before_login = _pl.StepsAndMarksTest.before_login
    open_playlist = _pl.StepsAndMarksTest.open_playlist

    def summon(self):
        return self.app.summon_hud()

    def test_it_comes_and_goes_and_follows_an_advance(self):
        self.open_playlist()
        self.app.move_to("pl-play")
        self.app.key("ENTER")
        self.assertIn(self.first, self.app.playing_path())
        self.playing_for()
        for _ in range(3):
            self.summon()
            self.app.wait_for(lambda f: not f.get("phud_shown"), timeout=6,
                              what="the HUD to hide by itself")
        self.app.key(">")
        self.assertTrue(_e2e.wait_for(
            lambda: self.second in (self.app.prop("path") or ""),
            timeout=30), "> did not advance")
        self.playing_for()
        self.summon()
        try:
            self.app.wait_for(
                lambda f: any(n.get("text") == _pl.SECOND_LONG_NAME
                              for n in f.get("nodes", [])),
                timeout=5, what="the HUD titled for the new item")
        except _app.AppError:
            texts = [n.get("text") for n in self.app.frame().get("nodes", [])
                     if n.get("text")]
            self.fail("the HUD came back without the new item's title: %r"
                      % texts[:20])
        self.assertEqual(0, self.app.quit(timeout=30))


class TheRightButtonPausesTest(_InputCase):
    """Row 36 (A4, #724/#737): with `mouse_click_pauses` off, the right
    button over a HIDDEN HUD pauses, on the app's own mpv with its default
    bindings -- where mpv's own MBTN_RIGHT (a context menu on master) is in
    play, which the spawned-mpv test could not have. Then, after playback,
    a left click still opens a tile."""

    CONF = {"mouse_click_pauses": False, "hud_hide_secs": 1.0}

    def test_right_click_pauses_and_the_mouse_survives_playback(self):
        self.open_film()
        self.play()
        self.playing_for()
        self.app.point(*_centre(self.app.frame()))
        # Up first, then hidden: the frame before the move is "hidden" too.
        self.app.wait_for(lambda f: f.get("phud_shown"), timeout=5,
                          what="the HUD on a pointer move")
        self.app.wait_for(lambda f: not f.get("phud_shown"), timeout=8,
                          what="the HUD hidden again")
        # Settled, which is the claim. A press in the instant the bar hides
        # is lost about half the time -- a separate finding (the register,
        # 2026-09-27), not this row's.
        time.sleep(1.0)
        for want in (True, False):
            self.app.press("MBTN_RIGHT")
            if not _e2e.wait_for(lambda: self.app.prop("pause") is want,
                                 timeout=5):
                binds = [(b.get("section"), b.get("cmd"))
                         for b in self.app.prop("input-bindings") or []
                         if b.get("key") == "MBTN_RIGHT"]
                f = self.app.frame()
                self.fail("the right button did not set pause=%s; bound: %r;"
                          " phud_mode=%r shown=%r hover=%r mouse=%r"
                          % (want, binds, f.get("phud_mode"),
                             f.get("phud_shown"), f.get("hover"),
                             self.app.prop("mouse-pos")))
            self.assertFalse(
                self.app.prop("user-data/mpv/context-menu/open"),
                "mpv's own context menu opened over the video")
        self.leave_by_hud_back()          # lands on the film's page
        self.app.move_to("nav-home")
        self.app.press_until("ENTER", lambda f: any(
            (n.get("id") or "").startswith("row-libs-") and n.get("vis")
            for n in f.get("nodes", [])), what="Home")
        f = self.app.frame()
        tile = next(n["id"] for n in f["nodes"]
                    if (n.get("id") or "").startswith("row-libs-")
                    and n.get("vis"))
        self.app.click(tile)
        self.app.wait_for(lambda f: _app.shown(f, "grid-shuffle")
                          or _app.shown(f, "grid-sort"), timeout=15,
                          what="a left click to open %s after playback" % tile)
        self.assertEqual(0, self.app.quit(timeout=30))


class ARemappedWakeKeyTest(_InputCase):
    """Row 34, the half with no intro fixture: moved off ENTER, the wake key
    is what summons the HUD for the keyboard. (ENTER not skipping an intro
    needs a server with media segments; the QA library has none.)"""

    CONF = {"hud_wake_key": "a"}

    def test_the_remapped_key_summons(self):
        self.open_film()
        self.play()
        self.playing_for()
        self.app.key("a")
        # Shown first, focused once Python's HUD scene arrives.
        self.app.wait_for(lambda f: f.get("phud_shown") and f.get("nav"),
                          timeout=5, what="the HUD, focused, on the remapped "
                                          "wake key")
        self.assertEqual(0, self.app.quit(timeout=30))


class _OscStyleMixin:
    """Row 39: under `osc_style` mpv the pointer brings up mpv's own bar and
    no HUD; under none, nothing at all. Measured on mpv's own window
    screenshot (OSD included) of a paused frame, before and after a move."""

    STYLE = None

    def shot(self, name):
        path = os.path.join(self.app.config_dir, name)
        self.app.mpv.command("screenshot-to-file", path, "window")
        return path

    def bottom_differs(self, a, b):
        from PIL import Image, ImageChops
        ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
        w, h = ia.size
        box = (0, int(h * 0.8), w, h)
        return ImageChops.difference(ia.crop(box), ib.crop(box)).getbbox()

    def test_the_pointer_brings_up_the_right_controls(self):
        self.open_film()
        self.play()
        self.playing_for()
        self.app.key("SPACE")
        self.assertTrue(_e2e.wait_for(lambda: self.app.prop("pause") is True,
                                      timeout=5))
        time.sleep(3)                        # anything shown has hidden
        before = self.shot("before.png")
        # Near the bottom: mpv's bar comes up for the pointer THERE (its
        # layout), and a move to the centre shows nothing under any style.
        f = self.app.frame()
        self.app.point(f.get("w", 1280) / 2, f.get("h", 720) - 20)
        time.sleep(1.0)
        after = self.shot("after.png")
        self.assertFalse(self.app.frame().get("phud_shown"),
                         "the HUD came up under osc_style %s" % self.STYLE)
        moved = self.bottom_differs(before, after)
        if self.STYLE == "mpv":
            self.assertTrue(moved, "no mpv OSC appeared on a pointer move")
        else:
            self.assertFalse(moved, "something drew on a pointer move under "
                                    "osc_style none: %r" % (moved,))
        self.assertEqual(0, self.app.quit(timeout=30))


class TheMpvOscStyleTest(_OscStyleMixin, _InputCase):
    STYLE = "mpv"
    CONF = {"osc_style": "mpv"}


class NoPlayerControlsTest(_OscStyleMixin, _InputCase):
    STYLE = "none"
    CONF = {"osc_style": "none"}


class MediaKeysDuringMusicTest(_MusicMixin, _InputCase):
    """Row 41, dispatch half: during music, MENU opens no OSD menu over the
    library and the media keys act on the music without leaving it."""

    def test_menu_and_media_keys_keep_the_library(self):
        self.play_album()
        first = self.app.prop("path")
        self.app.key("MENU")
        time.sleep(1)
        f = self.app.frame()
        self.assertTrue(_app.shown(f, "np-stop") and _app.shown(
            f, "nav-settings"), "MENU during music took the library away")
        self.assertFalse(f.get("phud_mode"))
        self.app.key("PLAYPAUSE")
        self.assertTrue(_e2e.wait_for(lambda: self.app.prop("pause") is True,
                                      timeout=5), "PLAYPAUSE did not pause")
        self.app.key("PLAYPAUSE")
        self.app.key("NEXT")
        self.assertTrue(_e2e.wait_for(
            lambda: self.app.prop("path") not in (None, first), timeout=10),
            "NEXT did not advance the music (the app's queue, not mpv's "
            "playlist: the path is the witness)")
        self.assertTrue(_app.shown(self.app.frame(), "nav-settings"))
        self.assertEqual(0, self.app.quit(timeout=30))


class TheBackButtonDuringMusicTest(_MusicMixin, _InputCase):
    """Row 35: the mouse's back (thumb) button navigates back while music
    plays -- `_nav_back` once refused it for the whole of music."""

    def test_the_thumb_button_goes_back(self):
        self.play_album()
        self.app.move_to("nav-settings")
        self.app.press_until("ENTER", lambda f: _app.shown(f, "stab-general"),
                             what="Settings during music")
        self.app.key("ESC")
        self.app.wait_for(lambda f: not _app.shown(f, "stab-general"),
                          timeout=10, what="Settings closed")
        self.assertTrue(_app.shown(self.app.frame(), "album-play"))
        self.app.press("MBTN_BACK")
        self.app.wait_for(lambda f: not _app.shown(f, "album-play")
                          and _app.shown(f, "nav-settings"), timeout=10,
                          what="the back button to leave the album page")
        self.assertTrue(_app.shown(self.app.frame(), "np-stop"),
                        "going back stopped the music bar")
        self.assertEqual(0, self.app.quit(timeout=30))


class TheImeFollowsTheTextBoxTest(unittest.TestCase):
    """#798: mpv >= 0.40 keeps the input method off its window unless
    `input-ime` is on, so CJK text could not be typed at all. A focused box
    turns it on, a password box does not, and leaving puts back what was
    there. The key queue is raised with it: a commit is one key per
    character in a burst, and mpv drops what does not fit. On the login form of a fresh config, so no server is needed.
    The real IME is a Windows/Wayland check by hand; this pins the switch."""

    def setUp(self):
        self.app = _app.App(backend=_pl._backend())
        self.addCleanup(lambda: self.app.close())
        self.app.start()
        self.app.wait_for(lambda f: _app.shown(f, "login-server"),
                          timeout=60, what="the login screen")

    def ime_becomes(self, want, what):
        if not _e2e.wait_for(lambda: self.app.prop("input-ime") is want,
                             timeout=5):
            self.fail("input-ime is %r, want %r: %s"
                      % (self.app.prop("input-ime"), want, what))

    def queue_is(self, want, what):
        got = self.app.prop("input-key-fifo-size")
        self.assertEqual(want, got, "input-key-fifo-size %s" % what)

    def focus(self, box):
        self.app.move_to(box)
        self.app.wait_for(lambda f: f.get("focus") == box, timeout=5,
                          what="%s taking the typing" % box)

    def test_on_in_a_box_off_in_the_password_and_after(self):
        for _ in range(3):
            self.focus("login-server")
            self.ime_becomes(True, "in the server box")
            self.queue_is(256, "in the server box")
            self.focus("login-pass")
            self.ime_becomes(False, "in the password box")
            self.queue_is(7, "in the password box")
            self.focus("login-user")
            self.ime_becomes(True, "in the username box")
            self.app.move_to("login-connect")
            self.app.wait_for(lambda f: f.get("focus") is None, timeout=5,
                              what="the boxes to give up the typing")
            self.ime_becomes(False, "after leaving the boxes")
            self.queue_is(7, "after leaving the boxes")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
