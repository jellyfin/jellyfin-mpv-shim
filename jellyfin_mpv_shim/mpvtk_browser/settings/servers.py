"""The Servers & Users tab.

Server list, per-server auto-download scope, and the user rows -- add, rename,
delete. Removing a server rebuilds the data source, which is why several of
these end in a navigation rather than an invalidate.
"""

import logging

from ...i18n import _
from ...mpvtk.widgets import (
    Button,
    Checkbox,
    Column,
    Dialog,
    Grid,
    Icon,
    Row,
    Spacer,
    Text,
    TextBox,
    VScroll,
)
from .. import theme

log = logging.getLogger("mpvtk_browser.settings")


class ServersTabMixin:

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
            server_rows.append(Text(_("No servers configured yet."), size="small",
                                    color=theme.SUBTLE_FG))
        else:
            server_rows.append(Grid(
                [self._server_row(sv, i) for i, sv in enumerate(servers)],
                cols=[{"w": 22}, {"flex": 1}, {}, {}, {},
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
                Text(u.get("name", "?"), size="normal", bold=True, flex=1),
                Text(_("active") if u.get("active") else "", size="caption",
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
                Column([Text(sv.get("name", "?"), size="normal", bold=True),
                        Text(sv.get("address", ""), size="caption",
                             color=theme.SUBTLE_FG)], gap=1, flex=1),
                Text(sv.get("username", ""), size="small",
                     color=theme.SUBTLE_FG),
                Text(_("Connected") if connected else _("Offline"),
                     size="small",
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Checkbox(_("Auto-download"), self._auto_dl_on(sv), 
                         id="sv-auto-%d" % i,
                         on_toggle=lambda u=sv.get("uuid"):
                             self._toggle_auto_server(u)),
                Button(_("Remove"), id="sv-rm-%d" % i, icon="delete",
                       size="small",
                       on_click=lambda u=sv.get("uuid"), n=sv.get("name"):
                           self._confirm(
                               _("Remove %s and its saved login?") % n,
                               lambda: self._remove_server(u),
                               title=_("Remove Server"), yes=_("Remove"))),
            ],
        }
    def _auto_dl_servers(self):
        """The configured allow-list as a set. Empty means no server."""
        raw = (self._config().get_settings().get("auto_download_servers")
               or "").strip()
        return {p.strip() for p in raw.split(",") if p.strip()}
    def _auto_dl_on(self, sv):
        return sv.get("uuid") in self._auto_dl_servers()
    def _toggle_auto_server(self, uuid):
        """Include/exclude one server from automatic downloads.

        Stored as an explicit include-list. Unticking the first server has to
        materialize "all" into the full list first, or the empty-means-all
        default would read the removal back as "everything is still on".
        """
        if not uuid:
            return
        try:
            known = [sv.get("uuid") for sv in self.controller.list_servers()
                     if sv.get("uuid")]
        except Exception:
            log.debug("list_servers failed", exc_info=True)
            return
        picked = set(self._auto_dl_servers())
        if uuid in picked:
            picked.discard(uuid)
        else:
            picked.add(uuid)
        # Stored in the servers' own order so the value is stable across
        # toggles rather than reshuffling with set iteration.
        self._set_setting("auto_download_servers",
                          ",".join(u for u in known if u in picked))
        self.invalidate()
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
                Text(_("Rename User"), size="title", bold=True),
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
