"""The Settings route.

Seven tabs -- general, browse, playback, home screen, servers & users,
downloads, logs. This module is the frame: the route entry, the tab bar, and
the dispatch to whichever tab is selected. Each tab is a mixin in its own
module, composed into ``SettingsMixin`` below.

**The first three are one renderer, not three.** They are the config form,
split across tabs by ``config.TAB_SECTIONS``; ``_settings_form`` reads the
tab off the route. They were a single page until it reached eleven groups
and about a hundred controls in one scroll.

The home-screen tab is the odd one out: unlike every other setting here, its
contents live on the *server* (DisplayPreferences, shared with
jellyfin-web), so it loads and saves asynchronously rather than through the
config module. It absorbed the old "Display" tab, which held one checkbox
out of the same document governing two home-screen rows.

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
    #: General/Browse/Playback are one form each, split out of what was a
    #: single hundred-control page (see config.TAB_SECTIONS); they are
    #: adjacent because they are the same kind of thing. "Display" is gone:
    #: it held exactly one preference, about two home-screen rows, and it
    #: now sits on the Home Screen tab beside the layout it belongs to.
    SETTINGS_TABS = ("general", "browse", "playback", "home", "servers",
                     "downloads", "logs")
    #: tab -> renderer method name. A table rather than the inline dict it
    #: used to be, because "every tab in the bar has a renderer" stopped
    #: being answerable from the method names alone once three tabs started
    #: sharing ``_settings_form``; a dead tab raises when clicked, so it is
    #: worth being able to check. tests/test_settings_mixins.py does.
    TAB_RENDERERS = {
        "general": "_settings_form",
        "browse": "_settings_form",
        "playback": "_settings_form",
        "home": "_settings_home",
        "servers": "_settings_servers",
        "downloads": "_settings_downloads",
        "logs": "_settings_logs",
    }
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
        labels = {"general": _("General"), "browse": _("Browse"),
                  "playback": _("Playback"), "home": _("Home Screen"),
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
        body = getattr(self, self.TAB_RENDERERS.get(tab, "_settings_form"))(
            route, size)
        head = [Row([tabs], pad=12)]
        return Column(head + [body], flex=1, align="stretch")
    def _set_settings_tab(self, route, tab):
        route["_tab"] = tab
        # Drop the home screen's state so entering that tab re-reads it. It
        # is server-side and cross-client, so a cached copy goes stale the
        # moment the user touches Jellyfin Web — and saving from a stale copy
        # would overwrite what they did there. Both halves (layout and the
        # artwork preference) come from one fetch, so they clear together.
        for key in ("_home_layout", "_home_error", "_home_loading",
                    "_display_prefs"):
            route.pop(key, None)
        self.status = ""
        self.invalidate()
