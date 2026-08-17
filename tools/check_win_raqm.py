#!/usr/bin/env python3
"""Assert that this build can shape text -- i.e. that Pillow has Raqm.

Sibling of ``check_win_libmpv_deps.py``, and it exists for the same reason:
the failure is invisible to everything downstream, so the one place it can be
caught is a step that asks directly.

**Pillow's binary wheels ship no FriBiDi, on any platform.** libraqm and
HarfBuzz are compiled into ``_imagingft``; FriBiDi alone is loaded at runtime
by a vendored shim, and Windows has no such DLL. Without it ``have_raqm`` is
0 and ``ImageFont.truetype`` silently returns a font using ``Layout.BASIC``
-- no exception, and no warning unless RAQM was asked for by name, which
nothing in this app does.

What that costs, and why it is not merely cosmetic:

* Right-to-left text is drawn in logical order with isolated letterforms.
  Arabic titles come out reversed and disconnected wherever the shim bakes
  text into a bitmap -- tile captions, Cast & Crew, the heading over a
  backdrop -- while the same string drawn as an ASS node is correct, because
  libass carries its own FriBiDi. That is #689.
* HarfBuzz is reachable only *through* Raqm, so no script gets GPOS shaping,
  Latin included. ``mpvtk.metrics`` measures with Pillow to model what libass
  will draw, so unkerned measurements feed every ellipsize and wrap decision
  in the UI.

Neither shows up in a test run, a smoke test or a screenshot of an English
library: the app starts, draws, and looks right. Hence a build step.

Run with no arguments after installing dependencies::

    python tools/check_win_raqm.py

``--require-bundled`` additionally insists the DLL came from a file we ship
rather than from somewhere on the machine's search path -- which is what a
release build wants, since a developer's box may have FriBiDi installed for
unrelated reasons and would otherwise pass while the artifact is broken.
"""

import argparse
import os
import sys


def _find_repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-bundled", action="store_true",
        help="fail unless a FriBiDi shipped with the app is what satisfied it")
    args = parser.parse_args()

    sys.path.insert(0, _find_repo_root())

    try:
        from PIL import features
    except ImportError as exc:
        print(f"error: Pillow is not importable: {exc}", file=sys.stderr)
        return 1

    # Through the app's own loader, so this checks the path the app takes
    # rather than a happier one. On a non-Windows box it is a no-op and the
    # answer comes from the system FriBiDi, which is the correct answer
    # there.
    from jellyfin_mpv_shim.win_fribidi import preload

    bundled = preload()

    raqm = features.check("raqm")
    print(f"Pillow      : {features.version('pil')}")
    print(f"RAQM        : {raqm}")
    print(f"FriBiDi     : {features.version_feature('fribidi')}")
    print(f"HarfBuzz    : {features.version_feature('harfbuzz')}")
    print(f"bundled DLL : {bundled or '(none found beside the app)'}")

    if not raqm:
        print("", file=sys.stderr)
        print("error: Pillow has no Raqm layout engine, so this build draws "
              "right-to-left text unshaped and measures every string "
              "unkerned. On Windows that means a FriBiDi DLL is missing from "
              "the build -- see jellyfin_mpv_shim/win_fribidi.py.",
              file=sys.stderr)
        return 1

    if args.require_bundled and not bundled:
        print("", file=sys.stderr)
        print("error: Raqm is available, but not from a FriBiDi we ship -- "
              "something already on this machine satisfied it. The shipped "
              "artifact would not have that, so this is a pass that does not "
              "transfer. Check the DLL reached the build directory.",
              file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
