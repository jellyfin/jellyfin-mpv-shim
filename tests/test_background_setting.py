"""The tray-dependent pair in the settings form.

"Keep running once the window is gone" is one question with two very
different answers depending on whether a tray exists, so the form shows
exactly one of the two checkboxes -- and the one it hides must not resurface
under "Advanced", which is where sections() puts anything uncurated.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # conffile reaches args.get_args() on import

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.settings import SettingsMixin  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.ui import UserInterface  # noqa: E402


class TrayDependentSectionsTest(unittest.TestCase):
    def _keys(self, tray):
        with mock.patch.object(cfg, "tray_available", lambda: tray):
            return {k for _t, keys in cfg.sections() for k in keys}

    def test_tray_present_offers_close_to_tray_only(self):
        keys = self._keys(tray=True)
        self.assertIn("close_to_tray", keys)
        self.assertNotIn("allow_background", keys)

    def test_no_tray_offers_allow_background_only(self):
        keys = self._keys(tray=False)
        self.assertIn("allow_background", keys)
        self.assertNotIn("close_to_tray", keys)

    def test_both_keys_are_editable_settings(self):
        # If either dropped out of the schema the swap would silently show
        # nothing at all on that machine.
        schema = cfg.settings_schema()
        for key in cfg.TRAY_DEPENDENT:
            self.assertEqual(schema.get(key), "bool", key)

    def test_the_hidden_one_is_labelled(self):
        for key in cfg.TRAY_DEPENDENT:
            self.assertTrue(cfg.label_for(key))


class StartMinimizedVisibilityTest(unittest.TestCase):
    """start_minimized depends on whichever "keep running" toggle is on
    screen, so it is offered only when that one is enabled -- and never
    resurfaces under Advanced when it isn't."""

    def _section(self, tray, keep_running):
        key = "close_to_tray" if tray else "allow_background"
        with mock.patch.object(cfg, "tray_available", lambda: tray), \
                mock.patch.object(settings, key, keep_running):
            return cfg.sections()

    def _keys(self, tray, keep_running):
        return {k for _t, keys in self._section(tray, keep_running)
                for k in keys}

    def test_offered_when_the_companion_is_on(self):
        for tray in (True, False):
            self.assertIn("start_minimized", self._keys(tray, True), tray)

    def test_hidden_when_the_companion_is_off(self):
        for tray in (True, False):
            self.assertNotIn("start_minimized", self._keys(tray, False), tray)

    def test_it_follows_the_companion_in_the_form(self):
        # Order is the explanation: the dependent setting reads as a
        # refinement of the one above it, not an unrelated toggle.
        for title, keys in self._section(True, True):
            if "start_minimized" in keys:
                self.assertLess(keys.index("close_to_tray"),
                                keys.index("start_minimized"))
                return
        self.fail("start_minimized was not offered at all")


class StartMinimizedGateTest(unittest.TestCase):
    """`--minimized` has to work on a machine with no tray -- that is the
    whole point of passing it on a headless-ish box."""

    def _ui(self, tray_available):
        ui = UserInterface()
        ui._tray = mock.Mock(available=tray_available) if tray_available is not None else None
        return ui

    def _may(self, ui, flag_passed):
        args = mock.Mock(start_minimized=True if flag_passed else None)
        with mock.patch("jellyfin_mpv_shim.args.get_args", return_value=args):
            return ui._may_start_minimized()

    def test_tray_available_is_enough(self):
        self.assertTrue(self._may(self._ui(True), flag_passed=False))

    def test_no_tray_and_config_only_is_refused(self):
        with mock.patch.object(settings, "allow_background", False):
            self.assertFalse(self._may(self._ui(False), flag_passed=False))

    def test_the_command_line_flag_is_honoured_without_a_tray(self):
        with mock.patch.object(settings, "allow_background", False):
            self.assertTrue(self._may(self._ui(False), flag_passed=True))

    def test_allow_background_is_enough_without_the_flag(self):
        with mock.patch.object(settings, "allow_background", True):
            self.assertTrue(self._may(self._ui(False), flag_passed=False))

    def test_no_tray_object_at_all_behaves_like_no_tray(self):
        with mock.patch.object(settings, "allow_background", False):
            self.assertFalse(self._may(self._ui(None), flag_passed=False))


class CompanionResetTest(unittest.TestCase):
    """Turning the companion off hides start_minimized, so its value has to
    go with it -- otherwise it keeps acting at every startup from a checkbox
    that is no longer on screen to untick."""

    def setUp(self):
        self.ui = mock.Mock()
        self.ui._config.return_value = cfg
        self.saved = []
        real_set = cfg.set_setting

        def record(key, value):
            self.saved.append((key, value))
            return real_set(key, value)

        patcher = mock.patch.object(cfg, "set_setting", record)
        patcher.start()
        self.addCleanup(patcher.stop)
        # set_setting persists; there is no config file under test.
        saver = mock.patch.object(settings, "save", lambda *a, **k: None)
        saver.start()
        self.addCleanup(saver.stop)
        for key in ("close_to_tray", "allow_background", "start_minimized"):
            self.addCleanup(setattr, settings, key, getattr(settings, key))

    def _set(self, key, value):
        SettingsMixin._set_setting(self.ui, key, value)

    def test_turning_off_close_to_tray_clears_start_minimized(self):
        settings.start_minimized = True
        self._set("close_to_tray", False)
        self.assertFalse(settings.start_minimized)
        self.assertIn(("start_minimized", False), self.saved)

    def test_turning_off_allow_background_clears_start_minimized(self):
        settings.start_minimized = True
        self._set("allow_background", False)
        self.assertFalse(settings.start_minimized)

    def test_turning_the_companion_on_leaves_it_alone(self):
        settings.start_minimized = False
        self._set("close_to_tray", True)
        self.assertEqual(self.saved, [("close_to_tray", True)])

    def test_no_pointless_write_when_it_was_already_off(self):
        settings.start_minimized = False
        self._set("close_to_tray", False)
        self.assertEqual(self.saved, [("close_to_tray", False)])

    def test_the_user_is_told(self):
        # A silent reset of a setting the user did not touch is the thing
        # being avoided here, not just the stale value.
        settings.start_minimized = True
        self._set("close_to_tray", False)
        status = self.ui.set_status.call_args[0][0]
        self.assertIn(cfg.label_for("start_minimized"), status)


class BackgroundNoteTest(unittest.TestCase):
    """The note is the only place the way out is written down, so it has to
    appear exactly when there is no other way out."""

    def _note(self):
        return SettingsMixin._dynamic_note(mock.Mock(), "allow_background")

    def test_note_appears_once_enabled(self):
        with mock.patch.object(settings, "allow_background", True):
            note = self._note()
        self.assertIsNotNone(note)
        self.assertIn("jellyfin-mpv-shim stop", note)

    def test_no_note_while_disabled(self):
        with mock.patch.object(settings, "allow_background", False):
            self.assertIsNone(self._note())

    def test_it_is_not_a_static_note(self):
        # A NOTES entry would render unconditionally and win over the dynamic
        # one, which is the mistake this guards.
        self.assertNotIn("allow_background", cfg.NOTES)


if __name__ == "__main__":
    unittest.main()
