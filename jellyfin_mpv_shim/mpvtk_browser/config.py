"""Settings-schema helpers for the mpvtk browser's Settings view.

Decoupled from gui_mgr (which pulls in Tk/pystray): classifies the same
``conf.Settings`` annotations the Tk browser's form uses, and reads/writes
the in-process ``conf.settings`` singleton directly (no IPC — the mpvtk
browser runs in the player's process).
"""

import typing

from ..conf import Settings, settings

# Keys that are editable in the Tk form but noisy / risky in a flat mpvtk
# list, or handled elsewhere. Hidden here to keep the form usable.
_HIDDEN = {"browser_ui", "language_config", "media_key_settings",
           "svp_url", "sub_ass_force_overrides"}


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


def set_setting(key, value):
    """Coerce ``value`` to the key's declared type, apply, and persist.
    Returns True on success, False if the value was invalid for the type."""
    schema = settings_schema()
    kind = schema.get(key)
    if kind is None:
        return False
    try:
        setattr(settings, key, coerce(kind, value))
    except (ValueError, TypeError):
        return False
    settings.save()
    return True
