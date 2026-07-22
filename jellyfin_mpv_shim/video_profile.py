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
        self.profile_subtypes = []

        shader_pack_builtin = get_resource("default_shader_pack")

        # Load shader pack
        self.shader_pack = shader_pack_builtin
        if settings.shader_pack_custom:
            self.shader_pack = conffile.get(APP_NAME, "shader_pack")
            if not os.path.exists(self.shader_pack):
                shutil.copytree(shader_pack_builtin, self.shader_pack)

        pack_name = "pack-next.json"
        if not os.path.exists(os.path.join(self.shader_pack, pack_name)):
            pack_name = "pack.json"

            if not os.path.exists(os.path.join(self.shader_pack, pack_name)):
                raise FileNotFoundError("Could not find default shader pack.")

        with open(os.path.join(self.shader_pack, pack_name)) as fh:
            pack = json.load(fh)
            self.default_groups = pack.get("default-setting-groups") or []
            self.profiles = pack.get("profiles") or {}
            self.groups = pack.get("setting-groups") or {}
            self.revert_ignore = set(pack.get("setting-revert-ignore") or [])

            self.profile_subtypes = set()
            for profile in self.profiles.values():
                for subtype in profile.get("subtype", []):
                    self.profile_subtypes.add(subtype)

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

    @staticmethod
    def api_setting_override(key: str, pack_value):
        """The two settings the pack uses to pin itself to OpenGL.

        default-shader-pack 84fc5df (2020, "Fix Windows and external MPV
        compatibility issues") added `gpu_api: opengl` and
        `fbo_format: rgba16f` together, and the pairing is the whole story:
        `rgba16f` is an OpenGL-backend format name. The Direct3D 11 backend
        calls the same format `rgba16hf` (mpv `video/out/d3d11/ra_d3d11.c`),
        so on d3d11 the pack's value fails to initialize, MPV falls back to
        *dumb mode*, and dumb mode disables every user shader — silently.
        Forcing `opengl` made the format name true again.

        Neither pin is needed now, and both cost something:

        - `fbo_format` is dropped outright. MPV's `auto` already tries
          16-bit float first (`rgba16f`, `rgba16hf`) on whichever backend
          is live, which is what the pack was asking for, spelled portably.
        - `gpu_api: opengl` is dropped, so MPV keeps the API it picked. The
          shaders do not need OpenGL — MPV cross-compiles user GLSL to
          SPIR-V, and the pack's profiles run unmodified on Vulkan and
          compile clean through the d3d11 chain. Forcing OpenGL does cost
          HDR on Windows, where the autoprobe order is d3d11, then Vulkan,
          then OpenGL last (mpv `video/out/gpu/context.c`).

        Only that one legacy value is refused. A profile that names some
        other API means it, rather than inheriting a 2020 workaround — a
        Direct3D 11 video filter like RTX Video Super Resolution genuinely
        cannot run on another backend — so those are passed through. The
        user's `shader_pack_gpu_api` outranks both.

        Returns the value to apply, or None to leave the setting alone.
        """
        if key == "fbo_format":
            log.debug("Ignoring shader pack fbo-format=%s; MPV's auto is portable.",
                      pack_value)
            return None
        choice = (settings.shader_pack_gpu_api or "auto").lower()
        if choice != "auto":
            return choice
        if str(pack_value).lower() == "opengl":
            log.debug("Ignoring shader pack gpu-api=opengl; leaving MPV's own choice.")
            return None
        return pack_value

    def process_setting_group(
        self, group_name: str, settings_to_apply: list, shaders_to_apply: list
    ):
        group = self.groups[group_name]
        for key, value in group.get("settings", []):
            if key in ("gpu_api", "fbo_format"):
                value = self.api_setting_override(key, value)
                if value is None:
                    continue
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
        if profile_name not in self.profiles:
            log.error("Shader profile {0} does not exist.".format(profile_name))
            return False

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
                log.info("Set MPV setting {0} to {1}".format(key, value))
                if key == "gpu_api":
                    # A pack may ask for an API this build has no context
                    # for -- a Direct3D 11 profile read on Linux, say. MPV
                    # rejects the value outright, and losing the rest of
                    # the profile (and raising into the menu) over that is
                    # worse than running it on the API we already have.
                    try:
                        setattr(self.player, key, value)
                    except Exception:
                        log.warning(
                            "MPV would not switch to gpu-api={0}; keeping the "
                            "current one.".format(value),
                            exc_info=True,
                        )
                else:
                    setattr(self.player, key, value)
                already_set.add((key, value))

            # Apply Shaders
            log.info("Set shaders: {0}".format(shaders_to_apply))
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
            if (
                profile.get("subtype", None) is not None
                and not settings.shader_pack_subtype in profile["subtype"]
            ):
                continue

            name = profile["displayname"]
            if name in profile_name_translation:
                name = profile_name_translation[name]
            profile_option_list.append((name, self.menu_handle, profile_name))
            if profile_name == self.current_profile:
                selected = i + 1
        self.menu.put_menu(_("Select Shader Profile"), profile_option_list, selected)
