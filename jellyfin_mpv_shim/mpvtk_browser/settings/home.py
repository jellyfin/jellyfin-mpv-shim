"""The Home Screen tab: which rows the home screen has, and how two of
them are illustrated.

The odd one out: unlike every other setting, these live on the *server*
(DisplayPreferences ``CustomPrefs``, id ``usersettings``, client ``emby``),
so they load and save asynchronously rather than through the config module,
and changing one here changes it in Jellyfin Web too. That is why this tab
alone has a loading state and a retry -- and why it says so on screen: a
setting that changes on its own, because you opened Jellyfin Web on your
phone, is otherwise indistinguishable from a bug.

**The episode-images preference used to be a tab of its own ("Display"),
holding that one checkbox.** It is here now because what it governs is two
home screen rows -- Next Up and Continue Watching -- so the tab that decides
whether you have those rows is the tab that should decide what they look
like. Both halves come out of the same DisplayPreferences document, which is
why they share one fetch, one error state and one retry.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Button,
    Checkbox,
    Column,
    Dropdown,
    Row,
    Text,
    VScroll,
)
from .. import home_sections, theme


class HomeTabMixin:

    def _settings_home(self, route, size):
        """Per-slot dropdowns, like jellyfin-web's home screen settings, plus
        the one preference that changes how two of those rows are
        illustrated.

        All of it lives on the server, not in the shim's config, so it is
        loaded and saved asynchronously and shared with every other Jellyfin
        client this user runs.
        """
        if not hasattr(self.source, "save_home_layout"):
            # The offline source: there is no server to hold any of this, and
            # the downloaded rows are not the configurable sections anyway.
            # Offering the controls here would fail only at save time, which
            # is the worst moment.
            return VScroll(Column([
                Text(_("Home Screen"), size="large", bold=True),
                Text(_("Connect to a server to change the home screen "
                       "sections."), size="normal", color=theme.SUBTLE_FG,
                     wrap=True),
            ], pad=self.CONTENT_PAD, gap=12, align="stretch"),
                id="settings-home", flex=1)
        if route.get("_home_error"):
            # Sticky, and deliberately so: this renders on every frame, so
            # clearing the flag here would re-fetch forever against a server
            # that is simply down.
            return VScroll(Column([
                Text(_("Home Screen"), size="large", bold=True),
                Text(_("These settings could not be loaded from your "
                       "server."), size="normal", color=theme.SUBTLE_FG, wrap=True),
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
        note_w = self._note_w(size)
        rows = [
            Text(_("Home Screen"), size="large", bold=True),
            # Said once, at the top, for the whole page: everything below it
            # is the same kind of setting, and repeating the caveat per
            # group would read as three different caveats.
            Text(_("These settings are stored on your server and shared "
                   "with Jellyfin Web."), size="caption", w=note_w,
                 color=theme.SUBTLE_FG, wrap=True),
            Text(_("Sections"), size="normal", bold=True),
            Text(_("The rows the home screen shows, in order. Sections this "
                   "app cannot draw are left as your web client set them."),
                 size="caption", w=note_w, color=theme.SUBTLE_FG, wrap=True),
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
                Text(_("Section %d") % (slot + 1), w=340, size="normal",
                     color=theme.SUBTLE_FG),
                Dropdown("home-slot-%d" % slot, labels, selected=selected,
                         w=340, force=True,
                         on_select=lambda i, _v, s=slot, vs=values:
                             self._set_home_slot(route, s, vs[i])),
            ], gap=12, align="center")
            rows.append(row)
            if note:
                rows.append(Text(note, size="caption", w=note_w,
                                 color=theme.SUBTLE_FG, wrap=True))
        rows += self._home_artwork_rows(route, note_w)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="settings-home", flex=1)

    def _home_artwork_rows(self, route, note_w=None):
        """How Next Up and Continue Watching are illustrated.

        Was the whole of a "Display" tab. Named for the two rows it affects
        rather than for the preference's key, because that is how someone
        arrives at it: they saw a thumbnail they did not want, on the home
        screen, and came here to turn it off.
        """
        prefs = route.get("_display_prefs")
        if prefs is None:
            # Loaded in the same job as the layout, so if the layout is up
            # this is too. Guarded anyway rather than indexed blindly: the
            # cost of being wrong is the whole tab raising mid-render.
            return []
        return [
            Text(_("Artwork"), size="normal", bold=True),
            Checkbox(_("Use episode images in 'Next Up' and "
                       "'Continue Watching' sections"),
                     bool(prefs.get("episode_images")),
                     id="display-episode-images",
                     on_toggle=lambda: self._toggle_display(
                         route, "episode_images")),
            Text(_("Off by default: the show's artwork is used instead, "
                   "because an episode image can spoil the episode you "
                   "have not watched yet."),
                 size="caption", w=note_w, color=theme.SUBTLE_FG, wrap=True),
        ]
    def _load_home_layout(self, route):
        """Fetch the layout AND the artwork preference, once per visit.

        One job for both: they are two reads of the same DisplayPreferences
        document, they are drawn on one page, and a page with two independent
        spinners and two independent retries would be a worse screen than the
        two tabs it replaced.

        Forces a refresh rather than trusting the browse cache: the user may
        have changed either in Jellyfin Web since the shim started, and
        showing them a stale value would silently overwrite that change on
        the next save.
        """
        if route.get("_home_loading"):
            return
        route["_home_loading"] = True
        ep = self._epoch
        server = route.get("server") or self.server

        def work():
            layout, _excludes = self.source.get_home_prefs(server,
                                                           refresh=True)
            return list(layout), dict(self.source.get_user_prefs(
                server, refresh=True))

        def done(result):
            route["_home_layout"], route["_display_prefs"] = result
            self.invalidate()

        def failed(_exc):
            # Not the defaults: offering editable controls we never read
            # would let the user "keep" settings that were never loaded and
            # then save that guess over their real one.
            route["_home_layout"] = None
            route["_display_prefs"] = None
            route["_home_error"] = True
            self.set_status(_("Could not load the home screen settings."))
            self.invalidate()

        def clear_guard():
            route["_home_loading"] = False

        # `always`, not a line in each of done/failed: on_done is
        # epoch-gated, so a fetch superseded by a background reconnect
        # (set_source bumps the epoch) or by leaving and coming back runs
        # NEITHER of them — and a guard left set means this tab never
        # fetches again. The render path then short-circuits on it every
        # frame: a permanent spinner with no error and no retry, escapable
        # only by switching tabs, which the user has no reason to guess.
        # Same shape and same reason as pagination.py's clear_guard.
        self.run_async(work, done, ep, on_error=failed, always=clear_guard)
    def _retry_home_layout(self, route):
        route.pop("_home_error", None)
        self.status = ""
        self.invalidate()

    def _toggle_display(self, route, key):
        """Flip the artwork preference, optimistically, and save.

        Optimistic with a rollback rather than "save then repaint": this is a
        checkbox, and a tick that appears a network round trip after the
        click reads as a dead control. Same bargain the rest of the browser's
        edits make -- see ItemActions.edit. Note a Checkbox is drawn from its
        value and nothing on the renderer side flips it, so the invalidate()
        is the whole update.
        """
        prefs = dict(route.get("_display_prefs") or {})
        previous = bool(prefs.get(key))
        prefs[key] = not previous
        route["_display_prefs"] = prefs
        self.invalidate()
        server = route.get("server") or self.server
        ep = self._epoch

        def work():
            self.source.save_user_prefs(server, prefs)

        def done(_r):
            self.set_status(_("Home screen settings saved."))
            # The home rows are cached on the route and were built with the
            # old value; drop them so returning shows the change rather than
            # the previous artwork until something else invalidates it.
            self._invalidate_home()

        def failed(_exc):
            rolled = dict(route.get("_display_prefs") or {})
            rolled[key] = previous
            route["_display_prefs"] = rolled
            self.set_status(_("Could not save the home screen settings."))
            self.invalidate()

        self.run_async(work, done, ep, on_error=failed)
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
