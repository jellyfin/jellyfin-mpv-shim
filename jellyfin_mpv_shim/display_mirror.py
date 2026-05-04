"""Tkinter-based fullscreen "ready to cast" / item-preview window.

Replaces the previous pywebview + Jinja2 HTML implementation. Same public
surface as the old DisplayMirror (run/stop/display_content/get_webview), so
the rest of the app needs no changes.

Design:
- A single fullscreen Tk root with a Canvas.
- Backdrop image fetched via requests, scaled to cover, then composed with
  a vertical dark gradient via Pillow for text legibility.
- Title / misc info / overview drawn as Canvas text items, positioned by
  bbox(...) walking up from the bottom-left.
- All Tk mutations happen on the main thread via root.after; commands from
  other threads (websocket events, player playback start/stop) are
  marshalled through a queue.Queue.
"""

import datetime
import logging
import math
import queue as queue_mod
import random
import threading
import tkinter as tk
from io import BytesIO
from typing import TYPE_CHECKING, Optional

import requests
from PIL import Image, ImageTk

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

class DisplayMirror:
    def __init__(self):
        self.queue: queue_mod.Queue = queue_mod.Queue()
        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None
        self.screen_w = 0
        self.screen_h = 0
        # Tk PhotoImage is GC'd if not held; keep the most recent reference.
        self._bg_photo: Optional[ImageTk.PhotoImage] = None

    # --- public API matching the previous DisplayMirror -----------------

    def get_webview(self):
        return self  # exposes hide()/show() for player.py

    def hide(self):
        self.queue.put(("hide", None))

    def show(self):
        self.queue.put(("show", None))

    def stop(self):
        self.queue.put(("die", None))

    def display_content(self, client: "client_type", arguments: dict):
        # Called from the websocket thread. Item fetch is synchronous here
        # (matches the prior behaviour); image fetch is deferred onto a
        # worker thread spawned by the Tk loop.
        try:
            item = client.jellyfin.get_item(arguments["Arguments"]["ItemId"])
        except Exception:
            log.warning("Could not fetch item for display.", exc_info=True)
            return
        server = client.config.data["auth.server"]
        self.queue.put(("display", self._build_item_data(item, server)))

    def run(self):
        self.root = tk.Tk()
        self.root.title("Jellyfin MPV Shim Mirror")
        self.root.configure(bg="black")
        self.root.attributes("-fullscreen", True)
        self.root.config(cursor="none")

        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()

        self.canvas = tk.Canvas(
            self.root,
            width=self.screen_w,
            height=self.screen_h,
            bg="black",
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Show idle state on startup. Network call goes on a worker so we
        # don't block mainloop's startup.
        threading.Thread(target=self._show_idle_async, daemon=True).start()

        self.root.after(50, self._poll_queue)
        self.root.mainloop()

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

    def _poll_queue(self):
        try:
            while True:
                cmd, payload = self.queue.get_nowait()
                if cmd == "die":
                    self.root.destroy()
                    return
                if cmd == "hide":
                    self.root.withdraw()
                elif cmd == "show":
                    self.root.deiconify()
                    self.root.attributes("-fullscreen", True)
                elif cmd == "display":
                    threading.Thread(
                        target=self._render_item, args=(payload,), daemon=True
                    ).start()
                elif cmd == "idle":
                    threading.Thread(target=self._show_idle_async, daemon=True).start()
        except queue_mod.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def _show_idle_async(self):
        url = _random_backdrop_url()
        backdrop = _fetch_image(url) if url else None
        data = {
            "title": _("Ready to cast"),
            "overview": _("Select your media in Jellyfin and play it here."),
            "misc": "",
            "rating": "",
        }
        self.root.after(0, lambda: self._render(data, backdrop))

    def _render_item(self, data: dict):
        backdrop = _fetch_image(data.get("backdrop_url"))
        self.root.after(0, lambda: self._render(data, backdrop))

    def _canvas_dimensions(self) -> tuple:
        """Return the actual rendered canvas size, not the (possibly multi-
        monitor) screen size. winfo_screenwidth/height returns the combined
        virtual desktop on multi-monitor and Wayland setups, which causes text
        wrap to misbehave."""
        self.canvas.update_idletasks()
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            # Canvas not realized yet — fall back to the root window, then to
            # the (potentially wrong) screen dims as a last resort.
            w = self.root.winfo_width() or self.screen_w
            h = self.root.winfo_height() or self.screen_h
        return w, h

    def _render(self, data: dict, backdrop: Optional["Image.Image"]):
        if not self.canvas:
            return
        self.canvas.delete("all")
        cw, ch = self._canvas_dimensions()

        if backdrop is not None:
            try:
                bg = _scale_to_cover(backdrop, cw, ch)
                bg = _apply_dark_gradient(bg)
                self._bg_photo = ImageTk.PhotoImage(bg)
                self.canvas.create_image(0, 0, image=self._bg_photo, anchor="nw")
            except Exception:
                log.warning("Failed to render backdrop.", exc_info=True)
                self._bg_photo = None

        self._render_text(data, cw, ch)

    def _render_text(self, data: dict, cw: int, ch: int):
        margin = max(40, cw // 30)
        wrap = cw - 2 * margin

        # Font sizes scale with the rendered canvas height; clamped to
        # sensible bounds for very small/large windows.
        info_size = max(14, min(28, ch // 50))
        body_size = max(18, min(36, ch // 30))

        # Title font scales down for long titles so multi-line episode names
        # don't dominate the screen. Hard-cap absurdly long ones.
        title = (data.get("title") or "")[:200]
        base_title_size = max(36, min(96, ch // 14))
        if len(title) > 60:
            title_size = int(base_title_size * 0.6)
        elif len(title) > 40:
            title_size = int(base_title_size * 0.75)
        else:
            title_size = base_title_size

        # Build bottom-up: place each line at the previous line's top edge.
        anchor_y = ch - margin

        def stack_text(text: str, font, fill: str, *, gap: int = 18) -> int:
            nonlocal anchor_y
            if not text:
                return anchor_y
            text_id = self.canvas.create_text(
                margin,
                anchor_y,
                text=text,
                font=font,
                fill=fill,
                anchor="sw",
                width=wrap,
            )
            bbox = self.canvas.bbox(text_id)
            if bbox:
                anchor_y = bbox[1] - gap
            return anchor_y

        stack_text(data.get("overview", ""), ("Helvetica", body_size), "#dddddd")
        misc_line = "    ".join(
            s for s in (data.get("misc"), data.get("rating")) if s
        )
        stack_text(misc_line, ("Helvetica", info_size), "#bbbbbb")
        stack_text(title, ("Helvetica", title_size, "bold"), "#ffffff", gap=8)


mirror = DisplayMirror()
