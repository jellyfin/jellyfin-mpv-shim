"""The two screens a books library needs, which are not one screen.

`CollectionType.books` holds two unrelated entity types (see
``jellyfin_mpv_shim/books.py`` and ``docs/readers.md`` §1), and they want
opposite things:

* an **audiobook** is ordinary audio. Its folder is an album in everything
  but name, so :class:`BooksPage` draws it as one — header, action bar,
  tabular track list — and the existing playback machinery does the rest.
* a **book** cannot be played, streamed, paged or rendered, so
  :class:`BookPage` is a download-and-open screen, and admits it.

**A books library browses by folder**, because the folder is the only
structure these libraries have — see ``docs/readers.md`` §3.
"""

import logging

from ...books import (book_format, format_label, is_audiobook, page_count,
                      reader_route,
                      progress_label, progress_settable)
from ...i18n import _, _p
from ...mpvtk.widgets import Column, Row, Table, Text, VScroll
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


def one_book(tracks):
    """Whether these audiobook files are chapters of ONE book.

    The distinction the browser has to make and the DTO almost does not
    support: three parts of one recording and four unrelated novels are both
    "a folder of AudioBooks", and nothing about the folder tells them apart.

    **`Album` is the only field that ever joins a rip** (``docs/readers.md``
    §3, §3.1), so one distinct album is one book and several albums are
    several books. Two cases the field cannot answer: an untagged rip has no
    album at all, and there the folder really is the only thing joining the
    files, so "nobody has one" reads as one book rather than as N; a folder
    where only *some* are tagged is several, because the tagged ones name a
    book the untagged ones are not claiming to be part of.
    """
    albums = {(t.get("Album") or "").strip() for t in tracks}
    return len(albums) == 1


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

        Four conditions, and the second is the subtle one:

        1. there is something loaded;
        2. **all** of it is loaded — a windowed list is full of holes
           (``pagination.spread``) and playing "from track 4" out of one
           queues blanks (``docs/readers.md`` §3);
        3. every item is an `AudioBook`. A folder mixing a book with an
           audiobook is a folder, and drawing it as an album would hide the
           book — there is no row in a track list that could open one;
        4. they are all the *same* book. See :func:`one_book`.
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
        if not one_book(loaded):
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
        folder = route.get("_folder")
        if folder is None:
            # **Kept on the route, not rebuilt.** Finished and Favorite flip
            # this dict optimistically and the next frame reads it back; a
            # literal rebuilt in the builder is a fresh dict every frame, so
            # the button could never show its own on-state and every press
            # re-sent the same value. One of the two named footguns in
            # CLAUDE.md, in its other form: state that a handler writes and
            # the next draw cannot see.
            folder = route.setdefault("_folder_stub", {
                "Id": route.get("parent_id"), "Type": "Folder",
                "Name": route.get("title", "")})
        ids = [t.get("Id") for t in tracks if t.get("Id")]
        header = Row([
            art.tiles.art_cell(folder, size=HEADER_ART),
            Column(self._header_text(folder, tracks, size[0]) + [
                self._actions(folder, tracks, ids, server, size[0]),
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
            # Which chapters are behind you is the whole state of a book --
            # it is what Resume is computed from -- and without the column
            # marking one played had no visible effect anywhere.
            watched=True,
            scroll_id="books", head_h=110, menu=True)
        return VScroll(Column([header, body], pad=chrome.CONTENT_PAD, gap=12,
                              align="stretch"),
                       id="books", flex=1,
                       offset=self.parked_scroll("books"),
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "books", off, mx))

    def _header_text(self, folder, tracks, width):
        out = [Text(folder.get("Name") or self.route.get("title", ""),
                    size="hero", bold=True)]
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
            out.append(Text("   ·   ".join(meta), size="small",
                            color=theme.SUBTLE_FG))
        overview = self._overview(folder, tracks)
        if overview:
            out.append(chrome.paragraph(
                overview, 15,
                max(120, self.ctx.art.tiles.body_w(width)
                    - HEADER_ART - HEADER_GAP)))
        return out

    @staticmethod
    def _overview(folder, tracks):
        """The book's description.

        The folder's own if it has one (an `.nfo` gives it one), otherwise
        the first chapter's — an audiobook's description lives in the
        FILE's tags, never on the directory. The *first* track's rather
        than any: they are chapters of one book (see :func:`one_book`).
        """
        overview = (folder.get("Overview") or "").strip()
        if overview:
            return overview
        return (tracks[0].get("Overview") or "").strip() if tracks else ""

    @staticmethod
    def _resume_at(tracks):
        """``(index, offset_ticks)`` for where listening left off, or None.

        The first chapter that is not finished, resumed at its own stored
        position. A book is the one thing people listen to over weeks, so
        "where was I" is the primary gesture here in a way it never is on
        an album — and the answer is *two* numbers, because a rip's
        position lives on whichever chapter was playing, not on the book.

        ``None`` means finished: every chapter played, so there is nothing
        to resume and Play from the top is the only sensible offer.
        """
        for i, track in enumerate(tracks):
            data = track.get("UserData") or {}
            if data.get("Played"):
                continue
            return i, (data.get("PlaybackPositionTicks") or 0) or None
        return None

    #: How much of a chapter's name the Resume button will carry.
    #:
    #: A chapter name is whatever the ripper typed and can be a sentence.
    #: The action row is a fixed row of buttons, so one long label pushes
    #: Download off the right edge of the window -- and the name is the
    #: least important part of the button anyway: what it has to say is
    #: *Resume*, and roughly where.
    RESUME_LABEL_MAX = 22

    #: Below this the Resume button is the bare word.
    #:
    #: Measured, not chosen: the action row is Resume, Restart, Add to play
    #: queue, Finished, Favorite and Download, and at about this width the
    #: chapter name is what tips Download off the right edge. Capping the
    #: name (RESUME_LABEL_MAX) bounds how bad it gets; dropping it is what
    #: makes the row fit, and "Resume" alone still says what the button
    #: does -- which is the part that has to survive.
    #:
    #: **Re-measure it when any label in that row changes.** It moved from
    #: 1060 when the queue button took jellyfin-web's wording ("Add to play
    #: queue", five characters more than "Add to Queue"), and the symptom is
    #: not a wrapped row -- it is Download simply gone off the edge.
    RESUME_NAME_MIN_W = 1100

    @classmethod
    def _resume_label(cls, tracks, index, width=None):
        """"Resume", plus enough of the chapter to say which one.

        Nothing at all for a single-file book: there is only one thing to
        resume, and repeating the title of the page on the button that
        resumes it is noise. Nothing on a narrow window either -- see
        RESUME_NAME_MIN_W.
        """
        if len(tracks) < 2:
            return _("Resume")
        if width is not None and width < cls.RESUME_NAME_MIN_W:
            return _("Resume")
        name = (tracks[index].get("Name") or "").strip()
        if not name:
            # No name to show, so say the position instead -- which is the
            # thing the user actually wants to know and is always available.
            return "%s  %d/%d" % (_("Resume"), index + 1, len(tracks))
        if len(name) > cls.RESUME_LABEL_MAX:
            name = name[:cls.RESUME_LABEL_MAX - 1].rstrip() + "…"
        return "%s  %s" % (_("Resume"), name)

    def _actions(self, folder, tracks, ids, server, width=None):
        actions = self.ctx.actions
        tiles = self.ctx.art.tiles
        btns = []
        resume = self._resume_at(tracks)
        started = resume is not None and (resume[0] or resume[1])
        if started:
            # There IS no plain "Play" once a book has been started: the
            # position is hours of listening and starting from chapter one
            # overwrites it as it goes, so the second button says out loud
            # that it goes back to the beginning (readers.md §3).
            index, offset = resume
            label = self._resume_label(tracks, index, width)
            btns.append(controls.action_btn(
                "play_arrow", label, "bk-resume",
                lambda: actions.play_list(ids, server, index, audio=True,
                                          items=tracks),
                primary=True, autofocus=True))
            btns.append(controls.action_btn(
                "first_page", _("Restart"), "bk-play",
                lambda: actions.play_list(ids, server, 0, audio=True)))
        else:
            btns.append(controls.action_btn(
                "play_arrow", _("Play"), "bk-play",
                lambda: actions.play_list(ids, server, 0, audio=True),
                primary=True, autofocus=True))
        btns.append(controls.action_btn(
            "playlist_add", _("Add to play queue"), "bk-queue",
            lambda: actions.queue_items(ids, server)))
        # Deliberately no Shuffle, which every other "container of tracks"
        # screen has. These are the chapters of a book; playing them in a
        # random order is not a feature anyone has ever wanted, and offering
        # it beside Resume invites exactly one misclick.
        btns.append(controls.action_btn(
            # "Watched" is what every other container says and is simply
            # wrong for something you listen to.
            "check", _("Finished"), "bk-watched",
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
        # Wrapped, not a bare Row: this is the widest action row in the app
        # -- six buttons, one of them carrying a chapter name -- and dropping
        # the name (RESUME_NAME_MIN_W) only postpones the point at which
        # Download leaves the window. Below that point a second line is the
        # honest answer; above it `wrap_row` hands back the same single Row
        # it always did.
        if not width:
            return Row(btns, gap=8, align="center")
        # Less the cover and its gap, the same subtraction `_header_text`
        # makes: this row sits in the column BESIDE the art, not across the
        # page, and measuring against the page width is how it kept
        # overflowing by exactly a cover.
        avail = max(120, tiles.body_w(width) - HEADER_ART - HEADER_GAP)
        return chrome.wrap_row(btns, avail, gap=8)


class AudiobookPage(Page):
    """One audiobook, as a place rather than as a thing that starts playing.

    A loose single-file audiobook had no destination at all: it is an
    ordinary `Audio` item, so a tile click started it exactly as a song
    does — which meant its **description, its length and its chapters were
    unreachable**, and there was nowhere to resume it from. That is right
    for a song, whose whole content is the sound, and wrong for a book.

    So an `AudioBook` tile now opens this, and the hover play chip starts
    it — the same split every other playable type already makes. The rows
    inside a *chapter list* are unaffected: there the entries are chapters
    of one book and clicking one is meant to play it.
    """

    kind = "audiobook"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server

        def work():
            return source.get_item(srv, route["item_id"])

        self.route_async(work, lambda it: route.__setitem__("_data", it),
                         epoch)

    def render(self, size):
        route = self.route
        item = route.get("_data")
        if item is None:
            return chrome.busy()
        if not item:
            return chrome.error(_("Item not available."))
        art = self.ctx.art
        server = route.get("server") or self.ctx.server
        text = [Text(item.get("Name") or route.get("title", ""), size="hero",
                     bold=True)]
        meta = self._meta(item)
        if meta:
            text.append(Text(meta, size="small", color=theme.SUBTLE_FG))
        text.append(self._actions(item, server))
        blocks = [Row([art.tiles.art_cell(item, size=BOOK_ART),
                       Column(text, gap=8, flex=1, align="stretch")],
                      gap=16, align="start")]
        if item.get("Overview"):
            blocks.append(chrome.paragraph(item["Overview"], 17,
                                           art.tiles.body_w(size[0])))
        chapters = self._chapter_rows(item, server)
        if chapters is not None:
            blocks.append(chapters)
        return VScroll(Column(blocks, pad=chrome.CONTENT_PAD, gap=16),
                       id="audiobook", flex=1,
                       offset=self.parked_scroll("audiobook"))

    @staticmethod
    def _meta(item):
        parts = []
        author = (item.get("AlbumArtist")
                  or ", ".join(item.get("Artists") or []))
        if author:
            parts.append(author)
        if item.get("ProductionYear"):
            parts.append(str(item["ProductionYear"]))
        runtime = item.get("RunTimeTicks")
        if runtime:
            parts.append(detail_components.fmt_ticks(runtime))
        chapters = item.get("Chapters") or []
        if len(chapters) > 1:
            parts.append(_("%d chapters") % len(chapters))
        position = (item.get("UserData") or {}).get("PlaybackPositionTicks")
        if position:
            parts.append(_("%s in")
                         % detail_components.fmt_ticks(position))
        return "   ·   ".join(parts)

    def _actions(self, item, server):
        actions = self.ctx.actions
        tiles = self.ctx.art.tiles
        position = (item.get("UserData") or {}).get(
            "PlaybackPositionTicks") or 0
        btns = []
        if position:
            # Same rule as a folder of chapters, and for the same reason:
            # no bare Play once a book has been started (readers.md §3).
            btns.append(controls.action_btn(
                "play_arrow",
                _("Resume") + "  " + detail_components.fmt_ticks(position),
                "ab-resume",
                lambda: actions.play(item, server, offset_ticks=position),
                primary=True, size=controls.PRIMARY_ROW, autofocus=True))
            btns.append(controls.action_btn(
                "first_page", _("Restart"), "ab-play",
                lambda: actions.play(item, server), size=controls.PRIMARY_ROW))
        else:
            btns.append(controls.action_btn(
                "play_arrow", _("Play"), "ab-play",
                lambda: actions.play(item, server),
                primary=True, size=controls.PRIMARY_ROW, autofocus=True))
        btns.append(controls.action_btn(
            "playlist_add", _("Add to play queue"), "ab-queue",
            lambda: actions.queue_items([item.get("Id")], server), size=controls.PRIMARY_ROW))
        btns.append(controls.action_btn(
            "check", _("Finished"), "ab-watched",
            lambda: actions.toggle_watched(item, server),
            on=is_watched(item), size=controls.PRIMARY_ROW))
        btns.append(controls.action_btn(
            "favorite", _("Favorite"), "ab-fav",
            lambda: actions.toggle_favorite(item, server),
            on=bool((item.get("UserData") or {}).get("IsFavorite")),
            size=controls.PRIMARY_ROW))
        download = detail_components.download_button(
            actions, tiles, item, server, "ab", size=controls.PRIMARY_ROW)
        if download is not None:
            btns.append(download)
        return Row(btns, gap=8, align="center")

    def _chapter_rows(self, item, server):
        """The book's own chapters, each starting playback at its mark.

        These are markers inside ONE file, not queue entries — so a row
        plays the book from that offset rather than playing anything of its
        own. That is the whole difference between this page and the folder
        of a rip, where the same gesture starts a different item.
        """
        chapters = item.get("Chapters") or []
        if len(chapters) < 2:
            return None
        rows = []
        for i, chapter in enumerate(chapters):
            start = chapter.get("StartPositionTicks") or 0
            rows.append({
                "id": "ab-ch-%d" % i,
                "cells": [str(i + 1),
                          chapter.get("Name") or _("Chapter %d") % (i + 1),
                          detail_components.fmt_ticks(start)],
                "on_click": (lambda offset=start:
                             self.ctx.actions.play(item, server,
                                                   offset_ticks=offset)),
            })
        return Column([
            Text(_("Chapters"), size="title", bold=True),
            Table([{"label": "#", "w": 46, "align": "right"},
                   {"label": _("Title"), "flex": 1},
                   {"label": _("Start"), "w": 90, "align": "right"}],
                  rows, size="normal", hover_bg=theme.BUTTON_BG),
        ], gap=8, align="stretch")


class BookPage(Page):
    """One book: what it is, how far through it you are, and the only two
    things that can be done with it.

    There is no Play button and there will not be one: a `Book` is not
    `IHasMediaSources` and `/Items/{id}/Download` is the whole API, so
    everything on this screen begins with a local copy. See ``books.py``
    and ``docs/readers.md`` §1.
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
        text = [Text(item.get("Name") or route.get("title", ""), size="hero",
                     bold=True)]
        context = self._context(item)
        if context:
            text.append(Text(context, size="normal", color=theme.SUBTLE_FG))
        meta = self._meta(item)
        if meta:
            text.append(Text(meta, size="small", color=theme.SUBTLE_FG))
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

    def _read_here(self, item, server):
        """Open this book in whichever built-in reader draws its format."""
        self.ctx.nav.navigate({"kind": reader_route(item), "server": server,
                               "item_id": item.get("Id"),
                               "title": item.get("Name", "")})

    def _buttons(self, item, server, state):
        actions = self.ctx.actions
        status, path = state or (None, None)
        # **Epub and the two comic formats Python can unpack read in this
        # window**, and the button row says so by what it offers rather than
        # by refusing afterwards; for every other format Read hands the file
        # to the desktop (readers.md §1). The list itself is
        # books.IN_WINDOW_FORMATS, so this row and the tile menu cannot
        # disagree about it.
        readable = reader_route(item) is not None
        btns = [controls.action_btn(
            # Two senses of one English word on one screen, which is exactly
            # what _p exists for: this one is the verb ("open this book"),
            # the toggle below is the state ("I have read it"). gettext keys
            # on the English, so without contexts no language could tell
            # them apart -- and several need different words.
            "menu_book", _p("open a book", "Read"), "bk-read",
            (lambda: self._read_here(item, server)) if readable
            else (lambda: actions.read_book(item, server)),
            primary=True, size=controls.PRIMARY_ROW, autofocus=True)]
        if readable:
            # Kept next to it rather than behind a setting: the built-in
            # reader draws paragraphs, headings, emphasis and pictures and
            # deliberately nothing else, so a book that needs more than
            # that needs this button, and needing it is not a state the
            # user can be expected to predict from a preferences page.
            btns.append(controls.action_btn(
                "", _("Open Externally"), "bk-read-ext",
                lambda: actions.read_book(item, server), size=controls.PRIMARY_ROW))
        if path:
            btns.append(controls.action_btn(
                "delete", _("Remove Download"), "bk-undownload",
                lambda: actions.confirm_remove_download(item), size=controls.PRIMARY_ROW))
        elif status in ("pending", "downloading"):
            # A label, not a button: there is nothing useful to press while
            # it is in flight, and a Download button that re-enqueues an
            # in-flight download reads as one that did nothing.
            btns.append(Text(_("Downloading…"), size="normal",
                             color=theme.SUBTLE_FG))
        elif not actions.offline:
            # Download, and only download. Read is the button that opens
            # one; a Download that also launched a reader would be a
            # surprise, and there is no way to ask for the copy without the
            # window if both buttons do the same thing.
            btns.append(controls.action_btn(
                "file_download", _("Download"), "bk-download",
                lambda: actions.download_book(item, server), size=controls.PRIMARY_ROW))
        if progress_settable(item):
            # Left out entirely for an epub: there is no number for the
            # user to read off and type (readers.md §2.1). The figure is
            # still read and shown in the meta line above; only setting it
            # is refused. See books.progress_settable.
            btns.append(controls.action_btn(
                "bookmark", _("Progress…"), "bk-progress",
                lambda: self.ctx.dialogs.open_book_progress(item, server),
                size=controls.PRIMARY_ROW))
        btns.append(controls.action_btn(
            # "Finished", not "Read". The verb and the state are the same
            # word in English, so with `Read` on both this page showed two
            # adjacent buttons with identical labels -- the _p contexts
            # tell translators them apart and give an English reader
            # nothing at all.
            "check", _("Finished"), "bk-watched",
            lambda: actions.toggle_watched(item, server),
            on=is_watched(item), size=controls.PRIMARY_ROW))
        btns.append(controls.action_btn(
            "favorite", _("Favorite"), "bk-fav",
            lambda: actions.toggle_favorite(item, server),
            on=bool((item.get("UserData") or {}).get("IsFavorite")),
            size=controls.PRIMARY_ROW))
        return Row(btns, gap=8, align="center")
