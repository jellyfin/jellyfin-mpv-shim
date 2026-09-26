"""The Servers & Users tab.

Server list, per-server auto-download scope, and the user rows -- add, rename,
delete. Removing a server rebuilds the data source, which is why several of
these end in a navigation rather than an invalidate.
"""

import logging

from ...constants import CONNECT_BUSY, CONNECT_SIGNED_OUT
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
from ..components import server_icon

log = logging.getLogger("mpvtk_browser.settings")


def _server_status(sv):
    """The status word in a server row.

    "Signed out" is its own word because it sends the user somewhere
    different: "Offline" under a server that answered and rejected the saved
    login sends them to check their network, which is the one thing that is
    not wrong with it."""
    if sv.get("connected"):
        return _("Connected")
    if sv.get("problem") == CONNECT_SIGNED_OUT:
        return _("Signed out")
    return _("Offline")


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
            on_logins = self._auto_dl_logins()
            server_rows.append(Grid(
                [self._server_row(sv, i, on_logins)
                 for i, sv in enumerate(servers)],
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
    def _server_row(self, sv, i, on_logins=None):
        connected = sv.get("connected")
        buttons = []
        if not connected:
            # **Both, always.** The probe behind `problem` can only say
            # whether the address answered, and it is wrong in both
            # directions often enough to matter -- a reverse proxy that is up
            # in front of a server that is not reads as signed out, and a
            # server that came back since the failure reads as unreachable.
            # So the verdict picks the ORDER, which is the emphasis this app
            # has (there is no primary-button styling, and inventing one here
            # would be a second authority over button colour). Nobody has to
            # fail a Retry to be offered the form.
            retry = Button(_("Retry"), id="sv-retry-%d" % i, icon="refresh",
                           size="small",
                           on_click=lambda u=sv.get("uuid"):
                               self.reconnect_server(u, switch=False))
            reauth = Button(_("Sign In Again"), id="sv-reauth-%d" % i,
                            icon="person", size="small",
                            on_click=lambda s=sv: self.open_reauth(s))
            buttons += ([reauth, retry]
                        if sv.get("problem") == CONNECT_SIGNED_OUT
                        else [retry, reauth])
        buttons.append(
            Button(_("Remove"), id="sv-rm-%d" % i, icon="delete",
                   size="small",
                   on_click=lambda u=sv.get("uuid"), n=sv.get("name"):
                       self._confirm(
                           _("Remove %s and its saved login?") % n,
                           lambda: self._remove_server(u),
                           title=_("Remove Server"), yes=_("Remove"))))
        return {
            "id": "sv-%d" % i,
            "bg": theme.PANEL_BG,
            "radius": 6,
            "cells": [
                # Where the server is, or that it is not answering. It was
                # a radio receiver for every row, which says nothing about
                # a server and left the colour to carry the whole state.
                Icon(server_icon(sv), 16,
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Column([Text(sv.get("name", "?"), size="normal", bold=True),
                        Text(sv.get("address", ""), size="caption",
                             color=theme.SUBTLE_FG)], gap=1, flex=1),
                Text(sv.get("username", ""), size="small",
                     color=theme.SUBTLE_FG),
                Text(_server_status(sv), size="small",
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Checkbox(_("Auto-download"), self._auto_dl_on(sv, on_logins),
                         id="sv-auto-%d" % i,
                         on_toggle=lambda u=sv.get("uuid"):
                             self._toggle_auto_server(u)),
                Row(buttons, gap=6),
            ],
        }

    # -------------------------------------------- getting a server back

    def reconnect_server(self, uuid, on_success=None, switch=True):
        """Try a saved server again, and say what happened either way.

        Shared by the Servers tab's Retry button and by picking an offline
        entry in the top bar's switcher, because they are one gesture with
        two doorways -- and a second implementation of "what do we do when it
        still will not connect" is how the two would come to offer different
        ways out of the same state.

        **``switch`` is what the two doorways disagree about, and it is the
        only thing they may.** Picking an entry in the switcher *is* a switch:
        it moves the browsed server and owes the handover `on_success`
        carries. Pressing Retry is not -- it is "get this one back", and it
        used to perform a switch anyway, because this always ended in
        `set_source(source, server_uuid=uuid)`. So a Retry on a second server
        from Settings left a SyncPlay group joined on the old one with no way
        to reach it, threw the user out of Settings onto the other server's
        Home, and left the persisted last-server naming the old one.
        Ruled 2026-09-19: Retry reconnects, it does not switch.

        With ``switch=False`` the rebuilt source is applied in place -- the
        browsed server is passed back to `set_source` unchanged, with
        ``keep_place``, which is the mechanism that function already has for
        a reconnect that is not a user's deliberate move. ``on_success`` is
        the *switch's* handover and is not run then; nothing passes both.

        **Not fixed by running the handover here unconditionally**, which is
        the obvious repair and is wrong: `_switch_server` guards
        `if uuid == self.server: return` before its handover and this has no
        such guard, and Retry is rendered for any unconnected server
        *including the one being browsed* -- so that would call `sync_leave`
        on the server just reconnected.

        The connect carries the apiclient's timeouts, so it goes through
        ``_run_long`` rather than the four-worker pool: a dead address parks
        the attempt for ~10 s, and four of those would be the whole pool.
        """
        if self.controller is None:
            return
        server = next((s for s in self._servers_snapshot()
                       if s.get("uuid") == uuid), None)
        name = (server or {}).get("name") or ""

        def work():
            try:
                ok, problem = self.controller.retry_server(uuid)
            except Exception:
                log.error("retry_server failed", exc_info=True)
                ok, problem = False, None
            if not ok:
                if problem == CONNECT_BUSY:
                    # Something else is already on it -- the health check, a
                    # websocket reconnect -- so this is the state a second
                    # press is in, and the same line says so. The failure
                    # dialog would be about a failure that has not happened,
                    # and its wording ("It may be switched off") is the
                    # opposite of what is going on. CR10.
                    self.set_status(_("Already reconnecting."))
                    self.invalidate()
                    return
                self.set_status("")
                self._server_problem_dialog(server or {"uuid": uuid}, problem)
                return
            source = None
            try:
                source = self.controller.rebuild_source()
            except Exception:
                log.error("rebuild_source failed", exc_info=True)
            self.set_status(_("Connected to %s.") % name)
            if source is None:
                # The connect worked and the switch did not, so `self.server`
                # is still the old one. `on_success` is the *switch's*
                # handover -- leave the SyncPlay group on the server being
                # left, remember the new one -- and running it here drops the
                # user out of a group and persists a server the UI is not
                # showing. CR2.
                self.invalidate()
                return
            # Write-then-invalidate, from this thread, which is the shell's
            # contract for anything arriving off the loop
            # (docs/browser-shell.md 2) -- and the same thing
            # `_do_switch_user` does from a pool worker.
            if switch:
                self.set_source(source, server_uuid=uuid)
                if on_success is not None:
                    on_success()
            else:
                # The server being browsed, not the one reconnected: that is
                # what makes `set_source` keep the place rather than reset to
                # Home, and what leaves `self.server` where the user left it.
                self.set_source(source, server_uuid=self.server,
                                keep_place=True)
            self.invalidate()

        self.set_status(_("Connecting to %s…") % name)
        if not self._run_long(work, "mpvtk-reconnect-server"):
            # A second press while one is in flight: say so rather than
            # look like a dead button.
            self.set_status(_("Already reconnecting."))
        self.invalidate()

    def _servers_snapshot(self):
        try:
            return self.controller.list_servers() or []
        except Exception:
            log.debug("list_servers failed", exc_info=True)
            return []

    def _server_problem_dialog(self, server, problem):
        """What to do about a server that still will not connect.

        Two buttons, always both, for the reason in ``_server_row``: the
        verdict decides the emphasis and never the availability.
        """
        name = server.get("name") or server.get("address") or ""
        if problem == CONNECT_SIGNED_OUT:
            detail = _("%s answered, but it no longer accepts the saved "
                       "login. Signing in again keeps its downloads and "
                       "settings.") % name
        else:
            detail = _("%s did not answer. It may be switched off, or this "
                       "machine may not be able to reach it right now.") % name

        def build():
            return Dialog("srvfail", self._dialog_shell("srvfail", [
                Text(_("Couldn't connect to %s") % name, size="title",
                     bold=True),
                Text(detail, size="small", color=theme.SUBTLE_FG, wrap=True,
                     w=420),
                self._dialog_buttons([
                    Button(_("Cancel"), id="srvfail-cancel",
                           on_click=self._close_dialog),
                    Button(_("Retry"), id="srvfail-retry", icon="refresh",
                           on_click=lambda: (
                               self._close_dialog(),
                               self.reconnect_server(server.get("uuid"),
                                                     switch=False))),
                    Button(_("Sign In Again"), id="srvfail-reauth",
                           icon="person",
                           on_click=lambda: (self._close_dialog(),
                                             self.open_reauth(server))),
                ]),
            ], w=480), on_dismiss=self._close_dialog)

        self._show_dialog(build)

    def open_reauth(self, server):
        """Send the user to the login form as a *replacement* for this
        server, so its uuid -- and therefore its downloads -- survive."""
        self.show_login(reauth={
            "uuid": server.get("uuid"),
            "name": server.get("name"),
            "address": server.get("address"),
            "username": server.get("username"),
        })
    def _auto_dl_on(self, sv, on_logins=None):
        """Is this row's server set to download unattended?

        The answer is the **account's**, not the login's: two rows that are
        two addresses for one server are one permission, and this is where
        that shows (R14). That has not changed.

        What has is where the answer comes from. Asked one row at a time it
        reaches `UserManager.auto_download_accounts()`, which takes the
        registry lock -- the lock `save()` holds across two durable writes and
        two directory fsyncs, and the one `server_id_for` and `actor_for` are
        documented as never taking. On the render path that was an acquisition
        per server row per frame, and ticking the checkbox calls `save()`. So
        a caller drawing several rows reads `on_logins` once for the screen
        and passes it in; the membership test is still account-keyed, because
        that set is built from accounts.
        """
        if on_logins is not None:
            return sv.get("uuid") in on_logins
        if self.controller is None:
            return False
        return bool(self.controller.auto_download_on(sv.get("uuid")))

    def _auto_dl_logins(self):
        """The set `_auto_dl_on` takes, read once per screen build."""
        if self.controller is None:
            return set()
        try:
            return self.controller.auto_download_logins()
        except Exception:
            log.debug("auto_download_logins failed", exc_info=True)
            return set()
    def _toggle_auto_server(self, uuid):
        """Include/exclude one server from automatic downloads.

        An explicit include-list: empty means none, so there is nothing to
        materialize on the way out of the last one. The store is the user
        registry rather than the config -- `users.set_auto_download`, R14 --
        so this writes no setting and the Checkbox needs the repaint below to
        move (mpvtk does not update one optimistically).
        """
        if not uuid or self.controller is None:
            return
        on = bool(self.controller.auto_download_on(uuid))
        self.controller.set_auto_download(uuid, not on)
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
