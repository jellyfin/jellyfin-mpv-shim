"""Music browsing and the now-playing bar.

The music tabs (albums / artists / songs / genres / playlists), the album,
artist, genre and playlist views, and the persistent audio bar.

State on ``self``: ``_now_playing`` (the latest playstate snapshot, written
from a foreign thread by core's ``on_playstate``) and ``_np_thread`` (the
1s ticker that keeps the bar's clock moving). Paging
state lives in the route dict.
"""

from ..i18n import _
from ..mpvtk.widgets import Box, Column, Icon, Row, Slider, Text
from . import components, theme


# Height of the now-playing bar. Shared, because the cast screen has to
# leave room for it — a full-bleed page that sizes itself to the whole
# window pushes the bar off the bottom of the screen, which is exactly what
# happened: casting music to a headless box showed no transport at all.
NOW_PLAYING_BAR_H = 64


class MusicMixin:

    # Pure formatters; see components/labels.py.
    _duration = staticmethod(components.track_duration)
    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    #: Every music kind is now a Page (pages/music*.py, pages/playlist.py).
    #: What stays here is the now-playing bar, which is chrome rather than a
    #: route -- it is drawn by the shell over whatever page is showing.
    ROUTES: dict = {}



    def _queue_items(self, ids, server):
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

    def _now_playing_bar(self, w):
        np = self._now_playing
        pos = np.get("position", 0) or 0
        # While dragging, show where the handle IS, not where playback is.
        # The clock sat frozen at the old position for the whole gesture,
        # which is the one moment it is actually being read.
        scrub = self._np_scrub
        shown = pos if scrub is None else scrub
        dur = np.get("duration", 0) or 0
        pp = "play_arrow" if np.get("paused") else "pause"
        repeat = np.get("repeat", "none")

        # Every control here is icon-only, so the tooltip is the only thing
        # that names it — the playback HUD has had them since it shipped and
        # this bar was simply never given any.
        def tbtn(icon, node_id, cb, color=None, tip=None):
            color = color or theme.TEXT_FG
            return Box([Icon(icon, 22, color=color)], id=node_id, pad=8,
                       bg=theme.BUTTON_BG, hover={"fill": theme.BUTTON_ACTIVE},
                       radius=6, align="center", direction="row", on_click=cb,
                       tip=tip)

        # commit-only: dragging shouldn't spam absolute seeks mid-gesture
        seek = Slider("np-seek", value=pos, min=0, max=max(1, dur),
                      force=True, flex=1,
                      on_change=self._np_scrub_change,
                      on_commit=self._np_scrub_commit,
                      on_cancel=self._np_scrub_cancel)
        title = np.get("title", "")
        sub = np.get("artist") or np.get("album") or ""
        return Row(
            [
                Column([Text(title, size=16, bold=True),
                        Text(sub, size=13, color=theme.SUBTLE_FG)],
                       gap=2, w=220),
                tbtn("skip_previous", "np-prev",
                     lambda: self._ctl(lambda c: c.prev()),
                     tip=_("Previous")),
                tbtn(pp, "np-pp", lambda: self._ctl(lambda c: c.toggle_pause()),
                     tip=_("Play") if np.get("paused") else _("Pause")),
                tbtn("skip_next", "np-next",
                     lambda: self._ctl(lambda c: c.next()), tip=_("Next")),
                tbtn("stop", "np-stop", lambda: self._ctl(lambda c: c.stop()),
                     tip=_("Stop")),
                Text(self._fmt(shown), size=14, w=48,
                     color=theme.SUBTLE_FG),
                seek,
                Text(self._fmt(dur), size=14, w=48, color=theme.SUBTLE_FG),
                tbtn("favorite" if np.get("favorite") else "favorite_border",
                     "np-fav", lambda: self._toggle_np_favorite(),
                     color=(theme.FAV_RED if np.get("favorite")
                            else theme.TEXT_FG),
                     tip=(_("Remove from Favorites") if np.get("favorite")
                          else _("Add to Favorites"))),
                tbtn("repeat_one" if repeat == "one" else "repeat", "np-repeat",
                     lambda: self._cycle_repeat(),
                     color=(theme.ACCENT if repeat != "none"
                            else theme.SUBTLE_FG),
                     tip=self._REPEAT_TIPS.get(repeat, _("Repeat"))),
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
            ] + ([] if self.headless else [
                # Dropped in headless: the queue is a normal route, and
                # normal routes render the nav chrome — so casting a song to
                # a locked box would otherwise hand over the whole library
                # in two clicks. Queue PLAYBACK still works; only the view
                # is unreachable.
                tbtn("queue_music", "np-queue", self._open_queue,
                     tip=_("Queue")),
            ]) + [
            ],
            pad=10, gap=10, align="center", h=NOW_PLAYING_BAR_H,
            bg=theme.PANEL_BG)
