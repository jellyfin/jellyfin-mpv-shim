"""What every settings tab shares.

The config store (``_config`` / ``_set_setting``) and the one layout helper
more than one tab uses. Everything else lives with the tab that draws it.

``_set_setting`` is the single write path, and it is where a setting's side
effects are dispatched -- applying audio config, switching to offline mode,
seeding the auto-download server. Those live with the general tab because
that is where the controls are; they are declared below rather than imported,
since it is the composed ``SettingsMixin`` that makes the call valid.
"""

import logging
from typing import TYPE_CHECKING

from ...conf import settings
from ...i18n import _
from ...mpvtk.widgets import (
    Column,
    Text,
)
from .. import theme

log = logging.getLogger("mpvtk_browser.settings")


class SettingsBase:

    if TYPE_CHECKING:
        # Side effects of a setting write, provided by GeneralTabMixin on the
        # composed SettingsMixin. Three, and the list is the coupling: if it
        # grows, the store is turning back into the form that draws it.
        def _apply_audio_settings(self) -> None: ...

        def _apply_work_offline(self, enabled) -> None: ...

        def _seed_auto_download_server(self) -> None: ...

    def _config(self):
        if self._config_obj is not None:
            return self._config_obj
        from .. import config as cfg
        return cfg
    def _set_setting(self, key, value):
        cfg = self._config()
        ok = cfg.set_setting(key, value)
        self.set_status((_("Saved: %s") if ok else _("Invalid value: %s"))
                        % key)
        # getattr: _config_obj may be a stand-in without the table, and a
        # missing one just means nothing to cascade.
        if ok and not value and key in getattr(cfg, "TRAY_DEPENDENT", ()):
            # start_minimized is about to disappear from the form (it is only
            # offered while this one is on), and leaving it set would leave a
            # setting still acting at every startup that nobody can see to
            # undo. Say so rather than resetting it silently.
            if settings.start_minimized and cfg.set_setting(
                    "start_minimized", False):
                self.set_status(_("Saved: %s (also turned off \"%s\")")
                                % (key, cfg.label_for("start_minimized")))
        if ok and key == "work_offline":
            self._apply_work_offline(bool(value))
        if ok and key == "auto_download_enable" and value:
            self._seed_auto_download_server()
        if ok and key.startswith("audio_"):
            self._apply_audio_settings()
        if ok and key in ("scroll_wheel_pixels", "snapped_scrolling"):
            # Wheel step / snapped toggle apply live -- the renderer just
            # re-derives, no reload needed.
            if self.app is not None and hasattr(self.app, "push_scroll_config"):
                self.app.push_scroll_config()
        if ok and key == "theme":
            # Colours apply immediately; sizes still need a restart, which is
            # what set_theme's docstring explains and what NOTES tells the
            # user. getattr because this mixin is also exercised against a
            # config stand-in that is not the browser.
            setter = getattr(self, "set_theme", None)
            if setter is not None:
                try:
                    setter(value)
                except Exception:
                    log.exception("could not switch theme live")
        self.invalidate()
    def _section(self, title, children, subtitle=None):
        """A full-width titled card. Settings panels are forms, not tile
        grids — they should span the pane rather than sit in a ragged
        left-aligned column."""
        head = [Text(title, size=20, bold=True)]
        if subtitle:
            head.append(Text(subtitle, size=14, color=theme.SUBTLE_FG,
                             wrap=True))
        return Column(head + children, pad=14, gap=8, bg=theme.CARD_BG,
                      radius=10, align="stretch")
    INDENT = 26   # per hierarchy level in the downloads tree
