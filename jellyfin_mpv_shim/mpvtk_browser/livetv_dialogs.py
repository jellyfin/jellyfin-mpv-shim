"""The two Live TV modals: the recording editor and the guide settings.

Its own mixin rather than more of ``dialogs.py``, which is already the
shell's largest: these two are a self-contained feature and share nothing
with the add-to picker or the download dialog beyond ``_show_dialog``.

State on ``self``: ``_timer_dlg`` and ``_guide_dlg``, each a dict or None,
loop-thread only — the same single-slot convention the download dialog uses.

**Both edit somebody else's document.** The guide settings live in the
DisplayPreferences ``CustomPrefs`` jellyfin-web reads (see ``live_tv``), and
a timer belongs to the server's DVR. So neither dialog writes anything until
Save, and both re-read rather than assuming their edit landed.
"""

from ..i18n import _, _p
from ..mpvtk.widgets import (
    Button, Checkbox, Column, Dialog, Dropdown, Row, Text, TextBox, VScroll)
from . import live_tv, theme

#: Keep-up-to choices (0 = as many as possible). jellyfin-web offers every
#: integer from 0 to 50; a 51-row dropdown does not fit over a 720p window,
#: so this is the round numbers — plus, at open time, whatever the rule is
#: actually set to (see :func:`_keep_choices`). Without that last part a
#: series set to 12 in the web client read back as "As many as possible",
#: because an unlisted value has no row to select.
KEEP_UP_TO = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 25, 30, 40, 50)


def _keep_choices(current):
    """The keep-up-to values to offer, always including ``current``."""
    values = set(KEEP_UP_TO)
    try:
        values.add(max(0, int(current or 0)))
    except (TypeError, ValueError):
        pass
    return tuple(sorted(values))


def _minutes(seconds):
    try:
        return str(int(seconds or 0) // 60)
    except (TypeError, ValueError):
        return "0"


def _int(value, fallback=0):
    """A DTO number as an int, tolerantly.

    Everything read out of a timer goes through one of these. A dialog whose
    *seeding* can raise is worse than it looks: ``_show_timer_editor``
    installs a builder that runs every frame, so a half-built form throws
    KeyError out of the render loop on every repaint and cannot be
    dismissed.
    """
    try:
        return int(value or fallback)
    except (TypeError, ValueError):
        return fallback


def _seconds(text, fallback=0):
    """Minutes typed into a padding box, as seconds.

    Anything unparsable keeps the value the timer already had rather than
    silently becoming zero — a stray keystroke must not drop the user's
    two-minute pre-roll.
    """
    try:
        return max(0, int(str(text).strip())) * 60
    except (TypeError, ValueError):
        return fallback


class LiveTvDialogsMixin:

    # ------------------------------------------------------ recording editor

    def _open_timer_editor(self, server, timer, series=False, on_change=None):
        """Edit one timer or series rule.

        ``timer`` may be a full DTO (the Schedule tab has one) or just an id
        (the program page knows only ``SeriesTimerId``); either way the
        authoritative copy is fetched, because a stale DTO would be written
        straight back over whatever changed in between.
        """
        timer_id = (timer or {}).get("Id")
        if not timer_id or server is None:
            return
        self._timer_dlg = {"server": server, "id": timer_id,
                           "series": series, "timer": None,
                           "on_change": on_change, "error": None,
                           "fields": {}}
        ep = self._epoch
        source = self.source

        def work():
            if series:
                return source.get_series_timer(server, timer_id)
            return source.get_timer(server, timer_id)

        def done(dto):
            dlg = self._timer_dlg
            if dlg is None or dlg["id"] != timer_id:
                return           # the dialog was closed or replaced
            # Fields first: "timer is not None" is what tells the builder
            # the form is ready, so setting it before the fields exist would
            # publish a half-built dialog if this raised.
            fields = self._timer_fields(dto or {}, series)
            dlg["fields"] = fields
            dlg["timer"] = dto or {}
            self._show_timer_editor()

        def failed(_exc):
            dlg = self._timer_dlg
            if dlg is None or dlg["id"] != timer_id:
                return
            dlg["error"] = _("The recording could not be read.")
            self._show_timer_editor()

        self.run_async(work, done, ep, on_error=failed)
        self._show_timer_editor()     # open immediately, in a loading state

    @staticmethod
    def _timer_fields(dto, series):
        """The dialog's editable state, seeded from the DTO.

        Held apart from the DTO so Cancel is free and Save can send only
        what the form owns — the rest of the timer (channel, times, priority)
        round-trips untouched through the gateway's read-modify-write.
        """
        fields = {"pre": _minutes(dto.get("PrePaddingSeconds")),
                  "post": _minutes(dto.get("PostPaddingSeconds"))}
        if series:
            fields.update({
                "new_only": bool(dto.get("RecordNewOnly")),
                "skip_in_library": bool(dto.get("SkipEpisodesInLibrary")),
                "any_channel": bool(dto.get("RecordAnyChannel")),
                "any_time": bool(dto.get("RecordAnyTime")),
                "keep_up_to": _int(dto.get("KeepUpTo")),
            })
        return fields

    def _timer_set(self, key, value):
        if self._timer_dlg is not None:
            self._timer_dlg["fields"][key] = value
            self._show_timer_editor()

    def _show_timer_editor(self):
        dlg = self._timer_dlg
        if dlg is None:
            return

        def build():
            timer, fields = dlg["timer"], dlg["fields"]
            if dlg.get("error"):
                body = [Text(dlg["error"], size=15, color=theme.FAV_RED)]
            elif timer is None:
                body = [Text(_("Loading…"), size=15, color=theme.SUBTLE_FG)]
            else:
                body = self._timer_form(timer, fields, dlg["series"])
            title = _("Series Options") if dlg["series"] else _("Recording")
            buttons = [Button(_("Close"), id="tm-close",
                              on_click=self._close_timer_editor)]
            if timer is not None and not dlg.get("error"):
                buttons.insert(0, Button(
                    _("Cancel Series") if dlg["series"]
                    else _("Cancel Recording"), id="tm-cancel",
                    on_click=self._timer_cancel))
                buttons.append(Button(_("Save"), id="tm-save",
                                      on_click=self._timer_save))
            return Dialog("timer", self._dialog_shell("timer", [
                Text(title, size=22, bold=True),
                Text((timer or {}).get("Name") or "", size=17),
                VScroll(Column(body, gap=12, align="stretch"), id="tm-body",
                        h=self._timer_body_h(dlg)),
                self._dialog_buttons(buttons),
            ], w=520), on_dismiss=self._close_timer_editor)
        self._show_dialog(build)

    @staticmethod
    def _timer_body_h(dlg):
        """Height of the scrolling form area.

        Fixed rather than measured: the series form is six controls tall and
        would push the dialog past a 720p window, and a dialog whose buttons
        are off-screen cannot be dismissed with the mouse.
        """
        return 260 if dlg.get("series") else 120

    def _timer_form(self, timer, fields, series):
        rows = []
        when = live_tv.air_time_label(timer)
        channel = timer.get("ChannelName") or ""
        subtitle = "   ·   ".join(p for p in (channel, when) if p)
        if subtitle:
            rows.append(Text(subtitle, size=15, color=theme.SUBTLE_FG))
        if series:
            rows.append(self._picker(
                # A LABEL on a picker ("Record: New episodes only"), not the
                # imperative on the button two screens away. Filipino
                # translates the two differently; jellyfin-web needs
                # LabelRecord and Record to say so.
                _p("series recording rule setting", "Record"), "tm-showtype",
                [_("New episodes only"), _("All episodes")],
                0 if fields["new_only"] else 1,
                lambda i, v: self._timer_set("new_only", i == 0)))
            rows.append(Checkbox(
                _("Skip episodes already in my library"),
                fields["skip_in_library"], id="tm-skip",
                on_toggle=lambda: self._timer_set(
                    "skip_in_library", not fields["skip_in_library"])))
            one_channel = (_("Only %s") % channel if channel
                           else _("One channel"))
            rows.append(self._picker(
                # Likewise a label, not the Live TV tab of the same name.
                _p("series recording rule setting", "Channels"),
                "tm-channels",
                [one_channel, _("All channels")],
                1 if fields["any_channel"] else 0,
                lambda i, v: self._timer_set("any_channel", i == 1)))
            start = live_tv.parse_time(timer.get("StartDate"))
            rows.append(self._picker(
                _("Air time"), "tm-airtime",
                [(_("Around %s") % live_tv.fmt_time(start)) if start
                 else _("Original air time"), _("Any time")],
                1 if fields["any_time"] else 0,
                lambda i, v: self._timer_set("any_time", i == 1)))
            keep = fields["keep_up_to"]
            choices = _keep_choices(keep)
            rows.append(self._picker(
                _("Keep up to"), "tm-keep",
                [self._keep_label(n) for n in choices],
                choices.index(keep) if keep in choices else 0,
                lambda i, v, c=choices: self._timer_set("keep_up_to", c[i])))
        rows.append(self._padding_row(
            _("Start early (minutes)"), "tm-pre", fields["pre"],
            lambda s: self._timer_set("pre", s)))
        rows.append(self._padding_row(
            _("Stop late (minutes)"), "tm-post", fields["post"],
            lambda s: self._timer_set("post", s)))
        return rows

    @staticmethod
    def _keep_label(count):
        if count == 0:
            return _("As many as possible")
        if count == 1:
            return _("1 episode")
        return _("%d episodes") % count

    @staticmethod
    def _picker(label, node_id, items, selected, on_select):
        return Row([Text(label, w=170, size=16, color=theme.SUBTLE_FG),
                    Dropdown(node_id, items, selected=selected, w=280,
                             size=16, on_select=on_select)],
                   gap=8, align="center")

    @staticmethod
    def _padding_row(label, node_id, value, on_change):
        # on_commit as well as on_submit: a padding typed and then dismissed
        # with a click on Save (rather than ENTER) must still be read.
        return Row([Text(label, w=170, size=16, color=theme.SUBTLE_FG),
                    TextBox(node_id, value, size=16, w=90,
                            on_submit=on_change, on_commit=on_change)],
                   gap=8, align="center")

    def _timer_save(self):
        dlg = self._timer_dlg
        if dlg is None or dlg["timer"] is None:
            return
        timer, fields = dlg["timer"], dlg["fields"]
        changes = {
            "PrePaddingSeconds": _seconds(fields["pre"],
                                          timer.get("PrePaddingSeconds") or 0),
            "PostPaddingSeconds": _seconds(
                fields["post"], timer.get("PostPaddingSeconds") or 0),
        }
        if dlg["series"]:
            changes.update({
                "RecordNewOnly": bool(fields["new_only"]),
                "SkipEpisodesInLibrary": bool(fields["skip_in_library"]),
                "RecordAnyChannel": bool(fields["any_channel"]),
                "RecordAnyTime": bool(fields["any_time"]),
                "KeepUpTo": int(fields["keep_up_to"]),
            })
        on_change = dlg["on_change"]
        server, timer_id, series = dlg["server"], dlg["id"], dlg["series"]
        self._close_timer_editor()
        self._actions.save_timer(server, timer_id, changes, series=series,
                                 on_done=on_change)

    def _timer_cancel(self):
        dlg = self._timer_dlg
        if dlg is None:
            return
        server, timer_id, series = dlg["server"], dlg["id"], dlg["series"]
        on_change = dlg["on_change"]
        # Closed first: the confirmation is itself a dialog, and there is one
        # modal slot — leaving this open would have the confirm replace it
        # and the editor never come back.
        self._close_timer_editor()
        if series:
            self._actions.cancel_series_timer(timer_id, server,
                                              on_done=on_change)
        else:
            self._actions.cancel_timer(timer_id, server, on_done=on_change)

    def _close_timer_editor(self):
        self._timer_dlg = None
        self._close_dialog()

    # ------------------------------------------------------- guide settings

    def _open_guide_settings(self, server, prefs, categories, on_save=None):
        """The guide's view settings: channel order, indicators, categories.

        ``prefs`` is the resolved dict the page already holds, so this opens
        instantly — there is nothing to fetch. ``categories`` is session
        state (see ``LiveTvPage._categories``) and is handed back to the
        caller rather than saved.
        """
        if server is None:
            return
        self._guide_dlg = {
            "server": server,
            "prefs": {"order": (prefs or {}).get("order",
                                                 live_tv.ORDER_NUMBER),
                      "favorites_first": bool((prefs or {}).get(
                          "favorites_first", True)),
                      "color_coded": bool((prefs or {}).get("color_coded")),
                      "indicators": dict((prefs or {}).get("indicators")
                                         or live_tv.INDICATOR_DEFAULTS)},
            "categories": set(categories or ()),
            "on_save": on_save,
        }
        self._show_guide_settings()

    def _guide_set(self, key, value):
        if self._guide_dlg is not None:
            self._guide_dlg["prefs"][key] = value
            self._show_guide_settings()

    def _guide_toggle_indicator(self, key):
        dlg = self._guide_dlg
        if dlg is None:
            return
        indicators = dlg["prefs"]["indicators"]
        indicators[key] = not indicators.get(key)
        self._show_guide_settings()

    def _guide_toggle_category(self, key):
        dlg = self._guide_dlg
        if dlg is None:
            return
        # Empty means "everything", so the checkboxes show all four ticked
        # when nothing is selected; un-ticking one from that state has to
        # start from all four rather than from the empty set.
        picked = dlg["categories"] or set(live_tv.CATEGORIES)
        picked.symmetric_difference_update({key})
        dlg["categories"] = picked
        self._show_guide_settings()

    def _show_guide_settings(self):
        dlg = self._guide_dlg
        if dlg is None:
            return

        def build():
            prefs = dlg["prefs"]
            picked = dlg["categories"] or set(live_tv.CATEGORIES)
            body = [
                self._picker(
                    _("Sort channels by"), "gs-order",
                    [_("Channel number"), _("Recently watched")],
                    1 if prefs["order"] == live_tv.ORDER_DATE_PLAYED else 0,
                    lambda i, v: self._guide_set(
                        "order", live_tv.ORDER_DATE_PLAYED if i
                        else live_tv.ORDER_NUMBER)),
                Checkbox(_("Place favorite channels first"),
                         prefs["favorites_first"], id="gs-fav",
                         on_toggle=lambda: self._guide_set(
                             "favorites_first",
                             not prefs["favorites_first"])),
                Checkbox(_("Color-coded backgrounds"), prefs["color_coded"],
                         id="gs-color",
                         on_toggle=lambda: self._guide_set(
                             "color_coded", not prefs["color_coded"])),
                Text(_("Show indicators for"), size=16, bold=True),
            ]
            body += [
                Checkbox(label, bool(prefs["indicators"].get(key)),
                         id="gs-ind-" + key,
                         on_toggle=lambda k=key:
                             self._guide_toggle_indicator(k))
                for key, label in live_tv.indicator_labels()
            ]
            body.append(Text(_("Categories"), size=16, bold=True))
            body += [
                Checkbox(label, key in picked, id="gs-cat-" + key,
                         on_toggle=lambda k=key:
                             self._guide_toggle_category(k))
                for key, label in live_tv.category_labels()
            ]
            return Dialog("guidecfg", self._dialog_shell("guidecfg", [
                Text(_("Guide Settings"), size=22, bold=True),
                VScroll(Column(body, gap=12, align="stretch"), id="gs-body",
                        h=340),
                self._dialog_buttons([
                    Button(_("Cancel"), id="gs-cancel",
                           on_click=self._close_guide_settings),
                    Button(_("Save"), id="gs-save",
                           on_click=self._guide_save)]),
            ], w=520), on_dismiss=self._close_guide_settings)
        self._show_dialog(build)

    def _guide_save(self):
        dlg = self._guide_dlg
        if dlg is None:
            return
        server, prefs = dlg["server"], dict(dlg["prefs"])
        # All four ticked is stored as "no filter" — see live_tv.category_kwargs.
        categories = set(dlg["categories"])
        if categories == set(live_tv.CATEGORIES):
            categories = set()
        on_save = dlg["on_save"]
        self._close_guide_settings()
        ep = self._epoch
        source = self.source
        # Adopt the new prefs HERE, on the loop thread, before anything is
        # reloaded. ``on_save`` repaints the guide, which re-fetches it, and
        # that fetch reads the prefs cache — from a pool worker submitted
        # before the save below. Doing this inside the save worker instead
        # loses the race however early in that worker it happens, and the
        # guide comes back drawn with the settings just changed away from.
        source.cache_live_tv_prefs(server, prefs)
        # Applied to the screen first and persisted in the background: these
        # are view settings, and making the guide wait on a DisplayPreferences
        # round trip to redraw would be a second of nothing happening.
        if on_save is not None:
            on_save(prefs, sorted(categories))

        def work():
            source.save_live_tv_prefs(server, prefs)

        def failed(_exc):
            self.set_status(_("Guide settings could not be saved."))
            self.invalidate()

        self.run_async(work, lambda _r: None, ep, on_error=failed)

    def _close_guide_settings(self):
        self._guide_dlg = None
        self._close_dialog()
