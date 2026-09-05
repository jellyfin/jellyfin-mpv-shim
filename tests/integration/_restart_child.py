"""A real app process that asks to be restarted, for test_restart_relaunch.

Run as a script, never imported. It *is* the launch that gets relaunched, so
it has to be re-runnable as itself -- which is the whole point: the relaunch
rebuilds the command from `sys.argv`, and this file is `sys.argv[0]`.

**The generation file is what stops it restarting for ever.** The first run
arms a restart and exits; the copy that comes back sees generation 1, records
that it exists, and exits without arming. Without it the test would spawn an
unbounded chain of real apps.

Runs the ordinary GUI path, **not** `--no-gui`: with no credentials saved,
CLI mode prompts for a server URL on stdin and dies of EOF, so it cannot be
driven unattended at all. The browser simply shows its login screen and waits,
which is what this needs.

The restart button's own path (arm, then quit through the UI) is covered by
unit tests against the real gateway. What needs a real process is the *other*
end -- that `main`'s exit actually spawns the replacement, after releasing the
instance lock, from a process that then dies. That is the half that shipped
broken, because it sat below a call that never returns.
"""

import os
import signal
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def _state_path(config_dir, name):
    return os.path.join(config_dir, name)


def main():
    config_dir = sys.argv[sys.argv.index("--config") + 1]
    os.makedirs(config_dir, exist_ok=True)
    gen_file = _state_path(config_dir, "generation")
    try:
        with open(gen_file, encoding="utf-8") as fh:
            generation = int(fh.read().strip() or "0")
    except Exception:
        generation = 0
    with open(gen_file, "w", encoding="utf-8") as fh:
        fh.write(str(generation + 1))

    if generation:
        # We are the relaunched copy. Record what we were started with --
        # the argv is the half most likely to be wrong, and a marker that
        # only said "something started" would pass for a copy launched
        # against the default config directory.
        with open(_state_path(config_dir, "relaunched"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(sys.argv))

    wedge = os.environ.get("JMS_RESTART_WEDGE")
    if wedge and not generation:
        # Make the orderly shutdown never finish, so the exit watchdog's
        # deadline is what ends this process. That is the path
        # `exit_watchdog.set_final_action` exists for, and the one a
        # relaunch written into `main`'s tail would silently miss -- which
        # is the same shape as the bug this whole file is downstream of.
        from jellyfin_mpv_shim import exit_watchdog
        from jellyfin_mpv_shim import timeline

        exit_watchdog.SHUTDOWN_DEADLINE = float(wedge)

        def never_returns():
            threading.Event().wait()

        # The second step of the shutdown loop, so the wedge lands after the
        # player is down and well before the instance lock is released --
        # which is exactly the state the final action has to cope with.
        timeline.timelineManager.stop = never_returns

    def drive():
        # Long enough for the app to be genuinely up -- servers resolved,
        # mpv created -- so the shutdown under test is a real one rather
        # than a half-built app tearing itself down.
        time.sleep(float(os.environ.get("JMS_RESTART_DELAY", "6")))
        if not generation:
            from jellyfin_mpv_shim import restart

            restart.request()
        # Ask the app to stop the way something outside it would, so the
        # shutdown under test is the orderly one with nothing test-shaped in
        # the middle of it. Both routes below reach the SAME halt event --
        # mpv_shim.main wires `single.on_stop` and its SIGTERM handler to
        # `halt.set` alike -- so this is one property measured through
        # whichever door the platform actually has.
        if os.name == "nt":
            # os.kill(pid, SIGTERM) is not a signal on Windows: any sig but
            # CTRL_C/CTRL_BREAK_EVENT is TerminateProcess, nothing can
            # handle it, and the exit code becomes the signal number. So the
            # app died where it stood -- no shutdown, no relaunch, and a
            # wedge test that saw rc 15 instead of the forced 1. The `stop`
            # subcommand exists precisely because stopping is a message and
            # not a signal (single_instance's module docstring).
            subprocess.run([sys.executable, os.path.join(REPO, "run.py"),
                            "--config", config_dir, "stop"],
                           timeout=60, capture_output=True)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=drive, daemon=True).start()

    # A positive control for the test's "MAIN RETURNED is absent" assertion:
    # without it, a child that died on an import error would satisfy that
    # assertion just as well as one that exited correctly.
    print("PARENT READY", flush=True)

    from jellyfin_mpv_shim.mpv_shim import main as app_main

    app_main()
    # Unreachable: main ends in exit_watchdog.finish(), which calls os._exit.
    # If this ever prints, the test says so rather than hanging.
    print("MAIN RETURNED", flush=True)


if __name__ == "__main__":
    # The tray child is started with the 'spawn' method, which re-executes
    # this file in a fresh interpreter. Without the guard it would re-run the
    # whole app -- a second copy that fights for the instance lock and
    # scribbles on the generation file. (This is not hypothetical: the
    # hand-written version of this experiment did exactly that.)
    import multiprocessing

    multiprocessing.freeze_support()
    main()
