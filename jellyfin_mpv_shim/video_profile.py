from .conf import settings
from . import conffile
from .utils import get_resource
from .constants import APP_NAME
from .i18n import _
import logging
import os.path
import shutil
import json

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type
    from .menu import OSDMenu as OSDMenu_type

profile_name_translation = {
    "Generic (FSRCNNX)": _("Generic (FSRCNNX)"),
    "Generic High (FSRCNNX x16)": _("Generic High (FSRCNNX x16)"),
    "Anime4K x4 Faithful (For SD)": _("Anime4K x4 Faithful (For SD)"),
    "Anime4K x4 Perceptual (For SD)": _("Anime4K x4 Perceptual (For SD)"),
    "Anime4K x4 Perceptual + Deblur (For SD)": _(
        "Anime4K x4 Perceptual + Deblur (For SD)"
    ),
    "Anime4K x2 Faithful (For HD)": _("Anime4K x2 Faithful (For HD)"),
    "Anime4K x2 Perceptual (For HD)": _("Anime4K x2 Perceptual (For HD)"),
    "Anime4K x2 Perceptual + Deblur (For HD)": _(
        "Anime4K x2 Perceptual + Deblur (For HD)"
    ),
}

log = logging.getLogger("video_profile")


class MPVSettingError(Exception):
    """Raised when MPV does not support a required setting."""

    pass


class VideoProfileManager:
    def __init__(
        self, menu: "OSDMenu_type", player_manager: "PlayerManager_type", player
    ):
        self.menu = menu
        self.playerManager = player_manager
        self.used_settings = set()
        self.current_profile = None
        self.player = player

        shader_pack_builtin = get_resource("default_shader_pack")

        # Load shader pack
        self.shader_pack = shader_pack_builtin
        if settings.shader_pack_custom:
            self.shader_pack = conffile.get(APP_NAME, "shader_pack")
            if not os.path.exists(self.shader_pack):
                shutil.copytree(shader_pack_builtin, self.shader_pack)

        if not os.path.exists(os.path.join(self.shader_pack, "pack.json")):
            raise FileNotFoundError("Could not find default shader pack.")

        with open(os.path.join(self.shader_pack, "pack.json")) as fh:
            pack = json.load(fh)
            self.default_groups = pack.get("default-setting-groups") or []
            self.profiles = pack.get("profiles") or {}
            self.groups = pack.get("setting-groups") or {}
            self.revert_ignore = set(pack.get("setting-revert-ignore") or [])

        self.defaults = {}
        for group in self.groups.values():
            setting_group = group.get("settings")
            if setting_group is None:
                continue

            for key, value in setting_group:
                if key in self.defaults or key in self.revert_ignore:
                    continue
                try:
                    self.defaults[key] = getattr(self.player, key)
                except Exception:
                    log.warning(
                        "Your MPV does not support setting {0} used in shader pack.".format(
                            key
                        ),
                        exc_info=True,
                    )

        if settings.shader_pack_profile is not None:
            self.load_profile(settings.shader_pack_profile, reset=False)

    def process_setting_group(
        self, group_name: str, settings_to_apply: list, shaders_to_apply: list
    ):
        group = self.groups[group_name]
        for key, value in group.get("settings", []):
            if key not in self.defaults:
                if key not in self.revert_ignore:
                    raise MPVSettingError(
                        "Cannot use setting group {0} due to MPV not supporting {1}".format(
                            group_name, key
                        )
                    )
            else:
                self.used_settings.add(key)
            settings_to_apply.append((key, value))
        for shader in group.get("shaders", []):
            shaders_to_apply.append(os.path.join(self.shader_pack, "shaders", shader))

    def load_profile(self, profile_name: str, reset: bool = True):
        if reset:
            self.unload_profile()
        log.info("Loading shader profile {0}.".format(profile_name))
        profile = self.profiles[profile_name]
        settings_to_apply = []
        shaders_to_apply = []
        try:
            # Read Settings & Shaders
            for group in self.default_groups:
                self.process_setting_group(group, settings_to_apply, shaders_to_apply)
            for group in profile.get("setting-groups", []):
                self.process_setting_group(group, settings_to_apply, shaders_to_apply)
            for shader in profile.get("shaders", []):
                shaders_to_apply.append(
                    os.path.join(self.shader_pack, "shaders", shader)
                )

            # Apply Settings
            already_set = set()
            for key, value in settings_to_apply:
                if (key, value) in already_set:
                    continue
                log.debug("Set MPV setting {0} to {1}".format(key, value))
                setattr(self.player, key, value)
                already_set.add((key, value))

            # Apply Shaders
            log.debug("Set shaders: {0}".format(shaders_to_apply))
            self.player.glsl_shaders = shaders_to_apply
            self.current_profile = profile_name
            return True
        except MPVSettingError:
            log.error("Could not apply shader profile.", exc_info=True)
            return False

    def unload_profile(self):
        log.info("Unloading shader profile.")
        self.player.glsl_shaders = []
        for setting in self.used_settings:
            value = self.defaults[setting]
            try:
                setattr(self.player, setting, value)
            except Exception:
                log.warning(
                    "Default setting {0} value {1} is invalid.".format(setting, value)
                )
        self.current_profile = None

    def menu_handle(self):
        profile_name = self.menu.menu_list[self.menu.menu_selection][2]
        settings_were_successful = True
        if profile_name is None:
            self.unload_profile()
        else:
            settings_were_successful = self.load_profile(profile_name)
        if settings.shader_pack_remember and settings_were_successful:
            settings.shader_pack_profile = profile_name
            settings.save()

        # Need to re-render menu.
        self.menu.menu_action("back")
        self.menu_action()

    def menu_action(self):
        selected = 0
        profile_option_list = [(_("None (Disabled)"), self.menu_handle, None)]
        for i, (profile_name, profile) in enumerate(self.profiles.items()):
            name = profile["displayname"]
            if name in profile_name_translation:
                name = profile_name_translation[name]
            profile_option_list.append((name, self.menu_handle, profile_name))
            if profile_name == self.current_profile:
                selected = i + 1
        self.menu.put_menu(_("Select Shader Profile"), profile_option_list, selected)
