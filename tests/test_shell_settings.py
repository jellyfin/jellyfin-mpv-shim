"""Settings, servers, authentication and the log viewer.

Everything reached from Settings that is not downloads: the server list, add
server, login, the PIN lock, offline mode, and the log tail.
"""

import unittest
import time
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    DownloadsController,
    FakeConfig,
    FakeController,
    FakeSource,
    LoginController,
    MultiServerSource,
    _SyncPool,
    build_scene,
    ids,
)


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig()
        self.b = MpvtkBrowser(app=None, source=FakeSource(), config=self.cfg)

    def _advanced(self):
        """Reveal the Advanced section (autoplay/seek_up live there)."""
        self.b.route["_advanced"] = True

    def test_settings_nav_and_render(self):
        self.b._open_settings()
        self.assertEqual(self.b.route["kind"], "settings")
        self._advanced()
        nodes, _h = build_scene(self.b)
        self.assertIn("set-autoplay", ids(nodes))     # bool -> checkbox
        self.assertIn("set-player_name", ids(nodes))  # str -> textbox
        self.assertIn("set-osc_mode", ids(nodes))     # enum -> dropdown

    def test_settings_tabs_present(self):
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        for tab in ("general", "home", "display", "servers", "downloads",
                    "logs"):
            self.assertIn("stab-" + tab, ids(nodes))

    def test_advanced_section_is_collapsed_by_default(self):
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("set-autoplay", ids(nodes))
        self.assertIn("set-adv", ids(nodes))

    def test_setting_note_renders_under_its_setting(self):
        """A note explains the default for the settings that follow it,
        so it has to land between them, not at the end of the group."""
        self.cfg.NOTES = {"osc_mode": "MPV keybinds are used by default."}
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("MPV keybinds are used by default.", texts)
        self.assertLess(texts.index("Osc Mode"),
                        texts.index("MPV keybinds are used by default."))
        self.assertLess(texts.index("MPV keybinds are used by default."),
                        texts.index("Lang"))

    def test_hud_key_settings_sit_under_the_keybind_note(self):
        """The real schema: the two HUD keyboard settings are curated
        into Interface directly below the note explaining the default,
        not buried in the auto-generated Advanced list."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        interface = dict(real.sections())["Interface"]
        self.assertEqual(
            interface[interface.index("osc_style"):][:3],
            ["osc_style", "hud_grab_keys", "hud_wake_key"])
        self.assertIn("osc_style", real.NOTES)
        advanced = dict(real.sections()).get("Advanced", [])
        self.assertNotIn("hud_grab_keys", advanced)
        self.assertNotIn("hud_wake_key", advanced)

    def test_discord_presence_is_a_curated_setting(self):
        """The real schema: it was config-file-only, so the only way to turn
        it on was to know the key existed."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        self.assertIn("discord_presence", dict(real.sections())["Interface"])
        self.assertNotIn("discord_presence",
                         dict(real.sections()).get("Advanced", []))
        self.assertIn("discord_presence", real.NOTES)
        # The Windows build bundles pypresence, so the always-visible note
        # must not name it -- a dependency most users never have to think
        # about reads as something they are missing.
        self.assertNotIn("pypresence", real.NOTES["discord_presence"])

    def test_one_way_doors_are_advanced_only(self):
        """enable_gui reads like "turn off the Jellyfin UI" and actually
        drops the app to CLI mode; headless takes the library away, Settings
        with it. Both leave conf.json as the way back on a machine with no
        tray, so neither belongs one click deep in Interface."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        groups = dict(real.sections())
        for key in ("enable_gui", "headless"):
            self.assertNotIn(key, groups["Interface"])
            self.assertIn(key, groups.get("Advanced", []))
            note = real.NOTES.get(key)
            self.assertIsNotNone(note, "%s hides the way to undo it and says "
                                       "nothing about it" % key)
            self.assertIn("conf.json", note)

    def test_cast_target_setup_is_spelled_out(self):
        """The classic cast-only behaviour is three ordinary settings, and
        nowhere in the app said which — which is how people reached for
        enable_gui to get it. The note rides the keep-running toggle, so it
        is visible whichever of the two this machine shows."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        for key in ("close_to_tray", "allow_background"):
            note = real.NOTES.get(key) or ""
            for label in (real.label_for("start_minimized"),
                          real.label_for("fullscreen")):
                self.assertIn(label, note)
        shown = dict(real.sections())["Interface"]
        self.assertTrue({"close_to_tray", "allow_background"} & set(shown),
                        "the recipe's anchor left the visible section")

    def test_discord_says_so_when_it_is_on_but_did_not_load(self):
        """Ticking the box with pypresence missing did nothing at all, and
        said nothing either: player.py reads the setting once at import and
        only enables the feature if the module imports, logging the failure
        somewhere nobody reads. A setting that silently does nothing is the
        same shape of bug as the pause guard."""
        from jellyfin_mpv_shim.conf import settings

        saved = settings.discord_presence
        available = [False]

        class Ctl:
            def rich_presence_available(self):
                return available[0]

        self.b.controller = Ctl()
        try:
            settings.discord_presence = False
            self.assertIsNone(self.b._dynamic_note("discord_presence"),
                              "warned about a feature that is switched off")
            settings.discord_presence = True
            note = self.b._dynamic_note("discord_presence")
            self.assertIsNotNone(note, "on but not loaded, and no sign of it")
            self.assertIn("pypresence", note)
            available[0] = True
            self.assertIsNone(self.b._dynamic_note("discord_presence"),
                              "warned about a feature that is working")
        finally:
            settings.discord_presence = saved

    def test_the_audio_device_list_comes_from_mpv(self):
        """Which devices exist depends on the platform, the sound server and
        what is plugged in this minute, so the options cannot be a literal in
        config.py the way the other enums are — and mpv, which has to open
        whichever one is chosen, is the only honest source."""
        class Ctl:
            def audio_devices(self):
                return [("Default (mpv decides)", None),
                        ("The S/PDIF one", "alsa/iec958:CARD=X,DEV=0")]

        self.b.controller = Ctl()
        opts = self.b._dynamic_enum("audio_device")
        self.assertEqual([v for _l, v in opts],
                         [None, "alsa/iec958:CARD=X,DEV=0"])
        self.assertIsNone(self.b._dynamic_enum("player_name"),
                          "every other setting must keep its static options")

    def _device_browser(self, long_name=True):
        name = ("SoundBlaster Live! 24-bit External SB0490 Digital Stereo "
                "(IEC958)") if long_name else "Speakers"

        class Ctl:
            def audio_devices(self):
                return [("Default (mpv decides)", None),
                        (name, "alsa/iec958:CARD=X")]

        b = MpvtkBrowser(app=None, source=FakeSource())   # the real schema
        b.controller = Ctl()
        b._open_settings()
        return b

    @staticmethod
    def _dropdown(b, node_id, size=(1280, 720)):
        from jellyfin_mpv_shim.mpvtk.layout import layout

        nodes, _h = layout(b.build(size), *size)
        return next(n for n in nodes
                    if n.get("id") == node_id and n.get("t") == "dropdown")

    def test_the_device_list_opens_wider_than_the_control(self):
        """Device descriptions are system strings and the identifying part is
        at the END — "…External SB0490 Digital Stereo (IEC958)" — so at the
        field width every row ellipsizes to the same thing.

        The OPEN list gets the extra room, not the control: one field wider
        than every other field is what you notice, and it is closed most of
        the time.
        """
        b = self._device_browser()
        dd = self._dropdown(b, "set-audio_device")
        field = b.FIELD_W
        self.assertEqual(dd["w"], field, "the control left the form's grid")
        self.assertGreater(dd.get("pw", 0), field,
                           "the open list is no wider than the control")
        self.assertLessEqual(dd["pw"], field * 1.5)

    def test_a_curated_enum_keeps_one_width(self):
        """Its labels are ours; if they do not fit, the fix is the label."""
        dd = self._dropdown(self._device_browser(), "set-audio_mode")
        self.assertEqual(dd["w"], MpvtkBrowser.FIELD_W)
        self.assertIsNone(dd.get("pw"))

    def test_a_short_device_list_does_not_get_a_wide_popup(self):
        """popup_w is a ceiling, not a width: nothing is gained by opening a
        list of "Speakers" at one and a half fields wide."""
        dd = self._dropdown(self._device_browser(long_name=False),
                            "set-audio_device")
        self.assertEqual(dd["pw"], dd["w"],
                         "a short list still opened oversized")

    def test_a_broken_device_list_does_not_break_the_settings_screen(self):
        """Reading it talks to mpv, which can be mid-restart."""
        class Ctl:
            def audio_devices(self):
                raise RuntimeError("no mpv")

        self.b.controller = Ctl()
        self.assertIsNone(self.b._dynamic_enum("audio_device"))
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        self.assertIn("set-player_name", ids(nodes))

    def test_exclusive_audio_is_hidden_where_mpv_ignores_it(self):
        """mpv honours --audio-exclusive on wasapi, coreaudio and sndio only.
        A checkbox that silently does nothing is worse than no checkbox — and
        it must not leak into Advanced either, which is what the `curated`
        seeding in sections() is for."""
        import sys as _sys
        from unittest import mock

        from jellyfin_mpv_shim.mpvtk_browser import config as real

        with mock.patch.object(_sys, "platform", "linux"):
            groups = dict(real.sections())
            self.assertNotIn("audio_exclusive", groups.get("Audio", []))
            self.assertNotIn("audio_exclusive", groups.get("Advanced", []))
        for plat in ("win32", "darwin"):
            with mock.patch.object(_sys, "platform", plat):
                self.assertIn("audio_exclusive",
                              dict(real.sections()).get("Audio", []),
                              "hidden on %s, where mpv honours it" % plat)

    def test_the_audio_device_setting_is_offered_everywhere(self):
        """Unlike exclusive mode: choosing the hardware device directly is
        precisely what Linux users need, since that is the platform where the
        sound server gets in the way."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        self.assertIn("audio_device", dict(real.sections())["Audio"])
        self.assertIn("audio_device", real.NOTES)

    def test_a_static_note_does_not_hide_the_dynamic_one(self):
        """Both lines render, not one.

        `notes.get(key) or self._dynamic_note(key)` meant giving a setting an
        explanatory line silently switched off its warning — which is how the
        Discord "not active" note shipped in a state where it could never
        appear, no matter what the feature was doing.
        """
        self.cfg.NOTES = {"lang": "A static explanation."}
        self.b._dynamic_note = lambda k: ("A live warning." if k == "lang"
                                          else None)
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("A static explanation.", texts)
        self.assertIn("A live warning.", texts)
        self.assertLess(texts.index("A static explanation."),
                        texts.index("A live warning."))

    def test_settings_without_notes_still_render(self):
        """NOTES is optional — a config object without it must not blow
        up the whole Settings view."""
        self.assertFalse(hasattr(self.cfg, "NOTES"))
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        self.assertIn("set-player_name", ids(nodes))

    def test_enum_dropdown_stores_value_not_label(self):
        self.b._open_settings()
        nodes, handlers = build_scene(self.b)
        handlers["set-lang"]["select"](1, "Dubbed")
        self.assertEqual(self.cfg.values["lang"], "dub")

    def test_setting_bool_toggle_saves(self):
        self.b._open_settings()
        self._advanced()
        nodes, handlers = build_scene(self.b)
        handlers["set-autoplay"]["click"]()
        self.assertFalse(self.cfg.values["autoplay"])

    def test_setting_text_submit_coerces(self):
        self.b._open_settings()
        self._advanced()
        nodes, handlers = build_scene(self.b)
        handlers["set-seek_up"]["submit"]("15")
        self.assertEqual(self.cfg.values["seek_up"], 15)  # coerced to int

    def test_setting_invalid_value_reports(self):
        self.b._open_settings()
        self._advanced()
        nodes, handlers = build_scene(self.b)
        handlers["set-seek_up"]["submit"]("not-a-number")
        self.assertIn("Invalid", self.b.status)

class TestLogin(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_show_login_renders_form(self):
        self.b.show_login()
        self.assertEqual(self.b.route["kind"], "login")
        nodes, _h = build_scene(self.b)
        for fid in ("login-server", "login-user", "login-pass",
                    "login-connect"):
            self.assertIn(fid, ids(nodes))
        # login is chrome-free
        self.assertNotIn("nav-home", ids(nodes))

    def test_login_failure_shows_error(self):
        self.b.show_login()
        _n, handlers = build_scene(self.b)
        handlers["login-server"]["change"]("bad")
        handlers["login-user"]["change"]("u")
        handlers["login-pass"]["change"]("p")
        handlers["login-connect"]["click"]()
        self.assertIn("add_server",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIn("Could not connect", self.b._login_error)
        self.assertEqual(self.b.route["kind"], "login")

    def test_login_success_loads_source(self):
        self.b.show_login()
        _n, handlers = build_scene(self.b)
        handlers["login-server"]["change"]("good")
        handlers["login-connect"]["click"]()
        # success -> rebuild source -> home
        self.assertEqual(self.b.route["kind"], "home")
        self.assertIsNone(self.b._login_error)

    def test_enter_submits_the_login_form(self):
        """Typing a password and pressing Enter is the reflex on every
        login form there is. Here it did nothing at all."""
        self.b.show_login()
        _n, handlers = build_scene(self.b)
        handlers["login-server"]["change"]("good")
        for fid in ("login-server", "login-user", "login-pass"):
            self.assertIn("submit", handlers[fid], "%s: no Enter" % fid)
        handlers["login-pass"]["submit"]("p")
        self.assertEqual(self.b.route["kind"], "home")

class TestLocked(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_locked_renders_pin_field(self):
        self.b.show_locked()
        self.assertEqual(self.b.route["kind"], "locked")
        nodes, _h = build_scene(self.b)
        self.assertIn("lock-pin", ids(nodes))
        self.assertIn("lock-unlock", ids(nodes))
        self.assertNotIn("nav-home", ids(nodes))   # chrome-free

    def test_wrong_pin_shows_error(self):
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("0000")
        handlers["lock-unlock"]["click"]()
        self.assertIn("Incorrect", self.b._pin_error)
        self.assertEqual(self.b.route["kind"], "locked")

    def test_correct_pin_unlocks_to_home(self):
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertEqual(self.b.route["kind"], "home")
        self.assertIsNone(self.b._pin_error)

    def test_correct_pin_with_no_source_is_not_a_bad_pin(self):
        """work_offline (or an unreachable server) leaves nothing to build a
        source from. Reporting that as a wrong PIN locked the client out for
        good — the PIN was right, so land somewhere usable instead.

        With no saved server that is the login form."""
        self.ctl.connect_and_rebuild = lambda: None
        self.ctl.known_servers = lambda: []
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertIsNone(self.b._pin_error)
        self.assertEqual(self.b.route["kind"], "login")
        self.assertFalse(self.b._locked)

    def test_a_correct_pin_with_a_down_server_offers_retry_not_login(self):
        """The user HAS a server; it just did not answer. Sending them to
        the login form told them to sign in again and lost the offline
        library — the same case the connecting screen was built for."""
        self.ctl.connect_and_rebuild = lambda: None
        self.ctl.known_servers = lambda: [{"address": "http://srv",
                                           "name": "Home"}]
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertIsNone(self.b._pin_error)
        self.assertEqual(self.b.route["kind"], "connecting")
        self.assertFalse(self.b._locked)
        nodes, h = build_scene(self.b)
        self.assertIn("conn-retry", h, "no way to retry the connection")

    def test_relock_gates_the_ui_again_on_reopen(self):
        """Unlocking covers that reopen, not the rest of the process's life:
        closing to the tray and re-raising has to re-prompt."""
        self.ctl.needs_unlock = lambda: True
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertEqual(self.b.route["kind"], "home")

        self.b.maybe_relock()
        self.assertEqual(self.b.route["kind"], "locked")
        self.assertTrue(self.b._locked)

    def test_relock_is_a_noop_without_a_startup_pin(self):
        self.ctl.needs_unlock = lambda: False
        self.b.maybe_relock()
        self.assertNotEqual(self.b.route["kind"], "locked")

    def test_relocking_twice_keeps_a_half_typed_pin(self):
        """The tray can fire show/hide at any moment; a second relock must
        not reset the gate under the user's fingers."""
        self.ctl.needs_unlock = lambda: True
        self.b.maybe_relock()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("12")
        self.b.maybe_relock()
        self.assertEqual(self.b._pin["pin"], "12")

    def test_tray_settings_cannot_bypass_the_gate(self):
        """Configure Servers / Show Console route straight to Settings — the
        logs and server list are behind the PIN too."""
        self.b.show_locked()
        self.b.open_settings("logs")
        self.assertEqual(self.b.route["kind"], "locked")

    def test_remote_display_content_cannot_bypass_the_gate(self):
        self.b.show_locked()
        self.b.display_item("s1", "item-1")
        self.assertEqual(self.b.route["kind"], "locked")

class TestServerSwitcher(unittest.TestCase):
    def test_switcher_shown_and_switches(self):
        b = MpvtkBrowser(app=None, source=MultiServerSource())
        b._pool = _SyncPool()
        nodes, handlers = build_scene(b)
        self.assertIn("nav-server", ids(nodes))
        handlers["nav-server"]["select"](1, "Remote")
        self.assertEqual(b.server, "srv2")
        self.assertEqual(b.route["kind"], "home")

    def test_switcher_hidden_for_single_server(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        nodes, _h = build_scene(b)
        self.assertNotIn("nav-server", ids(nodes))

class TestServersPanel(unittest.TestCase):
    def setUp(self):
        self.ctl = DownloadsController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()
        self.b.open_settings("servers")

    def test_users_and_servers_both_render(self):
        nodes, _h = build_scene(self.b)
        self.assertIn("su-0", ids(nodes))
        self.assertIn("su-1", ids(nodes))
        self.assertIn("sv-0", ids(nodes))
        self.assertIn("sv-1", ids(nodes))

    def test_sections_span_the_pane(self):
        """Settings panels are forms; their cards should fill the width
        rather than shrink to their content."""
        nodes, _h = build_scene(self.b, size=(1280, 720))
        cards = [n for n in nodes if n["t"] == "rect" and n["w"] > 900]
        self.assertGreaterEqual(len(cards), 2, "expected two full-width cards")

    def test_locked_user_gets_the_lock_glyph(self):
        nodes, _h = build_scene(self.b)
        icons = [n for n in nodes if n["t"] == "icon"]
        self.assertTrue(icons)

class TestAddServer(unittest.TestCase):
    def setUp(self):
        self.ctl = LoginController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_first_run_login_has_no_way_back(self):
        """With no servers there is no library behind the form."""
        self.b.server = None
        self.b.show_login()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("login-cancel", ids(nodes))

    def test_adding_another_server_can_be_cancelled(self):
        self.b.show_login()          # server is set -> pushed, not reset
        nodes, h = build_scene(self.b)
        self.assertIn("login-cancel", ids(nodes))
        h["login-cancel"]["click"]()
        self.assertNotEqual(self.b.route["kind"], "login")

    def test_known_servers_are_offered(self):
        self.b.show_login()
        nodes, h = build_scene(self.b)
        self.assertIn("login-known-0", ids(nodes))
        h["login-known-0"]["click"]()
        self.assertEqual(self.b._login["server"], "http://old.example")

    def test_a_known_server_can_go_straight_to_quick_connect(self):
        """"Use" only filled the URL box, so the passwordless path still
        meant finding the other button and starting over — on the one
        screen whose whole point is not typing anything."""
        self.b.show_login()
        self.ctl.route_ref = self.b.route
        _n, h = build_scene(self.b)
        self.assertIn("login-known-qc-0", h, "no per-server Quick Connect")
        h["login-known-qc-0"]["click"]()
        self.assertEqual(self.b._login["server"], "http://old.example")
        self.assertEqual(self.ctl.qc_calls, ["http://old.example"],
                         "Quick Connect did not start for that server")

    def test_quick_connect_needs_a_server_url(self):
        self.b.show_login()
        _n, h = build_scene(self.b)
        h["login-qc"]["click"]()
        self.assertEqual(self.ctl.qc_calls, [])
        self.assertIn("URL", self.b._login_error)

    def test_quick_connect_shows_the_code(self):
        self.b.show_login()
        self.b._login["server"] = "http://srv"
        self.ctl.route_ref = self.b.route
        _n, h = build_scene(self.b)
        h["login-qc"]["click"]()
        self.assertEqual(self.ctl.qc_calls, ["http://srv"])
        # The code reached the screen while the login was in flight.
        self.assertEqual(self.ctl.codes_shown[0].get("code"), "ABC123")
        # It wasn't approved, so we're back on the form with an explanation.
        nodes, _h = build_scene(self.b)
        self.assertIn("login-connect", ids(nodes))
        self.assertIn("Quick Connect", self.b._login_error)

    def test_quick_connect_code_renders(self):
        self.b.show_login()
        self.b.route["_qc"] = {"code": "ABC123", "status": "Waiting…",
                               "cancelled": False}
        nodes, _h = build_scene(self.b)
        self.assertIn("ABC123", [n.get("text") for n in nodes])
        self.assertNotIn("login-connect", ids(nodes))

    def test_quick_connect_can_be_cancelled(self):
        self.b.show_login()
        self.b._login["server"] = "http://srv"
        route = self.b.route
        route["_qc"] = {"code": "ZZZ", "status": "", "cancelled": False}
        _n, h = build_scene(self.b)
        h["login-qc-cancel"]["click"]()
        self.assertNotIn("_qc", route)
        nodes, _h = build_scene(self.b)
        self.assertIn("login-connect", ids(nodes))   # back to the form

class TestPinSetup(unittest.TestCase):
    """Blank new+confirm compared equal and fell through to set_pin(None),
    so Save on a "Set PIN" dialog quietly REMOVED the lock."""

    def _dialog(self, locked=False):
        calls = []
        ctl = FakeController()
        ctl.set_user_pin = lambda uid, pin, require_startup=False: (
            calls.append((uid, pin, require_startup)) or True)
        ctl.unlock_user = lambda uid, pin: True
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._open_pin_setup({"id": "u1", "name": "Kid", "locked": locked})
        _n, handlers = build_scene(b)
        return b, handlers, calls

    def test_saving_with_blank_fields_does_not_clear_the_pin(self):
        b, handlers, calls = self._dialog(locked=True)
        handlers["ps-ok"]["click"]()
        self.assertEqual(calls, [], "blank Save removed the lock")
        # the dialog stays open reporting why
        nodes, _h = build_scene(b)
        texts = " ".join(n.get("text", "") for n in nodes if n.get("text"))
        self.assertIn("new PIN", texts)

    def test_a_matching_pin_is_saved(self):
        b, handlers, calls = self._dialog()
        handlers["ps-new"]["change"]("1234")
        handlers["ps-confirm"]["change"]("1234")
        handlers["ps-ok"]["click"]()
        self.assertEqual([c[1] for c in calls], ["1234"])

    def test_mismatched_pins_are_refused(self):
        _b, handlers, calls = self._dialog()
        handlers["ps-new"]["change"]("1234")
        handlers["ps-confirm"]["change"]("9999")
        handlers["ps-ok"]["click"]()
        self.assertEqual(calls, [])

class TestWorkOfflineToggle(unittest.TestCase):
    """work_offline was persisted and then ignored until the next launch —
    the classic "setting written but not applied"."""

    def _browser(self, offline_source=None, live_source=None):
        ctl = FakeController()
        ctl.offline_source = lambda: offline_source
        ctl.connect_and_rebuild = lambda: live_source
        cfg = FakeConfig()
        cfg.schema["work_offline"] = "bool"
        cfg.values["work_offline"] = False
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=cfg)
        b._pool = _SyncPool()
        return b

    def test_turning_it_on_swaps_to_the_downloads(self):
        offline = FakeSource()
        b = self._browser(offline_source=offline)
        b._set_setting("work_offline", True)
        self.assertIs(b.source, offline, "still on the live source")

    def test_turning_it_off_reconnects(self):
        live = FakeSource()
        b = self._browser(offline_source=FakeSource(), live_source=live)
        b._set_setting("work_offline", True)
        b._offline = True
        b._set_setting("work_offline", False)
        self.assertIs(b.source, live)

    def test_nothing_downloaded_reports_instead_of_blanking(self):
        b = self._browser(offline_source=None)
        before = b.source
        b._set_setting("work_offline", True)
        self.assertIs(b.source, before, "swapped to an empty source")
        self.assertIn("Nothing downloaded", b.status)

    def test_other_settings_do_not_touch_the_source(self):
        b = self._browser(offline_source=FakeSource())
        before = b.source
        b._set_setting("player_name", "Bud")
        self.assertIs(b.source, before)

class TestPinStartupSeeding(unittest.TestCase):
    """Changing a PIN silently cleared "require at startup": the dialog
    always opened with the box unticked and saved that back."""

    def test_the_checkbox_reflects_the_stored_setting(self):
        ctl = FakeController()
        ctl.set_user_pin = lambda uid, pin, require_startup=False: True
        ctl.unlock_user = lambda uid, pin: True
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._open_pin_setup({"id": "u1", "name": "Kid", "locked": True,
                           "require_startup": True})
        nodes, _h = build_scene(b)
        self.assertIn("ps-startup", ids(nodes))
        # A Checkbox is composite sugar; the tick glyph is the state.
        self.assertIn("✓", [n.get("text") for n in nodes if n.get("text")],
                      "startup requirement shown as off")

    def test_it_is_off_when_not_required(self):
        ctl = FakeController()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._open_pin_setup({"id": "u1", "name": "Kid", "locked": True,
                           "require_startup": False})
        nodes, _h = build_scene(b)
        self.assertIn("ps-startup", ids(nodes))
        self.assertNotIn("✓",
                         [n.get("text") for n in nodes if n.get("text")])

    def test_list_users_exposes_it(self):
        """The dialog can only seed what the controller reports."""
        import jellyfin_mpv_shim.users as users_mod
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        class FakeUM:
            active_id = "u1"

            def public_users(self):
                return [{"id": "u1", "name": "A", "locked": True,
                         "default": True, "require_startup": True}]

            def is_locked(self, uid):
                return True

        real, users_mod.userManager = users_mod.userManager, FakeUM()
        self.addCleanup(lambda: setattr(users_mod, "userManager", real))
        got = PlayerGateway().list_users()
        self.assertTrue(got[0]["require_startup"])

class TestOfflineGates(unittest.TestCase):
    """Controls that cannot work without a server were still offered."""

    def _browser(self, offline):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b._offline = offline
        return b

    def test_offline_offers_no_download_button(self):
        """There is nothing to fetch from."""
        b = self._browser(True)
        self.assertIsNone(
            b._download_btn({"Id": "m1", "Type": "Movie"}, "srv1", "d"))

    def test_online_still_offers_it(self):
        b = self._browser(False)
        self.assertIsNotNone(
            b._download_btn({"Id": "m1", "Type": "Movie"}, "srv1", "d"))

    def test_remove_download_is_still_offered_offline(self):
        """Reclaiming space is exactly what you want offline."""
        b = self._browser(True)
        b.tiles._downloaded = {"m1"}
        btn = b._download_btn({"Id": "m1", "Type": "Movie"}, "srv1", "d")
        self.assertIn("d-undownload", ids(layout(btn, 1280, 720)[0]))

    def test_offline_hides_the_user_switcher(self):
        """Switching user reconnects, which cannot work with no server."""
        b = self._browser(True)
        b.controller.list_users = lambda: [
            {"id": "u1", "name": "A", "active": True},
            {"id": "u2", "name": "B", "active": False}]
        self.assertNotIn("nav-user", ids(build_scene(b)[0]))

    def test_online_shows_it(self):
        b = self._browser(False)
        b.controller.list_users = lambda: [
            {"id": "u1", "name": "A", "active": True},
            {"id": "u2", "name": "B", "active": False}]
        self.assertIn("nav-user", ids(build_scene(b)[0]))

class TestDisplayTab(unittest.TestCase):
    """The Display tab: per-user preferences held on the *server*.

    Everything here is shared with jellyfin-web, which is why it loads and
    saves asynchronously and why a stale read must never be written back.
    """

    def _browser(self, prefs=None, fail=None):
        src = FakeSource()
        self.saved = []
        self.reads = []

        def get_user_prefs(server, refresh=False):
            self.reads.append(refresh)
            if fail == "load":
                raise RuntimeError("server refused")
            return dict(prefs if prefs is not None else
                        {"episode_images": False})

        def save_user_prefs(server, values):
            if fail == "save":
                raise RuntimeError("server refused")
            self.saved.append(dict(values))

        src.get_user_prefs = get_user_prefs
        src.save_user_prefs = save_user_prefs
        b = MpvtkBrowser(app=None, source=src, config=FakeConfig())
        b._pool = _SyncPool()
        b.server = "srv1"
        b._open_settings()
        b.route["_tab"] = "display"
        # These preferences live on the server, so the first frame of the tab
        # is a spinner that kicks the fetch off -- exactly as the Home Screen
        # tab behaves. Under _SyncPool the fetch completes inside that frame,
        # so one throwaway build leaves the tab showing its real contents.
        build_scene(b)
        return b

    def test_the_toggle_renders_with_the_servers_value(self):
        b = self._browser({"episode_images": True})
        nodes, _h = build_scene(b)
        self.assertIn("display-episode-images", ids(nodes))

    def test_it_forces_a_refresh_rather_than_trusting_the_cache(self):
        """The user may have changed this in Jellyfin Web since we started;
        a stale value here gets written back over their real one.

        The home screen reads it too, on the way past and off the cache
        (``refresh=False``) -- it repaints constantly and must not re-fetch
        a server preference every time. Only the tab forces one, so this
        asserts on the tab's read rather than on the whole list.
        """
        b = self._browser()
        self.assertTrue(self.reads, "the tab never read the preferences")
        self.assertIs(self.reads[-1], True,
                      "the settings tab trusted the cache")

    def test_toggling_saves_the_flipped_value(self):
        b = self._browser({"episode_images": False})
        _n, handlers = build_scene(b)
        handlers["display-episode-images"]["click"]()
        self.assertEqual(self.saved, [{"episode_images": True}])

    def test_the_tick_moves_before_the_round_trip(self):
        """Optimistic: a checkbox that only ticks after the network reads as
        a dead control."""
        b = self._browser({"episode_images": False})
        _n, handlers = build_scene(b)
        handlers["display-episode-images"]["click"]()
        self.assertTrue(b.route["_display_prefs"]["episode_images"])

    def test_a_rejected_save_rolls_the_tick_back(self):
        b = self._browser({"episode_images": False}, fail="save")
        _n, handlers = build_scene(b)
        handlers["display-episode-images"]["click"]()
        self.assertFalse(b.route["_display_prefs"]["episode_images"],
                         "a rejected change stayed on screen")

    def test_a_failed_load_offers_a_retry_and_no_toggles(self):
        """Not the defaults: editable toggles we never read let the user
        'keep' a value that was never loaded, then save that guess."""
        b = self._browser(fail="load")
        nodes, _h = build_scene(b)
        self.assertIn("display-retry", ids(nodes))
        self.assertNotIn("display-episode-images", ids(nodes))

    def test_offline_says_so_instead_of_failing_at_save_time(self):
        src = FakeSource()
        self.assertFalse(hasattr(src, "save_user_prefs"))
        b = MpvtkBrowser(app=None, source=src, config=FakeConfig())
        b._pool = _SyncPool()
        b._open_settings()
        b.route["_tab"] = "display"
        build_scene(b)
        nodes, _h = build_scene(b)
        self.assertNotIn("display-episode-images", ids(nodes))
        self.assertNotIn("display-retry", ids(nodes))

    def test_a_superseded_fetch_does_not_strand_the_tab(self):
        """`on_done` is epoch-gated, so a fetch overtaken by a background
        reconnect (set_source bumps the epoch) or by leaving and coming
        back runs NEITHER callback. With the loading guard cleared only in
        those two, it stayed set — and the tab then short-circuits on it
        every frame: a permanent spinner, no error, no retry, escapable
        only by switching tabs.

        Driven by bumping the epoch mid-fetch, which is what actually
        happens, rather than by poking the flag.
        """
        src = FakeSource()
        b = MpvtkBrowser(app=None, source=src, config=FakeConfig())
        b._pool = _SyncPool()
        b.server = "srv1"

        def get_user_prefs(_server, refresh=False):
            b._bump_epoch()          # a reconnect lands while we are out
            return {"episode_images": False}

        src.get_user_prefs = get_user_prefs
        src.save_user_prefs = lambda *a: None
        b._open_settings()
        b.route["_tab"] = "display"
        build_scene(b)
        self.assertFalse(b.route.get("_display_loading"),
                         "the tab is stuck on a spinner for good")
        # ...and the next visit actually re-fetches rather than returning
        # early on the stale guard.
        reads = []
        src.get_user_prefs = lambda _s, refresh=False: (
            reads.append(refresh) or {"episode_images": True})
        build_scene(b)
        self.assertEqual(reads, [True])

    def test_leaving_the_tab_drops_the_cached_read(self):
        """Same rule as the home layout: a cached copy goes stale the moment
        the user touches Jellyfin Web."""
        b = self._browser()
        self.assertIsNotNone(b.route.get("_display_prefs"))
        b._set_settings_tab(b.route, "general")
        self.assertIsNone(b.route.get("_display_prefs"))


class TestSeriesTrailerAndSettingsSafety(unittest.TestCase):
    def test_a_series_with_trailers_offers_the_button(self):
        """The detail loader had always fetched trailers for a Series, but a
        Series routes to _render_series, which had no button — one wasted API
        call per load for a feature nobody could reach."""
        src = FakeSource()
        src.get_trailers = lambda srv, iid: [{"Id": "tr1"}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "series", "server": "srv1", "item_id": "sh1",
                    "title": "Show"})
        nodes, handlers = build_scene(b)
        self.assertIn("sa-trailer", ids(nodes))
        played = []
        # ItemActions.play_list, not the shell forwarder: the page calls its
        # own service now.
        b._actions.play_list = lambda ids_, srv, i, **kw: played.append(
            list(ids_))
        handlers["sa-trailer"]["click"]()
        self.assertEqual(played, [["tr1"]])

    def test_a_series_without_trailers_offers_nothing(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.source.get_trailers = lambda srv, iid: []
        b.navigate({"kind": "series", "server": "srv1", "item_id": "sh1",
                    "title": "Show"})
        self.assertNotIn("sa-trailer", ids(build_scene(b)[0]))

    def test_client_uuid_is_not_an_editable_settings_row(self):
        """It is the device identity the server keys sessions on; editing it
        free-text orphans every session and playstate it has recorded."""
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg
        self.assertNotIn("client_uuid", cfg.settings_schema())

    def test_set_offline_has_a_production_caller(self):
        """It was a public method only the tests reached; _offline was
        assigned directly elsewhere. One writer now."""
        import ast
        import inspect
        from jellyfin_mpv_shim.mpvtk_browser import app as app_mod
        src = inspect.getsource(app_mod)
        calls = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "set_offline"]
        self.assertTrue(calls, "set_offline is dead again")

class TestRemovingAServerRebuildsTheSource(unittest.TestCase):
    """Dropping the credential is not enough: LibrarySource holds its own
    connection per server, built once, so the removed server stayed in the
    switcher and stayed browsable — while playback refused it."""

    def _browser(self, removed_ok=True, rebuilt=None, offline=None):
        ctl = FakeController()
        self.calls = []
        ctl.remove_server = lambda uuid: (self.calls.append(uuid)
                                          or removed_ok)
        ctl.rebuild_source = lambda: rebuilt
        ctl.offline_source = lambda: offline
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=FakeConfig())
        b._pool = _SyncPool()
        b.nav_stack = [{"kind": "settings", "server": "srv1",
                        "_tab": "servers"}]
        return b

    def test_the_source_is_rebuilt_without_the_removed_server(self):
        fresh = FakeSource()
        b = self._browser(rebuilt=fresh)
        b._remove_server("srv1")
        self.assertEqual(self.calls, ["srv1"])
        self.assertIs(b.source, fresh, "still browsing the removed server")

    def test_it_leaves_you_where_you_were(self):
        """set_source lands on Home; someone managing servers wants to keep
        managing servers."""
        b = self._browser(rebuilt=FakeSource())
        b._remove_server("srv1")
        self.assertEqual(b.route["kind"], "settings")
        self.assertEqual(b.route.get("_tab"), "servers")

    def test_removing_the_last_server_falls_back_to_the_downloads(self):
        offline = FakeSource()
        b = self._browser(rebuilt=None, offline=offline)
        b._remove_server("srv1")
        self.assertIs(b.source, offline)

    def test_the_last_server_with_nothing_downloaded_goes_to_login(self):
        b = self._browser(rebuilt=None, offline=None)
        b._remove_server("srv1")
        self.assertEqual(b.route["kind"], "login")

    def test_a_refused_removal_says_so(self):
        b = self._browser(removed_ok=False)
        before = b.source
        b._remove_server("srv1")
        self.assertIs(b.source, before, "rebuilt after a failed removal")
        self.assertTrue(b.status, "the failure was silent")

class TestCopyLogsButton(unittest.TestCase):
    """The Copy button on Settings -> Logs. A helper test alone would not
    prove it is on screen or wired, which is how five features in this UI
    shipped unreachable."""

    def _browser(self, lines=("line one", "line two"), result=None):
        ctl = FakeController()
        ctl.recent_logs = lambda: list(lines)
        self.copied = []
        ctl.copy_text = lambda text: (self.copied.append(text)
                                      or (result or (True, "xclip", None)))
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=FakeConfig())
        b._pool = _SyncPool()
        b.nav_stack = [{"kind": "settings", "server": "srv1", "_tab": "logs"}]
        return b

    def test_the_button_is_on_the_logs_tab(self):
        b = self._browser()
        nodes, handlers = build_scene(b)
        self.assertIn("log-copy", ids(nodes), "no Copy button on screen")
        self.assertIn("log-copy", handlers, "the Copy button does nothing")

    def test_clicking_it_copies_every_captured_line(self):
        b = self._browser(lines=["a", "b", "c"])
        _nodes, handlers = build_scene(b)
        handlers["log-copy"]["click"]()
        self.assertEqual(self.copied, ["a\nb\nc"])
        self.assertIn("3", b.status)

    def test_it_copies_more_than_the_view_draws(self):
        """The view materializes only the rows in the viewport; the point of
        copying is to hand over the whole thing."""
        b = self._browser(lines=["l%d" % i for i in range(900)])
        _nodes, handlers = build_scene(b)
        handlers["log-copy"]["click"]()
        self.assertEqual(len(self.copied[0].splitlines()), 900)

    def test_no_clipboard_reports_where_it_put_the_text(self):
        b = self._browser(result=(True, "file", "/tmp/copied-logs.txt"))
        _nodes, handlers = build_scene(b)
        handlers["log-copy"]["click"]()
        self.assertIn("/tmp/copied-logs.txt", b.status)

    def test_a_failure_says_so(self):
        b = self._browser(result=(False, None, None))
        _nodes, handlers = build_scene(b)
        handlers["log-copy"]["click"]()
        self.assertIn("not copy", b.status.lower())

    def test_an_empty_log_says_so_instead_of_copying_nothing(self):
        b = self._browser(lines=[])
        _nodes, handlers = build_scene(b)
        handlers["log-copy"]["click"]()
        self.assertEqual(self.copied, [])
        self.assertIn("nothing", b.status.lower())

class TestConnectingScreen(unittest.TestCase):
    """The browser opened straight onto an empty home route, which renders
    as a bare _busy() spinner: nothing saying what it was waiting for, and
    against a server that never answers, no way out at all. Tk had this
    screen; mpvtk had "connecting" listed in CHROME_FREE and no route."""

    def _browser(self, downloads=True, offline=True):
        ctl = FakeController()
        ctl.has_downloads = lambda: downloads
        self.offline_source = FakeSource() if offline else None
        ctl.offline_source = lambda: self.offline_source
        self.retried = []
        ctl.retry_connect = lambda: (self.retried.append(1) or None)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b.show_connecting()
        return b

    def test_it_says_what_it_is_waiting_for(self):
        b = self._browser()
        nodes, _h = build_scene(b)
        texts = " ".join(n.get("text") or "" for n in nodes)
        self.assertIn("Connecting", texts)

    def test_it_is_chrome_free(self):
        """A top bar whose Home/Search go nowhere yet is worse than none."""
        b = self._browser()
        nodes, _h = build_scene(b)
        self.assertNotIn("nav-home", ids(nodes))

    def test_work_offline_browses_the_downloads(self):
        b = self._browser()
        _nodes, h = build_scene(b)
        self.assertIn("conn-offline", h, "no way out of the spinner")
        h["conn-offline"]["click"]()
        self.assertIs(b.source, self.offline_source)
        self.assertEqual(b.route["kind"], "home")

    def test_nothing_downloaded_offers_no_dead_end(self):
        """An empty offline library is a worse dead end than the spinner."""
        b = self._browser(downloads=False)
        nodes, _h = build_scene(b)
        self.assertNotIn("conn-offline", ids(nodes))

    def test_retry_appears_only_once_the_connect_gave_up(self):
        """A Retry offered while the first attempt is still in flight just
        starts a second one racing it."""
        b = self._browser()
        self.assertNotIn("conn-retry", ids(build_scene(b)[0]))
        b.connect_failed()
        self.assertIn("conn-retry", ids(build_scene(b)[0]))

    def test_a_failed_connect_says_so(self):
        b = self._browser()
        b.connect_failed()
        nodes, _h = build_scene(b)
        texts = " ".join(n.get("text") or "" for n in nodes)
        self.assertIn("reach", texts.lower())

    def test_a_failed_retry_says_so_rather_than_looking_dead(self):
        """_retry_connect's done() used to no-op on failure: nothing moved,
        and pressing it again looked identical to never pressing it."""
        b = self._browser()
        b.connect_failed()
        _nodes, h = build_scene(b)
        h["conn-retry"]["click"]()
        self.assertEqual(len(self.retried), 1)
        self.assertTrue(b.status, "a failed retry was silent")

    def test_sign_in_is_reachable_from_a_failed_connect(self):
        """Otherwise a saved server that is gone for good is unrecoverable
        without editing config by hand."""
        b = self._browser()
        b.connect_failed()
        _nodes, h = build_scene(b)
        h["conn-login"]["click"]()
        self.assertEqual(b.route["kind"], "login")

    def test_connect_failed_does_not_yank_a_user_who_moved_on(self):
        """It runs on the connect thread. If Work Offline already landed,
        stamping an error onto whatever route is now current would put a
        connect failure on the home screen."""
        b = self._browser()
        _nodes, h = build_scene(b)
        h["conn-offline"]["click"]()
        self.assertEqual(b.route["kind"], "home")
        b.connect_failed()
        self.assertEqual(b.route["kind"], "home")
        self.assertIsNone(b.route.get("_connect_error"))

class TestLiveLogTail(unittest.TestCase):
    """The Tk browser streamed the log live and held all 2000 lines. mpvtk
    took a one-shot snapshot of the last 500 — so the log you were staring
    at while reproducing a bug never moved, and the interesting part had
    usually already scrolled out of the ring."""

    def _browser(self, lines):
        ctl = FakeController()
        self.reads = 0

        def recent():
            self.reads += 1
            return list(lines)

        ctl.recent_logs = recent
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=FakeConfig())
        b._pool = _SyncPool()
        b.LOG_POLL_SECS = 0.01     # don't sleep a second per tick in a test
        b._browsing = True
        self.invalidated = []
        b.invalidate = lambda: self.invalidated.append(1)
        b.nav_stack = [{"kind": "settings", "server": "srv1", "_tab": "logs"}]
        return b

    def _tail(self, b, ticks=3):
        """Let the poller run a few ticks, then shut it down.

        It is *meant* to run until you leave the tab, so a plain join would
        hang; the tests that assert it exits on its own change the condition
        first and pass ticks=0.

        Records `self.ticked` — invalidations seen while the poller was
        actually ticking. A departing restartable poller invalidates once on
        its way out (that is what re-arms the view after a fast
        leave-and-return), so counting every invalidation would attribute
        teardown to the tick loop.
        """
        b._poll_logs(b.route)
        t = b._log_thread
        self.ticked = 0
        if t is not None:
            if ticks:
                time.sleep(b.LOG_POLL_SECS * (ticks + 1))
            self.ticked = len(self.invalidated)
            b._shutdown_evt.set()
            t.join(5)
            self.assertFalse(t.is_alive(), "the tail poller never stopped")

    def _scroll_node(self, b):
        nodes, _h = build_scene(b)
        for n in nodes:
            if n.get("id") == "settings-logs" and n["t"] == "scroll":
                return n
        self.fail("the log list is not a scroll container")

    def test_the_whole_ring_is_reachable_not_just_the_last_500(self):
        """The ring holds 2000 and the view drew the last 500, with no sign
        the rest existed. Assert on the scrollable extent rather than the
        row count: that is the thing the user can actually reach."""
        b = self._browser(["l%d" % i for i in range(2000)])
        node = self._scroll_node(b)
        self.assertGreaterEqual(
            node["ch"], 2000 * b.LOG_ROW_H,
            "the log list only scrolls over %d px" % node["ch"])

    def test_the_list_is_virtualized(self):
        """2000 un-virtualized rows is 2000 laid-out nodes per frame, at
        1Hz. The whole reason the cap existed."""
        b = self._browser(["l%d" % i for i in range(2000)])
        nodes, _h = build_scene(b)
        drawn = [n for n in nodes if str(n.get("id", "")).startswith("log-")]
        self.assertLess(len(drawn), 200,
                        "every line was materialized: %d" % len(drawn))
        self.assertTrue(drawn, "no log rows were materialized at all")

    def test_the_view_follows_the_tail(self):
        """Pinned to the newest line — the renderer unpins it as soon as
        the user scrolls up (see tests/lua/test_renderer.lua)."""
        b = self._browser(["a", "b"])
        nodes, _h = build_scene(b)
        scroll = [n for n in nodes
                  if n.get("id") == "settings-logs" and n["t"] == "scroll"]
        self.assertTrue(scroll, "the log list is not a scroll container")
        self.assertTrue(scroll[0].get("follow"), "the view does not follow")

    def test_opening_the_tab_starts_the_tail(self):
        """Every other test here starts the poller itself, so without this
        one, deleting the call from the view leaves them all green and the
        log simply never moves again — the exact shape of bug this UI has
        shipped before."""
        b = self._browser(["one"])
        self.assertIsNone(b._log_thread)
        build_scene(b)
        self.addCleanup(b._shutdown_evt.set)
        self.assertIsNotNone(b._log_thread, "rendering the tab started no tail")

    def test_another_tab_starts_no_tail(self):
        b = self._browser(["one"])
        b.route["_tab"] = "general"
        build_scene(b)
        self.addCleanup(b._shutdown_evt.set)
        self.assertIsNone(b._log_thread)

    def test_a_new_line_redraws_the_view(self):
        lines = ["one"]
        b = self._browser(lines)
        b._settings_logs(b.route, (1280, 720))    # record what was drawn
        lines.append("two")                       # something logged
        self._tail(b)
        self.assertTrue(self.ticked, "a new log line did not redraw")

    def test_a_quiet_log_does_not_redraw(self):
        """An idle app logs nothing for minutes. Rebuilding a 2000-row
        scene every second to draw the same thing is not free."""
        b = self._browser(["one", "two"])
        b._settings_logs(b.route, (1280, 720))
        self._tail(b)
        self.assertEqual(self.ticked, 0,
                         "an idle log redrew %d times" % self.ticked)

    def test_a_full_ring_still_redraws(self):
        """Once the ring is full every new line also drops one, so the
        count stops moving — length alone would go blind exactly when the
        log is busiest."""
        lines = ["l%d" % i for i in range(5)]
        b = self._browser(lines)
        b._settings_logs(b.route, (1280, 720))
        lines.pop(0)                 # ring rolled: same length, new content
        lines.append("newest")
        self._tail(b)
        self.assertTrue(self.ticked, "a rolled ring did not redraw")

    def test_it_stops_when_you_leave_the_tab(self):
        b = self._browser(["one"])
        b._settings_logs(b.route, (1280, 720))
        b.route["_tab"] = "general"
        self._tail(b, ticks=0)
        self.assertIsNone(b._log_thread, "the poller slot was never released")

    def test_it_stops_when_browsing_ends(self):
        """Playback started: the browser is not on screen and a poller
        holding a thread to redraw it is pure waste."""
        b = self._browser(["one"])
        b._settings_logs(b.route, (1280, 720))
        b._browsing = False
        self._tail(b, ticks=0)
        self.assertIsNone(b._log_thread)

    def test_leaving_and_returning_inside_one_tick_still_leaves_a_poller(self):
        """A poller only notices its route went stale on its NEXT tick. Leave
        the tab and come straight back inside that window and the sequence
        was: the view asks for a poller, _start_daemon refuses because the
        old thread is still registered, then the old thread wakes, sees a
        stale route and clears the slot. Nobody polling, and only the render
        path starts one — so the log froze until something else rebuilt it.

        The departing thread now invalidates once the slot is free, which
        re-runs the view and lets it start a fresh poller.
        """
        b = self._browser(["one"])
        first = dict(b.route)
        b._settings_logs(b.route, (1280, 720))       # poller for route A
        self.assertIsNotNone(b._log_thread)

        # Navigate away and back to a NEW route dict, before A's poller has
        # ticked. Its request is refused.
        b.nav_stack = [{"kind": "home", "server": "srv1"}]
        b.nav_stack = [dict(first)]
        b._poll_logs(b.route)      # refused: A's thread still holds the slot

        # A's thread now wakes, finds its route stale, exits, releases the
        # slot and wakes the loop.
        self.invalidated.clear()
        deadline = time.time() + 5
        while time.time() < deadline and not self.invalidated:
            time.sleep(0.01)
        self.addCleanup(b._shutdown_evt.set)
        self.assertTrue(
            self.invalidated,
            "the departing poller left the slot empty and woke nobody")

    def test_an_empty_log_still_renders_its_buttons(self):
        b = self._browser([])
        nodes, handlers = build_scene(b)
        self.assertIn("log-copy", ids(nodes))
        self.assertIn("log-refresh", handlers)

class TestPinFailsClosed(unittest.TestCase):
    """A raising PIN check used to fall through and apply the new PIN
    without the current one ever being confirmed."""

    def test_a_raising_verify_does_not_apply_the_change(self):
        saved = []
        ctl = FakeController()
        ctl.set_user_pin = lambda uid, pin, require_startup=False: saved.append(
            pin)

        def boom(uid, pin):
            raise OSError("user store unreadable")

        ctl.unlock_user = boom
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._open_pin_setup({"id": "u1", "name": "Kid", "locked": True})
        _n, h = build_scene(b)
        h["ps-new"]["change"]("9999")
        h["ps-confirm"]["change"]("9999")
        h["ps-ok"]["click"]()
        self.assertEqual(saved, [], "changed the PIN without verifying")


if __name__ == "__main__":
    unittest.main()
