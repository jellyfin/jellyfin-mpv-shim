"""mpvtk: a declarative UI toolkit rendered inside the mpv window.

Python builds an element tree (widgets.py), lays it out to a flat scene
(layout.py) and pushes it as JSON to a Lua renderer running inside mpv
(renderer.lua). The renderer owns all per-frame interaction — hover,
scrolling, text editing, dropdown popups — locally, with no Python
round-trips; semantic events (clicks, edits, selections) come back and
typically result in a new scene being pushed.

Works identically over python-mpv-jsonipc and libmpv (app.py backends).
Spike status: see README.md in this directory.
"""

from .app import MpvtkApp
from .rawimage import write_bgra
from .widgets import (
    Box,
    Button,
    Column,
    Dropdown,
    Element,
    Form,
    Gradient,
    Grid,
    HScroll,
    Image,
    Progress,
    Row,
    Spacer,
    Stack,
    Table,
    Text,
    TextBox,
    VScroll,
)

__all__ = [
    "MpvtkApp",
    "write_bgra",
    "Box",
    "Button",
    "Column",
    "Dropdown",
    "Element",
    "Form",
    "Gradient",
    "Grid",
    "HScroll",
    "Image",
    "Progress",
    "Row",
    "Spacer",
    "Stack",
    "Table",
    "Text",
    "TextBox",
    "VScroll",
]
