import gettext
import locale

from .conf import settings

translation = gettext.NullTranslations()


def configure():
    global translation
    from .utils import get_resource

    messages_dir = get_resource("messages")
    lang = None

    if settings.lang is not None:
        lang = settings.lang
    else:
        # This is more robust than the built-in language detection in gettext.
        # Specifically, it supports Windows correctly.
        lc = locale.getdefaultlocale()
        if lc is not None and lc[0] is not None:
            lang = lc[0]

    if lang is not None:
        translation = gettext.translation(
            "base", messages_dir, languages=[lang], fallback=True
        )
    else:
        translation = gettext.translation("base", messages_dir, fallback=True)


def get_translation():
    return translation


def _(string: str) -> str:
    return translation.gettext(string)
