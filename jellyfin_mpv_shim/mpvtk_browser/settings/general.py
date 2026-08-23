"""The General tab: the schema-driven config form.

``_setting_row`` renders one row per entry in ``settings_schema``, so adding a
setting is a schema change rather than a UI change. The rest is the handful of
controls that need more than a schema row -- the download-folder move, the
advanced toggle, the auto-download scope.
"""

import logging

from ...conf import settings
from ...i18n import _
from ...mpvtk.widgets import (
    Button,
    Checkbox,
    Column,
    Dropdown,
    Row,
    Text,
    TextBox,
    VScroll,
)
from .. import theme

log = logging.getLogger("mpvtk_browser.settings")


class GeneralTabMixin:

    #: Width of a settings field, and of the label column beside it. One
    #: constant because a form whose fields do not line up reads as broken;
    #: see _setting_row for the one thing allowed to exceed it, and why it is
    #: the popup rather than the control.
    FIELD_W = 340


    def _settings_form(self, route, size):
        """One tab's worth of the config form.

        Was ``_settings_general``, rendering every curated group in one
        scroll. The tab is read from the route rather than passed in because
        the dispatch table in ``settings/__init__`` maps three tabs onto this
        one renderer, and a per-tab wrapper would be three ways to get the
        same page slightly wrong.
        """
        cfg = self._config()
        schema = cfg.settings_schema()
        values = cfg.get_settings()
        show_adv = bool(route.get("_advanced"))
        # Once per render, not once per note: there are ~30 of them on the
        # Playback tab and each one would otherwise measure an 88-character
        # string of its own.
        note_w = self._note_w(size)
        query = (route.get("_q") or "").strip()
        if query:
            return self._search_results(cfg, schema, values, query, note_w)
        seen_advanced = False
        rows = []
        for title, keys in cfg.sections(route.get("_tab", "general")):
            # Membership, not the title itself: the disclosure used to be
            # a property of a group being *called* "Advanced", which capped
            # a tab at one and forced any tucked-away group to be named
            # that. See config.ADVANCED_GROUPS.
            advanced = title in getattr(cfg, "ADVANCED_GROUPS", ())
            if advanced:
                if not seen_advanced:
                    # One checkbox per tab, at the first advanced group:
                    # two would be two controls for one piece of state.
                    seen_advanced = True
                    rows.append(Checkbox(
                        _("Show advanced settings"), show_adv, id="set-adv",
                        on_toggle=lambda: self._toggle_advanced(route)))
                if not show_adv:
                    continue
            # The section-heading tier, as everywhere else in the app. At
            # "large" a settings group title was a size below every other
            # section title, which made the whole tab read as subordinate to
            # the pages that link to it.
            rows.append(Text(title, size="heading", bold=True))
            for key in keys:
                rows.extend(self._setting_rows(cfg, schema, values, key,
                                               note_w))
        # Two lines used to live here: a group footnote naming Interface
        # Scale, and a page footer saying "some changes take effect after
        # restarting". Both are gone -- `Interface Scale` now carries the
        # marker on its own row, and a footer that will not say *which*
        # changes is worse than nothing, because the only thing a reader can
        # do with it is distrust every control on the page.
        if route.get("_tab", "general") == "playback":
            # Playback only, and it is the same button the Logs tab carries
            # -- the folder itself is not playback-specific. What is, is the
            # reason to go there: `mpv.conf` lives in it, and several notes
            # on this tab end by telling you to put something in it
            # (display-fps-override, an hwdec pin). A settings page that
            # names a file and gives you no way to reach it is a page that
            # assumes you know where it keeps its config.
            rows.append(Row([Button(_("Open Config Folder"),
                                    id="set-open-config", icon="folder",
                                    on_click=self._open_config_folder)],
                            gap=8))
            rows.append(Text(
                _("Advanced MPV options go in mpv.conf, in this folder. "
                  "MPV reads it at startup and these settings are applied "
                  "afterwards, so most of them override it. Hardware "
                  "Decoding is the exception: set hwdec there and this app "
                  "leaves it alone."),
                size="caption", w=note_w,
                color=theme.SUBTLE_FG, wrap=True))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="settings", flex=1)
    def _setting_rows(self, cfg, schema, values, key, note_w):
        """One setting: its control, then whatever has to be said about it.

        Static note from the config module, AND one that depends on live
        state. Both, not either: `static or dynamic` meant giving a setting
        an explanatory line silently disabled its warning, which is how
        discord_presence shipped with a "not active" note that could never
        render.
        """
        rows = [self._setting_row(cfg, schema, values, key)]
        if key in getattr(cfg, "RESTART_REQUIRED", ()):
            # Above the explanatory note and kept to two words, because it
            # is read by position and length rather than by being read: the
            # eye finds a short line directly under a control. It replaces
            # the same sentence written into nine different notes in three
            # different phrasings, plus a footer that said "some changes"
            # without saying which.
            rows.append(Text(_("Requires restart"), size="caption",
                             color=theme.WARN_AMBER))
        notes = getattr(cfg, "NOTES", None) or {}
        for note in (notes.get(key), self._dynamic_note(key)):
            if note:
                # An explanatory line under the setting it belongs to; the
                # settings it qualifies follow directly below.
                rows.append(Text(note, size="caption", w=note_w,
                                 color=theme.SUBTLE_FG, wrap=True))
        return rows

    def _search_results(self, cfg, schema, values, query, note_w):
        """The form filtered to one query, across every config tab.

        Editable in place rather than a list of links. A result that only
        took you to the tab it lives on would make the search a slower way
        to do what the tab bar already does, and the reason people cannot
        find a setting is rarely that they cannot find the tab.

        The group heading is kept, with the tab named under it, because a
        flat list of controls loses the one piece of context that says what
        a setting is *for* -- "Deinterlace Automatically" under "Video
        Enhancement" reads differently from the same words on their own.
        """
        search = getattr(cfg, "search", None)
        groups = search(query) if search is not None else []
        rows = []
        if not groups:
            rows.append(Text(_('No settings match "%s".') % query,
                             size="normal", color=theme.SUBTLE_FG, wrap=True,
                             w=note_w))
            # Said here rather than in a note under the box, where it would
            # be permanent furniture for a case that is rare. The two things
            # a fruitless search most often means are a tab this does not
            # cover and a control the form is currently hiding.
            rows.append(Text(
                _("Only the General, Browse and Playback tabs are searched. "
                  "Some settings are hidden until the setting they depend "
                  "on is turned on."),
                size="caption", color=theme.SUBTLE_FG, wrap=True, w=note_w))
            return self._search_scroll(rows)
        labels = self.tab_labels()
        found = 0
        for tab, title, keys in groups:
            rows.append(Text(title, size="heading", bold=True))
            rows.append(Text(labels.get(tab, tab), size="caption",
                             color=theme.SUBTLE_FG))
            for key in keys:
                rows.extend(self._setting_rows(cfg, schema, values, key,
                                               note_w))
            found += len(keys)
        rows.append(Text(
            # "Matches: 3." rather than "3 matching settings", which needs
            # a plural form this codebase has no ngettext for -- and a
            # translator given only the singular writes the singular.
            _("Matches: %d. Pick a tab above to stop searching.") % found,
            size="caption", color=theme.SUBTLE_FG))
        return self._search_scroll(rows)

    def _search_scroll(self, rows):
        """Its own scroll region, not the tab form's.

        A separate id so the two keep separate offsets. Sharing "settings"
        meant a search run from halfway down the Playback tab opened its
        results halfway down as well -- and going back to the tab landed
        wherever the results had been left.
        """
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"), id="settings-search", flex=1)

    def _setting_row(self, cfg, schema, values, key):
        kind = schema.get(key, "str")
        val = values.get(key)
        label = cfg.label_for(key)
        if kind == "bool":
            return Checkbox(label, bool(val), id="set-" + key,
                            on_toggle=lambda k=key, v=val: self._set_setting(
                                k, not bool(v)))
        dynamic = self._dynamic_enum(key)
        opts = cfg.LABELED_ENUMS.get(key) or dynamic
        if opts:
            cur = next((i for i, (_l, v) in enumerate(opts)
                        if str(v) == str(val)), 0)
            # A curated enum has labels we wrote, so FIELD_W is a width we
            # chose. A dynamic one is system strings of unknown length --
            # audio device descriptions run to "SoundBlaster Live! 24-bit
            # External SB0490 Digital Stereo (IEC958)", and the part that
            # identifies the device is the END, so at FIELD_W every row
            # ellipsizes to the same thing. The OPEN list gets the extra room
            # rather than the control: one field wider than every other field
            # in the form is what you notice, and it is closed most of the
            # time.
            # Only the device list needs it: theme names are short, and a
            # popup wider than the control it drops from is what you notice.
            extra = {"popup_w": int(self.FIELD_W * 1.5)} \
                if key == "audio_device" and dynamic else {}
            widget = Dropdown(
                "set-" + key, [lbl for lbl, _v in opts], selected=cur,
                w=self.FIELD_W, force=True,
                on_select=lambda i, _v, k=key, o=opts: self._set_setting(
                    k, o[i][1]),
                **extra)
        elif key in cfg.ENUMS:
            opts = cfg.ENUMS[key]
            cur = opts.index(str(val)) if str(val) in opts else 0
            widget = Dropdown(
                "set-" + key, opts, selected=cur, w=self.FIELD_W,
                force=True,
                on_select=lambda i, _v, k=key, o=opts: self._set_setting(
                    k, o[i]))
        elif key == "sync_path":
            widget = Row([
                TextBox("set-" + key, text="" if val is None else str(val),
                        w=250,
                        on_change=lambda v: self._sync_path.__setitem__(
                            "path", v),
                        on_submit=lambda v: self._move_downloads(v)),
                # Moves what is in the field. It used to pass None, whose
                # only effect was a status line telling you to press Enter
                # — a button that could never do its own job.
                Button(_("Move"), id="set-sync-move",
                       on_click=lambda: self._move_downloads(
                           self._sync_path.get("path") or val)),
            ], gap=8, align="center")
        else:
            # on_commit as well as on_submit: ENTER is not the only way people
            # leave a field. Wired only here, so typing then clicking the next
            # row silently threw the edit away on 65 rows, with no toast and
            # no dirty marker. The sync_path row above already had a Move
            # button for the same reason; this generalizes it.
            widget = TextBox("set-" + key,
                             text="" if val is None else str(val),
                             w=self.FIELD_W,
                             on_submit=lambda v, k=key: self._set_setting(k, v),
                             on_commit=lambda v, k=key: self._set_setting(k, v))
        return Row([Text(label, w=self.FIELD_W, size="normal",
                         color=theme.SUBTLE_FG),
                    widget], gap=12, align="center")
    def _dynamic_enum(self, key):
        """``[(label, value), ...]`` for a setting whose choices are not
        knowable in advance, or None.

        ``LABELED_ENUMS`` in config.py is a literal, which is right for the
        settings whose options are a design decision. The audio device list
        is not one of those: it depends on the platform, the sound server and
        what is plugged in this minute, and mpv — the thing that will have to
        open the chosen device — is the only honest source for it.

        Themes are the same shape of answer for a different reason: they are
        JSON files now, and the user can drop their own into the config
        directory, so a literal list would only ever show the shipped ones.

        Read from the cache, not re-scanned: this runs on every rebuild of the
        form — which is every keystroke in any text field on it — and a theme
        only takes effect after a restart anyway, so re-reading the directory
        here would buy nothing for a directory listing and a JSON parse per
        theme per frame.
        """
        if key == "theme":
            try:
                from .. import themes

                return themes.choices()
            except Exception:
                log.debug("could not list themes", exc_info=True)
                return None
        if key != "audio_device" or self.controller is None:
            return None
        try:
            return self.controller.audio_devices()
        except Exception:
            log.debug("could not list audio devices", exc_info=True)
            return None

    def _dynamic_note(self, key):
        """Explanatory line that depends on live state rather than the key.

        NOTES in config.py is a static dict, but auto-download's scope is
        "the server you turned it on for", which only the browser knows.
        Naming it is what stops the setting reading as global.
        """
        if key == "allow_background":
            # Only once it is on: while it is off, closing the window still
            # exits, and telling someone how to stop an app that stops
            # normally is noise. When it is on this is the only exit there is.
            if not settings.allow_background:
                return None
            return _("To stop the application, re-launch and uncheck this "
                     "option or run `jellyfin-mpv-shim stop`.")
        if key == "discord_presence":
            # Only while it is on and not working. Ticking the box with
            # pypresence missing did nothing whatsoever and said nothing
            # either -- the same shape of failure as the pause guard: the
            # feature is off and there is no way to tell from the UI.
            if not settings.discord_presence or self.controller is None:
                return None
            try:
                if self.controller.rich_presence_available():
                    return None
            except Exception:
                return None
            return _("Not active: the \"pypresence\" package is missing or "
                     "failed to load. Install it and restart. (Details in "
                     "the Logs tab.)")
        if key == "hwdec":
            # A pin, not a preference: where mpv.conf sets hwdec, nothing
            # here writes the option at all, so the control above is inert
            # and has to say so. Silently ignoring a setting the user is
            # looking straight at is the failure this whole feature is
            # downstream of.
            from ...mpv_options import hwdec_pinned_by_config

            pinned = hwdec_pinned_by_config()
            if pinned is None:
                return None
            return _("Pinned by config: your mpv.conf sets hwdec=%s, which "
                     "overrides this.") % pinned
        if key != "auto_download_enable":
            return None
        name = self._auto_dl_scope_name()
        if not name:
            return None
        return _("Applies to %s, enable other servers in servers tab.") % name
    def _toggle_advanced(self, route):
        route["_advanced"] = not route.get("_advanced")
        self.invalidate()
    def _move_downloads(self, path, confirmed=False):
        """Relocating the download store copies files (possibly across
        drives), so it runs on its own thread — not the pool, whose four
        workers serve every route load — and reports progress into the
        status line.

        An empty path means "go back to the default location". That is a real
        thing to want, but it used to happen *silently*: clearing the field
        and pressing Enter relocated the whole store with no confirmation and
        no indication that is what an empty box meant. It asks first now, like
        every other destructive download action."""
        if path is not None and not str(path).strip():
            path = None
        if path is None:
            if not confirmed:
                self._confirm(
                    _("Move the downloads back to the default folder?"),
                    lambda: self._move_downloads(None, confirmed=True),
                    title=_("Use the default folder"), yes=_("Move"))
                return
        cfg = self._config()
        if not hasattr(cfg, "relocate_downloads"):
            self._set_setting("sync_path", path)
            return

        def work():
            def progress(copied, total):
                pct = 100 if not total else min(100, int(copied * 100 / total))
                self.set_status(_("Moving downloads… %d%%") % pct)
                self.invalidate()
            try:
                ok, message = cfg.relocate_downloads(path or "",
                                                     progress=progress)
            except Exception:
                log.error("download folder move failed", exc_info=True)
                ok, message = False, _("Moving the downloads failed.")
            self.set_status(message or (
                _("Download folder moved. Restart to finish switching.")
                if ok else _("Moving the downloads failed.")))

        # Set before starting, so the job's own progress line wins the race.
        self.set_status(_("Moving downloads…"))
        if not self._run_long(work, "mpvtk-move-downloads"):
            # Two concurrent copies of the same store would fight. Say so —
            # a second press that silently did nothing reads as a dead button.
            self.set_status(_("A move is already in progress."))
        self.invalidate()
    def _auto_dl_scope_name(self):
        """Display name of the server auto-download is scoped to.

        The stored allow-list wins over the currently-selected server: after
        unticking servers elsewhere, the note has to describe what is
        configured, not what happens to be on screen.
        """
        picked = self._auto_dl_servers()
        try:
            servers = self.controller.list_servers() if self.controller else []
        except Exception:
            log.debug("list_servers failed", exc_info=True)
            servers = []
        names = {sv.get("uuid"): sv.get("name") for sv in servers}
        if picked:
            chosen = [names.get(u) or u for u in picked]
            if len(chosen) == 1:
                return chosen[0]
            # Plural: the note's "enable other servers" advice still applies,
            # so keep the shape and just list them.
            return ", ".join(sorted(chosen))
        # Not yet seeded — name the server the toggle is about to claim.
        return names.get(self.server) or (str(self.server) if self.server
                                          else None)
    def _seed_auto_download_server(self):
        """Switching auto-download on means "for the server I am looking at".

        The allow-list is empty by default and empty means none, so without
        this the feature would switch on and do nothing. Only ever seeds an
        empty list: re-enabling after a deliberate change must not silently
        re-add a server the user unticked.
        """
        cfg = self._config()
        if (cfg.get_settings().get("auto_download_servers") or "").strip():
            return
        if not self.server:
            return
        cfg.set_setting("auto_download_servers", str(self.server))
    def _apply_work_offline(self, offline):
        """Swap the data source when the setting is toggled, rather than
        persisting a key that does nothing until the next launch. Tk
        applies it live too."""
        if self.controller is None or offline == self._offline:
            return

        ep = self._epoch

        def work():
            if offline:
                return self.controller.offline_source()
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is None:
                self.set_status(_("Nothing downloaded to browse offline.")
                                if offline else
                                _("Could not reach a server."))
                return
            self.set_source(source)
        self.run_async(work, done, ep)
    def _apply_audio_settings(self):
        """Audio settings apply live -- a mode change takes effect without a
        restart, and mid-playback without a reload."""
        # Through the gateway, not playerManager directly: a settings page
        # must be constructible without player.py (see
        # tests/test_source_invariants.py).
        self._safe(lambda c: c.apply_audio_settings())
