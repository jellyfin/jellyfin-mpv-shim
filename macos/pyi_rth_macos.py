# PyInstaller runtime hook for the macOS app (macos/build.sh). Runs before the
# app's own code, inside the frozen bundle only.
import ctypes
import os
import sys

# Pillow's shim dlopens the bare name "libfribidi.dylib", and dyld answers
# that from an image already loaded under the same install name. A Mac has
# no FriBiDi unless Homebrew installed one, so the bundle ships its own.
_fribidi = os.path.join(sys._MEIPASS, "libfribidi.dylib")
if os.path.exists(_fribidi):
    try:
        ctypes.CDLL(_fribidi, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass


# The libmpv binding is left out of the bundle. Fail its import the way
# python-mpv does when libmpv is missing, so the app takes the external mpv
# path it already has for that.
class _NoLibmpv:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name == "mpv":
            raise OSError("libmpv is not bundled in the macOS app")
        return None


sys.meta_path.insert(0, _NoLibmpv)
