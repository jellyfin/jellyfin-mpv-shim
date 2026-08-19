"""Code symbols cited in the reference docs still exist.

`prose_audit --stale` finds names that no longer exist in prose *inside
source files*. It never opens `docs/`, so the ~50k words of reference
documentation are the one body of prose here with no rot check at all --
and docs rot silently, which is the standing cost of having moved a fact out
of the code in the first place.

Caught on its first run: `docs/browser-shell.md` said the tile builders close
over `self._posters`. That dict was deleted -- it was a second, unbounded
owner of every decoded poster, which is the bug `TileRenderer.__init__` still
carries a comment about -- so the doc named a dead attribute *and* described a
design that had been deliberately dismantled.

Deliberately narrow, because a doc legitimately cites things this tree does
not define:

* only ``a.b``-shaped names whose first segment is one of OUR modules, plus
  ``self.<attr>``. Stdlib (``os._exit``), the Jellyfin server's C#
  (``LiveTvManager.AddInfoToProgramDto``), jellyfin-web's JS and D-Bus names
  are none of our business to keep in step;
* not the dated planning documents and checklists. Those are point-in-time
  records, like `docs/archive/`: naming something that was proposed and never
  built, or has since been renamed, is what they are FOR.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Backticked dotted name. The same shape `--stale` looks for, restricted to
#: the qualified form -- a bare `foo` in prose is a word as often as a symbol.
TOK = re.compile(r'`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)`')

#: `renderer.lua`, `conf.json`, `base.pot` -- a filename, not an attribute.
SUFFIXES = {"py", "lua", "json", "md", "pot", "po", "mo", "xml", "txt", "conf",
            "sh", "toml", "cfg", "ini", "bat", "iss", "in", "js", "tsx", "ts",
            "cs", "c", "h", "png", "jpg", "svg", "zip", "html", "css"}

#: A planning document records what was intended when it was written.
PLAN_DOCS = re.compile(r'(_PLAN|_FIXES|_CHECKLIST|_GAPS|\d{4}-\d{2})')


def _sources():
    out = subprocess.run(["git", "ls-files", "*.py", "*.lua"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if f.startswith("jellyfin_mpv_shim/")]


def _docs():
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out
            if not f.startswith("docs/archive/")
            and not PLAN_DOCS.search(os.path.basename(f))]


def _defined():
    """Names this package defines, per module and in aggregate.

    **Module heads only, deliberately.** Class-qualified citations
    (`TileRenderer.banner_box`) would roughly double the coverage, but our
    class names collide with foreign ones a doc legitimately cites --
    `Image.thumbnail` is Pillow, `Icon.HAS_DEFAULT_ACTION` is pystray,
    `Video.PropagatePlayedState` is the Jellyfin server, and this package
    defines an `Image`, an `Icon` and a `Video` of its own. Module basenames
    here are specific enough not to collide.

    Includes dict-key string literals, so `theme.ACCENT` resolves: that
    module serves its palette through a module ``__getattr__`` and defines no
    such global on purpose (see its docstring). A check that is too permissive
    here costs nothing -- it is looking for names that are GONE.
    """
    per, every = {}, set()
    for rel in _sources():
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        names = set(re.findall(r'^\s*(?:def|class)\s+(\w+)', text, re.M))
        names |= set(re.findall(r'^\s*(\w+)\s*[:=]', text, re.M))
        names |= set(re.findall(r'self\.(\w+)\s*=', text))
        names |= set(re.findall(r'function\s+[\w.:]*?(\w+)\s*\(', text))
        names |= set(re.findall(r'["\'](\w+)["\']\s*:', text))
        per.setdefault(os.path.basename(rel).rsplit(".", 1)[0], set()).update(names)
        every |= names
    return per, every


class DocSymbolsTest(unittest.TestCase):
    def test_cited_symbols_exist(self):
        per, every = _defined()
        dead, checked = [], 0
        for rel in _docs():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for cited in sorted(set(TOK.findall(text))):
                head, tail = cited.split(".")[0], cited.split(".")[1]
                if tail in SUFFIXES:
                    continue
                if head == "self":
                    if tail == "X":     # a stand-in, not a citation
                        continue
                    checked += 1
                    if tail not in every:
                        dead.append((rel, cited))
                elif head in per:
                    checked += 1
                    if tail not in per[head] and tail not in every:
                        dead.append((rel, cited))
        self.assertFalse(dead, "docs citing a name this package no longer "
                         "defines:\n" + "\n".join("  %s: %s" % d for d in dead))
        # A guard that resolves nothing passes for the wrong reason.
        # The reference docs currently offer ~70.
        self.assertGreater(checked, 50)


if __name__ == "__main__":
    unittest.main()
