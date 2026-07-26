"""The Logs tab: a live tail of the log file.

``_poll_logs`` is the same shape as the downloads poller -- daemon thread,
write then invalidate, exits on leaving the tab.
"""

import logging

from ...i18n import _
from ...mpvtk.widgets import (
    Button,
    Column,
    Row,
    Spacer,
    Table,
    Text,
    VScroll,
)
from .. import theme

log = logging.getLogger("mpvtk_browser.settings")


class LogsTabMixin:

    def _settings_logs(self, route, size):
        lines = []
        if self.controller is not None:
            try:
                lines = self.controller.recent_logs()
            except Exception:
                log.debug("recent_logs failed", exc_info=True)
        # Remember what we have drawn so the poller can tell whether a tick
        # actually changed anything (see _poll_logs).
        route["_log_len"] = len(lines)
        route["_log_last"] = lines[-1] if lines else None
        self._poll_logs(route)

        head = Row([Text(_("Logs"), size=20, bold=True), Spacer(),
                    Button(_("Copy"), id="log-copy", icon="content_copy",
                           on_click=lambda: self._copy_logs(lines)),
                    Button(_("Refresh"), id="log-refresh", icon="refresh",
                           on_click=self.invalidate),
                    Button(_("Open Config Folder"), id="log-conf",
                           icon="folder",
                           on_click=self._open_config_folder)],
                   gap=8, align="center", pad=self.CONTENT_PAD)
        if not lines:
            return Column([head,
                           Column([Text(_("No log output captured yet."),
                                        size=15, color=theme.SUBTLE_FG)],
                                  pad=self.CONTENT_PAD)],
                          flex=1, align="stretch")

        # Newest last, like a console. `follow` keeps the view pinned to the
        # newest line as lines arrive, and unpins the moment the user
        # scrolls up to read something — the renderer decides, because it is
        # the only side that knows the offset and the content height at the
        # same instant.
        rows = [{"cells": [line], "id": "log-%d" % i}
                for i, line in enumerate(lines)]
        virtual = {"offset": self._offset("settings-logs"),
                   "height": float(size[1])}
        table = Table([{"flex": 1}], rows, row_h=self.LOG_ROW_H, header_h=0,
                      size=14, fg=theme.SUBTLE_FG, virtual=virtual)
        return Column([
            head,
            VScroll(Column([table], pad=self.CONTENT_PAD),
                    id="settings-logs", flex=1, follow=True,
                    on_scroll=lambda off, mx: self._on_scroll(
                        "settings-logs", off, mx)),
        ], flex=1, align="stretch")
    def _poll_logs(self, route):
        """Re-render the logs tab while new lines are arriving.

        Only when something changed: an idle app logs nothing for minutes at
        a time, and rebuilding a 2000-row scene every second to draw the
        same thing would cost real frames for nothing.
        """
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(self.LOG_POLL_SECS):
                if (self.route is not route
                        or route.get("_tab") != "logs"
                        or not self._browsing):
                    break
                try:
                    lines = self.controller.recent_logs()
                except Exception:
                    break
                # Length alone is not enough: the ring is bounded, so once
                # it is full every new line also drops one and the count
                # stops moving. Compare the newest line too.
                last = lines[-1] if lines else None
                if (len(lines) != route.get("_log_len")
                        or last != route.get("_log_last")):
                    self.invalidate()

        self._start_daemon("_log_thread", "mpvtk-log-tail", tick,
                           restartable=True)
    def _copy_logs(self, lines):
        """Put the captured log on the clipboard.

        Copies *everything* the ring holds, not the 500 lines the view draws
        — the point is to hand the whole thing to someone else. Falls back to
        writing a file when there is no clipboard at all (a headless box, or
        one with none of wl-copy/xclip/xsel), because a button that silently
        does nothing is worse than one that tells you where it put the text.
        """
        if self.controller is None or not lines:
            self.set_status(_("There is nothing to copy yet."))
            self.invalidate()
            return
        text = "\n".join(lines)

        def work():
            return self.controller.copy_text(text)

        def done(res):
            ok, _method, path = res
            if not ok:
                self.set_status(_("Could not copy the log."))
            elif path:
                self.set_status(_("No clipboard available — saved to %s")
                                % path)
            else:
                self.set_status(_("Copied %d log lines.") % len(lines))

        def failed(_exc):
            self.set_status(_("Could not copy the log."))

        # Off the loop thread: a clipboard helper is a subprocess, and on a
        # wedged one the 10s timeout would otherwise freeze the UI.
        self.run_async(work, done, self._epoch, on_error=failed)
    def _open_config_folder(self):
        self._client_call(lambda c: c.open_config_folder())
    # How often the logs tab re-reads the ring while it is on screen. The
    # Tk browser got a push per line; there is no such channel in-process,
    # so poll — cheaply, since a tick that finds nothing new does not
    # re-render.
    LOG_POLL_SECS = 1.0
    # One line per row. Fixed height is what makes the list virtualizable,
    # and virtualization is what lets it show the whole 2000-line ring
    # rather than the last 500.
    LOG_ROW_H = 20
