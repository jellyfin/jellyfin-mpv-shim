"""Music browsing and the now-playing bar.

The music tabs (albums / artists / songs / genres / playlists), the album,
artist, genre and playlist views, and the persistent audio bar.

State on ``self``: ``_now_playing`` (the latest playstate snapshot, written
from a foreign thread by core's ``on_playstate``) and ``_np_thread`` (the
1s ticker that keeps the bar's clock moving). Paging
state lives in the route dict.
"""

from ..i18n import _
from ..mpvtk.widgets import (Box, Column, Dropdown, Icon, Row, Slider,
                             Text)
from . import components, theme


# Height of the now-playing bar. Shared, because the cast screen has to
# leave room for it — a full-bleed page that sizes itself to the whole
# window pushes the bar off the bottom of the screen, which is exactly what
# happened: casting music to a headless box showed no transport at all.
NOW_PLAYING_BAR_H = 64

# ...and the height when the scrubber is on its own row above the controls.
# Two rows rather than one because on a book the scrubber is the control
# that matters most and the row is the one with the most in it: on a single
# ten-hour .m4b, dragging the bar is how you move around the book, and it
# was the thing being squeezed to make room for everything else.
NOW_PLAYING_BAR_H2 = 96


def now_playing_bar_h(state, width):
    """How tall the bar will be for ``state`` at ``width``.

    A function rather than a constant because the layout is now a choice —
    and the two callers that subtract it (the content-height calculation
    and the cast screen) have to subtract what will actually be drawn, or
    the page is laid out against the wrong remaining height.
    """
    return (NOW_PLAYING_BAR_H2 if MusicMixin.np_two_row(state or {}, width)
            else NOW_PLAYING_BAR_H)


class MusicMixin:

    # Pure formatters; see components/labels.py.
    _duration = staticmethod(components.track_duration)
    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    #: Every music kind is now a Page (pages/music*.py, pages/playlist.py).
    #: What stays here is the now-playing bar, which is chrome rather than a
    #: route -- it is drawn by the shell over whatever page is showing.
    ROUTES: dict = {}



    def _queue_items(self, ids, server, next_up=False):
        if next_up:
            self._actions.queue_next_items(ids, server)
        else:
            self._actions.queue_items(ids, server)

    # -------------------------------------------------- now-playing bar

    @staticmethod
    def _fmt(secs):
        secs = int(secs or 0)
        return "%d:%02d" % (secs // 60, secs % 60)

    def _ctl(self, fn):
        if self.controller is not None:
            fn(self.controller)

    _REPEAT = ["none", "all", "one"]

    # What the button will do next, not what is currently set — the icon
    # already shows the state, and a tooltip that repeats it is no help.
    @property
    def _REPEAT_TIPS(self):
        return {"none": _("Repeat All"), "all": _("Repeat One"),
                "one": _("Repeat Off")}

    def _cycle_repeat(self):
        np = self._now_playing or {}
        cur = np.get("repeat", "none")
        nxt = self._REPEAT[(self._REPEAT.index(cur) + 1) % 3] \
            if cur in self._REPEAT else "all"
        np["repeat"] = nxt
        self._ctl(lambda c: c.set_repeat(nxt))
        self.invalidate()

    def _toggle_np_favorite(self):
        np = self._now_playing or {}
        np["favorite"] = not np.get("favorite")
        self._ctl(lambda c: c.toggle_favorite())
        self.invalidate()

    def _np_scrub_change(self, v):
        """Track the drag so the elapsed clock follows the handle.

        No pause, unlike the video HUD: pausing to inspect a frame makes
        sense; pausing a song because you touched the seek bar does not.
        The seek itself still only fires on release."""
        self._np_scrub = float(v)
        self.invalidate()

    def _np_scrub_commit(self, v):
        self._np_scrub = None
        self._ctl(lambda c: c.seek(float(v)))
        self.invalidate()

    def _np_scrub_cancel(self):
        self._np_scrub = None
        self.invalidate()

    @staticmethod
    def _chapter_index(chapters, pos):
        """Which chapter ``pos`` falls in. The last one that has started —
        the same rule the video HUD's picker uses."""
        current = 0
        for i, chapter in enumerate(chapters):
            if chapter["time"] <= pos:
                current = i
        return current

    #: How far the audiobook skip buttons jump. Asymmetric on purpose, and
    #: these are the numbers the playback HUD's own step buttons use: back
    #: is "I missed that, say it again" and forward is "get past the recap",
    #: which are different sized problems.
    SKIP_BACK = 10
    SKIP_FORWARD = 30

    #: What one square transport button costs the row, including the gap
    #: before it. Icon 20 + 7px padding either side, + the Row's gap.
    NP_BTN_W = 20 + 2 * 7 + 10
    #: A 48px clock Text plus its gap, and there are two of them.
    NP_CLOCK_W = 2 * (48 + 10)
    #: The volume icon and its slider, both gaps included.
    NP_VOL_W = (20 + 10) + (110 + 10)
    #: The chapter picker: a 38px chip plus its gap.
    NP_PICKER_W = 38 + 10

    #: The narrowest the scrubber may be squeezed to. It is the one control
    #: on this bar that has to be *dragged*, so it is the one that must not
    #: be what absorbs a shortfall — which is exactly what it was doing:
    #: the row is fixed-height and flex-width, so every control added to it
    #: came straight out of the slider, and past about a dozen the slider
    #: was zero pixels wide at almost every window size.
    NP_MIN_SEEK = 140

    #: Below this, the bar goes to two rows even for music.
    #:
    #: Measured rather than chosen: a one-row bar at 1000px gives the
    #: scrubber 264px and keeps five optional controls; two rows at the
    #: same width give it 872px and keep all ten. The single row is a space
    #: optimisation, and below roughly this width there is no space for it
    #: to be optimising. Crossing it is the one place the bar gains
    #: controls as the window NARROWS -- which is honest, because it also
    #: gains a row to put them on.
    NP_TWO_ROW_UNDER = 1100

    @staticmethod
    def np_two_row(np, w):
        """Whether the scrubber gets its own row above the controls.

        Always for an audiobook: a book is one long item and the scrubber
        is how you move around inside it, so it earns full width rather
        than whatever is left after eleven buttons. And for anything else
        on a narrow window, where the alternative is a scrubber a
        centimetre wide between two clocks.

        Static so the shell can ask how tall the bar will be before it
        builds one -- the page above has to be laid out against the height
        that will actually be drawn.
        """
        return bool(np.get("is_audiobook")) or w < MusicMixin.NP_TWO_ROW_UNDER

    def _np_plan(self, np, w):
        """Which optional controls this bar has room for.

        A *priority list* rather than a table of per-control width
        thresholds. Two reasons, and the second is the one that matters:

        * thresholds are guesses about a sum, and they were wrong — the
          bar fitted its own tier table at every width and still had a
          zero-width scrubber, because nothing checked the total;
        * what is worth dropping depends on what is playing. Chapter
          navigation outranks the favourite heart while a book is on and
          does not exist at all while a song is, so one fixed order cannot
          serve both.

        The longest PREFIX that fits is kept, not a greedy best-fit: a
        prefix is monotone in width, so narrowing the window only ever
        takes things away, and widening it only ever brings them back in
        the same order. A best fit would pop a cheap control in and out as
        an expensive one came and went.
        """
        book = bool(np.get("is_audiobook"))
        chapters = bool(np.get("chapters"))
        btn = self.NP_BTN_W
        # Most valuable first, so the LAST entries are the first to go.
        #
        # The tail is the reported order: repeat goes first, then the
        # heart, then the chapter step buttons. Repeat and favourite are
        # both one-press state you can set once and forget, and neither is
        # anything to do with getting through what is playing; the chapter
        # arrows are, which is why they outlive both. They go before the
        # chapter LIST because the list can still reach every chapter on
        # its own -- losing the arrows costs a shortcut, losing the list
        # costs the only way to jump.
        order = [("clock", self.NP_CLOCK_W)]
        if book:
            # ±10/30s outlive everything else on a book: they are the two
            # gestures people actually make while listening to one.
            order.append(("skip_btns", 2 * btn))
        order.append(("volbar", self.NP_VOL_W))
        if chapters:
            order.append(("chapters", self.NP_PICKER_W))
        order += [("queue", btn), ("stop", btn)]
        if book and chapters:
            # `book and`, and the two readers below say the same — they
            # drifted apart once, and the tier dict only holds keys that
            # were appended, so the weaker read raised KeyError and the
            # browser stopped repainting for the rest of playback. Any
            # chaptered audio that is not an AudioBook reached it: a
            # podcast or an m4b in a music library. The chapter PICKER is
            # still offered to those (above, under plain `chapters`); it is
            # the prev/next pair that is a book affordance, exactly as
            # skip_btns is.
            order.append(("ch_btns", 2 * btn))
        order += [("favorite", btn), ("repeat", btn)]

        # Everything the controls row always draws: its padding, the title
        # column and play/pause. On one row the scrubber and the two clocks
        # share that width too; on two they have a row of their own, so the
        # controls row is free to keep more.
        used = 2 * 10 + self._np_title_w(w) + 10 + btn
        if self.np_two_row(np, w):
            # The clock lives on the scrubber's row, where there is always
            # room for it, so it stops competing with the buttons.
            order = [(k, c) for k, c in order if k != "clock"]
        else:
            used += 2 * btn + self.NP_MIN_SEEK
        if not self._np_solo_chapters(np):
            used += 2 * btn
        plan = {key: False for key, _cost in order}
        plan["clock"] = plan.get("clock") or self.np_two_row(np, w)
        for key, cost in order:
            if used + cost > w:
                break               # prefix, so stop rather than skip
            used += cost
            plan[key] = True
        return plan

    #: The title/artist column. One width, deliberately, rather than a
    #: ladder that narrows with the window.
    #:
    #: The ladder was worse than it looked: shrinking the title frees more
    #: room than the cheapest control costs, so crossing one of its steps
    #: *gained* a button as the window narrowed. Two of those, plus the
    #: two-row threshold, meant three widths where controls popped in as
    #: you dragged the edge inwards. One constant leaves exactly one
    #: layout boundary, and the title truncates like any other text.
    NP_TITLE_W = 200

    @classmethod
    def _np_title_w(cls, w):
        return cls.NP_TITLE_W

    def _np_btn(self, icon, node_id, cb, tip, size=20, pad=7, color=None):
        """A square transport button, matching the bar's own row."""
        return Box([Icon(icon, size, color=color or theme.TEXT_FG)],
                   id=node_id, pad=pad, bg=theme.BUTTON_BG,
                   hover={"fill": theme.BUTTON_ACTIVE}, radius=6,
                   align="center", direction="row", on_click=cb, tip=tip)

    def _transport(self, np, tiers):
        """The buttons around play/pause, in the playback HUD's order.

        ``prev | chapter- | -10s | play/pause | +30s | chapter+ | next``.
        That ordering is the HUD's and worth matching exactly: the two
        pairs nest around the middle button, so the further from the centre
        a control is, the bigger the jump it makes. Anyone who has used the
        video HUD already knows where to press.

        The chapter pair uses ``undo``/``redo`` for the same reason — those
        are the glyphs the HUD gives chapter navigation, and ``fast_rewind``
        /``fast_forward`` read as scanning, which is not what they do.
        """
        pp = "play_arrow" if np.get("paused") else "pause"
        chapters = bool(np.get("chapters"))
        book = bool(np.get("is_audiobook"))
        # A chaptered book alone in the queue has nothing to skip TO. The
        # two buttons would sit either side of the chapter arrows -- the
        # ones that are the real answer here -- and do nothing but end
        # playback. See _np_solo_chapters.
        walk = not self._np_solo_chapters(np)
        out = [self._np_btn("skip_previous", "np-prev",
                            lambda: self._ctl(lambda c: c.prev()),
                            _("Previous"))] if walk else []
        if book and chapters and tiers["ch_btns"]:
            # Through the player's own chapter_seek, not a seek computed
            # here: "previous chapter" means re-start the current one
            # unless you press it in its first couple of seconds, and that
            # rule already lives in PlayerManager.chapter_target -- where
            # the HUD and the mouse buttons also read it. A second copy
            # would drift, and would go round SyncPlay.
            out.append(self._np_btn(
                "undo", "np-chprev",
                lambda: self._ctl(lambda c: c.chapter_seek(-1)),
                _("Previous Chapter")))
        if book and tiers["skip_btns"]:
            out.append(self._np_btn(
                "replay_10", "np-back",
                lambda: self._ctl(
                    lambda c: c.seek_relative(-self.SKIP_BACK)),
                _("Back %d seconds") % self.SKIP_BACK))
        out.append(self._np_btn(
            pp, "np-pp", lambda: self._ctl(lambda c: c.toggle_pause()),
            _("Play") if np.get("paused") else _("Pause")))
        if book and tiers["skip_btns"]:
            out.append(self._np_btn(
                "forward_30", "np-forward",
                lambda: self._ctl(
                    lambda c: c.seek_relative(self.SKIP_FORWARD)),
                _("Forward %d seconds") % self.SKIP_FORWARD))
        if book and chapters and tiers["ch_btns"]:
            out.append(self._np_btn(
                "redo", "np-chnext",
                lambda: self._ctl(lambda c: c.chapter_seek(1)),
                _("Next Chapter")))
        if walk:
            out.append(self._np_btn("skip_next", "np-next",
                                    lambda: self._ctl(lambda c: c.next()),
                                    _("Next")))
        return out

    @staticmethod
    def _np_solo_chapters(np):
        """A chaptered item that is the whole queue.

        Which is what a single-file audiobook is: one `.m4b` IS the book,
        so previous/next track have nowhere to go — pressing either can
        only end playback. Worse, they sit immediately outside the chapter
        arrows, so the two pairs read as a set and half of them are traps.

        Deliberately narrow: it takes BOTH conditions. A rip's chapters are
        queue entries and prev/next are exactly right there, and a lone
        song without chapters keeps them because that is what the bar has
        always done for music.
        """
        return bool(np.get("chapters")) and (np.get("queue_len") or 1) <= 1

    def _chapter_picker(self, np, pos, tiers):
        """The chapter LIST, in the right-hand cluster with the other
        pickers — where the HUD keeps its own.

        It belongs there rather than among the transport buttons because it
        is not a step: it is a place to choose from, like the queue button
        it now sits beside. Wedged between the scrubber and the volume it
        also stranded the two chapter arrows away from the play button they
        step around.
        """
        chapters = np.get("chapters") or []
        if not chapters or not tiers["chapters"]:
            return []
        labels = ["%s  %s" % (self._fmt(ch["time"]),
                              ch["title"] or _("Chapter %d") % (i + 1))
                  for i, ch in enumerate(chapters)]
        return [Dropdown(
            "np-chapters", labels,
            selected=self._chapter_index(chapters, pos), force=True,
            trigger_icon="bookmark", tip=_("Chapters"),
            # trigger_chip, not the HUD's chromeless glyph: this sits in a
            # row of filled square buttons on panel chrome, and a bare icon
            # among them reads as a different kind of control. The HUD's
            # style is right where it is -- floating over video.
            trigger_chip=(theme.BUTTON_BG, theme.BUTTON_ACTIVE,
                          theme.TEXT_FG),
            w=38, h=38,
            # The list is chapter TITLES, which are as long as the author
            # made them; the trigger is one icon wide and cannot size it.
            popup_w=420,
            on_select=lambda i, v, chs=chapters: self._ctl(
                lambda c: c.seek(chs[i]["time"])))]

    def _now_playing_bar(self, w):
        np = self._now_playing
        tiers = self._np_plan(np, w)
        pos = np.get("position", 0) or 0
        # While dragging, show where the handle IS, not where playback is.
        # The clock sat frozen at the old position for the whole gesture,
        # which is the one moment it is actually being read.
        scrub = self._np_scrub
        shown = pos if scrub is None else scrub
        dur = np.get("duration", 0) or 0
        repeat = np.get("repeat", "none")

        # commit-only: dragging shouldn't spam absolute seeks mid-gesture
        seek = Slider("np-seek", value=pos, min=0, max=max(1, dur),
                      force=True, flex=1,
                      # A floor as well as a plan: _np_plan decides what to
                      # draw, this stops the layout crushing the scrubber
                      # if the estimate is ever short.
                      min_w=self.NP_MIN_SEEK,
                      # Chapter slits, as the video HUD's seek bar has. On a
                      # ten-hour .m4b the bar is the only thing that says
                      # where you are IN THE BOOK rather than in the file --
                      # without them a scrub is a blind drag.
                      marks=[ch["time"] / dur
                             for ch in (np.get("chapters") or [])
                             if 0 < ch["time"] < dur] or None,
                      on_change=self._np_scrub_change,
                      on_commit=self._np_scrub_commit,
                      on_cancel=self._np_scrub_cancel)
        title = np.get("title", "")
        sub = np.get("artist") or np.get("album") or ""
        title_w = self._np_title_w(w)
        clock = [Text(self._fmt(shown), size="caption", w=48,
                      color=theme.SUBTLE_FG)] if tiers["clock"] else []
        end = [Text(self._fmt(dur), size="caption", w=48,
                    color=theme.SUBTLE_FG)] if tiers["clock"] else []

        right = []
        if tiers["stop"]:
            # With the transport centred, stop belongs on the right rather
            # than at the end of it: it is not a step through the media, it
            # ends playback -- and sitting immediately after Next it was one
            # slip from a skip. The playback HUD has no stop button at all
            # for the same reason (its back arrow yields to the library).
            right.append(self._np_btn(
                "stop", "np-stop", lambda: self._ctl(lambda c: c.stop()),
                _("Stop")))
        if tiers["favorite"]:
            right.append(self._np_btn(
                "favorite" if np.get("favorite") else "favorite_border",
                "np-fav", lambda: self._toggle_np_favorite(),
                (_("Remove from Favorites") if np.get("favorite")
                 else _("Add to Favorites")),
                color=(theme.FAV_RED if np.get("favorite")
                       else theme.TEXT_FG)))
        right += self._chapter_picker(np, shown, tiers)
        if tiers["repeat"]:
            right.append(self._np_btn(
                "repeat_one" if repeat == "one" else "repeat", "np-repeat",
                lambda: self._cycle_repeat(),
                self._REPEAT_TIPS.get(repeat, _("Repeat")),
                color=(theme.ACCENT if repeat != "none"
                       else theme.SUBTLE_FG)))
        if tiers["volbar"]:
            right += [
                Icon("volume_up", 20, color=theme.SUBTLE_FG),
                # Live for audible feedback, but only the release notifies:
                # on_change fires per mouse-move, and a notifying set_volume
                # wakes the timeline thread, which posts to the server.
                Slider("np-vol", value=np.get("volume", 100), min=0, max=100,
                       w=110, tip=_("Volume"),
                       on_change=lambda v: self._ctl(
                           lambda c: c.set_volume(v, notify=False)),
                       on_commit=lambda v: self._ctl(
                           lambda c: c.set_volume(v))),
            ]
        if tiers["queue"] and not self.headless:
            # Dropped in headless: the queue is a normal route, and normal
            # routes render the nav chrome — so casting a song to a locked
            # box would otherwise hand over the whole library in two
            # clicks. Queue PLAYBACK still works; only the view is
            # unreachable.
            right.append(self._np_btn("queue_music", "np-queue",
                                      self._open_queue, _("Queue")))

        scrub_row = Row(clock + [seek] + end, gap=10, align="center")
        title_col = Column([Text(title, size="normal", bold=True),
                            Text(sub, size="caption", color=theme.SUBTLE_FG)],
                           gap=2, w=title_w)

        if not self.np_two_row(np, w):
            # One row: the scrubber shares it with everything else and takes
            # whatever is left, so there is nothing to centre against --
            # the scrubber IS the middle.
            return Row([title_col] + self._transport(np, tiers)
                       + clock + [seek] + end + right,
                       pad=10, gap=10, align="center",
                       h=NOW_PLAYING_BAR_H, bg=theme.PANEL_BG)

        # Two rows: the scrubber gets the full width above the controls.
        # It is the control that matters most on a book -- dragging it is
        # how you move around a ten-hour item -- and it was the one being
        # squeezed to make room for the buttons.
        #
        # The controls row is three parts with EQUAL FLEX on the outer two,
        # which is what actually centres the transport in the window. A
        # single trailing Spacer only left-packs it, and flexing the gaps
        # either side of a fixed title and a variable right-hand cluster
        # centres it between them rather than in the bar -- so it drifts as
        # controls are shed.
        return Column([
            scrub_row,
            Row([Box([title_col], flex=1, direction="row", align="center"),
                 Row(self._transport(np, tiers), gap=10, align="center"),
                 Box(right, flex=1, direction="row", align="center",
                     justify="end", gap=10)],
                gap=10, align="center"),
        ], pad=(6, 10), gap=4, align="stretch", h=NOW_PLAYING_BAR_H2,
            bg=theme.PANEL_BG)