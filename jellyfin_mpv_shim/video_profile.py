from .conf import settings
from .mpv_options import NAIVE_HWDEC, hwdec_pinned_by_config
from . import conffile
from .utils import get_resource
from .constants import APP_NAME
from .i18n import _
import logging
import os.path
import shutil
import json
import sys

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type
    from .menu import OSDMenu as OSDMenu_type

# What a pack's optional "platforms" list is matched against. A profile may
# be built around something that exists on exactly one OS -- RTX Video Super
# Resolution is a Direct3D 11 video filter, not a shader -- and offering it
# elsewhere is offering a menu entry that cannot work.
if sys.platform == "win32":
    PLATFORM = "windows"
elif sys.platform == "darwin":
    PLATFORM = "macos"
else:
    PLATFORM = "linux"

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
    "ArtCNN (Denoise + Sharpen)": _("ArtCNN (Denoise + Sharpen)"),
    "ArtCNN High (C4F32)": _("ArtCNN High (C4F32)"),
}

log = logging.getLogger("video_profile")


def reset_saved_shader_settings():
    """Put the persisted shader settings back to their defaults.

    The two settings that can leave a machine unable to show video are the
    remembered profile (reapplied at startup by VideoProfileManager, before
    anyone can reach a menu to turn it off) and a forced graphics API. The
    ``k`` keybind clears the first, but that needs a visible window to press
    it in -- which is exactly what a bad gpu-api takes away. This is the same
    escape hatch from the command line.

    Returns ``[(key, old_value), ...]`` for what actually changed, so the
    caller can say what it did rather than claiming a reset that was a no-op.
    """
    changed = []
    if settings.shader_pack_profile is not None:
        changed.append(("shader_pack_profile", settings.shader_pack_profile))
        settings.shader_pack_profile = None
    # Compared case-insensitively against the default the same way
    # api_setting_override() reads it, so "AUTO" is not reported as a change.
    if (settings.shader_pack_gpu_api or "auto").lower() != "auto":
        changed.append(("shader_pack_gpu_api", settings.shader_pack_gpu_api))
        settings.shader_pack_gpu_api = "auto"
    if changed:
        settings.save()
    return changed


class MPVSettingError(Exception):
    """Raised when MPV does not support a required setting."""

    pass


def _hwdec_taken_out_of_our_hands():
    """Whether something outranks both us and the pack on hwdec.

    Two things do: the user's own mpv.conf (a pin -- nothing writes the
    option), and ``--disable-hwdec`` (the recovery path for hardware
    decoding stopping the window opening at all). A profile naming its
    decoder outranks the *setting*, but not these.
    """
    from .args import get_args

    try:
        if getattr(get_args(), "disable_hwdec", False):
            return True
    except Exception:
        log.debug("could not read the hwdec override", exc_info=True)
    return hwdec_pinned_by_config() is not None


class VideoProfileManager:
    def __init__(
        self, menu: "OSDMenu_type", player_manager: "PlayerManager_type", player
    ):
        self.menu = menu
        self.playerManager = player_manager
        self.used_settings = set()
        #: True while the loaded profile needs frames in system RAM. Not
        #: "hardware decoding is on" -- it is "if it is on, it has to be the
        #: copy kind". Derived, not read: see _wants_copy.
        self.wants_copy_hwdec = False
        #: Working state for that derivation, per load.
        self._sets_vf = False
        self._names_direct_hwdec = False
        #: A specific decoder the loaded profile requires, or None.
        #: Not the naive auto/auto-copy every profile carries -- see
        #: process_setting_group.
        self.forced_hwdec = None
        #: Profile name parked by suspend_for_still, or None. Distinct from
        #: current_profile, which stays set while suspended so the menu and
        #: the remembered setting still say what the user chose.
        self._suspended = None
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
    def profile_is_available(profile: dict) -> bool:
        """Whether this machine can run the profile at all.

        Gating is opt-in: a profile that says nothing about platforms runs
        everywhere, which is all of them but the few that had to declare
        themselves. Packs that predate this key are unaffected.
        """
        platforms = profile.get("platforms")
        return platforms is None or PLATFORM in platforms

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
            if key == "hwdec":
                # **A naive value is the pack's opinion about the machine;
                # a named decoder is a requirement of the profile.**
                #
                # Every profile pulls in a "hwdec-default" group setting
                # hwdec to auto-copy -- [iw]: "this was just me being
                # risk-averse in the past". That is a policy ("use hardware
                # decoding if you can"), and it is not the pack's to have:
                # hwdec is a user-facing setting that defaults OFF because
                # a long tail of graphics drivers handle it badly, up to
                # mpv hanging before the window opens (mpv#12948). Picking
                # a shader profile must not switch it back on, or the
                # breakage gets attributed to the profile -- the last place
                # anyone would look. Dropped.
                #
                # A *specific* decoder is different in kind. The shipped
                # rtx-vsr names d3d11va because its d3d11vpp filter
                # operates on Direct3D surfaces: the profile does not work
                # without it, and choosing that profile IS opting in.
                # Applied, and remembered, so the per-item write in
                # _play_media does not undo it on the next file.
                # The user's own mpv.conf outranks a pack as well as the
                # setting: where it pins hwdec, nothing writes the option.
                # Checked here rather than left to _play_media, because a
                # profile applies its settings directly and would otherwise
                # slip past the pin between one file and the next.
                if _hwdec_taken_out_of_our_hands():
                    log.info("Not applying the shader pack's hwdec=%s; "
                             "--disable-hwdec or mpv.conf decides it.", value)
                    continue
                if str(value).strip().lower() in NAIVE_HWDEC:
                    log.info("Not applying the shader pack's hwdec=%s; "
                             "hardware decoding follows the Hardware "
                             "Decoding setting.", value)
                    if not str(value).endswith("-copy"):
                        self._names_direct_hwdec = True
                    continue
                log.info("Shader profile requires hwdec=%s; applying it.",
                         value)
                self.forced_hwdec = value
                self._names_direct_hwdec = not str(value).endswith("-copy")
            if key == "vf":
                # A real video filter, which is the one thing here that
                # genuinely cannot read GPU frames -- unlike a glsl shader.
                self._sets_vf = True
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
        if not self.profile_is_available(profile):
            # Reachable without the menu: a config.json carried between
            # machines, or shader_pack_profile remembered on another OS.
            log.error(
                "Shader profile {0} needs {1}, so it cannot run here.".format(
                    profile_name, "/".join(profile["platforms"])
                )
            )
            return False

        settings_to_apply = []
        shaders_to_apply = []
        # Recomputed from the groups below rather than left standing: a
        # load with reset=False (the still-image resume, the startup
        # restore) does not go through unload_profile, so a stale True
        # would outlive the profile that set it.
        self.wants_copy_hwdec = False
        self._sets_vf = False
        self._names_direct_hwdec = False
        self.forced_hwdec = None
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

            self.wants_copy_hwdec = self._wants_copy()

            # Apply Shaders
            log.info("Set shaders: {0}".format(shaders_to_apply))
            self.player.glsl_shaders = shaders_to_apply
            self.current_profile = profile_name
            return True
        except MPVSettingError:
            log.error("Could not apply shader profile.", exc_info=True)
            return False

    def suspend_for_still(self):
        """Take the shader profile off while a still image is on screen.

        A shader pack is applied once -- from the menu, or restored at
        startup from ``shader_pack_profile`` -- and then left on the mpv
        instance. Nothing on the play path touched it, so an anime-upscaling
        chain ran over a photograph, and over a comic page at 1600x2400 or
        larger, where it is both wrong and expensive: these packs are built
        for moving pictures at broadcast resolutions and a scanned page is
        neither.

        **Not ``unload_profile``**, which is the whole reason this exists:
        that clears ``current_profile``, and the menu's selection and
        ``menu_handle``'s persistence both read it -- so opening a photo
        would have silently reset the user's chosen profile. This is a
        suspension: mpv is put back to defaults, the *name* is kept, and
        :meth:`resume_after_still` puts it back.

        Idempotent. Photos arrive in queues, and every one of them runs the
        play path.
        """
        if self._suspended is not None or self.current_profile is None:
            return
        self._suspended = self.current_profile
        log.info("Suspending shader profile %s for a still image.",
                 self._suspended)
        name = self.current_profile
        self.unload_profile()
        self.current_profile = name

    def resume_after_still(self):
        """Put back a profile :meth:`suspend_for_still` took off.

        A no-op when nothing was suspended, which is the ordinary case: this
        runs on every playback start, and almost none of them follow a
        still.
        """
        name, self._suspended = self._suspended, None
        if name is None:
            return
        log.info("Restoring shader profile %s.", name)
        # reset=False: unload already ran, and unloading again would write
        # every default a second time for nothing.
        self.load_profile(name, reset=False)

    def _wants_copy(self):
        """Does this profile need frames in system RAM?

        Only a real ``vf`` does -- a glsl shader runs inside the GPU
        renderer, on frames that are already on the GPU. So the pack's
        blanket ``hwdec: auto-copy`` is not the question; whether the
        profile installs a filter is.

        And a profile that names a **direct** hwdec mode alongside its
        filter is saying the opposite: its filter wants GPU frames. In the
        shipped pack that is exactly ``rtx-vsr``, whose
        ``format=nv12,d3d11vpp=scale=2:scaling-mode=nvidia`` is a Direct3D
        video-processor filter that operates on d3d11 surfaces --
        copying back would break the only profile in the pack that has a
        filter at all.
        """
        return self._sets_vf and not self._names_direct_hwdec

    def unload_profile(self):
        log.info("Unloading shader profile.")
        self.wants_copy_hwdec = False
        self._sets_vf = False
        self._names_direct_hwdec = False
        self.forced_hwdec = None
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
        for profile_name, profile in self.profiles.items():
            if (
                profile.get("subtype", None) is not None
                and not settings.shader_pack_subtype in profile["subtype"]
            ):
                continue
            if not self.profile_is_available(profile):
                continue

            name = profile["displayname"]
            if name in profile_name_translation:
                name = profile_name_translation[name]
            profile_option_list.append((name, self.menu_handle, profile_name))
            if profile_name == self.current_profile:
                # The row it landed on, not its index in the unfiltered
                # pack -- skipped profiles shift everything after them.
                selected = len(profile_option_list) - 1
        self.menu.put_menu(_("Select Shader Profile"), profile_option_list, selected)
