"""Modal dialogs.

The generic shell (``_show_dialog`` / ``_close_dialog`` / ``_message`` /
``_confirm``), the add-to playlist/collection picker, the download dialog
and the SyncPlay group dialog.

State on ``self``: ``_dialog`` — a builder callable or None — is the single
modal slot, rendered by core's ``build()``. Also ``_addto_build``,
``_addto_ids``, ``_addto_explicit_ids`` and ``_addcol_name`` (add-to
dialog), and ``_dl`` (download dialog). All are loop-thread only.
"""

from ..i18n import _
from ..mpvtk.widgets import (
    Button,
    Checkbox,
    Column,
    Dialog,
    Row,
    Spacer,
    Text,
    TextBox,
    VScroll,
)
from . import theme


class DialogsMixin:

    # ----------------------------------------------------- add to playlist

    def _open_add_to(self, item, server=None):
        server = server or self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        ep = self._epoch
        # A music container is not itself a playlist entry — Tk resolves
        # album/artist/genre to their track ids before offering the dialog.
        self._addto_ids = None
        # ids the caller supplied outright (the play queue), as opposed to
        # ones resolved from a container
        self._addto_explicit_ids = item.get("_ids")
        parent = self.route.get("parent_id")

        def work():
            def fetch(fn):
                try:
                    return fn(server)
                except Exception:
                    return []
            return (fetch(self.source.get_playlists),
                    fetch(getattr(self.source, "get_collections",
                                  lambda _s: [])),
                    item.get("_ids")
                    or self._resolve_play_ids(item, server, parent))
        self.run_async(
            work, lambda r: self._show_add_to(server, item, r[0], r[1], r[2]),
            ep)

    # Height of a picker list inside a dialog before it scrolls. Enough to
    # show several entries without the dialog growing past the window.
    PICKER_H = 240

    def _picker_list(self, node_id, entries, on_pick, empty_text):
        """Scrollable list of {Id, Name} buttons for a dialog.

        Scrollable, not a dropdown: these are the primary choice in the
        dialog, and a flat list of every playlist made it unusably tall.
        """
        if not entries:
            return Text(empty_text, size=15, color=theme.SUBTLE_FG)
        rows = [Button(e.get("Name", ""), id="%s-%d" % (node_id, i),
                       on_click=lambda eid=e.get("Id"): on_pick(eid))
                for i, e in enumerate(entries)]
        return VScroll(Column(rows, gap=6, align="stretch"),
                       id=node_id, h=self.PICKER_H)

    def _show_add_to(self, server, item, playlists, collections=(),
                     item_ids=None):
        item_id = item.get("Id")
        self._addto_ids = [i for i in (item_ids or [item_id]) if i]
        # Private by default, matching the Tk browser: the server creates
        # playlists public unless told otherwise.
        self._addto_name = {"name": "", "private": True}

        def build():
            named = bool((self._addto_name.get("name") or "").strip())
            rows = [
                Text(_("Add to Playlist"), size=22, bold=True),
                self._picker_list(
                    "add-pl", playlists,
                    lambda pid: self._add_to(server, pid, item_id),
                    _("No playlists yet.")),
                Row([
                    # Enter creates, same as the button beside it. Typing a
                    # name and pressing Enter did nothing.
                    TextBox("add-newname", placeholder=_("New playlist name…"),
                            w=280,
                            on_change=lambda v: self._addto_name_changed(v),
                            on_submit=lambda v: self._add_to_new(
                                server, item_id)),
                    Button(_("Create"), id="add-create",
                           on_click=lambda: self._add_to_new(server, item_id)),
                ], gap=10, align="center"),
            ]
            if named:
                # Only meaningful once there's a playlist to be private:
                # an always-on checkbox above an empty name box is noise.
                rows.append(Checkbox(
                    _("Private (only you can see it)"),
                    bool(self._addto_name.get("private")), id="add-private",
                    on_toggle=lambda: self._addto_name.__setitem__(
                        "private", not self._addto_name.get("private"))))
            buttons = []
            # Gated on whether the SOURCE does collections, not on whether
            # any exist — gating on the latter meant you could never create
            # your first one. The offline catalog has none either way.
            if hasattr(self.source, "get_collections") and not self._offline:
                buttons.append(Button(
                    _("Collections…"), id="add-collections",
                    on_click=lambda: self._show_add_to_collection(
                        server, item, collections)))
            buttons.append(Button(_("Close"), id="add-close",
                                  on_click=self._close_dialog))
            rows.append(self._dialog_buttons(buttons))
            return Dialog("addto",
                          self._dialog_shell("addto", rows, w=460),
                          on_dismiss=self._close_dialog)
        self._addto_build = build
        self._show_dialog(build)

    def _addto_name_changed(self, value):
        """Rebuild only when the name crosses empty <-> non-empty, which is
        what shows or hides the Private checkbox. Rebuilding on every
        keystroke would be pointless churn."""
        was = bool((self._addto_name.get("name") or "").strip())
        self._addto_name["name"] = value
        if bool((value or "").strip()) != was and self._addto_build:
            self._show_dialog(self._addto_build)

    def _show_add_to_collection(self, server, item, collections):
        """Collections get their own window: two long lists stacked in one
        dialog was the crowding."""
        item_id = item.get("Id")
        self._addcol_name = {"name": ""}

        def build():
            rows = [
                Text(_("Add to Collection"), size=22, bold=True),
                self._picker_list(
                    "add-col", collections,
                    lambda cid: self._add_to_col(server, cid, item_id),
                    _("No collections yet.")),
                Row([
                    TextBox("addcol-newname",
                            placeholder=_("New collection name…"), w=280,
                            on_change=lambda v: self._addcol_name.__setitem__(
                                "name", v),
                            on_submit=lambda v: self._add_to_new_col(
                                server, item_id)),
                    Button(_("Create"), id="addcol-create",
                           on_click=lambda: self._add_to_new_col(
                               server, item_id)),
                ], gap=10, align="center"),
                self._dialog_buttons([
                    Button(_("Back"), id="addcol-back",
                           on_click=lambda: self._show_dialog(
                               self._addto_build)),
                    Button(_("Close"), id="addcol-close",
                           on_click=self._close_dialog)]),
            ]
            return Dialog("addtocol",
                          self._dialog_shell("addtocol", rows, w=460),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _add_to_new(self, server, item_id):
        state = self._addto_name or {}
        name = state.get("name", "").strip()
        ids = self._addto_ids or ([item_id] if item_id else [])
        if name and ids:
            private = bool(state.get("private", True))
            self._edit_call(lambda c: c.playlist_new(
                server, name, ids, is_public=not private))
        self._close_dialog()

    def _add_to_new_col(self, server, item_id):
        name = (self._addcol_name or {}).get("name", "").strip()
        ids = self._collection_ids(item_id)
        if name and ids:
            self._edit_call(lambda c: c.collection_new(server, name, ids))
        self._close_dialog()

    def _collection_ids(self, item_id):
        """A collection holds the album, not its 300 tracks — only a
        playlist wants the resolved ids (Tk resolves for playlists only).

        The queue is the exception: it is a set of items with no container
        of its own, so it carries an explicit id list."""
        if self._addto_explicit_ids:
            return list(self._addto_explicit_ids)
        return [item_id] if item_id else []

    def _add_to_col(self, server, collection_id, item_id):
        ids = self._collection_ids(item_id)
        if collection_id and ids:
            self._edit_call(lambda c: c.collection_add(
                server, collection_id, ids))
        self._close_dialog()

    def _add_to(self, server, playlist_id, item_id):
        ids = self._addto_ids or ([item_id] if item_id else [])
        if playlist_id and ids:
            self._edit_call(lambda c: c.playlist_add(server, playlist_id, ids))
        self._close_dialog()

    # -------------------------------------------------------- downloads

    @staticmethod
    def _human_size(n):
        n = float(n or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return ("%d %s" % (n, unit) if unit == "B"
                        else "%.1f %s" % (n, unit))
            n /= 1024

    def _open_download(self, item):
        server = self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        # The include-watched filter is only meaningful for a container.
        # For a single item it must be True, or Download on something you
        # have already watched enqueues nothing at all, silently.
        container = item.get("Type") in ("Series", "Season", "Playlist",
                                         "MusicAlbum", "MusicArtist",
                                         "BoxSet")
        self._dl = {"server": server, "item": item, "est": None,
                    "container": container, "watched": not container}
        ep = self._epoch

        def work():
            return self.controller.download_estimate(
                server, item.get("Id"), item.get("Type"))

        def done(est):
            if self._dl is not None:
                self._dl["est"] = est
                if self._dl["container"]:
                    self._dl["watched"] = bool((est or {}).get("audio_only"))
            self._show_download()

        def failed(_exc):
            # Say the estimate failed rather than leaving the dialog on its
            # "estimating…" state forever. The controller used to return a
            # zero estimate here, which the dialog rendered as "Nothing left
            # to download." — a server error reported as success, with the
            # Download button withheld.
            if self._dl is not None:
                self._dl["error"] = _("Could not check what needs "
                                      "downloading.")
            self._show_download()

        self.run_async(work, done, ep, on_error=failed)
        self._show_download()   # show immediately with an "estimating" state

    def _show_download(self):
        dl = self._dl
        if dl is None:
            return

        def build():
            est = dl["est"]
            if dl.get("error"):
                info = Text(dl["error"], size=15, color=theme.FAV_RED)
            elif est is None:
                info = Text(_("Estimating…"), size=15, color=theme.SUBTLE_FG)
            else:
                line = _("%(count)d items · %(size)s") % {
                    "count": est.get("count", 0),
                    "size": self._human_size(est.get("total_bytes", 0))}
                extra = []
                if est.get("already_count"):
                    extra.append(_("%d already downloaded")
                                 % est["already_count"])
                if est.get("watched_count"):
                    extra.append(_("%d watched") % est["watched_count"])
                if extra:
                    line += "   (" + ", ".join(extra) + ")"
                info = Text(line, size=15, color=theme.SUBTLE_FG)
            return Dialog("download", self._dialog_shell("download", [
                Text(_("Download"), size=22, bold=True),
                Text(dl["item"].get("Name", ""), size=17),
                info,
            ] + ([Checkbox(_("Include watched"), dl["watched"],
                           id="dl-watched",
                           on_toggle=self._dl_toggle_watched)]
                 if dl["container"] else []) + [
                self._dialog_buttons([
                    Button(_("Cancel"), id="dl-cancel",
                           on_click=self._close_download),
                    # Confirming before the estimate lands loses the
                    # audio_only default and skips played tracks. And an
                    # estimate of nothing means there is nothing to fetch —
                    # everything here is already downloaded — so offering
                    # Download is a dead click. Tk guarded on the count.
                    Button(_("Download"), id="dl-ok",
                           on_click=self._dl_confirm)
                    if est is not None and est.get("count", 0) else
                    Text(_("Estimating…") if est is None
                         else _("Nothing left to download."),
                         size=15, color=theme.SUBTLE_FG)]),
            ], w=460), on_dismiss=self._close_download)
        self._show_dialog(build)

    def _dl_toggle_watched(self):
        if self._dl is not None:
            self._dl["watched"] = not self._dl["watched"]
            self._show_download()

    def _close_download(self):
        self._dl = None
        self._close_dialog()

    def _dl_confirm(self):
        dl = self._dl
        self._close_download()
        if dl is None:
            return
        item = dl["item"]
        # _edit_call, not _client_call: the latter swallows, so a rejected
        # download looked exactly like a queued one and the item just never
        # turned up.
        self._edit_call(
            lambda c: c.download_enqueue(dl["server"], item.get("Id"),
                                         item.get("Type"), dl["watched"]),
            on_ok=self._refresh_downloaded,
            error=_("The download could not be started."))

    # ------------------------------------------------------------- dialogs

    def _show_dialog(self, builder):
        self._dialog = builder
        self.invalidate()

    def _close_dialog(self):
        self._dialog = None
        self.invalidate()

    @staticmethod
    def _dialog_shell(node_id, children, w=440):
        # align="stretch" so button rows fill the shell's width; without it
        # they take their natural width and a trailing flex Spacer has no
        # leftover to absorb, which left the buttons hugging the left edge.
        return Column(children, pad=24, gap=16, bg="1e1e1e", radius=12,
                      border="555555", w=w, align="stretch")

    @staticmethod
    def _dialog_buttons(children):
        """Dialog action row: always trailing-aligned."""
        return Row(children, gap=10, justify="end")

    def _message(self, text, title=None):
        title = title or _("Notice")

        def build():
            return Dialog("msg", self._dialog_shell("msg", [
                Text(title, size=22, bold=True),
                Text(text, size=16, color=theme.SUBTLE_FG),
                self._dialog_buttons([
                    Button(_("OK"), id="dlg-ok",
                           on_click=self._close_dialog)]),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _on_clipboard_error(self, op, need):
        """Neither MPV's clipboard nor a desktop helper could be used.

        MPV only gained an X11 clipboard backend in 0.41 (its
        --clipboard-backends default is win32,mac,wayland,vo), so on an
        older MPV under X11 copy and paste do nothing at all. Silence
        reads as the text field being broken; say what to install.
        The renderer raises this at most once per session."""
        if op == "copy":
            text = _("Copying to the clipboard is not available.")
        else:
            text = _("Pasting from the clipboard is not available.")
        if need:
            text += " " + (
                _('Install the "%s" package (for example "apt install %s"), '
                  "or use MPV 0.41 or newer.") % (need, need))
        else:
            text += " " + _("Use MPV 0.41 or newer.")
        self._message(text, title=_("Clipboard"))

    def _confirm(self, text, on_yes, title=None, yes=None):
        title = title or _("Confirm")
        yes = yes or _("OK")

        def build():
            return Dialog("confirm", self._dialog_shell("confirm", [
                Text(title, size=22, bold=True),
                Text(text, size=16, color=theme.SUBTLE_FG),
                self._dialog_buttons([
                    Button(_("Cancel"), id="dlg-cancel",
                           on_click=self._close_dialog),
                    Button(yes, id="dlg-ok",
                           on_click=lambda: (self._close_dialog(),
                                             on_yes()))]),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    # -- SyncPlay ---------------------------------------------------------

    def _open_syncplay(self):
        server = self.server
        if self.controller is None or server is None:
            return
        ep = self._epoch

        def work():
            # None: every connected server, not just the selected one. A
            # group belongs to one server, so filtering to self.server made
            # half of them invisible with two accounts signed in.
            return (self.controller.get_sync_groups(None),
                    self.controller.sync_state())

        def done(res):
            groups, state = res
            self._show_syncplay(server, groups, state)

        # Fetch groups off-thread, then show the dialog on the loop.
        self.run_async(work, done, ep)

    def _show_syncplay(self, server, groups, state=None):
        joined = (state or {}).get("group_id")
        multi = len({g.get("server_uuid") for g in groups}) > 1

        def build():
            rows = [Text(_("SyncPlay"), size=22, bold=True)]
            if groups:
                for i, g in enumerate(groups):
                    gid = g.get("id")
                    here = joined is not None and gid == joined
                    who = ", ".join(g.get("participants") or [])
                    # Which server, but only when it disambiguates — a
                    # single-server session does not need it on every row.
                    if multi and g.get("server_name"):
                        who = ("%s · %s" % (g["server_name"], who) if who
                               else g["server_name"])
                    rows.append(Column([
                        Button(
                            # The joined group is not a join button; it says
                            # where you are. Every group used to look
                            # equally joinable.
                            (_("%s (joined)") % g.get("name")) if here
                            else (g.get("name") or _("Group")),
                            id="sp-join-%d" % i,
                            bg=theme.ACCENT if here else None,
                            fg=theme.ACCENT_FG if here else "eeeeee",
                            on_click=(self._close_dialog if here else
                                      (lambda gid=gid, srv=g.get("server_uuid"):
                                       self._sync_join(srv or server, gid)))),
                        Text(who, size=13, color=theme.SUBTLE_FG)
                        if who else Spacer(h=0),
                    ], gap=2))
            else:
                rows.append(Text(_("No active groups."), size=15,
                                 color=theme.SUBTLE_FG))
            buttons = [
                Button(_("New Group"), id="sp-new",
                       on_click=lambda: self._sync_new(server)),
            ]
            if joined is not None:
                # Only when there is something to leave. It used to render
                # unconditionally, so the one control that changes state was
                # offered when it could do nothing.
                buttons.append(Button(
                    _("Leave"), id="sp-leave",
                    on_click=lambda srv=(state or {}).get("server_uuid"):
                        self._sync_leave(srv or server)))
            buttons += [
                Button(_("Refresh"), id="sp-refresh",
                       on_click=lambda: self._open_syncplay()),
                Spacer(),
                Button(_("Close"), id="sp-close", on_click=self._close_dialog),
            ]
            rows.append(Row(buttons, gap=10, align="center"))
            return Dialog("syncplay", self._dialog_shell("syncplay", rows,
                                                         w=480),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    # Joining, creating and leaving are all button presses, so a failure has
    # to reach the user; _client_call swallows. The dialog closes first —
    # these are round trips, and holding it open until they land reads as a
    # hang — so the report lands on the status line behind it.
    def _sync_join(self, server, group_id):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_join(server, group_id),
                        error=_("Could not join the SyncPlay group."))

    def _sync_new(self, server):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_new(server),
                        error=_("Could not create the SyncPlay group."))

    def _sync_leave(self, server):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_leave(server),
                        error=_("Could not leave the SyncPlay group."))
