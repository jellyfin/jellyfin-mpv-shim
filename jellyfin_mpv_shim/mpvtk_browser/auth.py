"""Login, the lock screen, and user switching.

Covers the login form and Quick Connect, the startup-PIN lock gate, user
switching with its PIN prompt, and per-user PIN setup.

State on ``self``: ``_login`` / ``_login_error`` mirror the login form
(the renderer owns the live text; we mirror so Connect can read all three
fields at once), and ``_pin`` / ``_pin_error`` / ``_locked`` drive the lock
gate. Reads ``self.source`` and ``self.server``; leaves the screen via
core's ``navigate`` / ``nav_stack``.
"""

import logging

from ..i18n import _
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dialog,
    Icon,
    Row,
    Spacer,
    Text,
    TextBox,
)
from . import theme

log = logging.getLogger("mpvtk_browser.auth")


class AuthMixin:

    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "locked": (None, "_render_locked"),
        "login": (None, "_render_login"),
    }

    # ------------------------------------------------------------ users

    def _users(self):
        """Local users for the switcher: ``[{id, name, locked, active}]``."""
        if self.controller is None:
            return []
        try:
            return list(self.controller.list_users() or [])
        except Exception:
            log.debug("list_users failed", exc_info=True)
            return []

    def _switch_user(self, user):
        if user.get("active"):
            return
        if user.get("locked"):
            self._ask_pin(user)
        else:
            self._do_switch_user(user, None)

    def _ask_pin(self, user):
        state = {"pin": "", "error": None}

        def build():
            rows = [Text(_("Switch to %s") % user.get("name", ""), size=22,
                         bold=True)]
            if state["error"]:
                rows.append(Text(state["error"], size=15, color=theme.FAV_RED))
            rows += [
                TextBox("switch-pin", placeholder=_("PIN"), mask=True, w=240,
                        on_change=lambda v: state.__setitem__("pin", v),
                        on_submit=lambda v: submit()),
                self._dialog_buttons([
                    Button(_("Cancel"), id="switch-cancel",
                           on_click=self._close_dialog),
                    Button(_("Switch"), id="switch-ok", on_click=submit)]),
            ]
            return Dialog("switchpin", self._dialog_shell("switchpin", rows),
                          on_dismiss=self._close_dialog)

        def submit():
            self._do_switch_user(user, state["pin"], on_bad_pin=lambda: (
                state.__setitem__("error", _("Incorrect PIN.")),
                self._show_dialog(build)))
        self._show_dialog(build)

    def _do_switch_user(self, user, pin, on_bad_pin=None):
        ep = self._epoch

        def work():
            return self.controller.switch_user(user.get("id"), pin)

        def done(source):
            if source is False:
                if on_bad_pin is not None:
                    on_bad_pin()
                return
            self._close_dialog()
            if source is None:
                # switched fine, but that user has no reachable server and
                # nothing downloaded — the login screen, not a stuck dialog.
                self._locked = False
                self.show_login()
                return
            self.set_source(source)
        self.run_async(work, done, ep)

    def _open_pin_setup(self, u):
        # Seeded, not defaulted off: saving a changed PIN used to clear
        # the user's startup requirement without saying so.
        state = {"cur": "", "new": "", "confirm": "",
                 "startup": bool(u.get("require_startup")), "error": None}

        def build():
            rows = [Text(_("Set PIN for %s") % u.get("name", ""), size=22,
                         bold=True)]
            if state["error"]:
                rows.append(Text(state["error"], size=15, color=theme.FAV_RED))
            if u.get("locked"):
                rows.append(self._pin_field(_("Current PIN"), "ps-cur", state,
                                            "cur"))
            rows += [
                self._pin_field(_("New PIN"), "ps-new", state, "new"),
                self._pin_field(_("Confirm"), "ps-confirm", state, "confirm"),
                Checkbox(_("Require this PIN at startup"), state["startup"],
                         id="ps-startup",
                         on_toggle=lambda: (state.__setitem__(
                             "startup", not state["startup"]),
                             self._show_dialog(build))),
                Row([
                    Button(_("Remove PIN"), id="ps-remove",
                           on_click=lambda: save(remove=True))
                    if u.get("locked") else Spacer(h=0),
                    Spacer(),
                    Button(_("Cancel"), id="ps-cancel",
                           on_click=self._close_dialog),
                    Button(_("Save"), id="ps-ok", on_click=save),
                ], gap=10, align="center", justify="end"),
            ]
            return Dialog("pinsetup",
                          self._dialog_shell("pinsetup", rows, w=460),
                          on_dismiss=self._close_dialog)

        def save(remove=False):
            if self.controller is None:
                return self._close_dialog()
            if u.get("locked"):
                # Fail CLOSED: an exception here used to fall through and
                # apply the new PIN (or Remove PIN) without the current one
                # ever being confirmed.
                try:
                    ok = self.controller.unlock_user(u.get("id"),
                                                     state["cur"])
                except Exception:
                    log.debug("pin verify failed", exc_info=True)
                    ok = False
                if not ok:
                    state["error"] = _("Current PIN is incorrect.")
                    return self._show_dialog(build)
            if not remove and not state["new"]:
                # Empty new+confirm compared equal and fell through to
                # set_pin(None), i.e. Save on a "Set PIN" dialog quietly
                # removed the lock.
                state["error"] = _("Enter a new PIN.")
                return self._show_dialog(build)
            if not remove and state["new"] != state["confirm"]:
                state["error"] = _("The PINs don't match.")
                return self._show_dialog(build)
            self._safe(lambda c: c.set_user_pin(
                u.get("id"), None if remove else state["new"],
                require_startup=state["startup"]))
            self._close_dialog()
            self._after_users_changed()
        self._show_dialog(build)

    @staticmethod
    def _pin_field(label, node_id, state, key):
        return Row([Text(label, w=140, size=16, color=theme.SUBTLE_FG),
                    TextBox(node_id, placeholder=label, mask=True, w=200,
                            on_change=lambda v: state.__setitem__(key, v))],
                   gap=10, align="center")

    # --------------------------------------------------------------- login

    def show_login(self):
        """Show the add-server / login screen.

        Only resets the nav stack when there is nowhere to go back *to*: with
        servers already connected this is "add another", and cancelling has
        to return you to the library rather than trapping you on the form.
        """
        route = {"kind": "login", "title": _("Add Server")}
        if self.server is None:
            self.navigate(route, reset=True)
        else:
            self.navigate(route)

    def _render_login(self, route, size):
        def field(fid, ph, key, mask=False):
            return Row([
                Text(ph, w=140, size=17, color=theme.SUBTLE_FG),
                TextBox(fid, text=self._login[key], placeholder=ph, mask=mask,
                        w=360,
                        on_change=lambda v, k=key: self._login.__setitem__(
                            k, v)),
            ], gap=12, align="center")

        qc = route.get("_qc")
        rows = [Text(_("Connect to Jellyfin"), size=28, bold=True)]
        if self._login_error:
            rows.append(Text(self._login_error, size=15, color=theme.FAV_RED))

        known = []
        if self.controller is not None and not qc:
            try:
                known = self.controller.known_servers() or []
            except Exception:
                known = []
        if known:
            rows.append(Text(_("Previously added servers"), size=15,
                             color=theme.SUBTLE_FG))
            for i, k in enumerate(known):
                addr = k.get("address", "")
                rows.append(Row([
                    Icon("radio", 16, color=theme.SUBTLE_FG),
                    Text(k.get("name") or addr, size=16, flex=1),
                    Button(_("Use"), id="login-known-%d" % i, size=15,
                           on_click=lambda a=addr: self._use_known_server(a)),
                ], id="login-known-row-%d" % i, pad=8, gap=10, radius=6,
                   align="center", bg=theme.PANEL_BG))

        if qc:
            # Quick Connect: the user types this code into any signed-in
            # Jellyfin client; we poll until the server authorizes it.
            rows += [
                Text(_("Quick Connect"), size=20, bold=True),
                Text(_("Enter this code in the Jellyfin app or web client:"),
                     size=15, color=theme.SUBTLE_FG, wrap=True, w=460),
                Text(qc.get("code") or _("Requesting…"), size=44, bold=True,
                     align="center"),
                Text(qc.get("status") or "", size=15,
                     color=theme.SUBTLE_FG, align="center"),
                self._dialog_buttons([
                    Button(_("Cancel"), id="login-qc-cancel",
                           on_click=lambda: self._cancel_quick_connect(route)),
                ]),
            ]
        else:
            rows += [
                field("login-server", _("Server URL"), "server"),
                field("login-user", _("Username"), "user"),
                field("login-pass", _("Password"), "pass", mask=True),
                Row([
                    Button(_("Use Quick Connect"), id="login-qc",
                           icon="radio",
                           on_click=lambda: self._start_quick_connect(route)),
                    Spacer(),
                    # Only offer Cancel when there's something to go back to;
                    # on a first run there is no library behind this screen.
                    Button(_("Cancel"), id="login-cancel",
                           on_click=self.go_back)
                    if len(self.nav_stack) > 1 else Spacer(h=0),
                    Button(_("Connect"), id="login-connect",
                           on_click=self._do_login),
                ], gap=10, align="center"),
            ]

        form = Column(rows, pad=28, gap=16, bg=theme.CARD_BG, radius=12,
                      border=theme.BORDER, w=560, align="stretch")
        return Box([Spacer(),
                    Row([Spacer(), form, Spacer()]),
                    Spacer()],
                   flex=1, direction="column", align="stretch", gap=10)

    def _use_known_server(self, address):
        self._login["server"] = address
        self.invalidate()

    def _start_quick_connect(self, route):
        server = (self._login.get("server") or "").strip()
        if not server:
            self._login_error = _("Enter the server URL first.")
            self.invalidate()
            return
        if self.controller is None:
            return
        self._login_error = None
        route["_qc"] = {"code": None, "status": _("Contacting the server…"),
                        "cancelled": False}
        self.invalidate()
        ep = self._epoch

        def on_code(code):
            qc = route.get("_qc")
            if qc is not None:
                qc["code"] = code
                qc["status"] = _("Waiting for approval…")
                self.invalidate()

        def work():
            return self.controller.quick_connect(
                server, on_code,
                lambda: (route.get("_qc") or {}).get("cancelled", True))

        def done(ok):
            if (route.get("_qc") or {}).get("cancelled"):
                return
            route.pop("_qc", None)
            if ok:
                self._login_error = None
                self._after_login()
            else:
                self._login_error = _("Quick Connect was not approved.")
        self.run_async(work, done, ep)

    def _cancel_quick_connect(self, route):
        qc = route.get("_qc")
        if qc is not None:
            qc["cancelled"] = True    # the worker polls this and gives up
        route.pop("_qc", None)
        self.invalidate()

    def _do_login(self):
        if self.controller is None:
            return
        info = dict(self._login)
        self._login_error = _("Connecting…")
        self.invalidate()
        ep = self._epoch

        def work():
            return self.controller.add_server(
                info["server"], info["user"], info["pass"])

        def done(ok):
            if ok:
                self._login_error = None
                self._after_login()
            else:
                self._login_error = _(
                    "Could not connect. Please check your details.")
        self.run_async(work, done, ep)

    def _after_login(self):
        source = None
        if self.controller is not None:
            try:
                source = self.controller.rebuild_source()
            except Exception:
                log.warning("rebuild_source failed", exc_info=True)
        if source is not None:
            self.set_source(source)

    # -------------------------------------------------------------- locked

    def show_locked(self):
        """Show the startup-PIN unlock gate.

        Idempotent: re-locking an already-locked UI must not wipe a PIN the
        user is halfway through typing (the tray can fire show/hide at any
        moment)."""
        if self._locked:
            return
        self._locked = True
        self._pin["pin"] = ""
        self._pin_error = None
        self.navigate({"kind": "locked", "title": _("Locked")}, reset=True)

    def maybe_relock(self):
        """Re-gate the UI behind the startup PIN when the window is released
        or re-surfaced. Unlocking once must not leave the client open for the
        rest of the process's life — closing to the tray and re-raising
        re-prompts, matching the Tk browser."""
        if self.controller is None:
            return
        try:
            if self.controller.needs_unlock():
                self.show_locked()
        except Exception:
            log.debug("relock check failed", exc_info=True)

    def _render_locked(self, route, size):
        """Startup PIN gate.

        A full page rather than a modal, and it offers the other local users
        — a locked user must not be able to lock the whole client out, which
        is what a bare PIN prompt with no way past it amounts to."""
        users = [u for u in self._users() if not u.get("active")]
        active = next((u.get("name") for u in self._users()
                       if u.get("active")), None)
        rows = [
            Text(_("Enter your PIN"), size=30, bold=True),
            Text(_("%s is locked.") % active if active else "",
                 size=16, color=theme.SUBTLE_FG),
        ]
        if self._pin_error:
            rows.append(Text(self._pin_error, size=15, color=theme.FAV_RED))
        rows += [
            TextBox("lock-pin", text="", placeholder=_("PIN"), mask=True,
                    w=260, on_change=lambda v: self._pin.__setitem__("pin", v),
                    on_submit=lambda v: self._do_unlock()),
            Row([Button(_("Unlock"), id="lock-unlock", icon="lock",
                        on_click=self._do_unlock)], gap=10, justify="end"),
        ]
        if users:
            rows.append(Spacer(h=6))
            rows.append(Text(_("Or switch to another user"), size=15,
                             color=theme.SUBTLE_FG))
            for i, u in enumerate(users):
                rows.append(Row([
                    Icon("lock" if u.get("locked") else "person", 18),
                    Text(u.get("name", "?"), size=17, flex=1),
                    Button(_("Switch"), id="lock-switch-%d" % i, size=15,
                           on_click=lambda u=u: self._switch_user(u)),
                ], id="lock-user-%d" % i, pad=8, gap=10, radius=6,
                   align="center", bg=theme.PANEL_BG,
                   hover={"fill": theme.BUTTON_BG}))
        form = Column(rows, pad=28, gap=14, bg=theme.CARD_BG, radius=12,
                      border=theme.BORDER, w=460, align="stretch")
        return Box([Spacer(), Row([Spacer(), form, Spacer()]), Spacer()],
                   flex=1, direction="column", align="stretch")

    def _do_unlock(self):
        if self.controller is None:
            return
        pin = self._pin.get("pin", "")
        ep = self._epoch

        def work():
            # False means the PIN was wrong; None means it was right but
            # nothing could be built (no server answered and nothing is
            # downloaded). Conflating the two reported a correct PIN as
            # incorrect — permanently so with work_offline on, since the
            # connect is skipped and there is never a live source.
            if not self.controller.unlock(pin):
                return False
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is False:
                self._pin_error = _("Incorrect PIN.")
                return
            self._pin_error = None
            self._pin["pin"] = ""
            if source is None:
                self._locked = False
                self.show_login()
                return
            self.set_source(source)
        self.run_async(work, done, ep)

    def _busy(self):
        return Box(
            [Spacer(), Row([Spacer(), Busy(), Spacer()]), Spacer()],
            flex=1, direction="column", align="stretch",
        )
