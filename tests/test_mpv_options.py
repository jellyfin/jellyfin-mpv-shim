"""The option set mpv is constructed with.

These assertions used to be untestable: the code lived in the first 170 lines
of ``PlayerManager._init_mpv``, so reaching it meant constructing a player,
and on the libmpv backend that opens a real window. ``mpv_options`` imports
nothing from ``player``, so this module is cheap and side-effect free --
which the first test pins down, because it is the property that makes the
rest of the file possible.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import mpv_options            # noqa: E402
from jellyfin_mpv_shim.conf import settings          # noqa: E402
from jellyfin_mpv_shim.constants import DESKTOP_ID   # noqa: E402


class ImportIsFreeTest(unittest.TestCase):
    def test_it_does_not_drag_in_the_player_or_a_backend(self):
        # player.py builds its singleton at import and _init_mpv opens a
        # window. If option assembly ever reaches back into it, this file
        # stops being runnable without an X server and the extraction has
        # been undone.
        import subprocess

        code = ("import sys; sys.argv=[sys.argv[0]];"
                "import jellyfin_mpv_shim.mpv_options;"
                "bad=[m for m in ('jellyfin_mpv_shim.player','mpv',"
                "'python_mpv_jsonipc') if m in sys.modules];"
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "",
                         "mpv_options pulled in a module it must not need")


class SettingsCase(unittest.TestCase):
    """Base that restores every settings key a test touches."""

    def setUp(self):
        self._saved = {}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(settings, k, v)

    def set(self, **kw):
        for k, v in kw.items():
            self._saved.setdefault(k, getattr(settings, k))
            setattr(settings, k, v)

    def build(self, style=None, scripts=(), ext_mpv=False, window=False):
        if style is None:
            style = mpv_options.resolve_osc_style()
        return mpv_options.build_mpv_options(style, list(scripts), ext_mpv,
                                             window)


class ResolveOscStyleTest(SettingsCase):
    def test_jellyfin_is_a_legacy_alias_for_the_hud(self):
        self.set(osc_style="jellyfin", enable_gui=True,
                 thumbnail_osc_builtin=True)
        self.assertEqual(mpv_options.resolve_osc_style(), "mpvtk")

    def test_without_a_gui_there_is_nothing_to_draw_the_hud(self):
        # The HUD is rendered by the library browser.
        self.set(osc_style="mpvtk", enable_gui=False,
                 thumbnail_osc_builtin=True)
        self.assertEqual(mpv_options.resolve_osc_style(), "mpv")

    def test_the_legacy_opt_out_still_yields_the_users_own_osc(self):
        self.set(osc_style="mpvtk", enable_gui=True,
                 thumbnail_osc_builtin=False)
        self.assertEqual(mpv_options.resolve_osc_style(), "default")

    def test_the_opt_out_does_not_apply_once_the_gui_fallback_has(self):
        # Order matters: enable_gui=False moves mpvtk -> mpv, and "mpv" is
        # not subject to the thumbnail_osc_builtin opt-out.
        self.set(osc_style="mpvtk", enable_gui=False,
                 thumbnail_osc_builtin=False)
        self.assertEqual(mpv_options.resolve_osc_style(), "mpv")

    def test_explicit_styles_pass_through(self):
        for style in ("mpv", "default", "none"):
            self.set(osc_style=style, enable_gui=True,
                     thumbnail_osc_builtin=True)
            self.assertEqual(mpv_options.resolve_osc_style(), style)

    def test_no_controls_is_not_talked_out_of_it(self):
        """Both fallbacks exist to find something to draw the HUD with.
        Someone who asked for nothing has not got a problem to solve."""
        self.set(osc_style="none", enable_gui=False,
                 thumbnail_osc_builtin=False)
        self.assertEqual(mpv_options.resolve_osc_style(), "none")


class ScriptListTest(SettingsCase):
    def names(self, *a, **kw):
        import os
        return [os.path.basename(p) for p in mpv_options.mpv_scripts(*a, **kw)]

    def test_load_order_is_mouse_then_thumbfast_then_osc(self):
        self.set(menu_mouse=True)
        self.assertEqual(self.names("mpv", True),
                         ["mouse.lua", "thumbfast.lua", "trickplay-osc.lua"])

    def test_only_the_mpv_style_loads_an_osc_script(self):
        self.set(menu_mouse=False)
        for style in ("mpvtk", "default"):
            self.assertNotIn("trickplay-osc.lua", self.names(style, True))

    def test_thumbfast_follows_the_worker_not_the_setting(self):
        # A TrickPlay worker that failed to start must not advertise
        # thumbfast to mpv; the caller passes what actually happened.
        self.set(menu_mouse=False)
        self.assertEqual(self.names("default", False), [])
        self.assertEqual(self.names("default", True), ["thumbfast.lua"])

    def test_mouse_script_is_opt_in(self):
        self.set(menu_mouse=False)
        self.assertEqual(self.names("default", False), [])


class OscOptionTest(SettingsCase):
    def test_the_builtin_osc_is_held_off_for_both_shim_styles(self):
        for style in ("mpv", "mpvtk"):
            self.assertIs(self.build(style)["osc"], False)

    def test_no_controls_means_mpv_s_own_are_off_too(self):
        """"none" is the only style that replaces the OSC with nothing, so
        it is also the only one where forgetting this would leave mpv's own
        controls as the answer to "no controls please" (#615)."""
        self.assertIs(self.build("none")["osc"], False)

    def test_default_leaves_the_users_osc_alone(self):
        self.assertNotIn("osc", self.build("default"))


class ScriptPassingTest(SettingsCase):
    def test_external_mpv_takes_a_list(self):
        self.set(mpv_ext=True)
        opts = self.build("default", ["a.lua", "b.lua"])
        self.assertEqual(opts["script"], ["a.lua", "b.lua"])
        self.assertNotIn("scripts", opts)

    def test_libmpv_takes_a_joined_string(self):
        self.set(mpv_ext=False)
        opts = self.build("default", ["a.lua", "b.lua"])
        self.assertEqual(opts["scripts"], "a.lua:b.lua")
        self.assertNotIn("script", opts)

    def test_windows_joins_on_semicolons(self):
        self.set(mpv_ext=False)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(self.build("default", ["a.lua", "b.lua"])["scripts"],
                             "a.lua;b.lua")

    def test_no_scripts_means_neither_key(self):
        opts = self.build("default", [])
        self.assertNotIn("script", opts)
        self.assertNotIn("scripts", opts)


class ConfigDirTest(SettingsCase):
    def test_the_shim_config_is_used_by_default(self):
        self.set(mpv_ext=False, mpv_ext_no_ovr=True)
        self.assertIs(self.build("default")["config"], True)

    def test_external_mpv_can_keep_its_own_config(self):
        # Both halves are required: no_ovr only applies to external mpv.
        self.set(mpv_ext=True, mpv_ext_no_ovr=True)
        opts = self.build("default")
        self.assertNotIn("config", opts)
        self.assertNotIn("config_dir", opts)


class TlsTest(SettingsCase):
    def test_a_cert_needs_its_key_to_take_effect(self):
        self.set(tls_client_cert="c.pem", tls_client_key=None,
                 tls_server_ca=None)
        self.assertNotIn("tls_cert_file", self.build("default"))

    def test_a_pair_is_passed_through(self):
        self.set(tls_client_cert="c.pem", tls_client_key="k.pem",
                 tls_server_ca=None)
        opts = self.build("default")
        self.assertEqual(opts["tls_cert_file"], "c.pem")
        self.assertEqual(opts["tls_key_file"], "k.pem")
        self.assertNotIn("tls_ca_file", opts)

    def test_a_ca_is_only_read_alongside_a_pair(self):
        self.set(tls_client_cert=None, tls_client_key=None,
                 tls_server_ca="ca.pem")
        self.assertNotIn("tls_ca_file", self.build("default"))


class AudioDeviceTest(SettingsCase):
    """The device is applied LIVE, never at construction — and this is the
    test that says so, because doing both looks harmless and is not.

    apply_audio_settings snapshots mpv's device before overwriting it, so
    that choosing Default later can put it back. Constructing mpv with the
    device too means that snapshot records our own value as the original: on
    the next run, Default restored the device it was trying to leave, and the
    setting could only be undone by editing the config by hand.
    """

    def test_the_device_is_not_a_construction_option(self):
        self.set(audio_device="alsa/iec958:CARD=X,DEV=0", audio_exclusive=True)
        opts = self.build("default")
        self.assertNotIn("audio_device", opts)
        self.assertNotIn("audio_exclusive", opts)

    def test_nothing_appears_when_unset_either(self):
        self.set(audio_device=None, audio_exclusive=False)
        opts = self.build("default")
        self.assertNotIn("audio_device", opts)
        self.assertNotIn("audio_exclusive", opts)


class WindowGeometryTest(SettingsCase):
    def test_unset_sizes_fall_back_to_a_browsable_default(self):
        self.set(window_width=None, window_height=None, window_maximized=False)
        self.assertEqual(self.build("default")["geometry"], "1280x720")

    def test_absurdly_small_sizes_are_clamped(self):
        # A stored geometry small enough to make the browser unusable is
        # more likely corruption than intent.
        self.set(window_width=1, window_height=1, window_maximized=False)
        self.assertEqual(self.build("default")["geometry"], "320x240")

    def test_mpv_is_stopped_from_resizing_the_shared_window(self):
        self.assertIs(self.build("default")["auto_window_resize"], False)

    def test_maximized_is_opt_in(self):
        self.set(window_width=None, window_height=None, window_maximized=True)
        self.assertIs(self.build("default")["window_maximized"], True)
        self.set(window_maximized=False)
        self.assertNotIn("window_maximized", self.build("default"))


class ForceWindowTest(SettingsCase):
    def test_the_hud_style_asks_for_a_window_when_the_browser_wants_one(self):
        self.assertIs(self.build("mpvtk", window=True)["force_window"], True)

    def test_it_is_withheld_when_the_browser_does_not(self):
        self.assertNotIn("force_window", self.build("mpvtk", window=False))

    def test_other_styles_never_ask(self):
        # Without the in-window UI there is nothing to show in an empty
        # window; loading a file raises the VO by itself.
        for style in ("mpv", "default"):
            self.assertNotIn("force_window", self.build(style, window=True))


class DesktopHintsTest(SettingsCase):
    def test_x11_hints_are_set_where_they_exist(self):
        with mock.patch.object(sys, "platform", "linux"):
            opts = self.build("default")
        self.assertEqual(opts["x11_name"], DESKTOP_ID)
        self.assertEqual(opts["wayland_app_id"], DESKTOP_ID)

    def test_they_are_withheld_where_mpv_would_reject_them(self):
        # --x11-name only exists in X11-enabled builds; passing it makes mpv
        # exit at startup rather than raise something recoverable.
        for plat in ("win32", "darwin"):
            with mock.patch.object(sys, "platform", plat):
                opts = self.build("default")
            self.assertNotIn("x11_name", opts)
            self.assertNotIn("wayland_app_id", opts)


class ExternalMpvTest(SettingsCase):
    def test_the_ipc_block_is_backend_gated(self):
        self.set(mpv_ext_start=True, mpv_ext_ipc="/tmp/sock",
                 mpv_ext_start_retries=3, mpv_ext_start_retry_delay_ms=100,
                 mpv_ext_path="/usr/bin/mpv")
        opts = self.build("default", ext_mpv=True)
        self.assertEqual(opts["ipc_socket"], "/tmp/sock")
        self.assertEqual(opts["mpv_location"], "/usr/bin/mpv")
        self.assertEqual(opts["player-operation-mode"], "cplayer")
        self.assertEqual(opts["start_retries"], 3)
        self.assertNotIn("ipc_socket", self.build("default", ext_mpv=False))

    def test_an_unset_path_lets_the_library_find_mpv(self):
        self.set(mpv_ext_path=None)
        with mock.patch("platform.system", return_value="Linux"):
            self.assertIsNone(mpv_options.mpv_binary_location())

    def test_the_frozen_mac_build_ships_its_own(self):
        self.set(mpv_ext_path=None)
        with mock.patch("platform.system", return_value="Darwin"), \
                mock.patch.object(sys, "frozen", True, create=True):
            self.assertIsNotNone(mpv_options.mpv_binary_location())


if __name__ == "__main__":
    unittest.main()


class HwdecTest(SettingsCase):
    """Hardware decoding — the setting, and the policy behind "over-1080p".

    Off by default, following mpv rather than the other Jellyfin clients:
    mpv's manual says to "acknowledge that this may cause problems" and its
    maintainers decline to enable it by default (mpv#12948), because
    particular vendor/GPU combinations are badly broken. "over-1080p" is
    the option only *this* client can offer — the source resolution is in
    the DTO before playback starts, so decoding can be software where
    software is fine.
    """

    def test_the_default_is_software_decoding(self):
        self.assertEqual(settings.hwdec, "no")
        self.assertEqual(mpv_options.hwdec_for(2160), "no")

    def test_the_static_modes_ignore_the_height(self):
        for mode, expected in (("no", "no"), ("auto", "auto"),
                               ("auto-copy", "auto-copy")):
            self.set(hwdec=mode)
            for height in (None, 480, 1080, 2160):
                self.assertEqual(mpv_options.hwdec_for(height), expected,
                                 "%s at %r" % (mode, height))

    def test_the_threshold_is_strictly_above_1080(self):
        # 1920x1080 decodes in software and 4K does not, which is the line
        # the setting is named after.
        self.set(hwdec="over-1080p")
        self.assertEqual(mpv_options.hwdec_for(1080), "no")
        self.assertEqual(mpv_options.hwdec_for(1081), "auto")
        self.assertEqual(mpv_options.hwdec_for(2160), "auto")

    def test_an_unknown_height_stays_on_software(self):
        """Audio, a photo, or anything the server did not probe. Starting a
        file with hardware decoding already on and turning it off is the
        wrong way round — decoder init is where the bad paths hang."""
        self.set(hwdec="over-1080p")
        for height in (None, 0, "", "not a number"):
            self.assertEqual(mpv_options.hwdec_for(height), "no",
                             "height=%r" % (height,))

    def test_a_hand_edited_value_falls_back_to_software(self):
        # Handing an unknown string to mpv fails the option at
        # construction, which takes the whole player with it.
        self.set(hwdec="turbo-mode")
        with self.assertLogs("mpv_options", level="WARNING"):
            self.assertEqual(mpv_options.hwdec_for(2160), "no")

    def test_the_construction_default_is_software_for_the_threshold_mode(self):
        # Nothing is loaded at construction, so there is no height to judge;
        # _play_media raises it per file.
        self.set(hwdec="over-1080p")
        self.assertEqual(self.build()["hwdec"], "no")

    def test_a_static_mode_reaches_the_option_dict(self):
        self.set(hwdec="auto-copy")
        self.assertEqual(self.build()["hwdec"], "auto-copy")

    def test_the_cli_override_beats_every_mode(self):
        """`--disable-hwdec` is the recovery path for hardware decoding
        stopping the window opening at all, so nothing may outrank it."""
        for mode in ("auto", "auto-copy", "over-1080p"):
            self.set(hwdec=mode)
            with mock.patch.object(mpv_options, "hwdec_for",
                                   mpv_options.hwdec_for):
                with mock.patch("jellyfin_mpv_shim.args.get_args") as ga:
                    ga.return_value = mock.Mock(disable_hwdec=True)
                    self.assertEqual(mpv_options.hwdec_for(2160), "no", mode)

    def test_it_survives_argument_parsing_being_unavailable(self):
        # Imported bare in tests and tools; a missing override is "no
        # override", never a crash on the playback path.
        self.set(hwdec="auto")
        with mock.patch("jellyfin_mpv_shim.args.get_args",
                        side_effect=RuntimeError("no argv")):
            self.assertEqual(mpv_options.hwdec_for(2160), "auto")


class SourceHeightTest(unittest.TestCase):
    """What `over-1080p` judges. Read off the MediaSource, not the item: an
    item with several versions has one height per version, and the one
    playing is the one whose decoding is at stake."""

    def _height(self, source):
        from jellyfin_mpv_shim.player import _source_height
        return _source_height(mock.Mock(media_source=source))

    def test_it_reads_the_video_stream(self):
        self.assertEqual(self._height({"MediaStreams": [
            {"Type": "Audio", "Height": 999},
            {"Type": "Video", "Height": 2160}]}), 2160)

    def test_an_audio_only_source_has_no_height(self):
        self.assertIsNone(self._height({"MediaStreams": [
            {"Type": "Audio", "Channels": 2}]}))

    def test_degenerate_sources(self):
        for source in ({}, None, {"MediaStreams": []},
                       {"MediaStreams": [{"Type": "Video"}]}):
            self.assertIsNone(self._height(source), repr(source))
