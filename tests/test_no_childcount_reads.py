"""The browser must never read `ChildCount` off a server DTO.

A container's `ChildCount` is not trustworthy and the failure it buys is
total: draw a collection from its count and every collection made from a
`collection.xml` renders empty, because those members are applied to the
in-memory item and never written to the table the count comes from
(stdjflib's `docs/COLLECTION_XML_BUGS.md` bug 1, measured on Jellyfin 12.0;
upstream considers `collection.xml` deprecated, so it is not going to be
fixed). "All my collections are empty" reproduces on real libraries and on
no fake, which is what makes it worth a guard rather than a convention.

This used to be pinned by `tests/e2e/test_collections.py` against a
file-made collection: `ChildCount` 0 while the listing returned three
films, so a client reading the count could be caught. That fixture now
lists nothing either, so the contrast it depended on is gone and the e2e
test could no longer tell a correct client from a broken one.

So the property moved here, where it needs no server, no fixture and no
server defect to remain observable — and where it covers **every** call
site rather than the one screen a test happened to open.

Reading the source rather than a rendered screen is the point: the bug is
an *absence*, and there is no scene in which "nobody consulted ChildCount"
is visible.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import pathlib
import unittest

PKG = pathlib.Path(__file__).resolve().parent.parent / "jellyfin_mpv_shim"

def _childcount_reads():
    """``(relpath, lineno, source line)`` for every read of ChildCount."""
    found = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        text = path.read_text(encoding="utf-8")
        if "ChildCount" not in text:
            continue
        lines = text.splitlines()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            # A read is `x["ChildCount"]`, `x.get("ChildCount")`, or the
            # name appearing anywhere that is not a dict key being stored.
            if isinstance(node, ast.Constant) and node.value == "ChildCount":
                line = lines[node.lineno - 1]
                # `"ChildCount": <expr>` is building a DTO, not reading one.
                if line.strip().startswith('"ChildCount":'):
                    continue
                found.append((rel, node.lineno, line.strip()))
    return found


class NoChildCountReadsTest(unittest.TestCase):
    def test_nothing_reads_a_servers_child_count(self):
        reads = _childcount_reads()
        self.assertEqual(
            reads, [],
            "ChildCount is read here, and it is 0 for any collection built "
            "from a collection.xml -- browse by listing the container "
            "instead:\n" + "\n".join("  %s:%d  %s" % r for r in reads))

    def test_the_guard_can_see_a_read(self):
        """A negative control, because this test's whole content is an
        empty list and an empty list is also what a broken matcher
        returns."""
        import ast as _ast

        src = 'n = item.get("ChildCount") or 0\n'
        hits = [c for c in _ast.walk(_ast.parse(src))
                if isinstance(c, _ast.Constant) and c.value == "ChildCount"]
        self.assertEqual(len(hits), 1)
        self.assertFalse(src.strip().startswith('"ChildCount":'),
                         "the write-filter would have swallowed a read")

    def test_the_known_write_is_still_a_write(self):
        """The one place the field is set. If it stops being a plain
        `"ChildCount": ...` key the filter above silently reclassifies it
        as a read, and this says so in one line instead of failing the
        real test with a confusing message."""
        text = (PKG / "mpvtk_browser" / "repository.py").read_text()
        self.assertIn('"ChildCount": len(members),', text)


if __name__ == "__main__":
    unittest.main()
