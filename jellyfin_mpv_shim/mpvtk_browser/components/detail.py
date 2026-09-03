"""Pieces of an item's detail presentation, shared by more than one family.

``meta_line`` is wanted by detail, series *and* the music header; ``people_row``
by detail and series. That cross-family reach is what makes them components
rather than methods on a page.

See ``docs/archive/ARCHITECTURE_TARGET.md`` §1.4 for the line being drawn.
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
    #
    # Three, as Tk had it and as the Live TV cards do -- this is the quick
    # read, not the full list, and a film tagged with eight of them pushed
    # everything else off the line. The banner ellipsizes past its width
    # anyway, but that backstop should be rare rather than routine: the
    # genres are last, so they are what the ellipsis eats.
    genres = ", ".join((item.get("Genres") or [])[:3])
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


def download_button(actions, tiles, item, server, prefix,
                    size=controls.ROW):
    """Download, or Remove when it's already downloaded.

    ``size`` because this is the one button in the set that a caller
    appends to a row it built itself, rather than getting from
    ``common_actions``. The AudioBook page builds a row of 18s and got a
    16 on the end -- 42.5px against 40.0, the same defect the grid bar was
    levelled for, twice the size.

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
            lambda: actions.open_download(item), size=size)
    return controls.action_btn(
        "delete", _("Remove Download"), prefix + "-undownload",
        lambda: actions.confirm_remove_download(item), size=size)


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


#: Most links we will draw for one item. Anime in particular is tagged by
#: half a dozen databases at once (AniDB, AniList, MyAnimeList, Kitsu, TVDB,
#: TMDB, IMDb, Trakt...), and past a couple of rows this stops being a
#: reference and starts being the page. First wins, which is the server's
#: own order -- ``ExternalUrls`` comes out in provider-priority order, so
#: the ones it drops are the ones that library cares least about.
MAX_PROVIDER_LINKS = 8


#: Hosts whose links go nowhere, matched on the host and any subdomain of
#: it. Jellyfin still ships a Zap2It external id and still composes
#: ``https://tvlistings.zap2it.com/overview.html?programSeriesId=...`` for
#: anything carrying one -- but that listings site was retired, so the
#: button is an offer to leave the app for a page that does not exist. The
#: whole domain rather than the one host: nothing under it serves listings
#: any more, and the id we hold is only meaningful to that service.
#:
#: A *display* rule, not a safety one -- ``system_open.URL_SCHEMES`` is the
#: safety boundary and this is not a second one. It says "do not offer
#: this", and the honest place for that is next to the code that decides
#: what to draw.
DEAD_PROVIDER_HOSTS = ("zap2it.com",)


def _is_dead(url):
    """True for a link we will not offer. Host-only, so a query string
    naming one of these does not take an unrelated provider down with it."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        # Unparseable is its own kind of dead.
        return True
    return any(host == dead or host.endswith("." + dead)
               for dead in DEAD_PROVIDER_HOSTS)


def jellyfin_web_url(address, item):
    """The item's page in jellyfin-web, or None when it cannot be composed.

    ``#/details?id=…&serverId=…`` — read out of jellyfin-web's own
    ``appRouter.getRouteUrl`` and checked on both supported servers (12.0 in
    the bundle it serves, 10.11 in ``release-10.11.z``); the ``#!`` spelling
    is 10.8 and older. ``serverId`` comes from the DTO rather than from the
    uuid the browser keys servers by: those are the *server's* id only for a
    login that asked for it, and a random uuid4 otherwise (``clients.py``).
    It is left off when the DTO has none, which web reads as "the server you
    are signed in to" — the right answer for the only source that omits it.
    """
    if not address or not (item or {}).get("Id"):
        return None
    url = "%s/web/#/details?id=%s" % (address.rstrip("/"), item["Id"])
    server_id = item.get("ServerId")
    if server_id:
        url += "&serverId=%s" % server_id
    return url


def provider_link_buttons(item, on_open, web_url=None):
    """The provider links as a plain list of buttons, or ``[]``.

    Split from :func:`provider_links` because the season screen does not
    want a row of its own: it has a header row already (the title, the
    season picker, To Series) and these belong on the end of it, which
    means it needs the buttons rather than something already packed.

    ``ExternalUrls`` needs no ``Fields`` and costs no request: the server
    fills it in unconditionally on the single-item routes, which is what the
    detail page already fetches through (measured on 10.11 and 12.0, both
    ``/Users/{uid}/Items/{id}`` and ``/Items/{id}``). It is absent from list
    queries unless asked for, which is why this belongs to detail screens
    and not to a tile.

    Buttons rather than jellyfin-web's bare coloured text: the one thing the
    user has to know before pressing is that it *leaves the application*,
    and on a ten-foot UI a differently-coloured word does not say that. The
    ``open_in_new`` glyph is the same one web puts on its own external links.

    ``on_open`` takes the url. It is passed in rather than reached for
    because opening one is the shell stepping outside the process, which is
    the gateway's job and not a component's.

    ``web_url`` is the item's own page on the server it came from
    (:func:`jellyfin_web_url`), and leads rather than joining the tail: it is
    the one link here that is about *this* library rather than about the
    title in general, and it is the one somebody presses to carry on with the
    item somewhere else. Its node id is deliberately outside the
    ``detail-link-N`` series — those are indexed by provider, and shifting
    them by one would make every existing id name a different database.
    """
    seen, links = set(), []
    for entry in item.get("ExternalUrls") or ():
        if not isinstance(entry, dict):
            continue
        url, name = entry.get("Url"), entry.get("Name")
        # Both, and not just the url: a nameless link is a button captioned
        # with nothing, and the name is the only thing that says where it
        # goes. Deduped on the url because a server with two plugins for one
        # database answers with the same link twice.
        if not url or not name or url in seen or _is_dead(url):
            continue
        seen.add(url)
        links.append((str(name), str(url)))
        if len(links) >= MAX_PROVIDER_LINKS:
            break
    buttons = []
    if web_url:
        # One word, like the database names beside it -- those come off the
        # server untranslated because they are proper nouns, and a verb
        # phrase here ("Open in Jellyfin") would read as a different KIND of
        # control from its neighbours [iw]. This one is a word rather than a
        # name, so it does go through gettext.
        buttons.append(controls.action_btn(
            "open_in_new", _("Web"), "detail-web-link",
            lambda u=web_url: on_open(u), size=controls.ROW))
    buttons += [controls.action_btn(
        "open_in_new", name, "detail-link-%d" % i,
        # url bound as a default argument, not closed over: the loop
        # variable would otherwise be whatever it ended on, and every
        # button on the row would open the last provider.
        lambda u=url: on_open(u), size=controls.ROW)
        for i, (name, url) in enumerate(links)]
    return buttons


def provider_links(item, avail, on_open, web_url=None):
    """The provider links as a row of their own, or None when there are no
    links to draw. What the detail and series screens want, both of which
    place this under the synopsis with nothing else beside it."""
    from . import chrome

    buttons = provider_link_buttons(item, on_open, web_url=web_url)
    if not buttons:
        return None
    return chrome.wrap_row(buttons, avail, gap=8, align="center")
