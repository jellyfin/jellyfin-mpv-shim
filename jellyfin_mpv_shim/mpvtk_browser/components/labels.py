"""Item-derived strings and small layout maths.

Moved verbatim from ``TilesMixin`` — these never used ``self``, so being
methods only made them look coupled. Each carries the comment explaining the
bug it encodes; those are the reason to keep the logic rather than "simplify"
it later.
"""


#: Types whose caption is a LISTING -- "which channel, and when" rather
#: than a year or an episode number. The set ``air_time_line`` splits and
#: ``episode_subtitle`` composes.
LISTING_TYPES = ("Program", "Timer", "Recording", "SeriesTimer")


def air_time_line(item):
    """When a listing is on, as its own caption line — or "" for anything
    that is not a listing, or has no air time to report.

    Split out of :func:`episode_subtitle` because "BBC Two   ·   20:00 -
    20:30" is about 200px of text and a poster tile is 150 wide, so the
    channel survived and the time -- the half a listing exists to tell you
    -- was ellipsized away. Two lines fit; one never did.

    ``episode_subtitle(air_time=False)`` is the other half. Keeping them as
    a pair rather than having the caller slice a joined string means the
    separator, the SeriesTimer "any time" case and the recording-with-no-
    start case are decided once.
    """
    kind = item.get("Type")
    if kind not in LISTING_TYPES or item.get("_subtitle") is not None:
        return ""
    from .. import live_tv

    if kind == "SeriesTimer":
        # A series rule pinned to a slot says the slot; one that records at
        # any time has no air time to give, and says nothing.
        return "" if item.get("RecordAnyTime") else _a_time(item, live_tv)
    return live_tv.air_time_label(item)


def episode_subtitle(item, show_year=True, air_time=True):
    """The line under a tile's title.

    ``air_time=False`` leaves the air time out of a listing's line, for a
    caller that is drawing it as a third line instead — see
    :func:`air_time_line`. It changes nothing for any other item type.

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

        when = live_tv.air_time_label(item) if air_time else ""
        return "   ·   ".join(p for p in (item.get("ChannelName") or "",
                                          when) if p)
    if kind == "SeriesTimer":
        # A series rule has no single air time; what identifies it is the
        # channel it watches and whether it is pinned to one time slot.
        from .. import live_tv

        when = ("" if not air_time or item.get("RecordAnyTime")
                else _a_time(item, live_tv))
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


def tile_lines(item, parent_item=False, show_year=True, air_time=True):
    """``(title, subtitle)`` for a tile.

    ``air_time`` is passed through to :func:`episode_subtitle`: False when
    the caller is going to draw a listing's air time on its own line.

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
    return item.get("Name", ""), episode_subtitle(item, show_year, air_time)


def is_watched(item):
    """Whether a tile shows the watched check.

    Was ``TilesMixin._is_watched``.
    """
    ud = item.get("UserData") or {}
    if ud.get("Played"):
        return True
    # Folder is here for audiobooks: a book read from a folder of chapter
    # files is finished when its chapters are, and nothing sets Played on
    # the folder itself when you simply listen through it -- only marking it
    # by hand does. Without this a book you had actually finished showed no
    # tick, which is the one thing a shelf of them needs to say. It is right
    # for a Home Videos folder for the same reason.
    if item.get("Type") in ("Series", "Season", "Folder"):
        # `or 0` would read a MISSING count as zero-unplayed, i.e.
        # fully watched — so a Series DTO without UserData (search
        # results, the synthesized season fallback) showed a watched
        # check, and the toggle computed `not watched` and marked an
        # unwatched show unwatched: a no-op that reads as a dead button.
        return ud.get("UnplayedItemCount") == 0
    return False


def placeholder_glyph(item):
    """What to draw on a tile with no artwork.

    A **Material icon name** where one fits, else the title's first
    initial. The compositor tells them apart by looking the name up
    (`strips._paint_poster`), so a one-character answer can never be
    mistaken for an icon and vice versa.

    Library tiles are asked by collection type first: a UserView's `Type`
    is "CollectionFolder" for every library there is, so answering from
    the type map alone drew a folder on all of them.
    """
    ctype = (item.get("CollectionType") or "").lower()
    glyph = _LIBRARY_GLYPHS.get(ctype) if ctype else None
    if glyph is None:
        glyph = _TYPE_GLYPHS.get(item.get("Type"))
    if glyph:
        return glyph
    name = (item.get("Name") or "").strip()
    return name[0].upper() if name else "?"


#: Types that get a marker in the corner of their tile, and the Material
#: icon for each. jellyfin-web's ``getTypeIndicator``
#: (``components/indicators/indicators.js:140-149``) verbatim -- these four
#: types and no others.
#:
#: The point is a Home Videos library, which holds folders, photo albums,
#: photos and clips side by side: with artwork on all four there is nothing
#: in the tile that says which will open and which will start playing.
#: Nothing else in a library has this problem, which is why the map is
#: short rather than why the *drawing* is conditional -- see below.
#:
#: **Per item, not per row.** This was briefly decided per row ("only chip a
#: row holding more than one kind"), reasoning that a uniform row needs no
#: telling apart. It reads as icons flickering in and out as you scroll: the
#: rows are a grid of one folder, and whether the four videos and one album
#: you are looking at happen to share a row is not something a user can see
#: or should have to. Web draws it for every card of these types, in every
#: view, and that is both simpler and what a user coming from it expects.
TYPE_INDICATOR_ICONS = {
    "Video": "videocam",
    "Folder": "folder",
    "PhotoAlbum": "photo_album",
    "Photo": "photo",
}


def type_indicator_icon(item):
    """The corner type marker for a tile, or "" for types that get none."""
    return TYPE_INDICATOR_ICONS.get(item.get("Type"), "")


#: Types whose placeholder says *what it is* rather than what it is called.
#:
#: A first initial is a decent label when the name distinguishes things --
#: films, shows, people. It is useless where the name does not: a Home Videos
#: library is folders and albums named "2019", "2020", "Holiday", and a wall
#: of digits says nothing about which tiles you can open and which will start
#: playing. jellyfin-web draws an icon for exactly these
#: (``getItemTypeIcon``, ``utils/image.ts:130-161``).
#:
#: **Material icon names**, drawn by the strip compositor.
#:
#: A comment here used to say these had to be characters "because this is
#: baked into the tile bitmap by the strip compositor, which draws text,
#: not icon fonts". That was never true -- `strips._paint_glyph_badge` has
#: rasterized icon paths through `vector.icon_image` since the home-video
#: type badges landed. **[iw]**: "that comment is a lie."
#:
#: Taken from jellyfin-web rather than chosen, so a library with no
#: artwork looks like the same library does in every other client:
#: `getItemTypeIcon` for an item, `getLibraryIcon` for a library tile
#: (both `src/utils/image.ts`).
_TYPE_GLYPHS = {
    "MusicAlbum": "album",
    "MusicArtist": "person",
    "Person": "person",
    "Audio": "audiotrack",
    "Movie": "movie",
    "Series": "tv",
    "Episode": "tv",
    "Season": "tv",
    "Program": "live_tv",
    "TvChannel": "live_tv",
    "Book": "book",
    "Folder": "folder",
    "CollectionFolder": "folder",
    "BoxSet": "video_library",
    "Playlist": "queue",
    "Photo": "photo",
    "PhotoAlbum": "photo_album",
    # Beyond web's list, and consistent with it: a Trailer is a video and
    # a MusicVideo has its own icon in the library map.
    "Trailer": "theaters",
    "MusicVideo": "music_video",
    # web has no AudioBook arm, but one IS an Audio item, and this is the
    # case the table was originally written for: an author folder of three
    # books called "The ..." drew three tiles all reading "T".
    "AudioBook": "audiotrack",
}


#: A library tile, by its collection type -- web's `getLibraryIcon`. A
#: UserView carries no useful `Type`, so it is answered from here first.
_LIBRARY_GLYPHS = {
    "movies": "movie",
    "music": "music_note",
    "homevideos": "photo",
    "photos": "photo",
    "livetv": "live_tv",
    "tvshows": "tv",
    "trailers": "theaters",
    "musicvideos": "music_video",
    "books": "book",
    "boxsets": "video_library",
    "playlists": "queue",
    "channels": "videocam",
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
