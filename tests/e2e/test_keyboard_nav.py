"""Reaching the library by keyboard, against a real server.

Focus movement itself lives in `renderer.lua` and is covered by the Lua
suite. What Python owns is the other half: the scene has to *offer* something
to focus, every focusable thing needs a handler, and activating it has to go
somewhere. Those are the parts real data can break.

Three ways it breaks that fabricated data cannot show:

* **A screen with no activatable content.** The chrome is always there, so a
  screen whose content area produced no handlers still looks fine in a
  snapshot and is simply unreachable without a mouse.
* **Duplicate node ids.** `layout()` warns that "renderer state and events
  will target only the last occurrence" — so a collision silently makes one
  tile unreachable and misroutes the other. Node ids embed item ids, and a
  real library has a thousand of them, unicode titles and near-duplicate
  names; the fake has two dozen tidy ones.
* **Actions that only exist for some item types.** The tile menu is the
  keyboard route to everything per-item, and it is built per type — the
  07-25 checklist walks it "on at least a movie, an episode, a series, an
  album and a track" for that reason.

Activation is `handlers[id]["click"]()`, which is what the renderer calls for
the focused node when you press Enter — the same entry point a click uses, so
this exercises the keyboard path without needing a window or a real keypress.
"""

import collections
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SIZE = (1280, 720)

#: Ids belonging to the window chrome rather than the screen's content. A
#: screen is only reachable if something *other* than these is activatable.
CHROME_PREFIXES = ("nav-", "chrome-", "topbar-", "bar-")


class _SyncPool:
    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


@_e2e.require_server
class KeyboardNavTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk.layout import layout
        cls.MpvtkBrowser = MpvtkBrowser
        cls.layout = staticmethod(layout)
        cls.session = _e2e.Session()
        # This module asserts on the home screen's own nodes, so it owes the
        # layout a normalise -- see _e2e.normalise_home_layout.
        _e2e.normalise_home_layout(cls.session)
        cls.source = cls.session.library_source()
        cls.libraries = cls.source.get_libraries(_e2e.SOURCE_UUID)
        cls.by_name = {lib["Name"]: lib for lib in cls.libraries}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def setUp(self):
        self.browser = self.MpvtkBrowser(
            app=None, source=self.source, server_uuid=_e2e.SOURCE_UUID)
        self.browser._async._pool.shutdown(wait=True, cancel_futures=True)
        self.browser._pool = _SyncPool()
        self.browser._load_route(self.browser.route)
        self.addCleanup(self._shutdown)
        self.nodes, self.handlers = self._render()

    def _shutdown(self):
        try:
            self.browser.shutdown()
        except Exception:
            pass

    def _render(self):
        self.nodes, self.handlers = self.layout(
            self.browser.build(SIZE), *SIZE)
        return self.nodes, self.handlers

    def _activate(self, node_id):
        """Press Enter on `node_id`, then repaint.

        The renderer calls the focused node's click handler on Enter, so this
        is the keyboard path — no window and no real keypress needed.
        """
        self.assertIn(node_id, self.handlers,
                      "%s has no handlers at all" % node_id)
        self.assertIn("click", self.handlers[node_id],
                      "%s cannot be activated" % node_id)
        self.handlers[node_id]["click"]()
        return self._render()

    def _find(self, needle):
        matches = [i for i in self.handlers if needle in i]
        self.assertTrue(matches, "no node id containing %r" % needle)
        return matches[0]

    def _content_ids(self):
        return [i for i, h in self.handlers.items()
                if "click" in h
                and not any(i.startswith(p) for p in CHROME_PREFIXES)]

    def _goto_library(self, name):
        library = self.by_name.get(name)
        if library is None:
            self.skipTest("no %r library" % name)
        self._activate(self._find(library["Id"]))
        return library

    # -- the flows ---------------------------------------------------------

    def test_home_to_library_to_item_and_back(self):
        """The core flow, entirely by activation: a library tile, then an
        item, then back out the way you came."""
        self.assertEqual(self.browser.route["kind"], "home")
        self._goto_library("Movies")
        self.assertEqual(self.browser.route["kind"], "grid")
        self.assertEqual(self.browser.route.get("title"), "Movies")

        item = next((i for i in (self.browser.route.get("_items") or []) if i),
                    None)
        self.assertIsNotNone(item, "the grid loaded no items to open")
        self._activate(self._find(item["Id"]))
        self.assertEqual(self.browser.route["kind"], "detail")
        self.assertEqual(self.browser.route.get("title"), item["Name"])

        self.browser.go_back()
        self._render()
        self.assertEqual(self.browser.route["kind"], "grid",
                         "Back from an item did not return to the library")
        self.browser.go_back()
        self._render()
        self.assertEqual(self.browser.route["kind"], "home",
                         "Back from a library did not return home")

    def test_every_screen_offers_something_to_activate(self):
        """A screen whose content produced no handlers is mouse-only.

        The chrome is always activatable, so it is excluded — otherwise every
        screen passes, including one that drew no content at all.
        """
        screens = [("home", None)]
        for name in ("Movies", "Shows", "Music", "Test Media"):
            if name in self.by_name:
                screens.append((name, self.by_name[name]))

        for label, library in screens:
            with self.subTest(screen=label):
                if library is None:
                    self.browser.navigate({"kind": "home",
                                           "server": _e2e.SOURCE_UUID})
                else:
                    kind = ("music" if library.get("CollectionType") == "music"
                            else "grid")
                    self.browser.navigate({
                        "kind": kind, "server": _e2e.SOURCE_UUID,
                        "parent_id": library["Id"], "title": label,
                        "collection_type": library.get("CollectionType")})
                self._render()
                self.assertTrue(
                    self._content_ids(),
                    "%s drew no activatable content, so it cannot be reached "
                    "with a keyboard at all" % label)

    def test_no_duplicate_node_ids_on_real_data(self):
        """A collision makes one node unreachable and misroutes the other.

        `layout()` only warns: "renderer state and events will target only
        the last occurrence". Ids embed item ids, so this is a real-data
        risk — a thousand items, unicode titles, near-duplicate names.
        """
        checks = [("home", {"kind": "home", "server": _e2e.SOURCE_UUID})]
        for name in ("Movies", "Bulk Movies", "Shows"):
            library = self.by_name.get(name)
            if library:
                checks.append((name, {
                    "kind": "grid", "server": _e2e.SOURCE_UUID,
                    "parent_id": library["Id"], "title": name,
                    "collection_type": library.get("CollectionType")}))

        for label, route in checks:
            with self.subTest(screen=label):
                self.browser.navigate(dict(route))
                nodes, _handlers = self._render()
                counts = collections.Counter(n["id"] for n in nodes)
                dupes = sorted(i for i, c in counts.items() if c > 1)
                self.assertEqual(
                    dupes, [],
                    "%s drew duplicate node ids, so events reach only the "
                    "last of each: %s" % (label, dupes[:5]))

    def test_a_tile_offers_its_menu_for_every_item_type(self):
        """The tile menu is the keyboard route to per-item actions, and it is
        built per type — the 07-25 checklist walks a movie, an episode, a
        series, an album and a track for exactly that reason."""
        cases = []
        movie = self.session.find_all(library="Movies", item_type="Movie",
                                      Limit=1)
        series = self.session.find_all(library="Shows", item_type="Series",
                                       Limit=1)
        episode = self.session.find_all(library="Shows", item_type="Episode",
                                        Limit=1)
        album = self.session.find_all(library="Music", item_type="MusicAlbum",
                                      Limit=1)
        track = self.session.find_all(library="Music", item_type="Audio",
                                      Limit=1)
        for label, found in (("movie", movie), ("series", series),
                             ("episode", episode), ("album", album),
                             ("track", track)):
            if found:
                cases.append((label, found[0]))
        self.assertTrue(cases, "no items of any type to test")

        for label, item in cases:
            with self.subTest(item_type=label):
                entries = self.browser._tile_menu_entries(item)
                self.assertTrue(
                    entries,
                    "a %s tile offers no menu, so its actions are "
                    "unreachable by keyboard" % label)
                for entry in entries:
                    self.assertTrue(
                        entry[0], "%s has a menu entry with no label" % label)
                    self.assertTrue(
                        entry[2], "%s has a menu entry with no action" % label)

    def test_jumping_back_through_history_matches_pressing_back(self):
        """`662d4b06` — the history menu's backward jump skipped what a Back
        press reloads, so a jump past an editor showed stale membership while
        the same number of Back presses refetched.

        Same depth, two routes out, same destination.
        """
        self._goto_library("Movies")
        item = next((i for i in (self.browser.route.get("_items") or []) if i),
                    None)
        self.assertIsNotNone(item)
        self._activate(self._find(item["Id"]))
        self.assertEqual(self.browser.route["kind"], "detail")
        depth = len(self.browser.nav_stack)
        self.assertGreaterEqual(depth, 3, "not deep enough to jump")

        self.browser.go_back()
        self.browser.go_back()
        self._render()
        by_back = (self.browser.route["kind"],
                   self.browser.route.get("title"))

        # Rebuild the same stack, then leave it in one jump instead.
        self._goto_library("Movies")
        self._activate(self._find(item["Id"]))
        self.assertEqual(len(self.browser.nav_stack), depth)
        # go_back_to, not the Navigator's rewind_to: the shell's half of a
        # back press is decided by the page being LEFT (a playlist editor
        # makes what is underneath stale), which is the whole of 662d4b06 —
        # so a test that called the navigator directly would skip the
        # reloading the bug was about. Depth is 1-based; 1 is the root.
        self.browser.go_back_to(1)
        self._render()
        by_jump = (self.browser.route["kind"],
                   self.browser.route.get("title"))

        self.assertEqual(
            by_jump, by_back,
            "jumping back through history lands somewhere different from "
            "pressing Back the same number of times")


if __name__ == "__main__":
    unittest.main()
