import requests
import datetime
import logging
import webbrowser

from .constants import CLIENT_VERSION
from .conf import settings
from .i18n import _

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
                version = response.headers["location"][len(release_url) + 5 :]
                if CLIENT_VERSION != version:
                    self.new_version = version
                    break
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
