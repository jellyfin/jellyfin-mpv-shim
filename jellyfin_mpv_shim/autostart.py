"""\"Start with computer\": an explicit, per-user, reversible opt-in.

Off by default. Turning it on writes one per-user entry and nothing system
wide; turning it off (or uninstalling, on Windows) removes exactly that entry.

* Windows: a value named after the app under
  ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``. No administrator
  rights. The installer's uninstaller deletes the same value.
* Linux (non-Flatpak): ``$XDG_CONFIG_HOME/autostart/<DESKTOP_ID>.desktop``.
* Flatpak: unsupported here. A sandboxed app cannot write the host's autostart
  directory; the supported route is the desktop's Background portal, which this
  app does not call. Use the desktop's own "Startup applications" setting.
* macOS: unsupported (login items need a signed app bundle).

The entry launches with ``--minimized`` so the library window does not appear
at login; the app stays reachable from the tray, and as a cast target.
"""

import logging
import os
import shlex
import sys

from .constants import DESKTOP_ID, USER_APP_NAME

log = logging.getLogger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_args():
    """argv that starts this install, minimized.

    ``JELLYHOME_LAUNCHER`` comes first: a distribution package (the Linux
    .deb) keeps the application and its libraries in a private directory that
    only its launcher script puts on ``sys.path``, so ``python -m
    jellyfin_mpv_shim.mpv_shim`` would be an entry that never starts anything.
    The launcher exports its own path for exactly this.
    """
    launcher = os.environ.get("JELLYHOME_LAUNCHER")
    if launcher and os.path.isfile(launcher) and os.access(launcher, os.X_OK):
        return [launcher, "--minimized"]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    return [sys.executable, "-m", "jellyfin_mpv_shim.mpv_shim", "--minimized"]


def _desktop_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", DESKTOP_ID + ".desktop")


def _desktop_text():
    exec_line = " ".join(shlex.quote(a) for a in _launch_args())
    # The Desktop Entry spec wants `%` doubled and the Exec value quoted.
    exec_line = exec_line.replace("%", "%%")
    return ("[Desktop Entry]\nType=Application\nName=%s\nExec=%s\n"
            "X-GNOME-Autostart-enabled=true\nNoDisplay=false\n"
            % (USER_APP_NAME, exec_line))


def supported():
    """``(True, "")`` or ``(False, reason)`` for this platform."""
    from . import hostinfo

    if sys.platform.startswith("win32"):
        return True, ""
    if sys.platform.startswith("linux"):
        if hostinfo.is_flatpak():
            return False, ("Not available inside Flatpak. Add the app in "
                           "your desktop's Startup Applications settings.")
        return True, ""
    return False, "Not supported on this platform."


def is_enabled():
    """Whether our entry currently exists."""
    if sys.platform.startswith("win32"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, USER_APP_NAME)
            return True
        except OSError:
            return False
    return os.path.exists(_desktop_path())


def set_enabled(enabled):
    """Create or remove the entry. Returns ``(ok, message)``; never raises."""
    ok, why = supported()
    if not ok:
        # Disabling is always allowed to try to clean up a stale entry.
        if enabled:
            return False, why
    try:
        if sys.platform.startswith("win32"):
            return _set_windows(enabled)
        return _set_xdg(enabled)
    except Exception as exc:
        log.warning("Could not %s autostart.",
                    "enable" if enabled else "disable", exc_info=True)
        return False, str(exc)


def _set_windows(enabled):
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            cmd = " ".join('"%s"' % a if " " in a else a
                           for a in _launch_args())
            winreg.SetValueEx(key, USER_APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, USER_APP_NAME)
            except FileNotFoundError:
                pass
    return True, ""


def _set_xdg(enabled):
    path = _desktop_path()
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(_desktop_text())
        os.replace(tmp, path)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return True, ""


def reconcile(wanted):
    """Make the entry match the setting. Called at startup so a stale entry
    (setting turned off by hand-editing conf.json) is removed, and a moved
    install re-registers its new path. Only touches state when it differs."""
    if not wanted:
        if is_enabled():
            set_enabled(False)
        return
    ok, _why = supported()
    if ok:
        set_enabled(True)
