"""Hand a local file to whatever the desktop opens it with.

Written for books, which is the one kind of library item this app cannot
render: the server serves no page, no archive entry and no spine document
(see ``books.py``), so the only honest thing to do with a downloaded epub
is give it to the reader the user already has.

No dependency, for the reason CONTRIBUTING.md gives: every backend here is
either in the standard library or a command that ships with the desktop
being used. Modelled on ``clipboard.py``, including its rule that nothing
raises — callers get ``(ok, method)`` and decide what to say.

**The process is launched detached and never waited on.** A reader is a
long-lived GUI application; ``subprocess.run`` would block the caller for
as long as the user reads the book, and on Linux the caller would be the
browser's worker pool. ``xdg-open`` itself exits immediately, but the
handlers underneath it do not always, and one that inherits our pipes and
never exits is indistinguishable from a hang.
"""

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("system_open")

#: Openers to try on Linux/BSD, most portable first. ``xdg-open`` is the
#: freedesktop standard and is what a Flatpak's portal intercepts; the rest
#: are the desktop-specific ones it delegates to anyway, kept as a fallback
#: for a session where xdg-utils is not installed.
_LINUX = ["xdg-open", "gio", "kde-open", "gnome-open"]


def _argv(command, path):
    # gio is the only one of these that is not "<command> <file>".
    if command == "gio":
        return ["gio", "open", path]
    return [command, path]


def _spawn(argv):
    """Start ``argv`` detached. True if it started at all.

    Started, not succeeded: a launcher that forks and exits zero tells us
    nothing about whether the handler it picked could read the file. That
    distinction is the caller's to communicate — "opened it" here means
    "handed it over", which is as much as any of these can promise.
    """
    try:
        kwargs = {"stdin": subprocess.DEVNULL,
                  "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            # Its own session, so the reader does not die with us and a
            # terminal-based handler cannot steal our stdin.
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
        return True
    except Exception:
        log.debug("could not launch %s", argv[0], exc_info=True)
        return False


def open_path(path):
    """Open ``path`` with the system's handler. Returns ``(ok, method)``.

    ``method`` names what was used, for a status line that can say *how* it
    opened — the difference between "your reader is starting" and "nothing
    on this box knows what to do with an epub" is the only thing the user
    can act on.
    """
    if not path:
        return False, None
    if not os.path.exists(path):
        # Worth its own answer rather than letting the opener fail: a
        # missing file means the download went away, not that the desktop
        # has no reader.
        log.warning("cannot open %s: no such file", path)
        return False, None
    if os.name == "nt":
        try:
            os.startfile(path)  # type: ignore[attr-defined]  # Windows only
            return True, "startfile"
        except Exception:
            log.warning("startfile failed for %s", path, exc_info=True)
            return False, None
    if sys.platform == "darwin":
        return (True, "open") if _spawn(["open", path]) else (False, None)
    for command in _LINUX:
        if shutil.which(command) is None:
            continue
        if _spawn(_argv(command, path)):
            return True, command
    log.warning("no desktop opener found for %s", path)
    return False, None
