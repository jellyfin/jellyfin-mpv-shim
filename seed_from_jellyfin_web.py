#!/usr/bin/env python3
"""Seed untranslated strings from jellyfin-web's translations.

About a quarter of this project's user-facing strings are word-for-word the
same as jellyfin-web's -- the Live TV screens, the item detail actions, the
guide settings -- because those screens were built from jellyfin-web's. Its
translators have already done that work in 106 languages under a compatible
licence (jellyfin-web is GPL-2.0-or-later; this is GPL-3.0), so a string we
have never translated can usually be filled in from theirs.

Run it against a jellyfin-web checkout::

    ./seed_from_jellyfin_web.py --web-src ~/src/jellyfin-web --dry-run
    ./seed_from_jellyfin_web.py --web-src ~/src/jellyfin-web

WHAT IT WILL NOT DO, and why each guard is here:

*Only untranslated, non-fuzzy entries.* It never overwrites a translation a
volunteer wrote, and never clobbers a fuzzy entry -- that already carries
msgmerge's ``#|`` note about what it was matched from, which is worth more
than a guess from another project.

*Only where jellyfin-web is unambiguous IN THAT LANGUAGE.* jellyfin-web has
64 English strings served by more than one key, and every single one of them
is translated differently in at least one language: 'Channels' is four keys,
one of which is "select audio channels" and reads that way in Luxembourgish.
So a string with several keys is seeded only into languages where all of its
keys agree -- per (string, language), not per string, which keeps the ~60
languages that translate them identically while skipping the ones that fork.
:data:`CONTEXT_KEYS` overrides that for msgids we have given an explicit
``msgctxt``, because there we know which sense we mean.

*Only if the placeholders survive.* Their translation has to carry the same
``{0}`` / ``%(name)s`` set as our msgid. This is not hypothetical: adding
xgettext's format flags to our own catalogue immediately flagged 21 existing
translations that had dropped or mangled a placeholder, one of which raises
ValueError when formatted.

*Everything it writes is marked fuzzy.* msgfmt keeps fuzzy entries out of the
.mo, so nothing reaches a user unreviewed; Weblate shows them as needing
review, where accepting a good suggestion is far quicker than translating
from scratch. The provenance goes in a translator comment naming the key it
came from -- that comment survives ``regen_pot.sh --merge`` (it travels in
the .po, which is the only place it can: the .pot is regenerated from source
by xgettext, so anything written there is gone on the next run).

**Seed LAST, and merge before sweeping again.** ``regen_pot.sh --merge``
treats master's .po as the definition and the working tree's only as a
compendium, and a compendium fills entries the definition has no translation
for. That is fine for the ordinary case -- master has those untranslated, so
the seed and its comment both survive -- but an entry master holds as *fuzzy
with a value* already counts as translated, so master's version wins and the
seed is silently reverted. The CONTEXT_KEYS entries are exactly that shape.
Running a sweep on an unmerged seed branch drops them; running it after the
branch is on master does not.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
MESSAGES = HERE / "jellyfin_mpv_shim" / "messages"

#: Comment written above each seeded entry. Grep-able, and the marker
#: re-runs use to recognise their own previous output.
MARKER = "# seeded from jellyfin-web: "

#: Our locale directory -> jellyfin-web's strings file, where the names
#: differ. Everything else matches on the name or its lowercased,
#: hyphen-separated form.
LOCALE_ALIASES = {
    "zh_Hans": "zh-cn", "zh_Hant": "zh-tw", "zh_Hant_HK": "zh-hk",
    "pt_BR": "pt-br", "pt_PT": "pt-pt", "es_419": "es-419",
    "es_AR": "es-ar", "es_MX": "es-mx", "en_GB": "en-gb", "nb_NO": "nb",
}

#: For msgids we have given a context, we know which sense we mean, so the
#: ambiguity guard does not apply -- we name the key instead. These are the
#: four collisions ``i18n._p`` exists for; jellyfin-web splits the first two
#: exactly the same way, which is how we found them.
CONTEXT_KEYS = {
    ("series recording rule setting", "Record"): "LabelRecord",
    ("series recording rule setting", "Channels"): "LabelChannels",
    ("dialog heading", "Download"): "Download",
    ("home screen section type", "None"): "None",
}

BRACE = re.compile(r"\{([^{}]*)\}")
PRINTF = re.compile(r"%(?:\(([^)]*)\))?[-#0 +]*[\d*]*(?:\.[\d*]+)?[hlL]?([a-zA-Z%])")


def placeholders(text):
    """The set of substitutions a string performs, as comparable tokens."""
    out = set()
    for m in BRACE.finditer(text):
        # "{0:0.1f}" and "{0}" are the same argument; the format spec is the
        # translator's to adjust, the field name is not.
        out.add("{%s}" % m.group(1).split(":")[0].split("!")[0])
    for m in PRINTF.finditer(text):
        if m.group(2) == "%":
            continue
        out.add("%%(%s)%s" % (m.group(1) or "", m.group(2)))
    return out


def po_escape(s):
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\t", "\\t"))


def po_unescape(s):
    return (s.replace('\\n', "\n").replace('\\t', "\t")
             .replace('\\"', '"').replace("\\\\", "\\"))


def read_field(lines, i, keyword):
    """``(value, next_index)`` for a possibly multi-line po field."""
    m = re.match(r'%s\s+"((?:[^"\\]|\\.)*)"\s*$' % keyword, lines[i])
    if not m:
        return None, i
    parts, i = [m.group(1)], i + 1
    while i < len(lines):
        m = re.match(r'"((?:[^"\\]|\\.)*)"\s*$', lines[i])
        if not m:
            break
        parts.append(m.group(1))
        i += 1
    return po_unescape("".join(parts)), i


class Entry:
    """One block of a .po file, kept as its original lines until edited."""

    def __init__(self, lines):
        self.lines = lines
        self.ctx = self.msgid = None
        self.msgstr = ""
        self.fuzzy = False
        self.msgstr_at = None
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#,") and "fuzzy" in line:
                self.fuzzy = True
            if line.startswith("msgctxt "):
                self.ctx, i = read_field(lines, i, "msgctxt")
                continue
            if line.startswith("msgid ") and self.msgid is None:
                self.msgid, i = read_field(lines, i, "msgid")
                continue
            if line.startswith("msgstr "):
                self.msgstr_at = i
                self.msgstr, i = read_field(lines, i, "msgstr")
                continue
            i += 1

    def seedable(self, have_exact_key=False):
        """Whether this entry may be written to.

        Untranslated and not fuzzy, normally: a volunteer's work is never
        overwritten, and a fuzzy entry already carries msgmerge's ``#|`` note
        about what it was matched from, which beats a guess from another
        project.

        ``have_exact_key`` lifts the fuzzy half of that, and only that.
        msgmerge fills a brand-new msgctxt entry by similarity, which for
        ours produced "Transkodierung" for Record and "Angesehen" for
        Channels -- it had nothing better to go on. When CONTEXT_KEYS names
        the jellyfin-web key outright we are not guessing, so we win.
        """
        if not self.msgid or self.msgstr_at is None:
            return False
        if any(l.startswith("#~") for l in self.lines):
            return False
        if self.msgstr:
            return bool(have_exact_key and self.fuzzy)
        return not self.fuzzy

    def seed(self, text, key):
        """Write ``text`` as a fuzzy translation, noting where it came from.

        Order matters and msgfmt enforces it: every ``#`` comment, and the
        ``#,`` flag line in particular, belongs ABOVE msgctxt/msgid. Appending
        the flag after the header block put ``#, fuzzy`` between msgid and
        msgstr, which is a syntax error in every entry it touched.
        """
        head, body = [], []
        for line in self.lines[:self.msgstr_at]:
            if line.startswith(MARKER):
                continue                       # a previous run's note
            (head if line.startswith("#") and not body else body).append(line)

        out = [MARKER + key]
        flagged = False
        for line in head:
            if line.startswith("#|"):
                # msgmerge's note about the string it matched this off. We
                # are replacing that match, so the note is now a lie.
                continue
            if line.startswith("#,"):
                flags = [f.strip() for f in line[2:].split(",") if f.strip()]
                if "fuzzy" not in flags:
                    flags.insert(0, "fuzzy")
                out.append("#, " + ", ".join(flags))
                flagged = True
                continue
            out.append(line)
        if not flagged:
            out.append("#, fuzzy")
        out.extend(body)
        out.append('msgstr "%s"' % po_escape(text))
        self.lines = out


def parse_po(path):
    entries, block = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "":
            if block:
                entries.append(Entry(block))
                block = []
        else:
            block.append(line)
    if block:
        entries.append(Entry(block))
    return entries


def web_file(webdir, locale):
    for cand in (LOCALE_ALIASES.get(locale, locale), locale,
                 locale.replace("_", "-").lower()):
        p = webdir / (cand + ".json")
        if p.exists():
            return p
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--web-src", required=True,
                    help="path to a jellyfin-web checkout")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be seeded and write nothing")
    args = ap.parse_args()

    webdir = pathlib.Path(args.web_src).expanduser() / "src" / "strings"
    en_path = webdir / "en-us.json"
    if not en_path.exists():
        sys.exit("error: %s not found -- is --web-src a jellyfin-web checkout?"
                 % en_path)

    en = json.loads(en_path.read_text(encoding="utf-8"))
    keys_for = {}
    for key, value in en.items():
        keys_for.setdefault(value, []).append(key)

    totals = {"seeded": 0, "ambiguous": 0, "placeholder": 0, "absent": 0}
    per_locale, skipped_locales = [], []

    for d in sorted(p for p in MESSAGES.iterdir() if p.is_dir()):
        po_path = d / "LC_MESSAGES" / "base.po"
        if not po_path.exists():
            continue
        wf = web_file(webdir, d.name)
        if wf is None:
            skipped_locales.append(d.name)
            continue
        web = json.loads(wf.read_text(encoding="utf-8"))

        entries = parse_po(po_path)
        seeded = 0
        for e in entries:
            override = CONTEXT_KEYS.get((e.ctx, e.msgid)) if e.ctx else None
            if not e.seedable(have_exact_key=bool(override)):
                continue
            if override:
                candidates = [override]
            else:
                candidates = keys_for.get(e.msgid)
                if not candidates:
                    totals["absent"] += 1
                    continue
            values = {web[k] for k in candidates if web.get(k)}
            if not values:
                totals["absent"] += 1
                continue
            if len(values) > 1:
                # jellyfin-web's own translators needed different words here.
                totals["ambiguous"] += 1
                continue
            text = values.pop()
            if placeholders(e.msgid) != placeholders(text):
                totals["placeholder"] += 1
                continue
            key = override or next(k for k in candidates if web.get(k))
            e.seed(text, key)
            seeded += 1

        if seeded and not args.dry_run:
            # Blocks are separated by a BLANK line -- joining them with a
            # single newline merges every entry into one and the file stops
            # being a .po at all.
            po_path.write_text(
                "\n\n".join("\n".join(e.lines) for e in entries) + "\n",
                encoding="utf-8")
        totals["seeded"] += seeded
        per_locale.append((d.name, seeded, wf.name))

    per_locale.sort(key=lambda r: -r[1])
    print("%-12s %-14s %s" % ("locale", "jellyfin-web", "seeded"))
    for loc, n, wf in per_locale:
        if n:
            print("%-12s %-14s +%d" % (loc, wf, n))
    print()
    print("seeded              %6d" % totals["seeded"])
    print("skipped, ambiguous  %6d  (jellyfin-web forks the wording here)"
          % totals["ambiguous"])
    print("skipped, format     %6d  (their translation lost a placeholder)"
          % totals["placeholder"])
    print("skipped, no string  %6d  (jellyfin-web has no such English)"
          % totals["absent"])
    if skipped_locales:
        print("no jellyfin-web locale file: %s" % ", ".join(skipped_locales))
    if args.dry_run:
        print("\n(dry run -- nothing written)")
    else:
        print("\nEverything written is marked fuzzy: msgfmt keeps it out of "
              "the .mo\nand Weblate shows it as needing review. Check with "
              "'git diff' and\n'msgfmt --check' before committing.")


if __name__ == "__main__":
    main()
