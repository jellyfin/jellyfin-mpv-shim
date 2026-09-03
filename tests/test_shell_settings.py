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
    SearchConfig,
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
        for tab in ("general", "browse", "playback", "home", "servers",
                    "downloads", "logs"):
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

        controls = dict(real.sections("playback"))["Player Controls"]
        self.assertEqual(
            controls[controls.index("osc_style"):][:3],
            ["osc_style", "hud_grab_keys", "hud_wake_key"])
        self.assertIn("osc_style", real.NOTES)
        advanced = dict(real.sections()).get("Advanced", [])
        self.assertNotIn("hud_grab_keys", advanced)
        self.assertNotIn("hud_wake_key", advanced)

    def test_discord_presence_is_a_curated_setting(self):
        """The real schema: it was config-file-only, so the only way to turn
        it on was to know the key existed."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        self.assertIn("discord_presence",
                      dict(real.sections())["This Device"])
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
        advanced_title = next(t for t in groups
                              if t not in dict(real.SECTIONS))
        curated = {k for title, keys in groups.items()
                   if title != advanced_title for k in keys}
        for key in ("enable_gui", "headless"):
            self.assertNotIn(key, curated)
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
        shown = dict(real.sections())["Window"]
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
        # The audio group lives on the Playback tab since the General page
        # was split; opening on the default tab renders a form with no audio
        # controls in it at all.
        b.open_settings("playback")
        return b

    def test_the_playback_tab_can_reach_the_config_folder(self):
        """Several notes on this tab end by telling you to put something in
        mpv.conf. A page that names a file and gives you no way to reach it
        assumes you know where the app keeps its config."""
        from jellyfin_mpv_shim.mpvtk.layout import layout

        b = MpvtkBrowser(app=None, source=FakeSource())
        opened = []
        b.controller = type("Ctl", (), {
            "open_config_folder": lambda self: opened.append(True)})()
        # The click goes through `_client_call`, which is deliberately
        # asynchronous -- reaching outside the process must not stall the
        # loop thread. Inline here, or the assertion below races the pool.
        b._pool = _SyncPool()
        b.open_settings("playback")
        nodes, handlers = layout(b.build((1280, 720)), 1280, 720)
        self.assertIn("set-open-config", {n.get("id") for n in nodes})
        handlers["set-open-config"]["click"]()
        self.assertEqual(len(opened), 1)

    def test_the_other_config_tabs_do_not_carry_it(self):
        """It is on Playback because that is where the notes point at
        mpv.conf, not because every settings page needs a folder button --
        the Logs tab already has one, and three would be clutter."""
        from jellyfin_mpv_shim.mpvtk.layout import layout

        for tab in ("general", "browse"):
            with self.subTest(tab=tab):
                b = MpvtkBrowser(app=None, source=FakeSource())
                b.open_settings(tab)
                nodes, _h = layout(b.build((1280, 720)), 1280, 720)
                self.assertNotIn("set-open-config",
                                 {n.get("id") for n in nodes})

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

class TestSettingsSearch(unittest.TestCase):
    """Searching the config form.

    The settings form is about a hundred controls across three tabs, and the
    tab a control sits on is a curation decision rather than something a
    user can derive -- so "which tab is it on" is the question the search
    exists to stop people having to answer.
    """

    def setUp(self):
        self.cfg = SearchConfig()
        self.b = MpvtkBrowser(app=None, source=FakeSource(), config=self.cfg)
        self.b._open_settings()

    def _texts(self, nodes):
        return " ".join(n.get("text", "") for n in nodes if n.get("text"))

    def _type(self, query):
        nodes, handlers = build_scene(self.b)
        handlers["set-search-box"]["change"](query)
        return build_scene(self.b)

    def test_the_box_is_offered_on_the_form_tabs(self):
        nodes, _h = build_scene(self.b)
        self.assertIn("set-search-box", ids(nodes))

    def test_the_box_is_not_offered_where_it_would_search_nothing(self):
        """Four of the seven tabs are not this form -- three are their own
        screens and the home screen lives on the server. A box that found
        nothing on them would read as a broken search rather than as an
        absent one."""
        for tab in ("home", "servers", "downloads", "logs"):
            with self.subTest(tab=tab):
                self.b.route["_tab"] = tab
                nodes, _h = build_scene(self.b)
                self.assertNotIn("set-search-box", ids(nodes))

    def test_a_query_finds_a_setting_from_another_tab(self):
        """The whole point: `deband` lives on Playback and the search is run
        from General. A filter that only searched the tab in front of you
        would be answering the question the tab bar already answers."""
        self.assertEqual(self.b.route.get("_tab", "general"), "general")
        nodes, _h = self._type("deband")
        self.assertIn("set-deband", ids(nodes))

    def test_a_result_is_editable_where_it_is_found(self):
        """Not a link to the tab it lives on. The control has to work from
        the results, or the search has saved nobody anything."""
        _nodes, handlers = self._type("deband")
        handlers["set-deband"]["submit"]("standard")
        self.assertEqual(self.cfg.values["deband"], "standard")

    def test_the_note_is_searched_and_not_only_the_label(self):
        """The half that makes it useful. Nothing in "Debanding" contains
        the word people actually type; "banding" and "gradients" are both in
        the note, and neither is in any label."""
        nodes, _h = self._type("gradients")
        self.assertIn("set-deband", ids(nodes))

    def test_every_word_has_to_match(self):
        """AND, not OR. With notes this long, OR returns most of the form
        for any two common words, which is as useless as returning
        nothing."""
        nodes, _h = self._type("deband gradients")
        self.assertIn("set-deband", ids(nodes))
        nodes, _h = self._type("deband unrelatedword")
        self.assertNotIn("set-deband", ids(nodes))

    def test_a_result_keeps_the_group_it_belongs_to(self):
        """A flat list of controls loses the context that says what a
        setting is for."""
        nodes, _h = self._type("deband")
        self.assertIn("Video Enhancement", self._texts(nodes))

    def test_an_advanced_setting_is_findable_without_the_disclosure(self):
        """The disclosure exists so a tab is not a hundred controls long.
        Somebody who typed a query has already narrowed it, and leaving half
        the answers behind a checkbox that is not on screen would make the
        search quietly incomplete."""
        self.assertFalse(self.b.route.get("_advanced"))
        nodes, _h = self._type("seek")
        self.assertIn("set-seek_up", ids(nodes))

    def test_a_query_that_matches_nothing_says_so(self):
        """Rather than an empty page, which is indistinguishable from a
        broken screen."""
        nodes, _h = self._type("zzzznothing")
        text = self._texts(nodes)
        self.assertIn("zzzznothing", text)
        self.assertNotIn("set-player_name", ids(nodes))

    def test_picking_a_tab_ends_the_search(self):
        """The way out. While a query is live the tab bar is not describing
        what is on screen, which makes clicking one the obvious escape --
        and leaving the query running would make it the one gesture that
        did nothing."""
        _nodes, handlers = self._type("deband")
        nodes, handlers = build_scene(self.b)
        handlers["stab-browse"]["click"]()
        self.assertEqual(self.b.route.get("_q", ""), "")
        nodes, _h = build_scene(self.b)
        self.assertNotIn("set-deband", ids(nodes))

    def test_typing_asks_for_a_repaint(self):
        """A scene assertion is not a repaint assertion: build_scene renders
        when asked, so every test above would pass against a handler that
        stored the query and never redrew -- and the user would type into a
        box and watch nothing happen."""
        nodes, handlers = build_scene(self.b)
        seen = []
        self.b.invalidate = lambda *a: seen.append(1)
        handlers["set-search-box"]["change"]("deband")
        self.assertTrue(seen)

    def test_retyping_the_same_query_does_not_repaint(self):
        """`on_change` fires per keystroke, including ones that do not
        change the value."""
        nodes, handlers = build_scene(self.b)
        handlers["set-search-box"]["change"]("deband")
        seen = []
        self.b.invalidate = lambda *a: seen.append(1)
        nodes, handlers = build_scene(self.b)
        handlers["set-search-box"]["change"]("deband")
        self.assertFalse(seen)

    def test_the_results_scroll_apart_from_the_tab_form(self):
        """Sharing the tab form's scroll id meant a search run from halfway
        down Playback opened its results halfway down too."""
        nodes, _h = build_scene(self.b)
        self.assertIn("settings", ids(nodes))
        nodes, _h = self._type("deband")
        self.assertIn("settings-search", ids(nodes))
        self.assertNotIn("settings", ids(nodes))


class TestRestartBanner(unittest.TestCase):
    """Settings that do nothing until the app is started again.

    The failure this replaces is silent: the value is saved, the form says
    "Saved", and nothing whatsoever happens -- which is indistinguishable
    from a broken control.
    """

    def setUp(self):
        self.cfg = FakeConfig()
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(), config=self.cfg,
                              controller=self.ctl)
        self.b._open_settings()

    def _texts(self, nodes):
        return " ".join(n.get("text", "") for n in nodes if n.get("text"))

    def _change_a_restart_setting(self, value="Rename"):
        nodes, handlers = build_scene(self.b)
        handlers["set-player_name"]["submit"](value)
        return build_scene(self.b)

    def test_a_restart_required_setting_is_marked_on_its_row(self):
        """The marker is the whole lifecycle vocabulary now: marked means
        literally nothing happened, unmarked means it applied or applies to
        the next thing you play. It has to be on the row, because that is
        where the decision is made -- the banner only appears afterwards."""
        self.b.route["_advanced"] = True
        nodes, _h = build_scene(self.b)
        self.assertIn("Requires restart", self._texts(nodes))

    def test_a_setting_that_applies_now_is_not_marked(self):
        """The half that makes the marker worth reading. Marking everything
        would be the page footer this replaced."""
        cfg = FakeConfig()
        cfg.RESTART_REQUIRED = frozenset()
        b = MpvtkBrowser(app=None, source=FakeSource(), config=cfg,
                         controller=FakeController())
        b._open_settings()
        b.route["_advanced"] = True
        nodes, _h = build_scene(b)
        self.assertNotIn("Requires restart", self._texts(nodes))

    def test_the_page_no_longer_hedges_about_every_control(self):
        """"Some changes take effect after restarting" named no changes, so
        the only thing a reader could do with it was distrust the whole
        page."""
        nodes, _h = build_scene(self.b)
        self.assertNotIn("take effect after restarting", self._texts(nodes))

    def test_no_banner_before_anything_changes(self):
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-restart", ids(nodes))

    def test_changing_a_restart_setting_raises_the_banner(self):
        nodes, _h = self._change_a_restart_setting()
        self.assertIn("banner-restart", ids(nodes))

    def test_the_banner_names_the_setting(self):
        """Named, not counted: "2 settings need a restart" makes the user go
        looking for which two, and the answer is not on screen once they
        have left the tab."""
        nodes, _h = self._change_a_restart_setting()
        self.assertIn(self.cfg.label_for("player_name"), self._texts(nodes))

    def test_the_banner_text_is_not_truncated(self):
        """The banner promises to NAME the settings, and it was silently
        ellipsizing every one of them -- `wrap=True` inside a fixed-height
        Row is clamped to a single line, and the wrap slop then means the
        last word never fits. With the real config a single pending setting
        rendered as "Restart to apply: Interface…".

        Three names, so the string is long enough that a returning `wrap`
        truncates it; asserted on the text rather than on the node id, which
        is what the other banner tests check and why none of them saw it.
        """
        self.b._restart_keys = {"player_name", "osc_mode", "lang"}
        nodes, _h = build_scene(self.b)
        line = next(t for t in
                    (n.get("text", "") for n in nodes)
                    if t.startswith("Restart to apply"))
        self.assertNotIn("\u2026", line, line)
        for key in ("player_name", "osc_mode", "lang"):
            self.assertIn(self.cfg.label_for(key), line)

    def test_a_live_setting_raises_nothing(self):
        """The half that keeps the banner worth reading. Most settings apply
        immediately, and a banner after every write would be furniture."""
        nodes, handlers = build_scene(self.b)
        handlers["set-lang"]["select"](1, "Dubbed")
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-restart", ids(nodes))

    def test_rewriting_the_same_value_is_not_a_change(self):
        """`on_commit` fires when a field loses focus, whether or not
        anything was typed -- so this is the ordinary case of clicking from
        one row to the next, and it must not raise a banner."""
        current = self.cfg.values["player_name"]
        nodes, _h = self._change_a_restart_setting(current)
        self.assertNotIn("banner-restart", ids(nodes))

    def test_the_value_is_still_saved(self):
        """The banner is a notice, not a gate. Nothing about needing a
        restart stops the setting being written."""
        self._change_a_restart_setting("Newname")
        self.assertEqual(self.cfg.values["player_name"], "Newname")

    def test_later_puts_it_away_without_restarting(self):
        nodes, handlers = self._change_a_restart_setting()
        handlers["banner-restart-dismiss"]["click"]()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-restart", ids(nodes))
        self.assertEqual(self.ctl.restarts, 0)

    def test_the_restart_banner_outranks_the_other_two(self):
        """It is the only banner about something the user did seconds ago,
        and the only one they can dismiss by acting on it. Put third it
        would be invisible to exactly the people most likely to be changing
        settings -- anyone offline. Nothing else asserts the ordering, so
        moving the block below the others was a free mutation."""
        self.b._offline = True
        self.b._update = {"version": "9.9.9", "url": "http://example.invalid"}
        nodes, _h = self._change_a_restart_setting()
        ids = ids_of = {n.get("id") for n in nodes}
        self.assertIn("banner-restart-dismiss", ids)
        self.assertNotIn("banner-open", ids)      # the update banner
        self.assertNotIn("banner-retry", ids)     # the offline banner

    def test_later_asks_for_a_repaint(self):
        """A scene assertion is not a repaint assertion: `build_scene`
        renders when asked, so the dismissal test passes against a handler
        that clears the keys and never redraws -- and the user would click
        Later and watch the banner stay."""
        nodes, handlers = self._change_a_restart_setting()
        seen = []
        self.b.invalidate = lambda *a: seen.append(1)
        handlers["banner-restart-dismiss"]["click"]()
        self.assertTrue(seen)

    def test_restart_now_restarts(self):
        nodes, handlers = self._change_a_restart_setting()
        handlers["banner-restart"]["click"]()
        self.assertEqual(self.ctl.restarts, 1)

    def test_the_restart_is_told_which_settings_it_is_for(self):
        """The relaunch re-passes the command-line overrides that describe
        how this copy is running -- and three of them (`--scale`,
        `--mpv-loglevel`, `--gui`) name a setting that requires a restart.
        `main` applies those on top of the saved config, so a restart that
        did not say what it was for would come back with the old value and
        never apply the change, however many times the user pressed it."""
        nodes, handlers = self._change_a_restart_setting()
        handlers["banner-restart"]["click"]()
        self.assertEqual(self.ctl.restart_pending, [{"player_name"}])

    def test_no_button_where_the_app_cannot_restart_itself(self):
        """A button that takes the app away and does not bring it back is
        worse than no button, so the notice stands on its own and Later is
        still there to dismiss it."""
        self.ctl.restart_possible = False
        nodes, _h = self._change_a_restart_setting()
        self.assertNotIn("banner-restart", ids(nodes))
        self.assertIn("banner-restart-dismiss", ids(nodes))
        self.assertIn("Restart", self._texts(nodes))

    def test_a_restart_that_will_not_start_says_so_and_keeps_the_banner(self):
        """The user is left with a working app and an unapplied setting.
        Clearing the banner first would have left them with no sign of
        either."""
        self.ctl.restart_ok = False
        nodes, handlers = self._change_a_restart_setting()
        handlers["banner-restart"]["click"]()
        self.assertIn("Could not restart", self.b.status)
        nodes, _h = build_scene(self.b)
        self.assertIn("banner-restart", ids(nodes))

    def test_a_controller_without_the_seam_offers_no_button(self):
        """The browser is built against stand-ins and older gateways; a
        missing method must degrade to the notice rather than raise into
        the banner and take the whole window with it.

        Set to None rather than deleted, which is the same test: the guard
        is a ``getattr(..., None)`` and an absent attribute reaches it as
        None, so both spellings take the identical branch.
        """
        self.ctl.can_restart = None
        nodes, _h = self._change_a_restart_setting()
        self.assertNotIn("banner-restart", ids(nodes))
        self.assertIn("banner-restart-dismiss", ids(nodes))


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

class TestGamepadSwapAppliesLive(unittest.TestCase):
    """The confirm/back swap re-pushes the binding table.

    It has to apply without a restart, because the setting exists for
    somebody who has just pressed A and gone *back*: pressing it again is
    how they find out whether they picked the right switch. (`input_gamepad`
    beside it genuinely does need one -- mpv reads that option at startup.)
    """

    def _browser(self):
        from unittest import mock

        cfg = FakeConfig()
        cfg.schema["gamepad_swap_confirm"] = "bool"
        cfg.values["gamepad_swap_confirm"] = False
        cfg.schema["player_name"] = "str"
        cfg.values["player_name"] = "x"
        b = MpvtkBrowser(app=mock.Mock(), source=FakeSource(),
                         controller=mock.Mock(), config=cfg)
        b._pool = _SyncPool()
        b.app.push_gamepad.reset_mock()
        return b

    def test_saving_it_re_pushes_the_table(self):
        b = self._browser()
        b._set_setting("gamepad_swap_confirm", True)
        b.app.push_gamepad.assert_called_once_with()

    def test_an_unrelated_setting_does_not(self):
        b = self._browser()
        b._set_setting("player_name", "Bud")
        b.app.push_gamepad.assert_not_called()


class TestClockFormatAppliesLive(unittest.TestCase):
    """A Live TV listing's air time is baked into its tile caption.

    So a repaint is not enough: the guide and the "Ends at" labels are ASS
    and redraw, but every tile on screen keeps printing the format that has
    just been turned off until it ages out of the LRU. Same shape as
    `logo_legibility`, which is the other setting baked into a strip.
    """

    def _browser(self):
        cfg = FakeConfig()
        cfg.schema["clock_12h"] = "bool"
        cfg.values["clock_12h"] = False
        cfg.schema["player_name"] = "str"
        cfg.values["player_name"] = "x"
        b = MpvtkBrowser(app=None, source=FakeSource(), config=cfg)
        b._pool = _SyncPool()
        self.applied = []
        b.apply_clock_format = lambda: self.applied.append(True)
        return b

    def test_saving_it_retags_the_strips(self):
        b = self._browser()
        b._set_setting("clock_12h", True)
        self.assertEqual(self.applied, [True])

    def test_turning_it_off_again_does_too(self):
        """Both directions: the captions are equally stale coming back."""
        b = self._browser()
        b._set_setting("clock_12h", True)
        b._set_setting("clock_12h", False)
        self.assertEqual(self.applied, [True, True])

    def test_an_unrelated_setting_does_not(self):
        b = self._browser()
        b._set_setting("player_name", "Bud")
        self.assertEqual(self.applied, [])

    def test_it_is_not_a_restart_setting(self):
        """The banner means "nothing has happened yet", and something has."""
        from jellyfin_mpv_shim.mpvtk_browser import config

        self.assertNotIn("clock_12h", config.RESTART_REQUIRED)


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

class TestTheTabSplit(unittest.TestCase):
    """The config form is one page per tab now (config.TAB_SECTIONS).

    A reorganization's failure mode is not a crash, it is a setting that
    quietly stops being reachable — or one that appears on three pages at
    once. Both are cheap to pin and impossible to eyeball on a form this
    size, so they are pinned.
    """

    def _real(self):
        from jellyfin_mpv_shim.mpvtk_browser import config as real
        return real

    def _keys(self, groups):
        return [k for _t, keys in groups for k in keys]

    def test_no_setting_was_lost_in_the_split(self):
        real = self._real()
        everywhere = set(self._keys(real.sections()))
        tabbed = set()
        for tab in real.TAB_SECTIONS:
            tabbed |= set(self._keys(real.sections(tab)))
        self.assertEqual(everywhere - tabbed, set(),
                         "these settings are on no tab and cannot be reached")

    def test_no_setting_is_on_two_tabs(self):
        real = self._real()
        seen, twice = set(), set()
        for tab in real.TAB_SECTIONS:
            for key in self._keys(real.sections(tab)):
                (twice if key in seen else seen).add(key)
        self.assertEqual(twice, set(),
                         "these settings are drawn on more than one tab")

    def test_advanced_lands_on_exactly_one_tab(self):
        """Advanced is "everything uncurated", computed against every tab's
        keys. Computed per tab it would list the other two tabs' settings as
        uncurated — every key on every page, and the split would have made
        the form longer."""
        from jellyfin_mpv_shim.i18n import _ as translate

        real = self._real()
        title = translate("Advanced")
        with_advanced = [tab for tab in real.TAB_SECTIONS
                         if title in dict(real.sections(tab))]
        self.assertEqual(with_advanced, [real.ADVANCED_TAB])
        advanced = set(dict(real.sections(real.ADVANCED_TAB))[title])
        # Curated groups only -- Advanced is itself one of the groups on the
        # General tab, so comparing it against every group would compare it
        # against itself.
        curated = {k for tab in real.TAB_SECTIONS
                   for t, keys in real.sections(tab) if t != title
                   for k in keys}
        self.assertEqual(curated & advanced, set(),
                         "these are curated onto a tab AND listed under "
                         "Advanced, so they are editable in two places")

    def test_every_tab_actually_has_something_on_it(self):
        # A tab in the bar that renders an empty page reads as broken.
        real = self._real()
        for tab in real.TAB_SECTIONS:
            with self.subTest(tab=tab):
                self.assertTrue(real.sections(tab), "%s is empty" % tab)

    def test_an_unknown_tab_draws_nothing_rather_than_everything(self):
        # sections(None) means "every group" — the answer for "is this key
        # reachable at all". A typo'd tab name must not silently get that.
        real = self._real()
        self.assertEqual(real.sections("nonesuch"), [])
        self.assertTrue(real.sections(None))

    def test_the_activity_split_actually_holds(self):
        """The tabs are meant to divide by what you are doing. Spot-checked
        on the settings people go looking for, because the grouping is the
        whole feature and a later edit can quietly undo it."""
        real = self._real()
        expected = {
            "playback": ["osc_style", "audio_device", "subtitle_size",
                         "transcode_hevc", "skip_intro_on_seek"],
            # Downloading is acquiring library content for later, so it
            # browses rather than watches -- and the Downloads *tab* is the
            # manager, already full of per-item media management.
            "browse": ["theme", "scroll_mode", "poster_scale",
                       "library_image_cache_mb", "sync_path",
                       "auto_download_enable"],
            "general": ["player_name", "window_controls", "check_updates"],
        }
        for tab, keys in expected.items():
            on_tab = set(self._keys(real.sections(tab)))
            for key in keys:
                with self.subTest(tab=tab, key=key):
                    self.assertIn(key, on_tab)
        # The keep-running pair is one question asked of two machines, and
        # sections() shows whichever this one can honour -- so assert on the
        # pair, not on either name.
        self.assertTrue(
            {"close_to_tray", "allow_background"}
            & set(self._keys(real.sections("general"))),
            "neither keep-running toggle is on the General tab")


class TestHomeScreenTab(unittest.TestCase):
    """The Home Screen tab: which rows the home screen has, and how two of
    them are illustrated. Both are per-user preferences held on the
    *server*.

    Everything here is shared with jellyfin-web, which is why it loads and
    saves asynchronously and why a stale read must never be written back.

    The artwork half was a tab of its own ("Display") holding that single
    checkbox; it lives here now because what it governs is the Next Up and
    Continue Watching *rows*, so the tab that decides whether you have those
    rows is the tab that decides what they look like.
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
        # Present so the tab is not in its offline state; the layout half is
        # not what these assert on.
        src.save_home_layout = lambda *a: None
        b = MpvtkBrowser(app=None, source=src, config=FakeConfig())
        b._pool = _SyncPool()
        b.server = "srv1"
        b._open_settings()
        b.route["_tab"] = "home"
        # These preferences live on the server, so the first frame of the tab
        # is a spinner that kicks the fetch off. Under _SyncPool the fetch
        # completes inside that frame, so one throwaway build leaves the tab
        # showing its real contents.
        build_scene(b)
        return b

    def test_the_layout_and_the_artwork_option_share_one_page(self):
        # The point of the fold: one page, one fetch, one error state.
        b = self._browser()
        nodes, _h = build_scene(b)
        got = ids(nodes)
        self.assertIn("home-slot-0", got)
        self.assertIn("display-episode-images", got)

    def test_both_halves_come_from_one_fetch(self):
        # Two reads of the same DisplayPreferences document. Two spinners and
        # two retries on one page would be a worse screen than the two tabs
        # this replaced.
        b = self._browser()
        self.assertIsNotNone(b.route.get("_home_layout"))
        self.assertIsNotNone(b.route.get("_display_prefs"))

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
        self.assertIn("home-retry", ids(nodes))
        self.assertNotIn("display-episode-images", ids(nodes))
        self.assertNotIn("home-slot-0", ids(nodes))

    def test_offline_says_so_instead_of_failing_at_save_time(self):
        src = FakeSource()
        # Neither half of the page has a server to write to. Offering the
        # controls would fail only at save time, which is the worst moment.
        self.assertFalse(hasattr(src, "save_user_prefs"))
        self.assertFalse(hasattr(src, "save_home_layout"))
        b = MpvtkBrowser(app=None, source=src, config=FakeConfig())
        b._pool = _SyncPool()
        b._open_settings()
        b.route["_tab"] = "home"
        build_scene(b)
        nodes, _h = build_scene(b)
        self.assertNotIn("display-episode-images", ids(nodes))
        self.assertNotIn("home-retry", ids(nodes))
        self.assertNotIn("home-slot-0", ids(nodes))

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
        src.save_home_layout = lambda *a: None
        b._open_settings()
        b.route["_tab"] = "home"
        build_scene(b)
        self.assertFalse(b.route.get("_home_loading"),
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


class TestAdvancedGroupMembership(unittest.TestCase):
    """The disclosure is membership in a set, not a group's name.

    It used to be `title == "Advanced"`, which capped a tab at one hidden
    group and forced it to be called that — so #661's three tuning fields
    could not be tucked away beside the settings they qualify without being
    moved to another tab entirely.
    """

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.cfg = self.b._config()
        # _config() hands back a shared object, so anything overwritten
        # here leaks into every later test in the file. (Found exactly that
        # way: five unrelated settings tests started erroring.)
        #
        # Asserted, not `if hasattr`. These tests OVERWRITE both names, so
        # a rename made the guard register no cleanup and the assignment
        # simply invent the attribute -- all three passed while
        # `settings/general.py`'s `getattr(cfg, "ADVANCED_GROUPS", ())`
        # answered empty, the disclosure vanished and every advanced key
        # became permanently visible. A test that creates the thing it is
        # testing cannot fail.
        for attr in ("sections", "ADVANCED_GROUPS"):
            self.assertTrue(
                hasattr(self.cfg, attr),
                "config has no %r -- these tests would otherwise invent it "
                "and pass against a renamed or deleted attribute" % attr)
            self.addCleanup(setattr, self.cfg, attr,
                            getattr(self.cfg, attr))

    def test_production_reads_the_name_these_tests_override(self):
        """The other half: the attribute existing is not enough if the
        reader has moved on to a different one."""
        import inspect
        from jellyfin_mpv_shim.mpvtk_browser.settings import general
        self.assertIn("ADVANCED_GROUPS", inspect.getsource(general),
                      "settings/general.py no longer reads ADVANCED_GROUPS, "
                      "so overriding it here proves nothing")

    def _ids(self):
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        return ids(nodes)

    def test_a_second_advanced_group_is_also_hidden(self):
        self.cfg.sections = lambda tab=None: (
            [] if tab in ("browse", "playback") else
            [("Interface", ["player_name"]),
             ("Advanced", ["autoplay"]),
             ("Tuning", ["seek_up"])])
        self.cfg.ADVANCED_GROUPS = frozenset({"Advanced", "Tuning"})
        present = self._ids()
        self.assertIn("set-adv", present)
        self.assertNotIn("set-autoplay", present)
        self.assertNotIn("set-seek_up", present)

    def test_one_checkbox_however_many_groups(self):
        """Two would be two controls for one piece of state."""
        self.cfg.sections = lambda tab=None: (
            [] if tab in ("browse", "playback") else
            [("Advanced", ["autoplay"]), ("Tuning", ["seek_up"])])
        self.cfg.ADVANCED_GROUPS = frozenset({"Advanced", "Tuning"})
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        self.assertEqual(
            sum(1 for n in nodes if n.get("id") == "set-adv"), 1)

    def test_a_group_not_in_the_set_stays_visible(self):
        self.cfg.sections = lambda tab=None: (
            [] if tab in ("browse", "playback") else
            [("Interface", ["player_name"]), ("Tuning", ["seek_up"])])
        self.cfg.ADVANCED_GROUPS = frozenset({"Advanced"})
        present = self._ids()
        self.assertIn("set-seek_up", present)
        self.assertNotIn("set-adv", present)
