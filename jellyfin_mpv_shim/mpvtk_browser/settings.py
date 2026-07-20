"""The Settings route.

Four tabs — general, servers & users, downloads, logs — plus the
schema-driven config form (``_setting_row`` off ``settings_schema``) and
the downloads panel with its progress poller.

State on ``self``: ``_config_obj`` (the settings accessor; None means the
real config module, tests inject a fake), ``_sync_path`` (download-folder
field mirror), ``_dl_thread`` (the downloads poller) and ``_log_thread``
(the log tail), both started via core's ``_start_daemon``. The pollers run
on foreign threads, so they write then call ``invalidate()``; they exit on
``_shutdown_evt``, which is only ever set at shutdown, or as soon as the
user leaves the tab they belong to.
"""

import logging

from ..i18n import _
from ..mpvtk.widgets import (
    Box,
    Button,
    Checkbox,
    Column,
    Dialog,
    Dropdown,
    Grid,
    Icon,
    Row,
    Spacer,
    Table,
    Text,
    TextBox,
    VScroll,
)
from . import theme

log = logging.getLogger("mpvtk_browser.settings")


class SettingsMixin:

    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "settings": (None, "_render_settings"),
    }

    # ------------------------------------------------------------- settings

    def _config(self):
        if self._config_obj is not None:
            return self._config_obj
        from . import config as cfg
        return cfg

    def _open_settings(self):
        self.open_settings()

    def open_settings(self, tab="general"):
        """Open Settings on ``tab``. Public: the tray's Configure Servers /
        Show Console entries route here — which is why it has to respect the
        lock gate: the logs and server list are behind the PIN too."""
        if self._locked:
            return
        if self.route.get("kind") == "settings":
            self.route["_tab"] = tab   # already there — just switch tabs
            self.invalidate()
            return
        self.navigate({"kind": "settings", "server": self.server,
                       "title": _("Settings"), "_tab": tab})

    def _set_setting(self, key, value):
        ok = self._config().set_setting(key, value)
        self.set_status((_("Saved: %s") if ok else _("Invalid value: %s"))
                        % key)
        if ok and key == "work_offline":
            self._apply_work_offline(bool(value))
        self.invalidate()

    def _apply_work_offline(self, offline):
        """Swap the data source when the setting is toggled, rather than
        persisting a key that does nothing until the next launch. Tk
        applies it live too."""
        if self.controller is None or offline == self._offline:
            return

        ep = self._epoch

        def work():
            if offline:
                return self.controller.offline_source()
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is None:
                self.set_status(_("Nothing downloaded to browse offline.")
                                if offline else
                                _("Could not reach a server."))
                return
            self.set_source(source)
        self.run_async(work, done, ep)

    SETTINGS_TABS = ("general", "servers", "downloads", "logs")

    def _render_settings(self, route, size):
        tab = route.get("_tab", "general")
        labels = {"general": _("General"), "servers": _("Servers & Users"),
                  "downloads": _("Downloads"), "logs": _("Logs")}
        tabs = Row([
            Button(labels[t], id="stab-" + t,
                   bg=theme.ACCENT if tab == t else theme.BUTTON_BG,
                   fg=theme.ACCENT_FG if tab == t else theme.TEXT_FG,
                   on_click=lambda t=t: self._set_settings_tab(route, t))
            for t in self.SETTINGS_TABS
        ], gap=8)
        body = {
            "servers": self._settings_servers,
            "downloads": self._settings_downloads,
            "logs": self._settings_logs,
        }.get(tab, self._settings_general)(route, size)
        head = [Row([tabs], pad=12)]
        return Column(head + [body], flex=1, align="stretch")

    def _set_settings_tab(self, route, tab):
        route["_tab"] = tab
        self.status = ""
        self.invalidate()

    # -- General (the generated config form) ------------------------------

    def _settings_general(self, route, size):
        cfg = self._config()
        schema = cfg.settings_schema()
        values = cfg.get_settings()
        show_adv = bool(route.get("_advanced"))
        rows = []
        for title, keys in cfg.sections():
            advanced = title == _("Advanced")
            if advanced:
                rows.append(Checkbox(
                    _("Show advanced settings"), show_adv, id="set-adv",
                    on_toggle=lambda: self._toggle_advanced(route)))
                if not show_adv:
                    continue
            rows.append(Text(title, size=20, bold=True))
            notes = getattr(cfg, "NOTES", None) or {}
            for key in keys:
                rows.append(self._setting_row(cfg, schema, values, key))
                if key in notes:
                    # An explanatory line under the setting it belongs to;
                    # the settings it qualifies follow directly below.
                    rows.append(Text(notes[key], size=14,
                                     color=theme.SUBTLE_FG, wrap=True))
        rows.append(Text(_("Some changes take effect after restarting."),
                         size=14, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="settings", flex=1)

    def _toggle_collections(self, route):
        """Movies library <-> its collections, like jellyfin-web's toggle.
        Collections are server-wide and recursive, so this is a different
        query rather than a filter."""
        route["_collections"] = not route.get("_collections")
        route.pop("_items", None)
        route.pop("_total", None)
        route.pop("_loading", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _toggle_advanced(self, route):
        route["_advanced"] = not route.get("_advanced")
        self.invalidate()

    def _setting_row(self, cfg, schema, values, key):
        kind = schema.get(key, "str")
        val = values.get(key)
        label = cfg.label_for(key)
        if kind == "bool":
            return Checkbox(label, bool(val), id="set-" + key,
                            on_toggle=lambda k=key, v=val: self._set_setting(
                                k, not bool(v)))
        if key in cfg.LABELED_ENUMS:
            opts = cfg.LABELED_ENUMS[key]
            cur = next((i for i, (_l, v) in enumerate(opts)
                        if str(v) == str(val)), 0)
            widget = Dropdown(
                "set-" + key, [lbl for lbl, _v in opts], selected=cur, w=340,
                force=True,
                on_select=lambda i, _v, k=key, o=opts: self._set_setting(
                    k, o[i][1]))
        elif key in cfg.ENUMS:
            opts = cfg.ENUMS[key]
            cur = opts.index(str(val)) if str(val) in opts else 0
            widget = Dropdown(
                "set-" + key, opts, selected=cur, w=340, force=True,
                on_select=lambda i, _v, k=key, o=opts: self._set_setting(
                    k, o[i]))
        elif key == "sync_path":
            widget = Row([
                TextBox("set-" + key, text="" if val is None else str(val),
                        w=250,
                        on_change=lambda v: self._sync_path.__setitem__(
                            "path", v),
                        on_submit=lambda v: self._move_downloads(v)),
                # Moves what is in the field. It used to pass None, whose
                # only effect was a status line telling you to press Enter
                # — a button that could never do its own job.
                Button(_("Move"), id="set-sync-move",
                       on_click=lambda: self._move_downloads(
                           self._sync_path.get("path") or val)),
            ], gap=8, align="center")
        else:
            # on_commit as well as on_submit: ENTER is not the only way people
            # leave a field. Wired only here, so typing then clicking the next
            # row silently threw the edit away on 65 rows, with no toast and
            # no dirty marker. The sync_path row above already had a Move
            # button for the same reason; this generalizes it.
            widget = TextBox("set-" + key,
                             text="" if val is None else str(val), w=340,
                             on_submit=lambda v, k=key: self._set_setting(k, v),
                             on_commit=lambda v, k=key: self._set_setting(k, v))
        return Row([Text(label, w=340, size=17, color=theme.SUBTLE_FG),
                    widget], gap=12, align="center")

    def _move_downloads(self, path, confirmed=False):
        """Relocating the download store copies files (possibly across
        drives), so it runs on its own thread — not the pool, whose four
        workers serve every route load — and reports progress into the
        status line.

        An empty path means "go back to the default location". That is a real
        thing to want, but it used to happen *silently*: clearing the field
        and pressing Enter relocated the whole store with no confirmation and
        no indication that is what an empty box meant. It asks first now, like
        every other destructive download action."""
        if path is not None and not str(path).strip():
            path = None
        if path is None:
            if not confirmed:
                self._confirm(
                    _("Move the downloads back to the default folder?"),
                    lambda: self._move_downloads(None, confirmed=True),
                    title=_("Use the default folder"), yes=_("Move"))
                return
        cfg = self._config()
        if not hasattr(cfg, "relocate_downloads"):
            self._set_setting("sync_path", path)
            return

        def work():
            def progress(copied, total):
                pct = 100 if not total else min(100, int(copied * 100 / total))
                self.set_status(_("Moving downloads… %d%%") % pct)
                self.invalidate()
            try:
                ok, message = cfg.relocate_downloads(path or "",
                                                     progress=progress)
            except Exception:
                log.error("download folder move failed", exc_info=True)
                ok, message = False, _("Moving the downloads failed.")
            self.set_status(message or (
                _("Download folder moved. Restart to finish switching.")
                if ok else _("Moving the downloads failed.")))

        # Set before starting, so the job's own progress line wins the race.
        self.set_status(_("Moving downloads…"))
        if not self._run_long(work, "mpvtk-move-downloads"):
            # Two concurrent copies of the same store would fight. Say so —
            # a second press that silently did nothing reads as a dead button.
            self.set_status(_("A move is already in progress."))
        self.invalidate()

    # -- Servers & Users --------------------------------------------------

    def _settings_servers(self, route, size):
        users = self._users()
        # Grid, not per-row fixed widths: the name/status/button columns
        # share tracks across rows, and the button track auto-sizes to
        # the widest button set (translations included).
        user_rows = [Grid(
            [self._user_row(u, i, len(users) > 1)
             for i, u in enumerate(users)],
            cols=[{"w": 22}, {"flex": 1}, {"w": 90},
                  {"align": "right"}],
            gap=8, row_gap=4, row_pad=8,
        )]
        user_rows.append(Row([
            TextBox("su-newuser", placeholder=_("New user name…"), w=240,
                    on_change=lambda v: self._newuser.__setitem__("name", v),
                    on_submit=self._add_user),
            Button(_("Add User"), id="su-adduser", icon="person_add",
                   on_click=lambda: self._add_user(
                       self._newuser.get("name", ""))),
            Spacer(),
        ], gap=8, align="center"))

        servers = []
        if self.controller is not None:
            try:
                servers = self.controller.list_servers()
            except Exception:
                log.debug("list_servers failed", exc_info=True)
        active = next((u.get("name") for u in users if u.get("active")), None)
        server_rows = []
        if not servers:
            server_rows.append(Text(_("No servers configured yet."), size=15,
                                    color=theme.SUBTLE_FG))
        else:
            server_rows.append(Grid(
                [self._server_row(sv, i) for i, sv in enumerate(servers)],
                cols=[{"w": 22}, {"flex": 1}, {}, {},
                      {"align": "right"}],
                gap=12, row_gap=4, row_pad=8,
            ))
        server_rows.append(Row([
            Button(_("Add Server"), id="sv-add", icon="add",
                   on_click=self.show_login),
            Spacer(),
        ], gap=8, align="center"))

        return VScroll(Column([
            self._section(
                _("Users"), user_rows,
                subtitle=_("Each user has its own servers and device "
                           "identity; a locked user needs a PIN to switch "
                           "to.")),
            self._section(
                # Servers are scoped to the active user, so name the section
                # after them — otherwise removing one looks global.
                _("Servers for %s") % active if active else _("Servers"),
                server_rows),
        ], pad=self.CONTENT_PAD, gap=14, align="stretch"),
            id="settings-servers", flex=1)

    def _user_row(self, u, i, can_delete):
        """One Grid row spec for the Users list (cells share the Grid's
        tracks; the trailing button set varies per row)."""
        buttons = []
        if not u.get("active"):
            buttons.append(Button(_("Switch"), id="su-sw-%d" % i,
                                  on_click=lambda: self._switch_user(u)))
        buttons.append(Button(
            _("Change PIN") if u.get("locked") else _("Set PIN"),
            id="su-pin-%d" % i, icon="lock",
            on_click=lambda: self._open_pin_setup(u)))
        buttons.append(Button(_("Rename"), id="su-rn-%d" % i,
                              on_click=lambda: self._open_rename_user(u)))
        if can_delete and not u.get("active"):
            buttons.append(Button(
                _("Delete"), id="su-del-%d" % i, icon="delete",
                on_click=lambda: self._confirm(
                    _("Delete user %s and its saved logins?")
                    % u.get("name", ""),
                    lambda: self._delete_user(u),
                    title=_("Delete User"), yes=_("Delete"))))
        return {
            "id": "su-%d" % i,
            "bg": theme.PANEL_BG,
            "radius": 6,
            "cells": [
                Icon("lock" if u.get("locked") else "person", 18),
                Text(u.get("name", "?"), size=17, bold=True, flex=1),
                Text(_("active") if u.get("active") else "", size=14,
                     color=theme.OK_GREEN),
                Row(buttons, gap=8),
            ],
        }

    def _server_row(self, sv, i):
        connected = sv.get("connected")
        return {
            "id": "sv-%d" % i,
            "bg": theme.PANEL_BG,
            "radius": 6,
            "cells": [
                Icon("radio", 16,
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Column([Text(sv.get("name", "?"), size=17, bold=True),
                        Text(sv.get("address", ""), size=13,
                             color=theme.SUBTLE_FG)], gap=1, flex=1),
                Text(sv.get("username", ""), size=15,
                     color=theme.SUBTLE_FG),
                Text(_("Connected") if connected else _("Offline"),
                     size=15,
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Button(_("Remove"), id="sv-rm-%d" % i, icon="delete",
                       size=15,
                       on_click=lambda u=sv.get("uuid"), n=sv.get("name"):
                           self._confirm(
                               _("Remove %s and its saved login?") % n,
                               lambda: self._remove_server(u),
                               title=_("Remove Server"), yes=_("Remove"))),
            ],
        }

    def _remove_server(self, uuid):
        """Remove a server and rebuild the data source.

        Dropping the credential is not enough. LibrarySource holds its own
        connection per server, built once at construction, so the removed
        server stayed in the switcher and stayed browsable — while playback
        refused it, because that path re-checks the credentials. Tk rebuilt
        this is where that happens.
        """
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            if self.controller.remove_server(uuid) is False:
                raise RuntimeError("remove_server refused")
            return self.controller.rebuild_source()

        def done(source):
            if source is None:
                # That was the last server. Nothing to browse: the offline
                # catalog if there is one, otherwise back to login.
                source = self.controller.offline_source()
                if source is None:
                    self.show_login()
                    return
            self.set_source(source)
            # set_source lands on Home; the user was in Settings and almost
            # certainly wants to keep managing servers.
            self.open_settings("servers")

        def failed(_exc):
            self.set_status(_("The server could not be removed."))

        self.run_async(work, done, ep, on_error=failed)

    def _add_user(self, name):
        """Add a local user, and say so if it did not work.

        This used to go through _safe, which logs and returns — so a
        duplicate name cleared the field and changed nothing, with the box
        looking like it had accepted the input."""
        name = (name or "").strip()
        if not name or self.controller is None:
            return

        def ok():
            self._newuser["name"] = ""
            self._after_users_changed()

        self._edit_call(lambda c: c.add_user(name), on_ok=ok,
                        error=_("That user could not be added."))

    def _delete_user(self, u):
        if self.controller is None:
            return
        ok, err = (False, None)
        try:
            ok, err = self.controller.delete_user(u.get("id"))
        except Exception:
            log.error("delete_user failed", exc_info=True)
        if not ok and err:
            self._message(err)
        self._after_users_changed()

    def _open_rename_user(self, u):
        state = {"name": u.get("name", "")}

        def build():
            return Dialog("renameuser", self._dialog_shell("renameuser", [
                Text(_("Rename User"), size=22, bold=True),
                TextBox("ru-name", text=state["name"], w=280, force=True,
                        on_change=lambda v: state.__setitem__("name", v),
                        on_submit=lambda v: save()),
                self._dialog_buttons([
                    Button(_("Cancel"), id="ru-cancel",
                           on_click=self._close_dialog),
                    Button(_("Rename"), id="ru-ok", on_click=save)]),
            ]), on_dismiss=self._close_dialog)

        def save():
            name = (state["name"] or "").strip()
            if not name:
                self._close_dialog()
                return
            # Close first: the rename is a round trip, and leaving the dialog
            # up until it lands reads as a hang. A failure reports on the
            # status line behind it.
            self._close_dialog()
            self._edit_call(lambda c: c.rename_user(u.get("id"), name),
                            on_ok=self._after_users_changed,
                            error=_("That user could not be renamed."))
        self._show_dialog(build)

    def _after_users_changed(self):
        self.invalidate()

    # -- Downloads --------------------------------------------------------

    def _section(self, title, children, subtitle=None):
        """A full-width titled card. Settings panels are forms, not tile
        grids — they should span the pane rather than sit in a ragged
        left-aligned column."""
        head = [Text(title, size=20, bold=True)]
        if subtitle:
            head.append(Text(subtitle, size=14, color=theme.SUBTLE_FG,
                             wrap=True))
        return Column(head + children, pad=14, gap=8, bg=theme.CARD_BG,
                      radius=10, align="stretch")

    INDENT = 26   # per hierarchy level in the downloads tree

    def _settings_downloads(self, route, size):
        groups = route.get("_downloads")
        if groups is None:
            self._load_downloads(route)
            return self._busy()
        total = sum(g.get("size", 0) or 0 for g in groups)
        count = sum(g.get("count", 0) or 0 for g in groups)
        head = Row([
            Text(_("Downloads"), size=20, bold=True),
            Text(_("%(count)d items · %(size)s") % {
                "count": count, "size": self._human_size(total)},
                size=15, color=theme.SUBTLE_FG),
            Spacer(),
            Button(_("Refresh"), id="dl-refresh", icon="refresh",
                   on_click=lambda: self._load_downloads(route, force=True)),
        ], gap=12, align="center")
        rows = [head]
        if not groups:
            rows.append(Text(_("Nothing downloaded yet."), size=16,
                             color=theme.SUBTLE_FG))
        for gi, group in enumerate(groups):
            rows.append(self._dl_group(route, group, gi))
        self._poll_downloads(route)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=10,
                              align="stretch"),
                       id="settings-downloads", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "settings-downloads", off, mx))

    def _dl_row(self, node_id, title, meta, depth, on_delete, bold=False,
                icon=None, count=None, route=None, toggle=None,
                expanded=True, on_delete_watched=None):
        """One Grid row spec of the downloads tree. Indentation carries
        the level (inside the title cell, so the meta/Remove tracks stay
        shared across every depth); every level gets its own delete so a
        whole show can go at once. ``toggle`` (a collapse-state key)
        adds a disclosure chevron before the title."""
        title_cell = [Spacer(w=depth * self.INDENT, h=1)]
        if toggle is not None:
            title_cell.append(Box(
                [Icon("keyboard_arrow_down" if expanded
                      else "chevron_right", 16, color=theme.SUBTLE_FG)],
                id=node_id + "-tgl", pad=3, radius=4, direction="row",
                align="center", hover={"fill": theme.BUTTON_BG},
                on_click=lambda: self._dl_toggle(route, toggle)))
        else:
            # rows without a disclosure still reserve its gutter, so
            # titles stay monotonically indented down the tree
            title_cell.append(Spacer(w=22, h=1))
        if icon:
            title_cell.append(Icon(icon, 16, color=theme.SUBTLE_FG))
        title_cell.append(Text(title, size=17 if bold else 16, bold=bold))
        if count:
            # Collapsed groups (playlists) say how much they stand for.
            title_cell.append(Text(_("%d items") % count, size=14,
                                   color=theme.SUBTLE_FG))
        title_cell.append(Spacer())
        return {
            "id": node_id,
            "bg": theme.PANEL_BG if depth == 0 else None,
            "radius": 6,
            "cells": [
                Row(title_cell, gap=10, align="center", flex=1),
                Text(meta, size=14, color=theme.SUBTLE_FG,
                     align="right"),
                Row(([Button(_("Remove Watched"), id=node_id + "-rmw",
                             icon="check", size=15,
                             on_click=on_delete_watched)]
                     if on_delete_watched else []) +
                    [Button(_("Remove"), id=node_id + "-rm", icon="delete",
                            size=15, on_click=on_delete)],
                    gap=6, align="center"),
            ],
        }

    def _dl_toggle(self, route, key):
        route.setdefault(
            "_dl_collapsed", set()).symmetric_difference_update({key})
        self.invalidate()

    @staticmethod
    def _dl_key(entry, fallback):
        # stable across refreshes (ids); position only as a last resort
        return str(entry.get("id") or entry.get("title") or fallback)

    def _dl_group(self, route, group, gi):
        collapsed = route.get("_dl_collapsed") or set()
        kind = group.get("kind")
        children = group.get("children") or []
        gkey = self._dl_key(group, gi)
        g_open = gkey not in collapsed
        rows = [self._dl_row(
            "dl-g%d" % gi, group.get("title", "?"),
            self._human_size(group.get("size", 0)), 0,
            self._dl_delete_cb(
                route, group,
                series_id=group.get("id") if kind == "series" else None,
                playlist_id=group.get("id") if kind == "playlist" else None,
                # Groups without a server-side id (the flat "Movies &
                # Videos" bucket) delete their own rows explicitly. Passing
                # no scope at all used to reach syncManager.delete() with
                # every id None, which deleted the ENTIRE catalog behind a
                # prompt naming only this group.
                item_ids=(None if kind in ("series", "playlist")
                          else self._dl_group_item_ids(group))),
            bold=True, count=group.get("count"),
            icon={"movies": "movie", "playlist": "queue_music"}.get(kind),
            route=route, toggle=gkey if children else None,
            expanded=g_open,
            # Reclaim space on a finished show without losing what's
            # unwatched — the Tk browser's gesture.
            on_delete_watched=(
                self._dl_delete_cb(
                    route, group, watched_only=True,
                    series_id=group.get("id") if kind == "series" else None,
                    playlist_id=(group.get("id") if kind == "playlist"
                                 else None),
                    item_ids=(None if kind in ("series", "playlist")
                              else self._dl_group_item_ids(group)))
                if kind in ("series", "playlist")
                and group.get("watched_count") else None))]
        for ci, child in enumerate(children if g_open else []):
            if child.get("kind") == "season":
                skey = self._dl_key(child, "%d.%d" % (gi, ci))
                s_open = skey not in collapsed
                eps = child.get("children") or []
                rows.append(self._dl_row(
                    "dl-g%d-s%d" % (gi, ci), child.get("title", "?"),
                    self._human_size(child.get("size", 0)), 1,
                    self._dl_delete_cb(route, child,
                                       season_id=child.get("id")),
                    route=route, toggle=skey if eps else None,
                    expanded=s_open))
                for ei, ep in enumerate(eps if s_open else []):
                    rows.append(self._dl_item_row(
                        route, ep, "dl-g%d-s%d-e%d" % (gi, ci, ei), 2))
            else:
                rows.append(self._dl_item_row(
                    route, child, "dl-g%d-i%d" % (gi, ci), 1))
        return Grid(rows,
                    cols=[{"flex": 1}, {"w": 200, "align": "right"},
                          {"align": "right"}],
                    gap=10, row_gap=2, row_pad=6)

    def _dl_item_row(self, route, item, node_id, depth):
        num = item.get("index")
        title = ("%s. %s" % (num, item.get("title", ""))
                 if num is not None else item.get("title", ""))
        from .downloads import status_text
        # The watched marker is why "Remove Watched" is offered at all; with
        # no way to see which rows it means, the button read as a destructive
        # guess.
        meta = "   ".join(x for x in (
            _("watched") if item.get("watched") else "",
            status_text(item),
            self._human_size(item.get("size", 0))) if x)
        return self._dl_row(node_id, title, meta, depth,
                            self._dl_delete_cb(route, item,
                                               item_id=item.get("id")))

    @staticmethod
    def _dl_group_item_ids(group):
        """Every download id under a group, including nested season rows."""
        out = []
        for child in group.get("children") or ():
            if child.get("kind") == "season":
                out += [g.get("id") for g in child.get("children") or ()]
            elif child.get("id"):
                out.append(child["id"])
        return [i for i in out if i]

    def _dl_delete_cb(self, route, entry, item_id=None, series_id=None,
                      season_id=None, playlist_id=None, item_ids=None,
                      watched_only=False):
        def go():
            self._confirm(
                (_("Delete the watched downloads in %s?") if watched_only
                 else _("Delete the downloaded copy of %s?"))
                % entry.get("title", ""),
                lambda: self._delete_download(route, item_id=item_id,
                                              series_id=series_id,
                                              season_id=season_id,
                                              playlist_id=playlist_id,
                                              item_ids=item_ids,
                                              watched_only=watched_only),
                title=_("Delete Download"), yes=_("Delete"))
        return go

    # How often the downloads view re-reads the catalog while work is
    # outstanding. Downloads land asynchronously, so a static list is stale
    # the moment it renders.
    DL_POLL_SECS = 3.0

    def _poll_downloads(self, route):
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(self.DL_POLL_SECS):
                if (self.route is not route
                        or route.get("_tab") != "downloads"
                        or not self._browsing):
                    break
                try:
                    pending, _total = self.controller.download_activity()
                except Exception:
                    break
                if not pending:
                    # One last read before stopping. The transition that took
                    # pending to zero is exactly the one the list has not
                    # drawn yet, so breaking straight out left the item that
                    # had just finished reading "downloading" until someone
                    # pressed Refresh.
                    self._load_downloads(route, force=True)
                    break
                self._load_downloads(route, force=True)

        self._start_daemon("_dl_thread", "mpvtk-dl-poll", tick,
                           restartable=True)

    def _load_downloads(self, route, force=False):
        if self.controller is None:
            route["_downloads"] = []
            return
        if route.get("_dl_loading") and not force:
            return
        route["_dl_loading"] = True
        ep = self._epoch

        def work():
            return self.controller.list_downloads()

        def done(rows):
            route["_downloads"] = rows or []
            # badges elsewhere in the UI are keyed off the same catalog
            self._refresh_downloaded()

        # `always`, not part of done: a load dropped for being stale runs
        # neither callback, and a stuck _dl_loading makes every later render
        # of this panel return early — the list freezes until something calls
        # with force=True. There was no on_error either, so a failed load did
        # the same.
        self.run_async(work, done, ep,
                       always=lambda: route.__setitem__("_dl_loading", False))

    def _delete_download(self, route, item_id=None, series_id=None,
                         season_id=None, playlist_id=None, item_ids=None,
                         watched_only=False):
        """Delete, then re-read the catalog — in that order, on one worker.

        Submitting the delete and the reload as separate tasks raced: the
        reload could read the catalog before the delete had touched it, and
        the row came straight back."""
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            # No try/except here: the controller raises now, and swallowing
            # it a second time is what made a failed delete silent. The list
            # is re-read on the same worker so the reload cannot run before
            # the delete has touched the catalog.
            if item_ids is not None and not watched_only:
                for one in item_ids:
                    self.controller.delete_download(item_id=one)
            else:
                self.controller.delete_download(
                    item_id=item_id, series_id=series_id,
                    season_id=season_id, playlist_id=playlist_id,
                    watched_only=watched_only)
            return self.controller.list_downloads()

        def done(rows):
            route["_downloads"] = rows or []
            # badges elsewhere in the UI are keyed off the same catalog
            self._refresh_downloaded()

        def failed(_exc):
            self.set_status(_("The download could not be removed."))
        self.run_async(work, done, ep, on_error=failed,
                       always=lambda: route.__setitem__("_dl_loading", False))
        self._refresh_downloaded()

    # -- Logs -------------------------------------------------------------

    # How often the logs tab re-reads the ring while it is on screen. The
    # Tk browser got a push per line; there is no such channel in-process,
    # so poll — cheaply, since a tick that finds nothing new does not
    # re-render.
    LOG_POLL_SECS = 1.0

    # One line per row. Fixed height is what makes the list virtualizable,
    # and virtualization is what lets it show the whole 2000-line ring
    # rather than the last 500.
    LOG_ROW_H = 20

    def _settings_logs(self, route, size):
        lines = []
        if self.controller is not None:
            try:
                lines = self.controller.recent_logs()
            except Exception:
                log.debug("recent_logs failed", exc_info=True)
        # Remember what we have drawn so the poller can tell whether a tick
        # actually changed anything (see _poll_logs).
        route["_log_len"] = len(lines)
        route["_log_last"] = lines[-1] if lines else None
        self._poll_logs(route)

        head = Row([Text(_("Logs"), size=20, bold=True), Spacer(),
                    Button(_("Copy"), id="log-copy", icon="content_copy",
                           on_click=lambda: self._copy_logs(lines)),
                    Button(_("Refresh"), id="log-refresh", icon="refresh",
                           on_click=self.invalidate),
                    Button(_("Open Config Folder"), id="log-conf",
                           icon="folder",
                           on_click=self._open_config_folder)],
                   gap=8, align="center", pad=self.CONTENT_PAD)
        if not lines:
            return Column([head,
                           Column([Text(_("No log output captured yet."),
                                        size=15, color=theme.SUBTLE_FG)],
                                  pad=self.CONTENT_PAD)],
                          flex=1, align="stretch")

        # Newest last, like a console. `follow` keeps the view pinned to the
        # newest line as lines arrive, and unpins the moment the user
        # scrolls up to read something — the renderer decides, because it is
        # the only side that knows the offset and the content height at the
        # same instant.
        rows = [{"cells": [line], "id": "log-%d" % i}
                for i, line in enumerate(lines)]
        virtual = {"offset": self._offset("settings-logs"),
                   "height": float(size[1])}
        table = Table([{"flex": 1}], rows, row_h=self.LOG_ROW_H, header_h=0,
                      size=14, fg=theme.SUBTLE_FG, virtual=virtual)
        return Column([
            head,
            VScroll(Column([table], pad=self.CONTENT_PAD),
                    id="settings-logs", flex=1, follow=True,
                    on_scroll=lambda off, mx: self._on_scroll(
                        "settings-logs", off, mx)),
        ], flex=1, align="stretch")

    def _poll_logs(self, route):
        """Re-render the logs tab while new lines are arriving.

        Only when something changed: an idle app logs nothing for minutes at
        a time, and rebuilding a 2000-row scene every second to draw the
        same thing would cost real frames for nothing.
        """
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(self.LOG_POLL_SECS):
                if (self.route is not route
                        or route.get("_tab") != "logs"
                        or not self._browsing):
                    break
                try:
                    lines = self.controller.recent_logs()
                except Exception:
                    break
                # Length alone is not enough: the ring is bounded, so once
                # it is full every new line also drops one and the count
                # stops moving. Compare the newest line too.
                last = lines[-1] if lines else None
                if (len(lines) != route.get("_log_len")
                        or last != route.get("_log_last")):
                    self.invalidate()

        self._start_daemon("_log_thread", "mpvtk-log-tail", tick,
                           restartable=True)

    def _copy_logs(self, lines):
        """Put the captured log on the clipboard.

        Copies *everything* the ring holds, not the 500 lines the view draws
        — the point is to hand the whole thing to someone else. Falls back to
        writing a file when there is no clipboard at all (a headless box, or
        one with none of wl-copy/xclip/xsel), because a button that silently
        does nothing is worse than one that tells you where it put the text.
        """
        if self.controller is None or not lines:
            self.set_status(_("There is nothing to copy yet."))
            self.invalidate()
            return
        text = "\n".join(lines)

        def work():
            return self.controller.copy_text(text)

        def done(res):
            ok, _method, path = res
            if not ok:
                self.set_status(_("Could not copy the log."))
            elif path:
                self.set_status(_("No clipboard available — saved to %s")
                                % path)
            else:
                self.set_status(_("Copied %d log lines.") % len(lines))

        def failed(_exc):
            self.set_status(_("Could not copy the log."))

        # Off the loop thread: a clipboard helper is a subprocess, and on a
        # wedged one the 10s timeout would otherwise freeze the UI.
        self.run_async(work, done, self._epoch, on_error=failed)

    def _open_config_folder(self):
        self._client_call(lambda c: c.open_config_folder())
