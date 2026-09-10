"""Carry the translator context in `base.pot` across a regeneration.

    python3 tools/pot_context.py --restore new.pot --from base.pot -o base.pot
    python3 tools/pot_context.py --check base.pot

**A Weblate translator sees the English string and a filename.** No line
number (`--add-location=file`, docs/i18n.md section 2), no enclosing function,
no screenshot -- so "Off" arrives as three letters that must serve five
different settings dropdowns, and `%s` arrives holding nothing. The `#.`
comments in `base.pot` are the only channel that reaches them, and this is
what keeps those comments alive.

**Why the comments live in the `.pot` and not in the source.** xgettext can
lift `# TRANSLATORS:` comments out of Python with `--add-comments`, which is
the idiomatic answer and was not taken, for two reasons:

- 158 of the msgids are extracted from more than one file. Identical comments
  at every site collapse to one `#.` line and differing ones stack, so the
  source form is only correct while every site is edited together -- and the
  one that drifts reads as a second sense the translator must reconcile.
- It is ~1,100 comments across 65 files, dwarfing the code comments in the
  ones that hold a lot of strings (config.py alone has 234 strings).

So the `.pot` is edited by hand and regeneration preserves what is there.
That makes it a generated file with hand-written content in it, which is the
cost; `--check` and `tests/test_pot_context.py` are what make the cost safe,
because the failure mode -- a comment silently lost -- is then loud.

**Entries are keyed on `(msgctxt, msgid)`, which is gettext's own key.** A
reworded string therefore *loses* its comment rather than keeping a stale one,
and `--restore` names every comment it had to drop. That direction is
deliberate: docs/i18n.md section 5 records the opposite failure, where
msgmerge carried a note across a reword and left `Blend Frames` credited to
the key for `Dropped frames`. A dropped note is a line in the report; a
retained one is wrong in a file nobody rereads.
"""

import argparse
import os
import re
import sys


class PotError(Exception):
    pass


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def unescape(text):
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 1
            if index >= len(text):
                raise PotError("trailing backslash in %r" % text)
            out.append(_ESCAPES.get(text[index], text[index]))
        else:
            out.append(char)
        index += 1
    return "".join(out)


_QUOTED = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*$')


def _quoted(line):
    match = _QUOTED.match(line)
    if match is None:
        raise PotError("expected a quoted string, got %r" % line.strip())
    return unescape(match.group(1))


class Block:
    """One `.pot` stanza: its lines, its key, and where a `#.` would go."""

    def __init__(self, lines):
        self.lines = lines
        self.msgctxt = None
        self.msgid = ""
        self.comments = []          # the `#.` text, without the marker
        self._parse()

    def _parse(self):
        field = None
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("#."):
                self.comments.append(stripped[2:].strip())
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith("msgctxt"):
                field = "msgctxt"
                self.msgctxt = _quoted(stripped[7:])
                continue
            if stripped.startswith("msgid_plural"):
                field = None
                continue
            if stripped.startswith("msgid"):
                field = "msgid"
                self.msgid = _quoted(stripped[5:])
                continue
            if stripped.startswith("msgstr"):
                field = None
                continue
            if stripped.startswith('"') and field is not None:
                text = _quoted(stripped)
                if field == "msgctxt":
                    self.msgctxt += text
                else:
                    self.msgid += text

    @property
    def key(self):
        return (self.msgctxt, self.msgid)

    @property
    def is_header(self):
        return self.msgid == "" and self.msgctxt is None

    def with_comments(self, comments):
        """These lines with `comments` as the entry's only `#.` block.

        PO order is `#` translator, `#.` extracted, `#:` reference, `#,`
        flag, and every tool that reads one expects that. The index is
        computed on the list with the old `#.` lines already dropped: taking
        it from `self.lines` instead worked on the first pass and slid the
        block one `#:` line further down on every pass after, because the
        lines it counted were no longer the lines it indexed into.
        """
        kept = [line for line in self.lines
                if not line.strip().startswith("#.")]
        at = 0
        while at < len(kept):
            stripped = kept[at].strip()
            if stripped.startswith("#") and not stripped.startswith(
                    ("#:", "#,", "#~")):
                at += 1                 # a plain translator comment
                continue
            break
        return kept[:at] + ["#. " + text if text else "#."
                            for text in comments] + kept[at:]


def parse(path):
    """Every stanza of a `.pot`, in file order, blank lines dropped."""
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    blocks = []
    current = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(Block(current))
            current = []
    if current:
        blocks.append(Block(current))
    return blocks


def render(blocks):
    out = []
    for block in blocks:
        out.extend(block.lines)
        out.append("")
    return "\n".join(out)


def restore(new_path, old_path, out_path):
    """Put the old template's `#.` comments back onto the new one.

    Returns ``(restored, orphans, missing)`` -- counts of comments carried
    over, comments whose string no longer exists, and entries that end up
    with no comment at all.
    """
    new_blocks = parse(new_path)
    try:
        old_blocks = parse(old_path)
    except OSError:
        old_blocks = []

    old = {}
    for block in old_blocks:
        if block.is_header or not block.comments:
            continue
        old[block.key] = block.comments

    seen = set()
    restored = 0
    for block in new_blocks:
        if block.is_header:
            continue
        comments = old.get(block.key)
        if comments and not block.comments:
            block.lines = block.with_comments(comments)
            block.comments = list(comments)
            restored += 1
        if block.key in old:
            seen.add(block.key)

    orphans = [key for key in old if key not in seen]
    missing = [block.key for block in new_blocks
               if not block.is_header and not block.comments]

    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(render(new_blocks))
    return restored, orphans, missing


def uncommented(path):
    """``[(msgctxt, msgid), ...]`` for every entry with no `#.` comment."""
    return [block.key for block in parse(path)
            if not block.is_header and not block.comments]


def _show(keys, limit=20):
    for msgctxt, msgid in keys[:limit]:
        label = "%r" % msgid[:70]
        if msgctxt is not None:
            label = "[%s] %s" % (msgctxt, label)
        print("    " + label)
    if len(keys) > limit:
        print("    ... and %d more" % (len(keys) - limit))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pot")
    parser.add_argument("--restore", metavar="OLD",
                        help="carry OLD's #. comments onto the given template")
    parser.add_argument("-o", "--out",
                        help="where to write --restore's result "
                             "(default: overwrite the given template)")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if any entry has no #. comment")
    args = parser.parse_args(argv)

    if args.restore:
        out = args.out or args.pot
        restored, orphans, missing = restore(args.pot, args.restore, out)
        print("  kept %d translator comment(s)" % restored)
        if orphans:
            # Loud, and by string: this is a reworded msgid, and the note
            # that described the old wording is now gone for good.
            print("  DROPPED %d comment(s) whose string no longer exists:"
                  % len(orphans))
            _show(sorted(orphans, key=lambda k: (k[0] or "", k[1])))
        if missing:
            print("  %d entr(ies) still have no context" % len(missing))
        return 0

    if args.check:
        missing = uncommented(args.pot)
        if missing:
            print("%s: %d entr(ies) have no translator context:"
                  % (os.path.relpath(args.pot), len(missing)), file=sys.stderr)
            for msgctxt, msgid in missing[:40]:
                label = "%r" % msgid[:70]
                if msgctxt is not None:
                    label = "[%s] %s" % (msgctxt, label)
                print("    " + label, file=sys.stderr)
            if len(missing) > 40:
                print("    ... and %d more" % (len(missing) - 40),
                      file=sys.stderr)
            return 1
        print("%s: every entry has translator context."
              % os.path.relpath(args.pot))
        return 0

    parser.error("give --restore or --check")


if __name__ == "__main__":
    sys.exit(main())
