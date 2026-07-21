# noinspection PyUnresolvedReferences,PyPackageRequirements
import win32gui
import logging

from .constants import USER_APP_NAME

log = logging.getLogger("win_utils")

#: mpv registers its top-level window under this class on Windows. Matching
#: on it rather than on the title is what makes this survive title changes:
#: this used to look for " - mpv", which stopped matching the moment the
#: window was renamed to "<media> - Jellyfin MPV Shim" and silently took
#: raise-on-play with it.
MPV_WINDOW_CLASS = "mpv"

#: The mirror window also carries the app name, so a title-based match has
#: to rule it out or raise_mpv can raise the wrong window.
MIRROR_WINDOW_NAME = "Jellyfin MPV Shim Mirror"


def window_enumeration_handler(hwnd, top_windows):
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        cls = ""
    top_windows.append((hwnd, win32gui.GetWindowText(hwnd), cls))


def is_mpv_window(entry):
    """Does this enumerated window belong to our mpv?

    Class first, because it does not change when the title does. The title
    fallback covers builds whose class differs, and deliberately accepts
    both the current title and mpv's stock " - mpv" so an external-mpv
    setup that never got our --title still matches.
    """
    _hwnd, title, cls = entry
    if cls == MPV_WINDOW_CLASS:
        return True
    low = (title or "").lower()
    if MIRROR_WINDOW_NAME.lower() in low:
        return False
    return low.endswith(USER_APP_NAME.lower()) or " - mpv" in low


def raise_mpv():
    # This workaround is madness. Apparently SetForegroundWindow
    # won't work randomly, so I have to call ShowWindow twice.
    # Once to hide the window, and again to successfully raise the window.
    try:
        top_windows = []
        fg_win = win32gui.GetForegroundWindow()
        win32gui.EnumWindows(window_enumeration_handler, top_windows)
        for entry in top_windows:
            if is_mpv_window(entry):
                if entry[0] != fg_win:
                    win32gui.ShowWindow(entry[0], 6)  # Minimize
                    win32gui.ShowWindow(entry[0], 9)  # Un-minimize
                break

    except Exception:
        log.error("Could not raise MPV.", exc_info=True)


def mirror_act(state: bool, name: str = MIRROR_WINDOW_NAME):
    try:
        top_windows = []
        win32gui.EnumWindows(window_enumeration_handler, top_windows)
        for i in top_windows:
            if name in i[1]:
                win32gui.ShowWindow(i[0], 9 if state else 6)
                break

    except Exception:
        log.error("Could not raise/lower MPV mirror.", exc_info=True)
