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
    Menu,
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
        Box([Text(b.status, size="normal", wrap=True, w=tw - 32)],
            pad=16, bg=theme.CARD_BG, radius=10, border=theme.BORDER,
            align="stretch"),
        x=(w - tw) / 2, y=max(20, h - 140), w=tw)


def _may_syncplay(b):
    """Whether to offer this user SyncPlay at all, on the current server.

    Fails open in every direction — a source without the method (the
    offline one declares it, but a test double may not), no server yet, or
    an error asking — because the cost of being wrong that way is the
    button being there, which is where it was before.
    """
    from ..user_policy import NO_SYNCPLAY

    source, server = getattr(b, "source", None), getattr(b, "server", None)
    if source is None or server is None:
        return True
    ask = getattr(source, "syncplay_access", None)
    if ask is None:
        return True
    try:
        return ask(server) != NO_SYNCPLAY
    except Exception:
        return True


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


#: The three buttons a title bar has, in the order every desktop puts them.
#: Narrower than the nav buttons on purpose: they are furniture, not
#: features, and at the nav buttons' weight they read as three more app
#: actions competing with Settings and Search.
WINDOW_CONTROL_W = 34


def window_controls(b, prefix="win", icon_size=16, w=WINDOW_CONTROL_W,
                    fg=None, gap=4):
    """Minimize / maximize / close, for a window the desktop drew no title
    bar for. See ``WindowMixin.window_controls_wanted``.

    Shared by the library's top bar and the playback HUD's, parameterized
    rather than written twice: they are the same three buttons doing the
    same three things, and the failure mode of a second copy is a window
    you can close from one screen and not the other. ``prefix`` keeps the
    node ids distinct, since a scene may not have two nodes with one id.

    Only the close button departs from the shared hover: on every desktop
    it is the one that is coloured, and it is the only one here whose
    misclick costs anything.
    """
    if not b.window_controls:
        return []

    def ctl(icon, node_id, cb, tip, hover=None):
        return Button("", id="%s-%s" % (prefix, node_id), icon=icon,
                      size=icon_size, w=w, fg=fg,
                      tip=tip, on_click=cb, flat=True, hover=hover)

    return [
        Spacer(w=gap),
        ctl("minimize", "min", b.minimize_window, _("Minimize")),
        # One button, two glyphs -- crop_square to maximize, filter_none to
        # restore -- because that is the one piece of state a title bar
        # actually shows, and a button that does not change when the window
        # does reads as broken.
        ctl("filter_none" if b.maximized else "crop_square", "max",
            b.toggle_maximized,
            _("Restore") if b.maximized else _("Maximize")),
        # `circle` as well as the colour: flat buttons wash a circle on
        # hover, and dropping it would make this the one button on the bar
        # with a different hover *shape* as well as a different colour.
        ctl("close", "close", b.close_window, _("Close"),
            hover={"fill": theme.FAV_RED, "circle": True}),
    ]


#: The corner's hit box, and the three dots drawn in it. Bigger than it
#: looks: this is the whole resize affordance on a window with no frame, and
#: a grip you have to aim at is worse than no grip at all.
RESIZE_GRIP = 22


def resize_grip(b, w, h):
    """The bottom-right corner, draggable to resize the window.

    Only where the desktop drew no frame to resize by, and never while
    maximized (there the geometry write would silently un-maximize instead
    -- see the renderer's on_mouse_down).

    On Wayland this is usually dead weight in the best way: mpv implements
    its own edge-resize zone when ``border`` is false, and takes the press
    in the compositor's protocol before it can reach a script. The grip is
    for everywhere else -- and it is what the dots on screen are for
    either way, since mpv's zone is invisible.

    A ``Float`` so the corner is the *window's* corner, not the corner of
    whatever the page happens to end in; and last in the scene, so it is
    over the now-playing bar rather than under it. The dots are ordinary
    Boxes with no interaction of their own, which is what leaves the hit
    rect underneath them the topmost thing ``node_at`` will return.
    """
    if not b.window_controls or b.maximized or not w or not h:
        return None
    dots = [
        Box([], w=3, h=3, bg=theme.SUBTLE_FG, radius=1,
            anchor="se", dx=-3, dy=-3),
        Box([], w=3, h=3, bg=theme.SUBTLE_FG, radius=1,
            anchor="se", dx=-9, dy=-3),
        Box([], w=3, h=3, bg=theme.SUBTLE_FG, radius=1,
            anchor="se", dx=-3, dy=-9),
    ]
    # No w/h on the Float itself. _arrange_overlay ignores them (it measures
    # the child), but a Column reads a child's declared `h` when it divides
    # the main axis -- so declaring one here reserved 22px of PAGE for a
    # thing that is drawn absolutely, shortening every scroll viewport and
    # floating the now-playing bar 22px off the bottom of the window. The
    # toast above does the same thing correctly: size the child, not the
    # Float.
    return Float(
        Box([Stack(dots, w=RESIZE_GRIP, h=RESIZE_GRIP)],
            id="win-resize", w=RESIZE_GRIP, h=RESIZE_GRIP,
            window_resize="se", tip=_("Resize")),
        x=w - RESIZE_GRIP, y=h - RESIZE_GRIP)


def chrome_bar(b, compact, probe=False, servers=None,
                users=None):
    # `bar_title` where a route sets one, because the bar and the page
    # heading are answering different questions. A season is the case that
    # forced it: the bar said "Season 1", the heading said "Season 1" and
    # the picker beside it said "Season 1", so the one thing the screen
    # never told you was *which show*. The bar is the outer context, so it
    # carries the series; the heading stays the season.
    title = "" if probe else (b.route.get("bar_title")
                              or b.route.get("title") or _("Home"))

    # A theme may give the top bar accent-bordered buttons; the stock look
    # leaves the Button defaults alone.
    # Themed chrome buttons (empty dict on themes that don't ask), shared
    # with the Settings tabs so the two rows always match.
    accent_style = theme.chrome_button_style()

    def nav_button(label, node_id, icon, cb, on_context=None):
        # Icon-only when compact — the icons are the same ones the
        # labels sit next to, so nothing new has to be learned. The
        # tooltip carries the label in that state: compact is exactly
        # when the button stops saying what it does, and it was the
        # only mode with neither a label nor a tip.
        return Button("" if compact else label, id=node_id, icon=icon,
                      on_click=cb, tip=label if compact else None,
                      on_context=on_context, **accent_style)

    left = []
    if b._nav.can_go_back:
        # Right-click is where the history becomes visible. The mouse's
        # forward button has no arrow of its own — it is there so an
        # accidental Back costs nothing, which does not earn permanent
        # chrome — so without this menu nothing on screen ever says the
        # forward stack exists.
        left.append(nav_button(
            _("Back"), "nav-back", "arrow_back", b.go_back,
            on_context=lambda x, y: open_history_menu(b, x, y)))
    left.append(nav_button(
        _("Home"), "nav-home", "home", b.go_home,
        # Home inherits the history menu at the root, where there is no
        # Back button to right-click — which is exactly where a forward
        # stack is most likely to exist, since backing all the way out is
        # how you get one. Only when there is history to show: an
        # otherwise-empty menu listing the page you are on is the same
        # "nothing on offer" a tile with no actions declines to open.
        on_context=(None if b._nav.can_go_back or not b._nav.forward
                    else lambda x, y: open_history_menu(b, x, y))))
    # Home only, and not while offline.
    #
    # Not offline because every row on it is a server-side IsFavorite query
    # and a downloaded subset cannot stand in for one -- an empty screen
    # would read as "you have no favourites" rather than "this needs the
    # server". Same reasoning as the Live TV tile, which is also hidden.
    #
    # Home only because on a library "favorites" already means something
    # else and better: the Favorites *filter* on that library's own filter
    # row. A top-bar button that jumps to a global screen instead is the
    # same word doing two jobs one bar apart, and dropping it gives the
    # title the space back.
    if not b._offline and (b.route or {}).get("kind") == "home":
        left.append(nav_button(
            _("Favorites"), "nav-favorites", "favorite",
            lambda: b.navigate({"kind": "favorites", "server": b.server,
                                "title": _("Favorites")})))

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
                # "large", to match the Search button glued to its right
                # and the nav buttons either side. A TextBox derives its
                # height from its type size, and Buttons do not -- so when
                # the control default moved 20 -> 17 this field alone got
                # 3.7px shorter and sat 1.9px lower than the button it
                # abuts, on every screen.
                size="large",
                w=140 if compact else 220,
                on_change=lambda v: b._search_box.__setitem__("term", v),
                on_submit=b._search),
        # The textbox submits on Enter, but a visible button is the
        # discoverable affordance (and the only one with a pointer).
        Button("", id="nav-search-go", icon="search", size="large",
               tip=_("Search"),
               on_click=lambda: b._search(
                   b._search_box.get("term", "")), **accent_style),
        nav_button(_("Settings"), "nav-settings", "settings",
                   b._open_settings),
    ]
    # A user whose SyncPlayAccess is None gets a dialog that can only fail,
    # and "Could not join the SyncPlay group." is indistinguishable from a
    # network problem. Inserted rather than appended so the order is
    # unchanged for everyone who does have it.
    if _may_syncplay(b):
        right.insert(-1, nav_button(_("SyncPlay"), "nav-syncplay", "groups",
                                    b._open_syncplay))
    # Last, past Settings: this is window furniture, and on every desktop it
    # sits outboard of everything the application put in the bar. Counted by
    # the fit probe like everything else, so the bar simply goes icon-only
    # sooner on an undecorated window rather than having a width reserved.
    right += window_controls(b)
    middle = [Spacer(w=6), Text(title, size="title", bold=True), Spacer()]
    bar = Row(
        left + middle + right,
        pad=12, gap=8 if compact else 10, align="center", h=60,
        # No fill when a gradient is painting behind it, or the flat colour
        # would simply cover the gradient up.
        bg=None if theme.topbar_gradient() else theme.PANEL_BG,
        # Drag the window by the bar, and double-click to maximize, when the
        # bar IS the title bar. Not unconditional: with a real title bar
        # above it, a top bar that also drags the window means two of them,
        # and the one that is not the system's does not obey snapping or
        # edge tiling.
        window_drag=b.window_controls,
    )
    stops = theme.topbar_gradient()
    if not stops:
        return bar
    # Several jellyfin-web themes run a horizontal ramp across the header
    # rather than a flat fill; Purple Haze's is most of its identity.
    return Stack([Gradient(stops=stops, axis="x", h=60), bar], h=60)


#: Longest history menu. A deep stack outruns the window before it outruns
#: anyone's patience, and a floating Menu does not scroll — so it is
#: trimmed at the ends furthest from the current page, which are the two
#: least likely picks.
HISTORY_MAX = 12


def history_entries(b):
    """The page stack as (label, icon, depth) rows, root first.

    Ordered as a stack rather than as two "recent first" lists, because
    that is the question the menu answers: where am I, and what is on
    either side of me. ``depth`` is the 1-based stack position a pick
    rewinds to; the pages ahead carry the depth they would occupy, so
    picking one is that many forward steps.
    """
    stack = list(b._nav.stack)
    ahead = list(reversed(b._nav.forward))     # nearest first -> in order
    here = len(stack)

    def label(route):
        # Same fallback as the top bar's title, so a page reads in the
        # menu exactly as it does in the bar above it.
        return route.get("title") or _("Home")

    rows = [(label(r), "chevron_left", i + 1) for i, r in enumerate(stack)]
    rows[-1] = (label(stack[-1]), "check", here)
    rows += [(label(r), "chevron_right", here + i + 1)
             for i, r in enumerate(ahead)]
    if len(rows) <= HISTORY_MAX:
        return rows
    # Keep the current page in the window: it is the anchor the rest is
    # read against, and trimming from one end alone would drop it off a
    # deep stack. Biased toward the pages behind, which are the ones
    # anyone is likely to want.
    lo = min(max(0, here - 1 - HISTORY_MAX // 2),
             len(rows) - HISTORY_MAX)
    return rows[lo:lo + HISTORY_MAX]


def open_history_menu(b, x, y):
    b._menu = {"kind": "history", "x": x, "y": y}
    b.invalidate()


def history_menu_node(b):
    m = b._menu
    entries = history_entries(b)

    def pick(index, _value):
        b._close_menu()
        if not 0 <= index < len(entries):
            return
        depth = entries[index][2]
        if depth < b._nav.depth:
            b.go_back_to(depth)
        elif depth > b._nav.depth:
            b.go_forward_to(depth)

    return Menu("historymenu", [e[0] for e in entries], m["x"], m["y"],
                icons=[e[1] for e in entries],
                on_select=pick, on_dismiss=b._close_menu)


def banner(b):
    # Update first: the offline banner is persistent, so checking it
    # first meant an update notice was never seen while offline.
    if b._update:
        return Row([
            Text(_("Update available: %s") % b._update["version"],
                 size="normal"),
            Spacer(),
            Button(_("Open"), id="banner-open",
                   on_click=lambda: b._open_url(b._update["url"])),
            Button(_("Dismiss"), id="banner-dismiss",
                   on_click=b._dismiss_update),
        ], pad=10, gap=10, align="center", h=48, bg=theme.ACCENT_SOFT)
    if b._offline:
        return Row([
            Text(_("Offline — showing what's available."), size="normal"),
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
    row = [Icon("file_download", 20), Text(left, size="normal")]
    if pct is not None:
        row.append(Progress(pct / 100.0, w=160))
        row.append(Text("%d%%" % pct, size="small", w=48,
                        color=theme.SUBTLE_FG))
    row += [
        Spacer(),
        Button(_("View Downloads"), id="dlbar-view",
               on_click=lambda: b.open_settings("downloads")),
    ]
    return Row(row, pad=10, gap=10, align="center", h=44,
               bg=theme.PANEL_BG)

