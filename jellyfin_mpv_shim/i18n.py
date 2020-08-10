import gettext
import builtins

from .conf import settings

translation = gettext.NullTranslations()


def configure():
    global translation
    from .utils import get_resource
    messages_dir = get_resource("messages")

    if settings.lang is not None:
        translation = gettext.translation("base", messages_dir, languages=[settings.lang], fallback=True)
    else:
        translation = gettext.translation("base", messages_dir, fallback=True)


def get_translation():
    return translation


def _(string: str) -> str:
    return translation.gettext(string)
