# Test-environment shim (virtual display only, never a default): lets libmpv handles use software rendering.
#
# Inert unless JH_XVFB_GL_SHIM=1 is set AND no Wayland session is visible (it must never touch a run on the
# real desktop). It only supplies a default for the one mpv option `gpu_sw`; an explicit gpu_sw from the caller
# wins, and nothing else about the handle is changed. mpv still renders for real, in software -- the shim does
# not stub or fake any rendering. Only a missing `mpv` module is tolerated; any other error is left to surface.
import os

if os.environ.get("JH_XVFB_GL_SHIM") == "1" and not os.environ.get("WAYLAND_DISPLAY"):
    try:
        import mpv as _mpv
    except ImportError:
        _mpv = None
    if _mpv is not None:
        _orig = _mpv.MPV.__init__

        def _init(self, *a, **kw):
            kw.setdefault("gpu_sw", "yes")
            _orig(self, *a, **kw)

        _mpv.MPV.__init__ = _init
