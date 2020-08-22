from .conf import settings
from .i18n import _

import urllib.request
import urllib.error
import logging
import sys
import time

log = logging.getLogger("svp_integration")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .menu import OSDMenu as OSDMenu_type
    from .player import PlayerManager as PlayerManager_type


def list_request(path: str):
    try:
        response = urllib.request.urlopen(settings.svp_url + "?" + path)
        return response.read().decode("utf-8").replace("\r\n", "\n").split("\n")
    except urllib.error.URLError:
        log.error("Could not reach SVP API server.", exc_info=True)
        return None


def simple_request(path: str):
    response_list = list_request(path)
    if response_list is None:
        return None
    if len(response_list) != 1 or " = " not in response_list[0]:
        return None
    return response_list[0].split(" = ")[1]


def get_profiles():
    profile_ids = list_request("list=profiles")
    profiles = {}
    for profile_id in profile_ids:
        profile_id = profile_id.replace("profiles.", "")
        if profile_id == "predef":
            continue
        if profile_id == "P10000001_1001_1001_1001_100000000001":
            profile_name = _("Automatic")
        else:
            profile_name = simple_request("profiles.{0}.title".format(profile_id))
        if simple_request("profiles.{0}.on".format(profile_id)) == "false":
            continue
        profile_guid = "{" + profile_id[1:].replace("_", "-") + "}"
        profiles[profile_guid] = profile_name
    return profiles


def get_name_from_guid(profile_id: str):
    profile_id = "P" + profile_id[1:-1].replace("-", "_")
    if profile_id == "P10000001_1001_1001_1001_100000000001":
        return _("Automatic")
    else:
        return simple_request("profiles.{0}.title".format(profile_id))


def get_last_profile():
    return simple_request("rt.playback.last_profile")


def is_svp_alive():
    try:
        response = list_request("")
        return response is not None
    except Exception:
        log.error("Could not reach SVP API server.", exc_info=True)
        return False


def is_svp_enabled():
    return simple_request("rt.disabled") == "false"


def is_svp_active():
    response = simple_request("rt.playback.active")
    if response is None:
        return False
    return response != ""


def set_active_profile(profile_id: str):
    # As far as I know, there is no way to directly set the profile.
    if not is_svp_active():
        return False
    if profile_id == get_last_profile():
        return True
    for i in range(len(list_request("list=profiles"))):
        list_request("!profile_next")
        if get_last_profile() == profile_id:
            return True
    return False


def set_disabled(disabled: bool):
    return (
        simple_request("rt.disabled={0}".format("true" if disabled else "false"))
        == "true"
    )


class SVPManager:
    def __init__(self, menu: "OSDMenu_type", player_manager: "PlayerManager_type"):
        self.menu = menu

        if settings.svp_enable:
            socket = settings.svp_socket
            if socket is None:
                if sys.platform.startswith("win32") or sys.platform.startswith(
                    "cygwin"
                ):
                    socket = "mpvpipe"
                else:
                    socket = "/tmp/mpvsocket"

            # This actually *adds* another ipc server.
            player_manager.add_ipc(socket)

        if settings.svp_enable and not is_svp_alive():
            log.error(
                "SVP is not reachable. Please make sure you have the API enabled."
            )

    @staticmethod
    def is_available():
        if not settings.svp_enable:
            return False
        if not is_svp_alive():
            return False
        return True

    def menu_set_profile(self):
        profile_id = self.menu.menu_list[self.menu.menu_selection][2]
        if profile_id is None:
            set_disabled(True)
        else:
            set_active_profile(profile_id)
        # Need to re-render menu. SVP has a race condition so we wait a second.
        time.sleep(1)
        self.menu.menu_action("back")
        self.menu_action()

    def menu_set_enabled(self):
        set_disabled(False)

        # Need to re-render menu. SVP has a race condition so we wait a second.
        time.sleep(1)
        self.menu.menu_action("back")
        self.menu_action()

    def menu_action(self):
        if is_svp_active():
            selected = 0
            active_profile = get_last_profile()
            profile_option_list = [(_("Disabled"), self.menu_set_profile, None)]
            for i, (profile_id, profile_name) in enumerate(get_profiles().items()):
                profile_option_list.append(
                    (profile_name, self.menu_set_profile, profile_id)
                )
                if profile_id == active_profile:
                    selected = i + 1
            self.menu.put_menu(_("Select SVP Profile"), profile_option_list, selected)
        else:
            if is_svp_enabled():
                self.menu.put_menu(
                    _("SVP is Not Active"),
                    [
                        (_("Disable"), self.menu_set_profile, None),
                        (_("Retry"), self.menu_set_enabled),
                    ],
                    selected=1,
                )
            else:
                self.menu.put_menu(
                    _("SVP is Disabled"), [(_("Enable SVP"), self.menu_set_enabled)]
                )
