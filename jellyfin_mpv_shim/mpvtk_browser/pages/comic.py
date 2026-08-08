"""Reading a comic in the player's window.

The page itself is not drawn here at all: it is handed to mpv as a file to
display, and mpv's ``video-zoom`` / ``video-pan-x`` / ``video-pan-y`` are
the zoom and the scroll. What this module draws is two bars over the top of
it, which is the same thing the playback HUD does over video.

**That makes this a third window state** — a picture in the window with the
library's own chrome on top — and ``gateway/picture.py`` is where the case
for it is argued. The short version: ``PlayerManager`` keys its queue,
reporting and idle-quit off ``self._video``, which stays None here, so
nothing has to be told that the window is busy.

**The page size is read from the file's header, not from mpv.** Pillow
parses a JPEG header in well under a millisecond without decoding a pixel,
and knowing the size *before* the load is what lets the zoom be set in the
same breath as the picture. Asking mpv means asking repeatedly until it has
decoded, which is a poll loop in the render path for a number that was
sitting in the first two hundred bytes of the file.
"""

import logging

from ...books import page_count as stored_page_count, progress_of, \
    ticks_for_page
from ...comic import ComicArchive, ComicError
from ...i18n import _
from ...mpvtk.widgets import Button, Column, Dropdown, Row, Spacer, Text, \
    TextBox
from .. import theme
from ..components import chrome
from ..gateway.picture import fit_scale, fit_zoom, pan_bounds
from .base import Page

log = logging.getLogger("mpvtk_browser.pages.comic")

#: Height of the bars above and below the page.
TOP_BAR_H = 44
BOTTOM_BAR_H = 48

#: Zoom steps the +/- buttons walk, as multiples of the fit. A list rather
#: than a factor so every stop is a round number on the bar.
ZOOM_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)

#: How far one wheel notch moves the page, in physical pixels.
WHEEL_STEP = 120

#: Reading modes. "width" is the comic default — the page as wide as the
#: window, scrolled down — and "page" shows a whole page at once.
FIT_WIDTH, FIT_PAGE = "width", "page"


class ComicPage(Page):
    """One comic, one page at a time, shown through mpv."""

    kind = "comic"

    #: Keys this page takes over (see ``MpvtkApp.claim_keys``). The same
    #: set the epub reader claims and for the same reason — on a picture
    #: with no widgets in it, LEFT and RIGHT mean "turn".
    claimed_keys = ("LEFT", "RIGHT", "PGUP", "PGDWN", "SPACE", "HOME", "END")

    # -- lifecycle ---------------------------------------------------------

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        item_id = route["item_id"]

        def work():
            item = source.get_item(srv, item_id)
            state = (None, None)
            try:
                state = self.ctx.player.book_download_state(item_id)
            except Exception:
                log.debug("book download state unavailable", exc_info=True)
            return {"item": item, "state": state}

        self.route_async(work, self._opened, epoch)

    def _opened(self, data):
        self.route["_data"] = data
        status, path = data.get("state") or (None, None)
        if path:
            self._open_comic(path)
        elif status not in ("pending", "downloading"):
            self._fetch()

    def refresh_download_state(self):
        """The catalog changed — the shell's downloads hook.

        Same job as the epub reader's: this screen is *waiting* on a
        download rather than merely reporting one, so it opens the comic
        the moment the file lands.
        """
        data = self.route.get("_data")
        if not data or self.route.get("_comic") is not None:
            return
        item = data.get("item") or {}
        if not item.get("Id"):
            return
        try:
            data["state"] = self.ctx.player.book_download_state(item["Id"])
        except Exception:
            log.debug("book download state unavailable", exc_info=True)
            return
        _status, path = data["state"]
        if path:
            self._open_comic(path)

    def _fetch(self):
        data = self.route.get("_data") or {}
        item = data.get("item") or {}
        server = self.route.get("server") or self.ctx.server
        self.ctx.actions.download_book(item, server)

    def _open_comic(self, path):
        route = self.route
        if route.get("_opening"):
            return
        route["_opening"] = True

        def work():
            return ComicArchive(path)

        def done(archive):
            route["_comic"] = archive
            route["_opening"] = False
            if "_page" not in route:
                route["_page"] = self._resume_page(archive)
            self._show_page(route["_page"])
            self.ctx.invalidate()

        def failed(exc):
            route["_opening"] = False
            route["_error"] = str(exc) or _("This comic could not be opened.")
            self.ctx.invalidate()

        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed)

    def close(self):
        """Leave the window as we found it.

        Called by the shell when this route stops being the screen. Both
        halves matter: the picture is mpv's, so nothing else will take it
        down and the comic would sit behind the library grid; and the
        extracted pages are files, which nothing else will delete.
        """
        archive = self.route.pop("_comic", None)
        if archive is not None:
            try:
                archive.close()
            except Exception:
                log.debug("closing the comic failed", exc_info=True)
        try:
            self.ctx.player.clear_picture()
        except Exception:
            log.debug("clearing the picture failed", exc_info=True)

    # -- pages -------------------------------------------------------------

    @property
    def archive(self):
        return self.route.get("_comic")

    def page_index(self):
        return int(self.route.get("_page", 0))

    def page_count(self):
        archive = self.archive
        return archive.page_count if archive is not None else 0

    def _show_page(self, index, to_bottom=False):
        """Put page ``index`` in the window and place it.

        Extraction and the header read happen on the pool; only the two
        gateway calls come back to the loop thread, and they are
        fire-and-forget.
        """
        archive = self.archive
        if archive is None:
            return
        index = max(0, min(index, archive.page_count - 1))
        self.route["_page"] = index
        self._save_position(index)
        route = self.route

        def work():
            path = archive.page_path(index)
            return path, _picture_size(path)

        def done(result):
            path, size = result
            if route.get("_comic") is not archive:
                return              # navigated away while extracting
            route["_size"] = size
            self.ctx.player.show_picture(path)
            self._place(to_bottom=to_bottom)
            self.ctx.invalidate()

        def failed(exc):
            route["_error"] = str(exc) or _("This page could not be read.")
            self.ctx.invalidate()

        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed)

    def _resume_page(self, archive):
        """Where the server says this reader had got to.

        A comic's position is a **page index** — ``books.progress_of``
        returns it 1-based — and unlike an epub's it is a number every
        other client shows you, which is why the Progress dialog offers to
        set it. Clamped to what is actually in the archive: the count the
        server probed is its own count of the entries, and a file added or
        removed since would otherwise resume past the end.
        """
        data = self.route.get("_data") or {}
        item = data.get("item") or {}
        mode, page, _total = progress_of(item)
        if mode != "pages" or not page:
            return 0
        return max(0, min(page - 1, archive.page_count - 1))

    def _save_position(self, index):
        """Report the page back, so every other client resumes here too.

        Fire and forget, like the epub reader's: a failed write costs the
        position elsewhere, and blocking a page turn on a round trip would
        cost the reading. The DTO in hand is updated as well, or going back
        to the book's own page shows the figure it was loaded with.
        """
        data = self.route.get("_data") or {}
        item = data.get("item") or {}
        server = self.route.get("server") or self.ctx.server
        if not item.get("Id") or server is None:
            return
        ticks = ticks_for_page(index + 1)
        if self.route.get("_saved") == ticks:
            return
        self.route["_saved"] = ticks
        item.setdefault("UserData", {})["PlaybackPositionTicks"] = ticks
        setter = getattr(self.ctx.player, "set_position", None)
        if setter is None:
            return
        self.ctx.run.submit(lambda: setter(server, item["Id"], ticks))

    # -- gestures ----------------------------------------------------------

    def on_picture_gesture(self, kind, evt):
        """A wheel notch off the end of the page, or ctrl+wheel.

        Everything *inside* a page — the drag, the wheel that still has
        somewhere to go — is the renderer's and never reaches here. What
        arrives is only the two answers it cannot give: which page is next,
        and what a zoom step means.
        """
        if kind == "vzoom":
            self._step_zoom(1 if (evt.get("dir") or 0) > 0 else -1)
        elif kind == "vpan":
            self._turn(1 if evt.get("edge") == "bottom" else -1)

    def _turn(self, delta):
        archive = self.archive
        if archive is None:
            return
        index = self.page_index() + delta
        if not 0 <= index < archive.page_count:
            return
        # Entering a page backwards lands at its *bottom*: paging back and
        # arriving at the top means scrolling down to see what you went
        # back for, on every page you walk back through.
        self._show_page(index, to_bottom=delta < 0)
        self.ctx.invalidate()

    def _goto(self, index):
        if self.archive is None:
            return
        self._show_page(index)
        self.ctx.invalidate()

    # -- placement ---------------------------------------------------------

    def mode(self):
        return self.route.get("_mode") or FIT_WIDTH

    def zoom(self):
        try:
            return float(self.route.get("_zoom", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def _place(self, to_bottom=False):
        """Set mpv's three view properties for the current page.

        All the arithmetic is in ``gateway/picture.py`` so it can be
        tested without a window; this is the part that knows the numbers
        to feed it — the picture's size, the window's, and how much of the
        window the bars have taken.
        """
        size = self.route.get("_size")
        window = self.route.get("_window")
        if not size or not window:
            return
        top, area = self._reading_area(window)
        zoom = fit_zoom(size, window, area, self.mode(), self.zoom())
        min_x, max_x, min_y, max_y = pan_bounds(size, window, area, top, zoom)
        # max_y is the TOP of the page: pan moves the picture, so pushing
        # it down is what brings its top into view. See pan_bounds.
        pan_y = min_y if to_bottom else max_y
        self.ctx.player.set_picture_view(zoom=zoom, pan_x=0.0, pan_y=pan_y)
        self._push_pan(size, window, zoom,
                       (min_x, max_x, min_y, max_y))

    def _push_pan(self, size, window, zoom, bounds):
        """Hand the renderer the gesture model for this page.

        Everything continuous — the drag, the wheel — happens over there
        against mpv's own properties, so this is the whole of the
        conversation: the clamp, and the pixel size the pan unit is
        measured in. Re-sent on every zoom, page and resize, because all
        three change it.
        """
        setter = getattr(self.ctx.art, "set_picture_pan", None)
        if setter is None:
            return
        scale = fit_scale(size, window) * (2.0 ** zoom)
        min_x, max_x, min_y, max_y = bounds
        try:
            setter({"unitx": max(1.0, size[0] * scale),
                    "unity": max(1.0, size[1] * scale),
                    "minx": min_x, "maxx": max_x,
                    "miny": min_y, "maxy": max_y,
                    # One notch of the wheel, in physical pixels of the
                    # window. Three lines of a comic page is meaningless;
                    # a fixed distance is what every viewer does.
                    "step": WHEEL_STEP})
        except Exception:
            log.debug("could not push the pan model", exc_info=True)

    @staticmethod
    def _reading_area(window):
        """``(top, height)`` of the part of the window between the bars,
        in the same physical pixels mpv measures in."""
        from ...mpvtk.scaling import px

        top = px(TOP_BAR_H)
        return top, max(1, window[1] - top - px(BOTTOM_BAR_H))

    def _set_mode(self, mode):
        self.route["_mode"] = mode
        self.route["_zoom"] = 1.0
        self._place()
        self.ctx.invalidate()

    def _step_zoom(self, delta):
        current = self.zoom()
        if delta > 0:
            larger = [z for z in ZOOM_STEPS if z > current + 1e-6]
            self.route["_zoom"] = larger[0] if larger else ZOOM_STEPS[-1]
        else:
            smaller = [z for z in ZOOM_STEPS if z < current - 1e-6]
            self.route["_zoom"] = smaller[-1] if smaller else ZOOM_STEPS[0]
        self._place()
        self.ctx.invalidate()

    # -- input -------------------------------------------------------------

    def on_key(self, key):
        if key in ("RIGHT", "PGDWN", "SPACE"):
            self._turn(1)
        elif key in ("LEFT", "PGUP"):
            self._turn(-1)
        elif key == "HOME":
            self._goto(0)
        elif key == "END":
            self._goto(max(0, self.page_count() - 1))

    def _jump(self, text):
        try:
            number = int(str(text).strip())
        except (TypeError, ValueError):
            return
        self._goto(max(0, min(number - 1, self.page_count() - 1)))

    # -- render ------------------------------------------------------------

    def render(self, size):
        from ...mpvtk.scaling import raster

        route = self.route
        if route.get("_error"):
            return chrome.error(route["_error"])
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        # Coming back to a comic this page closed on the way out. The route
        # dict survives in the history with its data intact, so load() will
        # not run again — but the archive and the picture were given up, and
        # only the render pass can notice they are gone.
        if (self.archive is None and not route.get("_opening")
                and not route.get("_error")):
            path = (data.get("state") or (None, None))[1]
            if path:
                self._open_comic(path)
        # The window in the units mpv measures in. Kept on the route so the
        # placement maths can run from a button press, which has no size.
        window = raster(*size)
        if route.get("_window") != window:
            route["_window"] = window
            if route.get("_size"):
                self._place()
        item = data.get("item") or {}
        body = (self._waiting(data) if self.archive is None
                else Spacer(flex=1))
        return Column([self._top_bar(item), body, self._bottom_bar(size[0])],
                      flex=1, align="stretch")

    def _waiting(self, data):
        status, _path = data.get("state") or (None, None)
        if status == "error":
            message = _("The download failed.")
        else:
            message = _("Getting the comic…")
        return Column([Spacer(), Text(message, size=18,
                                      color=theme.SUBTLE_FG, align="center"),
                       Spacer()], flex=1, align="center")

    def _top_bar(self, item):
        title = item.get("Name") or self.route.get("title", "")
        return Row([
            Button("", id="cm-back", icon="arrow_back", w=34, h=34, pad=0,
                   justify="center", tip=_("Back"),
                   on_click=self.ctx.nav.go_back),
            Text(title, size=15, color=theme.TEXT_FG, flex=1),
        ], h=TOP_BAR_H, pad=(10, 4), gap=10, align="center",
            bg=theme.PANEL_BG)

    def _bottom_bar(self, width):
        total = self.page_count()
        narrow = width < 820

        def icon_btn(icon, node_id, cb, tip, enabled=True):
            return Button("", id=node_id, icon=icon, w=34, h=34, pad=0,
                          justify="center", tip=tip,
                          on_click=cb if enabled else None,
                          disabled=not enabled)

        index = self.page_index()
        children = [
            icon_btn("chevron_left", "cm-prev", lambda: self._turn(-1),
                     _("Previous page"), index > 0),
            icon_btn("chevron_right", "cm-next", lambda: self._turn(1),
                     _("Next page"), index + 1 < total),
        ]
        if total:
            children.append(Text(_("of %d") % total, size=14,
                                 color=theme.SUBTLE_FG))
            # A box rather than a label: a comic is the one thing in the
            # library people jump around inside by number.
            children.insert(2, TextBox(
                "cm-page", str(index + 1), w=56, size=14,
                on_submit=self._jump))
        children.append(Spacer())
        if not narrow:
            children.append(Dropdown(
                "cm-mode", [_("Fit Width"), _("Fit Page")],
                selected=0 if self.mode() == FIT_WIDTH else 1, size=14,
                w=140, on_select=lambda i, _v=None:
                    self._set_mode(FIT_WIDTH if i == 0 else FIT_PAGE)))
        # Text, not glyphs: the generated Material set has `add` but no
        # `remove`, and one icon beside one character reads as a mistake.
        # The epub reader's type-size buttons made the same call.
        def zoom_btn(label, node_id, cb, tip):
            return Button(label, id=node_id, w=34, h=34, pad=0, size=19,
                          justify="center", tip=tip, on_click=cb)

        children += [
            zoom_btn("\u2212", "cm-zoom-out", lambda: self._step_zoom(-1),
                     _("Zoom out")),
            Text("%d%%" % round(self.zoom() * 100), size=14,
                 color=theme.SUBTLE_FG, w=52, align="center"),
            zoom_btn("+", "cm-zoom-in", lambda: self._step_zoom(1),
                     _("Zoom in")),
        ]
        return Row(children, h=BOTTOM_BAR_H, pad=(10, 6), gap=6,
                   align="center", bg=theme.PANEL_BG)


def _picture_size(path):
    """``(w, h)`` from an image file's header, or None.

    ``Image.open`` parses the header and stops; nothing is decoded. The
    EXIF orientation is applied here because mpv applies it too, and a
    portrait page tagged as rotated would otherwise be fitted as a
    landscape one.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            try:
                orientation = (image.getexif() or {}).get(274)
            except Exception:
                orientation = None
            if orientation in (5, 6, 7, 8):
                width, height = height, width
            return int(width), int(height)
    except Exception:
        log.info("could not read the size of %s", path, exc_info=True)
        return None
