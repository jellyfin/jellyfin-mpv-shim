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
        with open(gen_file) as fh:
            generation = int(fh.read().strip() or "0")
    except Exception:
        generation = 0
    with open(gen_file, "w") as fh:
        fh.write(str(generation + 1))

    if generation:
        # We are the relaunched copy. Record what we were started with --
        # the argv is the half most likely to be wrong, and a marker that
        # only said "something started" would pass for a copy launched
        # against the default config directory.
        with open(_state_path(config_dir, "relaunched"), "w") as fh:
            fh.write("\n".join(sys.argv))

    def drive():
        # Long enough for the app to be genuinely up -- servers resolved,
        # mpv created -- so the shutdown under test is a real one rather
        # than a half-built app tearing itself down.
        time.sleep(float(os.environ.get("JMS_RESTART_DELAY", "6")))
        if not generation:
            from jellyfin_mpv_shim import restart

            restart.request()
        # SIGTERM rather than the UI's quit: it reaches `main`'s halt event
        # through the handler the app installs itself, so the shutdown is
        # the same orderly one a `stop` command or a session logout gets,
        # with nothing test-shaped in the middle of it.
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=drive, daemon=True).start()

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
