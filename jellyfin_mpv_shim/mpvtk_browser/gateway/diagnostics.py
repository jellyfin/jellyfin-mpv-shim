"""Logs, config paths, the clipboard, update checks.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging
import os
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.diagnostics")


class DiagnosticsMixin(GatewayCore):
    def open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            log.error("could not open url %s", url, exc_info=True)

    def check_updates(self):
        """One-shot update check at startup.

        Without it a GUI user only ever saw the update notice after starting
        playback, because that was the only thing driving the check."""
        from ...player import playerManager
        try:
            playerManager.update_check.check()
        except Exception:
            log.debug("startup update check failed", exc_info=True)

    def recent_logs(self):
        from ...log_utils import recent_log_lines
        return recent_log_lines()

    def config_dir(self):
        """The config directory, for messages and for the copy-to-file
        fallback."""
        from ... import conffile
        from ...constants import APP_NAME
        return os.path.dirname(conffile.get(APP_NAME, "conf.json"))

    def copy_text(self, text):
        """Copy to the system clipboard, falling back to a file.

        Returns ``(ok, method, path)``. mpv is offered first: it is
        in-process, so a box with none of wl-copy/xclip/xsel installed still
        works. See jellyfin_mpv_shim.clipboard."""
        from ...clipboard import copy_or_save
        player = None
        try:
            from ...player import playerManager
            player = playerManager._player
        except Exception:
            log.debug("no mpv handle for the clipboard", exc_info=True)
        return copy_or_save(
            text, os.path.join(self.config_dir(), "copied-logs.txt"),
            player=player)

    def open_config_folder(self):
        """Reveal the config directory. The tray menu used to be the only way
        to reach it, and the mpvtk browser has no tray."""
        import subprocess
        import sys

        from ... import conffile
        from ...constants import APP_NAME

        path = os.path.dirname(conffile.get(APP_NAME, "conf.json"))
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - documented Windows API
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            log.error("could not open config folder %s", path, exc_info=True)
