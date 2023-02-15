from __future__ import annotations
from typing import Optional
import logging

log = logging.getLogger("settings_base")

# This is NOT a full pydantic replacement!!!
# Compatible with PEP 563
# Tries to also deal with common errors in the config


def allow_none(constructor):
    def wrapper(input):
        if input is None or input == "null":
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
    "Optional[float]": allow_none(float),
    "Optional[int]": allow_none(int),
    "Optional[str]": allow_none(str),
    "Optional[bool]": allow_none(adv_bool),
    float: float,
    int: int,
    str: str,
    bool: adv_bool,
    Optional[float]: allow_none(float),
    Optional[int]: allow_none(int),
    Optional[str]: allow_none(str),
    Optional[bool]: allow_none(adv_bool),
}


class SettingsBase:
    def __init__(self):
        self.__fields_set__ = set()
        self.__fields__ = []
        for attr in self.__class__.__annotations__.keys():
            if attr.startswith("_"):
                continue

            self.__fields__.append(attr)
            setattr(self, attr, getattr(self.__class__, attr))

    def dict(self):
        result = {}
        for attr in self.__fields__:
            result[attr] = getattr(self, attr)
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
