import gettext
import logging
import sys

from .conf import settings

log = logging.getLogger("i18n")

translation = gettext.NullTranslations()


def _system_languages():
    """What the system says the interface language is, or None.

    **On everything but Windows this is gettext's own environment lookup,
    and that is the fix rather than a simplification.**
    ``locale.getdefaultlocale()`` was here for one good reason -- it reads
    Windows' UI language, which no environment variable carries -- and it is
    wrong three separate ways on Linux, all measured:

    - **It ignores ``LANGUAGE``**, which is the variable GNOME and KDE set
      for the display language. Plasma puts the user's pick there
      (``kcm_regionandlang``) and leaves ``LANG`` to formats, so changing the
      display language on KDE did nothing to this app at all.
    - **``LANG`` only counts if the locale has been generated.**
      ``LANG=de_DE.UTF-8`` answers ``('C', 'UTF-8')`` on a box with only
      ``en_US`` generated -- the Debian and container default, and the
      shipped Flatpak, whose sandbox has twenty English locales and nothing
      else. Both paths failed there, so the Flatpak was English whatever the
      user asked for.
    - **It is deprecated and removed in Python 3.15**, so it stops working
      rather than degrading.

    gettext honours ``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES`` and ``LANG`` in
    that order and needs no generated locale, which is exactly the answer the
    desktop meant. Returning None hands it that lookup.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import locale

        lc = locale.getdefaultlocale()
    except Exception:      # removed in 3.15; a missing answer is not fatal
        log.debug("no system locale available", exc_info=True)
        return None
    if lc is not None and lc[0]:
        return [lc[0]]
    return None


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
