#!/usr/bin/env python3

# Newer revisions of python-mpv require mpv-1.dll in the PATH.
import os
import sys
import multiprocessing

if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    # Detect if bundled via pyinstaller.
    # From: https://stackoverflow.com/questions/404744/
    if getattr(sys, "frozen", False):
        application_path = getattr(sys, "_MEIPASS")
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))
    os.environ["PATH"] = application_path + os.pathsep + os.environ["PATH"]

from jellyfin_mpv_shim.mpv_shim import main_desktop

if __name__ == "__main__":
    # https://stackoverflow.com/questions/24944558/pyinstaller-built-windows-exe-fails-with-multiprocessing
    multiprocessing.freeze_support()

    main_desktop(cef=True)
