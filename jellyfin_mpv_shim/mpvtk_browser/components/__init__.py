"""Leaf-level UI building blocks for the mpvtk browser.

Everything in this package is a plain function: data in, widget tree (or a
derived value) out. A component may take render resources and callbacks; it
must never reach for the app shell, the route dict, the data source or
navigation. ``tests/test_source_invariants.py`` enforces that, and
``docs/ARCHITECTURE_TARGET.md`` §1.4 explains why the line is drawn there.

The practical test for whether something belongs here: it needs ``art`` and
callbacks, but never ``nav``, ``source`` or ``route``.
"""

from .labels import (
    episode_subtitle,
    heading_for,
    human_size,
    is_watched,
    mixed_kind_glyph,
    placeholder_glyph,
    section_offsets,
    tile_lines,
    track_artists,
    track_duration,
)
from .banner import compose_banner, wrap_pil

__all__ = [
    "compose_banner",
    "episode_subtitle",
    "heading_for",
    "human_size",
    "is_watched",
    "mixed_kind_glyph",
    "placeholder_glyph",
    "section_offsets",
    "tile_lines",
    "track_artists",
    "track_duration",
    "wrap_pil",
]
