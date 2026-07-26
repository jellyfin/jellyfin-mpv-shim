"""Pieces of an item's detail presentation, shared by more than one family.

``meta_line`` is wanted by detail, series *and* the music header; ``people_row``
by detail and series. That cross-family reach is what makes them components
rather than methods on a page.

See ``docs/ARCHITECTURE_TARGET.md`` §1.4 for the line being drawn.
"""

from ...i18n import _
from . import controls
from .labels import is_watched


def fmt_ticks(ticks):
    """h:mm:ss / m:ss — a bare minutes:seconds rendered a 1h20m resume
    offset as "80:00".

    Was ``ViewsMixin._fmt_ticks``.
    """
    secs = int((ticks or 0) // 10000000)
    h, m, sec = secs // 3600, (secs % 3600) // 60, secs % 60
    return ("%d:%02d:%02d" % (h, m, sec) if h
            else "%d:%02d" % (m, sec))


def meta_line(item):
    """The year · runtime · rating · genres line under a title.

    Was ``ViewsMixin._meta_line``.
    """
    parts = []
    if item.get("ProductionYear"):
        parts.append(str(item["ProductionYear"]))
    rt = item.get("RunTimeTicks")
    if rt:
        # h:mm:ss, like Tk and jellyfin-web. "112 min" makes you do the
        # arithmetic to know whether it fits in an evening.
        parts.append(fmt_ticks(rt))
    if item.get("OfficialRating"):
        parts.append(str(item["OfficialRating"]))
    if item.get("CommunityRating"):
        parts.append("★ %.1f" % item["CommunityRating"])
    # Genres are already fetched (repository asks for them); Tk showed up
    # to three here and dropping them lost the quickest read on what a
    # thing actually is.
    genres = ", ".join(item.get("Genres") or [])
    if genres:
        parts.append(genres)
    return "   ·   ".join(parts)


def people_row(tiles, people):
    """The Cast & Crew carousel, or None when there is no one credited.

    Takes the ``TileRenderer`` rather than reaching for a shell: the row is
    built from tiles and tile geometry, which is all it needs.

    Was ``ViewsMixin._people_row``. Its ``server`` parameter was never used
    and is gone.
    """
    # Every credited person, not just Actor/Director/Writer — Producer,
    # GuestStar and Composer were silently dropped. Copied, not
    # mutated: these DTOs are shared with whatever else holds the item.
    # Role, then Type. A crew member has no Role — their job IS the Type
    # (Director, Writer, Producer) — so `Role or ""` captioned every one
    # of them blank. Type is read before it is overwritten below.
    cast = [dict(p, Type="Person",
                 _subtitle=(p.get("Role") or p.get("Type") or ""))
            for p in people][:24]
    if not cast:
        return None
    # Portrait, not square: Jellyfin serves person Primary images at
    # 2:3 like every other poster, so a square tile letterboxed or
    # cropped every face. geom_square is for album art.
    return tiles.tile_row(_("Cast & Crew"), cast, "detail-people",
                          geom=tiles.art.geom)


def download_button(actions, tiles, item, server, prefix):
    """Download, or Remove when it's already downloaded.

    The button used to always say Download, so pressing it on a complete
    item did nothing visible and there was no way to reclaim the space
    outside Settings -> Downloads.

    Takes the two services it needs rather than a shell: ``actions`` to run
    the effect, ``tiles`` because the downloaded-id sets live with the badge
    that draws from them.

    Was ``ViewsMixin._download_btn``.
    """
    if not tiles.is_downloaded(item):
        if actions.offline:
            # Nothing to fetch from. Tk swapped the button out rather
            # than offering a download with no server behind it.
            return None
        return controls.action_btn(
            "file_download", _("Download"), prefix + "-download",
            lambda: actions.open_download(item))
    return controls.action_btn(
        "delete", _("Remove Download"), prefix + "-undownload",
        lambda: actions.confirm_remove_download(item))


def common_actions(actions, tiles, item, server, prefix):
    """Watched / Favorite / Download — the buttons detail, series, season
    and playlist all carry.

    Was ``ViewsMixin._common_actions``.
    """
    ud = item.get("UserData") or {}
    return [
        controls.action_btn(
            "check", _("Watched"), prefix + "-watched",
            lambda: actions.toggle_watched(item, server),
            on=is_watched(item)),
        controls.action_btn(
            "favorite", _("Favorite"), prefix + "-fav",
            lambda: actions.toggle_favorite(item, server),
            on=bool(ud.get("IsFavorite"))),
        download_button(actions, tiles, item, server, prefix),
    ]
