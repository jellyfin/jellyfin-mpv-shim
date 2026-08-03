"""The Settings route.

Six tabs -- general, home screen, display, servers & users, downloads,
logs. This
module is the frame: the route entry, the tab bar, and the dispatch to
whichever tab is selected. Each tab is a mixin in its own module, composed
into ``SettingsMixin`` below.

The home-screen and display tabs are the odd ones out: unlike every other
setting here, their contents live on the *server* (DisplayPreferences,
shared with jellyfin-web), so they load and save asynchronously rather than
through the config module. They sit next to each other in the tab bar for
that reason.

State on ``self``: ``_config_obj`` (the settings accessor; None means the
real config module, tests inject a fake), ``_sync_path`` (download-folder
field mirror), ``_dl_thread`` (the downloads poller) and ``_log_thread``
(the log tail), both started via core's ``_start_daemon``. The pollers run
on foreign threads, so they write then call ``invalidate()``; they exit on
``_shutdown_evt``, which is only ever set at shutdown, or as soon as the
user leaves the tab they belong to.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Column,
    Row,
)
from .. import theme
from ..components import chrome, controls

from .base import SettingsBase
from .display import DisplayTabMixin
from .downloads import DownloadsTabMixin
from .general import GeneralTabMixin
from .home import HomeTabMixin
from .logs import LogsTabMixin
from .servers import ServersTabMixin


class SettingsMixin(
    GeneralTabMixin,
    HomeTabMixin,
    DisplayTabMixin,
    ServersTabMixin,
    DownloadsTabMixin,
    LogsTabMixin,
    SettingsBase,
):
    """The Settings route, composed from one mixin per tab.

    Inheritance rather than composition for the same reason the
    gateway and the player mixins use it: these all read and write the
    same browser state, and an owned object would need a
    back-reference to every piece of it.
    """

    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "settings": (None, "_render_settings"),
    }
    #: "display" sits next to "home" because they are the two tabs whose
    #: contents live on the server rather than in this installation's
    #: config, and grouping them is the only hint the tab bar can give.
    SETTINGS_TABS = ("general", "home", "display", "servers", "downloads",
                     "logs")
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
    def _render_settings(self, route, size):
        tab = route.get("_tab", "general")
        labels = {"general": _("General"), "home": _("Home Screen"),
                  "display": _("Display"),
                  "servers": _("Servers & Users"),
                  "downloads": _("Downloads"), "logs": _("Logs")}
        # Same treatment as the top bar's buttons (accent border + hover
        # glow) on themes that ask for it; the selected tab keeps its accent
        # fill either way — including under the pointer, which is controls
        # .tab_btn's whole job.
        tab_style = theme.chrome_button_style()

        def tab_button(t):
            return controls.tab_btn(
                labels[t], "stab-" + t, tab == t,
                lambda t=t: self._set_settings_tab(route, t),
                style=tab_style)

        # Wrapped: six translated labels do not fit a narrow page, and
        # "Logs" -- the last one -- was drawn off the right edge at 200%.
        tabs = chrome.wrap_row([tab_button(t) for t in self.SETTINGS_TABS],
                               (size[0] if size else 0) - 2 * 12, gap=8)
        body = {
            "home": self._settings_home,
            "display": self._settings_display,
            "servers": self._settings_servers,
            "downloads": self._settings_downloads,
            "logs": self._settings_logs,
        }.get(tab, self._settings_general)(route, size)
        head = [Row([tabs], pad=12)]
        return Column(head + [body], flex=1, align="stretch")
    def _set_settings_tab(self, route, tab):
        route["_tab"] = tab
        # Drop the home layout so entering the tab re-reads it. It is a
        # server-side, cross-client setting, so a cached copy goes stale the
        # moment the user touches Jellyfin Web — and saving from a stale copy
        # would overwrite what they did there.
        for key in ("_home_layout", "_home_error", "_home_loading",
                    "_display_prefs", "_display_error", "_display_loading"):
            route.pop(key, None)
        self.status = ""
        self.invalidate()
