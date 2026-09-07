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

import os
import sys
import types
from unittest import mock
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


class BrowseFullscreenAppliesTest(unittest.TestCase):
    """The OFF direction of `browser_fullscreen`, which never ran during music.

    `_apply_browse_fullscreen` reads::

        if settings.browser_fullscreen or settings.headless:
            self._player.fs = True
        elif not self._video:
            self._player.fs = False

    so ticking the box while a track played went fullscreen and unticking it
    did nothing -- the `elif` is the third site of this rule, and the audit
    that fixed the other two did not reach it because it lives one call below
    the method that was fixed.

    The `elif` was defensible when it was written: `set_fullscreen` persisted
    a music-time toggle as the VIDEO preference, so deferring to "the
    fullscreen the video session chose" was coherent. Fixing that (the
    sibling repair on this branch) is what made this one stale -- during
    music `browser_fullscreen` is now both what a toggle writes and what
    should be applied.
    """

    def _pm_for(self, video):
        pm = _window_pm(video)
        pm._player.fs = True          # start fullscreen, so OFF is observable
        return pm

    def test_unticking_applies_wherever_the_library_is_showing(self):
        from jellyfin_mpv_shim.conf import settings

        for label, video, showing in STATES:
            if not showing:
                continue
            with self.subTest(state=label):
                pm = self._pm_for(video)
                with mock.patch.object(settings, "browser_fullscreen", False), \
                        mock.patch.object(settings, "headless", False):
                    pm._apply_browse_fullscreen()
                self.assertFalse(
                    pm._player.fs,
                    "%s: the library is on screen and browser_fullscreen is "
                    "off, but the window stayed fullscreen" % label)

    def test_ticking_applies_wherever_the_library_is_showing(self):
        """The direction that already worked. Kept because it is the half a
        repair of the other one could break, and because a test that only
        asserts the broken direction cannot tell a fix from a regression."""
        from jellyfin_mpv_shim.conf import settings

        for label, video, showing in STATES:
            if not showing:
                continue
            with self.subTest(state=label):
                pm = _window_pm(video)
                pm._player.fs = False
                with mock.patch.object(settings, "browser_fullscreen", True), \
                        mock.patch.object(settings, "headless", False):
                    pm._apply_browse_fullscreen()
                self.assertTrue(pm._player.fs, "%s: did not go fullscreen"
                                % label)

    def test_a_video_on_screen_is_left_alone(self):
        """The reason the guard exists at all: a film owns the window and
        `settings.fullscreen` is what governs it."""
        from jellyfin_mpv_shim.conf import settings

        for label, video, showing in STATES:
            if showing:
                continue
            with self.subTest(state=label):
                pm = self._pm_for(video)
                with mock.patch.object(settings, "browser_fullscreen", False), \
                        mock.patch.object(settings, "headless", False):
                    pm._apply_browse_fullscreen()
                self.assertTrue(
                    pm._player.fs,
                    "%s: browsing's fullscreen preference was applied to a "
                    "video that owns the window" % label)


class BrowseBackgroundRepaintTest(unittest.TestCase):
    """A live theme change has to repaint mpv's background during music too.

    The colour behind the browser is an mpv *property*; nothing in the scene
    paints the whole window. `refresh_browse_bg` gated on
    `_showing_browse_bg`, which means "we issued a stop and the window is
    painted with nothing loaded" -- and that is never true during music,
    because a track is loaded. But the window is still painted by
    `background-color` (a picture-less audio file, and these are global vo
    options that survive the file change -- `set_browse_window` says so), so
    the flag is being read as "is the browse background visible" when it
    answers something narrower.
    """

    def _pm_for(self, video, showing_bg):
        pm = _window_pm(video)
        pm._showing_browse_bg = showing_bg
        pm._player.background_color = "#000000"
        return pm

    def test_the_background_repaints_wherever_the_library_is_showing(self):
        from jellyfin_mpv_shim import player_window

        for label, video, showing in STATES:
            if not showing:
                continue
            with self.subTest(state=label):
                # False, because that is the state music is actually in: a
                # track is loaded, so nothing ever set this flag.
                pm = self._pm_for(video, showing_bg=(video is None))
                with mock.patch.object(player_window, "BROWSE_BG_HEX",
                                       "#abcdef"):
                    pm.refresh_browse_bg()
                self.assertEqual(
                    pm._player.background_color, "#abcdef",
                    "%s: the library is on screen showing the old theme's "
                    "background" % label)

    def test_a_video_keeps_mpvs_own_background(self):
        """The half that must not change: during playback the background is
        what letterbox bars are painted with, and `browse_yield` deliberately
        puts mpv's own colour back there."""
        from jellyfin_mpv_shim import player_window

        for label, video, showing in STATES:
            if showing:
                continue
            with self.subTest(state=label):
                pm = self._pm_for(video, showing_bg=False)
                with mock.patch.object(player_window, "BROWSE_BG_HEX",
                                       "#abcdef"):
                    pm.refresh_browse_bg()
                self.assertEqual(
                    pm._player.background_color, "#000000",
                    "%s: the browse background was painted behind a video"
                    % label)


class SettingsMenuStaysShutTest(unittest.TestCase):
    """The OSD menu must not open while the library is on screen.

    `toggle_settings_menu`'s own comment states the rule -- the OSD menu
    "lands *under* the mpvtk overlay bitmaps and steals the arrow keys from
    the browser, so it must not open here even when the HUD declines
    (browsing, idle, no video)" -- and then gates on `_video is not None`,
    which is true during music.

    Reachable today despite `browse_block_keys`: the key is only one of the
    two callers. `menu_action` comes from a **remote**, which no key block
    touches, so a cog press on a phone while a track plays opens a menu
    under the library and takes its arrows.
    """

    def _pm_for(self, video):
        pm = _pm(video)
        pm._osc_style_resolved = "mpvtk"
        pm.do_not_handle_pause = False
        pm.opened = []
        pm.on_hud_menu = lambda: pm.opened.append(1)
        return pm

    def test_it_stays_shut_wherever_the_library_is_showing(self):
        for label, video, showing in STATES:
            if not showing:
                continue
            with self.subTest(state=label):
                pm = self._pm_for(video)
                pm.toggle_settings_menu()
                self.assertEqual(
                    pm.opened, [],
                    "%s: the HUD menu opened over the library" % label)

    def test_it_still_opens_over_a_video(self):
        for label, video, showing in STATES:
            if showing:
                continue
            with self.subTest(state=label):
                pm = self._pm_for(video)
                pm.toggle_settings_menu()
                self.assertEqual(pm.opened, [1],
                                 "%s: the gear menu did not open" % label)


class OneAudioRuleTest(unittest.TestCase):
    """"Is this item audio?" is asked at three sites, and two had their own copy.

    `_item_is_audio` is the rule -- ``MediaType == "Audio" or Type ==
    "Audio"`` -- and the server is not consistent about which field carries
    it, which is why the predicate accepts either and why STATES above models
    both. The copies:

    * `player_reporting.push_playstate` inlined the rule verbatim. Not a bug
      by itself, but it is a second authority, and the two would diverge the
      first time either moved.
    * `ItemActions.play` implemented **half** of it -- ``Type == "Audio"``
      only -- so an item carrying `MediaType` and any other `Type` launched
      down the VIDEO branch: `_start(audio=False)` clears `_browsing` and
      hands the window over. An **audiobook** is exactly that shape
      (`Type="AudioBook"`, `MediaType="Audio"`).

    What makes this worth a lint's worth of care rather than two edits: the
    browser decides between browse and HUD mode on this answer, and a
    playstate that says "not audio" while the library is up takes the
    `_yield()` branch -- which puts the renderer in HUD mode with its
    auto-hide armed and the library as the scene it hides.
    """

    def test_the_playstate_flag_agrees_with_the_predicate(self):
        """The reported flag is what the browser routes on, so it is the one
        that has to match -- not the expression that computes it."""
        from jellyfin_mpv_shim.player import _item_is_audio
        from jellyfin_mpv_shim.utils import item_is_audio

        for label, video, _showing in STATES:
            with self.subTest(state=label):
                item = getattr(video, "item", None) or {}
                self.assertEqual(
                    item_is_audio(item), _item_is_audio(video),
                    "%s: the item-level and video-level answers differ"
                    % label)

    def test_an_audiobook_launches_as_audio(self):
        """The half-rule's actual victim, named because `Type` alone reads
        as complete until you meet one."""
        from jellyfin_mpv_shim.utils import item_is_audio

        self.assertTrue(item_is_audio({"Type": "AudioBook",
                                       "MediaType": "Audio"}))
        self.assertTrue(item_is_audio({"MediaType": "Audio"}))
        self.assertTrue(item_is_audio({"Type": "Audio"}))
        self.assertFalse(item_is_audio({"Type": "Movie",
                                        "MediaType": "Video"}))
        self.assertFalse(item_is_audio({}))
        self.assertFalse(item_is_audio(None))

    def test_the_launch_path_uses_it(self):
        """Driven rather than read: asserting that `play` *calls* a helper
        would pass just as well if it passed the wrong item to it."""
        from jellyfin_mpv_shim.mpvtk_browser.item_actions import ItemActions

        for item, expected in (
            ({"Type": "Audio", "Name": "Song"}, True),
            ({"Type": "AudioBook", "MediaType": "Audio", "Name": "Book"},
             True),
            ({"MediaType": "Audio", "Name": "Untyped"}, True),
            ({"Type": "Movie", "MediaType": "Video", "Name": "Film"}, False),
        ):
            with self.subTest(item=item.get("Name")):
                launched = []
                actions = ItemActions(
                    services=types.SimpleNamespace(controller=None,
                                                   source=None),
                    run=None, dialogs=None,
                    on_launch=lambda audio, title: launched.append(audio))
                actions.play(item, server="srv")
                self.assertEqual(
                    [expected], launched,
                    "%r launched down the %s branch"
                    % (item, "video" if expected else "audio"))


class TheCursorNeverHidesOverTheLibraryTest(unittest.TestCase):
    """[iw] "Cursor hiding should never happen when the main UI is visible,
    only the mpvtk HUD."

    That is the visible edge of a worse state, and the whole of the reported
    bug. `set_hud(True)` is a MODE CHANGE: `ui_suspend()` drops the mouse
    section, and the renderer withholds `allow-hide-cursor` from that section
    exactly so the pointer stays alive over a UI
    (docs/mpv-backends.md). So a HUD engaged while the library is on screen

      * hides the cursor -- the tell,
      * auto-hides after `hud_hide_secs`, and `phud_hide` calls
        `ui_suspend`, which takes the LIBRARY off screen with it,
      * and brings it back on motion, because `mouse-pos` is observed
        directly and needs no section.

    Every symptom reported for the video -> music playlist advance, from one
    state. Reproduction: a playlist of one video then one song, skip to the
    next track, stop moving the mouse.

    Enforced in `HudController.engage` rather than at the four call sites
    that already guard it, so a guard read a beat before `_browsing` flips
    cannot get past it.
    """

    def _browser(self):
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "tests"))
        from _shell_harness import FakeSource, HudController, StubHudApp
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.set_app(app)
        return b, app

    def test_an_engage_while_browsing_is_refused(self):
        b, app = self._browser()
        b._browsing = True
        b.hud.state = {"stopped": False, "is_audio": False, "id": "v1"}
        app.calls.clear()
        b.hud.engage()
        self.assertNotIn(
            ("hud", True), app.calls,
            "the HUD was engaged over the library: the cursor now hides, and "
            "the auto-hide takes the library with it")

    def test_playback_still_engages_it(self):
        """The control. A refusal that also refused the real case would take
        the player controls away entirely."""
        b, app = self._browser()
        b._browsing = False
        b.hud.state = {"stopped": False, "is_audio": False, "id": "v1"}
        app.calls.clear()
        b.hud.engage()
        self.assertIn(("hud", True), app.calls,
                      "a video got no HUD at all")

    def test_a_yield_overtaken_by_enter_browse_does_not_engage(self):
        """The actual interleaving, from Izzie's log:

            Player is busy; deferring UI action to the action thread
            window: browse=on <- gateway.playback.on_browse_enter:23
            refusing a HUD engage while browsing <- app._yield:2294

        `_yield` clears `_browsing` FIRST and engages LAST, and the work in
        between is not atomic: `_tell_controller("on_browse_leave")` reaches
        the gateway, `run_action` defers because the player lock is held for
        the whole of a start, and the next queue item -- a song -- brings the
        library back inside that window. The engage that follows is stale by
        the time it runs.

        Not a fifth caller and not a flag read early: one call spanning a
        re-entry. Modelled by re-entering browse from the leave callback,
        which is exactly where the log shows it happening.
        """
        b, app = self._browser()
        b._browsing = True
        b.hud.state = {"stopped": False, "is_audio": False, "id": "v1"}

        real_tell = b._tell_controller

        def overtaking(name):
            real_tell(name)
            if name == "on_browse_leave":
                b.enter_browse()      # the song arrives mid-yield

        b._tell_controller = overtaking
        app.calls.clear()
        b._yield()

        self.assertTrue(b._browsing,
                        "the library did not come back, so this models the "
                        "wrong thing")
        self.assertNotIn(
            ("hud", True), app.calls,
            "the yield's trailing engage landed after browse had been "
            "re-entered, leaving HUD mode over the library")
        self.assertEqual(
            ("active", True), app.calls[-1],
            "the renderer was not left asserted for browse")

    def test_a_handoff_returns_the_hud_to_idle(self):
        """Reported: changing an audio or subtitle track during a transcode
        sometimes yields "to a dead HUD where moving the mouse does not bring
        the player back", intermittently.

        A track change on a transcode deletes and re-creates it, so the
        loading screen comes up and `LoadFeedback.clear()` hands off through
        `_yield()`. The renderer early-returns from `mpvtk-hud yes` when it
        is ALREADY in HUD mode -- which it is, playback never left it -- so
        nothing was re-established. If the bar was still up (the gear menu
        the track was changed in), `phud.shown` stayed true for a stream that
        had ended and summon was never re-bound: the mouse does nothing,
        because the renderer believes it is already showing.

        Intermittent because it depends on the bar still being up when the
        handoff lands; if the auto-hide fired first, `phud_hide` re-binds
        summon and it recovers on its own.

        Measured in tests/lua/: after the second engage the wake binding is
        gone, and a False/True cycle restores it.
        """
        b, app = self._browser()
        b._browsing = False          # playing; the window is already ours
        b.hud.state = {"stopped": False, "is_audio": False, "id": "v1"}
        b.hud.shown = True           # the bar the user changed the track in
        app.calls.clear()
        b._yield()
        self.assertEqual(
            [("hud", False), ("hud", True)],
            [c for c in app.calls if c[0] == "hud"],
            "the handoff re-sent the engage without resetting, so the "
            "renderer kept a shown flag from the stream that ended")
        self.assertFalse(b.hud.shown)

    def test_a_re_send_does_not_hide_a_bar_in_use(self):
        """The control. Only a handoff resets: the other callers are
        re-sends -- a settings change, a SyncPlay join, a fresh renderer --
        and hiding a bar somebody is using would be its own bug."""
        b, app = self._browser()
        b._browsing = False
        b.hud.state = {"stopped": False, "is_audio": False, "id": "v1"}
        b.hud.shown = True
        app.calls.clear()
        b.hud.engage()
        self.assertNotIn(("hud", False), app.calls,
                         "a settings re-push hid the HUD")
        self.assertTrue(b.hud.shown)

    def test_the_reported_sequence_leaves_browse_asserted(self):
        """Video, then the queue advances to a track: whatever order the
        pushes arrive in, the renderer must not be left in HUD mode while
        the library is what is drawn."""
        b, app = self._browser()
        b._browsing = True
        b.on_playstate({"stopped": False, "is_audio": False, "id": "v1"})
        b.on_playstate({"stopped": False, "is_audio": True, "id": "a1"})
        self.assertTrue(b._browsing, "the library is not on screen")
        # A late video push from the timeline thread, built before the
        # advance -- the shape that would re-engage behind the library.
        b.on_playstate({"stopped": False, "is_audio": False, "id": "v1"})
        if b._browsing:
            self.assertNotEqual(
                ("hud", True), app.calls[-1],
                "a late push left the renderer in HUD mode with the library "
                "on screen")


class LaunchingAsAudioTest(unittest.TestCase):
    """The FOURTH copy of "is this audio", and the one a type list cannot fix.

    Reported: playing a music playlist and moving the pointer off the window
    blanked the entire library, with "the flash of it loading the HUD before
    it starts playing" as the tell -- and only for playlists, not plain music
    and not audiobooks.

    `tiles.py` decided with ``t in ("MusicAlbum", "MusicArtist",
    "MusicGenre")``. A music PLAYLIST is in no such list, so it launched down
    the video branch: `_start(audio=False)` clears `_browsing` and yields the
    window, the renderer enters HUD mode, and `phud_hide` -- which fires when
    the pointer leaves -- calls `ui_suspend`. The library is what gets
    suspended, because the library is what is on screen.

    The item knew all along. `Playlist.MediaType` is `PlaylistMediaType`,
    which `PlaylistManager` computes from the contents (Audio or Video), so
    the server had already answered the question the list was guessing at.
    """

    def test_a_music_playlist_launches_as_audio(self):
        from jellyfin_mpv_shim.utils import launches_as_audio

        self.assertTrue(launches_as_audio(
            {"Type": "Playlist", "MediaType": "Audio", "Name": "Mix"}))

    def test_a_video_playlist_does_not(self):
        """The control, and the reason this is not "playlists are audio":
        the same type carries either answer."""
        from jellyfin_mpv_shim.utils import launches_as_audio

        self.assertFalse(launches_as_audio(
            {"Type": "Playlist", "MediaType": "Video", "Name": "Films"}))

    def test_the_music_containers_still_do(self):
        from jellyfin_mpv_shim.utils import launches_as_audio

        for t in ("MusicAlbum", "MusicArtist", "MusicGenre"):
            with self.subTest(type=t):
                self.assertTrue(launches_as_audio({"Type": t}))

    def test_an_audio_item_and_an_audiobook_do(self):
        from jellyfin_mpv_shim.utils import launches_as_audio

        self.assertTrue(launches_as_audio({"Type": "Audio"}))
        self.assertTrue(launches_as_audio({"Type": "AudioBook",
                                           "MediaType": "Audio"}))

    def test_a_music_library_play_all_does(self):
        """The second site: a music library's Play All sent no `audio` at
        all. `CollectionType` is what it has to go on."""
        from jellyfin_mpv_shim.utils import launches_as_audio

        self.assertTrue(launches_as_audio({"Type": "CollectionFolder"},
                                          "music"))
        self.assertTrue(launches_as_audio(
            {"Type": "CollectionFolder", "CollectionType": "music"}))

    def test_video_and_photos_are_untouched(self):
        from jellyfin_mpv_shim.utils import launches_as_audio

        for item in ({"Type": "Movie", "MediaType": "Video"},
                     {"Type": "Episode", "MediaType": "Video"},
                     {"Type": "Photo", "MediaType": "Photo"},
                     {"Type": "BoxSet"},
                     {"Type": "CollectionFolder", "CollectionType": "movies"},
                     {}):
            with self.subTest(item=item):
                self.assertFalse(launches_as_audio(item))

    def test_the_first_queued_entry_outranks_the_container(self):
        """Izzie: a playlist can arrive "looking like a video going into the
        software" and only turn out to be music later. So the container's own
        label cannot be trusted, and the entry that will actually start is
        the answer -- which is also the right answer for a MIXED playlist."""
        from jellyfin_mpv_shim.utils import launches_as_audio

        lying = {"Type": "Playlist", "MediaType": "Video", "Name": "Mix"}
        song = {"Type": "Audio", "MediaType": "Audio"}
        film = {"Type": "Movie", "MediaType": "Video"}
        self.assertTrue(
            launches_as_audio(lying, first=song),
            "a playlist mislabelled as video still launched as video, so it "
            "yields the window and the auto-hide blanks the library")
        self.assertFalse(
            launches_as_audio({"Type": "Playlist", "MediaType": "Audio"},
                              first=film),
            "a mixed playlist starting on a film kept the library up")


class HudStateDoesNotOutliveItsVideoTest(unittest.TestCase):
    """A song after a film must not leave the film's HUD state behind.

    `hud.state` is cleared on a `stopped` push, and a queue advance does not
    always produce one -- the player suppresses the incidental stopped pushes
    a load makes. So advancing from a video straight into a track left the
    HUD holding the film's playstate for the whole song, and
    `reassert_window_state` reads exactly that (`hud.state is not None`) as
    "a video is in flight, re-enter HUD mode" -- which it does the moment mpv
    is re-created under a playing track.
    """

    def _browser(self):
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "tests"))
        from _shell_harness import FakeSource, HudController, StubHudApp
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.set_app(app)
        b._browsing = True
        return b, app

    VIDEO = {"stopped": False, "is_audio": False, "id": "v1"}
    SONG = {"stopped": False, "is_audio": True, "id": "a1"}

    def test_a_renderer_recreate_during_music_stays_in_browse(self):
        """The property, not the mechanism.

        The first version of this asserted `hud.state is None` after the
        advance -- the mechanism -- and the fix that satisfied it wrote
        destructively from a path that races a video start. What actually
        matters is what a fresh renderer is told, so that is what is
        asserted, and any way of getting it right passes.
        """
        b, app = self._browser()
        b.on_playstate(self.VIDEO)
        self.assertIsNotNone(b.hud.state, "the video did not arm the HUD, so "
                                          "this test proves nothing")
        b.on_playstate(self.SONG)
        # Minimized, which is where this actually bites: browsing takes the
        # first branch of reassert_window_state and never reaches the one
        # under test. A first draft asserted from the browsing state and so
        # could not fail -- the mutation that dropped the guard passed it.
        b.minimize()
        app.calls.clear()
        b.reassert_window_state()
        self.assertEqual(
            ("active", False), app.calls[-1],
            "mpv re-created under a playing track re-entered HUD mode from "
            "the film that preceded it")

    def test_the_video_after_the_song_gets_its_hud_back(self):
        """The return leg, which the forward fix must not cost.

        Reported after the first fix landed: "when I go back in the playlist
        back to the video, the HUD stays dismissed and I have no player
        controls at all." Not reproduced here -- at this layer the round trip
        is correct -- so this pins the layer rather than claiming the bug.
        """
        b, app = self._browser()
        b.on_playstate(self.VIDEO)
        b.on_playstate(self.SONG)
        b.on_playstate(self.VIDEO)
        self.assertIsNotNone(b.hud.state,
                             "the returning video armed no HUD state, so "
                             "nothing can draw the bar")
        self.assertFalse(b._browsing, "the window was not yielded to video")
        self.assertIn(("hud", True), app.calls[-3:],
                      "the renderer was never put back into HUD mode")

    def test_a_ticker_push_does_not_wipe_a_video_start(self):
        """The clearing above runs on the TRANSITION, not on every audio
        push. The now-playing ticker sends one a second, and a start is not
        atomic -- `_start(audio=False)` clears `_browsing` and raises the
        loading screen while late audio pushes can still land."""
        b, _app = self._browser()
        b.on_playstate(self.VIDEO)
        b.on_playstate(self.SONG)
        b.on_playstate(self.VIDEO)
        state = b.hud.state
        b.on_playstate(dict(self.SONG))     # a late push, built pre-advance
        self.assertIs(b.hud.state, state,
                      "a late audio push wiped the HUD state of the video "
                      "that had already started -- no player controls")

    def test_the_renderer_stays_in_browse_across_the_advance(self):
        b, app = self._browser()
        b.on_playstate(self.VIDEO)
        b.on_playstate(self.SONG)
        self.assertTrue(b._browsing)
        b.reassert_window_state()
        self.assertEqual(
            ("active", True), app.calls[-1],
            "a re-assert during music put the renderer back into HUD mode")
