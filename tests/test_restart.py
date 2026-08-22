"""Relaunching the app, and what the relaunched copy is started with.

The dangerous half of a restart is not the spawn, it is the argv. A launch
rebuilt wrongly comes back as a *different app*: against the default config
directory instead of the one the user chose, with a recovery flag re-applied,
or -- worst -- with the password from a one-off ``--server``/``--password``
login back on the process list of a launch nobody typed.

So `command()` is built from an allowlist of parsed arguments, and these
tests are mostly about what does **not** survive.
"""

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


class ShutdownOrderTest(unittest.TestCase):
    """Where the relaunch sits in ``mpv_shim.main``.

    Not an implementation detail: started any earlier, the new copy finds
    the single-instance lock still held, hands off to the process that is
    exiting, and quits -- so the restart looks exactly like a plain quit,
    and only on a machine where the timing works out.
    """

    def test_the_relaunch_is_after_the_lock_is_released(self):
        import inspect

        from jellyfin_mpv_shim import mpv_shim

        source = inspect.getsource(mpv_shim.main)
        release = source.index("single.release")
        relaunch = source.index("relaunch_if_requested")
        self.assertLess(release, relaunch,
                        "the relaunch must come after the instance lock is "
                        "released, or the new copy hands off to this one")

    def test_the_relaunch_is_after_the_watchdog_finishes(self):
        """So a new process is never started by a run the exit watchdog is
        about to kill."""
        import inspect

        from jellyfin_mpv_shim import mpv_shim

        source = inspect.getsource(mpv_shim.main)
        self.assertLess(source.index("exit_watchdog.finish()"),
                        source.index("relaunch_if_requested"))


if __name__ == "__main__":
    unittest.main()
