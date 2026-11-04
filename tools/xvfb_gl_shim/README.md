# Software-GL shim for the real-player integration legs on a virtual display

Why: under Xvfb mpv refuses software rendering by default ("Suspected software renderer or indirect
context"), so no GPU video output initialises, mpv falls back to `sdl`, and the real-player legs of
`tests/integration/run_integration.py` stall (`test_settings_screen`) or fail (window decorations). Passing
`gpu-sw=yes` lets it use the software Vulkan/GL path that Xvfb offers.

    env -u WAYLAND_DISPLAY JH_XVFB_GL_SHIM=1 PATH="$PWD/tools/xvfb_gl_shim:$PATH" PYTHONPATH="$PWD/tools/xvfb_gl_shim" \
        tools/limited_run.sh -t 900 -- python3 tests/integration/run_integration.py --backend libmpv

* `sitecustomize.py` defaults `gpu_sw="yes"` for libmpv handles; `mpv` does the same for the external backend.
* Inert unless `JH_XVFB_GL_SHIM=1` is set **and** `WAYLAND_DISPLAY` is unset; it only supplies a default for
  `gpu_sw` (an explicit value wins), swallows nothing but a missing `mpv` module, and renders for real in
  software (it fakes no output). Covered by `tests/test_xvfb_gl_shim.py`.
* Test environment only. It changes neither tests nor application defaults, and says nothing about GPU output,
  hardware decoding or visual quality on a real desktop.
* Always unset `WAYLAND_DISPLAY` for virtual-display runs (the runner now does it for `xvfb-run` legs).
