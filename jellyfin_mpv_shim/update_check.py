import requests
import datetime
import logging
import webbrowser

from .constants import CLIENT_VERSION
from .conf import settings
from .i18n import _
from .version import is_newer

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type

log = logging.getLogger("update_check")

release_url = "https://github.com/jellyfin/jellyfin-mpv-shim/releases/"
release_urls = [release_url]
one_day = 86400


class UpdateChecker:
    def __init__(self, player_manager: "PlayerManager_type"):
        self.playerManager = player_manager
        self.has_notified = False
        self.new_version = None
        self.last_check = None

    def _check_updates(self):
        log.info("Checking for updates...")
        for release_url in release_urls:
            try:
                response = requests.get(
                    release_url + "latest", allow_redirects=False, timeout=(3, 10)
                )
                if response.status_code != 302:
                    log.warning("Release page returned bad status code.")
                    continue
                if not response.headers["location"].startswith(release_url):
                    log.warning("Release page does not start with release_url.")
                    continue
                # .../releases/tag/v2.10.0 -> 2.10.0. Taken from the last path
                # segment rather than by offset, so the tags dropping their
                # "v" some day changes nothing here.
                tag = response.headers["location"].rpartition("/")[2]
                version = tag[1:] if tag[:1] in ("v", "V") else tag
                # A difference is not an upgrade: this also runs on
                # pre-releases and local builds, whose version is *ahead* of
                # the newest stable tag. /releases/latest never points at a
                # pre-release, so there is nothing here to opt in or out of.
                if is_newer(version, CLIENT_VERSION):
                    self.new_version = version
                    break
                log.info("Up to date (running %s, latest release %s).",
                         CLIENT_VERSION, version)
            except Exception:
                log.error("Could not check for updates.", exc_info=True)
        return self.new_version is not None

    def check(self):
        if not settings.check_updates:
            return

        if (
            self.last_check is not None
            and (datetime.datetime.utcnow() - self.last_check).total_seconds() < one_day
        ):
            log.info("Update check performed in last day. Skipping.")
            return

        self.last_check = datetime.datetime.utcnow()
        if self.new_version is not None or self._check_updates():
            if not self.has_notified and settings.notify_updates:
                self.has_notified = True
                log.info("Update Available: {0}".format(self.new_version))
                self.notify()

    def notify(self):
        """Surface the available update. When a UI is running (it sets
        ``notify_update``) the notice goes to the browser window; otherwise it
        falls back to an MPV OSD toast for CLI/headless users."""
        notify_ui = getattr(self.playerManager, "notify_update", None)
        if notify_ui is not None:
            try:
                notify_ui(self.new_version, release_url + "latest")
                return
            except Exception:
                log.error("Could not send update notice to the UI.", exc_info=True)
        self.playerManager.show_text(
            _(
                "MPV Shim v{0} Update Available\nOpen menu (press c) for details."
            ).format(self.new_version),
            5000,
            1,
        )

    def open(self):
        self.playerManager.set_fullscreen(False)
        try:
            webbrowser.open(release_url + "latest")
        except Exception:
            log.error("Could not open release URL.", exc_info=True)
