"""The Settings route.

Five tabs -- general, home screen, servers & users, downloads, logs. This
module is the frame: the route entry, the tab bar, and the dispatch to
whichever tab is selected. Each tab is a mixin in its own module, composed
into ``SettingsMixin`` below.

The home-screen tab is the odd one out: unlike every other setting here, its
layout lives on the *server* (DisplayPreferences, shared with jellyfin-web),
so it loads and saves asynchronously rather than through the config module.

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
    Button,
    Column,
    Row,
)
from .. import theme

from .base import SettingsBase
from .downloads import DownloadsTabMixin
from .general import GeneralTabMixin
from .home import HomeTabMixin
from .logs import LogsTabMixin
from .servers import ServersTabMixin


class SettingsMixin(
    GeneralTabMixin,
    HomeTabMixin,
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
    SETTINGS_TABS = ("general", "home", "servers", "downloads", "logs")
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
                  "servers": _("Servers & Users"),
                  "downloads": _("Downloads"), "logs": _("Logs")}
        tabs = Row([
            Button(labels[t], id="stab-" + t,
                   bg=theme.ACCENT if tab == t else theme.BUTTON_BG,
                   fg=theme.ACCENT_FG if tab == t else theme.TEXT_FG,
                   on_click=lambda t=t: self._set_settings_tab(route, t))
            for t in self.SETTINGS_TABS
        ], gap=8)
        body = {
            "home": self._settings_home,
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
        for key in ("_home_layout", "_home_error", "_home_loading"):
            route.pop(key, None)
        self.status = ""
        self.invalidate()
