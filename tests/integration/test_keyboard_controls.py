"""Keyboard-shortcut routing tests against the real PlayerManager singleton.

``build_player`` bypasses ``__init__``, so it has *no* key bindings and no menu.
The bindings we care about are registered in ``PlayerManager._init_mpv`` via
``@self._player.on_key_press(...)`` / the ``keypress(settings.kb_*)`` wrapper. So
these tests drive the **module-level singleton** ``player.playerManager`` — its
``_player`` is a :class:`FakeMPV` whose ``_key_bindings`` were populated at
construction, and its ``menu`` is a real ``OSDMenu``.

A key is fired with ``pm._player.press_key(key)``. Most handlers ``put_task(...)``
onto ``pm.evt_queue`` (the action thread would drain them); we assert the queued
task without executing it. The handlers that call a collaborator directly
(``toggle_pause``, ``menu.menu_action``, seeking, …) are checked by stubbing that
collaborator with a recorder.

The bug class these guard against is a **mis-wired binding**: a key that fires the
wrong handler, or a menu/seek key that ignores the menu-shown state. Each test
asserts a key hits the *right* handler and that no handler raises.

NOTE: ``settings.kb_debug`` is gone (#16). It used to be excluded from the
full-sweep test because its handler called ``pdb.set_trace()`` and would hang
the run — which is most of why a released build should not have carried it.

The singleton is shared and cached across tests, so every test resets the mutable
state it depends on (menu hidden, ``_video`` present, ``do_not_handle_pause`` /
``is_in_intro`` cleared, queue drained) in ``setUp`` and ``tearDown``.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402


player = h.import_player_with_fake_mpv()
settings = player.settings


class FakeVideo:
    """The minimum a key handler touches off ``pm._video``."""

    def __init__(self):
        self.item_id = "v"
        self.client = None            # get_seek_times short-circuits to defaults
        self.parent = mock.Mock(has_next=True, has_prev=True)

    def get_current_intro(self, _t):
        return False, None


def _drain(pm):
    """Pull every queued task out of evt_queue without running it, returning the
    list of (func, args) entries."""
    out = []
    while not pm.evt_queue.empty():
        out.append(tuple(pm.evt_queue.get()))
    return out


def _queued_names(pm):
    return [getattr(f, "__name__", f) for f, _a in _drain(pm)]


class KeyboardRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pm = player.playerManager

    def setUp(self):
        pm = self.pm
        pm.menu.is_menu_shown = False
        pm.do_not_handle_pause = False
        pm.is_in_intro = False
        pm._video = FakeVideo()
        _drain(pm)  # start clean

    def tearDown(self):
        _drain(self.pm)
        self.pm.menu.is_menu_shown = False

    # -- stop / next / prev (queued) ---------------------------------------

    def test_stop_keys_queue_stop_and_close(self):
        # kb_stop and the symbolic STOP / CLOSE_WIN all wire to the same handler.
        for key in (settings.kb_stop, "STOP", "CLOSE_WIN"):
            _drain(self.pm)
            self.pm._player.press_key(key)
            self.assertEqual(_queued_names(self.pm), ["stop_and_close"],
                             "key %r did not queue stop_and_close" % key)

    def test_next_and_prev_keys_queue_play_next_prev(self):
        self.pm._player.press_key(settings.kb_next)
        self.assertEqual(_queued_names(self.pm), ["play_next"])
        self.pm._player.press_key(settings.kb_prev)
        self.assertEqual(_queued_names(self.pm), ["play_prev"])

    def test_watched_unwatched_keys_queue_the_right_task(self):
        self.pm._player.press_key(settings.kb_watched)
        self.assertEqual(_queued_names(self.pm), ["watched_skip"])
        self.pm._player.press_key(settings.kb_unwatched)
        self.assertEqual(_queued_names(self.pm), ["unwatched_quit"])

    # -- media keys: seek vs. play_next depending on media_key_seek --------

    def test_media_keys_play_next_prev_when_seek_disabled(self):
        # media_key_seek defaults False -> NEXT/PREV behave like next/prev track.
        with mock.patch.object(settings, "media_key_seek", False):
            for key in ("NEXT", "XF86_NEXT"):
                _drain(self.pm)
                self.pm._player.press_key(key)
                self.assertEqual(_queued_names(self.pm), ["play_next"],
                                 "media key %r should queue play_next" % key)
            for key in ("PREV", "XF86_PREV"):
                _drain(self.pm)
                self.pm._player.press_key(key)
                self.assertEqual(_queued_names(self.pm), ["play_prev"],
                                 "media key %r should queue play_prev" % key)

    def test_media_keys_seek_when_seek_enabled(self):
        # media_key_seek True -> NEXT/PREV seek forward/back and queue nothing.
        with mock.patch.object(settings, "media_key_seek", True), \
                mock.patch.object(self.pm, "seek") as seek, \
                mock.patch.object(self.pm, "get_seek_times",
                                  return_value=(-15.0, 30.0)):
            self.pm.is_in_intro = False
            self.pm._player.press_key("NEXT")
            seek.assert_called_once_with(30.0)
            self.assertEqual(_drain(self.pm), [])

            seek.reset_mock()
            self.pm._player.press_key("PREV")
            seek.assert_called_once_with(-15.0)
            self.assertEqual(_drain(self.pm), [])

    def test_media_next_skips_intro_when_in_intro(self):
        """skip_intro_on_seek is OFF by default, so it has to be turned on
        here -- without the patch this asserted the default's behaviour and
        called it the opposite. See the counter-test below."""
        with mock.patch.object(settings, "media_key_seek", True), \
                mock.patch.object(settings, "skip_intro_on_seek", True), \
                mock.patch.object(self.pm, "skip_intro") as skip, \
                mock.patch.object(self.pm, "seek") as seek:
            self.pm.is_in_intro = True
            self.pm._player.press_key("NEXT")
            skip.assert_called_once_with()
            seek.assert_not_called()

    def test_media_next_seeks_through_an_intro_when_the_setting_is_off(self):
        """The default. Seek-to-skip hijacks a seek the user asked for, so it
        is opt-in: with it off, NEXT seeks past the intro like any other
        moment in the file rather than jumping to its end."""
        with mock.patch.object(settings, "media_key_seek", True), \
                mock.patch.object(settings, "skip_intro_on_seek", False), \
                mock.patch.object(self.pm, "skip_intro") as skip, \
                mock.patch.object(self.pm, "seek") as seek, \
                mock.patch.object(self.pm, "get_seek_times",
                                  return_value=(-15.0, 30.0)):
            self.pm.is_in_intro = True
            self.pm._player.press_key("NEXT")
            skip.assert_not_called()
            seek.assert_called_once_with(30.0)

    # -- pause: toggle when menu hidden, confirm when menu shown ------------

    def test_pause_toggles_playback_when_menu_hidden(self):
        with mock.patch.object(self.pm, "toggle_pause") as toggle, \
                mock.patch.object(self.pm.menu, "menu_action") as menu_action:
            self.pm.menu.is_menu_shown = False
            self.pm._player.press_key(settings.kb_pause)
            toggle.assert_called_once_with()
            menu_action.assert_not_called()

    def test_pause_confirms_selection_when_menu_shown(self):
        with mock.patch.object(self.pm, "toggle_pause") as toggle, \
                mock.patch.object(self.pm.menu, "menu_action") as menu_action:
            self.pm.menu.is_menu_shown = True
            self.pm._player.press_key(settings.kb_pause)
            menu_action.assert_called_once_with("ok")
            toggle.assert_not_called()

    # -- menu open/close ----------------------------------------------------

    def test_menu_key_shows_then_hides_menu(self):
        with mock.patch.object(self.pm.menu, "show_menu") as show, \
                mock.patch.object(self.pm.menu, "hide_menu") as hide:
            self.pm.menu.is_menu_shown = False
            self.pm._player.press_key(settings.kb_menu)
            show.assert_called_once_with()
            hide.assert_not_called()

            show.reset_mock()
            self.pm.menu.is_menu_shown = True
            self.pm._player.press_key(settings.kb_menu)
            hide.assert_called_once_with()
            show.assert_not_called()

    def test_menu_key_opens_the_hud_menu_under_the_mpvtk_osc(self):
        with mock.patch.object(self.pm, "_osc_style_resolved", "mpvtk"), \
                mock.patch.object(self.pm, "on_hud_menu") as hud, \
                mock.patch.object(self.pm.menu, "show_menu") as show:
            self.pm._player.press_key(settings.kb_menu)
            hud.assert_called_once_with()
            show.assert_not_called()

    def test_menu_key_never_reaches_the_osd_menu_under_the_mpvtk_osc(self):
        """The OSD menu is a classic-OSC surface: it draws under the mpvtk
        overlay bitmaps and takes the arrow keys off the browser. So when the
        HUD declines — or there is no video at all to have a HUD — the key
        does nothing rather than falling through."""
        cases = (
            ("hud declines", FakeVideo(), mock.Mock(return_value=False)),
            ("no video", None, mock.Mock(return_value=True)),
            ("no hud wired", FakeVideo(), None),
        )
        for label, video, hud in cases:
            with self.subTest(label):
                with mock.patch.object(self.pm, "_osc_style_resolved", "mpvtk"), \
                        mock.patch.object(self.pm, "_video", video), \
                        mock.patch.object(self.pm, "on_hud_menu", hud), \
                        mock.patch.object(self.pm.menu, "show_menu") as show, \
                        mock.patch.object(self.pm.menu, "hide_menu") as hide:
                    self.pm._player.press_key(settings.kb_menu)
                    show.assert_not_called()
                    hide.assert_not_called()

    def test_menu_key_still_opens_the_osd_menu_under_the_classic_osc(self):
        for style in ("mpv", "default", None):
            with self.subTest(style):
                with mock.patch.object(self.pm, "_osc_style_resolved", style), \
                        mock.patch.object(self.pm, "on_hud_menu") as hud, \
                        mock.patch.object(self.pm.menu, "show_menu") as show:
                    self.pm.menu.is_menu_shown = False
                    self.pm._player.press_key(settings.kb_menu)
                    show.assert_called_once_with()
                    hud.assert_not_called()

    def test_menu_key_ignored_while_loading(self):
        # do_not_handle_pause guards against opening the menu mid-load.
        with mock.patch.object(self.pm.menu, "show_menu") as show:
            self.pm.do_not_handle_pause = True
            self.pm._player.press_key(settings.kb_menu)
            show.assert_not_called()

    # -- nav keys: menu action when shown, seek when hidden ----------------

    def test_nav_keys_drive_menu_when_shown(self):
        cases = {
            settings.kb_menu_left: "left",
            settings.kb_menu_right: "right",
            settings.kb_menu_up: "up",
            settings.kb_menu_down: "down",
        }
        with mock.patch.object(self.pm.menu, "menu_action") as menu_action, \
                mock.patch.object(self.pm, "kb_seek") as kb_seek:
            self.pm.menu.is_menu_shown = True
            for key, action in cases.items():
                menu_action.reset_mock()
                self.pm._player.press_key(key)
                menu_action.assert_called_once_with(action)
            kb_seek.assert_not_called()

    def test_nav_keys_seek_when_menu_hidden(self):
        with mock.patch.object(self.pm, "kb_seek") as kb_seek, \
                mock.patch.object(self.pm.menu, "menu_action") as menu_action:
            self.pm.menu.is_menu_shown = False
            self.pm.is_in_intro = False
            for key, action in ((settings.kb_menu_left, "left"),
                                (settings.kb_menu_right, "right"),
                                (settings.kb_menu_up, "up"),
                                (settings.kb_menu_down, "down")):
                kb_seek.reset_mock()
                self.pm._player.press_key(key)
                kb_seek.assert_called_once_with(action)
            menu_action.assert_not_called()

    def test_right_and_up_skip_intro_when_in_intro_and_menu_hidden(self):
        """Opt-in -- see the media-key pair above for why the patch is here."""
        with mock.patch.object(settings, "skip_intro_on_seek", True), \
                mock.patch.object(self.pm, "skip_intro") as skip, \
                mock.patch.object(self.pm, "kb_seek") as kb_seek:
            self.pm.menu.is_menu_shown = False
            self.pm.is_in_intro = True
            for key in (settings.kb_menu_right, settings.kb_menu_up):
                skip.reset_mock()
                self.pm._player.press_key(key)
                skip.assert_called_once_with()
            kb_seek.assert_not_called()

    def test_right_and_up_seek_through_an_intro_when_the_setting_is_off(self):
        """The default: an intro does not make the seek keys mean something
        else. Both keys, because they route through separate handlers
        (_on_menu_right / _on_menu_up) that each carry their own copy of the
        condition."""
        with mock.patch.object(settings, "skip_intro_on_seek", False), \
                mock.patch.object(self.pm, "skip_intro") as skip, \
                mock.patch.object(self.pm, "kb_seek") as kb_seek:
            self.pm.menu.is_menu_shown = False
            self.pm.is_in_intro = True
            for key, action in ((settings.kb_menu_right, "right"),
                                (settings.kb_menu_up, "up")):
                kb_seek.reset_mock()
                self.pm._player.press_key(key)
                kb_seek.assert_called_once_with(action)
            skip.assert_not_called()

    def test_ok_key_confirms_a_shown_menu_and_opens_nothing(self):
        # It used to forward regardless of menu state, and menu_action("ok")
        # on a hidden menu is show_menu() -- so ENTER *opened* the OSD menu.
        # [iw]: "ENTER doesn't need to open the menu, `c` is fine for that."
        with mock.patch.object(self.pm.menu, "menu_action") as menu_action:
            self.pm.menu.is_menu_shown = False
            self.pm._player.press_key(settings.kb_menu_ok)
            menu_action.assert_not_called()
            self.pm.menu.is_menu_shown = True
            self.pm._player.press_key(settings.kb_menu_ok)
            menu_action.assert_called_once_with("ok")

    def test_esc_key_backs_out_menu_or_leaves_fullscreen(self):
        with mock.patch.object(self.pm.menu, "menu_action") as menu_action:
            self.pm.menu.is_menu_shown = True
            self.pm._player.press_key(settings.kb_menu_esc)
            menu_action.assert_called_once_with("back")

        # Menu hidden: esc drops fullscreen instead.
        self.pm.menu.is_menu_shown = False
        self.pm.fullscreen_disable = False
        self.pm._player.commands.clear()
        self.pm._player.press_key(settings.kb_menu_esc)
        self.assertIn(("set", "fullscreen", "no"), self.pm._player.commands)
        self.assertTrue(self.pm.fullscreen_disable)

    # -- fullscreen ---------------------------------------------------------

    def test_fullscreen_key_toggles_fullscreen(self):
        with mock.patch.object(self.pm, "toggle_fullscreen") as toggle:
            self.pm._player.press_key(settings.kb_fullscreen)
            toggle.assert_called_once_with()

    # -- mis-wiring / crash sweep ------------------------------------------

    def test_no_binding_raises_on_press(self):
        # Press every registered binding (both menu-shown and menu-hidden) and
        # assert none raises. Nothing is skipped any more: kb_debug used to
        # be, because its handler called pdb.set_trace() and would hang the
        # run -- which is also why it is gone (#16; [iw]: "isn't needed
        # anymore, I haven't used it in literal years").
        pm = self.pm
        keys = list(pm._player._key_bindings)
        self.assertIn(settings.kb_stop, keys)   # sanity: bindings exist
        self.assertFalse(hasattr(settings, "kb_debug"),
                         "the pdb binding is gone; nothing should re-add it")

        # Stub the collaborators that would otherwise pause playback / sleep /
        # touch the profile manager, so the sweep only checks the routing layer.
        # settings.save() (fired by kb_kill_shader) needs a real config path,
        # which the fake-mpv harness doesn't set — mock it out; it's an
        # environment concern, not part of the routing under test.
        with mock.patch.object(pm.menu, "show_menu"), \
                mock.patch.object(pm.menu, "hide_menu"), \
                mock.patch.object(pm.menu, "menu_action"), \
                mock.patch.object(pm, "toggle_pause"), \
                mock.patch.object(pm, "toggle_fullscreen"), \
                mock.patch.object(pm, "kb_seek"), \
                mock.patch.object(pm, "seek"), \
                mock.patch.object(pm, "skip_intro"), \
                mock.patch.object(settings, "save"):
            for shown in (False, True):
                pm.menu.is_menu_shown = shown
                for key in keys:
                    _drain(pm)
                    try:
                        pm._player.press_key(key)
                    except Exception as exc:  # noqa: BLE001
                        self.fail("key %r (menu_shown=%s) raised %r"
                                  % (key, shown, exc))


if __name__ == "__main__":
    unittest.main()


class RemoteMenuCommandTest(unittest.TestCase):
    """GoHome / GoToSettings reach the in-window browser's real pages, and
    keep their historical "open the OSD menu" meaning everywhere else."""

    def _player(self, mpvtk=False, video=None):
        pm = h.build_player(player)
        pm._mpv_alive = True
        pm.mpvtk_active = mpvtk
        pm._video = video
        pm.handled = []
        pm.on_nav_command = lambda name: (pm.handled.append(name) or True)
        return pm

    def test_settings_reaches_the_browser(self):
        pm = self._player(mpvtk=True)
        pm.menu_action("settings")
        self.assertEqual(pm.handled, ["settings"])
        self.assertEqual(pm.menu.actions, [])

    def test_home_reaches_the_browser(self):
        pm = self._player(mpvtk=True)
        pm.menu_action("home")
        self.assertEqual(pm.handled, ["home"])

    def test_without_the_browser_settings_opens_the_osd_menu(self):
        pm = self._player(mpvtk=False)
        pm.on_nav_command = None
        pm.menu_action("settings")
        # The OSD menu is the only settings surface a build with no
        # in-window UI has. It opens through the same toggle the kb_menu
        # key uses (it used to be an aliased kb_seek("home"), which could
        # only ever open it — pressing the cog twice re-showed the root).
        self.assertTrue(pm.menu.is_menu_shown)

    def test_during_playback_settings_opens_the_players_own_menu(self):
        """With no in-window OSC resolved, that is still the OSD menu.
        Under mpvtk it is the HUD's gear instead — see
        test_during_playback_settings_opens_the_hud_menu."""
        pm = self._player(mpvtk=True, video=object())
        pm.menu_action("settings")
        self.assertEqual(pm.handled, [], "browser must not take over mid-play")
        self.assertTrue(pm.menu.is_menu_shown)

    def test_during_playback_settings_opens_the_hud_menu(self):
        """The in-window OSC's gear replaces the OSD menu entirely: OSD
        text draws *under* the mpvtk overlay bitmaps and takes the arrow
        keys with it, so opening it over the HUD is a dead menu behind a
        live one."""
        pm = self._player(mpvtk=True, video=object())
        pm._osc_style_resolved = "mpvtk"
        opened = []
        pm.on_hud_menu = lambda: opened.append(True)
        pm.menu_action("settings")
        self.assertEqual(len(opened), 1)
        self.assertFalse(pm.menu.is_menu_shown)

    def test_during_playback_the_hamburger_opens_the_hud_menu_too(self):
        pm = self._player(mpvtk=True, video=object())
        pm._osc_style_resolved = "mpvtk"
        opened = []
        pm.on_hud_menu = lambda: opened.append(True)
        pm.menu_action("menu")
        self.assertEqual(len(opened), 1)

    def test_search_during_playback_does_nothing(self):
        pm = self._player(mpvtk=True, video=object())
        pm.menu_action("search")
        self.assertEqual(pm.handled, [])
        self.assertEqual(pm.menu.actions, [])
        self.assertFalse(pm.menu.is_menu_shown)

    def test_an_open_osd_menu_wins(self):
        pm = self._player(mpvtk=True)
        pm.menu.is_menu_shown = True
        pm.menu_action("settings")
        self.assertEqual(pm.handled, [])
        self.assertEqual(pm.menu.actions, ["home"])
