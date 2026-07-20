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
            "children": [_entry(r) for r in items] if video else [],
        })

    series = {}
    loose = []
    for r in rows:
        if owned.get(r.get("item_id")) in live:
            continue                 # counted under its playlist
        sid = r.get("series_id")
        if not sid:
            loose.append(_entry(r))
            continue
        show = series.setdefault(sid, {
            "kind": "series", "id": sid,
            "title": r.get("series_name") or _("Unknown Series"),
            "size": 0, "count": 0, "children": {},
        })
        show["size"] += row_size(r)
        show["count"] += 1
        season_id = r.get("season_id") or sid
        season = show["children"].setdefault(season_id, {
            "kind": "season", "id": season_id, "series_id": sid,
            # Season 0 is Specials, not "Season 0"; the catalog's stored
            # SeasonName is better than either when present.
            "title": season_title(r),
            "size": 0, "count": 0, "children": [],
        })
        season["size"] += row_size(r)
        season["count"] += 1
        season["children"].append(_entry(r))

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
                    "count": len(loose), "children": loose})
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
