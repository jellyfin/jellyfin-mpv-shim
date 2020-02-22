import win32gui
import logging

log = logging.getLogger('win_utils')

def windowEnumerationHandler(hwnd, top_windows):
    top_windows.append((hwnd, win32gui.GetWindowText(hwnd)))

def raise_mpv():
    # This workaround is madness. Apparently SetForegroundWindow
    # won't work randomly, so I have to call ShowWindow twice.
    # Once to hide the window, and again to successfully raise the window.
    try:
        top_windows = []
        fg_win = win32gui.GetForegroundWindow()
        win32gui.EnumWindows(windowEnumerationHandler, top_windows)
        for i in top_windows:
            if " - mpv" in i[1].lower():
                if i[0] != fg_win:
                    win32gui.ShowWindow(i[0], 6) # Minimize
                    win32gui.ShowWindow(i[0], 9) # Un-minimize
                break

    except Exception:
        log.error("Could not raise MPV.", exc_info=True)

def mirror_act(state):
    try:
        top_windows = []
        win32gui.EnumWindows(windowEnumerationHandler, top_windows)
        for i in top_windows:
            if "Jellyfin MPV Shim Mirror" in i[1]:
                win32gui.ShowWindow(i[0], 9 if state else 6)
                break

    except Exception:
        log.error("Could not raise/lower MPV mirror.", exc_info=True)
