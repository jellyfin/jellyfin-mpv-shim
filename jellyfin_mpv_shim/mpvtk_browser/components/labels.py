"""Item-derived strings and small layout maths.

Moved verbatim from ``TilesMixin`` — these never used ``self``, so being
methods only made them look coupled. Each carries the comment explaining the
bug it encodes; those are the reason to keep the logic rather than "simplify"
it later.
"""


def episode_subtitle(item, show_year=True):
    """The line under a tile's title.

    Was ``TilesMixin._subtitle``.
    """
    if item.get("_subtitle") is not None:
        return item["_subtitle"]      # pseudo-items (chapters)
    if item.get("Type") == "Episode":
        # Lead with the series. A bare "S1E1" on a Continue Watching or
        # Next Up tile does not say which show it belongs to, which is
        # the one thing you need there.
        series = item.get("SeriesName") or ""
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if s is not None and e is not None:
            se = "S%dE%d" % (s, e)
            return "%s · %s" % (series, se) if series else se
        return series
    kind = item.get("Type")
    if kind in ("Program", "Timer", "Recording"):
        # The channel and when it is on, not the year: these are listings,
        # so "which channel, and when" is the whole useful content of the
        # line. Guide data frequently has no ProductionYear at all.
        from .. import live_tv

        return "   ·   ".join(p for p in (item.get("ChannelName") or "",
                                          live_tv.air_time_label(item)) if p)
    if kind == "SeriesTimer":
        # A series rule has no single air time; what identifies it is the
        # channel it watches and whether it is pinned to one time slot.
        from .. import live_tv

        when = (_a_time(item, live_tv) if not item.get("RecordAnyTime")
                else "")
        channel = ("" if item.get("RecordAnyChannel")
                   else (item.get("ChannelName") or ""))
        return "   ·   ".join(p for p in (channel, when) if p)
    if kind == "TvChannel":
        # The channel number and what is on it right now — which is why the
        # channel list asks for AddCurrentProgram.
        current = (item.get("CurrentProgram") or {}).get("Name") or ""
        number = str(item.get("Number") or "").strip()
        return "   ·   ".join(p for p in (number, current) if p)
    # The year is the ONLY thing show_year governs. Every branch above is a
    # channel, an air time or an episode number -- a Live TV listing with
    # "showYear off" must still say which channel and when, because that is
    # not a year and switching it off was not a request to blank the line.
    return str(item.get("ProductionYear") or "") if show_year else ""


def _a_time(item, live_tv):
    start = live_tv.parse_time(item.get("StartDate"))
    return live_tv.fmt_time(start) if start else ""


def tile_lines(item, parent_item=False, show_year=True):
    """``(title, subtitle)`` for a tile.

    ``parent_item`` flips an episode around: the series becomes the title
    and the episode name the subtitle. That is what a "Latest TV" row wants
    — the server hands back a Series when a show got several new episodes
    and a bare Episode when it got one, so without this the same row reads
    as a list of shows with an episode title dropped in the middle of it.
    Anything that is not an Episode is unaffected, series rows included.
    """
    if parent_item and item.get("Type") == "Episode":
        series = item.get("SeriesName")
        if series:
            return series, item.get("Name", "")
    return item.get("Name", ""), episode_subtitle(item, show_year)


def is_watched(item):
    """Whether a tile shows the watched check.

    Was ``TilesMixin._is_watched``.
    """
    ud = item.get("UserData") or {}
    if ud.get("Played"):
        return True
    if item.get("Type") in ("Series", "Season"):
        # `or 0` would read a MISSING count as zero-unplayed, i.e.
        # fully watched — so a Series DTO without UserData (search
        # results, the synthesized season fallback) showed a watched
        # check, and the toggle computed `not watched` and marked an
        # unwatched show unwatched: a no-op that reads as a dead button.
        return ud.get("UnplayedItemCount") == 0
    return False


def placeholder_glyph(item):
    """The character drawn on a tile with no artwork.

    Was ``TilesMixin._glyph``.
    """
    glyph = _TYPE_GLYPHS.get(item.get("Type"))
    if glyph:
        return glyph
    name = (item.get("Name") or "").strip()
    return name[0].upper() if name else "?"


#: Types a mixed grid has to tell apart at a glance. A Home Videos library
#: holds folders, photo albums, photos and clips side by side, and with
#: artwork on all four there is nothing in the tile that says which will open
#: and which will start playing.
#:
#: Only these four. A movies library is all one kind, so a chip on every tile
#: would be noise -- which is why the *grid* decides whether to draw them (see
#: TileRenderer.image_map) rather than each tile deciding for itself.
MIXED_KIND_GLYPHS = {
    "Folder": "▸",
    "CollectionFolder": "▸",
    "PhotoAlbum": "▣",
    "Photo": "▣",
    "Video": "▶",
    "MusicVideo": "▶",
}


def mixed_kind_glyph(item):
    """The type chip for a tile in a mixed grid, or "" for types that do not
    need one."""
    return MIXED_KIND_GLYPHS.get(item.get("Type"), "")


#: Types whose placeholder says *what it is* rather than what it is called.
#:
#: A first initial is a decent label when the name distinguishes things --
#: films, shows, people. It is useless where the name does not: a Home Videos
#: library is folders and albums named "2019", "2020", "Holiday", and a wall
#: of digits says nothing about which tiles you can open and which will start
#: playing. jellyfin-web draws an icon for exactly these
#: (``getItemTypeIcon``, ``utils/image.ts:130-161``).
#:
#: Glyphs rather than the Material icons used elsewhere in the chrome because
#: this is baked into the tile bitmap by the strip compositor, which draws
#: text, not icon fonts.
_TYPE_GLYPHS = {
    "Audio": "♪",
    "MusicAlbum": "♪",
    "MusicArtist": "♪",
    "Folder": "▸",
    "CollectionFolder": "▸",
    "PhotoAlbum": "▣",
    "Photo": "▣",
}


def heading_for(item):
    """``(title, context)`` for a detail heading.

    An episode's series and SxEy go on their own line above the episode
    title rather than being joined into one string — joined, a name of
    any length ran off the end of the banner and was cut mid-word
    ("Clannad · S1E1 · On the Hillside Pa").

    Was ``TilesMixin._heading_for``.
    """
    title = item.get("Name", "")
    if item.get("Type") != "Episode":
        return title, ""
    s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
    se = "S%sE%s" % (s, e) if s is not None and e is not None else ""
    context = "   ·   ".join(
        p for p in (item.get("SeriesName"), se) if p)
    return title, context


def section_offsets(elements, gap, pad=0):
    """Content-y of each element's top in a ``Column(pad=pad, gap=gap)`` —
    the snap breakpoints for a variable-height list (home sections, which
    are heading + carousel of differing heights). Uses the layout engine's
    own measurement so a stop lands the section heading flush, not a few px
    off. None entries are skipped to match Column.

    Was ``TilesMixin._section_offsets``.
    """
    from ...mpvtk.layout import measure

    offs, y = [], float(pad)
    for el in elements:
        if el is None:
            continue
        offs.append(y)
        y += measure(el)[1] + gap
    return offs


def track_duration(item):
    """``m:ss`` for a track, or "" when the server gave no runtime.

    Was ``MusicMixin._duration``; moved so the track table can be rendered
    without a MusicMixin (see tile_renderer.TileRenderer.track_list).
    """
    secs = (item.get("RunTimeTicks") or 0) // 10000000
    return "%d:%02d" % (secs // 60, secs % 60) if secs else ""


def track_artists(item):
    """Comma-joined artist names, falling back to album artists.

    Was ``MusicMixin._artists``.
    """
    return ", ".join(item.get("Artists") or item.get("AlbumArtists") or [])


def human_size(n):
    """Byte count as a short human string.

    Was ``DialogsMixin._human_size``; the detail page's media-info line
    wants it too, and a second copy is how the two drift.
    """
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit) if unit == "B"
                    else "%.1f %s" % (n, unit))
        n /= 1024
