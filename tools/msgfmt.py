#!/usr/bin/env python3
"""Compile a gettext ``.po`` catalog to ``.mo``, without GNU gettext.

``gen_pkg.sh`` calls GNU ``msgfmt`` when it is on PATH and falls back to this.
The fallback exists for Windows: Git Bash ships no gettext tools, so the only
reason the Windows CI jobs can compile translations at all is that they install
the Git for Windows SDK for its MSYS2 userland. That SDK has no usable aarch64
flavor, and the ARM64 runner image has no MSYS2 — so on that runner ``msgfmt``
is simply absent. The loop in ``gen_pkg.sh`` does not stop when a command is
missing, so without this the ARM64 installer would build and ship with every
locale silently empty.

Emitted without a hash table (as ``msgfmt --no-hash`` does). The hash table is
an optional lookup accelerator; consumers that do not find one fall back to a
binary search over the sorted key table, which is why the keys are sorted here.
Python's ``gettext`` ignores it outright.

``tests/test_msgfmt.py`` compiles every catalog in the tree with both this and
GNU ``msgfmt`` and asserts the two produce the same set of translations, so a
divergence is a test failure rather than 86 quietly wrong locales.
"""

import argparse
import re
import struct
import sys
from typing import Dict, List, Optional, Tuple

# The magic number identifying a little-endian .mo file.
MAGIC = 0x950412DE

# Separator between a context and its message id in a .mo key, and between the
# forms of a plural entry. Both are part of the format, not a local convention.
CONTEXT_SEP = "\x04"
PLURAL_SEP = "\x00"

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    '"': '"',
    "\\": "\\",
    "'": "'",
    "?": "?",
}

_OCTAL = re.compile(r"[0-7]{1,3}")
_HEX = re.compile(r"[0-9a-fA-F]{1,2}")


class PoError(Exception):
    """A .po file that cannot be parsed. Carries the line number."""


def unescape(text: str, lineno: int) -> str:
    """Resolve the C escape sequences inside one quoted .po string body."""
    out: List[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        i += 1
        if i >= len(text):
            raise PoError(f"line {lineno}: string ends in a backslash")
        char = text[i]
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
            i += 1
        elif char == "x":
            match = _HEX.match(text, i + 1)
            if not match:
                raise PoError(f"line {lineno}: \\x with no hex digits")
            out.append(chr(int(match.group(), 16)))
            i = match.end()
        elif char in "01234567":
            match = _OCTAL.match(text, i)
            # Cannot fail: the current character is already an octal digit.
            out.append(chr(int(match.group(), 8)))
            i = match.end()
        else:
            raise PoError(f"line {lineno}: unknown escape \\{char}")
    return "".join(out)


def _quoted(line: str, lineno: int) -> str:
    """Take the body of a line that is a single quoted string."""
    line = line.strip()
    if len(line) < 2 or not line.startswith('"') or not line.endswith('"'):
        raise PoError(f"line {lineno}: expected a quoted string, got {line!r}")
    return unescape(line[1:-1], lineno)


class _Entry:
    """One message being accumulated, spanning however many lines it takes."""

    def __init__(self) -> None:
        self.ctxt: Optional[str] = None
        self.msgid: Optional[str] = None
        self.plural: Optional[str] = None
        # Indexed by the N of msgstr[N]; a singular entry uses index 0.
        self.strs: Dict[int, str] = {}
        self.fuzzy = False

    def key(self) -> str:
        msgid = self.msgid or ""
        if self.plural is not None:
            msgid = msgid + PLURAL_SEP + self.plural
        if self.ctxt is not None:
            return self.ctxt + CONTEXT_SEP + msgid
        return msgid

    def value(self) -> str:
        if self.plural is not None:
            forms = [self.strs.get(i, "") for i in range(max(self.strs) + 1)]
            return PLURAL_SEP.join(forms)
        return self.strs.get(0, "")


def strip_pot_creation_date(header: str) -> str:
    """Drop ``POT-Creation-Date`` from the header, as GNU msgfmt does.

    That field is the only thing in a catalog that moves when nothing has been
    translated, so leaving it in makes every ``.mo`` in the tree differ after
    any ``regen_pot.sh`` run. gettext stopped emitting it for that reason; a
    ``.mo`` that keeps it is still valid, but it is a visible difference
    between this and the real tool and ``tests/test_msgfmt.py`` compares them.
    """
    kept = [
        line
        for line in header.split("\n")
        if not line.startswith("POT-Creation-Date:")
    ]
    return "\n".join(kept)


def parse(text: str) -> Dict[str, str]:
    """Parse .po source into the {key: translation} mapping a .mo stores.

    Skips the three kinds of entry GNU msgfmt also leaves out of a .mo:
    obsolete ones (``#~``), untranslated ones, and fuzzy ones. The header is
    the documented exception to the last — it is emitted even when fuzzy,
    which matters here because ``seed_from_jellyfin_web.py`` writes fuzzy
    entries by design and a header lost that way takes the charset with it.
    """
    catalog: Dict[str, str] = {}
    entry = _Entry()
    # Which field trailing "..." continuation lines append to.
    section: Optional[Tuple[str, int]] = None

    def flush(lineno: int) -> None:
        nonlocal entry, section
        if entry.msgid is not None:
            value = entry.value()
            is_header = entry.msgid == "" and entry.ctxt is None
            if is_header:
                value = strip_pot_creation_date(value)
            if value and (is_header or not entry.fuzzy):
                key = entry.key()
                if key in catalog:
                    raise PoError(f"line {lineno}: duplicate message {key!r}")
                catalog[key] = value
        entry = _Entry()
        section = None

    lines = text.splitlines()
    for index, raw in enumerate(lines):
        lineno = index + 1
        line = raw.strip()

        # An entry ends once a msgstr has been seen and a line arrives that
        # can only belong to the next one. Note "msgstr[1]" is not such a
        # line, so the test is against the keywords that start an entry
        # rather than against "anything after a msgstr".
        #
        # The blank and comment cases are what keep a flag attached to the
        # right entry. "#, fuzzy" precedes msgctxt/msgid, so an entry that
        # ended only at those keywords would clear the flag of the entry just
        # starting -- and fuzzy translations, which is most of what
        # seed_from_jellyfin_web.py writes, would reach users unreviewed.
        starts_entry = (
            not line
            or line.startswith("#")
            or line.split(" ", 1)[0] in ("msgctxt", "msgid")
        )
        if entry.strs and starts_entry:
            flush(lineno)

        if not line:
            continue

        if line.startswith("#"):
            # An obsolete entry is commented out wholesale. Its lines never
            # reach the parser below, so skipping them here is enough.
            if line.startswith("#~"):
                continue
            if line.startswith("#,"):
                flags = [f.strip() for f in line[2:].split(",")]
                if "fuzzy" in flags:
                    entry.fuzzy = True
            # Every other comment kind (#. #: #| and plain #) is metadata the
            # .mo does not carry.
            continue

        if line.startswith('"'):
            if section is None:
                raise PoError(f"line {lineno}: continuation with no message")
            field, plural_index = section
            piece = _quoted(line, lineno)
            if field == "msgctxt":
                entry.ctxt = (entry.ctxt or "") + piece
            elif field == "msgid":
                entry.msgid = (entry.msgid or "") + piece
            elif field == "msgid_plural":
                entry.plural = (entry.plural or "") + piece
            else:
                entry.strs[plural_index] = entry.strs.get(plural_index, "") + piece
            continue

        keyword, _, rest = line.partition(" ")

        if keyword == "msgctxt":
            entry.ctxt = _quoted(rest, lineno)
            section = ("msgctxt", 0)
        elif keyword == "msgid":
            entry.msgid = _quoted(rest, lineno)
            section = ("msgid", 0)
        elif keyword == "msgid_plural":
            entry.plural = _quoted(rest, lineno)
            section = ("msgid_plural", 0)
        elif keyword == "msgstr":
            entry.strs[0] = _quoted(rest, lineno)
            section = ("msgstr", 0)
        elif keyword.startswith("msgstr["):
            try:
                plural_index = int(keyword[len("msgstr[") : -1])
            except ValueError:
                raise PoError(f"line {lineno}: bad plural index {keyword!r}")
            entry.strs[plural_index] = _quoted(rest, lineno)
            section = ("msgstr", plural_index)
        else:
            raise PoError(f"line {lineno}: unknown keyword {keyword!r}")

    flush(len(lines) + 1)
    return catalog


def generate(catalog: Dict[str, str]) -> bytes:
    """Serialize a parsed catalog into .mo bytes."""
    # Keys are sorted because a reader without a hash table binary-searches
    # them. Sorting is over the encoded bytes, which is what such a reader
    # compares -- sorting the str keys instead orders by code point and is a
    # different order for any key outside ASCII.
    entries = sorted(
        (key.encode("utf-8"), value.encode("utf-8")) for key, value in catalog.items()
    )

    count = len(entries)
    # 7 uint32 of header, then two (length, offset) pairs per entry.
    keys_table = 7 * 4
    values_table = keys_table + count * 8
    body = values_table + count * 8

    key_index = bytearray()
    value_index = bytearray()
    blob = bytearray()

    offset = body
    for key, _ in entries:
        key_index += struct.pack("<II", len(key), offset)
        blob += key + b"\x00"
        offset += len(key) + 1
    for _, value in entries:
        value_index += struct.pack("<II", len(value), offset)
        blob += value + b"\x00"
        offset += len(value) + 1

    header = struct.pack(
        "<Iiiiiii",
        MAGIC,
        0,  # format revision
        count,
        keys_table,
        values_table,
        0,  # hash table size: no hash table
        offset,  # where a hash table would start
    )
    return header + bytes(key_index) + bytes(value_index) + bytes(blob)


def compile_po(source: str) -> bytes:
    return generate(parse(source))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("po", help="the .po file to compile")
    parser.add_argument("-o", "--output", required=True, help="the .mo to write")
    args = parser.parse_args(argv)

    with open(args.po, "r", encoding="utf-8") as handle:
        source = handle.read()

    try:
        data = compile_po(source)
    except PoError as error:
        print(f"{args.po}: {error}", file=sys.stderr)
        return 1

    with open(args.output, "wb") as handle:
        handle.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
