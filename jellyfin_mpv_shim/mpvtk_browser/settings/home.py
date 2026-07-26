"""The Home Screen tab.

The odd one out: unlike every other setting, this layout lives on the
*server* (DisplayPreferences, shared with jellyfin-web), so it loads and saves
asynchronously rather than through the config module. That is why this tab
alone has a loading state and a retry.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Button,
    Column,
    Dropdown,
    Row,
    Text,
    VScroll,
)
from .. import home_sections, theme


class HomeTabMixin:

    def _settings_home(self, route, size):
        """Per-slot dropdowns, like jellyfin-web's home screen settings.

        This layout lives on the server, not in the shim's config, so it is
        loaded and saved asynchronously and shared with every other Jellyfin
        client this user runs.
        """
        if not hasattr(self.source, "save_home_layout"):
            # The offline source: there is no server to hold the layout, and
            # the downloaded rows are not the configurable sections anyway.
            # Offering dropdowns here would fail only at save time.
            return VScroll(Column([
                Text(_("Home Screen Layout"), size=20, bold=True),
                Text(_("Connect to a server to change the home screen "
                       "sections."), size=17, color=theme.SUBTLE_FG,
                     wrap=True),
            ], pad=self.CONTENT_PAD, gap=12, align="stretch"),
                id="settings-home", flex=1)
        if route.get("_home_error"):
            # Sticky, and deliberately so: this renders on every frame, so
            # clearing the flag here would re-fetch forever against a server
            # that is simply down.
            return VScroll(Column([
                Text(_("Home Screen Layout"), size=20, bold=True),
                Text(_("The layout could not be loaded from your server."),
                     size=17, color=theme.SUBTLE_FG, wrap=True),
                Button(_("Try Again"), id="home-retry",
                       on_click=lambda: self._retry_home_layout(route)),
            ], pad=self.CONTENT_PAD, gap=12, align="stretch"),
                id="settings-home", flex=1)
        layout = route.get("_home_layout")
        if layout is None:
            self._load_home_layout(route)
            return self._busy()
        options = home_sections.section_labels()
        values = [v for v, _l in options]
        labels = [lbl for _v, lbl in options]
        rows = [
            Text(_("Home Screen Layout"), size=20, bold=True),
            Text(_("These sections are stored on your server and shared "
                   "with Jellyfin Web."), size=14, color=theme.SUBTLE_FG,
                 wrap=True),
        ]
        for slot in range(home_sections.SLOT_COUNT):
            current = layout[slot]
            if current in values:
                selected = values.index(current)
                note = None
            else:
                # A section jellyfin-web can draw and we cannot (Live TV,
                # recordings, books). Showing it as "None" would be a lie that
                # this screen then makes true on save, so it is called out and
                # left alone unless the user picks something.
                selected = values.index(home_sections.NONE)
                note = _("Set to “%s” in Jellyfin Web, which this app does "
                         "not display.") % current
            row = Row([
                Text(_("Section %d") % (slot + 1), w=340, size=17,
                     color=theme.SUBTLE_FG),
                Dropdown("home-slot-%d" % slot, labels, selected=selected,
                         w=340, force=True,
                         on_select=lambda i, _v, s=slot, vs=values:
                             self._set_home_slot(route, s, vs[i])),
            ], gap=12, align="center")
            rows.append(row)
            if note:
                rows.append(Text(note, size=14, color=theme.SUBTLE_FG,
                                 wrap=True))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="settings-home", flex=1)
    def _load_home_layout(self, route):
        """Fetch the layout once per visit to the tab.

        Forces a refresh rather than trusting the browse cache: the user may
        have changed this in Jellyfin Web since the shim started, and showing
        them a stale layout would silently overwrite that change on save.
        """
        if route.get("_home_loading"):
            return
        route["_home_loading"] = True
        ep = self._epoch
        server = route.get("server") or self.server

        def work():
            layout, _excludes = self.source.get_home_prefs(server,
                                                           refresh=True)
            return list(layout)

        def done(layout):
            route["_home_layout"] = layout
            route["_home_loading"] = False
            self.invalidate()

        def failed(_exc):
            route["_home_loading"] = False
            # Not the defaults: offering an editable layout we never read
            # would let the user "keep" settings that were never loaded and
            # then save that guess over their real one.
            route["_home_layout"] = None
            route["_home_error"] = True
            self.set_status(_("Could not load the home screen layout."))
            self.invalidate()

        self.run_async(work, done, ep, on_error=failed)
    def _retry_home_layout(self, route):
        route.pop("_home_error", None)
        self.status = ""
        self.invalidate()
    def _set_home_slot(self, route, slot, value):
        layout = list(route.get("_home_layout") or
                      home_sections.DEFAULT_LAYOUT)
        if layout[slot] == value:
            return
        layout[slot] = value
        route["_home_layout"] = layout
        self.invalidate()
        server = route.get("server") or self.server
        ep = self._epoch

        def work():
            self.source.save_home_layout(server, layout)

        def done(_r):
            self.set_status(_("Home screen layout saved."))
            # The home route caches its rows; drop them so the next visit
            # rebuilds against the new layout instead of showing the old one
            # until something else invalidates it.
            self._invalidate_home()

        def failed(_exc):
            self.set_status(_("Could not save the home screen layout."))

        self.run_async(work, done, ep, on_error=failed)
    def _invalidate_home(self):
        """Drop cached home rows so the layout change is visible on return."""
        for entry in self.nav_stack:
            if entry.get("kind") == "home":
                entry.pop("_data", None)
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
