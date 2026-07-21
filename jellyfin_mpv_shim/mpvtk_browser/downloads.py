"""Shaping the sync catalog into the downloads manager's display tree.

This is view logic over plain rows, and it used to live in ``ui.py`` — the
player bridge — where it was ~110 lines of grouping wedged between thin
controller methods, and untestable without a live ``syncManager``. Here it
is pure: rows in, display tree out.

``ui.py`` keeps the seam (reaching the sync db, catching its failures) and
calls these; ``SettingsMixin`` renders what comes back.
"""

import json

from ..i18n import _
from ..sync.db import (ORIGIN_AUTO_NEXT_UP, ORIGIN_AUTO_LOOKAHEAD, is_auto)

# Automatic downloads are shown as their own subtrees rather than mixed into
# the series they belong to: they arrived without being asked for, they are
# the only ones the reaper may delete, and seeing them separately is how you
# tell what the scheduler is holding. Ordered as rendered.
AUTO_GROUPS = (
    (ORIGIN_AUTO_NEXT_UP, _("Automatic: Next Up")),
    (ORIGIN_AUTO_LOOKAHEAD, _("Automatic: Actively Watched")),
)

#: Fallback bucket for an auto row whose origin names no source we know —
#: only reachable from a catalog written by an early build of the feature.
#: It still has to appear somewhere, or it would be disk used with no way to
#: reclaim it from this screen.
AUTO_OTHER_TITLE = _("Automatic")

# A downloaded playlist made only of these is listed item by item in the
# downloads manager. Whitelist, not an audio blacklist: a row with a missing
# or unrecognized type must stay collapsed rather than risk unfolding a
# few-hundred-track music playlist.
VIDEO_TYPES = ("Movie", "Episode", "Video")


def season_title(row):
    """Display name for a downloaded episode's season."""
    try:
        name = (json.loads(row.get("item_json") or "{}")
                .get("SeasonName") or "").strip()
    except (ValueError, TypeError):
        name = ""
    if name:
        return name
    idx = row.get("parent_index")
    if idx is None:
        return _("Episodes")
    return _("Specials") if idx == 0 else _("Season %s") % idx


def row_size(row):
    """Bytes to show for a row.

    ``size_bytes`` is the *expected* size and is only known once the source
    has been probed; ``downloaded_bytes`` is what is actually on disk.
    Reading a non-existent "size" key is why this used to show 0 B.
    """
    return (row.get("downloaded_bytes") or 0) or (row.get("size_bytes") or 0)


def row_watched(row):
    """Whether a downloaded item has been played.

    The catalog stores the server's UserData blob verbatim; nothing was
    reading Played out of it, so the downloads panel could neither mark a
    watched item nor tell whether "Remove Watched" would delete anything.
    """
    try:
        return bool(json.loads(row.get("userdata_json") or "{}").get("Played"))
    except (ValueError, TypeError):
        return False


def _entry(row):
    return {
        "kind": "item",
        "id": row.get("item_id"),
        "title": row.get("name") or row.get("item_id"),
        "status": row.get("status") or "",
        "size": row_size(row),
        "index": row.get("index_number"),
        # Kept apart from "size" so the view can show a percentage: size is
        # whichever of the two is meaningful, these are the raw pair.
        "done": row.get("downloaded_bytes") or 0,
        "total": row.get("size_bytes") or 0,
        "watched": row_watched(row),
    }


def status_text(entry):
    """What to show next to a downloading item.

    The raw catalog values ("pending", "downloading") were rendered verbatim
    and untranslated. Tk turned them into "Queued" / "Downloading 42%", which
    is the difference between a status column and a debug dump.
    """
    from ..sync.db import (STATUS_COMPLETE, STATUS_DOWNLOADING,
                           STATUS_ERROR, STATUS_PENDING)
    status = entry.get("status") or ""
    if status == STATUS_COMPLETE:
        return ""                      # the size says it; no label needed
    if status == STATUS_DOWNLOADING:
        total = entry.get("total") or 0
        done = entry.get("done") or 0
        if total:
            return _("Downloading %d%%") % int(done * 100 / total)
        return _("Downloading")
    if status == STATUS_PENDING:
        return _("Queued")
    if status == STATUS_ERROR:
        return _("Failed")
    return status


def group_downloads(rows, playlists, playlist_items, owned):
    """Downloads grouped for display, mirroring the Tk DownloadsPanel::

        [{"kind": "playlist"|"series"|"movies", "title", "id",
          "size", "count", "children": [...]}]

    ``rows`` is the flat catalog, ``playlists`` the playlist records,
    ``playlist_items(playlist_id)`` yields a playlist's rows, and ``owned``
    maps item_id -> playlist_id.

    Playlists come first. A *music* playlist is listed collapsed — hundreds
    of tracks nobody wants enumerated — but a video playlist is a handful of
    films or episodes, so it expands like a series does. Either way its items
    are owned by the playlist and must not also appear below. Series nest
    their seasons; everything left over lands in one flat group.
    """
    # Playlists that still exist as records. An ownership row can outlive its
    # playlist (deleted, or its members all removed), and skipping those rows
    # unconditionally made the downloads invisible AND undeletable — disk used
    # with no way to reclaim it. Only skip what a LIVE playlist group shows.
    live = {pl["playlist_id"] for pl in playlists}

    out = []
    for pl in playlists:
        items = playlist_items(pl["playlist_id"])
        # An all-video playlist lists its items; anything else (music, or
        # mixed — one video in a 400-song playlist must not unfold the whole
        # thing) stays collapsed.
        video = bool(items) and all(
            (r.get("type") or "") in VIDEO_TYPES for r in items)
        out.append({
            "kind": "playlist",
            "id": pl["playlist_id"],
            "title": pl.get("name") or _("Playlist"),
            "size": sum(row_size(r) for r in items),
            "count": len(items),
            "watched_count": sum(1 for r in items if row_watched(r)),
            "children": [_entry(r) for r in items] if video else [],
        })

    # Automatic downloads are lifted out before the series/movies grouping so
    # they appear once, under the source that fetched them, rather than mixed
    # into the shows the user chose to download by hand.
    auto = {}
    series = {}
    loose = []
    for r in rows:
        if owned.get(r.get("item_id")) in live:
            continue                 # counted under its playlist
        if is_auto(r.get("origin")):
            auto.setdefault(r.get("origin"), []).append(r)
            continue
        sid = r.get("series_id")
        if not sid:
            loose.append(_entry(r))
            continue
        show = series.setdefault(sid, {
            "kind": "series", "id": sid,
            "title": r.get("series_name") or _("Unknown Series"),
            "size": 0, "count": 0, "watched_count": 0, "children": {},
        })
        show["size"] += row_size(r)
        show["count"] += 1
        show["watched_count"] = show.get("watched_count", 0) + (
            1 if row_watched(r) else 0)
        season_id = r.get("season_id") or sid
        season = show["children"].setdefault(season_id, {
            "kind": "season", "id": season_id, "series_id": sid,
            # Season 0 is Specials, not "Season 0"; the catalog's stored
            # SeasonName is better than either when present.
            "title": season_title(r),
            "size": 0, "count": 0, "watched_count": 0, "children": [],
        })
        season["size"] += row_size(r)
        season["count"] += 1
        season["watched_count"] = season.get("watched_count", 0) + (
            1 if row_watched(r) else 0)
        season["children"].append(_entry(r))

    # Automatic groups lead: they are the ones that change without the user
    # doing anything, so they are what you open this screen to check.
    known = {origin for origin, _t in AUTO_GROUPS}
    ordered = list(AUTO_GROUPS) + [
        (o, AUTO_OTHER_TITLE) for o in sorted(auto) if o not in known]
    for origin, title in ordered:
        items = auto.get(origin)
        if not items:
            continue
        items = sorted(items, key=lambda r: (
            str(r.get("series_name") or ""), r.get("parent_index") or 0,
            r.get("index_number") or 0, str(r.get("name") or "")))
        out.append({
            # A kind of its own, with id None: the renderer deletes a group
            # without a server-side id by listing its item ids explicitly,
            # which is exactly right here — there is no server-side object
            # that means "the things auto-download fetched".
            "kind": "auto", "id": None, "origin": origin, "title": title,
            "size": sum(row_size(r) for r in items),
            "count": len(items),
            "watched_count": sum(1 for r in items if row_watched(r)),
            "children": [_entry(r) for r in items],
        })

    shows = []
    for show in series.values():
        seasons = sorted(show["children"].values(),
                         key=lambda x: str(x["title"]))
        for s2 in seasons:
            s2["children"].sort(key=lambda e: (e["index"] is None,
                                               e["index"], e["title"]))
        show["children"] = seasons
        shows.append(show)
    shows.sort(key=lambda g: str(g["title"]))
    out += shows
    if loose:
        loose.sort(key=lambda e: str(e["title"]))
        out.append({"kind": "movies", "id": None,
                    "title": _("Movies & Videos"),
                    "size": sum(e["size"] for e in loose),
                    "count": len(loose),
                    "watched_count": sum(1 for e in loose if e["watched"]),
                    "children": loose})
    return out


def progress_summary(pending_rows):
    """Status-bar line for downloads still in flight:
    ``{"pending": n, "name": str, "percent": int|None}``, or None when
    ``pending_rows`` is empty."""
    if not pending_rows:
        return None
    # The in-flight one is whichever has bytes on disk but isn't done.
    active = next((r for r in pending_rows
                   if (r.get("downloaded_bytes") or 0) > 0), pending_rows[0])
    total = active.get("size_bytes") or 0
    done = active.get("downloaded_bytes") or 0
    return {
        "pending": len(pending_rows),
        "name": active.get("name") or "",
        "percent": int(done * 100 / total) if total else None,
    }
