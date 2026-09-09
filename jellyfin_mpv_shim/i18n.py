import gettext
import logging
import os
import sys

from .conf import settings

log = logging.getLogger("i18n")

translation = gettext.NullTranslations()

# gettext's own search order. Nothing on Windows sets these, which is why the
# block below exists at all; when something does set one, it wins.
_GETTEXT_ENVVARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def _system_languages():
    """What the system says the interface language is, or None.

    **None means "gettext, you work it out", and on everything but Windows
    that is the fix rather than a simplification.** gettext honours
    ``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES`` and ``LANG`` in that order and
    needs no generated locale, which is exactly the answer the desktop meant.
    ``locale.getdefaultlocale()`` used to answer here and was wrong three ways
    on Linux, all measured -- most visibly it ignores ``LANGUAGE``, which is
    where KDE puts the display language.

    Windows sets none of those four, and it is also the platform where the
    display language and the regional format are **two independent settings**.
    ``locale.getdefaultlocale()`` read the second one, so a German-format
    English-display machine got a German UI. It is removed in Python 3.15
    anyway. ``docs/i18n.md`` section 7 has the measurements and the two
    replacements that look right and are not.
    """
    if not sys.platform.startswith("win"):
        return None
    if any(os.environ.get(name) for name in _GETTEXT_ENVVARS):
        return None
    try:
        return _windows_ui_languages()
    except Exception:  # a missing answer is not fatal; English is
        log.debug("no Windows UI language available", exc_info=True)
        return None


def _windows_ui_languages():
    """Windows' display languages as catalog names, best match first."""
    languages = []
    for tag in _preferred_ui_tags() or ():
        for name in _catalog_names(tag):
            if name not in languages:
                languages.append(name)
    return languages or None


def _catalog_names(tag):
    """A BCP-47 tag as the catalog names to try, most specific first.

    ``zh-Hans-CN`` -> ``zh_Hans_CN``, ``zh_Hans``, ``zh``. Widened here
    rather than left to gettext, which splits a code on its **first**
    underscore only: it would offer ``zh_Hans_CN`` and then ``zh``, skipping
    the catalog this app actually ships. It also does nothing whatever with a
    hyphen, so the separator has to change on the way through.
    """
    parts = [part for part in tag.replace("-", "_").split("_") if part]
    return ["_".join(parts[:count]) for count in range(len(parts), 0, -1)]


def _preferred_ui_tags():
    """``GetUserPreferredUILanguages`` as a list of tags, or None.

    Not the single ``GetUserDefaultUILanguage`` that Mercurial and MComix
    reach for: the list is the user's own fallback order, and its BCP-47
    names keep the script subtag that a numeric language id collapses --
    which is the whole difference between ``zh_Hans`` and ``zh_Hant``.
    """
    import ctypes
    from ctypes import wintypes

    MUI_LANGUAGE_NAME = 0x8

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_languages = kernel32.GetUserPreferredUILanguages
    get_languages.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.ULONG),
        ctypes.POINTER(ctypes.c_wchar),
        ctypes.POINTER(wintypes.ULONG),
    ]
    get_languages.restype = wintypes.BOOL

    count, size = wintypes.ULONG(), wintypes.ULONG()
    # First call sizes the buffer. `size` counts characters, not bytes, and
    # what comes back is null-separated *and* null-terminated, so the split
    # yields two trailing empties -- `_catalog_names` drops them.
    if not get_languages(
        MUI_LANGUAGE_NAME, ctypes.byref(count), None, ctypes.byref(size)
    ):
        return None
    buffer = (ctypes.c_wchar * size.value)()
    if not get_languages(
        MUI_LANGUAGE_NAME, ctypes.byref(count), buffer, ctypes.byref(size)
    ):
        return None
    return buffer[: size.value].split("\0")


def configure():
    global translation
    from .utils import get_resource

    messages_dir = get_resource("messages")
    # Falsy, not `is not None`: the picker writes None for "use the system
    # language", and a cleared or hand-edited conf.json can hold "" -- which
    # as an explicit language matches no catalog and so silently means
    # English forever.
    languages = [settings.lang] if settings.lang else _system_languages()
    translation = gettext.translation(
        "base", messages_dir, languages=languages, fallback=True
    )


def get_translation():
    return translation


def _(string: str) -> str:
    return translation.gettext(string)


def _p(context: str, string: str) -> str:
    """Translate ``string`` in a named ``context``.

    gettext keys on the English, so one word used in two senses collapses to
    one entry and no language can tell them apart. "Record" is a form label
    on the timer editor's picker ("Record: New episodes only") and an
    imperative verb on the program page's button; jellyfin-web needs two keys
    for exactly that pair and Filipino translates them differently. Same for
    "Channels" (picker label vs the Live TV tab), "Download" (button verb vs
    dialog heading) and "None" (no track vs no home section, which is a
    gender-agreement problem in Italian).

    Use it only where the senses genuinely differ. A context is part of the
    key, so adding one to a string that did not need it throws away every
    existing translation of it.

    ``context`` is never shown to the user; it is a note to the translator.
    Extraction is ``--keyword=_p:1c,2`` in ``regen_pot.sh`` -- and note that
    ``pygettext3`` cannot extract this at all, which is why that script uses
    ``xgettext``.
    """
    return translation.pgettext(context, string)
