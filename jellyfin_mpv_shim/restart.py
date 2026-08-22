"""Restarting the app in place, for settings that only apply at startup.

**The relaunch happens at the very end of the ordinary exit**, from
``mpv_shim.main``'s ``finally`` after the single-instance lock has been
released -- not as an ``exec`` from wherever the button was pressed. That is
the whole design, and it is what makes one implementation work everywhere:

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
#: the thing that asks (the settings screen) and the thing that acts
#: (``main``'s finally) have no reference to each other and should not
#: acquire one for this.
_requested = False


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
    enable_gui = getattr(args, "enable_gui", None)
    if enable_gui is not None:
        out.append("--gui" if enable_gui else "--no-gui")
    minimized = getattr(args, "start_minimized", None)
    if minimized is not None:
        out.append("--minimized" if minimized else "--no-minimized")
    if getattr(args, "mpv_loglevel", None):
        out += ["--mpv-loglevel", args.mpv_loglevel]
    if getattr(args, "ui_scale", None) is not None:
        out += ["--scale", str(args.ui_scale)]
    if getattr(args, "disable_hwdec", False):
        # Kept on purpose. It is a recovery flag, and a restart is exactly
        # when somebody who needed it still needs it -- coming back with
        # hardware decoding on would undo the thing that got the window
        # open.
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
    """
    return command() is not None


def request():
    """Ask for a relaunch after the shutdown that is about to happen.

    Does not itself stop anything: the caller triggers the ordinary exit,
    which is the only way the shutdown sequence runs in full.
    """
    global _requested
    _requested = True
    # Logged, and at INFO. Without it a restart that does not happen is
    # indistinguishable in the log from an ordinary quit -- the shutdown
    # sequence is identical and the only difference is one boolean nobody
    # can see. That ambiguity cost a debugging session.
    log.info("Restart armed; it will happen after the shutdown completes.")


def cancel():
    global _requested
    if _requested:
        log.info("Restart disarmed.")
    _requested = False


def requested():
    return _requested


def relaunch_if_requested():
    """Start a fresh copy, if one was asked for. Returns True if spawned.

    Called from ``main``'s ``finally``, last. Failure is logged and
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
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception:
        log.exception("Could not restart; start %s again by hand.",
                      os.path.basename(sys.argv[0] or ""))
        return False
