"""The play queue and the playlist editor.

Both are multi-select list editors over an ordered set of items, so both
subclass :class:`SelectionPage` — which is what the mixin was really
expressing when the two shared ``_pe_sel`` / ``_pe_set_sel`` /
``_select_click`` under names prefixed for one of them.

Selection and items live in the *route dict* (``route["_sel"]``,
``route["_items"]``, ``route["_anchor"]``). Edits are optimistic: they mutate
the route dict, call the server, and restore in ``on_error``. Note the runner
drops ``on_error`` when the epoch has moved, so navigating away mid-flight can
leave a rejected edit in the route dict (see ``mpvtk/MIGRATION.md``).
"""

from ...i18n import _
from ...mpvtk.widgets import (
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
from .. import components, theme
from ..components import chrome
from .base import Page


def moves_to_reorder(before_ids, after_ids):
    """Sequential ``(id, absolute_index)`` moves turning ``before_ids``
    into ``after_ids``, each applied as remove-then-insert.

    Derived from the RESULT rather than from the selection. The previous
    version emitted ``(selected_id, target + offset)``, which assumed the
    selection ends up contiguous starting at its new minimum — true for
    Top/Bottom, false for Up/Down, which move each row one step and
    deliberately preserve gaps. It also emitted downward moves in forward
    order, and those do not compose: each removal shifts the rows after
    it, so the second insert lands in the wrong place. Either way the
    editor showed one order and the server ended up with another,
    silently, until the next reload.

    Walking the target left to right is correct by construction — once
    position i matches it is never disturbed again — and usually emits
    FEWER moves than there are selected rows.
    """
    cur = list(before_ids)
    moves = []
    for idx, want in enumerate(after_ids):
        if idx >= len(cur) or cur[idx] == want:
            continue
        if want is None or want not in cur:
            continue
        cur.remove(want)
        cur.insert(idx, want)
        moves.append((want, idx))
    return moves


def block_move(items, sel, where):
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


def pe_title(item):
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


class SelectionPage(Page):
    """Multi-select list semantics shared by the queue and the playlist
    editor. Not a route on its own."""

    def selection(self):
        """Selected row indices as a set (multi-select)."""
        return set(self.route.get("_sel") or ())

    def set_selection(self, sel, anchor=None):
        route = self.route
        route["_sel"] = set(sel)
        if anchor is not None:
            route["_anchor"] = anchor
        self.ctx.invalidate()

    def click_row(self, i, mods):
        """Standard list selection semantics against ``route["_sel"]``.

        - plain click: select just this row, and make it the anchor
        - shift-click: select the whole range from the anchor to here, so two
          clicks pick any run of rows
        - ctrl-click: toggle this row, keeping the rest

        ``mods`` comes from the renderer's click payload (mpvtk carries
        shift/ctrl for handlers that declare a parameter)."""
        route = self.route
        mods = mods or {}
        sel = self.selection()
        anchor = route.get("_anchor")
        if mods.get("shift") and anchor is not None:
            lo, hi = (anchor, i) if anchor <= i else (i, anchor)
            self.set_selection(set(range(lo, hi + 1)))
        elif mods.get("ctrl"):
            sel.symmetric_difference_update({i})
            self.set_selection(sel, anchor=i)
        else:
            self.set_selection({i}, anchor=i)


class QueuePage(SelectionPage):
    kind = "queue"

    def load(self, epoch):
        route = self.route
        srv = self.route.get("server") or self.ctx.server

        def work():
            q = ({"items": [], "current_id": None} if self.ctx.player is None
                 else self.ctx.player.get_queue())
            ids = [e["id"] for e in q.get("items", []) if e.get("id")]
            by_id = {}
            if ids:
                try:
                    for it in self.ctx.source.get_items_by_ids(srv, ids):
                        by_id[it.get("Id")] = it
                except Exception:
                    pass
            entries = [
                {"item": by_id.get(e["id"], {"Id": e["id"],
                                             "Name": e["id"]}),
                 "pid": e.get("playlist_item_id")}
                for e in q.get("items", [])]
            return {"entries": entries, "current_id": q.get("current_id")}
        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        """The play queue, deliberately the same table + toolbar as the
        playlist editor: the two do the same job on the same kind of list."""
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        entries = data.get("entries") or []
        current = data.get("current_id")
        sel = self.selection()
        n = len(entries)
        toolbar = chrome.wrap_row([
            Text(_("Play Queue"), size=26, bold=True), Spacer(),
            Button(_("Top"), id="q-top", icon="vertical_align_top",
                   on_click=lambda: self._move("top")),
            Button(_("Up"), id="q-up", icon="keyboard_arrow_up",
                   on_click=lambda: self._move("up")),
            Button(_("Down"), id="q-down", icon="keyboard_arrow_down",
                   on_click=lambda: self._move("down")),
            Button(_("Bottom"), id="q-bottom", icon="vertical_align_bottom",
                   on_click=lambda: self._move("bottom")),
            Text(_("%d selected") % len(sel) if sel else "", size=15,
                 color=theme.SUBTLE_FG),
            Button(_("Select All"), id="q-all",
                   on_click=lambda: self.set_selection(set(range(n)))),
            Button(_("Clear"), id="q-none",
                   on_click=lambda: self.set_selection(set())),
            Button(_("To Playlist"), id="q-toplaylist", icon="queue_music",
                   on_click=lambda: self._to_playlist()),
            Button(_("Remove"), id="q-remove", icon="delete",
                   on_click=lambda: self._remove_selected()),
        ], size[0] - 2 * chrome.CONTENT_PAD, gap=8)
        rows = [toolbar, Spacer(h=2)]
        if not entries:
            rows.append(Text(_("The queue is empty."), size=18,
                             color=theme.SUBTLE_FG))
        else:
            rows.append(self.ctx.art.tiles.track_list(
                [e["item"] for e in entries], "q",
                lambda i: self.ctx.actions.skip_to(entries[i].get("pid")),
                playing_id=current, selected=sel, scroll_id="queue",
                head_h=60,
                on_select=lambda i, mods: self.click_row(i, mods)))
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=8,
                          align="stretch"), id="queue",
                       flex=1,
                       offset=self.parked_scroll('queue'),
                       on_scroll=lambda off, mx: self.ctx.art.scroll.on_scroll(
                           "queue", off, mx))

    def _to_playlist(self):
        """Save the playing queue as / into a playlist (Tk's playbar
        button). The add-to dialog does the rest."""
        route = self.route
        entries = (route.get("_data") or {}).get("entries") or []
        ids = [e["item"].get("Id") for e in entries if e.get("item")]
        ids = [i for i in ids if i]
        if not ids:
            return
        server = route.get("server") or self.ctx.server
        self.ctx.dialogs.add_to({"Id": ids[0], "Type": "Audio",
                           "Name": _("Play Queue"), "_ids": ids},
                          server=server)

    def _remove_selected(self):
        route = self.route
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        sel = sorted(self.selection())
        pids = [entries[i].get("pid") for i in sel
                if i < len(entries) and entries[i].get("pid")]
        if not pids:
            return
        route["_sel"] = set()

        def reload():
            # Guarded, and against the CAPTURED route. on_error is
            # deliberately not epoch-gated (see AsyncRunner), so this can land
            # after the user has navigated somewhere else entirely — and it
            # used to wipe _data, bump the epoch and re-issue the load for
            # whatever page they were now on, flashing an unrelated view back
            # to a spinner and killing its in-flight load.
            #
            # ctx.nav.is_current, NOT `route is not self.route`: on a Page
            # those are the same object by construction, so the guard would
            # never fire. Only the shell knows which route is on screen.
            if not self.ctx.nav.is_current(route):
                return
            route.pop("_data", None)
            self.ctx.nav.reload(route)

        if self.ctx.player is None:
            return reload()
        # An edit, not a swallowed call: every other edit in this UI reports,
        # and a removal that silently did nothing left the rows on screen with
        # no explanation. Re-read either way — on failure to put back what is
        # really in the queue.
        self.ctx.actions.edit(lambda c: c.queue_remove(pids), on_ok=reload,
                              on_error=reload,
                              error=_("Those items could not be removed."))

    def _move(self, where):
        route = self.route
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        was, was_sel = list(entries), set(self.selection())
        moved = block_move(entries, self.selection(), where)
        if moved is None:
            return
        data["entries"], route["_sel"] = moved
        order = [e["pid"] for e in data["entries"] if e.get("pid")]
        self.ctx.invalidate()

        def restore():
            data["entries"], route["_sel"] = was, was_sel
        self.ctx.actions.edit(lambda c: c.queue_reorder(order),
                              on_error=restore,
                              error=_("The queue could not be reordered."))


class PlaylistEditPage(SelectionPage):
    kind = "playlist_edit"

    def load(self, epoch):
        route = self.route
        srv = self.route.get("server") or self.ctx.server
        iid = self.route["item_id"]

        def work():
            meta: dict = {}
            try:
                meta = self.ctx.source.get_playlist(srv, iid) or {}
            except Exception:
                pass
            return self.ctx.source.get_playlist_items(srv, iid), meta

        def done(res):
            items, meta = res
            route["_items"] = items
            # Read the *server's* visibility before offering the toggle;
            # assuming private meant the first click could flip a public
            # playlist's visibility based on a value we never read.
            if "OpenAccess" in meta:
                route["_public"] = bool(meta.get("OpenAccess"))
                route["_public_known"] = True
        self.route_async(work, done, epoch)

    def render(self, size):
        route = self.route
        items = route.get("_items")
        if items is None:
            return chrome.busy()
        sel = self.selection()
        n = len(items)
        toolbar = chrome.wrap_row([
            Button(_("Top"), id="pe-top", icon="vertical_align_top",
                   on_click=lambda: self._move("top")),
            Button(_("Up"), id="pe-up", icon="keyboard_arrow_up",
                   on_click=lambda: self._move("up")),
            Button(_("Down"), id="pe-down", icon="keyboard_arrow_down",
                   on_click=lambda: self._move("down")),
            Button(_("Bottom"), id="pe-bottom", icon="vertical_align_bottom",
                   on_click=lambda: self._move("bottom")),
            Spacer(),
            Text(_("%d selected") % len(sel) if sel else "", size=15,
                 color=theme.SUBTLE_FG),
            Button(_("Select All"), id="pe-all",
                   on_click=lambda: self.set_selection(set(range(n)))),
            Button(_("Clear"), id="pe-none",
                   on_click=lambda: self.set_selection(set())),
            Button(_("Remove"), id="pe-remove", icon="delete",
                   on_click=lambda: self._remove()),
        ], size[0] - 2 * chrome.CONTENT_PAD, gap=8)
        rename_row = Row([
            TextBox("pe-name", text=route.get("title", ""), w=280,
                    on_change=lambda v: route.__setitem__("_newname", v),
                    on_submit=lambda v: self._rename()),
            Button(_("Rename"), id="pe-rename", icon="edit",
                   on_click=lambda: self._rename()),
            Checkbox(_("Public"), bool(route.get("_public")), id="pe-public",
                     on_toggle=lambda: self._toggle_public()),
            Spacer(),
            Button(_("Delete Playlist"), id="pe-delete", icon="delete",
                   on_click=lambda: self.ctx.dialogs.confirm(
                       _("Delete the playlist %s?") % route.get("title", ""),
                       lambda: self._delete(),
                       title=_("Delete Playlist"), yes=_("Delete"))),
        ], gap=10, align="center")
        table = Table(
            [{"label": "#", "w": 46, "align": "right"},
             {"label": _("Title"), "flex": 3},
             {"label": _("Type"), "w": 120},
             {"label": _("Time"), "w": 80, "align": "right"}],
            [{"id": "pe-row-%d" % i,
              "selected": i in sel,
              "cells": [str(i + 1), pe_title(it),
                        it.get("Type", ""), components.track_duration(it)],
              # A one-parameter handler opts into the click modifiers, which
              # is what makes shift-range selection possible.
              "on_click": (lambda mods, i=i: self.click_row(i, mods))}
             for i, it in enumerate(items)],
            size=17, row_h=34, hover_bg=theme.BUTTON_BG)
        rows = [Text("%s — %s" % (route.get("title", ""), _("Edit")),
                     size=26, bold=True), Spacer(h=4), rename_row, toolbar,
                Spacer(h=2), table]
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="playlist-edit", flex=1,
                       offset=self.parked_scroll('playlist-edit'),
                       on_scroll=lambda off, mx: self.ctx.art.scroll.on_scroll(
                           "playlist-edit", off, mx))

    def _move(self, where):
        """Move the whole selection as a block, preserving its internal
        order — moving 20 rows should not require 20 clicks."""
        route = self.route
        items = route.get("_items") or []
        sel = sorted(self.selection())
        moved = block_move(items, sel, where)
        if moved is None:
            return
        # `items` still references the PRE-move list (block_move copies),
        # which is what the server currently has.
        route["_items"], route["_sel"] = moved
        server = route.get("server") or self.ctx.server
        pid = route["item_id"]
        batch = moves_to_reorder(
            [e.get("PlaylistItemId") for e in items],
            [e.get("PlaylistItemId") for e in route["_items"]])
        self.ctx.invalidate()
        if not batch:
            return
        ep = self.ctx.run.epoch

        def work():
            # One ordered batch, not N concurrent tasks: moves are
            # absolute-index operations that only compose in order.
            self.ctx.player.playlist_move_many(server, pid, batch)

        def done(_ok):
            pass   # the optimistic order is what we just asked for

        def failed(_exc):
            # Don't leave the optimistic order lying: re-read the truth.
            self.ctx.status(_("The playlist could not be reordered."))
            route.pop("_items", None)
            self.ctx.nav.load(route)
        self.ctx.run.run(work, done, ep, on_error=failed)

    def _remove(self):
        route = self.route
        items = route.get("_items") or []
        sel = sorted(self.selection())
        if not sel:
            return
        entries = [items[i] for i in sel if i < len(items)]
        route["_items"] = [it for i, it in enumerate(items)
                           if i not in set(sel)]
        route["_sel"] = set()
        ids = [e.get("PlaylistItemId") for e in entries
               if e.get("PlaylistItemId")]
        self.ctx.invalidate()
        if not ids:
            return
        server = route.get("server") or self.ctx.server
        self.ctx.actions.edit(
            lambda c: c.playlist_remove(server, route["item_id"], ids),
            # Put the rows back: the list showed them gone either way.
            on_error=lambda: route.__setitem__("_items", items))

    def _delete(self):
        """Delete, then navigate — not both at once. Firing the delete onto
        the pool and pruning immediately meant a failed delete still walked
        the user out of a playlist that still exists."""
        route = self.route
        pid = route["item_id"]
        server = route.get("server") or self.ctx.server
        ep = self.ctx.run.epoch

        def work():
            self.ctx.player.playlist_delete(server, pid)
            return True

        def done(_ok):
            self.ctx.nav.after_playlist_deleted(pid)

        def failed(_exc):
            self.ctx.status(_("The playlist could not be deleted."))
        self.ctx.run.run(work, done, ep, on_error=failed)

    def _rename(self):
        route = self.route
        name = (route.get("_newname") or route.get("title") or "").strip()
        if not name:
            return
        was = route.get("title")
        route["title"] = name
        server = route.get("server") or self.ctx.server
        self.ctx.invalidate()
        self.ctx.actions.edit(
            lambda c: c.playlist_update(server, route["item_id"], name=name),
            on_error=lambda: route.__setitem__("title", was))

    def _toggle_public(self):
        route = self.route
        # Refuse until the loader has read the server's OpenAccess: flipping a
        # value we never read could make a public playlist private (or worse,
        # the reverse) on the very first click.
        if not route.get("_public_known"):
            self.ctx.dialogs.message(_("Still reading this playlist's visibility from "
                            "the server. Try again in a moment."))
            return
        was = route.get("_public")
        route["_public"] = not was
        server = route.get("server") or self.ctx.server
        self.ctx.invalidate()
        # Visibility especially must not be left showing a value the server
        # rejected — that is the difference between private and public.
        self.ctx.actions.edit(
            lambda c: c.playlist_update(server, route["item_id"],
                                        is_public=route["_public"]),
            on_error=lambda: route.__setitem__("_public", was))
