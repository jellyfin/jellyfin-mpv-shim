from __future__ import annotations
from typing import Optional
import logging

log = logging.getLogger("settings_base")

# This is NOT a full pydantic replacement!!!
# Compatible with PEP 563
# Tries to also deal with common errors in the config


def allow_none(constructor):
    def wrapper(input):
        # An empty string means "cleared": the config UI submits text fields
        # verbatim, so an emptied nullable field arrives as "" — and several
        # consumers crash on "" where they tolerate None (mpv_ext_ipc,
        # mpv_ext_path, lang, shader_pack_profile, ...). "" -> None also keeps
        # Optional[int] fields from erroring out (int("") raises, which would
        # silently drop the user's clear).
        if input is None or input == "null" or input == "":
            return None
        return constructor(input)

    return wrapper


yes_set = {1, "yes", "Yes", "True", "true", "1", True}


def adv_bool(value):
    return value in yes_set


object_types = {
    "float": float,
    "int": int,
    "str": str,
    "bool": adv_bool,
    "list": list,
    "Optional[float]": allow_none(float),
    "Optional[int]": allow_none(int),
    "Optional[str]": allow_none(str),
    "Optional[bool]": allow_none(adv_bool),
    float: float,
    int: int,
    str: str,
    bool: adv_bool,
    list: list,
    Optional[float]: allow_none(float),
    Optional[int]: allow_none(int),
    Optional[str]: allow_none(str),
    Optional[bool]: allow_none(adv_bool),
}


class SettingsBase:
    def __init__(self):
        #: Fields a command-line flag is overriding FOR THIS RUN, mapped to
        #: the value that was on disk. See `apply_cli_override`. First,
        #: because `__setattr__` reads it.
        self.__cli_overrides__ = {}
        self.__fields_set__ = set()
        self.__fields__ = []
        for attr in self.__class__.__annotations__.keys():
            if attr.startswith("_"):
                continue

            self.__fields__.append(attr)
            setattr(self, attr, getattr(self.__class__, attr))

    def __setattr__(self, name, value):
        """Any ordinary write drops a CLI override on that field.

        The user has now stated an intent for it -- from the settings
        screen, or from one of the several places that persist a value as a
        side effect -- and that outranks a flag which only ever applied to
        this run.

        Here rather than at the writers, because there are eight of them and
        one of them is "whatever gets added next". `self.__dict__.get` and
        not `getattr`: this runs on every attribute set including the ones
        in `__init__` before the record exists, and it must not recurse.
        """
        overrides = self.__dict__.get("__cli_overrides__")
        if overrides:
            overrides.pop(name, None)
        object.__setattr__(self, name, value)

    def apply_cli_override(self, key, value):
        """Set ``key`` for this run without letting it reach the config file.

        `save()` serializes `dict()`, and `dict()` walks every field -- so a
        flag assigned straight onto the settings object is written out by the
        next save from anywhere, and there are many: window geometry (with
        `remember_window_size` on by default), audio device, shader profile,
        every settings-screen edit. One run with `--minimized` and the option
        was on for good.

        So the on-disk value is remembered here and handed back by `dict()`
        instead of the live one. `--scale` used to dodge this with a comment
        asking callers not to save; three sibling flags had the same shape
        and no such comment.
        """
        if key not in self.__fields__:
            raise KeyError(key)
        was = getattr(self, key)
        setattr(self, key, value)          # clears any earlier record
        self.__cli_overrides__[key] = was

    def dict(self):
        """The settings as they should be PERSISTED and presented.

        Not quite "as they are in effect": a field under a CLI override
        reports the value from the config file. That is what keeps
        `--minimized` out of `conf.json`, and it is also the honest answer
        for the settings screen, which is showing what is configured rather
        than what this particular launch was told to do.
        """
        overrides = self.__cli_overrides__
        result = {}
        for attr in self.__fields__:
            value = overrides[attr] if attr in overrides else getattr(self, attr)
            # Structured fields opt in by exposing _to_dict() on their items.
            if isinstance(value, list) and value and hasattr(value[0], "_to_dict"):
                result[attr] = [item._to_dict() for item in value]
            else:
                result[attr] = value
        return result

    def parse_obj(self, object):
        new_obj = self.__class__()
        annotations = self.__class__.__annotations__

        for attr in self.__fields__:
            if attr in object:
                parse = object_types[annotations[attr]]
                try:
                    setattr(new_obj, attr, parse(object[attr]))
                    new_obj.__fields_set__.add(attr)
                except:
                    log.error(
                        "Setting {0} had invalid value {1}.".format(attr, object[attr]),
                        exc_info=True,
                    )
        return new_obj
