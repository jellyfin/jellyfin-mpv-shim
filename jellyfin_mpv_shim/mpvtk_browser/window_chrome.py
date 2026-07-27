"""The window furniture around whatever page is showing.

The top bar, the offline/update banner, the download bar and the toast.
Function-per-piece taking the browser, mirroring ``hud.py`` — these are large
widget trees that read a lot of shell state and own none of it, so a class
would only give them a second ``self`` to confuse with the first.

Not to be confused with ``components/chrome.py``, which is the *content
area's* primitives (the busy and error states). This is the frame; that is
what goes inside it.

``build()`` stays on the browser: assembling the window from these pieces,
the page, the dialogs and the now-playing bar is the shell's actual job. So
does everything with state or a thread behind it — ``set_status``,
``_arm_toast_clear``, ``set_download_status``, ``_poll_download_status``,
``notify_update`` — because the toast timer and the download-bar poller are
registered in the browser's daemon table by attribute name.
"""

import time

from ..i18n import _
from ..mpvtk.layout import natural_size
from ..mpvtk.widgets import (
    Box,
    Button,
    Dropdown,
    Float,
    Gradient,
    Icon,
    Progress,
    Row,
    Spacer,
    Stack,
    Text,
    TextBox,
)
from . import theme


def toast_node(b, w, h):
    """Status messages as a floating toast, on every screen.

    ``status`` used to be rendered in exactly one place — the Settings
    tab — while being written from fourteen, including _edit_call's
    "the change could not be applied". So a rejected Add to Playlist
    from the home screen wrote its error to a field that never reached
    a pixel, which is the very thing _edit_call exists to prevent."""
    if not b.status:
        return None
    left = b.TOAST_SECS - (time.time() - (b._status_at or 0))
    if left <= 0:
        b.status = ""
        return None
    b._arm_toast_clear(left)
    tw = min(max(320, len(b.status) * 9), max(360, w - 80))
    return Float(
        Box([Text(b.status, size=16, wrap=True, w=tw - 32)],
            pad=16, bg=theme.CARD_BG, radius=10, border=theme.BORDER,
            align="stretch"),
        x=(w - tw) / 2, y=max(20, h - 140), w=tw)


def chrome(b, w):
    # Fit probe instead of a hardcoded breakpoint: the bar goes
    # icon-only exactly when the labelled version wouldn't leave
    # the title its minimum room at this window width — however
    # many switchers/buttons this session happens to show.
    #
    # The probe and the real bar are built from one snapshot of the
    # servers and users. Building the bar twice per frame asked the
    # source and the controller for both lists twice, on the loop
    # thread — cheap today, but it is a list_users() and a servers()
    # round the render path that nothing forced to be cheap.
    servers, users = chrome_lists(b)
    probe = chrome_bar(b, compact=False, probe=True,
                             servers=servers, users=users)
    compact = natural_size(probe)[0] + b.TITLE_MIN_W > w
    return chrome_bar(b, compact=compact, servers=servers, users=users)


def chrome_lists(b):
    try:
        servers = b.source.servers()
    except Exception:
        servers = []
    return servers, b._users()


def chrome_bar(b, compact, probe=False, servers=None,
                users=None):
    title = "" if probe else (b.route.get("title") or _("Home"))

    # A theme may give the top bar accent-bordered buttons; the stock look
    # leaves the Button defaults alone.
    # Themed chrome buttons (empty dict on themes that don't ask), shared
    # with the Settings tabs so the two rows always match.
    accent_style = theme.chrome_button_style()

    def nav_button(label, node_id, icon, cb):
        # Icon-only when compact — the icons are the same ones the
        # labels sit next to, so nothing new has to be learned. The
        # tooltip carries the label in that state: compact is exactly
        # when the button stops saying what it does, and it was the
        # only mode with neither a label nor a tip.
        return Button("" if compact else label, id=node_id, icon=icon,
                      on_click=cb, tip=label if compact else None,
                      **accent_style)

    left = []
    if b._nav.can_go_back:
        left.append(nav_button(_("Back"), "nav-back", "arrow_back",
                               b.go_back))
    left.append(nav_button(
        _("Home"), "nav-home", "home",
        lambda: b.navigate({"kind": "home", "server": b.server},
                              reset=True)))

    right = []
    if servers is None:
        servers = chrome_lists(b)[0]
    if len(servers) > 1:
        cur = next((i for i, s in enumerate(servers)
                    if s["uuid"] == b.server), 0)
        # sized to its content within bounds (so long names count in
        # the fit probe); overlong labels ellipsize renderer-side
        right.append(Dropdown(
            "nav-server", [s["name"] for s in servers],
            selected=cur, min_w=110, tip=_("Server"),
            max_w=150 if compact else 260,
            on_select=lambda i, v: b._switch_server(servers[i]["uuid"])))
    if users is None:
        users = b._users()
    # Not while offline: switching user reconnects, which cannot work
    # with no server, and Tk gated it for that reason.
    if len(users) > 1 and not b._offline:
        cur = next((i for i, u in enumerate(users)
                    if u.get("active")), 0)
        right.append(Dropdown(
            "nav-user",
            [u.get("name", "?") for u in users],
            selected=cur, min_w=100, tip=_("User"),
            max_w=130 if compact else 200, force=True,
            icons=["lock" if u.get("locked") else "person" for u in users],
            on_select=lambda i, v: b._switch_user(users[i])))
    right += [
        TextBox("nav-search", placeholder=_("Search…"),
                w=140 if compact else 220,
                on_change=lambda v: b._search_box.__setitem__("term", v),
                on_submit=b._search),
        # The textbox submits on Enter, but a visible button is the
        # discoverable affordance (and the only one with a pointer).
        Button("", id="nav-search-go", icon="search", size=18,
               tip=_("Search"),
               on_click=lambda: b._search(
                   b._search_box.get("term", "")), **accent_style),
        nav_button(_("SyncPlay"), "nav-syncplay", "groups",
                   b._open_syncplay),
        nav_button(_("Settings"), "nav-settings", "settings",
                   b._open_settings),
    ]
    middle = [Spacer(w=6), Text(title, size=22, bold=True), Spacer()]
    bar = Row(
        left + middle + right,
        pad=12, gap=8 if compact else 10, align="center", h=60,
        # No fill when a gradient is painting behind it, or the flat colour
        # would simply cover the gradient up.
        bg=None if theme.topbar_gradient() else theme.PANEL_BG,
    )
    stops = theme.topbar_gradient()
    if not stops:
        return bar
    # Several jellyfin-web themes run a horizontal ramp across the header
    # rather than a flat fill; Purple Haze's is most of its identity.
    return Stack([Gradient(stops=stops, axis="x", h=60), bar], h=60)


def banner(b):
    # Update first: the offline banner is persistent, so checking it
    # first meant an update notice was never seen while offline.
    if b._update:
        return Row([
            Text(_("Update available: %s") % b._update["version"],
                 size=16),
            Spacer(),
            Button(_("Open"), id="banner-open",
                   on_click=lambda: b._open_url(b._update["url"])),
            Button(_("Dismiss"), id="banner-dismiss",
                   on_click=b._dismiss_update),
        ], pad=10, gap=10, align="center", h=48, bg=theme.ACCENT_SOFT)
    if b._offline:
        return Row([
            Text(_("Offline — showing what's available."), size=16),
            Spacer(),
            Button(_("Configure Servers"), id="banner-servers",
                   on_click=b.show_login),
            Button(_("Retry"), id="banner-retry",
                   on_click=b._retry_connect),
            # An amber warning wash: the theme's WARN_AMBER darkened far
            # enough to sit behind body text rather than beside it.
        ], pad=10, gap=10, align="center", h=48,
            bg=theme.mix(theme.WARN_AMBER, theme.WINDOW_BG, 0.72))
    return None


def download_bar(b):
    """A persistent bar while downloads are outstanding, with a way into
    the manager. Downloads are otherwise completely invisible once the
    confirm dialog closes."""
    st = b._dl_status
    if not st or not st.get("pending"):
        return None
    name = st.get("name") or ""
    pct = st.get("percent")
    left = (_("Downloading %(name)s — %(n)d remaining")
            if name else _("Downloading — %(n)d remaining")) % {
        "name": name, "n": st["pending"]}
    row = [Icon("file_download", 20), Text(left, size=16)]
    if pct is not None:
        row.append(Progress(pct / 100.0, w=160))
        row.append(Text("%d%%" % pct, size=15, w=48,
                        color=theme.SUBTLE_FG))
    row += [
        Spacer(),
        Button(_("View Downloads"), id="dlbar-view",
               on_click=lambda: b.open_settings("downloads")),
    ]
    return Row(row, pad=10, gap=10, align="center", h=44,
               bg=theme.PANEL_BG)

