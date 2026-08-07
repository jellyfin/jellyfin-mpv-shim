"""The two screens a books library needs, which are not one screen.

`CollectionType.books` holds two unrelated entity types (see
``jellyfin_mpv_shim/books.py``), and they want opposite things:

* an **audiobook** is ordinary audio. Its folder is an album in everything
  but name, so :class:`BooksPage` draws it as one — header, action bar,
  tabular track list — and the existing playback machinery does the rest.
* a **book** cannot be played, streamed, paged or rendered. The server
  offers one endpoint that yields its bytes and nothing that yields part of
  it, so :class:`BookPage` is a download-and-open screen, and admits it.

**A books library browses by folder**, which is jellyfin-web's own default
tab for one (``constants/views/books.ts``: slot 0 is Folders) and, more to
the point, the only structure these libraries have — nothing joins a
multi-file audiobook rip except the directory it sits in. `SeriesName` is
populated for books and null for audiobooks; `Album` is the reverse and is
tag-derived, so an untagged rip has nothing at all joining it but its
parent. That is why the *folder* is the unit here and not a metadata field.
"""

import logging

from ...books import (book_format, format_label, is_audiobook, page_count,
                      progress_label, progress_settable)
from ...i18n import _, _p
from ...mpvtk.widgets import Column, Row, Text, VScroll
from .. import theme
from ..components import chrome, controls, detail as detail_components
from ..components.labels import is_watched
from .base import Page
from .grid import GridPage
from .music import HEADER_ART, HEADER_GAP

log = logging.getLogger("mpvtk_browser.pages.books")

#: The cover on a book's own page. Larger than a track list's header art —
#: a book page has nothing else to look at, and the cover is most of what
#: identifies an edition.
BOOK_ART = 220


def _track_order(track):
    """Sort key for the chapters of an audiobook.

    Disc/track first, then the name. `IndexNumber` is what a tagged rip
    carries and what the album screens sort by; an untagged one has none,
    and falls through to the filename order the names came from — which for
    "Chapter 01".."Chapter 12" is right, and is the best available answer
    when the tags say nothing.
    """
    return (track.get("ParentIndexNumber") or 0,
            track.get("IndexNumber") if track.get("IndexNumber") is not None
            else 1 << 30,
            str(track.get("Name") or ""))


class BooksPage(GridPage):
    """A books library, or a folder inside one.

    A :class:`GridPage` with one extra question asked at render time: are
    the things in this folder the chapters of an audiobook? If they are, it
    draws an album instead of a grid. Everything else — paging, sorting,
    filters, view settings, the virtualized window — is inherited, and is
    what a folder of five hundred books still needs.
    """

    kind = "books"

    def load(self, epoch):
        super().load(epoch)
        # The folder's own DTO, for the header of the track-list rendering:
        # its name, its cover, and the UserData the action bar toggles. Only
        # the track path draws it, but it is asked for unconditionally
        # because *whether* that path applies is not known until the
        # children have landed — and a second load pass, started once they
        # had, would put a visible pop into the one rendering that has a
        # header to pop.
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        folder_id = route.get("parent_id")
        if not folder_id:
            return

        def work():
            return source.get_item(srv, folder_id)

        self.route_async(work, lambda it: route.__setitem__("_folder", it),
                         epoch)

    # -- is this an audiobook? --------------------------------------------

    def _tracks(self):
        """The chapters, in playing order — or ``None`` when this folder is
        not an audiobook and should be drawn as a grid.

        Three conditions, and the middle one is the subtle one:

        1. there is something loaded;
        2. **all** of it is loaded. A windowed list is full of holes
           (``pagination.spread``), and a track list is a claim about a
           whole book: playing "from track 4" out of a list whose tracks 5
           and 6 have not arrived would queue two blanks. A folder too big
           to hold at once is not an audiobook anyone rips, so it falls
           back to the grid rather than growing a paged track list;
        3. every item is an `AudioBook`. A folder mixing a book with an
           audiobook is a folder, and drawing it as an album would hide the
           book — there is no row in a track list that could open one.
        """
        items = self.route.get("_items")
        if not items:
            return None
        loaded = [i for i in items if i]
        total = self.route.get("_total") or 0
        if not loaded or len(loaded) < total:
            return None
        if not all(is_audiobook(i) for i in loaded):
            return None
        return sorted(loaded, key=_track_order)

    # -- render ------------------------------------------------------------

    def render(self, size):
        tracks = self._tracks()
        if tracks is None:
            return super().render(size)
        return self._album(tracks, size)

    def _album(self, tracks, size):
        art = self.ctx.art
        route = self.route
        server = route.get("server") or self.ctx.server
        # The id and type ride the fallback too. The Download button hands
        # this dict to the download dialog, which estimates by (id, type) --
        # so a folder whose own DTO has not landed (or whose fetch failed)
        # would offer a Download of nothing at all.
        folder = route.get("_folder") or {
            "Id": route.get("parent_id"), "Type": "Folder",
            "Name": route.get("title", "")}
        ids = [t.get("Id") for t in tracks if t.get("Id")]
        header = Row([
            art.tiles.art_cell(folder, size=HEADER_ART),
            Column(self._header_text(folder, tracks, size[0]) + [
                self._actions(folder, tracks, ids, server),
            ], gap=8, flex=1, align="stretch"),
        ], gap=16, align="start")
        body = art.tiles.track_list(
            tracks, "bktrk",
            lambda i: self.ctx.actions.play_list(ids, server, i, audio=True,
                                                 items=tracks),
            # The album column is the book's own title on every row, which
            # the header above already says. Dropping it gives the chapter
            # names the width they need.
            album=False,
            scroll_id="books", head_h=110, menu=True)
        return VScroll(Column([header, body], pad=chrome.CONTENT_PAD, gap=12,
                              align="stretch"),
                       id="books", flex=1,
                       offset=self.parked_scroll("books"),
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "books", off, mx))

    def _header_text(self, folder, tracks, width):
        out = [Text(folder.get("Name") or self.route.get("title", ""),
                    size=28, bold=True)]
        # The author, which for an audiobook is the album artist. Named
        # rather than folded into the meta line: on a book it is the single
        # most useful thing on the screen after the title.
        author = folder.get("AlbumArtist") or ""
        if not author and tracks:
            author = ", ".join(tracks[0].get("Artists") or [])
        meta = [x for x in (
            author,
            _("%d chapters") % len(tracks) if len(tracks) > 1 else "",
            detail_components.fmt_ticks(
                sum(t.get("RunTimeTicks") or 0 for t in tracks)),
        ) if x]
        if meta:
            out.append(Text("   ·   ".join(meta), size=15,
                            color=theme.SUBTLE_FG))
        overview = (folder.get("Overview") or "").strip()
        if overview:
            out.append(chrome.paragraph(
                overview, 15,
                max(120, self.ctx.art.tiles.body_w(width)
                    - HEADER_ART - HEADER_GAP)))
        return out

    def _resume_at(self, tracks):
        """``(index, offset_ticks)`` for where listening left off, or None.

        The first chapter that is not finished, resumed at its own stored
        position. A book is the one thing people listen to over weeks, so
        "where was I" is the primary gesture here in a way it never is on
        an album — and the answer is *two* numbers, because a rip's
        position lives on whichever chapter was playing, not on the book.
        """
        for i, track in enumerate(tracks):
            data = track.get("UserData") or {}
            if data.get("Played"):
                continue
            return i, (data.get("PlaybackPositionTicks") or 0) or None
        return None

    def _actions(self, folder, tracks, ids, server):
        actions = self.ctx.actions
        tiles = self.ctx.art.tiles
        btns = []
        resume = self._resume_at(tracks)
        # Resume leads when there is something to resume, exactly as the
        # detail page does. It is only ever *offered* — Play from the top is
        # always available beside it.
        if resume is not None and (resume[0] or resume[1]):
            index, offset = resume
            label = _("Resume")
            if len(tracks) > 1:
                label += "  " + (tracks[index].get("Name") or "")
            btns.append(controls.action_btn(
                "play_arrow", label, "bk-resume",
                lambda: actions.play_list(ids, server, index, audio=True,
                                          items=tracks),
                primary=True, autofocus=True))
        btns.append(controls.action_btn(
            "play_arrow", _("Play"), "bk-play",
            lambda: actions.play_list(ids, server, 0, audio=True),
            primary=(resume is None or not (resume[0] or resume[1])),
            autofocus=(resume is None or not (resume[0] or resume[1]))))
        btns.append(controls.action_btn(
            "playlist_add", _("Add to Queue"), "bk-queue",
            lambda: actions.queue_items(ids, server)))
        # Deliberately no Shuffle, which every other "container of tracks"
        # screen has. These are the chapters of a book; playing them in a
        # random order is not a feature anyone has ever wanted, and offering
        # it beside Resume invites exactly one misclick.
        btns.append(controls.action_btn(
            "check", _("Watched"), "bk-watched",
            lambda: actions.toggle_watched(folder, server),
            on=is_watched(folder)))
        btns.append(controls.action_btn(
            "favorite", _("Favorite"), "bk-fav",
            lambda: actions.toggle_favorite(folder, server),
            on=bool((folder.get("UserData") or {}).get("IsFavorite"))))
        # A folder is not a downloads row, so "is this downloaded" has to be
        # asked of its chapters. All of them, not any: a book half on disk
        # is not one you can listen to on a train.
        downloaded = bool(ids) and all(tiles.is_downloaded(t) for t in tracks)
        if downloaded:
            btns.append(controls.action_btn(
                "delete", _("Remove Download"), "bk-undownload",
                lambda: actions.remove_downloads(
                    ids, folder.get("Name") or "")))
        elif not actions.offline:
            btns.append(controls.action_btn(
                "file_download", _("Download"), "bk-download",
                lambda: actions.open_download(folder)))
        return Row(btns, gap=8, align="center")


class BookPage(Page):
    """One book: what it is, how far through it you are, and the only two
    things that can be done with it.

    There is no Play button and there will not be one. Jellyfin serves no
    page, no archive entry and no spine document — `Book` is not
    `IHasMediaSources` and `/Items/{id}/Download` is the whole API — so
    every way of showing a book in this window would begin by fetching the
    entire file, and end with a worse reader than the one already
    installed. See ``books.py``.
    """

    kind = "book"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        iid = route["item_id"]

        def work():
            item = source.get_item(srv, iid)
            # The catalog answer rides along with the item so the buttons
            # are right on the first frame. Asked here rather than at render
            # time because it stats a file, and render runs per frame.
            state = (None, None)
            try:
                state = self.ctx.player.book_download_state(iid)
            except Exception:
                log.debug("book download state unavailable", exc_info=True)
            return {"item": item, "state": state}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def refresh_download_state(self):
        """Re-read this book's catalog state, called when it changes.

        The state is resolved once, in ``load``, because answering it stats
        a file and ``render`` runs per frame. That is fine until the answer
        moves under the screen — which for this page it always does, since
        every button on it is the thing that moves it: pressing Download
        queues a row, the worker completes it a moment later, Remove takes
        it away again. Without this the button kept saying "Download"
        through the whole download and for as long as the page stayed open.

        Reached generically by the shell through the downloads-changed hook,
        so this page owns the knowledge of what needs re-reading rather than
        the shell knowing about books.
        """
        data = self.route.get("_data")
        if not data:
            return
        item = data.get("item") or {}
        if not item.get("Id"):
            return
        try:
            data["state"] = self.ctx.player.book_download_state(item["Id"])
        except Exception:
            log.debug("book download state unavailable", exc_info=True)

    def render(self, size):
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        item = data.get("item")
        if not item:
            return chrome.error(_("Item not available."))
        server = route.get("server") or self.ctx.server
        art = self.ctx.art
        text = [Text(item.get("Name") or route.get("title", ""), size=28,
                     bold=True)]
        context = self._context(item)
        if context:
            text.append(Text(context, size=17, color=theme.SUBTLE_FG))
        meta = self._meta(item)
        if meta:
            text.append(Text(meta, size=15, color=theme.SUBTLE_FG))
        text.append(self._buttons(item, server, data.get("state")))
        blocks = [Row([art.tiles.art_cell(item, size=BOOK_ART),
                       Column(text, gap=8, flex=1, align="stretch")],
                      gap=16, align="start")]
        if item.get("Overview"):
            blocks.append(chrome.paragraph(item["Overview"], 18,
                                           art.tiles.body_w(size[0])))
        return VScroll(Column(blocks, pad=chrome.CONTENT_PAD, gap=16),
                       id="book", flex=1,
                       offset=self.parked_scroll("book"))

    @staticmethod
    def _context(item):
        """Series and volume, when the scanner found any.

        `SeriesName` on a Book is whatever `BookResolver` parsed, which is
        the series for a numbered book and the containing folder otherwise
        — often the author. Shown as-is either way: guessing which it is
        would be wrong about half the library, and both are the right thing
        to show above the title.
        """
        series = (item.get("SeriesName") or "").strip()
        if not series:
            return ""
        index = item.get("IndexNumber")
        return "%s #%d" % (series, index) if index is not None else series

    @staticmethod
    def _meta(item):
        parts = [format_label(book_format(item))]
        if item.get("ProductionYear"):
            parts.append(str(item["ProductionYear"]))
        pages = page_count(item)
        if pages:
            parts.append(_("%d pages") % pages)
        # Where the reader is, if the format has anywhere to be. Books have
        # no runtime to show in the usual sense; this is the line that takes
        # its place.
        progress = progress_label(item)
        if progress:
            parts.append(progress)
        genres = ", ".join((item.get("Genres") or [])[:3])
        if genres:
            parts.append(genres)
        return "   ·   ".join(p for p in parts if p)

    def _buttons(self, item, server, state):
        actions = self.ctx.actions
        status, path = state or (None, None)
        btns = [controls.action_btn(
            # Two senses of one English word on one screen, which is exactly
            # what _p exists for: this one is the verb ("open this book"),
            # the toggle below is the state ("I have read it"). gettext keys
            # on the English, so without contexts no language could tell
            # them apart -- and several need different words.
            "menu_book", _p("open a book", "Read"), "bk-read",
            lambda: actions.read_book(item, server),
            primary=True, size=18, autofocus=True)]
        if path:
            btns.append(controls.action_btn(
                "delete", _("Remove Download"), "bk-undownload",
                lambda: actions.confirm_remove_download(item), size=18))
        elif status in ("pending", "downloading"):
            # A label, not a button: there is nothing useful to press while
            # it is in flight, and a Download button that re-enqueues an
            # in-flight download reads as one that did nothing.
            btns.append(Text(_("Downloading…"), size=16,
                             color=theme.SUBTLE_FG))
        elif not actions.offline:
            # Download, and only download. Read is the button that opens
            # one; a Download that also launched a reader would be a
            # surprise, and there is no way to ask for the copy without the
            # window if both buttons do the same thing.
            btns.append(controls.action_btn(
                "file_download", _("Download"), "bk-download",
                lambda: actions.download_book(item, server), size=18))
        if progress_settable(item):
            # Left out entirely for an epub, whose stored position is an
            # index into a JavaScript library's locations array rather than
            # anything a reader shows you a number for -- there is nothing
            # to type. The figure is still read and shown in the meta line
            # above; only setting it is refused. See books.progress_settable.
            btns.append(controls.action_btn(
                "bookmark", _("Progress…"), "bk-progress",
                lambda: self.ctx.dialogs.open_book_progress(item, server),
                size=18))
        btns.append(controls.action_btn(
            "check", _p("mark a book finished", "Read"), "bk-watched",
            lambda: actions.toggle_watched(item, server),
            on=is_watched(item), size=18))
        btns.append(controls.action_btn(
            "favorite", _("Favorite"), "bk-fav",
            lambda: actions.toggle_favorite(item, server),
            on=bool((item.get("UserData") or {}).get("IsFavorite")),
            size=18))
        return Row(btns, gap=8, align="center")
