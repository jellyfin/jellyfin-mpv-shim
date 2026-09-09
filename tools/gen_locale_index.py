"""Write `jellyfin_mpv_shim/locale_index.py`: what the language picker lists.

    python3 tools/gen_locale_index.py            # regenerate
    python3 tools/gen_locale_index.py --check    # name every locale? (a test)

**Why a generated file rather than a runtime scan.** The picker wants two
things per locale: a name someone can recognise, and how complete the
translation is. Completeness comes from the `.po` files, which are in git but
**not shipped** (`pyproject.toml` packages `*.mo` and `base.pot` only), and
counting it means parsing 86 catalogs. So it is computed here, checked in, and
refreshed by `gen_pkg.sh` alongside the `.mo` compile -- which means a release
always carries current numbers and a source checkout carries the numbers from
the last regeneration.

**Why the numbers are shown at all.** 41 of the 86 locales sit between 25% and
50%, which is the jellyfin-web seed line: a completeness *threshold* set
anywhere useful hides French, Russian, Polish and Japanese, and set low enough
to keep them means nothing. Listing everything with its number is honest, needs
no arbitrary bar, and tells a would-be translator where the gaps are -- and
that is the population most likely to open this menu.

**Endonyms, not English names**, because someone reaching for this control is
by definition someone who cannot read the current one. Most of the table below
was read out of `iso-codes` -- its language names translated into that same
language *are* the endonym -- and the rest were written by hand. Two are still
English (`sdh`, `pon`); an endonym nobody could attest is worse than a name in
the wrong language, so they wait for someone who knows.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tools import msgfmt                                        # noqa: E402

MESSAGES = os.path.join(ROOT, "jellyfin_mpv_shim", "messages")
OUT = os.path.join(ROOT, "jellyfin_mpv_shim", "locale_index.py")

#: locale directory -> the name of that language, in that language.
#:
#: Seeded from `iso-codes` (`/usr/share/iso-codes` plus its own translations,
#: so "de" is whatever the German iso_639-3 catalog calls German) and
#: hand-completed for the locales it had no translation for. Regional
#: variants carry the region in the same language, since the whole point is
#: that the reader cannot read English.
ENDONYMS = {
    "af": "Afrikaans",
    "am": "አማርኛ",
    "ar": "العربية",
    "az": "Azərbaycan dili",
    "be": "Беларуская",
    "bg": "Български",
    "bn": "বাংলা",
    "br": "Brezhoneg",
    "bs": "Bosanski",
    "ca": "Català",
    "ckb": "کوردیی ناوەندی",
    "cs": "Čeština",
    "cy": "Cymraeg",
    "da": "Dansk",
    "de": "Deutsch",
    "dv": "ދިވެހި",
    "el": "Ελληνικά",
    "en_GB": "English (United Kingdom)",
    "en@pirate": "English (Pirate)",
    "eo": "Esperanto",
    "es": "Español",
    "es_419": "Español (Latinoamérica)",
    "es_AR": "Español (Argentina)",
    "es_MX": "Español (México)",
    "et": "Eesti",
    "eu": "Euskara",
    "fa": "فارسی",
    "fi": "Suomi",
    "fil": "Filipino",
    "fr": "Français",
    "ga": "Gaeilge",
    "gl": "Galego",
    "gsw": "Schwiizerdütsch",
    "he": "עברית",
    "hi": "हिंदी",
    "hr": "Hrvatski",
    "hu": "Magyar",
    "id": "Bahasa Indonesia",
    "it": "Italiano",
    "ja": "日本語",
    "jbo": "la .lojban.",
    "ka": "ქართული",
    "kk": "Қазақ тілі",
    "km": "ភាសាខ្មែរ",
    "kn": "ಕನ್ನಡ",
    "ko": "한국어",
    "kw": "Kernewek",
    "lb": "Lëtzebuergesch",
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "lzh": "文言",
    "mi": "Reo Māori",
    "ml": "മലയാളം",
    "mn": "Монгол",
    "mr": "मराठी",
    "ms": "Bahasa Melayu",
    "nb_NO": "Norsk bokmål",
    "nds": "Plattdüütsch",
    "ne": "नेपाली",
    "nl": "Nederlands",
    "nn": "Norsk nynorsk",
    "oc": "Occitan",
    "pl": "Polski",
    # English, deliberately: no endonym for Pohnpeian could be attested here.
    "pon": "Pohnpeian",
    "pt": "Português",
    "pt_BR": "Português (Brasil)",
    "pt_PT": "Português (Portugal)",
    "ro": "Română",
    "ru": "Русский",
    # English, deliberately -- see "pon" above.
    "sdh": "Southern Kurdish",
    "sk": "Slovenčina",
    "sl": "Slovenščina",
    "sq": "Shqip",
    "sr": "Српски",
    "sv": "Svenska",
    "sw": "Kiswahili",
    "ta": "தமிழ்",
    "th": "ไทย",
    "tr": "Türkçe",
    "ug": "ئۇيغۇرچە",
    "uk": "Українська",
    "uz": "Oʻzbekcha",
    "vi": "Tiếng Việt",
    "zh_Hans": "简体中文",
    "zh_Hant": "繁體中文",
    "zh_Hant_HK": "繁體中文（香港）",
}


def locales():
    """Every locale directory that has a catalog, in directory order."""
    out = []
    for name in sorted(os.listdir(MESSAGES)):
        po = os.path.join(MESSAGES, name, "LC_MESSAGES", "base.po")
        if os.path.exists(po):
            out.append((name, po))
    return out


def total_msgids(pot_path):
    """How many entries the template has, which is the denominator.

    Counted off `msgid` lines rather than through the parser: `msgfmt.parse`
    drops untranslated entries by design, and in a `.pot` that is all of
    them. The header (`msgid ""`) is not a string anybody translates.
    """
    total = 0
    with open(pot_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("msgid ") and not line.startswith("msgid_plural"):
                total += 1
    return max(0, total - 1)


def translated(po_path):
    """How many entries this locale actually contributes.

    `msgfmt.parse` is the compiler's own reader, so this counts exactly what
    reaches the `.mo`: non-empty, non-fuzzy, non-obsolete. Counting anything
    else would advertise a completeness the running app does not have.
    """
    with open(po_path, "r", encoding="utf-8") as fh:
        catalog = msgfmt.parse(fh.read())
    return len([k for k in catalog if k != ""])


def build():
    total = total_msgids(os.path.join(MESSAGES, "base.pot"))
    rows = []
    for code, po in locales():
        name = ENDONYMS.get(code)
        if name is None:
            continue
        done = translated(po)
        pct = int(round(100.0 * done / total)) if total else 0
        rows.append((code, name, pct))
    rows.sort(key=lambda r: r[1].casefold())
    return total, rows


def render(total, rows):
    out = [
        '"""Languages the interface can be shown in, and how complete each is.',
        "",
        "Generated by ``tools/gen_locale_index.py`` -- do not edit by hand.",
        "Regenerate after a translation sync; ``gen_pkg.sh`` does it as part of",
        "a build, so a release always ships current numbers.",
        "",
        "``PERCENT`` counts what actually reaches the ``.mo``: non-empty,",
        "non-fuzzy, non-obsolete entries, against %d in the template." % total,
        '"""',
        "",
        "#: ``(locale directory, name in that language, percent translated)``,",
        "#: ordered by name so a reader finds their own script together.",
        "LOCALES = [",
    ]
    for code, name, pct in rows:
        out.append('    ("%s", "%s", %d),' % (code, name, pct))
    out += ["]", ""]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="only report locales with no endonym")
    args = ap.parse_args(argv)
    if args.check:
        missing = [c for c, _po in locales() if c not in ENDONYMS]
        for code in missing:
            print("no endonym for %s" % code)
        return 1 if missing else 0
    total, rows = build()
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(render(total, rows))
    print("wrote %s: %d locales, %d msgids in the template"
          % (os.path.relpath(OUT, ROOT), len(rows), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
