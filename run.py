#!/usr/bin/env python3

# Newer revisions of python-mpv require mpv-1.dll in the PATH.
import os
import sys
if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    os.environ["PATH"] = os.path.dirname(__file__) + os.pathsep + os.environ["PATH"]

from plex_mpv_shim.mpv_shim import main
main()

