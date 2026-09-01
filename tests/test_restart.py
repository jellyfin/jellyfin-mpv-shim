"""Relaunching the app, and what the relaunched copy is started with.

The dangerous half of a restart is not the spawn, it is the argv. A launch
rebuilt wrongly comes back as a *different app*: against the default config
directory instead of the one the user chose, with a recovery flag re-applied,
or -- worst -- with the password from a one-off ``--server``/``--password``
login back on the process list of a launch nobody typed.

So `command()` is built from an allowlist of parsed arguments, and these
tests are mostly about what does **not** survive.
"""

import ast
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import restart                              # noqa: E402


def fake_args(**over):
    """A parsed-args stand-in with every field `_durable_flags` looks at.

    Spelled out rather than partially filled: the function reads each with
    getattr and a default, so a stand-in missing a field would silently take
    the "not set" branch and a test for that flag would pass without the
    flag ever being considered.
    """
    base = dict(config=None, enable_gui=None, start_minimized=None,
                mpv_loglevel=None, ui_scale=None, disable_hwdec=False,
                debug=False,
                # The one-shot half. Present so a test can set them and
                # watch them NOT come through.
                reset_shaders=False, server=None, username=None,
                password="", quick_connect=False, command=[])
    base.update(over)
    return SimpleNamespace(**base)


class CommandTest(unittest.TestCase):
    def setUp(self):
        restart.cancel()
        self.addCleanup(restart.cancel)

    def _command(self, **over):
        with mock.patch("jellyfin_mpv_shim.args.get_args",
                        return_value=fake_args(**over)), \
                mock.patch.object(sys, "argv", [__file__]):
            return restart.command()

    def test_the_interpreter_and_the_script_are_named(self):
        cmd = self._command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], __file__)

    def test_the_config_directory_survives(self):
        """The one that matters most. Without it a restart from a
        non-default configuration directory comes back against the default
        one -- different servers, different settings, and no sign that
        anything went wrong."""
        cmd = self._command(config="/tmp/other-config")
        self.assertIn("--config", cmd)
        self.assertEqual(cmd[cmd.index("--config") + 1], "/tmp/other-config")

    def test_the_recovery_flag_survives(self):
        """`--disable-hwdec` is kept deliberately: a restart is exactly when
        somebody who needed it still needs it, and coming back with hardware
        decoding on would undo the thing that got the window open."""
        self.assertIn("--disable-hwdec", self._command(disable_hwdec=True))

    def test_the_per_run_overrides_survive(self):
        cmd = self._command(mpv_loglevel="debug", ui_scale=1.5, debug=True)
        self.assertIn("--debug", cmd)
        self.assertEqual(cmd[cmd.index("--mpv-loglevel") + 1], "debug")
        self.assertEqual(cmd[cmd.index("--scale") + 1], "1.5")

    def test_the_boolean_overrides_keep_their_direction(self):
        """`--gui`/`--no-gui` is a BooleanOptionalAction, so "off" is a
        different flag rather than an absent one. Dropping the negative
        would restart with the GUI back on."""
        self.assertIn("--no-gui", self._command(enable_gui=False))
        self.assertIn("--gui", self._command(enable_gui=True))
        self.assertIn("--no-minimized", self._command(start_minimized=False))
        self.assertIn("--minimized", self._command(start_minimized=True))

    def test_an_unset_override_is_not_invented(self):
        """None means "the config decides". Passing a flag for it would
        freeze the current config value onto the command line, where it
        would then outrank the setting the user is about to change."""
        cmd = self._command()
        for flag in ("--gui", "--no-gui", "--minimized", "--no-minimized",
                     "--scale", "--mpv-loglevel", "--config", "--debug"):
            self.assertNotIn(flag, cmd)

    def test_credentials_never_survive(self):
        """The security-relevant one. `--password` is documented as visible
        to other processes via ps; putting it back on a launch the user did
        not type would re-expose it, for a login that already happened."""
        cmd = self._command(server="https://example.invalid",
                            username="someone", password="hunter2",
                            quick_connect=True)
        joined = " ".join(cmd)
        self.assertNotIn("hunter2", joined)
        self.assertNotIn("someone", joined)
        self.assertNotIn("example.invalid", joined)
        self.assertNotIn("--quick-connect", cmd)

    def test_one_shot_actions_never_survive(self):
        """`--reset-shaders` is a recovery action and `add`/`clear`/`stop`
        are commands, not modes. Repeating any of them would make a restart
        do something the user asked for once."""
        cmd = self._command(reset_shaders=True, command=["clear"])
        self.assertNotIn("--reset-shaders", cmd)
        self.assertNotIn("clear", cmd)

    def test_a_frozen_build_does_not_name_the_exe_twice(self):
        """There, sys.argv[0] IS the executable, so naming both would pass
        the exe to itself as a positional -- which argparse reads as an
        unknown command and refuses to start on."""
        with mock.patch("jellyfin_mpv_shim.args.get_args",
                        return_value=fake_args()), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "/opt/app/app.exe"), \
                mock.patch.object(sys, "argv", ["/opt/app/app.exe"]):
            self.assertEqual(restart.command(), ["/opt/app/app.exe"])

    def test_an_unreconstructable_launch_answers_None(self):
        """`python -m`, an embedded interpreter, a deleted entry point.
        Saying so is what lets the UI offer a notice instead of a button
        that takes the app away and does not bring it back."""
        with mock.patch("jellyfin_mpv_shim.args.get_args",
                        return_value=fake_args()), \
                mock.patch.object(sys, "argv", ["/nonexistent/script.py"]):
            self.assertIsNone(restart.command())
            self.assertFalse(restart.supported())

    def test_unparseable_arguments_still_give_a_restart(self):
        """No flags is a worse restart than the right flags, but it is still
        a restart, and refusing to do one at all is worse than both."""
        with mock.patch("jellyfin_mpv_shim.args.get_args",
                        side_effect=RuntimeError("no argv here")), \
                mock.patch.object(sys, "argv", [__file__]):
            self.assertEqual(restart.command(), [sys.executable, __file__])


class FrozenEnvironmentTest(unittest.TestCase):
    """What a PyInstaller build must not hand its replacement.

    onefile is two processes: the bootloader extracts the archive to a temp
    directory, marks the environment, and re-runs itself -- the marked run
    being the one that starts Python. A relaunch that inherits those marks
    is read as the *same* launch's second stage, so it skips extraction and
    uses the dying parent's temp directory, which the parent deletes on the
    way out. The restarted app then has no files to run from.

    Untestable from this box (building a bootloader is a network fetch and a
    compile), so the contract is pinned here instead: these are the variables
    PyInstaller documents, and the assertions say what must happen to each.
    """

    def _env(self, frozen=True, **environ):
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(sys, "frozen", frozen, create=True):
            return restart.child_env()

    def test_the_onefile_marks_are_dropped(self):
        env = self._env(_MEIPASS2="/tmp/_MEI123",
                        _PYI_ARCHIVE_FILE="/opt/app",
                        _PYI_APPLICATION_HOME_DIR="/tmp/_MEI123",
                        _PYI_PARENT_PROCESS_LEVEL="1")
        self.assertEqual(env, {})

    def test_an_unknown_pyi_variable_is_dropped_too(self):
        """Matched by prefix rather than by name: these are private to the
        bootloader, the family has already been renamed once between major
        versions, and the failure mode of missing one is a build that will
        not start."""
        self.assertEqual(self._env(_PYI_SOMETHING_NEW="x"), {})

    def test_a_library_path_is_restored_from_the_bootloaders_copy(self):
        """PyInstaller points these at its temp directory and saves what was
        there as `<NAME>_ORIG`. Restoring is what its own documentation says
        to do before spawning; here the temp directory is additionally about
        to be deleted."""
        env = self._env(LD_LIBRARY_PATH="/tmp/_MEI123",
                        LD_LIBRARY_PATH_ORIG="/usr/local/lib")
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/usr/local/lib"})

    def test_a_library_path_the_bootloader_invented_is_removed(self):
        """No `_ORIG` means it created the variable rather than overwriting
        one, so the honest restoration is for it not to exist."""
        self.assertEqual(self._env(LD_LIBRARY_PATH="/tmp/_MEI123"), {})

    def test_the_macos_variables_are_covered(self):
        env = self._env(DYLD_LIBRARY_PATH="/tmp/_MEI",
                        DYLD_FRAMEWORK_PATH="/tmp/_MEI",
                        DYLD_FRAMEWORK_PATH_ORIG="/orig")
        self.assertEqual(env, {"DYLD_FRAMEWORK_PATH": "/orig"})

    def test_the_rest_of_the_environment_is_untouched(self):
        env = self._env(HOME="/home/someone", DISPLAY=":0",
                        _MEIPASS2="/tmp/_MEI")
        self.assertEqual(env, {"HOME": "/home/someone", "DISPLAY": ":0"})

    def test_nothing_is_stripped_when_not_frozen(self):
        """The gate is load-bearing rather than an optimisation. Run from
        source, `LD_LIBRARY_PATH` is the user's own and there is no `_ORIG`
        to restore it from -- so the frozen branch would silently delete
        it."""
        env = self._env(frozen=False, LD_LIBRARY_PATH="/usr/local/lib")
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/usr/local/lib"})

    def test_the_child_is_started_with_that_environment(self):
        """The table above is only worth having if it reaches Popen."""
        restart.request()
        self.addCleanup(restart.cancel)
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=["x"]), \
                mock.patch.object(restart, "child_env",
                                  return_value={"SENTINEL": "1"}):
            restart.relaunch_if_requested()
        self.assertEqual(popen.call_args.kwargs.get("env"), {"SENTINEL": "1"})


class WindowsSpawnTest(unittest.TestCase):
    """The creation flags, asserted as the value handed to Popen.

    Written against the call rather than by reading the source: the first
    version of this grepped for "DETACHED_PROCESS" and failed on the comment
    saying why it is not used, which is the same mistake -- checking the
    marker instead of the value -- that made the original restart bug
    invisible.
    """

    #: The real Windows values. Named here because this suite runs on Linux,
    #: where `subprocess` has neither.
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008

    def _flags(self):
        import subprocess

        restart.request()
        self.addCleanup(restart.cancel)
        with mock.patch.object(os, "name", "nt"), \
                mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP",
                                  self.CREATE_NEW_PROCESS_GROUP, create=True), \
                mock.patch.object(subprocess, "DETACHED_PROCESS",
                                  self.DETACHED_PROCESS, create=True), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=["app.exe"]):
            restart.relaunch_if_requested()
        return popen.call_args.kwargs.get("creationflags")

    def test_the_child_gets_its_own_process_group(self):
        """So a Ctrl-C in the console this copy was started from does not
        reach the new one."""
        self.assertTrue(self._flags() & self.CREATE_NEW_PROCESS_GROUP)

    def test_the_child_is_not_detached_from_the_console(self):
        """DETACHED_PROCESS was here first and is wrong for a console build:
        it gives the child no console while leaving the stdout and stderr
        handles it inherited pointing at nothing. It is also unnecessary --
        Windows does not take children down with their parent the way a
        POSIX session does -- and the restarted copy should behave exactly
        like the launch it replaces."""
        self.assertFalse(self._flags() & self.DETACHED_PROCESS)

    def test_posix_gets_a_new_session_instead(self):
        import subprocess

        restart.request()
        self.addCleanup(restart.cancel)
        with mock.patch.object(os, "name", "posix"), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=["app"]):
            restart.relaunch_if_requested()
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertNotIn("creationflags", kwargs)


class RequestTest(unittest.TestCase):
    def setUp(self):
        restart.cancel()
        self.addCleanup(restart.cancel)

    def test_nothing_is_spawned_unless_asked(self):
        """This runs at the end of EVERY exit, so the default has to be
        that an ordinary quit stays an ordinary quit."""
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(restart.relaunch_if_requested())
        popen.assert_not_called()

    def test_requesting_then_exiting_spawns_one_copy(self):
        restart.request()
        self.assertTrue(restart.requested())
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=["x"]):
            self.assertTrue(restart.relaunch_if_requested())
        self.assertEqual(popen.call_count, 1)

    def test_a_second_call_spawns_nothing(self):
        """Both exit paths can reach here -- the orderly one and the
        watchdog's forced one -- and two spawns would leave two copies
        racing for the instance lock. `_requested` is cleared on the way IN
        for this reason; `exit_watchdog` guards it as well, so deleting
        either half alone is otherwise invisible."""
        restart.request()
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=["x"]):
            self.assertTrue(restart.relaunch_if_requested())
            self.assertFalse(restart.relaunch_if_requested())
        self.assertEqual(popen.call_count, 1)

    def test_it_is_detached_from_the_terminal_that_started_us(self):
        """Otherwise a Ctrl-C in the console the old copy was started from
        reaches the new one, and the restart looks like a crash."""
        restart.request()
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=["x"]):
            restart.relaunch_if_requested()
        kwargs = popen.call_args.kwargs
        if os.name == "nt":
            self.assertTrue(kwargs.get("creationflags"))
        else:
            self.assertTrue(kwargs.get("start_new_session"))

    def test_cancelling_disarms_it(self):
        """The gateway cancels when the shutdown it armed for never
        starts. Leaving it armed would turn the user's next ordinary quit
        into a surprise restart."""
        restart.request()
        restart.cancel()
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(restart.relaunch_if_requested())
        popen.assert_not_called()

    def test_a_spawn_that_fails_does_not_raise_on_the_way_out(self):
        """By this point the app has already shut down, so raising would
        turn "the restart did not happen" into a traceback and change
        nothing else."""
        restart.request()
        with mock.patch("subprocess.Popen", side_effect=OSError("nope")), \
                mock.patch.object(restart, "command", return_value=["x"]):
            self.assertFalse(restart.relaunch_if_requested())

    def test_an_unreconstructable_launch_does_not_spawn(self):
        restart.request()
        with mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(restart, "command", return_value=None):
            self.assertFalse(restart.relaunch_if_requested())
        popen.assert_not_called()


class RestartRequiredSetTest(unittest.TestCase):
    """What may and may not be in `config.RESTART_REQUIRED`.

    Both failures here are silent. A misspelled key simply never matches,
    so the banner never appears and the setting goes on doing nothing; a
    key that belongs to another category asks for a restart that was not
    needed, which is how a banner stops being read.
    """

    def test_every_key_is_a_real_setting(self):
        from jellyfin_mpv_shim.conf import Settings
        from jellyfin_mpv_shim.mpvtk_browser import config

        unknown = sorted(k for k in config.RESTART_REQUIRED
                         if k not in Settings.__annotations__)
        self.assertEqual(unknown, [],
                         "not settings in conf.py: %s" % ", ".join(unknown))

    def test_every_key_is_reachable_from_the_settings_form(self):
        """A key nobody can change from the UI cannot raise the banner, so
        listing it says nothing. Hand-edited-only settings are a real
        category here -- they just are not this one."""
        from jellyfin_mpv_shim.mpvtk_browser import config

        shown = {k for tab in config.FORM_TABS
                 for _title, keys in config.sections(tab) for k in keys}
        # The tray pair and the audio toggles are hidden per machine, and
        # nothing in RESTART_REQUIRED is one of those, so an unreachable
        # key here is a mistake rather than an environment.
        missing = sorted(k for k in config.RESTART_REQUIRED if k not in shown)
        self.assertEqual(missing, [],
                         "in RESTART_REQUIRED but not on any settings tab: "
                         "%s" % ", ".join(missing))

    def test_the_per_item_settings_are_not_in_it(self):
        """The distinction the whole set turns on. `hwdec`, `deband` and
        the rest of PRESET_SETTINGS are written on every item played, so a
        restart banner for one of them would be asking the user to restart
        for a change that reaches the next thing they play anyway."""
        from jellyfin_mpv_shim.mpv_options import PRESET_SETTINGS
        from jellyfin_mpv_shim.mpvtk_browser import config

        overlap = sorted(set(PRESET_SETTINGS) & config.RESTART_REQUIRED)
        self.assertEqual(overlap, [], "applies per item, not per launch: "
                                      "%s" % ", ".join(overlap))
        self.assertNotIn("hwdec", config.RESTART_REQUIRED)
        self.assertNotIn("deinterlace_auto", config.RESTART_REQUIRED)


class ArmingTest(unittest.TestCase):
    """The gateway's half: arm, then quit -- and disarm if the quit did not
    happen.

    Written against the REAL gateway rather than a stand-in controller,
    because the stand-in is what let this ship untested: every UI test
    called a fake `restart_app` that returned True, so nothing exercised the
    method that actually has to arm anything.
    """

    def setUp(self):
        from jellyfin_mpv_shim import restart

        restart.cancel()
        self.addCleanup(restart.cancel)

    def _gateway(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        return PlayerGateway()

    def _reconstructable(self):
        """Pin `command()` so these tests are about arming rather than about
        whatever argv the test runner happens to have.

        Not incidental: `supported()` really is environment-dependent -- it
        is answering "can this particular launch be rebuilt" -- and a test
        that inherited the runner's argv would pass or fail for reasons that
        have nothing to do with the code under it. (It failed exactly that
        way when first written, which is the argument for the patch rather
        than against the design.)
        """
        from jellyfin_mpv_shim import restart

        patcher = mock.patch.object(restart, "command",
                                    return_value=["/usr/bin/python3", "app"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_real_gateway_arms_and_quits(self):
        from jellyfin_mpv_shim import restart
        from jellyfin_mpv_shim.mpvtk_browser import ui

        self._reconstructable()
        quits = []
        with mock.patch.object(ui.user_interface, "quit_app",
                               side_effect=lambda: quits.append(1) or True):
            self.assertTrue(self._gateway().restart_app())
        self.assertEqual(len(quits), 1)
        self.assertTrue(restart.requested())

    def test_a_quit_that_does_nothing_disarms_the_restart(self):
        """`quit_app` returns False when no shutdown callback is wired, and
        it fails *quietly* -- so an except clause cannot see it. Left armed,
        the flag would relaunch the app on the user's next ordinary quit,
        minutes later, in a session with nothing to do with this button."""
        from jellyfin_mpv_shim import restart
        from jellyfin_mpv_shim.mpvtk_browser import ui

        self._reconstructable()
        with mock.patch.object(ui.user_interface, "quit_app",
                               return_value=False):
            self.assertFalse(self._gateway().restart_app())
        self.assertFalse(restart.requested())

    def test_a_quit_that_raises_disarms_the_restart(self):
        from jellyfin_mpv_shim import restart
        from jellyfin_mpv_shim.mpvtk_browser import ui

        self._reconstructable()
        with mock.patch.object(ui.user_interface, "quit_app",
                               side_effect=RuntimeError("no")):
            self.assertFalse(self._gateway().restart_app())
        self.assertFalse(restart.requested())

    def test_nothing_is_armed_when_the_launch_cannot_be_rebuilt(self):
        from jellyfin_mpv_shim import restart
        from jellyfin_mpv_shim.mpvtk_browser import ui

        with mock.patch.object(restart, "command", return_value=None), \
                mock.patch.object(ui.user_interface, "quit_app") as quit_app:
            self.assertFalse(self._gateway().restart_app())
        quit_app.assert_not_called()
        self.assertFalse(restart.requested())

    def test_arming_says_so_in_the_log(self):
        """The shutdown sequence is byte-for-byte identical whether or not a
        restart is coming, so without this line a log cannot distinguish
        "the restart did not fire" from "the restart was never armed" --
        which is the whole question when somebody reports it not working."""
        from jellyfin_mpv_shim import restart

        with self.assertLogs("restart", level="INFO") as caught:
            restart.request()
        self.assertTrue(any("Restart armed" in m for m in caught.output),
                        caught.output)


class QuitReportsWhetherItWorkedTest(unittest.TestCase):
    def test_quit_app_answers_false_with_nothing_wired(self):
        from jellyfin_mpv_shim.mpvtk_browser import ui

        with mock.patch.object(ui.user_interface, "stop_callback", None):
            self.assertFalse(ui.user_interface.quit_app())

    def test_quit_app_answers_true_once_it_has_fired(self):
        from jellyfin_mpv_shim.mpvtk_browser import ui

        fired = []
        with mock.patch.object(ui.user_interface, "stop_callback",
                               lambda: fired.append(1)):
            self.assertTrue(ui.user_interface.quit_app())
        self.assertEqual(len(fired), 1)

    def test_the_trays_quit_still_goes_through_the_same_door(self):
        """`_quit` is what the tray menu is wired to. It delegates now, so a
        future change to one path cannot leave the other behind."""
        from jellyfin_mpv_shim.mpvtk_browser import ui

        fired = []
        with mock.patch.object(ui.user_interface, "stop_callback",
                               lambda: fired.append(1)):
            ui.user_interface._quit()
        self.assertEqual(len(fired), 1)


class LogPreservationTest(unittest.TestCase):
    """The predecessor's log survives the restart.

    `configure_log_file` opens `log.txt` with mode="w" -- one run per file,
    which is right for an app that starts once. A restart is two runs
    telling one story, and the half worth reading ("Restart armed",
    "Restarting: ...", and whatever went wrong) is the half the successor
    would truncate. The case where anyone opens that file is exactly the
    case where the replacement did not come up.
    """

    def setUp(self):
        import shutil
        import tempfile

        restart.cancel()
        self.addCleanup(restart.cancel)
        self.dir = tempfile.mkdtemp(prefix="jms-logpres-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = os.path.join(self.dir, "log.txt")

    def _preserve(self, write_logs=True, create=True):
        from types import SimpleNamespace

        if create:
            with open(self.log, "w", encoding="utf-8") as fh:
                fh.write("Restarting: /usr/bin/python3 app\n")
        with mock.patch("jellyfin_mpv_shim.conffile.get",
                        return_value=self.log), \
                mock.patch("jellyfin_mpv_shim.conf.settings",
                           SimpleNamespace(write_logs=write_logs)):
            restart._preserve_log()

    def test_the_old_log_is_moved_aside(self):
        self._preserve()
        moved = os.path.join(self.dir, restart.PREVIOUS_LOG)
        self.assertTrue(os.path.exists(moved), os.listdir(self.dir))
        with open(moved, encoding="utf-8") as fh:
            self.assertIn("Restarting:", fh.read())
        # Moved, not copied: the successor opens `log.txt` with mode="w" and
        # a leftover would just be truncated again.
        self.assertFalse(os.path.exists(self.log))

    def test_nothing_happens_when_file_logging_is_off(self):
        """`write_logs` defaults off, so most installs have no file at all
        and this must not invent one."""
        self._preserve(write_logs=False)
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, restart.PREVIOUS_LOG)))

    def test_a_missing_log_is_not_an_error(self):
        self._preserve(create=False)      # must not raise

    def test_a_failure_does_not_stop_the_restart(self):
        """Best-effort: on Windows an open file cannot be renamed, and
        losing the previous log is not a reason to skip the relaunch."""
        with mock.patch("os.replace", side_effect=OSError("in use")):
            self._preserve()              # must not raise

    def test_the_log_is_preserved_before_the_child_starts(self):
        """Or the successor truncates it first and there is nothing to
        move."""
        restart.request()
        order = []
        with mock.patch.object(restart, "command", return_value=["x"]), \
                mock.patch.object(restart, "_preserve_log",
                                  side_effect=lambda: order.append("log")), \
                mock.patch("subprocess.Popen",
                           side_effect=lambda *a, **k: order.append("spawn")):
            restart.relaunch_if_requested()
        self.assertEqual(order, ["log", "spawn"])


class ShutdownOrderTest(unittest.TestCase):
    """Where the relaunch sits in ``mpv_shim.main``.

    Not an implementation detail: started any earlier, the new copy finds
    the single-instance lock still held, hands off to the process that is
    exiting, and quits -- so the restart looks exactly like a plain quit,
    and only on a machine where the timing works out.
    """

    def test_finish_never_returns(self):
        """The fact the ordering below rests on, measured rather than read.

        `exit_watchdog.finish` ends in `os._exit`, so **anything written
        after it is dead code**. The relaunch was originally placed there
        and never ran once: the restart armed, the app shut down cleanly,
        and nothing came back.

        Run in a subprocess because the thing being asserted is that the
        interpreter stops -- there is no in-process way to survive it.
        """
        import subprocess
        import textwrap

        script = textwrap.dedent("""
            import sys
            sys.argv = [sys.argv[0]]
            from jellyfin_mpv_shim import exit_watchdog
            exit_watchdog.arm()
            print("ARMED", flush=True)
            exit_watchdog.finish()
            print("REACHED", flush=True)
        """)
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=120,
                             cwd=os.path.dirname(os.path.dirname(
                                 os.path.abspath(__file__))))
        # A positive control first: "REACHED is absent" is also satisfied by
        # a script that died on an import error, which would make this test
        # green against a `finish` that had stopped existing.
        self.assertIn("ARMED", out.stdout,
                      "the script never got as far as finish():\n%s\n%s"
                      % (out.stdout, out.stderr))
        self.assertEqual(out.returncode, 0,
                         "finish(0) should end the process with status 0")
        self.assertNotIn("REACHED", out.stdout,
                         "exit_watchdog.finish() returned; the ordering rule "
                         "below is no longer load-bearing and its comment in "
                         "mpv_shim.main should be revisited")

    def test_nothing_in_main_comes_after_the_watchdog_finishes(self):
        """The general form of the bug, so it cannot come back for some
        other feature.

        Deliberately not "the relaunch is before finish()": the mistake was
        not specific to the relaunch, and a rule naming only it would let
        the next person append the next thing. This walks main's syntax
        tree rather than matching text, so a second `finish()` call or a
        statement tucked into another block cannot slip past.
        """
        import ast
        import inspect
        import textwrap

        from jellyfin_mpv_shim import mpv_shim

        tree = ast.parse(textwrap.dedent(inspect.getsource(mpv_shim.main)))
        finishes = [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "finish"
                    and getattr(n.func.value, "id", "") == "exit_watchdog"]
        self.assertEqual(len(finishes), 1, "expected exactly one finish()")
        end = finishes[0].lineno
        after = [n for n in ast.walk(tree)
                 if isinstance(n, ast.stmt) and n.lineno > end]
        self.assertEqual(
            [ast.dump(n)[:60] for n in after], [],
            "statements follow exit_watchdog.finish(), which calls os._exit "
            "and never returns -- they are dead code")

    def test_the_relaunch_is_registered_rather_than_written_after_the_loop(self):
        """It has to run on the forced exit too, and `main`'s shutdown loop
        is not reached when a step wedges. The relaunch therefore goes
        through `set_final_action`; a call written into the loop's tail
        would cover only the tidy exit."""
        import inspect

        from jellyfin_mpv_shim import mpv_shim

        source = inspect.getsource(mpv_shim.main)
        self.assertIn("set_final_action", source)
        self.assertLess(source.index("set_final_action"),
                        source.index("exit_watchdog.finish()"))

    def test_the_final_action_stops_the_tray_before_relaunching(self):
        """The tray is a separate process, stopped in the sixth of seven
        shutdown steps. On the forced path the wedge can be anywhere before
        that, so without this a restart puts a live replacement beside a
        tray icon whose app is gone -- and under `start_minimized` that dead
        icon is the only thing the user would try to click."""
        import inspect

        from jellyfin_mpv_shim import mpv_shim

        source = inspect.getsource(mpv_shim.main)
        body = source[source.index("def final_action"):]
        body = body[:body.index("exit_watchdog.set_final_action")]
        self.assertIn("stop_tray", body)
        self.assertLess(body.index("stop_tray"),
                        body.index("relaunch_if_requested"))

    def test_the_final_action_gives_the_instance_lock_up_first(self):
        """On the forced path the lock may never have been released -- the
        wedge can be anywhere -- and a new copy that finds it held hands off
        to this dying process and exits, so the restart would look like a
        plain quit."""
        import inspect

        from jellyfin_mpv_shim import mpv_shim

        source = inspect.getsource(mpv_shim.main)
        body = source[source.index("def final_action"):]
        body = body[:body.index("exit_watchdog.set_final_action")]
        self.assertLess(body.index("single.release"),
                        body.index("relaunch_if_requested"))


class PristineSnapshotOrderTest(unittest.TestCase):
    """The picture snapshot must be taken before anything writes to the new
    mpv, and the shader pack writes at *construction*.

    `menu.update_player` (and the first `OSDMenu`) builds a
    `VideoProfileManager`, whose `__init__` re-applies the remembered
    profile; `default-setting-groups` writes `deband` and, through
    `profile=gpu-hq`, every property `render_quality` owns. Snapshotted
    after that, "the user's own value" is the PACK's -- so turning Debanding
    off hands back `deband=yes, grain=0` over the user's mpv.conf,
    permanently, with no profile loaded and no grain shaders to justify the
    zero. `shader_pack_remember` defaults on, so that is the ordinary path
    for anyone who has picked a profile once.

    Structural rather than behavioural, and deliberately: reproducing it at
    runtime needs a real mpv, a real shader pack and a remembered profile.
    It was verified that way once by hand (with the old ordering the
    snapshot really did record `deband=True, grain=0.0,
    scale=ewa_lanczos`); this keeps the ordering from drifting back without
    paying for that setup on every run.
    """

    def _init_mpv_tree(self):
        import inspect
        import textwrap

        from jellyfin_mpv_shim import player

        return ast.parse(textwrap.dedent(inspect.getsource(player.PlayerManager._init_mpv)))

    def _first_line(self, tree, predicate):
        return min((n.lineno for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and predicate(n)), default=None)

    def test_the_snapshot_precedes_the_shader_pack(self):
        tree = self._init_mpv_tree()

        def named(node, name):
            func = node.func
            return (getattr(func, "id", None) == name
                    or getattr(func, "attr", None) == name)

        snapshot = self._first_line(
            tree, lambda n: named(n, "_snapshot_render_pristine"))
        self.assertIsNotNone(snapshot, "_init_mpv no longer snapshots")
        for builder in ("OSDMenu", "update_player"):
            line = self._first_line(tree, lambda n, b=builder: named(n, b))
            if line is None:
                continue
            self.assertLess(
                snapshot, line,
                "%s builds a VideoProfileManager, which re-applies the "
                "remembered shader profile and writes deband/scale into the "
                "new mpv. The snapshot must come first or it records the "
                "pack's values as the user's." % builder)

    def test_the_snapshot_is_paired_with_clearing_what_we_wrote(self):
        """Both halves move together: a snapshot without the reset would
        leave the previous mpv's keys marked as written against a fresh
        handle."""
        tree = self._init_mpv_tree()
        snapshot = self._first_line(
            tree, lambda n: getattr(n.func, "attr", None)
            == "_snapshot_render_pristine")
        writes = [n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Assign)
                  and any(getattr(t, "attr", None) == "_render_written"
                          for t in n.targets)]
        self.assertTrue(writes, "_render_written is no longer reset")
        self.assertLessEqual(min(writes), snapshot)


class FinalActionTest(unittest.TestCase):
    """`exit_watchdog.set_final_action` — the hook the restart hangs on."""

    def setUp(self):
        from jellyfin_mpv_shim import exit_watchdog

        self.wd = exit_watchdog
        self.addCleanup(setattr, exit_watchdog, "_final_action",
                        exit_watchdog._final_action)
        self.addCleanup(setattr, exit_watchdog, "_final_done",
                        exit_watchdog._final_done)
        exit_watchdog._final_action = None
        exit_watchdog._final_done = False

    def test_it_runs_once_however_many_paths_reach_it(self):
        """The orderly exit disarms the watchdog first, but the watchdog can
        already be past that check and mid-dump -- so the two really do
        race. Two spawns would leave two copies fighting for the instance
        lock."""
        calls = []
        self.wd.set_final_action(lambda: calls.append(1))
        self.wd._run_final_action()
        self.wd._run_final_action()
        self.assertEqual(len(calls), 1)

    def test_a_failing_action_does_not_stop_the_exit(self):
        """It is the last statement of a process that is already leaving;
        raising here would replace a clean exit with a traceback and change
        nothing else."""
        self.wd.set_final_action(mock.Mock(side_effect=RuntimeError("no")))
        self.wd._run_final_action()          # must not raise

    def test_no_action_registered_is_not_an_error(self):
        self.wd._run_final_action()

    def test_both_exit_paths_run_it(self):
        """The whole reason the hook exists. Read from the source of the two
        functions rather than by calling them, because both end in
        `os._exit` -- there is no way to observe them from in here."""
        import inspect

        for fn in (self.wd.finish, self.wd.arm):
            with self.subTest(path=fn.__name__):
                self.assertIn("_run_final_action()", inspect.getsource(fn))

    def test_it_runs_before_the_log_is_shut_down(self):
        """Or the relaunch's own log line is written into a dead logger and
        the restart is invisible again -- which is the bug this whole area
        is being fixed for."""
        import inspect

        for fn in (self.wd.finish, self.wd.arm):
            with self.subTest(path=fn.__name__):
                src = inspect.getsource(fn)
                self.assertLess(src.index("_run_final_action()"),
                                src.index("logging.shutdown()"))


if __name__ == "__main__":
    unittest.main()
