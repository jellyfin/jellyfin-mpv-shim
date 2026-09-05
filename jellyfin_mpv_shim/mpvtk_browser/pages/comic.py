"""Reading a comic in the player's window.

The page itself is not drawn here at all: it is handed to mpv as a file to
display, and mpv's ``video-zoom`` / ``video-pan-x`` / ``video-pan-y`` are
the zoom and the scroll. What this module draws is two bars over the top of
it, the same thing the playback HUD does over video — **a third window
state**, argued in ``docs/readers.md`` §5.2.

**The page size is read from the file's header, not from mpv**, because
knowing it *before* the load is what lets the zoom be set in the same
breath as the picture; asking mpv is a poll loop in the render path. See
``docs/readers.md`` §5.
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

        # ``always``, because AsyncRunner drops BOTH callbacks when the
        # epoch has moved on — press Back while a novel's index is still
        # building and neither `done` nor `failed` ever runs. Without this
        # the flag stays True for the life of the route dict, and since
        # every re-entry is gated on it, that history entry says
        # "Getting the comic…" for the rest of the session.
        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed,
                         always=lambda: route.update(_opening=False))

    def close(self):
        """Leave the window as we found it.

        Called by the shell when this route stops being the screen. Both
        halves matter: the picture is mpv's, so leaving without clearing it
        leaves the comic behind the library grid (``docs/readers.md`` §5.2);
        and the extracted pages are files, which nothing else will delete.
        """
        self.route.pop("_showing", None)
        # The error described a page of the archive we are about to give up,
        # and the re-open on the way back in is `_error`-guarded. Keeping it
        # here meant leaving a comic on a bad page -- the natural reaction to
        # one -- made it unopenable for the rest of the session, with the
        # in-place recovery (`done` clears `_error` on the first page that
        # reads) unreachable because nothing would re-open the archive.
        self.route.pop("_error", None)
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
            if route.get("_page") != index:
                # A newer turn has already been asked for. Extractions run
                # on a pool several deep when the key is held, and they
                # finish in whatever order they finish: without this, a
                # late page-3 result shows page 3 while the bar reads
                # "6 of N", and it stays wrong until the next turn.
                return
            route["_size"] = size
            route.pop("_error", None)
            self.ctx.player.show_picture(path)
            route["_showing"] = True
            # A page is up, so the "Downloading…" toast is reporting
            # something that has finished (#2). Same call as the epub
            # reader makes, and harmless on every page after the first --
            # it only clears a toast that is still that message.
            self.ctx.actions.clear_downloading_toast(
                (route.get("_data") or {}).get("item") or {})
            self._place(to_bottom=to_bottom)
            self.ctx.invalidate()

        def failed(exc):
            if route.get("_comic") is not archive:
                # Same liveness question `done` asks. Without it, leaving
                # mid-extraction writes the errno from our own cleanup onto
                # a route the user is no longer looking at — and `_error`
                # is checked before anything else on the way back in.
                return
            if route.get("_page") != index:
                return
            route["_error"] = str(exc) or _("This page could not be read.")
            self.ctx.invalidate()

        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed)

    def _resume_page(self, archive):
        """Where the server says this reader had got to.

        A comic's position is a **page index**, 1-based out of
        ``books.progress_of`` (``docs/readers.md`` §2). Clamped to what is
        actually in the archive: the count the server probed is its own
        count of the entries, and a file added or removed since would
        otherwise resume past the end.
        """
        data = self.route.get("_data") or {}
        item = data.get("item") or {}
        mode, page, _total = progress_of(item)
        if mode != "pages" or not page:
            return 0
        return max(0, min(page - 1, archive.page_count - 1))

    def _save_position(self, index):
        """Report the page back, so every other client resumes here too.

        Fire and forget, like the epub reader's, and through
        ``record_reading_position`` for the reasons in ``docs/readers.md``
        §2.2. The DTO in hand is updated as well — this page's own copy; the
        book page below holds a different dict, which ``_land_back`` reloads
        on the way out.
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
        setter = getattr(self.ctx.player, "record_reading_position", None)
        if setter is None:
            return
        self.ctx.run.submit(lambda: setter(server, item["Id"], ticks))

    # -- gestures ----------------------------------------------------------

    def on_picture_gesture(self, kind, evt):
        """A wheel notch off the end of the page, or ctrl+wheel.

        Everything *inside* a page is the renderer's and never reaches here
        (``docs/readers.md`` §5.6). What arrives is only the two answers it
        cannot give: which page is next, and what a zoom step means.
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
        """The reading mode, from the route or from the setting behind it.

        A setting, not page state, read per call rather than captured —
        Settings is reachable from the tray while a comic is up
        (``docs/readers.md`` §5.6). The route still holds it so that a mode
        is not written to disk before it is asked for.
        """
        mode = self.route.get("_mode")
        if mode in (FIT_WIDTH, FIT_PAGE):
            return mode
        from ...conf import settings

        stored = getattr(settings, "comic_fit", FIT_WIDTH)
        return stored if stored in (FIT_WIDTH, FIT_PAGE) else FIT_WIDTH

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

        Everything continuous happens over there against mpv's own
        properties (``docs/readers.md`` §5.6), so this is the whole of the
        conversation: the clamp, and the pixel size the pan unit is measured
        in. Re-sent on every zoom, page and resize, because all three change
        it.
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
                    # **Which page this clamp is for**, and the renderer
                    # never reads it. It is here to make the payload
                    # DIFFER: `set_picture_pan` skips an unchanged model,
                    # and the renderer releases its end-of-page interlock
                    # when a fresh one arrives. In Fit Page every page is
                    # fitted whole, so consecutive pages of the same size
                    # produce a byte-identical clamp -- no message, no
                    # release, and the wheel turned exactly one page and
                    # then went dead. Fit Width hid it because a taller
                    # page has a pan range, so the clamp really does
                    # change.
                    "page": self.page_index(),
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
        # Sticky: through config.set_setting rather than by assigning to
        # settings, so it is coerced and validated the same way the Settings
        # form writes it (readers.md §5.6).
        from .. import config

        try:
            config.set_setting("comic_fit", mode)
        except Exception:
            log.warning("could not save the comic reading mode", exc_info=True)
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
        data = route.get("_data")
        if data is None:
            if route.get("_error"):
                # Nothing else to draw yet, so the error IS the screen.
                return chrome.error(route["_error"])
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
        elif (self.archive is not None and not route.get("_showing")
                and not route.get("_error")):
            # The archive is still open but the window is not showing the
            # page: the browser yielded (playback, or being minimized),
            # which issues `stop`, and came back. Nothing else notices —
            # the route was never retired, so the re-open above does not
            # fire — and the bars would repaint over an empty window until
            # the user pressed Next.
            #
            # `_error` is checked here for the same reason the re-open above
            # checks it. Only `done` sets `_showing`, so after a failed
            # extraction this condition is still true on the very next
            # render — and `_show_page` dispatches unconditionally, so it
            # asked again every repaint, one pool job per frame on the
            # shared api pool. The failure is already on screen with the
            # bars around it; the way out of it is Next/Prev, which calls
            # `_show_page` directly.
            self._show_page(route.get("_page", 0))
        # The window in the units mpv measures in. Kept on the route so the
        # placement maths can run from a button press, which has no size.
        window = raster(*size)
        if route.get("_window") != window:
            route["_window"] = window
            if route.get("_size"):
                self._place()
        item = data.get("item") or {}
        if route.get("_error"):
            # **Inside the page's own Column**, not instead of it. "comic"
            # is CHROME_FREE, so returning a bare error node leaves a line
            # of grey text on an empty window with no Back button, no bars
            # and no way out but the tray — for one unreadable page in an
            # otherwise fine comic. Keeping the bars means the next/prev
            # buttons are still there, and `done` clears `_error` on the
            # first page that does read.
            body = Column([Spacer(), chrome.error(route["_error"]),
                           Spacer()], flex=1, align="center")
        elif self.archive is None:
            body = self._waiting(data)
        else:
            body = Spacer(flex=1)
        return Column([self._top_bar(item), body, self._bottom_bar(size[0])],
                      flex=1, align="stretch")

    def _waiting(self, data):
        status, _path = data.get("state") or (None, None)
        if status == "error":
            message = _("The download failed.")
        else:
            message = _("Getting the comic…")
        return Column([Spacer(), Text(message, size="large",
                                      color=theme.SUBTLE_FG, align="center"),
                       Spacer()], flex=1, align="center")

    def _top_bar(self, item):
        title = item.get("Name") or self.route.get("title", "")
        return Row([
            Button("", id="cm-back", icon="arrow_back", w=34, h=34, pad=0,
                   justify="center", tip=_("Back"),
                   on_click=self.ctx.nav.go_back),
            Text(title, size="small", color=theme.TEXT_FG, flex=1),
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
            children.append(Text(_("of %d") % total, size="caption",
                                 color=theme.SUBTLE_FG))
            # A box rather than a label: a comic is the one thing in the
            # library people jump around inside by number.
            children.insert(2, TextBox(
                "cm-page", str(index + 1), w=56, size="caption",
                on_submit=self._jump))
        children.append(Spacer())
        if not narrow:
            children.append(Dropdown(
                "cm-mode", [_("Fit Width"), _("Fit Page")],
                selected=0 if self.mode() == FIT_WIDTH else 1, size="caption",
                w=140, on_select=lambda i, _v=None:
                    self._set_mode(FIT_WIDTH if i == 0 else FIT_PAGE)))
        # Text, not glyphs: the generated Material set has `add` but no
        # `remove`, and one icon beside one character reads as a mistake.
        # The epub reader's type-size buttons made the same call.
        def zoom_btn(label, node_id, cb, tip):
            return Button(label, id=node_id, w=34, h=34, pad=0, size="large",
                          justify="center", tip=tip, on_click=cb)

        children += [
            zoom_btn("\u2212", "cm-zoom-out", lambda: self._step_zoom(-1),
                     _("Zoom out")),
            Text("%d%%" % round(self.zoom() * 100), size="caption",
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
