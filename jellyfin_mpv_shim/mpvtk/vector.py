"""Vector icons for mpvtk — the same Material icon set and SVG->ASS
pipeline the Tk browser (rasterized) and the jellyfin OSC (ASS) use.

Icons are converted at first use from `ui_icon_paths.py`
(dep-free generated data) via the shared `svgpath` converter, on the
24x24 unit canvas with the OSC's corner-anchor convention: two
zero-length contours pin the bounding box so libass scales and aligns
the drawing exactly like a 24x24 box regardless of the glyph's ink.
The renderer scales with \\fscx/\\fscy — crisp at any size.
"""

from ..ui_icon_paths import ICON_PATHS
from ..svgpath import svg_path_to_ass

_ANCHOR = "m 0 0 l 0 0 m 24 24 l 24 24"
_cache = {}


def icon_ass(name):
    """Unit-canvas (24x24, corner-anchored) ASS drawing for a Material
    icon. Raises KeyError for unknown names."""
    path = _cache.get(name)
    if path is None:
        path = "%s %s" % (_ANCHOR, svg_path_to_ass(ICON_PATHS[name]))
        _cache[name] = path
    return path


def icon_names():
    return sorted(ICON_PATHS)
