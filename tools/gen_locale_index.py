"""Write `jellyfin_mpv_shim/locale_index.py`: what the language picker lists.

    python3 tools/gen_locale_index.py            # regenerate
    python3 tools/gen_locale_index.py --check    # name every locale? (a test)

**Why a generated file rather than a runtime scan.** The picker wants two
things per locale: a name someone can recognise, and whether the
translation is complete. Completeness comes from the `.po` files, which are in git but
**not shipped** (`pyproject.toml` packages `*.mo` and `base.pot` only), and
counting it means parsing 86 catalogs. So it is computed here, checked in, and
refreshed by `gen_pkg.sh` alongside the `.mo` compile -- which means a release
always carries current numbers and a source checkout carries the numbers from
the last regeneration.

**Why only complete locales are offered.** A partly translated interface is
a mixed-language one, so the picker lists a locale only when it translates
every string the code asks for. Completeness is counted from the source, not
from `base.pot`, which is regenerated on request and lags the code; a
catalog "complete" against a stale template would otherwise qualify. No
percentage is stored or shown. `tests/test_interface_language.py` regenerates
this file in memory and fails if the checked-in one differs.

**Endonyms, not English names**, because someone reaching for this control is
by definition someone who cannot read the current one. Most of the table below
was read out of `iso-codes` -- its language names translated into that same
language *are* the endonym -- and the rest were written by hand. Two are still
English (`sdh`, `pon`); an endonym nobody could attest is worse than a name in
the wrong language, so they wait for someone who knows.
"""

import argparse
import ast
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


def source_msgids():
    """Every string the shipped code asks to have translated.

    Read from the source with ``ast``, not from ``base.pot``: the template is
    only regenerated on request (see ``regen_pot.sh``), so it lags the code,
    and a catalog complete against a stale template is not complete. This is
    the same call set ``regen_pot.sh`` extracts (``_(s)`` and ``_p(ctx, s)``,
    keyed the way a ``.mo`` keys a context) without needing ``xgettext``.
    """
    ids = set()
    pkg = os.path.join(ROOT, "jellyfin_mpv_shim")
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            with open(os.path.join(dirpath, fname), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                args = node.args
                if name == "_" and args:
                    if _is_str(args[0]):
                        ids.add(args[0].value)
                elif name == "_p" and len(args) > 1:
                    if _is_str(args[0]) and _is_str(args[1]):
                        ids.add(args[0].value + msgfmt.CONTEXT_SEP
                                + args[1].value)
    return ids


def _is_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def translated(po_path):
    """The keys this locale actually contributes to its ``.mo``.

    `msgfmt.parse` is the compiler's own reader, so this is exactly what
    reaches the running app: non-empty, non-fuzzy, non-obsolete.
    """
    with open(po_path, "r", encoding="utf-8") as fh:
        catalog = msgfmt.parse(fh.read())
    return {k for k in catalog if k != ""}


def build():
    """``(count of source strings, [(code, name, complete)])``.

    ``complete`` means every string in the source is translated. There is no
    partial credit: a mostly-translated interface is a mixed-language one.
    """
    wanted = source_msgids()
    rows = []
    for code, po in locales():
        name = ENDONYMS.get(code)
        if name is None:
            continue
        rows.append((code, name, wanted <= translated(po)))
    rows.sort(key=lambda r: r[1].casefold())
    return len(wanted), rows


def render(total, rows):
    out = [
        '"""Languages the interface can be shown in, and whether each is complete.',
        "",
        "Generated by ``tools/gen_locale_index.py`` -- do not edit by hand.",
        "Regenerate after a translation sync or after adding a string;",
        "``gen_pkg.sh`` does it as part of a build.",
        "",
        "A locale is complete only when it translates every one of the %d" % total,
        "strings the code asks for, counted from the source (not from",
        "``base.pot``, which lags it) against what reaches the ``.mo``:",
        "non-empty, non-fuzzy, non-obsolete entries. The picker offers only",
        "complete locales.",
        '"""',
        "",
        "#: ``(locale directory, name in that language, complete)``,",
        "#: ordered by name so a reader finds their own script together.",
        "LOCALES = [",
    ]
    for code, name, complete in rows:
        out.append('    ("%s", "%s", %s),' % (code, name, complete))
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
    print("wrote %s: %d locales, %d complete, %d strings in the source"
          % (os.path.relpath(OUT, ROOT), len(rows),
             sum(1 for r in rows if r[2]), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
