"""Every virtualized list must have an on_scroll.

Virtualization windows against ``_offset(scroll_id)``, which prefers the
renderer's live ``user-data/mpvtk/scroll`` property and falls back to the
throttled ``on_scroll`` copy. So a virtualized list with no ``on_scroll``
works on mpv >= 0.36 and, on anything older, renders blank past the first
screenful — the window stays pinned at offset 0 forever.

Two of these have shipped: search Songs (found by the audit) and the album
page. Both were single missing kwargs among eleven correct ones, invisible
to review and invisible on the reviewer's mpv. This finds the next one by
reading the source, since no unit test can exercise the mpv < 0.36 path.
"""

import ast
import inspect
import os
import unittest

from jellyfin_mpv_shim.mpvtk_browser import app as app_mod

PKG = os.path.dirname(inspect.getfile(app_mod))
MODULES = ["app", "dialogs", "auth", "settings", "queue_edit", "music",
           "views", "tiles"]


def _kwargs(call):
    return {kw.arg for kw in call.keywords if kw.arg}


def _scroll_ids_requested(tree):
    """Scroll ids passed as ``scroll_id=`` anywhere — i.e. the lists that
    are windowed and therefore depend on a live offset."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "scroll_id" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    out.add(kw.value.value)
    return out


def _follow_containers(tree):
    """``{id: has_on_scroll}`` for every scroll container built with
    ``follow=True``."""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr",
                                                         None)
        if name not in ("VScroll", "HScroll", "Scroll"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        follow = kw.get("follow")
        if not (isinstance(follow, ast.Constant) and follow.value is True):
            continue
        sid = kw.get("id")
        sid = sid.value if isinstance(sid, ast.Constant) else "<dynamic>"
        out[sid] = "on_scroll" in kw
    return out


def _scroll_containers(tree):
    """``{id: has_on_scroll}`` for every VScroll/HScroll/Scroll built with a
    literal id."""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr",
                                                         None)
        if name not in ("VScroll", "HScroll", "Scroll"):
            continue
        sid = None
        for kw in node.keywords:
            if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                sid = kw.value.value
        if sid is None:
            continue
        # A duplicate id in two branches counts as wired only if both are.
        wired = "on_scroll" in _kwargs(node)
        out[sid] = out.get(sid, True) and wired
    return out


def _all_modules():
    trees = {}
    for mod in MODULES:
        path = os.path.join(PKG, mod + ".py")
        if os.path.exists(path):
            with open(path) as fh:
                trees[mod] = ast.parse(fh.read())
    return trees


class TestVirtualizedListsAreWired(unittest.TestCase):
    def test_every_windowed_list_reports_its_scroll(self):
        trees = _all_modules()
        requested = set()
        containers = {}
        for tree in trees.values():
            requested |= _scroll_ids_requested(tree)
            for sid, wired in _scroll_containers(tree).items():
                containers[sid] = containers.get(sid, True) and wired

        missing = sorted(sid for sid in requested
                         if sid in containers and not containers[sid])
        self.assertEqual(
            missing, [],
            "virtualized but no on_scroll — blank past the first screenful "
            "on mpv < 0.36: %s" % missing)

    def test_every_follow_container_can_be_told_it_snapped(self):
        """`follow` moves the scroll offset renderer-side, which invalidates
        the virtualized window Python built the scene with. The renderer
        reports the snap as a scroll event — but that only reaches anything
        if the container registered an on_scroll. Without one the panel draws
        blank at the new offset until an unrelated rebuild happens, which is
        exactly how the logs tab shipped broken."""
        trees = _all_modules()
        bad = []
        seen = 0
        for tree in trees.values():
            for sid, wired in _follow_containers(tree).items():
                seen += 1
                if not wired:
                    bad.append(sid)
        self.assertEqual(bad, [], "follow=True with no on_scroll: %s" % bad)
        self.assertTrue(seen, "no follow containers found — check the parser")

    def test_the_check_can_see_the_containers_it_is_checking(self):
        """A parser that silently matched nothing would pass forever."""
        trees = _all_modules()
        requested = set()
        containers = {}
        for tree in trees.values():
            requested |= _scroll_ids_requested(tree)
            containers.update(_scroll_containers(tree))
        self.assertGreater(len(requested), 5, "found almost no windowed lists")
        matched = requested & set(containers)
        self.assertGreater(len(matched), 5,
                           "windowed ids did not match any scroll container; "
                           "requested=%s containers=%s"
                           % (sorted(requested), sorted(containers)))


if __name__ == "__main__":
    unittest.main()
