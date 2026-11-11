"""The startup-error dialog: what it says, when it stays quiet, and that it never raises."""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import autostart, startup_error  # noqa: E402


def _missing_mpv():
    try:
        try:
            raise OSError("Cannot find libmpv.so.2")
        except OSError as first:
            raise FileNotFoundError(2, "No such file or directory", "mpv") from first
    except FileNotFoundError as e:
        return e


class HintTest(unittest.TestCase):
    def test_a_missing_libmpv_names_the_package_on_linux(self):
        self.assertIn("libmpv2", startup_error.hint_for(_missing_mpv(), "linux"))

    def test_windows_points_at_the_graphics_driver_not_a_package_manager(self):
        hint = startup_error.hint_for(_missing_mpv(), "win32")
        self.assertIn("graphics driver", hint)
        self.assertNotIn("apt", hint)

    def test_macos_says_reinstall(self):
        self.assertIn("Reinstall", startup_error.hint_for(_missing_mpv(), "darwin"))

    def test_an_unrelated_error_gets_no_invented_advice(self):
        self.assertEqual(startup_error.hint_for(ValueError("bad config"), "linux"), "")

    def test_the_message_has_details_and_a_way_to_get_the_log(self):
        with mock.patch.object(startup_error, "log_location", return_value=None):
            title, body = startup_error.message_for(ValueError("boom\nsecond line"), "linux")
        self.assertIn("could not start", title)
        self.assertIn("ValueError: boom", body)
        self.assertNotIn("second line", body)          # first line only
        self.assertIn("terminal", body)

    def test_the_log_path_is_named_when_there_is_one(self):
        with mock.patch.object(startup_error, "log_location", return_value="/x/log.txt"):
            self.assertIn("/x/log.txt", startup_error.message_for(ValueError("e"), "linux")[1])


class ShowTest(unittest.TestCase):
    def test_linux_tries_zenity_first_and_stops_after_one_dialog(self):
        with mock.patch.object(startup_error.shutil, "which", side_effect=lambda n: "/usr/bin/" + n), \
                mock.patch.object(startup_error.subprocess, "run") as run:
            self.assertTrue(startup_error.show("T", "B", "linux"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][0], "zenity")
        self.assertEqual(run.call_args[1]["timeout"], startup_error.DIALOG_TIMEOUT)

    def test_with_no_helper_installed_it_reports_that_nothing_was_shown(self):
        with mock.patch.object(startup_error.shutil, "which", return_value=None):
            self.assertFalse(startup_error.show("T", "B", "linux"))

    def test_macos_quotes_the_message_for_applescript(self):
        cmd = startup_error._commands('T"', 'a "quoted" \\ line', "darwin")[0]
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertIn('\\"quoted\\"', cmd[2])
        self.assertIn("\\\\", cmd[2])

    def test_a_failing_helper_never_raises(self):
        with mock.patch.object(startup_error.shutil, "which", return_value="/x"), \
                mock.patch.object(startup_error.subprocess, "run", side_effect=OSError("no")):
            self.assertFalse(startup_error.show("T", "B", "linux"))


class ReportFatalTest(unittest.TestCase):
    def test_it_is_silent_when_a_terminal_is_attached(self):
        with mock.patch.object(startup_error, "_interactive", return_value=True), \
                mock.patch.object(startup_error, "show") as show:
            self.assertFalse(startup_error.report_fatal(ValueError("x"), "linux"))
        show.assert_not_called()

    def test_it_shows_a_dialog_when_launched_from_a_menu(self):
        with mock.patch.object(startup_error, "_interactive", return_value=False), \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(startup_error, "show", return_value=True) as show:
            os.environ.pop("JELLYHOME_NO_ERROR_DIALOG", None)
            self.assertTrue(startup_error.report_fatal(_missing_mpv(), "linux"))
        self.assertIn("libmpv2", show.call_args[0][1])

    def test_the_opt_out_variable_suppresses_it(self):
        with mock.patch.object(startup_error, "_interactive", return_value=False), \
                mock.patch.dict(os.environ, {"JELLYHOME_NO_ERROR_DIALOG": "1"}), \
                mock.patch.object(startup_error, "show") as show:
            startup_error.report_fatal(ValueError("x"), "linux")
        show.assert_not_called()


class MainWrapperTest(unittest.TestCase):
    """``mpv_shim.main`` reports an exception that escapes startup, and is otherwise itself."""

    def wrapped(self, body):
        from jellyfin_mpv_shim import mpv_shim
        return mpv_shim._report_startup_failure(body)

    def test_main_is_still_the_inspectable_startup_body(self):
        import inspect
        from jellyfin_mpv_shim import mpv_shim
        self.assertTrue(hasattr(mpv_shim.main, "__wrapped__"))
        self.assertEqual(mpv_shim.main.__name__, "main")
        src = inspect.getsource(mpv_shim.main)
        for marker in ("args = get_args()", "_claim_sigterm(halt)", "set_final_action", "exit_watchdog.finish()"):
            self.assertIn(marker, src)
        self.assertNotIn("report_fatal", src, "the report lives in the decorator, not the body")

    def test_an_escaping_exception_is_reported_once_and_reraised_unchanged(self):
        boom = ValueError("no mpv here")

        def startup_body():
            raise boom

        raised = None
        with mock.patch.object(startup_error, "report_fatal") as report:
            try:                  # not assertRaises: it strips the traceback this checks
                self.wrapped(startup_body)()
            except ValueError as exc:
                raised = exc
        self.assertIs(raised, boom)
        report.assert_called_once_with(boom)
        import traceback
        self.assertIn("startup_body", "".join(traceback.format_exception(raised)))

    def test_a_normal_start_reports_nothing_and_passes_arguments_and_result_through(self):
        with mock.patch.object(startup_error, "report_fatal") as report:
            self.assertEqual(self.wrapped(lambda a, b=0: a + b)(1, b=2), 3)
        report.assert_not_called()

    def test_exit_and_interrupt_are_not_intercepted(self):
        for exc in (SystemExit(1), KeyboardInterrupt()):
            def body(exc=exc):
                raise exc
            with mock.patch.object(startup_error, "report_fatal") as report:
                with self.assertRaises(type(exc)):
                    self.wrapped(body)()
            report.assert_not_called()

    def test_a_failing_report_never_masks_the_original_error(self):
        boom = RuntimeError("the real failure")

        def body():
            raise boom

        with mock.patch.object(startup_error, "report_fatal", side_effect=OSError("dialog broke")):
            with self.assertRaises(RuntimeError) as caught:
                self.wrapped(body)()
        self.assertIs(caught.exception, boom)


class AutostartLauncherTest(unittest.TestCase):
    def test_a_package_launcher_is_what_autostart_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            launcher = os.path.join(d, "jellyhome-player")
            with open(launcher, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(launcher, 0o755)
            with mock.patch.dict(os.environ, {"JELLYHOME_LAUNCHER": launcher}):
                self.assertEqual(autostart._launch_args(), [launcher, "--minimized"])

    def test_a_stale_launcher_path_falls_back_to_the_module_form(self):
        with mock.patch.dict(os.environ, {"JELLYHOME_LAUNCHER": "/nonexistent/x"}):
            args = autostart._launch_args()
        self.assertEqual(args[1:3], ["-m", "jellyfin_mpv_shim.mpv_shim"])


if __name__ == "__main__":
    unittest.main()
