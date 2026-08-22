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
        """Open a link in the desktop's browser. ``(ok, method)``.

        Through ``system_open`` rather than ``webbrowser``, for three
        reasons. It applies a scheme allowlist -- this now carries links the
        *server* composed (``ExternalUrls`` on the detail page), not only our
        own release URL, and a desktop opener hands ``file://`` or a
        registered application scheme to whatever claims it. It uses the same
        opener list the rest of the app does, which in a Flatpak means
        ``xdg-open`` and therefore the portal, where ``webbrowser`` looks for
        a browser *inside* the sandbox and finds none. And it answers whether
        it worked, so a caller can say so; ``webbrowser.open``'s return value
        is famously optimistic and this one ignored it anyway.
        """
        from ...system_open import open_url
        try:
            return open_url(url)
        except Exception:
            # system_open promises not to raise; this is the belt to its
            # braces, because the caller here is a click handler on the
            # render loop.
            log.error("could not open url", exc_info=True)
            return (False, None)

    def check_updates(self):
        """One-shot update check at startup.

        Without it a GUI user only ever saw the update notice after starting
        playback, because that was the only thing driving the check."""
        from ...player import playerManager
        try:
            playerManager.update_check.check()
        except Exception:
            log.debug("startup update check failed", exc_info=True)

    def rich_presence_available(self):
        """Whether Discord Rich Presence actually came up this session.

        The setting is a request, not a state: ``player.py`` reads it once at
        import and only sets its flag if ``rich_presence`` (and so
        ``pypresence``) imports. Ticking the box with the optional dependency
        missing therefore did nothing at all, silently — the only sign was a
        line in the log nobody had a reason to read.

        Reports the *flag*, not whether ``pypresence`` can be imported now:
        installing it mid-session does not enable the feature, so answering
        "yes" would be a nicer lie than the silence it replaces.
        """
        from ...player import discord_presence
        return bool(discord_presence)

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

    @staticmethod
    def can_restart():
        """Whether a restart can be performed, asked before offering one.

        Before anything is shut down, deliberately: a machine where the
        launch cannot be reconstructed gets a banner saying "restart to
        apply" instead of a button that takes the app away and does not
        bring it back.
        """
        from ...restart import supported

        return supported()

    def restart_app(self):
        """Restart the whole application.

        Arms the relaunch and then triggers the **ordinary** shutdown --
        which is what saves the window geometry, posts the final progress
        report and releases the single-instance lock before the new copy
        starts. The relaunch itself is the last thing `mpv_shim.main` does;
        see restart.py for why it is there and not here.

        Returns False if it could not be started, so the caller can say so
        rather than leaving the user looking at an app that did not quit.
        """
        from ...restart import cancel, request, supported

        if not supported():
            log.error("Restart asked for, but this launch cannot be "
                      "reconstructed; not quitting.")
            return False
        request()
        started = False
        try:
            from ..ui import user_interface

            started = user_interface.quit_app()
        except Exception:
            log.exception("could not start the restart")
        if not started:
            # The flag is set but the shutdown never began, so disarm it.
            # Leaving it armed would turn the user's NEXT ordinary quit into
            # a surprise relaunch -- a bug that would surface minutes later,
            # in a session that had nothing to do with this button.
            #
            # Checked on the RETURN VALUE, not just on an exception: quitting
            # with no shutdown callback wired fails quietly, which is exactly
            # the case an except clause cannot see.
            cancel()
            return False
        return True
