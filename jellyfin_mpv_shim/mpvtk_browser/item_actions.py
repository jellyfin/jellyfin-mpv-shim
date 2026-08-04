"""``ItemActions`` — the things a user does *to* an item.

Step 6c's second prep, and it exists for the same reason 6b did: the budget
said so. With rendering extracted, measuring the detail/series/season family
against ``PageContext`` still cost twelve-odd ``ctx.shell`` uses, and every
one of them was an action rather than a widget — toggle watched, favourite,
download, play, play the next episode.

So this is the action counterpart to :class:`~.tile_renderer.TileRenderer`.
Where that one turns items into pixels, this one turns a click into an
effect: it composes the raw capability (already on ``PlayerGateway``) with
the four things that make it a *user* action rather than an API call —

* run it off the loop thread, because every one of these is a round trip;
* apply it optimistically and roll back if the server refuses, because a
  tick that appears and silently reverts on the next reload is worse than
  one that never appeared;
* say something when it fails, because a swallowed error looks exactly like
  success;
* wake the render loop, because none of the above runs on it.

**Why the constructor looks like this.** ``services`` is a live provider,
read through and never snapshotted, exactly as ``TileRenderer.art`` is: the
browser swaps ``source`` and ``controller`` on a reconnect or a user switch,
and an action holding a stale controller would mutate the server the user
just left. The rest are callbacks for things only the shell can do — show a
dialog over the page, name the loading spinner, refresh the download badges.

That the list is this long is not an accident of the extraction; it is the
finding. A user action genuinely touches the data source, the player, the
dialog layer and the chrome. Bundling it here is what lets a *page* touch
none of them.
"""

import logging
import random

from ..i18n import _

log = logging.getLogger("mpvtk_browser.item_actions")


class ItemActions:
    """User actions on library items: play, mark, download."""

    def __init__(self, services, run, dialogs, on_launch,
                 on_downloads_changed=None):
        #: Live provider. Only ``source``, ``controller``, ``offline``,
        #: ``invalidate`` and ``set_status`` are read from it, and they are
        #: read at call time — see the module docstring.
        self.services = services
        #: An :class:`~.async_runner.AsyncRunner`.
        self.run = run
        #: Something offering ``confirm(text, on_yes, title, yes)`` and
        #: ``open_download(item)``. The dialog renders in the shell's layer,
        #: above the page, so the page never sees one.
        self.dialogs = dialogs
        #: ``on_launch(audio, title)`` — hand the window over (video) or show
        #: the now-playing bar (audio), and name the spinner before the queue
        #: has even resolved.
        self._on_launch = on_launch
        #: Called after a download is deleted, to re-read the badges.
        self._on_downloads_changed = on_downloads_changed or (lambda: None)
        #: Cached edit-capability probe; see can_edit.
        self._edit_ok = None
        #: Cached recording-capability probe; see can_record.
        self._record_ok = None

    # -- plumbing ----------------------------------------------------------

    def _fire(self, fn):
        """A fire-and-forget mutation. Nothing waits on the result, and a
        failure must not escape onto a pool worker's traceback."""
        def task():
            try:
                fn(self.services.controller)
            except Exception:
                log.warning("client action failed", exc_info=True)
        self.run.submit(task)

    def _launch(self, work):
        """Start playback off the loop thread.

        ``work(ctl)`` runs on a pool worker. Starting playback is seconds of
        work — build a Media, ask for PlaybackInfo, load the file under the
        player's lock — and running it inline froze the UI from the click
        until playback began.

        Deliberately NOT epoch-gated: the user pressed Play, and navigating
        elsewhere while it starts is not a reason to cancel it.
        """
        ctl = self.services.controller
        if ctl is None:
            return

        def failed(_exc):
            self.services.set_status(_("Playback could not be started."))
            self.services.invalidate()

        self.run.run(lambda: work(ctl), lambda _r: None, self.run.epoch,
                     on_error=failed)

    def edit(self, fn, on_ok=None, on_error=None, error=None):
        """A mutating edit whose failure the user must see.

        The swallowing variant made an "Add to Queue" the server rejected look
        exactly like one that worked. ``on_error`` undoes whatever the view
        already showed optimistically — leaving a rejected change on screen is
        worse than never showing it.
        """
        msg = error or _("The change could not be applied.")

        def work():
            fn(self.services.controller)

        def done(_ok):
            if on_ok is not None:
                on_ok()

        def failed(_exc):
            if on_error is not None:
                on_error()
            self.services.set_status(msg)

        self.run.run(work, done, self.run.epoch, on_error=failed)

    def skip_to(self, playlist_item_id):
        """Jump the player to a queue entry."""
        if playlist_item_id and self.services.controller is not None:
            self._fire(lambda c: c.skip_to(playlist_item_id))

    # -- playback ----------------------------------------------------------

    def play(self, item, server, offset_ticks=None, srcid=None, aid=None,
             sid=None):
        """Yield/keep-browse and start a single ``item``. Episodes queue the
        rest of the season so autoplay-next chains them (like the Tk
        browser)."""
        self._on_launch(audio=item.get("Type") == "Audio",
                        title=item.get("Name") or "")
        if self.services.controller is None:
            return
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            srv, iid, series = server, item.get("Id"), item.get("SeriesId")
            source = self.services.source

            # Queue fetch AND playback start on the same worker: the fetch
            # decides the queue the start consumes, and splitting them put
            # the start back on the loop thread via on_done.
            def work(ctl):
                try:
                    q = source.get_series_queue(srv, series,
                                                start_item_id=iid)
                    ids = [e.get("Id") for e in q if e.get("Id")] or [iid]
                except Exception:
                    log.debug("series queue fetch failed", exc_info=True)
                    ids = [iid]
                ctl.play_list(ids, srv, 0, offset_ticks=offset_ticks,
                              srcid=srcid, aid=aid, sid=sid)

            self._launch(work)
        else:
            self._launch(
                lambda ctl: ctl.play(item, server, offset_ticks=offset_ticks,
                                     srcid=srcid, aid=aid, sid=sid))

    def play_list(self, ids, server, start_index=0, audio=False, items=None,
                  pause_stills=True):
        """Play a whole list from ``start_index`` (album/playlist/song).

        ``items`` (the DTOs behind ``ids``) supplies the resume offset for
        the entry actually being started, as the Tk browser does —
        without it, clicking a half-watched entry restarted it from zero.

        The chosen entry is re-located by id after dropping empty ones:
        filtering first and trusting the caller's index shifted the queue
        out from under the entry that was clicked.

        ``pause_stills`` says whether a photo at the head of the queue opens
        paused -- true for "show me this picture", false for "run the
        slideshow". See PlayerManager.play."""
        start_id = ids[start_index] if 0 <= start_index < len(ids) else None
        offset = None
        title = ""
        if items is not None and 0 <= start_index < len(items):
            offset = ((items[start_index].get("UserData") or {})
                      .get("PlaybackPositionTicks")) or None
            # Names the spinner before the queue is even resolved.
            title = items[start_index].get("Name") or ""
        ids = [i for i in ids if i]
        if not ids:
            return
        try:
            pos = ids.index(start_id)
        except ValueError:
            pos = 0
        self._on_launch(audio=audio, title=title)
        self._launch(
            lambda ctl: ctl.play_list(ids, server, pos, offset_ticks=offset,
                                      pause_stills=pause_stills))

    def play_shuffle(self, ids, server, audio=True):
        ids = [i for i in ids if i]
        random.shuffle(ids)
        self.play_list(ids, server, 0, audio=audio)

    def queue_items(self, ids, server):
        # An edit, not a swallowed client call: this is a button press, so a
        # rejected "Add to Queue" must not look like one that worked.
        self.edit(
            lambda c: c.queue_items(server, [i for i in ids if i]),
            error=_("Those items could not be added to the queue."))

    def queue_next_items(self, ids, server):
        """Queue after the current item rather than at the end of the queue."""
        self.edit(
            lambda c: c.queue_next_items(server, [i for i in ids if i]),
            error=_("Those items could not be added to the queue."))

    def instant_mix(self, seed_id, server):
        ep = self.run.epoch
        source = self.services.source

        def work():
            return source.get_instant_mix(server, seed_id)

        def done(items):
            self.play_list([i.get("Id") for i in items], server, 0, audio=True)

        self.run.run(work, done, ep)

    def play_next_up(self, series_id, server):
        ep = self.run.epoch
        source = self.services.source

        def work():
            item = source.get_next_up(server, series_id)
            if item is None:
                # A series nobody has started has no "next up" — the button
                # did nothing at all. Start at the beginning, as Tk does.
                first = source.get_series_queue(server, series_id, limit=1)
                item = first[0] if first else None
            return item

        def done(item):
            if item:
                # Resume where it was left: Next Up on a part-watched
                # episode restarted it from zero.
                offset = ((item.get("UserData") or {})
                          .get("PlaybackPositionTicks")) or None
                self.play(item, server, offset_ticks=offset)

        self.run.run(work, done, ep)

    def shuffle_series(self, series_id, server):
        """Shuffle the whole show, like Tk's series-page Shuffle."""
        ep = self.run.epoch
        source = self.services.source

        def work():
            return [e.get("Id") for e in
                    source.get_series_queue(server, series_id, limit=200)
                    if e.get("Id")]

        def done(ids):
            if ids:
                self.play_shuffle(ids, server, audio=False)

        self.run.run(work, done, ep)

    # -- user data ---------------------------------------------------------

    def toggle_watched(self, item, server):
        from . import components

        ud = item.setdefault("UserData", {})
        was_played, was_count = ud.get("Played"), ud.get("UnplayedItemCount")
        new = not components.is_watched(item)
        ud["Played"] = new
        if item.get("Type") in ("Series", "Season"):
            ud["UnplayedItemCount"] = 0 if new else 1

        def work(ctl):
            # Roll the optimistic flip back if nothing recorded it (offline
            # un-watching, or nothing downloaded to queue against). Leaving
            # the tick up meant the UI claimed a change that never happened
            # and quietly reverted on the next reload.
            if ctl.set_watched(server, item.get("Id"), new) is False:
                ud["Played"] = was_played
                if was_count is None:
                    ud.pop("UnplayedItemCount", None)
                else:
                    ud["UnplayedItemCount"] = was_count
                self.services.invalidate()

        self._fire(work)
        self.services.invalidate()

    def toggle_favorite(self, item, server):
        ud = item.setdefault("UserData", {})
        was = ud.get("IsFavorite")
        new = not bool(was)
        ud["IsFavorite"] = new

        def work(ctl):
            # Roll back when nothing recorded it, as toggle_watched does —
            # offline the heart used to lie until the next reload.
            if ctl.set_favorite(server, item.get("Id"), new) is False:
                ud["IsFavorite"] = was
                self.services.invalidate()

        self._fire(work)
        self.services.invalidate()

    # -- live tv -----------------------------------------------------------
    #
    # Recording is not an optimistic action, unlike watched/favorite. The
    # server decides the timer's id, whether a "record this" became a series
    # rule, and what the padding is — so the caller re-reads afterwards
    # (``on_done``) rather than guessing. Getting that wrong here is worse
    # than a wrong tick: you find out at the point the programme did not
    # record.

    def schedule_recording(self, item, server, series=False, on_done=None):
        """Record ``item`` (a Program), or its whole series."""
        program_id = item.get("Id")
        if not program_id:
            return
        done = _("Series recording scheduled.") if series \
            else _("Recording scheduled.")

        def work(ctl):
            if series:
                ctl.create_series_timer(server, program_id)
            else:
                ctl.create_timer(server, program_id)

        self.edit(work,
                  on_ok=lambda: self._recording_changed(done, on_done),
                  error=_("The recording could not be scheduled."))

    def cancel_timer(self, timer_id, server, on_done=None):
        """Cancel a scheduled recording (or stop one in progress), after
        confirming — this is destructive and one stray click away from a
        Record button."""
        if not timer_id:
            return
        self.dialogs.confirm(
            _("Cancel this recording?"),
            lambda: self._do_cancel(
                lambda ctl: ctl.cancel_timer(server, timer_id),
                _("Recording cancelled."), on_done),
            title=_("Cancel Recording"), yes=_("Cancel Recording"))

    def cancel_series_timer(self, series_timer_id, server, on_done=None):
        """Stop recording a series."""
        if not series_timer_id:
            return
        self.dialogs.confirm(
            _("Stop recording this series?"),
            lambda: self._do_cancel(
                lambda ctl: ctl.cancel_series_timer(server, series_timer_id),
                _("Series recording cancelled."), on_done),
            title=_("Cancel Series"), yes=_("Cancel Series"))

    def _do_cancel(self, work, message, on_done):
        self.edit(work,
                  on_ok=lambda: self._recording_changed(message, on_done),
                  error=_("The recording could not be cancelled."))

    def save_timer(self, server, timer_id, changes, series=False,
                   on_done=None):
        """Apply an edited timer/series rule from the recording editor."""
        if not timer_id:
            return

        def work(ctl):
            if series:
                ctl.update_series_timer(server, timer_id, changes)
            else:
                ctl.update_timer(server, timer_id, changes)

        self.edit(work,
                  on_ok=lambda: self._recording_changed(_("Recording settings "
                                                          "saved."), on_done),
                  error=_("The recording settings could not be saved."))

    def _recording_changed(self, message, on_done):
        self.services.set_status(message)
        if on_done is not None:
            on_done()
        self.services.invalidate()

    # -- capability --------------------------------------------------------

    def can_record(self, server=None):
        """Whether the Record affordances should be offered.

        Two independent questions, and both have to say yes:

        1. Can this *apiclient* schedule a recording at all — the version
           probe ``live_tv_apis``, cached because it is a question about
           imported code and cannot change while we run.
        2. May this *user*, on this server. ``EnableLiveTvManagement`` is a
           third Live TV permission, separate from the access one the
           browse gate reads, and without it every Record button answers
           403 with a generic failure. Not cached here: it is per server
           and the answer already is cached, on the client.

        Fails OPEN in both, exactly as ``can_edit`` does and for the same
        reason: only a probe that positively answers False hides them. The
        API call is the real check.
        """
        if self._record_ok is None:
            try:
                answer = self.services.controller.live_tv_apis()
            except Exception:
                answer = None
            self._record_ok = answer is not False
        if not self._record_ok:
            return False
        source = getattr(self.services, "source", None)
        ask = getattr(source, "can_manage_live_tv", None)
        if ask is None or server is None:
            return True
        try:
            return bool(ask(server))
        except Exception:
            return True

    def can_edit(self):
        """Whether the apiclient can edit playlists/collections.

        Fails OPEN: only a probe that positively answers False hides the
        affordances. An inconclusive probe (a controller without the method)
        hiding every edit control is a worse failure than showing one that
        errors — and the API call is the real check anyway.

        Cached: this is read from build(), i.e. once a repaint.

        Was ``TilesMixin._edit_apis``.
        """
        if self._edit_ok is None:
            try:
                answer = self.services.controller.edit_apis()
            except Exception:
                answer = None
            self._edit_ok = answer is not False
        return self._edit_ok

    # -- downloads ---------------------------------------------------------

    @property
    def offline(self):
        """Whether there is a server to download from at all."""
        return self.services.offline

    def open_download(self, item):
        """Ask the shell for the download dialog (size estimate, options)."""
        self.dialogs.open_download(item)

    def confirm_remove_download(self, item):
        """Confirm, then delete this item's downloaded copy."""
        self.dialogs.confirm(
            _("Delete the downloaded copy of %s?") % item.get("Name", ""),
            lambda: self.remove_download(item),
            title=_("Delete Download"), yes=_("Delete"))

    def remove_download(self, item):
        """Delete this item's download, then refresh the badges."""
        iid, t = item.get("Id"), item.get("Type")
        ep = self.run.epoch
        ctl = self.services.controller

        def work():
            if t == "Series":
                ctl.delete_download(series_id=iid)
            elif t == "Season":
                ctl.delete_download(series_id=item.get("SeriesId"),
                                    season_id=iid)
            elif t == "Playlist":
                ctl.delete_download(playlist_id=iid)
            else:
                ctl.delete_download(item_id=iid)

        def done(_ok):
            self._on_downloads_changed()

        def failed(_exc):
            self.services.set_status(_("The download could not be removed."))

        self.run.run(work, done, ep, on_error=failed)
