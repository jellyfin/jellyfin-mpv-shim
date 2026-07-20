"""Put text on the system clipboard, without adding a dependency.

There is no clipboard library here on purpose (see CONTRIBUTING.md on
dependencies). Everything below is either already in the process — mpv — or
a command that ships with the desktop the user is already running.

Nothing here raises: every backend is probed and the first one that *verifiably*
worked wins. Callers get ``(ok, method)`` and decide what to say.
"""

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("clipboard")

# (command, needs-stdin) per platform, most specific first. Wayland before X11
# because a Wayland session usually also answers to xclip via XWayland, and
# copying there lands in the wrong clipboard.
_LINUX = [
    (["wl-copy"], True),
    (["xclip", "-selection", "clipboard"], True),
    (["xsel", "--clipboard", "--input"], True),
]
_DARWIN = [(["pbcopy"], True)]
_WINDOWS = [(["clip"], True)]


def _commands():
    if sys.platform == "darwin":
        return _DARWIN
    if os.name == "nt":
        return _WINDOWS
    return _LINUX


def _via_mpv(text, player):
    """mpv 0.40+ exposes a writable ``clipboard/text``. Older builds have it
    read-only, or not at all, and a failed write does not always raise — so
    read it back rather than trusting the set."""
    if player is None:
        return False
    try:
        player.command("set", "clipboard/text", text)
    except Exception:
        try:
            setattr(player, "clipboard_text", text)
        except Exception:
            return False
    try:
        return player.clipboard_text == text
    except Exception:
        return False


def _via_command(text):
    for argv, _stdin in _commands():
        if shutil.which(argv[0]) is None:
            continue
        try:
            proc = subprocess.run(argv, input=text.encode("utf-8"),
                                  timeout=10, capture_output=True)
        except Exception:
            log.debug("clipboard command %s failed", argv[0], exc_info=True)
            continue
        if proc.returncode == 0:
            return argv[0]
        log.debug("clipboard command %s exited %d", argv[0], proc.returncode)
    return None


def copy_text(text, player=None):
    """Copy ``text``. Returns ``(ok, method)``.

    ``player`` is an optional mpv handle to try first — it is in-process and
    needs no external binary, which matters on a bare system where none of the
    CLI tools are installed.
    """
    if not text:
        return False, None
    if _via_mpv(text, player):
        return True, "mpv"
    name = _via_command(text)
    if name:
        return True, name
    return False, None


def copy_or_save(text, fallback_path, player=None):
    """Copy ``text``, or write it to ``fallback_path`` if nothing can.

    Returns ``(ok, method, path)``. ``path`` is set only when the text was
    written to a file instead — a headless box with no clipboard at all should
    still give the user something they can send on, rather than a dead button.
    """
    ok, method = copy_text(text, player=player)
    if ok:
        return True, method, None
    try:
        with open(fallback_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True, "file", fallback_path
    except Exception:
        log.warning("could not write %s", fallback_path, exc_info=True)
        return False, None, None
