"""The cast screen — "Ready to cast" and the remote-selected item preview.

This was ``display_mirror.py``, a whole second UI that attached its OWN
MpvtkApp to the player's mpv window and ran its own loop. Two owners of one
window cannot coexist, which is why ``display_mirroring`` had to fall back
to the Tk browser and why it was the last thing keeping Tk alive.

It is a route now. The compositing and metadata formatting are unchanged —
that is most of this file, and it is why the screen still looks the same —
but the lifecycle is gone: ``run``/``stop``/``get_webview``/``hide``/``show``
and a hand-rolled ``_set_active`` that was a worse copy of the browser's
``_yield()``/``enter_browse()``.

Backdrop + gradient + text are baked into ONE full-window bitmap. That is not
stylistic: mpv composites overlay bitmaps ABOVE all script ASS (see mpvtk
GUIDE §6), so text drawn as an overlay node would be hidden behind the
backdrop. Bake it in, or it does not render.

``headless`` (conf.py) makes this the only page: see MpvtkBrowser.headless
and CastMixin.navigate's refusal. The screen itself is the same either way —
without the flag it is simply what a DisplayContent from a phone shows.
"""

import datetime
import hashlib
import logging
import math
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ..clients import clientManager
from ..i18n import _
from ..imageutil import apply_dark_gradient, pil_font, scale_to_cover

# PIL and requests are imported inside the functions that need them, as
# strips.py does. Nothing here runs before mpv_shim has probed Pillow, but
# keeping `import mpvtk_browser.app` free of optional heavyweight imports is
# the pattern this package follows and the one tests/test_imageutil.py
# guards.

log = logging.getLogger("mpvtk_browser.cast")

def _server_url(server: str, path: str) -> str:
    return f"{server.rstrip('/')}/{path.lstrip('/')}"


def _backdrop_url(item: dict, server: str) -> Optional[str]:
    if item.get("BackdropImageTags"):
        return _server_url(
            server,
            f"Items/{item['Id']}/Images/Backdrop/0?tag={item['BackdropImageTags'][0]}",
        )
    if item.get("ParentBackdropItemId"):
        return _server_url(
            server,
            f"Items/{item['ParentBackdropItemId']}/Images/Backdrop/0"
            f"?tag={item['ParentBackdropImageTags'][0]}",
        )
    return None


def _logo_url(item: dict, server: str) -> Optional[str]:
    if item.get("ImageTags", {}).get("Logo"):
        return _server_url(
            server, f"Items/{item['Id']}/Images/Logo/0?tag={item['ImageTags']['Logo']}"
        )
    if item.get("ParentLogoItemId") and item.get("ParentLogoImageTag"):
        return _server_url(
            server,
            f"Items/{item['ParentLogoItemId']}/Images/Logo/0"
            f"?tag={item['ParentLogoImageTag']}",
        )
    return None


def _display_name(item: dict) -> str:
    name = item.get("EpisodeTitle") or item.get("Name", "")
    if item.get("Type") == "TvChannel":
        if item.get("Number"):
            return f"{item['Number']} {name}"
        return name
    if (
        item.get("Type") == "Episode"
        and item.get("IndexNumber") is not None
        and item.get("ParentIndexNumber") is not None
    ):
        number = f"S{item['ParentIndexNumber']} E{item['IndexNumber']}"
        if item.get("IndexNumberEnd"):
            number += f"-{item['IndexNumberEnd']}"
        name = f"{number} - {name}"
    return name


def _parse_jf_datetime(s: str) -> datetime.datetime:
    return datetime.datetime.strptime(s.partition(".")[0], "%Y-%m-%dT%H:%M:%S")


def _misc_info(item: dict) -> str:
    parts = []
    typ = item.get("Type")

    if typ == "Episode" and item.get("PremiereDate"):
        parts.append(_parse_jf_datetime(item["PremiereDate"]).strftime("%x"))
    if item.get("StartDate"):
        parts.append(_parse_jf_datetime(item["StartDate"]).strftime("%x"))
    if typ == "Series" and item.get("ProductionYear"):
        if item.get("Status") == "Continuing":
            parts.append(f"{item['ProductionYear']}-Present")
        else:
            text = str(item["ProductionYear"])
            if item.get("EndDate"):
                end_year = _parse_jf_datetime(item["EndDate"]).year
                if end_year != item["ProductionYear"]:
                    text += f"-{end_year}"
            parts.append(text)
    if typ not in ("Series", "Episode"):
        if item.get("ProductionYear"):
            parts.append(str(item["ProductionYear"]))
        elif item.get("PremiereDate"):
            parts.append(str(_parse_jf_datetime(item["PremiereDate"]).year))
    if item.get("RunTimeTicks") and typ not in ("Series", "Audio"):
        minutes = math.ceil(item["RunTimeTicks"] / 600000000)
        parts.append(f"{minutes}min")
    if item.get("OfficialRating") and typ not in ("Season", "Episode"):
        parts.append(item["OfficialRating"])
    if item.get("Video3DFormat"):
        parts.append("3D")
    return "    ".join(parts)


def _rating(item: dict) -> str:
    rating = item.get("CommunityRating")
    if rating:
        return f"★ {rating:.1f}"
    return ""


def _random_backdrop_url() -> Optional[str]:
    if not clientManager.clients:
        return None
    try:
        client = random.choice(list(clientManager.clients.values()))
        params = {
            "SortBy": "Random",
            "Limit": 1,
            "IncludeItemTypes": "Movie,Series",
            "ImageTypes": "Backdrop",
            "Recursive": True,
            "MaxOfficialRating": "PG-13",
        }
        items = client.jellyfin.user_items(params=params).get("Items") or []
        if not items:
            return None
        return _backdrop_url(items[0], client.config.data["auth.server"])
    except Exception:
        log.warning("Could not fetch random backdrop.", exc_info=True)
        return None


# ---- Image processing ---------------------------------------------------

def _fetch_image(url: Optional[str], timeout: int = 10):
    if not url:
        return None
    import requests
    from PIL import Image

    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception:
        log.warning("Failed to fetch image %s", url, exc_info=True)
        return None


# ---- The window ---------------------------------------------------------

def _wrap(draw, text, font, max_w):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = (cur + " " + word).strip()
            if not cur or draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


class CastMixin:
    """The cast/idle screen as a route.

    State on ``self``: ``_cast`` (the item data being shown), ``_cast_entry``
    (the baked bitmap), ``_cast_backdrop`` (decoded, so a resize
    re-composites without re-downloading — and without re-rolling the random
    idle backdrop mid-drag), and ``_cast_lock``.

    Compositing runs on the shared pool and writes then ``invalidate()``s,
    like every other foreign-thread producer in this package.
    """

    ROUTES = {"cast": (None, "_render_cast")}

    def show_cast(self, reset=True):
        """Make the cast screen the current page."""
        self.navigate({"kind": "cast"}, reset=reset, force=True)

    def _render_cast(self, route, size):
        from ..mpvtk.widgets import Column, Image as ImageNode
        from .music import NOW_PLAYING_BAR_H

        # Full-bleed, so unlike every other view this sizes itself rather
        # than flexing — which means it has to account for the now-playing
        # bar itself. Claiming the whole window laid the bar out BELOW the
        # screen: casting music to a headless box showed no transport at
        # all, on a page that is the only page there is.
        w, h = size
        if self._now_playing is not None:
            h = max(1, h - NOW_PLAYING_BAR_H)
        size = (w, h)

        if self._cast_size != size:
            self._cast_size = size
            self._recomposite_cast()
        entry = self._cast_entry
        if entry is None:
            # Nothing baked yet: a plain dark field rather than a flash of
            # whatever was behind us.
            return Column([], w=size[0], h=size[1])
        return ImageNode(entry["src"], entry["iw"], entry["ih"],
                         w=size[0], h=size[1])

    def display_cast_item(self, server_uuid, item_id):
        """A remote picked something: show it.

        Called from the websocket thread, so the client lookup and the fetch
        both happen on the pool — the lookup is cheap but it is not this
        thread's business, and doing it here means one place handles "that
        server is gone" rather than two."""
        ep = self._epoch

        def work():
            from ..clients import clientManager
            client = clientManager.clients.get(server_uuid)
            if client is None:
                raise LookupError("no client for server %r" % server_uuid)
            item = client.jellyfin.get_item(item_id)
            server = client.config.data["auth.server"]
            return self._build_item_data(item, server)

        def done(data):
            self._set_cast_data(data)

        def failed(_exc):
            log.warning("could not fetch the item to display", exc_info=True)

        # Not epoch-gated in effect: the cast screen is the only page in
        # headless, so there is nothing to navigate away to.
        self.run_async(work, done, ep, on_error=failed)

    def show_cast_idle(self):
        self._set_cast_data({"idle": True})

    def _set_cast_data(self, data):
        self._cast = data
        # New item -> new backdrop. Dropped here and only here, so a resize
        # re-composites from memory and the idle screen's random backdrop
        # stays put.
        with self._cast_lock:
            self._cast_backdrop = None
            self._cast_backdrop_key = None
        self._recomposite_cast()

    def _recomposite_cast(self):
        if self._cast_size is None or self.strips is None:
            return
        data, size = self._cast, self._cast_size
        self._pool.submit(lambda: self._composite(data, size))

    @staticmethod
    def _build_item_data(item: dict, server: str) -> dict:
        return {
            "title": _display_name(item),
            "overview": item.get("Overview", "") or "",
            "misc": _misc_info(item),
            "rating": _rating(item),
            "backdrop_url": _backdrop_url(item, server),
            "logo_url": _logo_url(item, server),
        }

    def _composite(self, data, size):
        from PIL import Image

        try:
            w, h = size
            if data.get("idle"):
                title = _("Ready to cast")
                overview = _("Select your media in Jellyfin and play it here.")
                misc = rating = ""
                # Only roll the random backdrop when we don't already have one
                # — otherwise it changes on every resize tick (and queries the
                # server to do it).
                with self._cast_lock:
                    resolved = self._cast_backdrop_key is not None
                url = None if resolved else _random_backdrop_url()
            else:
                title = data.get("title") or ""
                overview = data.get("overview") or ""
                misc = data.get("misc") or ""
                rating = data.get("rating") or ""
                url = data.get("backdrop_url")

            backdrop = self._get_backdrop(url)
            if backdrop is not None:
                canvas = apply_dark_gradient(scale_to_cover(backdrop, w, h))
            else:
                canvas = Image.new("RGBA", (w, h), (18, 18, 20, 255))

            self._draw_text(canvas, title, misc, rating, overview, w, h)
            # Content-keyed, not a monotonic counter: a counter is a
            # guaranteed cache miss, so every resize tick retained another
            # full-window BGRA buffer (~8 MB at 1080p) until the LRU capped
            # at 48 of them.
            key = "mirror-%dx%d-%s" % (
                w, h,
                hashlib.sha1(
                    "\x00".join((title, misc, rating, overview,
                                 self._cast_backdrop_key or "")).encode("utf-8")
                ).hexdigest())
            entry = self.strips.bitmap(key, canvas)
            with self._cast_lock:
                self._cast_entry = entry
            self.invalidate()
        except Exception:
            log.warning("Display mirror composite failed.", exc_info=True)

    def _get_backdrop(self, url):
        """Decoded backdrop for the current data, fetched at most once.

        The idle screen picks a *random* backdrop, so re-resolving it per
        composite made the picture change while the user dragged the window;
        item screens just re-downloaded the same image."""
        with self._cast_lock:
            if self._cast_backdrop_key is not None:
                return self._cast_backdrop
        if not url:
            with self._cast_lock:
                self._cast_backdrop_key = ""
                self._cast_backdrop = None
            return None
        image = _fetch_image(url)
        with self._cast_lock:
            self._cast_backdrop = image
            self._cast_backdrop_key = url
        return image

    def _draw_text(self, canvas, title, misc, rating, overview, cw, ch):
        from PIL import ImageDraw

        draw = ImageDraw.Draw(canvas)
        margin = max(40, cw // 30)
        wrap = cw - 2 * margin
        info_size = max(14, min(28, ch // 50))
        body_size = max(18, min(36, ch // 30))
        title = (title or "")[:200]
        base = max(36, min(96, ch // 14))
        title_size = int(base * (0.6 if len(title) > 60
                                 else 0.75 if len(title) > 40 else 1.0))

        # Bottom-up: overview, then misc·rating, then the title.
        y = ch - margin

        def stack(text, font, fill, gap=18):
            nonlocal y
            if not text:
                return
            lines = _wrap(draw, text, font, wrap)
            for line in reversed(lines):
                asc, desc = font.getmetrics()
                lh = asc + desc
                draw.text((margin, y - lh), line, font=font, fill=fill)
                y -= lh + 4
            y -= gap - 4

        info = "    ".join(s for s in (misc, rating) if s)
        stack(overview, pil_font(body_size, text=overview), (221, 221, 221))
        stack(info, pil_font(info_size, text=info), (187, 187, 187))
        stack(title, pil_font(title_size, bold=True, text=title),
              (255, 255, 255), gap=8)
