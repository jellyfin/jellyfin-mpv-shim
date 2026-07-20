"""The play queue and the playlist editor.

Both are multi-select list editors over an ordered set of items, and share
``_block_move`` for the reorder arithmetic.

State on ``self``: none — the selection and items live in the *route dict*
(``route["sel"]``, ``route["items"]``, ``route["anchor"]``). Edits are
optimistic: they mutate the route dict, call the server, and restore in
``on_error``. Note ``run_async`` drops ``on_error`` when the epoch has
moved, so navigating away mid-flight can leave a rejected edit in the route
dict (see ``MIGRATION.md``).
"""

from ..i18n import _
from ..mpvtk.widgets import (
    Button,
    Checkbox,
    Column,
    Row,
    Spacer,
    Table,
    Text,
    TextBox,
    VScroll,
)
from . import theme


class QueueEditMixin:

    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "playlist_edit": ("_load_playlist_edit", "_render_playlist_edit"),
        "queue": ("_load_queue", "_render_queue"),
    }

    # --------------------------------------------------------------- queue

    def _render_queue(self, route, size):
        """The play queue, deliberately the same table + toolbar as the
        playlist editor: the two do the same job on the same kind of list."""
        data = route.get("_data")
        if data is None:
            return self._busy()
        entries = data.get("entries") or []
        current = data.get("current_id")
        sel = self._pe_sel(route)
        n = len(entries)
        toolbar = Row([
            Text(_("Play Queue"), size=26, bold=True), Spacer(),
            Button(_("Top"), id="q-top", icon="vertical_align_top",
                   on_click=lambda: self._queue_move(route, "top")),
            Button(_("Up"), id="q-up", icon="keyboard_arrow_up",
                   on_click=lambda: self._queue_move(route, "up")),
            Button(_("Down"), id="q-down", icon="keyboard_arrow_down",
                   on_click=lambda: self._queue_move(route, "down")),
            Button(_("Bottom"), id="q-bottom", icon="vertical_align_bottom",
                   on_click=lambda: self._queue_move(route, "bottom")),
            Text(_("%d selected") % len(sel) if sel else "", size=15,
                 color=theme.SUBTLE_FG),
            Button(_("Select All"), id="q-all",
                   on_click=lambda: self._pe_set_sel(route, set(range(n)))),
            Button(_("Clear"), id="q-none",
                   on_click=lambda: self._pe_set_sel(route, set())),
            Button(_("To Playlist"), id="q-toplaylist", icon="queue_music",
                   on_click=lambda: self._queue_to_playlist(route)),
            Button(_("Remove"), id="q-remove", icon="delete",
                   on_click=lambda: self._queue_remove_selected(route)),
        ], gap=8, align="center")
        rows = [toolbar, Spacer(h=2)]
        if not entries:
            rows.append(Text(_("The queue is empty."), size=18,
                             color=theme.SUBTLE_FG))
        else:
            rows.append(self._track_list(
                [e["item"] for e in entries], "q",
                lambda i: self._queue_skip(entries[i].get("pid")),
                playing_id=current, selected=sel, scroll_id="queue",
                head_h=60,
                on_select=lambda i, mods: self._select_click(route, i, mods)))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                          align="stretch"), id="queue",
                       flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "queue", off, mx))

    def _queue_to_playlist(self, route):
        """Save the playing queue as / into a playlist (Tk's playbar
        button). The add-to dialog does the rest."""
        entries = (route.get("_data") or {}).get("entries") or []
        ids = [e["item"].get("Id") for e in entries if e.get("item")]
        ids = [i for i in ids if i]
        if not ids:
            return
        server = route.get("server") or self.server
        self._open_add_to({"Id": ids[0], "Type": "Audio",
                           "Name": _("Play Queue"), "_ids": ids},
                          server=server)

    def _queue_remove_selected(self, route):
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        sel = sorted(self._pe_sel(route))
        pids = [entries[i].get("pid") for i in sel
                if i < len(entries) and entries[i].get("pid")]
        if not pids:
            return
        route["_sel"] = set()

        def reload():
            # Guarded, and against the CAPTURED route rather than
            # self.route. on_error is deliberately not epoch-gated (see
            # run_async), so this can land after the user has navigated
            # somewhere else entirely — and it used to wipe _data, bump the
            # epoch and re-issue the load for whatever page they were now
            # on, flashing an unrelated view back to a spinner and killing
            # its in-flight load.
            if route is not self.route:
                return
            route.pop("_data", None)
            self._bump_epoch()
            self._load_route(route)
            self.invalidate()

        if self.controller is None:
            return reload()
        # _edit_call, not _safe: every other edit in this UI reports, and a
        # removal that silently did nothing left the rows on screen with no
        # explanation. Re-read either way — on failure to put back what is
        # really in the queue.
        self._edit_call(lambda c: c.queue_remove(pids), on_ok=reload,
                        on_error=reload,
                        error=_("Those items could not be removed."))

    @staticmethod
    def _block_move(items, sel, where):
        """Move the selected indices as one block. Returns (items, new_sel)
        or None when nothing moves. Shared by the queue and the playlist
        editor so the two behave identically."""
        sel = sorted(sel)
        if not sel or not items:
            return None
        n = len(items)
        if where in ("up", "down"):
            # One step each, against a floor/ceiling, so a non-contiguous
            # selection keeps its gaps — this is what Tk did. Treating it as
            # a block silently reordered rows the user had not selected, and
            # a selection whose leading row was already at the edge no-opped
            # for the whole selection instead of moving the rest.
            out = list(items)
            new_sel = set()
            if where == "up":
                edge = -1
                for i in sel:
                    if i - 1 > edge:
                        out.insert(i - 1, out.pop(i))
                        edge = i - 1
                    else:
                        edge = i
                    new_sel.add(edge)
            else:
                edge = n
                for i in reversed(sel):
                    if i + 1 < edge:
                        out.insert(i + 1, out.pop(i))
                        edge = i + 1
                    else:
                        edge = i
                    new_sel.add(edge)
            if new_sel == set(sel):
                return None      # already packed against that edge
            return out, new_sel
        # Top/Bottom stay block moves: gathering a scattered selection is
        # the point of them.
        target = {"top": 0, "bottom": n - len(sel)}[where]
        if sel == list(range(target, target + len(sel))):
            return None
        block = [items[i] for i in sel]
        rest = [it for i, it in enumerate(items) if i not in set(sel)]
        return (rest[:target] + block + rest[target:],
                set(range(target, target + len(block))))

    def _queue_move(self, route, where):
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        was, was_sel = list(entries), set(self._pe_sel(route))
        moved = self._block_move(entries, self._pe_sel(route), where)
        if moved is None:
            return
        data["entries"], route["_sel"] = moved
        order = [e["pid"] for e in data["entries"] if e.get("pid")]
        self.invalidate()

        def restore():
            data["entries"], route["_sel"] = was, was_sel
        self._edit_call(lambda c: c.queue_reorder(order), on_error=restore,
                        error=_("The queue could not be reordered."))

    def _queue_skip(self, pid):
        if pid and self.controller is not None:
            self._safe(lambda c: c.skip_to(pid))

    # --------------------------------------------------------- playlist edit

    @staticmethod
    def _pe_title(item):
        """Series-aware entry title, like the Tk editor's: an episode reads
        "Show — S02E05 · Title" so a 300-row playlist is navigable."""
        name = item.get("Name", "")
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            se = ("S%sE%s" % (s, e)) if s is not None and e is not None else ""
            parts = [p for p in (item.get("SeriesName"), se) if p]
            if parts:
                return "%s · %s" % (" — ".join(parts), name)
        artists = item.get("Artists") or []
        if artists:
            return "%s — %s" % (", ".join(artists), name)
        return name

    def _pe_sel(self, route):
        """Selected row indices as a set (multi-select)."""
        return set(route.get("_sel") or ())

    def _render_playlist_edit(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        sel = self._pe_sel(route)
        n = len(items)
        toolbar = Row([
            Button(_("Top"), id="pe-top", icon="vertical_align_top",
                   on_click=lambda: self._pe_move(route, "top")),
            Button(_("Up"), id="pe-up", icon="keyboard_arrow_up",
                   on_click=lambda: self._pe_move(route, "up")),
            Button(_("Down"), id="pe-down", icon="keyboard_arrow_down",
                   on_click=lambda: self._pe_move(route, "down")),
            Button(_("Bottom"), id="pe-bottom", icon="vertical_align_bottom",
                   on_click=lambda: self._pe_move(route, "bottom")),
            Spacer(),
            Text(_("%d selected") % len(sel) if sel else "", size=15,
                 color=theme.SUBTLE_FG),
            Button(_("Select All"), id="pe-all",
                   on_click=lambda: self._pe_set_sel(route, set(range(n)))),
            Button(_("Clear"), id="pe-none",
                   on_click=lambda: self._pe_set_sel(route, set())),
            Button(_("Remove"), id="pe-remove", icon="delete",
                   on_click=lambda: self._pe_remove(route)),
        ], gap=8, align="center")
        rename_row = Row([
            TextBox("pe-name", text=route.get("title", ""), w=280,
                    on_change=lambda v: route.__setitem__("_newname", v),
                    on_submit=lambda v: self._pe_rename(route)),
            Button(_("Rename"), id="pe-rename", icon="edit",
                   on_click=lambda: self._pe_rename(route)),
            Checkbox(_("Public"), bool(route.get("_public")), id="pe-public",
                     on_toggle=lambda: self._pe_toggle_public(route)),
            Spacer(),
            Button(_("Delete Playlist"), id="pe-delete", icon="delete",
                   on_click=lambda: self._confirm(
                       _("Delete the playlist %s?") % route.get("title", ""),
                       lambda: self._pe_delete(route),
                       title=_("Delete Playlist"), yes=_("Delete"))),
        ], gap=10, align="center")
        table = Table(
            [{"label": "#", "w": 46, "align": "right"},
             {"label": _("Title"), "flex": 3},
             {"label": _("Type"), "w": 120},
             {"label": _("Time"), "w": 80, "align": "right"}],
            [{"id": "pe-row-%d" % i,
              "selected": i in sel,
              "cells": [str(i + 1), self._pe_title(it),
                        it.get("Type", ""), self._duration(it)],
              # A one-parameter handler opts into the click modifiers, which
              # is what makes shift-range selection possible.
              "on_click": (lambda mods, i=i: self._select_click(
                  route, i, mods))}
             for i, it in enumerate(items)],
            size=17, row_h=34, hover_bg=theme.BUTTON_BG)
        rows = [Text("%s — %s" % (route.get("title", ""), _("Edit")),
                     size=26, bold=True), Spacer(h=4), rename_row, toolbar,
                Spacer(h=2), table]
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="playlist-edit", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "playlist-edit", off, mx))

    def _pe_set_sel(self, route, sel, anchor=None):
        route["_sel"] = set(sel)
        if anchor is not None:
            route["_anchor"] = anchor
        self.invalidate()

    def _select_click(self, route, i, mods):
        """Standard list selection semantics against ``route["_sel"]``.

        - plain click: select just this row, and make it the anchor
        - shift-click: select the whole range from the anchor to here, so two
          clicks pick any run of rows
        - ctrl-click: toggle this row, keeping the rest

        ``mods`` comes from the renderer's click payload (mpvtk carries
        shift/ctrl for handlers that declare a parameter)."""
        mods = mods or {}
        sel = self._pe_sel(route)
        anchor = route.get("_anchor")
        if mods.get("shift") and anchor is not None:
            lo, hi = (anchor, i) if anchor <= i else (i, anchor)
            self._pe_set_sel(route, set(range(lo, hi + 1)))
        elif mods.get("ctrl"):
            sel.symmetric_difference_update({i})
            self._pe_set_sel(route, sel, anchor=i)
        else:
            self._pe_set_sel(route, {i}, anchor=i)

    def _pe_move(self, route, where):
        """Move the whole selection as a block, preserving its internal
        order — moving 20 rows should not require 20 clicks."""
        items = route.get("_items") or []
        sel = sorted(self._pe_sel(route))
        moved = self._block_move(items, sel, where)
        if moved is None:
            return
        route["_items"], route["_sel"] = moved
        target = min(route["_sel"])
        server = route.get("server") or self.server
        pid = route["item_id"]
        picked = [items[i] for i in sel]
        batch = [(e.get("PlaylistItemId"), target + o)
                 for o, e in enumerate(picked) if e.get("PlaylistItemId")]
        self.invalidate()
        if not batch:
            return
        ep = self._epoch

        def work():
            # One ordered batch, not N concurrent tasks: moves are
            # absolute-index operations that only compose in order.
            self.controller.playlist_move_many(server, pid, batch)

        def done(_ok):
            pass   # the optimistic order is what we just asked for

        def failed(_exc):
            # Don't leave the optimistic order lying: re-read the truth.
            self.set_status(_("The playlist could not be reordered."))
            route.pop("_items", None)
            self._load_route(route)
        self.run_async(work, done, ep, on_error=failed)

    def _pe_remove(self, route):
        items = route.get("_items") or []
        sel = sorted(self._pe_sel(route))
        if not sel:
            return
        entries = [items[i] for i in sel if i < len(items)]
        route["_items"] = [it for i, it in enumerate(items)
                           if i not in set(sel)]
        route["_sel"] = set()
        ids = [e.get("PlaylistItemId") for e in entries
               if e.get("PlaylistItemId")]
        self.invalidate()
        if not ids:
            return
        server = route.get("server") or self.server
        self._edit_call(
            lambda c: c.playlist_remove(server, route["item_id"], ids),
            # Put the rows back: the list showed them gone either way.
            on_error=lambda: route.__setitem__("_items", items))

    def _pe_delete(self, route):
        """Delete, then navigate — not both at once. Firing the delete onto
        the pool and pruning immediately meant a failed delete still walked
        the user out of a playlist that still exists."""
        pid = route["item_id"]
        server = route.get("server") or self.server
        ep = self._epoch

        def work():
            self.controller.playlist_delete(server, pid)
            return True

        def done(_ok):
            self.after_playlist_deleted(pid)

        def failed(_exc):
            self.set_status(_("The playlist could not be deleted."))
        self.run_async(work, done, ep, on_error=failed)

    def _pe_rename(self, route):
        name = (route.get("_newname") or route.get("title") or "").strip()
        if not name:
            return
        was = route.get("title")
        route["title"] = name
        server = route.get("server") or self.server
        self.invalidate()
        self._edit_call(
            lambda c: c.playlist_update(server, route["item_id"], name=name),
            on_error=lambda: route.__setitem__("title", was))

    def _pe_toggle_public(self, route):
        # Refuse until the loader has read the server's OpenAccess: flipping a
        # value we never read could make a public playlist private (or worse,
        # the reverse) on the very first click.
        if not route.get("_public_known"):
            self._message(_("Still reading this playlist's visibility from "
                            "the server. Try again in a moment."))
            return
        was = route.get("_public")
        route["_public"] = not was
        server = route.get("server") or self.server
        self.invalidate()
        # Visibility especially must not be left showing a value the server
        # rejected — that is the difference between private and public.
        self._edit_call(
            lambda c: c.playlist_update(server, route["item_id"],
                                        is_public=route["_public"]),
            on_error=lambda: route.__setitem__("_public", was))

    # ---------------------------------------- route loaders

    def _load_playlist_edit(self, route, ep):
        srv = route.get("server") or self.server
        iid = route["item_id"]

        def work():
            meta = {}
            try:
                meta = self.source.get_playlist(srv, iid) or {}
            except Exception:
                pass
            return self.source.get_playlist_items(srv, iid), meta

        def done(res):
            items, meta = res
            route["_items"] = items
            # Read the *server's* visibility before offering the toggle;
            # assuming private meant the first click could flip a public
            # playlist's visibility based on a value we never read.
            if "OpenAccess" in meta:
                route["_public"] = bool(meta.get("OpenAccess"))
                route["_public_known"] = True
        self._route_async(route, work, done, ep)

    def _load_queue(self, route, ep):
        srv = route.get("server") or self.server

        def work():
            q = ({"items": [], "current_id": None} if self.controller is None
                 else self.controller.get_queue())
            ids = [e["id"] for e in q.get("items", []) if e.get("id")]
            by_id = {}
            if ids:
                try:
                    for it in self.source.get_items_by_ids(srv, ids):
                        by_id[it.get("Id")] = it
                except Exception:
                    pass
            entries = [
                {"item": by_id.get(e["id"], {"Id": e["id"],
                                             "Name": e["id"]}),
                 "pid": e.get("playlist_item_id")}
                for e in q.get("items", [])]
            return {"entries": entries, "current_id": q.get("current_id")}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
