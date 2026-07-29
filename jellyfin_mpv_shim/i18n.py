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
