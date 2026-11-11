"""A visible message when the app cannot start, for people who launched it from a menu.

Opened from the application menu, Start menu or Applications folder there is no
terminal, and the Windows build is windowed (no stdout at all): an exception at
startup used to mean the window simply never appeared. :func:`report_fatal`
turns that into a short native dialog that says what failed, the one thing to
try, and where the log is. It is best effort and never raises, and it stays
silent when a terminal is attached, where the traceback is already in front of
the user.
"""

import logging
import os
import shutil
import subprocess
import sys

from .constants import APP_NAME, USER_APP_NAME

log = logging.getLogger("startup_error")

#: How long a dialog helper may stay open. A dismissed or ignored dialog must not
#: keep a dead app's process alive for ever.
DIALOG_TIMEOUT = 300


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _is_mpv_load_failure(exc):
    """Some exception in the chain is a failed load/spawn that names mpv.

    A missing libmpv arrives as ``OSError`` from the binding, and then as
    ``FileNotFoundError: 'mpv'`` from the external-mpv fallback it starts.
    """
    return any(isinstance(e, (OSError, ImportError)) and "mpv" in str(e).lower()
               for e in _chain(exc))


def hint_for(exc, platform=None):
    """One line of advice for the likeliest cause of ``exc``, or ''."""
    platform = platform or sys.platform
    if not _is_mpv_load_failure(exc):
        return ""
    if platform.startswith("linux"):
        return ("The mpv video library (libmpv) could not be loaded. "
                "Install it with your package manager (Debian/Ubuntu: "
                "sudo apt install libmpv2) and start the app again.")
    if platform.startswith("win"):
        return ("The bundled video library could not be loaded. Update "
                "your graphics driver (it provides Vulkan) and reinstall "
                "the app.")
    if platform == "darwin":
        return ("The bundled video player is missing or blocked. "
                "Reinstall the app from the original download.")
    return ""


def log_location():
    """Where the log is, or how to get one, in words."""
    try:
        from . import conffile
        from .conf import settings

        if getattr(settings, "write_logs", False):
            return conffile.get(APP_NAME, "log.txt")
    except Exception:
        pass
    return None


def message_for(exc, platform=None):
    """``(title, body)`` for a fatal startup error."""
    title = "%s could not start" % USER_APP_NAME
    parts = []
    hint = hint_for(exc, platform)
    if hint:
        parts.append(hint)
    first = (str(exc).strip().splitlines() or [""])[0][:300]
    parts.append("Details: %s%s" % (type(exc).__name__, ": " + first if first else ""))
    where = log_location()
    parts.append("Log file: %s" % where if where else
                 "Start the app from a terminal to see the full error.")
    return title, "\n\n".join(parts)


def _commands(title, body, platform):
    """The helper commands to try, in order, for this platform."""
    if platform == "darwin":
        script = 'display dialog %s with title %s buttons {"OK"} with icon stop' % (
            _applescript_quote(body), _applescript_quote(title))
        return [["osascript", "-e", script]]
    if platform.startswith("linux"):
        return [["zenity", "--error", "--no-markup", "--title", title, "--text", body],
                ["kdialog", "--error", body, "--title", title],
                ["notify-send", "--urgency=critical", title, body]]
    return []


def _applescript_quote(text):
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def show(title, body, platform=None):
    """Show a native dialog. Returns whether one was shown. Never raises."""
    platform = platform or sys.platform
    try:
        if platform.startswith("win"):
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, body, title, 0x10)   # MB_ICONERROR
            return True
        for cmd in _commands(title, body, platform):
            if shutil.which(cmd[0]) is None:
                continue
            subprocess.run(cmd, timeout=DIALOG_TIMEOUT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        log.debug("could not show the startup error dialog", exc_info=True)
    return False


def _interactive():
    try:
        return bool(sys.stderr and sys.stderr.isatty())
    except Exception:
        return False


def report_fatal(exc, platform=None):
    """Log ``exc`` and, if nobody is watching a terminal, show a dialog."""
    try:
        title, body = message_for(exc, platform)
        log.error("%s: %s", title, body.replace("\n\n", " | "))
        if _interactive() or os.environ.get("JELLYHOME_NO_ERROR_DIALOG"):
            return False
        return show(title, body, platform)
    except Exception:
        return False
