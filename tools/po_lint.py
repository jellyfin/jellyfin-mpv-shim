"""Find translations that raise when the app formats them, and flag them.

    python3 tools/po_lint.py --check   jellyfin_mpv_shim/messages/*/LC_MESSAGES/base.po
    python3 tools/po_lint.py --fuzzy   <same>      # rewrite in place
    python3 tools/po_lint.py --filter in.po -o out.po

**Four locales ship translations that raise today** -- `ar`, `gl`, `ms` and
`sk` -- and nothing notices: `msgfmt` exits 0, the entry lands in the `.mo`,
and gettext hands it back to be formatted and raise. One of them is
``Page %(page)d of %(total)d`` in the reader's bottom bar, so in Galician,
Malay and Arabic the reader raises **on every repaint**.

**Filter, do not gate** ([iw]). A broken translation must never reach a build,
and the way to guarantee that is to drop the entry at compile time rather than
fail the build: gettext then returns the msgid, so that one string falls back
to English and the rest of the locale is unaffected. Failing instead would
hand Weblate volunteers a way to break a release, and would mean fixing `.po`
files by hand -- which is Weblate's job, not ours (docs/i18n.md section 4).

**Fuzzy is the drop.** Marking an entry `#, fuzzy` does both jobs at once,
which is why no new mechanism is needed: Weblate shows a fuzzy entry as "Needs
editing", so it enters the translator's queue and clears when someone fixes
it; and **both compile paths already exclude fuzzy** -- `tools/msgfmt.py`
emits an entry only ``if value and (is_header or not entry.fuzzy)``, and GNU
`msgfmt` needs an opt-in ``-f`` to include one.

Two things this deliberately does **not** do:

- **It does not diff placeholder sets.** It attempts the substitution the app
  will attempt, with arguments synthesised from the *msgid*, and drops only
  what raises. That is what distinguishes the four real crashers from the
  `ru` entry with ``{0: 0.1f}`` in it, which reads like a typo and formats
  perfectly -- a space is a valid format-spec flag. A placeholder diff also
  trips on a literal ``%`` in "125%" and on a stray brace in prose.
- **It does not blame a translation for a msgid it cannot format itself.**
  If the synthesised arguments do not work on the English either, the
  synthesis is wrong and the entry is skipped, so a gap here can only ever
  miss a bad translation and never drop a good one.
"""

import argparse
import os
import re
import string
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tools.msgfmt import unescape, _quoted, PoError            # noqa: E402

#: One printf conversion. ``%%`` is matched too, so it can be counted out
#: rather than counted as an argument.
_PERCENT = re.compile(r"%(?:\(([^)]*)\))?[-+ #0]*[\d*]*(?:\.[\d*]+)?"
                      r"[hlL]?([a-zA-Z%])")


def _percent_args(text):
    """The right-hand side of ``%`` that satisfies every conversion here.

    ``1`` for every value: it is accepted by ``%s``, ``%d``, ``%i``, ``%f``
    and ``%x`` alike, which is every conversion this codebase uses. A named
    set and a positional one never mix in one string, and Python raises if
    they do -- which is a real crash and should be reported as one.
    """
    names, count = [], 0
    for name, conv in _PERCENT.findall(text):
        if conv == "%":
            continue
        if name:
            names.append(name)
        else:
            count += 1
    if names:
        return {n: 1 for n in names}
    return tuple([1] * count)


def _brace_args(text):
    """``(args, kwargs)`` for a ``str.format`` template."""
    args, kwargs = [], {}
    auto = 0
    for _lit, field, _spec, _conv in string.Formatter().parse(text):
        if field is None:
            continue
        head = field.split(".")[0].split("[")[0]
        if head == "":
            auto += 1
        elif head.isdigit():
            args += [1] * (int(head) + 1 - len(args))
        else:
            kwargs[head] = 1
    args += [1] * max(0, auto - len(args))
    return args, kwargs


def _style(msgid, flags):
    """Which formatting the app will apply, from xgettext's own flags.

    The flags are authoritative and already computed -- ``python-format`` and
    ``python-brace-format`` are written by extraction and preserved by
    Weblate. Sniffing the msgid is the fallback for an entry that predates a
    flag or came in from a seed.
    """
    # The negative flags first, and they are the reason the sniff below
    # cannot stand on its own: `no-python-format` is what xgettext writes
    # for a string with a literal percent in it -- "100% on X11" reads as a
    # `% o` conversion -- and the app never formats those, so nothing in
    # them can raise (docs/settings-curation.md section 4).
    if "no-python-format" in flags or "no-python-brace-format" in flags:
        return None
    if "python-brace-format" in flags:
        return "brace"
    if "python-format" in flags:
        return "percent"
    if any(conv != "%" for _n, conv in _PERCENT.findall(msgid)):
        return "percent"
    if any(f is not None
           for _l, f, _s, _c in string.Formatter().parse(msgid)):
        return "brace"
    return None


def _raises(text, style, args, kwargs):
    try:
        if style == "percent":
            # `args` is already the whole right-hand side -- a mapping for
            # named conversions, a tuple for positional ones.
            text % args
        else:
            text.format(*args, **kwargs)
    except Exception as exc:                                   # noqa: BLE001
        return exc
    return None


def check_entry(msgid, msgstrs, flags):
    """The exception this entry would raise in the app, or None.

    ``msgstrs`` is every form -- a plural entry has several and one bad form
    is enough, since gettext picks by count at runtime and the app cannot
    know which.
    """
    style = _style(msgid, flags)
    if style is None:
        return None
    if style == "percent":
        args, kwargs = _percent_args(msgid), {}
    else:
        args, kwargs = _brace_args(msgid)
    # The control: if the English cannot be formatted with these, the
    # synthesis is wrong and nothing here may be blamed on a translator.
    if _raises(msgid, style, args, kwargs) is not None:
        return None
    for text in msgstrs:
        if not text:
            continue
        exc = _raises(text, style, args, kwargs)
        if exc is not None:
            return exc
    return None


class Entry:
    """One `.po` block: where it starts, what it says, and its flags."""

    def __init__(self, start):
        self.start = start          # index of the block's first line
        self.flag_line = None       # index of its "#," line, if any
        self.body = None            # index of msgctxt/msgid, i.e. where a
        #                             new flag line would be inserted
        self.flags = []
        self.msgid = ""
        self.msgstrs = []
        self.obsolete = False

    @property
    def fuzzy(self):
        return "fuzzy" in self.flags


def parse_entries(lines):
    """Every entry in a `.po`, in file order.

    A hand-rolled pass rather than `msgfmt.parse`, because what is wanted
    here is *where each entry lives* so one line can be inserted into it --
    and the compiler's reader deliberately throws that away.
    """
    entries = []
    entry = None
    field = None                    # "msgid" / "msgstr", for continuations

    def start(index):
        return Entry(index)

    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            entry = None
            field = None
            continue
        if entry is None:
            entry = start(index)
            entries.append(entry)
            field = None
        if line.startswith("#~"):
            entry.obsolete = True
            continue
        if line.startswith("#,"):
            entry.flag_line = index
            entry.flags += [f.strip() for f in line[2:].split(",")]
            continue
        if line.startswith("#"):
            continue
        if entry.body is None:
            entry.body = index
        if line.startswith("msgid_plural"):
            field = None
            continue
        if line.startswith("msgid"):
            field = "msgid"
            entry.msgid = unescape(_quoted(line[5:].strip(), index + 1),
                                   index + 1)
            continue
        if line.startswith("msgstr"):
            field = "msgstr"
            rest = line[6:].strip()
            if rest.startswith("["):
                rest = rest.split("]", 1)[1].strip()
            entry.msgstrs.append(
                unescape(_quoted(rest, index + 1), index + 1))
            continue
        if line.startswith('"') and field is not None:
            text = unescape(_quoted(line, index + 1), index + 1)
            if field == "msgid":
                entry.msgid += text
            else:
                entry.msgstrs[-1] += text
    return entries


def offenders(path):
    """``[(entry, exception), ...]`` for one catalog.

    Fuzzy and obsolete entries are skipped: neither reaches a `.mo`, so
    neither can crash anything, and re-reporting them every run would bury
    the ones that can.
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out = []
    for entry in parse_entries(lines):
        if entry.obsolete or entry.fuzzy or not entry.msgid:
            continue
        exc = check_entry(entry.msgid, entry.msgstrs, entry.flags)
        if exc is not None:
            out.append((entry, exc))
    return lines, out


def rewrite(lines, bad):
    """``lines`` with a fuzzy flag on each offending entry."""
    out = list(lines)
    # Back to front, so an insertion never moves an index still to be used.
    for entry, _exc in sorted(bad, key=lambda e: e[0].start, reverse=True):
        if entry.flag_line is not None:
            out[entry.flag_line] = out[entry.flag_line].rstrip() + ", fuzzy"
        else:
            at = entry.body if entry.body is not None else entry.start
            out.insert(at, "#, fuzzy")
    return out


def report(path, bad):
    for entry, exc in bad:
        print("%s: %r -> %s: %s"
              % (os.path.relpath(path), entry.msgid[:60],
                 type(exc).__name__, exc))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--fuzzy", action="store_true",
                    help="mark offenders fuzzy in place")
    ap.add_argument("--filter", action="store_true",
                    help="write a cleaned copy to -o and leave the input be")
    ap.add_argument("-o", "--out", help="destination for --filter")
    args = ap.parse_args(argv)

    if args.filter and (len(args.files) != 1 or not args.out):
        ap.error("--filter takes one file and -o")

    total = 0
    for path in args.files:
        try:
            lines, bad = offenders(path)
        except (OSError, PoError) as exc:
            print("%s: could not read: %s" % (path, exc), file=sys.stderr)
            # A catalog we cannot read is not a catalog we may silently
            # compile: for --filter, copy it through and let the compiler
            # be the one to complain.
            if args.filter:
                with open(path, "r", encoding="utf-8") as src, \
                        open(args.out, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
            continue
        total += len(bad)
        report(path, bad)
        if args.fuzzy and bad:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rewrite(lines, bad)) + "\n")
        if args.filter:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rewrite(lines, bad)) + "\n")
    if total and not (args.fuzzy or args.filter):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
