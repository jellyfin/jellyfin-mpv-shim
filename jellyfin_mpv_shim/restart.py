"""Restarting the app in place, for settings that only apply at startup.

**The relaunch happens as the process's last act**, registered with
``exit_watchdog.set_final_action`` so it runs on *both* ways out -- the
orderly ``finish()`` and the deadline that force-kills a wedged shutdown --
rather than as an ``exec`` from wherever the button was pressed.

It is deliberately **not** a line at the end of ``main``: that placement was
dead code below ``os._exit``, and it would also miss the wedged exit, which
is exactly when a user who pressed *Restart Now* most needs the app to come
back. See docs/architecture.md section 4. What that buys:

- the shutdown sequence still runs, so the final progress report is posted,
  the window geometry is saved and credentials are flushed;
- the lock is already free when the new process starts, so it does not find
  the old copy still holding it and hand off to a process that is exiting;
- the tray child, the mpv window and the worker threads are all gone, so
  nothing is inherited or orphaned;
- there is no ``execv`` from a multithreaded process, and no platform where
  that behaves differently.

The cost is that the app really does go away and come back, which is what
the user asked for.

``command()`` rebuilds the launch from **parsed** arguments rather than
copying ``sys.argv``, and the allowlist there is deliberate: a flag nobody
has taught this function about is dropped rather than repeated. Dropping is
the safe direction -- ``--reset-shaders`` would re-run a recovery action,
``add``/``clear`` are one-shot commands, and ``--password`` would put the
user's password back on the process list of a launch they did not type.
"""

import logging
import os
import subprocess
import sys

log = logging.getLogger("restart")

#: Set by :func:`request`, read once by :func:`relaunch_if_requested` on the
#: way out. A plain module global rather than state on any object because
#: the thing that asks (the settings screen) and the thing that acts (the
#: watchdog's final-action hook) have no reference to each other and should
#: not acquire one for this.
_requested = False

#: Settings the user changed that this restart is FOR. Read by
#: :func:`_durable_flags`, which drops any command-line override that would
#: land on top of one of them. Empty for a restart nobody attributed to a
#: setting, which is the safe default: nothing is dropped.
_pending_settings = frozenset()


def command():
    """The argv that would start this app again, or None if unknown.

    ``--config`` above all must survive: without it a restart from a
    non-default configuration directory would come back against the default
    one, which is a different app with different servers.
    """
    exe = sys.executable
    if not exe:
        return None
    if getattr(sys, "frozen", False):
        # sys.argv[0] IS the executable here, so naming it twice would pass
        # the exe to itself as a positional -- which argparse reads as an
        # unknown command.
        base = [exe]
    else:
        script = sys.argv[0] if sys.argv else ""
        if not script or not os.path.exists(script):
            # `python -m something`, an embedded interpreter, a deleted
            # entry point. Rather than guess at a module name that may not
            # be importable as __main__, say we cannot do it: the caller
            # falls back to telling the user to restart by hand, which is
            # honest and was the only option before this existed.
            return None
        base = [exe, script]
    return base + _durable_flags()


def _durable_flags():
    """The command-line options that describe *how this copy is running*.

    Everything else is left out by construction -- see the module docstring
    for why the allowlist is the safe direction.
    """
    from .args import get_args

    try:
        args = get_args()
    except Exception:
        # Argument parsing not available in this embedding. No flags is a
        # worse restart than the right flags, but it is still a restart,
        # and the alternative is refusing to do one at all.
        log.debug("could not read the arguments for a restart", exc_info=True)
        return []
    out = []
    if getattr(args, "config", None):
        out += ["--config", args.config]
    # The overrides that SHADOW a setting are dropped when that setting is
    # what the restart is for. `main` applies these after loading the config
    # ("so they win"), so re-passing one would overwrite the value the user
    # just saved -- and three of the four name a key in RESTART_REQUIRED.
    # Launch with `--scale 2.0`, set Interface Scale to 100%, press Restart
    # Now: without this the app comes back at 200%, the banner is gone
    # because it is session state, nothing is logged, and pressing Restart
    # again never helps.
    #
    # Only for the settings being restarted for, not always: somebody who
    # passed `--scale 2.0` and restarts for an unrelated reason still means
    # it for this sitting.
    pending = _pending_settings
    enable_gui = getattr(args, "enable_gui", None)
    if enable_gui is not None and "enable_gui" not in pending:
        out.append("--gui" if enable_gui else "--no-gui")
    minimized = getattr(args, "start_minimized", None)
    if minimized is not None and "start_minimized" not in pending:
        out.append("--minimized" if minimized else "--no-minimized")
    if getattr(args, "mpv_loglevel", None) and "mpv_log_level" not in pending:
        out += ["--mpv-loglevel", args.mpv_loglevel]
    if (getattr(args, "ui_scale", None) is not None
            and "ui_scale" not in pending):
        out += ["--scale", str(args.ui_scale)]
    if getattr(args, "disable_hwdec", False):
        # Kept unconditionally, unlike the four above, and the difference is
        # what dropping it costs: this is the recovery flag for hardware
        # decoding stopping the window from opening at all, so a restart
        # that quietly turned it back on could leave the user with no window
        # to turn anything off from. It shadows no setting -- `hwdec` is
        # applied per item, so it is never a reason to restart.
        out.append("--disable-hwdec")
    if getattr(args, "debug", False):
        out.append("--debug")
    return out


#: Environment PyInstaller's onefile bootloader sets for the *second stage*
#: of its own launch, which a relaunch must not inherit.
#:
#: onefile is two processes: the bootloader extracts the archive to a temp
#: directory, marks the environment, and runs itself again -- the marked run
#: being the one that starts Python. A child that inherits those marks is
#: read by the new bootloader as its own second stage, so it skips extraction
#: and uses **the dying parent's temp directory**, which the parent removes
#: on the way out. The restarted app then has no files.
#:
#: `_MEIPASS2` is the PyInstaller 5-and-earlier spelling; 6.x uses the
#: `_PYI_` family (`_PYI_ARCHIVE_FILE`, `_PYI_APPLICATION_HOME_DIR`,
#: `_PYI_PARENT_PROCESS_LEVEL`). The prefix is matched rather than the names
#: listed, so a future addition to that family is covered by default --
#: which is the right direction, since the failure is a build that will not
#: start and the variables are private to the bootloader anyway.
_PYI_MARKERS = ("_MEIPASS2",)
_PYI_PREFIX = "_PYI_"

#: Search paths the bootloader points at its temp directory, having saved
#: whatever was there under ``<NAME>_ORIG``. Restoring them is what
#: PyInstaller's own documentation tells you to do before spawning anything,
#: and here the temp directory is additionally about to be deleted.
_PYI_PATH_VARS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
                  "DYLD_FRAMEWORK_PATH", "LIBPATH")


def child_env():
    """The environment to start the new copy with.

    A plain copy of ours, **except** under a frozen build, where the
    PyInstaller bootloader has left marks in the environment that describe
    *this* launch and would misdirect the next one. See the two tables above.

    Gated on ``sys.frozen`` rather than applied everywhere, and that gate is
    load-bearing: run from source, `LD_LIBRARY_PATH` is the user's own and
    there is no `_ORIG` to restore it from, so the same code would silently
    delete it.
    """
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        return env
    for name in list(env):
        if name in _PYI_MARKERS or name.startswith(_PYI_PREFIX):
            env.pop(name, None)
    for name in _PYI_PATH_VARS:
        original = env.pop(name + "_ORIG", None)
        if original is not None:
            env[name] = original
        else:
            # No _ORIG means the bootloader created the variable rather than
            # overwriting one, so the correct restoration is to remove it.
            env.pop(name, None)
    return env


def supported():
    """Whether the restart button can be offered at all.

    Asked **before** anything is shut down, so a machine this cannot work on
    gets a banner that says "restart to apply" rather than a button that
    quietly does nothing after taking the app away.

    Deliberately NOT memoized here. It reads `sys.argv[0]` and the
    filesystem, both of which a test legitimately patches, and a module-level
    memo made the answer stick across tests -- caching a value computed under
    one patch and handing it to the next. The per-frame cost this avoids is
    cached by the caller instead, where the scope is one browser session
    (`MpvtkBrowser.can_restart`).
    """
    return command() is not None


def request(pending=()):
    """Ask for a relaunch after the shutdown that is about to happen.

    Does not itself stop anything: the caller triggers the ordinary exit,
    which is the only way the shutdown sequence runs in full.

    ``pending`` is the settings this restart is for. A command-line override
    naming one of them is dropped from the relaunch, because ``main``
    applies those on top of the saved config and would undo the change the
    user is restarting to get. See :func:`_durable_flags`.
    """
    global _requested, _pending_settings
    _requested = True
    _pending_settings = frozenset(pending or ())
    # Logged, and at INFO. Without it a restart that does not happen is
    # indistinguishable in the log from an ordinary quit -- the shutdown
    # sequence is identical and the only difference is one boolean nobody
    # can see. That ambiguity cost a debugging session.
    log.info("Restart armed; it will happen after the shutdown completes.")


def cancel():
    global _requested, _pending_settings
    if _requested:
        log.info("Restart disarmed.")
    _requested = False
    _pending_settings = frozenset()


def requested():
    return _requested


#: What the predecessor's log is renamed to before the replacement starts.
#: Beside `log.txt` so a bug report picks up both.
PREVIOUS_LOG = "log.prev.txt"


def _preserve_log():
    """Move this run's log aside so the replacement does not truncate it.

    ``configure_log_file`` opens ``log.txt`` with ``mode="w"``: one run per
    file, which is right for an app that starts once. A restart is two runs
    telling one story, and the half that matters -- "Restart armed",
    "Restarting: ..." and whatever went wrong before it -- is the half the
    successor would overwrite. The case where anyone reads it is precisely
    the case where the replacement failed to come up, so the evidence would
    be gone exactly when it is wanted.

    Renaming rather than copying: the parent's handler keeps writing to the
    same inode, so the rest of its shutdown lands in the preserved file
    where it belongs. Best-effort -- on Windows an open file cannot be
    renamed, and losing the previous log is not a reason to skip the
    restart.
    """
    try:
        from . import conffile
        from .conf import settings
        from .constants import APP_NAME

        if not getattr(settings, "write_logs", False):
            return                    # nothing is writing a file to save
        path = conffile.get(APP_NAME, "log.txt")
        if os.path.exists(path):
            os.replace(path, os.path.join(os.path.dirname(path),
                                          PREVIOUS_LOG))
    except Exception:
        log.debug("could not preserve the log across the restart",
                  exc_info=True)


def relaunch_if_requested():
    """Start a fresh copy, if one was asked for. Returns True if spawned.

    Called from ``exit_watchdog``'s final-action hook, immediately before
    ``os._exit`` on whichever exit path got there. Failure is logged and
    swallowed: by this point the app has already shut down, so raising would
    turn "the restart did not happen" into a traceback on the way out and
    change nothing else.
    """
    global _requested
    if not _requested:
        log.debug("No restart was requested; exiting normally.")
        return False
    # Cleared on the way in, not on the way out: the watchdog's forced exit
    # and the orderly one can both reach here, and two spawns would leave
    # two copies racing for the instance lock. `exit_watchdog` guards this
    # as well; belt and braces, because the cost of being wrong is a second
    # app the user did not ask for.
    _requested = False
    cmd = command()
    if cmd is None:
        log.error("Cannot restart: this launch cannot be reconstructed. "
                  "Start %s again by hand.", os.path.basename(sys.argv[0] or ""))
        return False
    kwargs = {"close_fds": True, "env": child_env()}
    if os.name == "nt":
        # Its own process group, so a Ctrl-C in the console this copy was
        # started from does not reach the new one.
        #
        # **Not DETACHED_PROCESS**, which was here first and is wrong for a
        # console build: it gives the child no console, leaving the stdout
        # and stderr handles it inherited pointing at nothing. It is also
        # unnecessary -- Windows does not take children down with their
        # parent the way a POSIX session does -- and the goal is for the
        # restarted copy to behave exactly like the launch it replaces,
        # console included.
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # A new session, so a Ctrl-C in the terminal that started the old
        # copy does not reach the new one, and so the child is not killed
        # with the old process group.
        kwargs["start_new_session"] = True
    log.info("Restarting: %s", " ".join(cmd))
    # After the line above, so the preserved file contains it.
    _preserve_log()
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception:
        log.exception("Could not restart; start %s again by hand.",
                      os.path.basename(sys.argv[0] or ""))
        return False
