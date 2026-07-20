"""Settings-schema helpers for the mpvtk browser's Settings view.

Decoupled from gui_mgr (which pulls in Tk/pystray): classifies the same
``conf.Settings`` annotations the Tk browser's form uses, and reads/writes
the in-process ``conf.settings`` singleton directly (no IPC — the mpvtk
browser runs in the player's process).
"""

import typing

from ..conf import Settings, settings
from ..i18n import _

# Structured / non-scalar config that the flat form can't express, plus
# internal bookkeeping. Everything else is editable — including browser_ui,
# which is the only in-UI way back to the Tk browser.
_HIDDEN = {"language_config", "audio_output", "close_prompt_shown"}

# Curated groups, mirroring the Tk browser's form. Anything not listed shows
# under "Advanced".
SECTIONS = [
    (_("Interface"), ["player_name", "browser_ui", "browser_fullscreen",
                      "enable_gui", "start_minimized", "close_to_tray",
                      "fullscreen", "enable_osc", "osc_style",
                      "hud_grab_keys", "hud_wake_key", "raise_mpv",
                      "check_updates", "notify_updates"]),
    (_("Playback"), ["auto_play", "always_transcode", "local_kbps",
                     "remote_kbps", "direct_paths", "remote_direct_paths",
                     "playback_timeout"]),
    (_("Subtitles & Languages"), ["subtitle_size", "subtitle_color",
                                  "subtitle_position", "language_preference",
                                  "preferred_language", "remember_audio_track",
                                  "remember_subtitle_track", "lang_filter",
                                  "lang_filter_sub", "lang_filter_audio"]),
    (_("Transcoding"), ["allow_transcode_to_h265", "prefer_transcode_to_h265",
                        "transcode_hevc", "transcode_av1", "transcode_4k",
                        "transcode_hdr", "transcode_hi10p",
                        "transcode_dolby_vision", "force_video_codec",
                        "force_audio_codec"]),
    (_("Skip Intro / Credits"), ["skip_intro_enable", "skip_intro_always",
                                 "skip_credits_enable", "skip_credits_always",
                                 "skip_intro_on_seek"]),
    (_("Library Browser"), ["library_image_cache_mb"]),
    (_("Downloads"), ["sync_path", "prefer_downloaded"]),
]

# Free-text is wrong for these: an unlisted value silently breaks the feature.
ENUMS = {
    "browser_ui": ["mpvtk", "tk"],
    "subtitle_position": ["top", "bottom", "middle"],
    "mpv_log_level": ["fatal", "error", "warn", "info", "debug"],
    "shader_pack_subtype": ["lq", "hq"],
}

# Enums whose stored value isn't presentable: [(label, value), ...].
LABELED_ENUMS = {
    "osc_style": [
        (_("Jellyfin UI"), "mpvtk"),
        (_("MPV UI with thumbnails"), "mpv"),
        (_("MPV built-in default"), "default"),
    ],
    "language_preference": [
        (_("Unset"), "unset"),
        (_("Dubbed (shows only)"), "dubbed_shows"),
        (_("Subbed (shows only)"), "subbed_shows"),
        (_("Dubbed (all)"), "dubbed_all"),
        (_("Subbed (all)"), "subbed_all"),
        (_("Custom (set in config)"), "custom"),
    ],
}

LABEL_OVERRIDES = {
    "sync_path": _("Download Folder"),
    "prefer_downloaded": _("Prefer Downloaded Copy"),
    "close_to_tray": _("Close to Tray (keep running)"),
    "osc_style": _("Player Controls Style"),
    "browser_ui": _("Library Browser UI"),
    "browser_fullscreen": _("Fullscreen Library Browser"),
    "hud_grab_keys": _("Always Bind Arrow Keys to Player Controls"),
    "hud_wake_key": _("Player Controls Activation Key"),
}

# Explanatory line rendered under a setting, for the ones whose default
# isn't self-explanatory from the label alone.
NOTES = {
    "osc_style": _("MPV keybinds are used by default. Press ENTER to drive "
                   "the player controls by keyboard."),
}

_ACRONYMS = {"gui": "GUI", "ssl": "SSL", "tls": "TLS", "osc": "OSC",
             "mpv": "MPV", "hdr": "HDR", "av1": "AV1", "h265": "H265",
             "hevc": "HEVC", "kbps": "kbps", "url": "URL", "ipc": "IPC",
             "uuid": "UUID", "svp": "SVP", "id": "ID", "4k": "4K",
             "hi10p": "Hi10P", "ui": "UI"}


def label_for(key):
    if key in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[key]
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in key.split("_"))


def sections():
    """``[(title, [key, ...]), ...]`` — curated groups first, then Advanced
    with everything else that's editable."""
    schema = settings_schema()
    curated = set()
    out = []
    for title, keys in SECTIONS:
        present = [k for k in keys if k in schema]
        curated.update(present)
        if present:
            out.append((title, present))
    advanced = sorted(k for k in schema if k not in curated)
    if advanced:
        out.append((_("Advanced"), advanced))
    return out


def _classify(ann):
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    if typing.get_origin(ann) is typing.Union:
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return _classify(non_none[0])
    return "skip"  # lists / structured configs — not editable in the flat form


def settings_schema():
    """``{key: "bool"|"int"|"float"|"str"}`` for the editable settings."""
    out = {}
    for key, ann in Settings.__annotations__.items():
        if key.startswith("_") or key in _HIDDEN:
            continue
        kind = _classify(ann)
        if kind != "skip":
            out[key] = kind
    return out


def get_settings():
    return settings.dict()


def coerce(kind, value):
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value)


def materialize_language_preset():
    """The language dropdown writes language_config rules (README-style): a
    preset generates rules, Unset clears them, Custom leaves them alone.

    Ported from gui_mgr; without it choosing "Dubbed (shows only)" persists a
    string that nothing reads and track selection never changes."""
    from ..language_config import preset_rules, parse_language_config

    pref = settings.language_preference
    if pref == "custom":
        return
    if pref == "unset":
        settings.language_config = None
        return
    settings.language_config = parse_language_config(
        preset_rules(pref, settings.preferred_language))


def set_setting(key, value):
    """Coerce ``value`` to the key's declared type, apply, and persist.
    Returns True on success, False if the value was invalid for the type.

    ``sync_path`` is *not* handled here — moving the download store is a long
    filesystem operation, see relocate_downloads()."""
    schema = settings_schema()
    kind = schema.get(key)
    if kind is None:
        return False
    try:
        setattr(settings, key, coerce(kind, value))
    except (ValueError, TypeError):
        return False
    if key in ("language_preference", "preferred_language"):
        try:
            materialize_language_preset()
        except Exception:  # a bad preset must not block the save
            pass
    settings.save()
    return True


def relocate_downloads(new_path, progress=None):
    """Move the download store to ``new_path`` and persist the *resolved*
    path. Returns (ok, message). Blocking — call from a worker."""
    from ..sync.manager import syncManager

    ok, message = syncManager.relocate(new_path or None, progress=progress)
    if ok:
        settings.sync_path = syncManager.root if new_path else None
        settings.save()
    return ok, message
