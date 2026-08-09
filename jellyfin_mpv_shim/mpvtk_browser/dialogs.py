"""Modal dialogs.

The generic shell (``_show_dialog`` / ``_close_dialog`` / ``_message`` /
``_confirm``), the add-to playlist/collection picker, the download dialog
and the SyncPlay group dialog.

State on ``self``: ``_dialog`` — a builder callable or None — is the single
modal slot, rendered by core's ``build()``. Also ``_addto_build``,
``_addto_ids``, ``_addto_explicit_ids`` and ``_addcol_name`` (add-to
dialog), ``_dl`` (download dialog), ``_bkprog``/``_bkprog_build`` (the
book reading-position dialog) and ``_minfo``/``_minfo_src`` (the media
info dialog). All are loop-thread only.
"""

from ..i18n import _, _p
from ..mpvtk.widgets import (
    Box,
    Button,
    Checkbox,
    Column,
    Dialog,
    Dropdown,
    Row,
    Spacer,
    Text,
    TextBox,
    VScroll,
)
from . import components, theme
from .components import media_info


#: Collection types a filter category can actually match, transcribed
#: from jellyfin-web's ``FilterButton.tsx`` -- ``isFiltersFeaturesEnabled``
#: and friends, which gate by ``viewType``.
#:
#: Our grids are coarser than web's tabs, so the mapping is: a movies
#: library is its Movies tab, a tvshows library its Series tab, a music
#: library its **Albums** tab (that is what our grid lists), and an
#: untyped library its Mixed tab. ``None`` in a gate means an untyped or
#: mixed library.
#:
#: Ungated categories are the ones web offers everywhere, and are left
#: to the "did the server return any options" test below.

#: Features and Video Types: web's ``isFiltersFeaturesEnabled``. These
#: ask about a *media* item -- has it subtitles, a trailer, a theme song
#: -- and a Series is a container with no media of its own, so on a TV
#: library they match by proxy while on a music or books library they
#: can only ever match nothing.
_FEATURE_LIBRARIES = frozenset({"movies", "tvshows"})

#: Audio/subtitle language: web's ``isFiltersLanguagesEnabled``, which is
#: the feature set plus Mixed.
_LANGUAGE_LIBRARIES = frozenset({"movies", "tvshows", None})

#: Played / Unplayed / Resumable: web's ``getVisibleFiltersStatus``,
#: which hides them for Albums, Artists, Songs, Photos and Studios. Of
#: the grids we draw that is the music one -- an album has no play state
#: of its own. Favorites is offered unconditionally, there and here.
#:
#: Written as an exclusion rather than an allow-list because that is what
#: it is: web names the tabs that do NOT get these, and the alternative
#: here would be enumerating every collection type Jellyfin has or might
#: add, where anything left out silently loses its Unplayed box.
_EXCEPT_MUSIC = ("except", frozenset({"music"}))


def _applies(gate, collection_type):
    """Whether a gated row belongs on a ``collection_type`` library.

    ``None`` means everywhere; a frozenset is an allow-list; an
    ``("except", set)`` pair is a deny-list. Both forms are needed --
    "only movies and TV" and "anything but music" are different claims,
    and writing the second as an allow-list means a collection type
    nobody updated the table for quietly loses filters that do apply.
    """
    if gate is None:
        return True
    if isinstance(gate, tuple) and gate and gate[0] == "except":
        return collection_type not in gate[1]
    return collection_type in gate


#: Every category the panel can draw, in web's order. Each is
#: ``(label, kind, spec, libs)``; "vals" names the `_filtervals` entry a
#: picker's options come from, and ``libs`` is the gate above (``None``
#: for "everywhere"). A "checks" spec is ``(key, label, libs)`` per box,
#: because Status is offered on every library but three of its five boxes
#: are not.
#:
#: A category is drawn only when it has something to offer: it applies to
#: this library, and the server returned options for it (or it is a fixed
#: enum). The second half is jellyfin-web's own gate
#: (`!!filters?.AudioLanguages?.length`) and is what makes this work
#: against a server with no language filters -- Jellyfin 11 and earlier
#: -- with no version check anywhere. A section whose every row is gated
#: out is not drawn at all, heading included.
FILTER_SECTIONS = (
    (_("Status"), "checks", (
        ("unplayed", _("Unplayed"), _EXCEPT_MUSIC),
        ("played", _("Played"), _EXCEPT_MUSIC),
        ("favorite", _("Favorites"), None),
        ("resumable", _("Resumable"), _EXCEPT_MUSIC),
        ("liked", _("Liked"), None),
    ), None),
    (_("Features"), "checks", (
        ("has_subtitles", _("Has Subtitles"), None),
        ("has_trailer", _("Has Trailer"), None),
        ("has_special_feature", _("Has Special Features"), None),
        ("has_theme_song", _("Has Theme Song"), None),
        ("has_theme_video", _("Has Theme Video"), None),
    ), _FEATURE_LIBRARIES),
    (_("Genres"), "pick", ("genre", "genres"), None),
    (_("Years"), "pick", ("year", "years"), None),
    (_("Parental Rating"), "pick", ("official_ratings", "official_ratings"),
     None),
    (_("Tags"), "pick", ("tags", "tags"), None),
    (_("Audio Language"), "pick", ("audio_languages", "audio_languages"),
     _LANGUAGE_LIBRARIES),
    (_("Subtitle Language"), "pick",
     ("subtitle_languages", "subtitle_languages"), _LANGUAGE_LIBRARIES),
)


class DialogsMixin:

    # ----------------------------------------------------- add to playlist

    def _open_add_to(self, item, server=None):
        server = server or self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        ep = self._epoch
        # A music container is not itself a playlist entry — Tk resolves
        # album/artist/genre to their track ids before offering the dialog.
        self._addto_ids = None
        # ids the caller supplied outright (the play queue), as opposed to
        # ones resolved from a container
        self._addto_explicit_ids = item.get("_ids")
        parent = self.route.get("parent_id")

        def work():
            def fetch(fn):
                try:
                    return fn(server)
                except Exception:
                    return []
            return (fetch(self.source.get_playlists),
                    fetch(getattr(self.source, "get_collections",
                                  lambda _s: [])),
                    item.get("_ids")
                    or self._resolve_play_ids(item, server, parent))
        self.run_async(
            work, lambda r: self._show_add_to(server, item, r[0], r[1], r[2]),
            ep)

    # Height of a picker list inside a dialog before it scrolls. Enough to
    # show several entries without the dialog growing past the window.
    PICKER_H = 240

    def _picker_list(self, node_id, entries, on_pick, empty_text):
        """Scrollable list of {Id, Name} buttons for a dialog.

        Scrollable, not a dropdown: these are the primary choice in the
        dialog, and a flat list of every playlist made it unusably tall.
        """
        if not entries:
            return Text(empty_text, size="small", color=theme.SUBTLE_FG)
        rows = [Button(e.get("Name", ""), id="%s-%d" % (node_id, i),
                       on_click=lambda eid=e.get("Id"): on_pick(eid))
                for i, e in enumerate(entries)]
        return VScroll(Column(rows, gap=6, align="stretch"),
                       id=node_id, h=self.PICKER_H)

    def _show_add_to(self, server, item, playlists, collections=(),
                     item_ids=None):
        item_id = item.get("Id")
        self._addto_ids = [i for i in (item_ids or [item_id]) if i]
        # Private by default, matching the Tk browser: the server creates
        # playlists public unless told otherwise.
        self._addto_name = {"name": "", "private": True}

        def build():
            named = bool((self._addto_name.get("name") or "").strip())
            rows = [
                Text(_("Add to Playlist"), size="title", bold=True),
                self._picker_list(
                    "add-pl", playlists,
                    lambda pid: self._add_to(server, pid, item_id),
                    _("No playlists yet.")),
                Row([
                    # Enter creates, same as the button beside it. Typing a
                    # name and pressing Enter did nothing.
                    TextBox("add-newname", placeholder=_("New playlist name…"),
                            w=280,
                            on_change=lambda v: self._addto_name_changed(v),
                            on_submit=lambda v: self._add_to_new(
                                server, item_id)),
                    Button(_("Create"), id="add-create",
                           on_click=lambda: self._add_to_new(server, item_id)),
                ], gap=10, align="center"),
            ]
            if named:
                # Only meaningful once there's a playlist to be private:
                # an always-on checkbox above an empty name box is noise.
                rows.append(Checkbox(
                    _("Private (only you can see it)"),
                    bool(self._addto_name.get("private")), id="add-private",
                    on_toggle=self._addto_private_toggled))
            buttons = []
            # Gated on whether the SOURCE does collections, not on whether
            # any exist — gating on the latter meant you could never create
            # your first one. The offline catalog has none either way.
            #
            # And on the permission, which is a different question from the
            # apiclient capability `can_edit` answers: the whole of
            # CollectionController is behind EnableCollectionManagement, and
            # a newly created account does not have it. Without this the
            # button opened a picker whose every row 403s. Playlists stay,
            # because PlaylistController has no such policy.
            if (hasattr(self.source, "get_collections") and not self._offline
                    and self._actions.can_manage_collections(server)):
                buttons.append(Button(
                    _("Collections…"), id="add-collections",
                    on_click=lambda: self._show_add_to_collection(
                        server, item, collections)))
            buttons.append(Button(_("Close"), id="add-close",
                                  on_click=self._close_dialog))
            rows.append(self._dialog_buttons(buttons))
            return Dialog("addto",
                          self._dialog_shell("addto", rows, w=460),
                          on_dismiss=self._close_dialog)
        self._addto_build = build
        self._show_dialog(build)

    def _addto_private_toggled(self):
        """Flip Private, and redraw so the tick moves.

        A Checkbox is composited on this side -- the renderer has no notion
        of one, and draws whatever colour the last tree gave it -- so a
        toggle that only writes state changes nothing on screen. Clicking it
        used to flip the value invisibly: no feedback at all, and after two
        clicks no way to tell what it was going to create. Every other
        on_toggle in the browser redraws; this one was the exception.
        """
        self._addto_name["private"] = not self._addto_name.get("private")
        if self._addto_build:
            self._show_dialog(self._addto_build)

    def _addto_name_changed(self, value):
        """Rebuild only when the name crosses empty <-> non-empty, which is
        what shows or hides the Private checkbox. Rebuilding on every
        keystroke would be pointless churn."""
        was = bool((self._addto_name.get("name") or "").strip())
        self._addto_name["name"] = value
        if bool((value or "").strip()) != was and self._addto_build:
            self._show_dialog(self._addto_build)

    def _show_add_to_collection(self, server, item, collections):
        """Collections get their own window: two long lists stacked in one
        dialog was the crowding."""
        item_id = item.get("Id")
        self._addcol_name = {"name": ""}

        def build():
            rows = [
                Text(_("Add to Collection"), size="title", bold=True),
                self._picker_list(
                    "add-col", collections,
                    lambda cid: self._add_to_col(server, cid, item_id),
                    _("No collections yet.")),
                Row([
                    TextBox("addcol-newname",
                            placeholder=_("New collection name…"), w=280,
                            on_change=lambda v: self._addcol_name.__setitem__(
                                "name", v),
                            on_submit=lambda v: self._add_to_new_col(
                                server, item_id)),
                    Button(_("Create"), id="addcol-create",
                           on_click=lambda: self._add_to_new_col(
                               server, item_id)),
                ], gap=10, align="center"),
                self._dialog_buttons([
                    Button(_("Back"), id="addcol-back",
                           on_click=lambda: self._show_dialog(
                               self._addto_build)),
                    Button(_("Close"), id="addcol-close",
                           on_click=self._close_dialog)]),
            ]
            return Dialog("addtocol",
                          self._dialog_shell("addtocol", rows, w=460),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _add_to_new(self, server, item_id):
        state = self._addto_name or {}
        name = state.get("name", "").strip()
        ids = self._addto_ids or ([item_id] if item_id else [])
        if name and ids:
            private = bool(state.get("private", True))
            self._edit_call(lambda c: c.playlist_new(
                server, name, ids, is_public=not private))
        self._close_dialog()

    def _add_to_new_col(self, server, item_id):
        name = (self._addcol_name or {}).get("name", "").strip()
        ids = self._collection_ids(item_id)
        if name and ids:
            self._edit_call(lambda c: c.collection_new(server, name, ids))
        self._close_dialog()

    def _collection_ids(self, item_id):
        """A collection holds the album, not its 300 tracks — only a
        playlist wants the resolved ids (Tk resolves for playlists only).

        The queue is the exception: it is a set of items with no container
        of its own, so it carries an explicit id list."""
        if self._addto_explicit_ids:
            return list(self._addto_explicit_ids)
        return [item_id] if item_id else []

    def _add_to_col(self, server, collection_id, item_id):
        ids = self._collection_ids(item_id)
        if collection_id and ids:
            self._edit_call(lambda c: c.collection_add(
                server, collection_id, ids))
        self._close_dialog()

    def _add_to(self, server, playlist_id, item_id):
        ids = self._addto_ids or ([item_id] if item_id else [])
        if playlist_id and ids:
            self._edit_call(lambda c: c.playlist_add(server, playlist_id, ids))
        self._close_dialog()

    # -------------------------------------------------------- downloads

    _human_size = staticmethod(components.human_size)

    #: Height the media-info list scrolls at. A film with eight subtitle
    #: tracks is a hundred rows; every other dialog here is a choice with a
    #: handful of options, so this one is the only one that scrolls to a
    #: fixed height rather than sizing to its content.
    MEDIA_INFO_H = 420

    def _open_media_info(self, item, server=None):
        """jellyfin-web's ``itemMediaInfo``: every attribute of every stream.

        **The DTO is refetched rather than trusted.** Web can test
        ``item.MediaSources`` on a card because its list responses carry
        them; ours deliberately do not — MediaSources on a grid query is a
        third of the response body for something no tile draws (see
        repository.GRID_FIELDS). So the menu offers this on *type*, and the
        streams are fetched when it is opened. Web fetches too
        (``loadMediaInfo`` -> ``getItem``); it just had enough to decide
        beforehand.
        """
        server = server or self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        item_id = item.get("Id")
        if not item_id:
            return
        ep = self._epoch
        self._minfo_src = 0

        def work():
            try:
                return self.source.get_item(server, item_id)
            except Exception:
                # Fall back to what the tile already had. It is thin -- no
                # streams -- but the dialog then says "no media information"
                # rather than never opening, which from a menu press is
                # indistinguishable from a broken entry.
                return item

        self.run_async(work, self._show_media_info, ep)

    def _show_media_info(self, item):
        item = item or {}
        sources = item.get("MediaSources") or []

        def build():
            from .components import chrome

            src = sources[self._minfo_src] if sources else {}
            rows = []
            if len(sources) > 1:
                # A version picker, as web has: an item with several
                # versions describes a different file per version, and
                # showing only the first is quietly wrong rather than
                # incomplete.
                rows.append(Dropdown(
                    "minfo-src",
                    [s.get("Name") or _("Unknown") for s in sources],
                    selected=self._minfo_src,
                    on_select=self._pick_media_info_source,
                    popup_w=self.MESSAGE_W))
            for label, value in media_info.source_attributes(src):
                rows.append(self._minfo_row(label, value))
            for stream in media_info.visible_streams(src):
                attrs = media_info.stream_attributes(stream, src)
                if not attrs:
                    continue
                rows.append(Text(media_info.stream_heading(stream), size="normal",
                                 bold=True, color=theme.ACCENT))
                for label, value in attrs:
                    rows.append(self._minfo_row(label, value))
            if not rows:
                rows.append(Text(_("No media information is available."),
                                 size="small", color=theme.SUBTLE_FG))
            return Dialog("minfo", self._dialog_shell("minfo", [
                Text(_("Media Info"), size="title", bold=True),
                chrome.paragraph(item.get("Name") or "", 16,
                                 self.MESSAGE_W, color=theme.SUBTLE_FG),
                VScroll(Column(rows, gap=6, align="stretch"),
                        id="minfo-scroll", h=self.MEDIA_INFO_H),
                self._dialog_buttons([
                    Button(_("OK"), id="minfo-ok",
                           on_click=self._close_dialog, autofocus=True)]),
            ]), on_dismiss=self._close_dialog)

        self._minfo_build = build
        self._show_dialog(build)

    #: The label column, and what a value therefore gets. **An explicit
    #: width, not flex** -- and that is load-bearing, not tidiness: a
    #: ``wrap=True`` Text with no ``w`` measures one line tall (layout.py's
    #: note on `measure`), so inside a Row it makes the Row too short, and
    #: the text is then clipped and the last visible line ellipsized. A path
    #: with no spaces to break on -- `/media/Films/Blade_Runner_2049/...` --
    #: is the shape that shows it, and losing its tail loses the filename,
    #: which is the half anyone reads.
    MINFO_LABEL_W = 150
    MINFO_VALUE_W = 440 - 2 * 24 - 150 - 8

    def _minfo_row(self, label, value):
        """One labelled attribute, wrapped rather than ellipsized."""
        return Row([
            Text(label, size="small", color=theme.SUBTLE_FG,
                 w=self.MINFO_LABEL_W),
            Text(value, size="small", wrap=True, w=self.MINFO_VALUE_W),
        ], gap=8, align="start")

    def _pick_media_info_source(self, index, _value):
        """Switch versions.

        Reads the index in the *handler* and asks for a repaint. The
        renderer flips a Dropdown's own selection optimistically, so the
        control would look right while every row below it still described
        the old file -- which is the browser's standing footgun in its
        quietest form.
        """
        self._minfo_src = int(index)
        self.invalidate()

    def _open_download(self, item):
        server = self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        # The include-watched filter is only meaningful for a container.
        # For a single item it must be True, or Download on something you
        # have already watched enqueues nothing at all, silently.
        # "Folder" is here for books: a multi-file audiobook is a folder of
        # chapter files and nothing else joins them, so the folder is the
        # download unit (see sync.manager._expand). Without it the dialog
        # treated one as a single item, forced Include Watched on, and
        # offered no way to skip the chapters already listened to.
        container = item.get("Type") in ("Series", "Season", "Playlist",
                                         "MusicAlbum", "MusicArtist",
                                         "BoxSet", "Folder", "CollectionFolder")
        self._dl = {"server": server, "item": item, "est": None,
                    "container": container, "watched": not container}
        ep = self._epoch

        def work():
            return self.controller.download_estimate(
                server, item.get("Id"), item.get("Type"))

        def done(est):
            if self._dl is not None:
                self._dl["est"] = est
                if self._dl["container"]:
                    self._dl["watched"] = bool((est or {}).get("audio_only"))
            self._show_download()

        def failed(_exc):
            # Say the estimate failed rather than leaving the dialog on its
            # "estimating…" state forever. The controller used to return a
            # zero estimate here, which the dialog rendered as "Nothing left
            # to download." — a server error reported as success, with the
            # Download button withheld.
            if self._dl is not None:
                self._dl["error"] = _("Could not check what needs "
                                      "downloading.")
            self._show_download()

        self.run_async(work, done, ep, on_error=failed)
        self._show_download()   # show immediately with an "estimating" state

    def _show_download(self):
        dl = self._dl
        if dl is None:
            return

        def build():
            est = dl["est"]
            if dl.get("error"):
                info = Text(dl["error"], size="small", color=theme.FAV_RED)
            elif est is None:
                info = Text(_("Estimating…"), size="small", color=theme.SUBTLE_FG)
            else:
                line = _("%(count)d items · %(size)s") % {
                    "count": est.get("count", 0),
                    "size": self._human_size(est.get("total_bytes", 0))}
                extra = []
                if est.get("already_count"):
                    extra.append(_("%d already downloaded")
                                 % est["already_count"])
                if est.get("watched_count"):
                    extra.append(_("%d watched") % est["watched_count"])
                # A book states its size nowhere on the wire, so its share
                # of the total is genuinely unknown. Said out loud: a size
                # that silently undercounts is worse than one that admits
                # what it left out, and for a books-only download the whole
                # figure would otherwise read as 0 B.
                if est.get("unsized_count"):
                    extra.append(_("%d of unknown size")
                                 % est["unsized_count"])
                if extra:
                    line += "   (" + ", ".join(extra) + ")"
                info = Text(line, size="small", color=theme.SUBTLE_FG)
            return Dialog("download", self._dialog_shell("download", [
                # The dialog's NOUN heading. The button below it and the
                # tile menu entry are the verb, and German splits them:
                # Download vs Herunterladen.
                Text(_p("dialog heading", "Download"), size="title", bold=True),
                Text(dl["item"].get("Name", ""), size="normal"),
                info,
            ] + ([Checkbox(_("Include watched"), dl["watched"],
                           id="dl-watched",
                           on_toggle=self._dl_toggle_watched)]
                 if dl["container"] else []) + [
                self._dialog_buttons([
                    Button(_("Cancel"), id="dl-cancel",
                           on_click=self._close_download),
                    # Confirming before the estimate lands loses the
                    # audio_only default and skips played tracks. And an
                    # estimate of nothing means there is nothing to fetch —
                    # everything here is already downloaded — so offering
                    # Download is a dead click. Tk guarded on the count.
                    Button(_("Download"), id="dl-ok",
                           on_click=self._dl_confirm)
                    if est is not None and est.get("count", 0) else
                    Text(_("Estimating…") if est is None
                         else _("Nothing left to download."),
                         size="small", color=theme.SUBTLE_FG)]),
            ], w=460), on_dismiss=self._close_download)
        self._show_dialog(build)

    def _dl_toggle_watched(self):
        if self._dl is not None:
            self._dl["watched"] = not self._dl["watched"]
            self._show_download()

    def _close_download(self):
        self._dl = None
        self._close_dialog()

    def _dl_confirm(self):
        dl = self._dl
        self._close_download()
        if dl is None:
            return
        item = dl["item"]
        # _edit_call, not _client_call: the latter swallows, so a rejected
        # download looked exactly like a queued one and the item just never
        # turned up.
        self._edit_call(
            lambda c: c.download_enqueue(dl["server"], item.get("Id"),
                                         item.get("Type"), dl["watched"]),
            on_ok=self._refresh_downloaded,
            error=_("The download could not be started."))

    # ------------------------------------------------------------- dialogs

    def _show_dialog(self, builder):
        self._dialog = builder
        self.invalidate()

    def _close_dialog(self):
        self._dialog = None
        self.invalidate()

    # ---------------------------------------------------------- view settings

    def filter_panel(self, get_vals, get_filters, on_set, on_toggle,
                     on_clear, collection_type=None):
        """The filter panel: a modal, because it is a page of controls.

        Not accordions, which is what jellyfin-web uses to keep the height
        down -- **[iw]**: "I'm inclined to use checkboxes and drop-downs
        instead, accordions are annoying". So every category is open and
        the body scrolls, which is the same shape as the Live TV guide's
        settings dialog.

        ``collection_type`` gates the categories to the ones this library
        can match (see FILTER_SECTIONS). A plain value rather than a
        getter, unlike the other three: it is part of the route's
        identity, so it cannot change while this panel is open -- which
        is the one kind of capture the stale-capture audit accepts.
        """
        def build():
            vals = get_vals()
            filters = get_filters()
            rows: list = []
            for label, kind, spec, libs in FILTER_SECTIONS:
                if not _applies(libs, collection_type):
                    continue
                if kind == "checks":
                    boxes = [
                        Checkbox(text, bool(filters.get(key)),
                                 id="flt-" + key,
                                 on_toggle=lambda k=key: on_toggle(k))
                        for key, text, box_libs in spec
                        if _applies(box_libs, collection_type)
                    ]
                    if not boxes:
                        # Every box gated out, so the heading would be a
                        # section label with nothing under it.
                        continue
                    rows.append(Text(label, size="normal", bold=True))
                    rows += boxes
                    continue
                key, vals_key = spec
                options = vals.get(vals_key) or []
                if not options:
                    # Nothing to choose from: the server does not offer
                    # this category (or has no items carrying it), so the
                    # picker would be an empty list pretending to be a
                    # control.
                    continue
                rows.append(Text(label, size="normal", bold=True))
                rows.append(self._filter_picker(key, options, filters, on_set))
            if not rows:
                rows = [Text(_("This library offers no filters."),
                             size="small", color=theme.SUBTLE_FG)]
            return Dialog(
                "filters",
                self._dialog_shell("filters", [
                    Text(_("Filters"), size="title", bold=True),
                    VScroll(Column(rows, gap=10, align="stretch"),
                            id="flt-body", h=380),
                    self._dialog_buttons([
                        Button(_("Clear All"), id="flt-clear",
                               on_click=on_clear),
                        Button(_("Done"), id="flt-done",
                               on_click=self._close_dialog),
                    ]),
                ], w=520),
                on_dismiss=self._close_dialog)
        self._show_dialog(build)

    @staticmethod
    def _option_pair(option):
        """``(label, value)`` for a picker option.

        The two endpoints answer differently: `Filters` gives bare
        strings (a genre, a rating), `Filters2` gives NameValuePairs the
        source has already flattened to tuples ("English (eng)", "eng").
        """
        if isinstance(option, (tuple, list)) and len(option) == 2:
            return str(option[0]), option[1]
        return str(option), option

    def _filter_picker(self, key, options, filters, on_set):
        pairs = [self._option_pair(o) for o in options]
        current = filters.get(key)
        selected = 0
        for i, (_label, value) in enumerate(pairs):
            if value == current or str(value) == str(current):
                selected = i + 1
                break
        return Dropdown(
            "flt-" + key, [_("Any")] + [lbl for lbl, _v in pairs],
            selected=selected, w=440, popup_w=440,
            on_select=lambda i, _v, p=pairs, k=key: on_set(
                k, None if i == 0 else p[i - 1][1]))

    def view_settings(self, current, on_set, paginated=None):
        """A library's view settings, in a modal.

        Same shape as the guide's settings dialog and for the same reason:
        controls that are read once and then rarely touched do not earn
        permanent space on the filter row, which is already carrying the
        sort, three filters, Play All and Shuffle.

        ``current(setting)`` reads a live value and ``on_set(setting,
        value)`` writes one -- applied immediately rather than on a Save
        button, because each is a one-click change the user can see happen
        and undo. There is nothing to validate and nothing to batch.

        ``paginated`` is ``(is_on, toggle)`` for the pagination switch, or
        None to leave it out. A separate argument rather than another
        ``current``/``on_set`` name because it is not the same kind of
        setting: the others are this library's and live on the server, this
        one is the application's and lives in ``conf.json``. The dialog says
        so rather than quietly filing it with them.
        """
        from . import view_prefs

        def _is_list(read):
            return view_prefs.is_list(read("imageType"), read("viewType"))

        def build():
            image_types = [
                ("primary", _("Auto")), ("poster", _("Poster")),
                ("thumb", _("Thumbnail")),
                ("banner", _("Banner")), ("logo", _("Logo")),
                ("disc", _("Disc")),
            ]
            values = [v for v, _l in image_types]
            # While the list view is on, imageType IS "list" -- there is no
            # artwork being drawn to point at, so the picker falls back to
            # Auto and choosing any entry writes it, which is what leaves
            # the list.
            stored = current("imageType")
            if _is_list(current):
                stored = view_prefs.GRID_IMAGE_TYPE
            body = [
                self._picker(
                    _("Artwork"), "vs-imagetype",
                    [lbl for _v, lbl in image_types],
                    values.index(stored) if stored in values else 0,
                    lambda i, v: on_set("imageType", values[i])),
                Text(_("“Auto” shapes the tiles from the artwork itself, "
                       "which usually comes out as posters. Choose "
                       "“Poster” to insist on them."),
                     size="caption", color=theme.SUBTLE_FG, wrap=True),
                Checkbox(_("Show titles"), bool(current("showTitle")),
                         id="vs-showtitle",
                         on_toggle=lambda: on_set(
                             "showTitle", not current("showTitle"))),
                Checkbox(_("Show years"), bool(current("showYear")),
                         id="vs-showyear",
                         on_toggle=lambda: on_set(
                             "showYear", not current("showYear"))),
                # Stored as an ARTWORK value, which is where web keeps it:
                # its own picker is one dropdown of primary/banner/disc/
                # logo/thumb/list. Two controls here because "draw this as a
                # table" is a different question from "which picture", but
                # they write the one key, so leaving the list is picking an
                # artwork type -- which is exactly what it is in web.
                Checkbox(_("List instead of a grid"), _is_list(current),
                         id="vs-listview",
                         on_toggle=lambda: on_set(
                             "imageType",
                             view_prefs.GRID_IMAGE_TYPE if _is_list(current)
                             else view_prefs.LIST_IMAGE_TYPE)),
                Text(_("These are stored on your server and shared with "
                       "Jellyfin Web."), size="caption", color=theme.SUBTLE_FG,
                     wrap=True),
            ]
            device = self._cover_size_row()
            # Only while this library is actually drawing logos. It is a
            # global setting, so it is reachable from Settings whatever is on
            # screen; here it is offered at the moment it is visibly doing
            # something, which is the same argument Cover Size rides in on.
            if stored == "logo":
                device += self._logo_legibility_row()
            if paginated is not None:
                is_on, toggle = paginated
                device += [
                    Checkbox(_("Paginated"), bool(is_on()), id="vs-paginated",
                             on_toggle=toggle),
                    Text(_("Show one page of tiles at a time instead of "
                           "scrolling. Applies to every library on this "
                           "device."), size="caption", color=theme.SUBTLE_FG,
                         wrap=True),
                ]
            if device:
                # A rule, because what follows is not one of the above:
                # everything over the line is this library's and lives
                # on the server, everything under it is this device's.
                body += [Box(h=1, bg=theme.BORDER)] + device
            return Dialog("viewcfg", self._dialog_shell("viewcfg", [
                Text(_("View Settings"), size="title", bold=True),
                Column(body, gap=12, align="stretch"),
                self._dialog_buttons([
                    Button(_("Done"), id="vs-done",
                           on_click=self._close_dialog)]),
            ], w=440), on_dismiss=self._close_dialog)

        self._show_dialog(build)

    def _cover_size_row(self):
        """The global Cover Size setting, on the View menu.

        It lives in Settings, but this is where you find out what the values
        mean: changing it and walking back to a library to look was the whole
        difficulty (#616). Below the rule with Paginated, because like that
        one it is this device's setting rather than this library's.

        Returns [] when there is no config to write to (the offline
        stand-ins), so the dialog simply does not offer it.
        """
        from ..conf import settings
        from . import config as cfg

        config = self._config()
        if not hasattr(config, "set_setting"):
            return []
        opts = cfg.LABELED_ENUMS["poster_scale"]
        cur = getattr(settings, "poster_scale", None)
        sel = next((i for i, (_l, v) in enumerate(opts) if v == cur), 0)

        def pick(i, _v):
            if config.set_setting("poster_scale", opts[i][1]):
                self.apply_cover_size()

        return [
            Row([Text(_("Cover Size"), w=150, size="normal",
                      color=theme.SUBTLE_FG),
                 Dropdown("vs-coversize", [lbl for lbl, _v in opts],
                          selected=sel, w=200, size="normal", force=True,
                          on_select=pick)],
                gap=8, align="center"),
        ]

    def _logo_legibility_row(self):
        """"Make library logos more legible", on the View menu.

        The LIBRARY half of the pair -- the Live TV one is Settings-only,
        because Live TV has no View menu to put it on and its default is the
        one nobody has to go looking for. This is the half that is off by
        default (a film's logo is white by convention and already reads on a
        dark background), so this is the one someone with dark logo artwork
        has to be able to find, and here is where they are looking: the
        library they just set to Logo artwork (#637).

        Offered only in that case, for the same reason. It is this device's
        setting rather than this library's, so it sits under the rule with
        Cover Size and Paginated.

        Returns [] when there is no config to write to (the offline
        stand-ins), exactly as :meth:`_cover_size_row` does.
        """
        from ..conf import settings
        from . import config as cfg

        config = self._config()
        if not hasattr(config, "set_setting"):
            return []
        key = "logo_legibility_library"
        on = bool(getattr(settings, key, False))

        def toggle():
            if config.set_setting(key, not on):
                self.apply_logo_legibility()

        return [
            Checkbox(cfg.label_for(key), on,
                     id="vs-logolegible", on_toggle=toggle),
            Text(_("Backs transparent logos with the light plate they were "
                   "drawn for, and shadows the ones whose own outline is "
                   "white. Off puts the theme's card colour behind them "
                   "instead, with no shadows."),
                 size="caption", color=theme.SUBTLE_FG, wrap=True),
        ]

    @staticmethod
    def _dialog_shell(node_id, children, w=440):
        # align="stretch" so button rows fill the shell's width; without it
        # they take their natural width and a trailing flex Spacer has no
        # leftover to absorb, which left the buttons hugging the left edge.
        return Column(children, pad=24, gap=16, bg=theme.PANEL_BG,
                      radius=12, border=theme.BORDER, w=w,
                      align="stretch")

    @staticmethod
    def _dialog_buttons(children):
        """Dialog action row: always trailing-aligned."""
        return Row(children, gap=10, justify="end")

    #: Text width inside a dialog shell: its width less the padding on both
    #: sides. Used to wrap a message rather than let it ellipsize.
    MESSAGE_W = 440 - 2 * 24

    def _message(self, text, title=None):
        title = title or _("Notice")

        def build():
            from .components import chrome

            return Dialog("msg", self._dialog_shell("msg", [
                Text(title, size="title", bold=True),
                # Wrapped, not a bare Text. A plain one ellipsizes at the
                # shell's width, which is fine for "Recording scheduled."
                # and self-defeating for a dialog whose entire content is
                # an explanation -- the reason a control is missing was
                # being cut off mid-sentence.
                chrome.paragraph(text, 16, self.MESSAGE_W,
                                 color=theme.SUBTLE_FG),
                self._dialog_buttons([
                    Button(_("OK"), id="dlg-ok",
                           on_click=self._close_dialog)]),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _on_clipboard_error(self, op, need):
        """Neither MPV's clipboard nor a desktop helper could be used.

        MPV only gained an X11 clipboard backend in 0.41 (its
        --clipboard-backends default is win32,mac,wayland,vo), so on an
        older MPV under X11 copy and paste do nothing at all. Silence
        reads as the text field being broken; say what to install.
        The renderer raises this at most once per session."""
        if op == "copy":
            text = _("Copying to the clipboard is not available.")
        else:
            text = _("Pasting from the clipboard is not available.")
        if need:
            text += " " + (
                _('Install the "%(package)s" package (for example '
                  '"apt install %(package)s"), or use MPV 0.41 or newer.')
                % {"package": need})
        else:
            text += " " + _("Use MPV 0.41 or newer.")
        self._message(text, title=_("Clipboard"))

    def _confirm(self, text, on_yes, title=None, yes=None, detail=None):
        """Ask before doing something. ``detail`` is a second, emphasised
        line for a confirmation whose *consequence* is the point.

        The message wraps. It used to be a bare Text, which ellipsizes at
        the shell's width -- so "Remove <a long film name> from this
        playlist?" was already being cut off, and a confirmation that
        explains what it is about to destroy would have had the explanation
        truncated. `_message` had worked this out and used
        `chrome.paragraph`; this one had not.
        """
        title = title or _("Confirm")
        yes = yes or _("OK")

        def build():
            from .components import chrome

            rows = [
                Text(title, size="title", bold=True),
                chrome.paragraph(text, 16, self.MESSAGE_W,
                                 color=theme.SUBTLE_FG),
            ]
            if detail:
                # Not SUBTLE_FG: this is the sentence the dialog exists to
                # make sure was read.
                rows.append(chrome.paragraph(detail, 16, self.MESSAGE_W))
            return Dialog("confirm", self._dialog_shell("confirm", rows + [
                self._dialog_buttons([
                    Button(_("Cancel"), id="dlg-cancel",
                           on_click=self._close_dialog),
                    Button(yes, id="dlg-ok",
                           on_click=lambda: (self._close_dialog(),
                                             on_yes()))]),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    # -- book reading progress --------------------------------------------

    def _open_book_progress(self, item, server=None):
        """Pull a book's reading position from the server, and let the user
        push a corrected one back.

        This exists because the shim hands a book to an external reader and
        never hears from it again. Everything else in the library reports
        its own progress — the player is watching. A book has no player, so
        the round trip is manual: pull to see where another device left off
        before opening the file, push to record where you actually got to
        after closing it.

        It is a *pull*, not a read of the DTO on screen. The position moving
        on some other client is the entire situation this dialog is for, so
        showing the number the page happened to load would answer the one
        question it was opened to ask, wrongly.
        """
        from ..books import (PROGRESS_NONE, PROGRESS_PAGES, PROGRESS_PERCENT,
                             progress_of, progress_settable)

        server = server or self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        mode, value, total = progress_of(item)
        if not progress_settable(item):
            # Two different refusals, because they have different answers.
            if mode == PROGRESS_PERCENT:
                # An epub's stored number is an index into epub.js's
                # locations array over ~1024-character runs, not a
                # percentage of anything a reader displays. There is no
                # number for the user to read off their reader and type
                # here, and the scale is not the one the word "percent"
                # implies -- so a box that accepted one would record a
                # confident wrong place. Shown read-only instead.
                text = _("%s records your place as an ebook reader "
                         "position, which no application shows you a "
                         "number for — so there is nothing meaningful to "
                         "set by hand. It is still read from the server "
                         "and shown above.") % (item.get("Name") or "")
            else:
                # mobi and azw resolve as books but carry no runtime at
                # all, so there is no unit to show a position in.
                text = (_("%s stores no reading position.")
                        % (item.get("Name") or ""))
            self._message(text, title=_("Reading Progress"))
            return
        self._bkprog = {
            "item": item, "server": server, "mode": mode,
            "value": value, "total": total, "typed": str(value),
            "busy": True, "note": "",
        }
        self._pull_book_progress()

        def build():
            state = self._bkprog
            if state is None:
                return None
            # Only the paged formats reach here (progress_settable), so
            # this reads as pages throughout rather than branching on a
            # mode that can only have one value.
            if state["busy"]:
                current = Text(_("Reading the position…"), size="small",
                               color=theme.SUBTLE_FG)
            else:
                total_now = state["total"]
                current = Text(
                    (_("Page %(page)d of %(total)d") % {
                        "page": state["value"], "total": total_now}
                     if total_now else _("Page %d") % state["value"]),
                    size="normal")
            rows = [
                Text(_("Reading Progress"), size="title", bold=True),
                Text(item.get("Name", ""), size="normal"),
                current,
            ]
            if state["note"]:
                rows.append(Text(state["note"], size="small",
                                 color=theme.SUBTLE_FG))
            rows.append(Row([
                Text(_("Page"), size="small", color=theme.SUBTLE_FG),
                # force=True: a Pull has to be able to move this box. The
                # renderer keeps its own edit state otherwise, which is
                # right for a field the user is typing in and wrong for one
                # whose whole purpose is to show what the server just said.
                # Safe because on_change writes `typed` on every keystroke,
                # so the forced value IS what was typed.
                TextBox("bkprog-value", text=state["typed"], w=120,
                        force=True,
                        on_change=lambda v: state.__setitem__("typed", v),
                        on_submit=lambda v: self._save_book_progress()),
                Text(_("of %d") % state["total"] if state["total"] else "",
                     size="small", color=theme.SUBTLE_FG),
            ], gap=10, align="center"))
            rows.append(self._dialog_buttons([
                Button(_("Close"), id="bkprog-close",
                       on_click=self._close_book_progress),
                # Named for what it does, not "Refresh": the pair of verbs
                # is the whole model this dialog is asking the user to hold.
                Button(_("Pull"), id="bkprog-pull", icon="cloud_download",
                       on_click=self._pull_book_progress),
                Button(_("Push"), id="bkprog-push", icon="cloud_upload",
                       on_click=self._save_book_progress),
            ]))
            return Dialog("bkprog", self._dialog_shell("bkprog", rows),
                          on_dismiss=self._close_book_progress)
        self._bkprog_build = build
        self._show_dialog(build)

    def _close_book_progress(self):
        self._bkprog = None
        self._bkprog_build = None
        self._close_dialog()

    def _pull_book_progress(self):
        """Re-read the position from the server and redraw.

        Redraw explicitly: the dialog is a builder closure over ``_bkprog``,
        and the values it shows are read when it BUILDS. Writing new numbers
        into the dict changes nothing on screen until something asks for a
        frame — the same rule the Checkbox bug was about (see the browser's
        docs on state that changes between draws).
        """
        from ..books import progress_of

        state = self._bkprog
        if state is None:
            return
        state["busy"] = True
        state["note"] = ""
        self._redraw_book_progress()
        ep = self._epoch
        item, server = state["item"], state["server"]

        def work():
            return self.controller.get_position(server, item.get("Id"))

        def done(userdata):
            if self._bkprog is not state:
                return              # dialog closed, or opened on another book
            state["busy"] = False
            if userdata is None:
                state["note"] = _("The position could not be read.")
            else:
                # Read through the same helper the page uses, against a copy
                # carrying the fresh UserData — so the tick-to-page rules
                # live in exactly one place.
                fresh = dict(item, UserData=userdata)
                _mode, value, total = progress_of(fresh)
                state["value"], state["total"] = value, total
                state["typed"] = str(value)
                # The item the page is drawing shares this dict, so the
                # screen behind the dialog is now right too.
                item["UserData"] = userdata
            self._redraw_book_progress()

        def failed(_exc):
            if self._bkprog is not state:
                return
            state["busy"] = False
            state["note"] = _("The position could not be read.")
            self._redraw_book_progress()

        self.run_async(work, done, ep, on_error=failed)

    def _save_book_progress(self):
        """Push a page number. Pages are the only unit this accepts — see
        ``books.progress_settable`` for why an epub has none to offer."""
        from ..books import ticks_for_page

        state = self._bkprog
        if state is None or state["busy"]:
            return
        try:
            typed = float((state["typed"] or "").strip())
        except ValueError:
            state["note"] = _("Enter a number.")
            self._redraw_book_progress()
            return
        page = int(typed)
        if page < 1 or (state["total"] and page > state["total"]):
            state["note"] = (_("Enter a page between 1 and %d.")
                             % state["total"] if state["total"]
                             else _("Enter a page of 1 or more."))
            self._redraw_book_progress()
            return
        ticks = ticks_for_page(page)
        state["busy"] = True
        self._redraw_book_progress()
        ep = self._epoch
        item, server = state["item"], state["server"]

        def work():
            return self.controller.set_position(server, item.get("Id"), ticks)

        def done(ok):
            if self._bkprog is not state:
                return
            state["busy"] = False
            if ok:
                # Not a local guess at the new value: the pull is what makes
                # the dialog agree with the server, and a push the server
                # quietly clamped would otherwise be shown as accepted.
                self._pull_book_progress()
                self.set_status(_("Reading position saved."))
            else:
                state["note"] = _("The position could not be saved.")
                self._redraw_book_progress()

        def failed(_exc):
            if self._bkprog is not state:
                return
            state["busy"] = False
            state["note"] = _("The position could not be saved.")
            self._redraw_book_progress()

        self.run_async(work, done, ep, on_error=failed)

    def _redraw_book_progress(self):
        if self._bkprog_build is not None:
            self._show_dialog(self._bkprog_build)

    # -- SyncPlay ---------------------------------------------------------

    def _open_syncplay(self):
        server = self.server
        if self.controller is None or server is None:
            return
        ep = self._epoch

        def work():
            # None: every connected server, not just the selected one. A
            # group belongs to one server, so filtering to self.server made
            # half of them invisible with two accounts signed in.
            return (self.controller.get_sync_groups(None),
                    self.controller.sync_state())

        def done(res):
            groups, state = res
            self._show_syncplay(server, groups, state)

        # Fetch groups off-thread, then show the dialog on the loop.
        self.run_async(work, done, ep)

    def _may_create_sync_group(self, server):
        """Fails open; see user_policy."""
        from ..user_policy import CREATE_AND_JOIN

        ask = getattr(self.source, "syncplay_access", None)
        if ask is None or server is None:
            return True
        try:
            return ask(server) == CREATE_AND_JOIN
        except Exception:
            return True

    def _show_syncplay(self, server, groups, state=None):
        joined = (state or {}).get("group_id")
        multi = len({g.get("server_uuid") for g in groups}) > 1

        def build():
            rows = [Text(_("SyncPlay"), size="title", bold=True)]
            if groups:
                for i, g in enumerate(groups):
                    gid = g.get("id")
                    here = joined is not None and gid == joined
                    who = ", ".join(g.get("participants") or [])
                    # Which server, but only when it disambiguates — a
                    # single-server session does not need it on every row.
                    if multi and g.get("server_name"):
                        who = ("%s · %s" % (g["server_name"], who) if who
                               else g["server_name"])
                    rows.append(Column([
                        Button(
                            # The joined group is not a join button; it says
                            # where you are. Every group used to look
                            # equally joinable.
                            (_("%s (joined)") % g.get("name")) if here
                            else (g.get("name") or _("Group")),
                            id="sp-join-%d" % i,
                            bg=theme.ACCENT if here else None,
                            fg=theme.ACCENT_FG if here else theme.TEXT_FG,
                            # Same rule as a tab: being the joined group
                            # outranks being under the pointer, and the
                            # default hover fill would paint the accent out.
                            hover={"fill": theme.ACCENT_HOVER if here
                                   else theme.BUTTON_ACTIVE},
                            on_click=(self._close_dialog if here else
                                      (lambda gid=gid, srv=g.get("server_uuid"):
                                       self._sync_join(srv or server, gid)))),
                        Text(who, size="caption", color=theme.SUBTLE_FG)
                        if who else Spacer(h=0),
                    ], gap=2))
            else:
                rows.append(Text(_("No active groups."), size="small",
                                 color=theme.SUBTLE_FG))
            # Two rows, not one: what you can do to the GROUP, and then what
            # you can do to the dialog. Five buttons on one line ran off the
            # right edge the moment Resume appeared -- and this set is
            # variable (Resume, Leave and New Group each come and go), so
            # widening to fit the worst case would leave the common case
            # mostly empty. Split by meaning and each row is short.
            actions = []
            # SyncPlayAccess is three-valued: `JoinGroups` may join a group
            # somebody else made and may not make one. Treating it as a
            # boolean either hides a dialog that works or offers a button
            # that 403s. See user_policy.
            if self._may_create_sync_group(server):
                actions.append(Button(_("New Group"), id="sp-new",
                                      on_click=lambda: self._sync_new(server)))
            if (state or {}).get("can_resume"):
                # Stopping playback halts the group instead of leaving it, so
                # this is the way back into what the others are watching.
                # jellyfin-web's LabelSyncPlayResumePlayback, same place in
                # the same menu.
                actions.append(Button(_("Resume local playback"), id="sp-resume",
                                      on_click=lambda: self._sync_resume()))
            if joined is not None:
                # Only when there is something to leave. It used to render
                # unconditionally, so the one control that changes state was
                # offered when it could do nothing.
                actions.append(Button(
                    _("Leave"), id="sp-leave",
                    on_click=lambda srv=(state or {}).get("server_uuid"):
                        self._sync_leave(srv or server)))
            if actions:
                rows.append(Row(actions, gap=10, align="center"))
            rows.append(self._dialog_buttons([
                Button(_("Refresh"), id="sp-refresh",
                       on_click=lambda: self._open_syncplay()),
                Button(_("Close"), id="sp-close", on_click=self._close_dialog),
            ]))
            # Wider than the other dialogs (440-480) because it carries the
            # most buttons, and with room to spare on purpose: every label
            # here is translated, and the row is measured in English by the
            # only test that can see it.
            return Dialog("syncplay", self._dialog_shell("syncplay", rows,
                                                         w=560),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    # Joining, creating and leaving are all button presses, so a failure has
    # to reach the user; _client_call swallows. The dialog closes first —
    # these are round trips, and holding it open until they land reads as a
    # hang — so the report lands on the status line behind it.
    def _sync_join(self, server, group_id):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_join(server, group_id),
                        error=_("Could not join the SyncPlay group."))

    def _sync_new(self, server):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_new(server),
                        error=_("Could not create the SyncPlay group."))

    def _sync_leave(self, server):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_leave(server),
                        error=_("Could not leave the SyncPlay group."))

    def _sync_resume(self):
        self._close_dialog()
        self._edit_call(lambda c: c.sync_resume(),
                        error=_("Could not resume SyncPlay playback."))
