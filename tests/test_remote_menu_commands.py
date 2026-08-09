"""The remote's hamburger and search buttons.

jellyfin-web's remote draws both, and this client did not advertise either
in ``SupportedCommands`` — so they were dead buttons rather than missing
ones. What they should *do* differs entirely with what is on screen, and
that is the whole of this file:

    hamburger, browsing    the focused tile's context menu — which is
                           where Play / Queue / Watched / Favorite /
                           Download live, so this is those actions from
                           ten feet away with no second UI for them
    hamburger, playing     the player's settings menu
    cog, browsing          the Settings page
    cog, playing           the player's settings menu
    search, browsing       the cursor in the chrome's search box
    search, playing        nothing at all

"The player's settings menu" is the HUD's gear under the in-window OSC and
the OSD menu otherwise, and that distinction is not cosmetic: the OSD menu
draws as mpv OSD text, which lands *under* the mpvtk overlay bitmaps and
takes the arrow keys with it. Sending the cog there mid-playback is what
this used to do.

Driven through the real ``menu_action`` on an uninitialized PlayerManager
(the ``__new__`` pattern from test_kiosk_fullscreen): the routing IS the
behaviour, and every branch of it is a decision about state that no
integration test would pin as precisely.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.constants import CAPABILITIES  # noqa: E402
from jellyfin_mpv_shim.event_handler import NAVIGATION_DICT  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class _Menu:
    """The legacy OSD menu."""

    def __init__(self, shown=False):
        self.is_menu_shown = shown
        self.actions = []

    def show_menu(self):
        self.is_menu_shown = True

    def hide_menu(self):
        self.is_menu_shown = False

    def menu_action(self, action):
        self.actions.append(action)


class _Audio:
    """A playing track, as PlayerManager._current_is_audio reads one."""

    item = {"MediaType": "Audio"}


class _Player:
    """Enough mpv for the property reads and the keypress."""

    def __init__(self, userdata=None):
        self.keys = []
        self.texts = []
        self._data = userdata or {}

    def command(self, *args):
        if args[0] == "keypress":
            self.keys.append(args[1])
            return None
        if args[0] == "get_property":       # the jsonipc backend's path
            return self._data.get(args[1], False)
        return None

    def _get_property(self, prop):          # the libmpv backend's path
        return self._data.get(prop, False)

    def show_text(self, *a):
        self.texts.append(a)


class RemoteCommandBase(unittest.TestCase):
    def _pm(self, browsing=False, playing=False, osc="mpvtk",
            menu_shown=False, hud_shown=False, audio=False):
        # A summoned HUD owns input exactly as the library does — same
        # bindings, same flag, which is what lets a remote's arrows drive
        # it. So "the renderer has input" is true in both, and anything
        # that means different things in a library and a player has to
        # tell them apart by the video, not by the flag.
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = _Player({"user-data/mpvtk/active": browsing or hud_shown,
                              "user-data/mpvtk/hud": False})
        # Music is the state that breaks the obvious test: it keeps _video
        # SET and keeps the library up, so "is the library on screen?" is
        # not "is _video None?".
        pm._video = _Audio() if audio else (object() if playing else None)
        pm.mpvtk_active = True
        pm._osc_style_resolved = osc
        pm.do_not_handle_pause = False
        pm.menu = _Menu(menu_shown)
        pm.commands = []
        pm.hud_menus = 0
        pm.on_nav_command = lambda name: (pm.commands.append(name)
                                          or True) if browsing else False
        pm.on_nav_back = None
        pm.on_hud_menu = lambda: setattr(pm, "hud_menus", pm.hud_menus + 1)
        pm.kb_seek = lambda action: pm.commands.append("kb_seek:" + action)
        return pm


class TestTheyAreAdvertised(RemoteCommandBase):
    """A command the client does not list is one jellyfin-web will not
    send, however well it is handled here."""

    def test_both_buttons_are_in_the_capabilities(self):
        self.assertIn("ToggleContextMenu", CAPABILITIES["SupportedCommands"])
        self.assertIn("GoToSearch", CAPABILITIES["SupportedCommands"])

    def test_every_advertised_navigation_command_is_routed(self):
        """The dispatch reads NAVIGATION_DICT itself, so translating a
        command and never routing it is no longer possible — that pairing
        used to be a hand-maintained tuple beside the dict."""
        for name in ("ToggleContextMenu", "GoToSearch"):
            self.assertIn(name, NAVIGATION_DICT)
        for command, action in NAVIGATION_DICT.items():
            if command in ("Back", "Select", "MoveUp", "MoveDown",
                           "MoveLeft", "MoveRight"):
                continue
            self.assertIn(command, CAPABILITIES["SupportedCommands"],
                          "%s is routed but never advertised" % command)


class TestTheHamburger(RemoteCommandBase):
    def test_browsing_it_opens_the_focused_tile_s_context_menu(self):
        pm = self._pm(browsing=True)
        pm.menu_action("menu")
        self.assertEqual(pm._player.keys, ["MENU"])
        self.assertEqual(pm.hud_menus, 0)
        self.assertFalse(pm.menu.is_menu_shown, "the OSD menu came up")

    def test_playing_it_opens_the_hud_menu(self):
        pm = self._pm(playing=True)
        pm.menu_action("menu")
        self.assertEqual(pm.hud_menus, 1)
        self.assertFalse(pm.menu.is_menu_shown,
                         "the OSD menu drew under the HUD's overlays")
        self.assertEqual(pm._player.keys, [])

    def test_with_no_in_window_osc_it_falls_back_to_the_osd_menu(self):
        pm = self._pm(playing=True, osc="slimbox")
        pm.menu_action("menu")
        self.assertTrue(pm.menu.is_menu_shown)
        self.assertEqual(pm.hud_menus, 0)

    def test_it_toggles_the_osd_menu_rather_than_re_showing_it(self):
        """Same as the kb_menu key. Re-showing the root of a menu that is
        already open reads as the button having done nothing."""
        pm = self._pm(playing=True, osc="slimbox", menu_shown=True)
        pm.menu_action("menu")
        self.assertFalse(pm.menu.is_menu_shown)

    def test_with_the_hud_summoned_it_is_still_the_settings_menu(self):
        """The trap: a summoned HUD sets the same input flag the library
        does, so "the renderer has input" reads as "the library is up"
        over a playing video — and the hamburger would send a context-menu
        keypress at a HUD, where no node has one. A dead button."""
        pm = self._pm(playing=True, hud_shown=True)
        pm.menu_action("menu")
        self.assertEqual(pm.hud_menus, 1)
        self.assertEqual(pm._player.keys, [])

    def test_the_keyboard_menu_key_matches_the_remote(self):
        browsing = self._pm(browsing=True)
        browsing._on_menu_key()
        self.assertEqual(browsing._player.keys, ["MENU"])
        playing = self._pm(playing=True, hud_shown=True)
        playing._on_menu_key()
        self.assertEqual(playing.hud_menus, 1)
        self.assertEqual(playing._player.keys, [])

    def test_mid_load_it_says_to_wait_rather_than_opening_anything(self):
        pm = self._pm(playing=True)
        pm.do_not_handle_pause = True
        pm.menu_action("menu")
        self.assertEqual(pm.hud_menus, 0)
        self.assertTrue(pm._player.texts)


class TestTheCog(RemoteCommandBase):
    def test_browsing_it_still_opens_the_settings_page(self):
        pm = self._pm(browsing=True)
        pm.menu_action("settings")
        self.assertEqual(pm.commands, ["settings"])
        self.assertEqual(pm.hud_menus, 0)

    def test_playing_it_opens_the_hud_menu_not_the_osd_menu(self):
        """The regression this fixes: it went to kb_seek("home"), which
        drew the OSD menu under the overlay bitmaps and took the arrows."""
        pm = self._pm(playing=True)
        pm.menu_action("settings")
        self.assertEqual(pm.hud_menus, 1)
        self.assertFalse(pm.menu.is_menu_shown)
        self.assertEqual(pm.commands, [])

    def test_playing_without_the_hud_it_opens_the_osd_menu(self):
        pm = self._pm(playing=True, osc="slimbox")
        pm.menu_action("settings")
        self.assertTrue(pm.menu.is_menu_shown)

    def test_with_the_hud_summoned_it_is_the_hud_menu(self):
        pm = self._pm(playing=True, hud_shown=True)
        pm.menu_action("settings")
        self.assertEqual(pm.hud_menus, 1)


class TestSearch(RemoteCommandBase):
    def test_browsing_it_goes_to_the_browser(self):
        pm = self._pm(browsing=True)
        pm.menu_action("search")
        self.assertEqual(pm.commands, ["search"])

    def test_playing_it_does_nothing_at_all(self):
        """Not "nothing visible" — nothing. Falling through to the OSD
        fallback is how a no-op becomes a menu over the video."""
        pm = self._pm(playing=True)
        pm.menu_action("search")
        self.assertEqual(pm.commands, [])
        self.assertEqual(pm.hud_menus, 0)
        self.assertFalse(pm.menu.is_menu_shown)
        self.assertEqual(pm._player.keys, [])

    def test_it_does_not_reach_the_osd_menu_even_when_that_is_open(self):
        pm = self._pm(playing=True, osc="slimbox", menu_shown=True)
        pm.menu_action("search")
        self.assertEqual(pm.menu.actions, [])


class TestWhileMusicPlays(RemoteCommandBase):
    """Music keeps the library on screen — that is what the now-playing bar
    is for — while still holding `_video`. Every "is the library up?" test
    written as `_video is None` therefore answers no while the user is
    looking straight at it, and these buttons go dead in the one playback
    state where the library is still there to use them on."""

    def _pm_music(self):
        return self._pm(browsing=True, audio=True)

    def test_the_hamburger_still_opens_the_context_menu(self):
        pm = self._pm_music()
        pm.menu_action("menu")
        self.assertEqual(pm._player.keys, ["MENU"])
        self.assertEqual(pm.hud_menus, 0,
                         "went to the HUD, which has no state during audio")

    def test_search_still_reaches_the_browser(self):
        pm = self._pm_music()
        pm.menu_action("search")
        self.assertEqual(pm.commands, ["search"])

    def test_the_cog_still_opens_the_settings_page(self):
        pm = self._pm_music()
        pm.menu_action("settings")
        self.assertEqual(pm.commands, ["settings"])
        self.assertEqual(pm.hud_menus, 0)

    def test_a_video_is_still_a_player(self):
        """The distinction has to cut both ways, or this just deletes the
        guard: a picture on screen is not the library."""
        pm = self._pm(playing=True, hud_shown=True)
        pm.menu_action("menu")
        self.assertEqual(pm.hud_menus, 1)
        self.assertEqual(pm._player.keys, [])

    def test_back_still_navigates_the_library(self):
        """The same mistake, in the one place it survived.

        The mouse's back button rides this: the renderer routes it as a
        synthetic ESC and the player decides. Refusing it for the whole of
        music and audiobook playback made the button dead while the library
        was on screen — and because you could then never go back, FORWARD
        had nothing to return to and looked broken with it.
        """
        pm = self._pm_music()
        backs = []
        pm.on_nav_back = lambda: (backs.append(1) or True)
        self.assertTrue(pm._nav_back(), "BACK was refused during audio")
        self.assertEqual(len(backs), 1)

    def test_back_over_a_video_is_still_the_players(self):
        """A picture IS the player: there the library is not on screen, and
        ESC has to keep meaning "leave fullscreen"."""
        pm = self._pm(playing=True, hud_shown=True)
        pm.on_nav_back = lambda: True
        self.assertFalse(pm._nav_back(),
                         "BACK was taken from the player over a video")

    def test_back_while_idle_still_navigates(self):
        pm = self._pm(browsing=True)
        pm.on_nav_back = lambda: True
        self.assertTrue(pm._nav_back())

    def test_back_needs_a_handler_and_an_active_ui(self):
        pm = self._pm_music()
        pm.on_nav_back = None
        self.assertFalse(pm._nav_back())
        pm.on_nav_back = lambda: True
        pm.mpvtk_active = False
        self.assertFalse(pm._nav_back())


class TestHome(RemoteCommandBase):
    def test_it_reaches_the_browser_over_a_playing_video(self):
        """Home is the way *out* of what is playing. It used to be declined
        while a video was up and land in the OSD-menu fallback, so the one
        button that means "take me back to the library" opened a menu over
        the film instead. The browser stops playback and shows the home
        screen — see tests/test_shell_library.py."""
        pm = self._pm(playing=True, hud_shown=True)
        pm.on_nav_command = lambda name: pm.commands.append(name) or True
        pm.menu_action("home")
        self.assertEqual(pm.commands, ["home"])
        self.assertFalse(pm.menu.is_menu_shown)

    def test_settings_and_search_still_stop_at_the_player(self):
        """The other two are not ways out: opening a library page behind a
        film nobody asked to leave is not what either button means."""
        pm = self._pm(playing=True, hud_shown=True)
        pm.on_nav_command = lambda name: pm.commands.append(name) or True
        pm.menu_action("settings")
        pm.menu_action("search")
        self.assertEqual(pm.commands, [])


class TestTheOtherCommandsAreUnchanged(RemoteCommandBase):
    def test_arrows_still_drive_spatial_navigation(self):
        pm = self._pm(browsing=True)
        for action in ("up", "down", "left", "right", "ok", "back"):
            pm.menu_action(action)
        self.assertEqual(pm._player.keys,
                         ["UP", "DOWN", "LEFT", "RIGHT", "ENTER", "ESC"])

    def test_an_open_osd_menu_still_takes_the_arrows(self):
        pm = self._pm(playing=True, osc="slimbox", menu_shown=True)
        pm.menu_action("up")
        self.assertEqual(pm.menu.actions, ["up"])


class EnterKeyTest(RemoteCommandBase):
    """The physical ENTER key, which is not the remote's Select.

    Select routes through ``menu_action`` (above) and has always known what
    is on screen. ENTER goes straight to ``_on_menu_ok``, which was the one
    handler in its group with no ``is_menu_shown`` guard -- so it did not
    mean "confirm", it meant *open the OSD menu*, because
    ``menu_action("ok")`` on a hidden menu is ``show_menu()``.
    """

    def test_it_confirms_while_the_menu_is_up(self):
        for osc in ("mpvtk", "classic"):
            with self.subTest(osc=osc):
                pm = self._pm(playing=True, osc=osc, menu_shown=True)
                pm._on_menu_ok()
                self.assertEqual(pm.menu.actions, ["ok"])

    def test_it_does_not_open_the_osd_menu_under_mpvtk(self):
        # The exact thing toggle_settings_menu refuses, by the other door:
        # the OSD menu draws as mpv OSD text, lands under the overlay
        # bitmaps, and takes the arrow keys with it. (Under the classic OSC
        # it opens nothing either -- see below -- but this is the case that
        # was actively harmful rather than merely unwanted.)
        pm = self._pm(playing=True, osc="mpvtk")
        pm._on_menu_ok()
        self.assertFalse(pm.menu.is_menu_shown)
        self.assertEqual(pm.menu.actions, [])

    def test_it_does_not_become_a_second_door_to_the_gear(self):
        # Swallowed, not rerouted. mpv binds ENTER to playlist-next and we
        # mean nothing by it during playback; inventing a new gesture is
        # not what removing a hazard looks like.
        pm = self._pm(playing=True, osc="mpvtk")
        pm._on_menu_ok()
        self.assertEqual(pm.hud_menus, 0)

    def test_it_opens_nothing_under_the_classic_osc_either(self):
        # This started as "the classic OSC keeps what it had", which was the
        # conservative reading. [iw] overruled it: "ENTER doesn't need to
        # open the menu, `c` is fine for that." So the rule is one rule.
        pm = self._pm(playing=True, osc="classic")
        pm._on_menu_ok()
        self.assertFalse(pm.menu.is_menu_shown)
        self.assertEqual(pm.menu.actions, [])

    def test_browsing_under_mpvtk_is_also_refused(self):
        # Not only during playback: the library is the case where the OSD
        # menu stealing the arrow keys is most visible.
        pm = self._pm(browsing=True, osc="mpvtk")
        pm._on_menu_ok()
        self.assertFalse(pm.menu.is_menu_shown)


if __name__ == "__main__":
    unittest.main()
