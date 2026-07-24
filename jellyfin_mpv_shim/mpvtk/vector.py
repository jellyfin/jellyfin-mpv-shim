"""Vector icons for mpvtk — the same Material icon set and SVG->ASS
pipeline the Tk browser (rasterized) and the jellyfin OSC (ASS) use.

Icons are converted at first use from `ui_icon_paths.py`
(dep-free generated data) via the shared `svgpath` converter, on the
24x24 unit canvas with the OSC's corner-anchor convention: two
zero-length contours pin the bounding box so libass scales and aligns
the drawing exactly like a 24x24 box regardless of the glyph's ink.
The renderer scales with \\fscx/\\fscy — crisp at any size.
"""

import logging

from ..ui_icon_paths import ICON_PATHS
from ..svgpath import svg_path_to_ass

log = logging.getLogger("mpvtk.vector")

_ANCHOR = "m 0 0 l 0 0 m 24 24 l 24 24"
_cache = {}
_warned = set()


def icon_ass(name):
    """Unit-canvas (24x24, corner-anchored) ASS drawing for a Material
    icon.

    An unknown name yields an empty (but correctly sized) drawing rather
    than raising. This used to be a KeyError out of the middle of layout,
    which takes down the *entire* scene — one button naming an icon that
    isn't in the generated set and the whole browser goes blank. A missing
    glyph is a cosmetic bug; it should not be a fatal one. It is logged
    once per name so it still gets noticed.
    """
    path = _cache.get(name)
    if path is None:
        try:
            drawing = svg_path_to_ass(ICON_PATHS[name])
        except KeyError:
            if name not in _warned:
                _warned.add(name)
                log.warning("unknown icon %r — rendering nothing", name)
            drawing = ""
        path = ("%s %s" % (_ANCHOR, drawing)) if drawing else _ANCHOR
        _cache[name] = path
    return path


def icon_names():
    return sorted(ICON_PATHS)
