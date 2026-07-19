"""In-mpv-window "ready to cast" / item-preview mirror (mpvtk).

Renders inside the player's own mpv window via mpvtk, replacing the earlier
Tk+Pillow fullscreen window (which itself replaced a pywebview+Jinja2 one).
Same public surface as before (run/stop/display_content/get_webview), so the
rest of the app is unchanged.

Design:
- Attach mpvtk to playerManager's mpv (no separate window/process).
- Backdrop fetched via requests, scaled to cover, composited with a vertical
  dark gradient, then the title / misc / overview text is baked into the SAME
  bitmap with Pillow (bitmaps composite above ASS, so text must be baked, not
  drawn as an overlay — see mpvtk GUIDE §6). The whole thing is one
  full-window Image node.
- ``display_content`` (websocket thread) and playback hide/show marshal onto
  the mpvtk loop via ``invalidate()``; compositing runs on a worker pool.
"""

import datetime
import logging
import math
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import TYPE_CHECKING, Optional

import requests
from PIL import Image

from .clients import clientManager
from .i18n import _

log = logging.getLogger("display_mirror")

if TYPE_CHECKING:
    from jellyfin_apiclient_python import client as client_type


# ---- URL builders (ported from the old display_mirror.helpers) ---------

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

def _fetch_image(url: Optional[str], timeout: int = 10) -> Optional["Image.Image"]:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception:
        log.warning("Failed to fetch image %s", url, exc_info=True)
        return None


def _scale_to_cover(image: "Image.Image", w: int, h: int) -> "Image.Image":
    """Scale `image` to fully cover (w, h), center-cropping the overflow."""
    iw, ih = image.size
    scale = max(w / iw, h / ih)
    new_w, new_h = max(1, int(iw * scale)), max(1, int(ih * scale))
    image = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return image.crop((left, top, left + w, top + h))


def _apply_dark_gradient(
    image: "Image.Image", height_fraction: float = 0.55, max_alpha: int = 200
) -> "Image.Image":
    """Composite a vertical transparent->dark gradient over the image's bottom."""
    w, h = image.size
    grad_h = max(1, int(h * height_fraction))
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    # Build the gradient as a single column then resize horizontally — much
    # faster than per-row paste for large images.
    column = Image.new("RGBA", (1, grad_h))
    for y in range(grad_h):
        alpha = int(max_alpha * (y / max(1, grad_h - 1)) ** 1.5)
        column.putpixel((0, y), (0, 0, 0, alpha))
    column = column.resize((w, grad_h), Image.NEAREST)
    overlay.paste(column, (0, h - grad_h))
    return Image.alpha_composite(image, overlay)


# ---- The window ---------------------------------------------------------

def _pil_font(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


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


class DisplayMirror:
    def __init__(self):
        self._app = None
        self._store = None
        self._size = None
        self._data = {"idle": True}
        self._entry = None       # baked {"src","iw","ih"} for the current data
        self._version = 0
        self._visible = True
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2,
                                        thread_name_prefix="mirror")
        self.stop_callback = None

    # --- public API matching the previous DisplayMirror -----------------

    def get_webview(self):
        return self  # exposes hide()/show() for player.py

    def hide(self):
        # Playback started: yield the window to the video.
        self._visible = False
        self._invalidate()

    def show(self):
        self._visible = True
        self._invalidate()

    def stop(self):
        if self._app is not None:
            self._app.quit()

    def display_content(self, client: "client_type", arguments: dict):
        # Websocket thread: fetch the item, then recomposite on a worker.
        try:
            item = client.jellyfin.get_item(arguments["Arguments"]["ItemId"])
        except Exception:
            log.warning("Could not fetch item for display.", exc_info=True)
            return
        server = client.config.data["auth.server"]
        self._set_data(self._build_item_data(item, server))

    def run(self):
        from .player import playerManager, is_using_ext_mpv
        from .mpvtk.app import MpvtkApp
        from .mpvtk.rawimage import MemoryStore, cache_dir
        from .mpvtk_browser.strips import StripStore

        self._app = MpvtkApp.attach(playerManager.get_mpv(),
                                    ext=is_using_ext_mpv)
        self._store = StripStore(
            mem_store=MemoryStore() if self._app.in_process else None,
            cache_dir=None if self._app.in_process
            else cache_dir("mpvtk-mirror-"))
        playerManager.mpvtk_active = True
        playerManager.set_browse_window(True)
        self._set_data({"idle": True})   # "Ready to cast" on startup
        try:
            self._app.run(self._build)   # blocks: this is the main loop
        finally:
            playerManager.mpvtk_active = False
            if self.stop_callback is not None:
                self.stop_callback()

    # --- internals ------------------------------------------------------

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

    def _build(self, size):
        from .mpvtk.widgets import Column, Image as ImageNode

        if self._size != size:
            self._size = size
            self._recomposite()   # window size changed -> rebuild the bitmap
        e = self._entry
        if not self._visible or e is None:
            return Column([], w=size[0], h=size[1])
        return ImageNode(e["src"], e["iw"], e["ih"], w=size[0], h=size[1])

    def _set_data(self, data):
        self._data = data
        self._recomposite()

    def _recomposite(self):
        if self._size is None or self._store is None:
            return
        data, size = self._data, self._size
        self._pool.submit(lambda: self._composite(data, size))

    def _composite(self, data, size):
        try:
            w, h = size
            if data.get("idle"):
                title = _("Ready to cast")
                overview = _("Select your media in Jellyfin and play it here.")
                misc = rating = ""
                url = _random_backdrop_url()
            else:
                title = data.get("title") or ""
                overview = data.get("overview") or ""
                misc = data.get("misc") or ""
                rating = data.get("rating") or ""
                url = data.get("backdrop_url")

            backdrop = _fetch_image(url) if url else None
            if backdrop is not None:
                canvas = _apply_dark_gradient(_scale_to_cover(backdrop, w, h))
            else:
                canvas = Image.new("RGBA", (w, h), (18, 18, 20, 255))

            self._draw_text(canvas, title, misc, rating, overview, w, h)
            self._version += 1
            entry = self._store.bitmap("mirror%d" % self._version, canvas)
            with self._lock:
                self._entry = entry
            self._invalidate()
        except Exception:
            log.warning("Display mirror composite failed.", exc_info=True)

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

        stack(overview, _pil_font(body_size), (221, 221, 221))
        stack("    ".join(s for s in (misc, rating) if s),
              _pil_font(info_size), (187, 187, 187))
        stack(title, _pil_font(title_size, bold=True), (255, 255, 255), gap=8)

    def _invalidate(self):
        if self._app is not None:
            self._app.invalidate()


mirror = DisplayMirror()
