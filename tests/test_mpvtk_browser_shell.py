"""Unit tests for the mpvtk browser shell (mpvtk_browser.app.MpvtkBrowser):
route stack, epoch-guarded async, and build() -> scene. Uses a fake data
source (no network) and app=None (invalidate is a no-op). Headless — the
tree is turned into a scene with the real layout engine and asserted on.
"""

import threading
import unittest

from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser


class FakeSource:
    """Minimal LibrarySource stand-in for the shell tests."""

    def __init__(self):
        self.libraries = [
            {"Id": "lib1", "Name": "Movies", "Type": "CollectionFolder",
             "CollectionType": "movies"},
        ]
        self.home_rows = [
            {"title": "Continue Watching", "items": [
                {"Id": "m1", "Name": "Alpha", "Type": "Movie",
                 "ProductionYear": 2001}],
             "collection_type": None},
        ]
        self.grid_items = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Movie"}
            for i in range(30)
        ]

    def servers(self):
        return [{"uuid": "srv1", "name": "Home Server"}]

    def get_libraries(self, server_uuid):
        return list(self.libraries)

    def get_home_rows(self, server_uuid, libraries=None):
        return list(self.home_rows)

    def get_library_items(self, server_uuid, parent_id, start_index=0,
                          **kw):
        page = self.grid_items[start_index:start_index + 20]
        return page, len(self.grid_items)

    def image_spec(self, item, image_type="Primary", width=280):
        return None  # no artwork in tests -> placeholder tiles, no network

    def image_url(self, *a, **k):
        return None


def build_scene(browser, size=(1280, 720)):
    nodes, handlers = layout(browser.build(size), *size)
    return nodes, handlers


def ids(nodes):
    return {n.get("id") for n in nodes}


def types(nodes):
    return [n["t"] for n in nodes]


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_initial_route_is_home(self):
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_navigate_pushes_and_back_pops(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.assertEqual(self.b.route["kind"], "grid")
        self.assertEqual(len(self.b.nav_stack), 2)
        self.b.go_back()
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_back_stops_at_root(self):
        self.b.go_back()
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_navigate_reset_clears_stack(self):
        self.b.navigate({"kind": "grid", "parent_id": "lib1"})
        self.b.navigate({"kind": "home", "server": "srv1"}, reset=True)
        self.assertEqual(len(self.b.nav_stack), 1)
        self.assertEqual(self.b.route["kind"], "home")

    def test_epoch_bumps_on_navigation(self):
        e0 = self.b._epoch
        self.b.navigate({"kind": "grid", "parent_id": "lib1"})
        self.assertGreater(self.b._epoch, e0)

    def test_after_playlist_deleted_prunes(self):
        self.b.navigate({"kind": "grid", "parent_id": "PL9", "title": "PL"})
        self.b.after_playlist_deleted("PL9")
        self.assertTrue(all(r.get("parent_id") != "PL9"
                            for r in self.b.nav_stack))
        self.assertEqual(self.b.route["kind"], "home")

    def test_open_folder_item_navigates_to_grid(self):
        self.b._open_item({"Id": "lib1", "Name": "Movies",
                           "Type": "CollectionFolder",
                           "CollectionType": "movies"})
        self.assertEqual(self.b.route["kind"], "grid")
        self.assertEqual(self.b.route["parent_id"], "lib1")


class TestAsyncStaleness(unittest.TestCase):
    def test_superseded_result_is_dropped(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        applied = []
        gate = threading.Event()
        released = threading.Event()

        def work():
            gate.set()
            released.wait(2.0)
            return "value"

        b.run_async(work, lambda r: applied.append(r), epoch=b._epoch)
        self.assertTrue(gate.wait(2.0))     # worker is running
        b._bump_epoch()                      # user navigated away meanwhile
        released.set()
        b._pool.shutdown(wait=True)
        self.assertEqual(applied, [], "stale result must not be applied")

    def test_current_result_is_applied(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        applied = []
        b.run_async(lambda: "value", lambda r: applied.append(r),
                    epoch=b._epoch)
        b._pool.shutdown(wait=True)
        self.assertEqual(applied, ["value"])


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_home_loading_shows_spinner(self):
        # No data yet on the fresh route -> Busy spinner.
        self.b.route.pop("_data", None)
        nodes, _h = build_scene(self.b)
        self.assertIn("busy", types(nodes))

    def test_home_with_data_renders_strip_rows(self):
        src = self.b.source
        self.b.route["_data"] = {
            "libraries": src.libraries, "rows": src.home_rows}
        nodes, handlers = build_scene(self.b)
        self.assertIn("img", types(nodes))          # ImageMap strip present
        # tile hit-regions are registered as click handlers
        self.assertTrue(any(k.startswith("row-libs-") for k in handlers))

    def test_grid_with_data_renders(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.b.route["_items"] = self.b.source.grid_items
        self.b.route["_total"] = len(self.b.source.grid_items)
        nodes, _h = build_scene(self.b)
        self.assertIn("img", types(nodes))

    def test_chrome_present_on_grid(self):
        self.b.navigate({"kind": "grid", "parent_id": "lib1",
                         "title": "Movies"})
        self.b.route["_items"] = []
        self.b.route["_total"] = 0
        nodes, _h = build_scene(self.b)
        self.assertIn("nav-home", ids(nodes))
        self.assertIn("nav-back", ids(nodes))   # depth > 1

    def test_chrome_absent_on_chrome_free_route(self):
        self.b.nav_stack.append({"kind": "connecting"})
        nodes, _h = build_scene(self.b)
        self.assertNotIn("nav-home", ids(nodes))


class FakeController:
    def __init__(self):
        self.entered = 0
        self.left = 0
        self.played = []

    def on_browse_enter(self):
        self.entered += 1

    def on_browse_leave(self):
        self.left += 1

    def play(self, item, server_uuid):
        self.played.append((item.get("Id"), server_uuid))


class TestPlaybackLifecycle(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def test_click_playable_yields_and_plays(self):
        self.b._open_item({"Id": "m1", "Name": "Alpha", "Type": "Movie"})
        self.assertFalse(self.b._browsing, "browser should yield to playback")
        self.assertEqual(self.ctl.left, 1)     # OSC handed back
        self.assertEqual(self.ctl.played, [("m1", "srv1")])

    def test_yielded_build_is_empty(self):
        self.b._browsing = False
        nodes, _h = build_scene(self.b)
        # No strip overlays / chrome while yielded to the video + OSC.
        self.assertNotIn("img", types(nodes))
        self.assertNotIn("nav-home", ids(nodes))

    def test_playstate_stopped_returns_to_browse(self):
        self.b._browsing = False
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b._browsing)
        self.assertEqual(self.ctl.entered, 1)   # took the window + OSC off

    def test_playstate_playing_keeps_yielded(self):
        self.b._browsing = True
        self.b.on_playstate({"stopped": False, "position": 5})
        self.assertFalse(self.b._browsing)

    def test_enter_browse_calls_controller(self):
        self.b.enter_browse()
        self.assertTrue(self.b._browsing)
        self.assertGreaterEqual(self.ctl.entered, 1)

    def test_set_source_repopulates_and_resets_home(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b.navigate({"kind": "grid", "parent_id": "lib1"})
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.server, "srv1")
        self.assertEqual(b.route["kind"], "home")
        self.assertEqual(len(b.nav_stack), 1)


if __name__ == "__main__":
    unittest.main()
