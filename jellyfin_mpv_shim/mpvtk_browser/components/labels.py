"""Item-derived strings and small layout maths.

Moved verbatim from ``TilesMixin`` — these never used ``self``, so being
methods only made them look coupled. Each carries the comment explaining the
bug it encodes; those are the reason to keep the logic rather than "simplify"
it later.
"""


def episode_subtitle(item):
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
    if item.get("Type") == "Program":
        # The channel, not the year: "On Now" is a list of things airing
        # right now, so which channel to turn to is the useful half. Guide
        # data frequently has no ProductionYear at all.
        return item.get("ChannelName") or ""
    return str(item.get("ProductionYear") or "")


def tile_lines(item, parent_item=False):
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
    return item.get("Name", ""), episode_subtitle(item)


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
    if item.get("Type") in ("Audio", "MusicAlbum", "MusicArtist"):
        return "♪"  # ♪
    name = (item.get("Name") or "").strip()
    return name[0].upper() if name else "?"


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
