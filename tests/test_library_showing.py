"""``_library_showing()`` — the predicate that decides whether the user is
looking at the library, and the three call sites that ride it.

It had **no test at all** and six callers. That is the gap this file closes,
and the reason it matters is that the obvious spelling is wrong in a way that
looks right:

    ``_video is None``          # what everything used to ask
    ``_library_showing()``      # what it means to ask

A video PICTURE is what takes the library away; **music does not**. Audio
keeps ``_video`` set *and* keeps the browser on screen — that is what the
now-playing bar is for — so ``_video is None`` answers "is the library up?"
with a no while the user is looking straight at it.

``_nav_back`` asked it the wrong way and refused BACK for the whole of music
and audiobook playback. The mouse's back button rides the same path (the
renderer routes it as a synthetic ESC), so it died the moment anything
played; and with no way back, FORWARD had nothing to return to and looked
broken with it. `CLAUDE.md` lists this as one of two "ambushes that fire far
from the feature that owns them".

So the table below is not about a boolean. Each row is a state a user can be
in, and each call site is asked what it does there — because the defect was
never in the predicate, it was in who did not call it.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import types
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()


def _video(**item):
    """A stand-in for the queue's current item.

    `_item_is_audio` reads metadata off `video.item` and nothing else, so
    this models exactly the field it consults -- and deliberately carries
    BOTH spellings the server uses, because the predicate accepts either.
    """
    return types.SimpleNamespace(item=dict(item))


#: The states a user can actually be in, and whether the library is on screen.
#: `None` is browse; the rest are things playing.
STATES = (
    ("browse, nothing playing", None, True),
    ("a film playing", _video(MediaType="Video", Type="Movie"), False),
    ("an episode playing", _video(MediaType="Video", Type="Episode"), False),
    ("music playing", _video(MediaType="Audio", Type="Audio"), True),
    ("an audiobook playing", _video(MediaType="Audio", Type="AudioBook"),
     True),
    # The server is not consistent about which field carries it, and the
    # predicate accepts either. A stand-in that modelled only one would make
    # the other branch unreachable while reporting a pass.
    ("an item typed Audio with no MediaType", _video(Type="Audio"), True),
    ("an item whose MediaType is Audio only", _video(MediaType="Audio"),
     True),
    # A photo is a picture, so it takes the library away like a film does.
    ("a photo on screen", _video(MediaType="Photo", Type="Photo"), False),
)


def _pm(video):
    """A PlayerManager with no mpv behind it.

    `__new__`, not `__init__`: the constructor ends in `_init_mpv()` and
    would open a window. Same idiom as `test_auth_header_truth_table`.
    """
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._video = video
    return pm


class LibraryShowingTest(unittest.TestCase):
    def test_the_table(self):
        for label, video, expected in STATES:
            with self.subTest(state=label):
                self.assertIs(
                    _pm(video)._library_showing(), expected,
                    "%s: _library_showing() should be %s" % (label, expected))

    def test_it_is_not_the_same_question_as_video_is_none(self):
        """The premise of every row below, stated once.

        If these two ever agree everywhere, this file is testing nothing:
        the whole point is that the cheap spelling differs from the correct
        one on the audio rows, and a fixture that lost its audio states
        would make every assertion here pass against `_video is None`.
        """
        differ = [label for label, video, expected in STATES
                  if (video is None) != expected]
        self.assertTrue(
            differ,
            "no state distinguishes _library_showing() from `_video is "
            "None`, so these tests would pass against the bug they exist "
            "for")


class NavBackTest(unittest.TestCase):
    """The shipped defect, at the call site that shipped it."""

    def _back(self, video):
        calls = []
        pm = _pm(video)
        pm.mpvtk_active = True
        pm.on_nav_back = lambda: calls.append(1) or True
        return pm._nav_back(), calls

    def test_back_works_while_music_plays(self):
        for label, video, showing in STATES:
            if not showing:
                continue
            with self.subTest(state=label):
                ok, calls = self._back(video)
                self.assertTrue(
                    ok and calls,
                    "%s: BACK was refused while the library was on screen "
                    "-- this is the bug that killed the mouse's back button "
                    "for the whole of music playback" % label)

    def test_back_is_refused_over_a_picture(self):
        """The other direction, and it is not symmetric politeness: over a
        video BACK belongs to the player, and handing it to the browser
        would navigate the library nobody can see."""
        for label, video, showing in STATES:
            if showing:
                continue
            with self.subTest(state=label):
                ok, calls = self._back(video)
                self.assertFalse(
                    ok or calls,
                    "%s: BACK reached the browser over a picture" % label)


class NavCommandTest(unittest.TestCase):
    def _command(self, video, action):
        calls = []
        pm = _pm(video)
        pm.mpvtk_active = True
        pm.on_nav_command = lambda a: calls.append(a) or True
        return pm._nav_command(action), calls

    def test_a_nav_command_follows_the_same_rule(self):
        for label, video, showing in STATES:
            with self.subTest(state=label):
                ok, calls = self._command(video, "back")
                self.assertIs(bool(ok and calls), showing,
                              "%s: a nav command did not follow the library"
                              % label)

    def test_home_is_the_exception_and_gets_through_anyway(self):
        """`_NAV_COMMANDS_WHILE_PLAYING`. Home means "leave this and show me
        the library", so refusing it during playback would make it the one
        button with no way to undo the state it is trying to leave."""
        for label, video, _showing in STATES:
            with self.subTest(state=label):
                ok, calls = self._command(video, "home")
                self.assertTrue(ok and calls,
                                "%s: home was refused" % label)


class LibraryHasInputTest(unittest.TestCase):
    """`_library_has_input` is `_library_showing()` AND the renderer owning
    input -- a summoned HUD owns input too, so the second half cannot be
    dropped and the first half cannot be skipped."""

    def _has_input(self, video, mpvtk_input):
        pm = _pm(video)
        pm._mpvtk_input_active = lambda: mpvtk_input
        return pm._library_has_input()

    def test_it_needs_both_halves(self):
        for label, video, showing in STATES:
            with self.subTest(state=label):
                self.assertIs(self._has_input(video, True), showing,
                              "%s: with the renderer holding input" % label)
                self.assertFalse(
                    self._has_input(video, False),
                    "%s: the renderer does not hold input, so the library "
                    "does not either" % label)


class _FakeWindow:
    """Enough mpv for `show_picture`, recording what it was told."""

    def __init__(self):
        self.loaded = []
        self.keepaspect = False
        self.osd_width, self.osd_height = 1280, 720
        self.window_maximized = False
        self.fullscreen = False
        self.image_display_duration = 1
        self.keep_open = False
        self.fs = False

    def command(self, *args):
        if args and args[0] == "loadfile":
            self.loaded.append(args[1])


def _window_pm(video):
    import threading

    pm = _pm(video)
    # `__new__` skips `__init__`, so the RLock the @synchronous methods take
    # does not exist yet. A real one, not a stub: re-entrancy across these
    # methods is load-bearing (show_picture calls stop, which is synchronous).
    pm._lock = threading.RLock()
    pm._player = _FakeWindow()
    pm._mpv_alive = True
    pm._loading = False
    pm._geometry_armed = "1280x720"
    pm._showing_browse_bg = True
    pm._suspend_shaders_for_still = lambda: None
    pm.stopped = []
    pm.stop = lambda *a, **k: pm.stopped.append(1)
    return pm


class ShowPictureTest(unittest.TestCase):
    """A comic page needs mpv's video output, and `show_picture` decides
    whether it may have it.

    The guard used to be `self._video is not None` -- the wrong question for
    the one type that plays without taking the library away. A comic opened
    from behind the now-playing bar was refused **silently**: the page sets
    `route["_showing"]` regardless, which disarms its own self-repair, so the
    reader drew its top bar, its page counter and its Next/Prev buttons, and
    never a page.

    That this went unnoticed is a fake-shaped hole, not an oversight of
    testing: `tests/_shell_harness.py`'s stand-in is
    ``def show_picture(self, path): self.pictures.append(path); return True``
    -- it has no `_video` at all, so the refusal could not happen in any
    shell test. The review question for a new fake, applied backwards.
    """

    def test_a_page_opens_with_nothing_playing(self):
        pm = _window_pm(None)
        self.assertTrue(pm.show_picture("/tmp/page.png"))
        self.assertEqual(pm._player.loaded, ["/tmp/page.png"])
        self.assertEqual(pm.stopped, [], "nothing was playing to stop")

    def test_a_page_stops_the_music_and_takes_the_window(self):
        """One mpv, one file: the page cannot share the window with a track,
        so opening a comic stops the music [iw]. The refusal was neither --
        it left the music playing AND showed no page."""
        for label, video, showing in STATES:
            if not showing or video is None:
                continue      # the audio rows
            with self.subTest(state=label):
                pm = _window_pm(video)
                self.assertTrue(
                    pm.show_picture("/tmp/page.png"),
                    "%s: the reader was refused the window" % label)
                self.assertEqual(pm._player.loaded, ["/tmp/page.png"],
                                 "%s: no page reached mpv" % label)
                self.assertEqual(pm.stopped, [1],
                                 "%s: the music kept playing under the page"
                                 % label)

    def test_a_video_still_keeps_the_window(self):
        """The other direction, and it is not symmetry for its own sake: a
        film owns the window, so the library is not on screen to reach a
        comic from in the first place."""
        for label, video, showing in STATES:
            if showing:
                continue
            with self.subTest(state=label):
                pm = _window_pm(video)
                self.assertFalse(pm.show_picture("/tmp/page.png"),
                                 "%s: a picture took the window from a "
                                 "video" % label)
                self.assertEqual(pm._player.loaded, [])
                self.assertEqual(pm.stopped, [])


class FullscreenPersistTest(unittest.TestCase):
    """Which settings key a persisted fullscreen toggle lands in.

    `set_fullscreen`'s docstring states the rule -- "browsing writes
    browser_fullscreen, playback writes fullscreen. They're separate settings
    precisely because people want different answers for the two" -- and the
    line below it asked `_video is not None`. So a toggle made while music
    played was stored as the VIDEO preference, which silently armed or
    disarmed auto-fullscreen for the next film.
    """

    def _persist(self, video, enabled):
        from jellyfin_mpv_shim.conf import settings

        pm = _window_pm(video)
        before = (settings.fullscreen, settings.browser_fullscreen,
                  settings.save)
        settings.save = lambda *a, **k: None
        settings.fullscreen = settings.browser_fullscreen = not enabled

        def restore():
            (settings.fullscreen, settings.browser_fullscreen,
             settings.save) = before
        self.addCleanup(restore)
        pm.set_fullscreen(enabled, persist=True)
        return settings.fullscreen, settings.browser_fullscreen

    def test_the_key_follows_what_is_on_screen(self):
        for label, video, showing in STATES:
            with self.subTest(state=label):
                video_key, browser_key = self._persist(video, True)
                if showing:
                    self.assertTrue(browser_key,
                                    "%s: the library's own preference was "
                                    "not recorded" % label)
                    self.assertFalse(
                        video_key,
                        "%s: this wrote the VIDEO setting, so the next film "
                        "goes fullscreen unasked (or stops doing so)" % label)
                else:
                    self.assertTrue(video_key, "%s" % label)
                    self.assertFalse(browser_key, "%s" % label)
