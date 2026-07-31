"""The Display tab — per-user preferences held on the server.

The second tab whose contents do not live in the shim's config. Like the
Home Screen tab, these are DisplayPreferences ``CustomPrefs`` values shared
with jellyfin-web (id ``usersettings``, client ``emby``), so they load and
save asynchronously and changing one here changes it in the web client too.

That sharing is the reason this is a separate tab rather than three more
checkboxes under General: everything under General is *this installation's*
setting, and a screen that mixed the two would give no way to tell which
was which. The note under the heading says so explicitly, because a setting
that changes on its own -- because you opened Jellyfin Web on your phone --
is otherwise indistinguishable from a bug.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Button,
    Checkbox,
    Column,
    Text,
    VScroll,
)
from .. import theme, user_prefs


class DisplayTabMixin:

    def _settings_display(self, route, size):
        if not hasattr(self.source, "save_user_prefs"):
            # Offline: there is no server to hold them. Offering the toggles
            # would fail only at save time, which is the worst moment.
            return VScroll(Column([
                Text(_("Display"), size=20, bold=True),
                Text(_("Connect to a server to change these settings."),
                     size=17, color=theme.SUBTLE_FG, wrap=True),
            ], pad=self.CONTENT_PAD, gap=12, align="stretch"),
                id="settings-display", flex=1)
        if route.get("_display_error"):
            # Sticky for the same reason the home tab's is: this renders on
            # every frame, so clearing the flag here would re-fetch forever
            # against a server that is simply down.
            return VScroll(Column([
                Text(_("Display"), size=20, bold=True),
                Text(_("These settings could not be loaded from your "
                       "server."), size=17, color=theme.SUBTLE_FG, wrap=True),
                Button(_("Try Again"), id="display-retry",
                       on_click=lambda: self._retry_display(route)),
            ], pad=self.CONTENT_PAD, gap=12, align="stretch"),
                id="settings-display", flex=1)
        prefs = route.get("_display_prefs")
        if prefs is None:
            self._load_display_prefs(route)
            return self._busy()
        rows = [
            Text(_("Display"), size=20, bold=True),
            Text(_("These settings are stored on your server and shared "
                   "with Jellyfin Web."), size=14, color=theme.SUBTLE_FG,
                 wrap=True),
            Checkbox(_("Use episode images in Next Up and Continue Watching"),
                     bool(prefs.get("episode_images")),
                     id="display-episode-images",
                     on_toggle=lambda: self._toggle_display(
                         route, "episode_images")),
            Text(_("Off by default: the show's artwork is used instead, "
                   "because an episode image can spoil the episode you "
                   "have not watched yet."),
                 size=14, color=theme.SUBTLE_FG, wrap=True),
        ]
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=12,
                              align="stretch"),
                       id="settings-display", flex=1)

    def _load_display_prefs(self, route):
        """Fetch once per visit to the tab.

        Forces a refresh rather than trusting the cache, exactly as the home
        tab does: the user may have changed this in Jellyfin Web since the
        shim started, and a stale value here would be silently written back
        over their real one on the next toggle.
        """
        if route.get("_display_loading"):
            return
        route["_display_loading"] = True
        ep = self._epoch
        server = route.get("server") or self.server

        def work():
            return dict(self.source.get_user_prefs(server, refresh=True))

        def done(prefs):
            route["_display_prefs"] = prefs
            self.invalidate()

        def failed(_exc):
            # Not the defaults: offering editable toggles we never read lets
            # the user "keep" a value that was never loaded and then save
            # that guess over their real one.
            route["_display_prefs"] = None
            route["_display_error"] = True
            self.set_status(_("Could not load your display settings."))
            self.invalidate()

        def clear_guard():
            route["_display_loading"] = False

        # `always`, not a line in each of done/failed: on_done is
        # epoch-gated, so a fetch superseded by a background reconnect
        # (set_source bumps the epoch) or by leaving and coming back runs
        # NEITHER of them — and a guard left set means this tab never
        # fetches again. The render path then short-circuits on it every
        # frame: a permanent spinner with no error and no retry, escapable
        # only by switching tabs, which the user has no reason to guess.
        # Same shape and same reason as pagination.py's clear_guard.
        self.run_async(work, done, ep, on_error=failed, always=clear_guard)

    def _retry_display(self, route):
        route.pop("_display_error", None)
        self.status = ""
        self.invalidate()

    def _toggle_display(self, route, key):
        """Flip one preference, optimistically, and save.

        Optimistic with a rollback rather than "save then repaint": these are
        checkboxes, and a tick that appears a network round trip after the
        click reads as a dead control. Same bargain the rest of the browser's
        edits make -- see ItemActions.edit.
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
            self.set_status(_("Display settings saved."))
            # The home rows are cached on the route and were built with the
            # old value; drop them so returning shows the change rather than
            # the previous artwork until something else invalidates it.
            self._invalidate_home()

        def failed(_exc):
            rolled = dict(route.get("_display_prefs") or {})
            rolled[key] = previous
            route["_display_prefs"] = rolled
            self.set_status(_("Could not save your display settings."))
            self.invalidate()

        self.run_async(work, done, ep, on_error=failed)
