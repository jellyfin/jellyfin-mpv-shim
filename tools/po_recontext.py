"""Carry a catalogue's translations across the addition of a `msgctxt`.

    python3 tools/po_recontext.py --plan tools/recontext_plans/<name>.json \
        --web-src ~/src/jellyfin-web [--dry-run]

**A context is part of gettext's key**, so giving one to a string that never
had one discards every existing translation of it -- 60 locales for `Default`,
66 for `Top`. That cost is what has kept two-sense msgids in this catalogue
unsplit (docs/i18n.md section 6), and this is what removes it: the volunteer's
words are moved onto the new keys instead of being dropped on the floor.

**It writes to the per-locale `.po` files**, which CLAUDE.md otherwise
forbids. Same standing as `tools/po_lint.py --fuzzy`: a bounded, deliberate
exception, run once per split, touching only the entries the plan names.
Weblate cannot do this itself -- it sees a new msgid and an obsolete one, with
nothing connecting them.

The plan says, per msgid, what each new context should inherit. Every entry it
writes lands in exactly one of four states, and which one is a claim about
evidence rather than a default:

``as-is``
    We know the existing translation carries this sense, so it moves over
    unmarked and the locale keeps working. The evidence is one of two things:
    jellyfin-web agrees with itself in this language (a `# seeded from
    jellyfin-web:` marker means the seeder found every key for that English
    saying the same thing, so both senses are that word); or the translation
    already existed at the commit named by ``since``, which is when the second
    sense first reached the source -- so it cannot have been written for it.

``seeded``
    jellyfin-web has its own key for *this* sense and this language
    translates it, so we take that rather than guess. Only `Default` has
    this, and it is the case worth having: 15 of jellyfin-web's languages
    translate `Default` and `MediaInfoDefault` differently, and ours were
    seeded from whichever won.

``fuzzy``
    The words are carried over but flagged. Both compilers drop fuzzy
    entries, so the string falls back to English until a human looks, and
    Weblate lists it as "Needs editing" with the old wording in front of
    them. This is where an honest "we do not know" goes.

There is deliberately no "drop it, we know it is wrong" state. Knowing that
a language distinguishes the senses means holding jellyfin-web's translation
of both keys -- and holding them means there is a right word to seed, so the
drop never has a case of its own. What looked like one was jellyfin-web
having translated only the sibling key, which is silence and not evidence.

Untranslated and obsolete entries are ignored, and an entry that already
carries the context is left exactly as it is, so a re-run cannot overwrite a
volunteer's later work.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tools.msgfmt import unescape, _quoted, PoError          # noqa: E402

MESSAGES = os.path.join(REPO, "jellyfin_mpv_shim", "messages")
MARKER = "# seeded from jellyfin-web: "


def _escape(text):
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n").replace("\t", "\\t"))


class Entry:
    """One `.po` stanza, kept as its own lines so it can be rewritten."""

    def __init__(self, lines):
        self.lines = lines
        self.ctx = None
        self.msgid = ""
        self.msgstr = ""
        self.flags = []
        self.obsolete = False
        self.seeded_key = None
        self._parse()

    def _parse(self):
        field = None
        for index, raw in enumerate(self.lines):
            line = raw.strip()
            if line.startswith("#~"):
                self.obsolete = True
                continue
            if line.startswith("#,"):
                self.flags += [f.strip() for f in line[2:].split(",")]
                continue
            if raw.startswith(MARKER):
                self.seeded_key = raw[len(MARKER):].strip()
                continue
            if line.startswith("#"):
                continue
            if line.startswith("msgctxt"):
                field = "ctx"
                self.ctx = unescape(_quoted(line[7:].strip(), index + 1),
                                    index + 1)
                continue
            if line.startswith("msgid_plural"):
                field = None
                continue
            if line.startswith("msgid"):
                field = "msgid"
                self.msgid = unescape(_quoted(line[5:].strip(), index + 1),
                                      index + 1)
                continue
            if line.startswith("msgstr"):
                field = "str"
                rest = line[6:].strip()
                if rest.startswith("["):
                    rest = rest.split("]", 1)[1].strip()
                self.msgstr = unescape(_quoted(rest, index + 1), index + 1)
                continue
            if line.startswith('"') and field is not None:
                text = unescape(_quoted(line, index + 1), index + 1)
                if field == "ctx":
                    self.ctx += text
                elif field == "msgid":
                    self.msgid += text
                elif field == "str":
                    self.msgstr += text

    @property
    def fuzzy(self):
        return "fuzzy" in self.flags


def parse_po(path):
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    entries, current = [], []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            entries.append(Entry(current))
            current = []
    if current:
        entries.append(Entry(current))
    return entries


def render(entries):
    out = []
    for entry in entries:
        out.extend(entry.lines)
        out.append("")
    return "\n".join(out)


def new_entry(ctx, msgid, msgstr, fuzzy=False, seeded_key=None):
    lines = []
    if seeded_key:
        lines.append(MARKER + seeded_key)
    if fuzzy:
        lines.append("#, fuzzy")
    lines.append('msgctxt "%s"' % _escape(ctx))
    lines.append('msgid "%s"' % _escape(msgid))
    lines.append('msgstr "%s"' % _escape(msgstr))
    return Entry(lines)


def existed_at(commit, path, ctx, msgid):
    """Whether `path` already translated (ctx, msgid) at `commit`.

    The dating evidence behind an ``as-is``: if the words were in the
    catalogue before the second sense reached the source, they were not
    written for it.
    """
    rel = os.path.relpath(path, REPO)
    try:
        blob = subprocess.run(
            ["git", "-C", REPO, "show", "%s:%s" % (commit, rel)],
            capture_output=True, check=True).stdout.decode("utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return False        # the locale did not exist yet, so neither did it
    for entry in [Entry(b.splitlines()) for b in blob.split("\n\n") if b.strip()]:
        if (entry.ctx, entry.msgid) == (ctx, msgid):
            return bool(entry.msgstr) and not entry.fuzzy and not entry.obsolete
    return False


def web_strings(web_src, locale):
    """jellyfin-web's strings for a locale directory name, or None."""
    from importlib.util import spec_from_file_location, module_from_spec

    spec = spec_from_file_location(
        "_seed", os.path.join(REPO, "seed_from_jellyfin_web.py"))
    seed = module_from_spec(spec)
    spec.loader.exec_module(seed)
    path = seed.web_file(pathlib.Path(web_src) / "src" / "strings", locale)
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def decide(rule, entry, web, since_ok):
    """``(msgstr, fuzzy, seeded_key, state)`` for one context of one locale."""
    key = rule.get("web_key")
    sibling = rule.get("web_sibling")
    if web is not None and key:
        mine = web.get(key)
        theirs = web.get(sibling) if sibling else None
        if theirs and mine == theirs:
            # Every jellyfin-web key for this English says the same thing
            # here, so the word we already have serves both senses.
            return entry.msgstr, False, None, "as-is"
        if theirs and mine:
            # jellyfin-web splits the senses in this language, so its own
            # word for THIS sense is the answer -- whichever of the two ours
            # happened to carry. Taking it is not overwriting a volunteer:
            # the key it is going onto did not exist until now.
            return mine, False, key, "seeded"
        # Only ONE of the two keys is translated here, which says nothing
        # about whether this language distinguishes the senses -- and an
        # untranslated sibling must not be read as one. Dropping on that
        # cost af/bn/ckb/dv/gsw/kn/mi/ms/ne/oc/sq/th/ug/uz a working
        # translation apiece before this fell through instead.
    if entry.seeded_key:
        # The seeder only writes a marker where every jellyfin-web key for
        # that English agreed in this language, so both senses are that word.
        return entry.msgstr, False, None, "as-is"
    if since_ok:
        return entry.msgstr, False, None, "as-is"
    return entry.msgstr, True, None, "fuzzy"


def apply_plan(path, plan, web_src, dry_run):
    locale = pathlib.Path(path).parts[-3]
    entries = parse_po(path)
    have = {(e.ctx, e.msgid) for e in entries if not e.obsolete}
    web = web_strings(web_src, locale) if web_src else None

    counts = {"as-is": 0, "seeded": 0, "fuzzy": 0}
    added = []
    for rule in plan:
        msgid = rule["msgid"]
        source = next((e for e in entries
                       if not e.obsolete and e.ctx is None
                       and e.msgid == msgid and e.msgstr and not e.fuzzy),
                      None)
        if source is None:
            continue
        for ctx_rule in rule["contexts"]:
            ctx = ctx_rule["ctx"]
            if (ctx, msgid) in have:
                continue        # already split, or a volunteer got there first
            since = ctx_rule.get("since")
            since_ok = bool(since) and existed_at(since, path, None, msgid)
            merged = dict(rule)
            merged.update(ctx_rule)
            text, fuzzy, seeded, state = decide(merged, source, web, since_ok)
            counts[state] += 1
            added.append(new_entry(ctx, msgid, text, fuzzy, seeded))
    if added and not dry_run:
        # Before the obsolete tail, where msgmerge would have put them. Not
        # cosmetic: a live entry after a fuzzy "#~" run was silently dropped
        # by tools/msgfmt.py until that was fixed, and GNU msgfmt and Weblate
        # both write obsolete entries last, so nothing else has to think
        # about the other order.
        at = next((i for i, e in enumerate(entries) if e.obsolete), len(entries))
        entries[at:at] = added
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(render(entries))
    return locale, counts, len(added)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plan", required=True,
                    help="JSON describing the splits (see the module docstring)")
    ap.add_argument("--web-src",
                    help="path to a jellyfin-web checkout, for the senses it "
                         "has a key of its own for")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    with open(args.plan, encoding="utf-8") as handle:
        plan = json.load(handle)

    totals = {"as-is": 0, "seeded": 0, "fuzzy": 0}
    written = 0
    for entry in sorted(os.listdir(MESSAGES)):
        path = os.path.join(MESSAGES, entry, "LC_MESSAGES", "base.po")
        if not os.path.isfile(path):
            continue
        try:
            locale, counts, n = apply_plan(path, plan, args.web_src,
                                           args.dry_run)
        except PoError as exc:
            print("%s: could not read: %s" % (entry, exc), file=sys.stderr)
            return 1
        for k, v in counts.items():
            totals[k] += v
        written += n
        if any(counts.values()):
            print("%-12s %s" % (locale, " ".join(
                "%s=%d" % (k, v) for k, v in counts.items() if v)))
    print("\n%s %d entr(ies): %s"
          % ("would write" if args.dry_run else "wrote", written,
             ", ".join("%s=%d" % kv for kv in totals.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
