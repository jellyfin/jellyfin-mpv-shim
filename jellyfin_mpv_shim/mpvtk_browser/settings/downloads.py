"""The Downloads tab and its progress poller.

Rows are grouped by their parent (a season, an album) so a series does not
fill the panel with episodes. ``_poll_downloads`` runs on a daemon thread,
writes state and calls ``invalidate()``; it exits on ``_shutdown_evt`` or as
soon as the user leaves the tab.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Box,
    Button,
    Column,
    Grid,
    Icon,
    Row,
    Spacer,
    Text,
    VScroll,
)
from .. import theme


class DownloadsTabMixin:

    def _settings_downloads(self, route, size):
        groups = route.get("_downloads")
        if groups is None:
            self._load_downloads(route)
            return self._busy()
        total = sum(g.get("size", 0) or 0 for g in groups)
        count = sum(g.get("count", 0) or 0 for g in groups)
        head = Row([
            Text(_("Downloads"), size="heading", bold=True),
            Text(_("%(count)d items · %(size)s") % {
                "count": count, "size": self._human_size(total)},
                size="small", color=theme.SUBTLE_FG),
            Spacer(),
            Button(_("Refresh"), id="dl-refresh", icon="refresh",
                   on_click=lambda: self._load_downloads(route, force=True)),
        ], gap=12, align="center")
        rows = [head]
        if not groups:
            rows.append(Text(_("Nothing downloaded yet."), size="normal",
                             color=theme.SUBTLE_FG))
        for gi, group in enumerate(groups):
            rows.append(self._dl_group(route, group, gi))
        self._poll_downloads(route)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=10,
                              align="stretch"),
                       id="settings-downloads", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "settings-downloads", off, mx))
    def _dl_row(self, node_id, title, meta, depth, on_delete, bold=False,
                icon=None, count=None, route=None, toggle=None,
                expanded=True, on_delete_watched=None):
        """One Grid row spec of the downloads tree. Indentation carries
        the level (inside the title cell, so the meta/Remove tracks stay
        shared across every depth); every level gets its own delete so a
        whole show can go at once. ``toggle`` (a collapse-state key)
        adds a disclosure chevron before the title."""
        title_cell = [Spacer(w=depth * self.INDENT, h=1)]
        if toggle is not None:
            title_cell.append(Box(
                [Icon("keyboard_arrow_down" if expanded
                      else "chevron_right", 16, color=theme.SUBTLE_FG)],
                id=node_id + "-tgl", pad=3, radius=4, direction="row",
                align="center", hover={"fill": theme.BUTTON_BG},
                on_click=lambda: self._dl_toggle(route, toggle)))
        else:
            # rows without a disclosure still reserve its gutter, so
            # titles stay monotonically indented down the tree
            title_cell.append(Spacer(w=22, h=1))
        if icon:
            title_cell.append(Icon(icon, 16, color=theme.SUBTLE_FG))
        title_cell.append(Text(title, size=17 if bold else 16, bold=bold))
        if count:
            # Collapsed groups (playlists) say how much they stand for.
            title_cell.append(Text(_("%d items") % count, size="caption",
                                   color=theme.SUBTLE_FG))
        title_cell.append(Spacer())
        return {
            "id": node_id,
            "bg": theme.PANEL_BG if depth == 0 else None,
            "radius": 6,
            "cells": [
                Row(title_cell, gap=10, align="center", flex=1),
                Text(meta, size="caption", color=theme.SUBTLE_FG,
                     align="right"),
                Row(([Button(_("Remove Watched"), id=node_id + "-rmw",
                             icon="check", size="small",
                             on_click=on_delete_watched)]
                     if on_delete_watched else []) +
                    [Button(_("Remove"), id=node_id + "-rm", icon="delete",
                            size="small", on_click=on_delete)],
                    gap=6, align="center"),
            ],
        }
    def _dl_toggle(self, route, key):
        route.setdefault(
            "_dl_collapsed", set()).symmetric_difference_update({key})
        self.invalidate()
    @staticmethod
    def _dl_key(entry, fallback):
        # stable across refreshes (ids); position only as a last resort
        return str(entry.get("id") or entry.get("title") or fallback)
    def _dl_group(self, route, group, gi):
        collapsed = route.get("_dl_collapsed") or set()
        kind = group.get("kind")
        children = group.get("children") or []
        gkey = self._dl_key(group, gi)
        g_open = gkey not in collapsed
        rows = [self._dl_row(
            "dl-g%d" % gi, group.get("title", "?"),
            self._human_size(group.get("size", 0)), 0,
            self._dl_delete_cb(
                route, group,
                series_id=group.get("id") if kind == "series" else None,
                playlist_id=group.get("id") if kind == "playlist" else None,
                # Groups without a server-side id (the flat "Movies &
                # Videos" bucket) delete their own rows explicitly. Passing
                # no scope at all used to reach syncManager.delete() with
                # every id None, which deleted the ENTIRE catalog behind a
                # prompt naming only this group.
                item_ids=(None if kind in ("series", "playlist")
                          else self._dl_group_item_ids(group))),
            bold=True, count=group.get("count"),
            icon={"movies": "movie", "playlist": "queue_music",
                  "audiobooks": "audiotrack", "books": "menu_book"}.get(kind),
            route=route, toggle=gkey if children else None,
            expanded=g_open,
            # Reclaim space on a finished show without losing what's
            # unwatched — the Tk browser's gesture.
            on_delete_watched=(
                self._dl_delete_cb(
                    route, group, watched_only=True,
                    series_id=group.get("id") if kind == "series" else None,
                    playlist_id=(group.get("id") if kind == "playlist"
                                 else None),
                    item_ids=(None if kind in ("series", "playlist")
                              else self._dl_group_item_ids(group)))
                if kind in ("series", "playlist")
                and group.get("watched_count") else None))]
        for ci, child in enumerate(children if g_open else []):
            # A middle level: a season, or one audiobook's chapter files.
            # Told apart by having children of its own rather than by kind,
            # so a new nested group renders without touching this loop --
            # only its delete scope has to be named.
            if child.get("kind") in ("season", "audiobook"):
                skey = self._dl_key(child, "%d.%d" % (gi, ci))
                s_open = skey not in collapsed
                eps = child.get("children") or []
                rows.append(self._dl_row(
                    "dl-g%d-s%d" % (gi, ci), child.get("title", "?"),
                    self._human_size(child.get("size", 0)), 1,
                    self._dl_delete_cb(
                        route, child,
                        # A season is a server-side object and deletes by
                        # id. An audiobook is not -- nothing joins its
                        # chapters but the folder they came from -- so it
                        # names its rows, like the flat groups do.
                        season_id=(child.get("id")
                                   if child.get("kind") == "season" else None),
                        item_ids=(None if child.get("kind") == "season"
                                  else self._dl_group_item_ids(child))),
                    route=route, toggle=skey if eps else None,
                    expanded=s_open, count=child.get("count")))
                for ei, ep in enumerate(eps if s_open else []):
                    rows.append(self._dl_item_row(
                        route, ep, "dl-g%d-s%d-e%d" % (gi, ci, ei), 2))
            else:
                rows.append(self._dl_item_row(
                    route, child, "dl-g%d-i%d" % (gi, ci), 1))
        return Grid(rows,
                    cols=[{"flex": 1}, {"w": 200, "align": "right"},
                          {"align": "right"}],
                    gap=10, row_gap=2, row_pad=6)
    def _dl_item_row(self, route, item, node_id, depth):
        num = item.get("index")
        # Not numbered when the title is already qualified: those rows read
        # "Show - S01E04 - Chapter Four", and a leading "4. " on top of that
        # is both redundant and, in a group mixing shows, meaningless.
        title = ("%s. %s" % (num, item.get("title", ""))
                 if num is not None and not item.get("qualified")
                 else item.get("title", ""))
        from ..downloads import status_text
        # The watched marker is why "Remove Watched" is offered at all; with
        # no way to see which rows it means, the button read as a destructive
        # guess.
        meta = "   ".join(x for x in (
            _("watched") if item.get("watched") else "",
            status_text(item),
            self._human_size(item.get("size", 0))) if x)
        return self._dl_row(node_id, title, meta, depth,
                            self._dl_delete_cb(route, item,
                                               item_id=item.get("id")))
    @classmethod
    def _dl_group_item_ids(cls, group):
        """Every download id under a group, however deeply nested.

        Recursive rather than one hard-coded level: the Audiobooks section
        is group -> book -> chapter, which the season-shaped version walked
        one level of and then took the *book* rows' (absent) ids. That is
        how a group-level Remove silently deletes nothing.
        """
        out = []
        for child in group.get("children") or ():
            if child.get("children"):
                out += cls._dl_group_item_ids(child)
            elif child.get("id"):
                out.append(child["id"])
        return [i for i in out if i]
    def _dl_delete_cb(self, route, entry, item_id=None, series_id=None,
                      season_id=None, playlist_id=None, item_ids=None,
                      watched_only=False):
        def go():
            self._confirm(
                (_("Delete the watched downloads in %s?") if watched_only
                 else _("Delete the downloaded copy of %s?"))
                % entry.get("title", ""),
                lambda: self._delete_download(route, item_id=item_id,
                                              series_id=series_id,
                                              season_id=season_id,
                                              playlist_id=playlist_id,
                                              item_ids=item_ids,
                                              watched_only=watched_only),
                title=_("Delete Download"), yes=_("Delete"))
        return go
    def _poll_downloads(self, route):
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(self.DL_POLL_SECS):
                if (self.route is not route
                        or route.get("_tab") != "downloads"
                        or not self._browsing):
                    break
                try:
                    pending, _total = self.controller.download_activity()
                except Exception:
                    break
                if not pending:
                    # One last read before stopping. The transition that took
                    # pending to zero is exactly the one the list has not
                    # drawn yet, so breaking straight out left the item that
                    # had just finished reading "downloading" until someone
                    # pressed Refresh.
                    self._load_downloads(route, force=True)
                    break
                self._load_downloads(route, force=True)

        self._start_daemon("_dl_thread", "mpvtk-dl-poll", tick,
                           restartable=True)
    def _load_downloads(self, route, force=False):
        if self.controller is None:
            route["_downloads"] = []
            return
        if route.get("_dl_loading") and not force:
            return
        route["_dl_loading"] = True
        ep = self._epoch

        def work():
            return self.controller.list_downloads()

        def done(rows):
            route["_downloads"] = rows or []
            # badges elsewhere in the UI are keyed off the same catalog
            self._refresh_downloaded()

        # `always`, not part of done: a load dropped for being stale runs
        # neither callback, and a stuck _dl_loading makes every later render
        # of this panel return early — the list freezes until something calls
        # with force=True. There was no on_error either, so a failed load did
        # the same.
        self.run_async(work, done, ep,
                       always=lambda: route.__setitem__("_dl_loading", False))
    def _delete_download(self, route, item_id=None, series_id=None,
                         season_id=None, playlist_id=None, item_ids=None,
                         watched_only=False):
        """Delete, then re-read the catalog — in that order, on one worker.

        Submitting the delete and the reload as separate tasks raced: the
        reload could read the catalog before the delete had touched it, and
        the row came straight back."""
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            # No try/except here: the controller raises now, and swallowing
            # it a second time is what made a failed delete silent. The list
            # is re-read on the same worker so the reload cannot run before
            # the delete has touched the catalog.
            if item_ids is not None and not watched_only:
                for one in item_ids:
                    self.controller.delete_download(item_id=one)
            else:
                self.controller.delete_download(
                    item_id=item_id, series_id=series_id,
                    season_id=season_id, playlist_id=playlist_id,
                    watched_only=watched_only)
            return self.controller.list_downloads()

        def done(rows):
            route["_downloads"] = rows or []
            # badges elsewhere in the UI are keyed off the same catalog
            self._refresh_downloaded()

        def failed(_exc):
            self.set_status(_("The download could not be removed."))
        self.run_async(work, done, ep, on_error=failed,
                       always=lambda: route.__setitem__("_dl_loading", False))
        self._refresh_downloaded()
    # How often the downloads view re-reads the catalog while work is
    # outstanding. Downloads land asynchronously, so a static list is stale
    # the moment it renders.
    DL_POLL_SECS = 3.0
