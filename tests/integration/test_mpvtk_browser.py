"""Phase 0 exit test: the mpvtk browser shell rendered in a REAL mpv window,
via the attach path (MpvtkApp.attach -> AdoptBackend), on both backends.

This proves the whole Phase-0 stack end to end against a live mpv:
renderer.lua loads into an externally-created handle, the browser builds a
scene (chrome + strip rows via the production StripStore), it reaches the
renderer, and interaction round-trips. No player.py, no server, no network
(a fake source with placeholder tiles). Run per backend under xvfb by
run_integration.py.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402


def _make_source():
    class FakeSource:
        libraries = [
            {"Id": "lib1", "Name": "Movies", "Type": "CollectionFolder",
             "CollectionType": "movies"},
            {"Id": "lib2", "Name": "Shows", "Type": "CollectionFolder",
             "CollectionType": "tvshows"},
        ]
        rows = [{"title": "Continue Watching", "items": [
            {"Id": "m%d" % i, "Name": "Title %d" % i, "Type": "Movie",
             "ProductionYear": 2000 + i} for i in range(6)],
            "collection_type": None}]

        def servers(self):
            return [{"uuid": "srv1", "name": "Test"}]

        def get_libraries(self, server_uuid):
            return list(self.libraries)

        def get_home_rows(self, server_uuid, libraries=None):
            return list(self.rows)

        def get_library_items(self, server_uuid, parent_id, start_index=0,
                              **kw):
            items = [{"Id": "g%d" % i, "Name": "Grid %d" % i, "Type": "Movie"}
                     for i in range(24)]
            return items[start_index:start_index + 24], len(items)

        def image_spec(self, item, image_type="Primary", width=280):
            return None  # placeholder tiles -> no network

        def image_url(self, *a, **k):
            return None

    return FakeSource()


def _spawn_handle():
    """Create a raw mpv handle the way the player would, so the browser can
    attach to it (rather than mpvtk spawning its own)."""
    from jellyfin_mpv_shim.mpvtk.app import _SPAWN_OPTS

    if h.BACKEND == "jsonipc":
        import python_mpv_jsonipc
        opts = dict(_SPAWN_OPTS)
        opts["geometry"] = "1280x720"
        return python_mpv_jsonipc.MPV(start_mpv=True, **opts), True
    import mpv as libmpv
    opts = {k.replace("_", "-"): v for k, v in _SPAWN_OPTS.items()}
    opts["geometry"] = "1280x720"
    return libmpv.MPV(**opts), False


@h.require_real_mpv
class TestMpvtkBrowserOnRealMpv(unittest.TestCase):
    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        # Exercise the storage path that matches the backend, as the real
        # integration will: memory on in-process libmpv, files on jsonipc.
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-itest-")))
        self.browser = MpvtkBrowser(self.app, _make_source(), strips=strips)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            self.browser.shutdown()
            try:
                self.handle.terminate()
            except Exception:
                pass

    def test_renders_home_in_real_window(self):
        self.assertTrue(self.app.ready.wait(15),
                        "renderer never became ready in the attached mpv")
        # Let the async home load complete and repaint.
        deadline = time.time() + 6
        st = None
        while time.time() < deadline:
            st = self.app.debug_state()
            if st and st.get("overlays", 0) >= 1:
                break
            time.sleep(0.3)
        self.assertTrue(st and st.get("w", 0) > 0, "no render size: %r" % st)
        self.assertGreaterEqual(
            st.get("overlays", 0), 1,
            "expected at least one strip overlay on the home screen: %r" % st)

    def test_click_navigates_into_a_library(self):
        self.assertTrue(self.app.ready.wait(15))
        # Wait for the home rows to actually RENDER (a strip overlay present
        # means the post-load re-render registered the tile hit-handlers) —
        # not just for the data to load, or the click races the render.
        deadline = time.time() + 6
        while time.time() < deadline:
            st = self.app.debug_state()
            if (st and st.get("overlays", 0) >= 1
                    and "_data" in self.browser.route):
                break
            time.sleep(0.2)
        self.app.debug(cmd="click", id="row-libs-lib1")
        deadline = time.time() + 4
        while time.time() < deadline and self.browser.route["kind"] != "grid":
            time.sleep(0.2)
        self.assertEqual(self.browser.route["kind"], "grid")
        self.assertEqual(self.browser.route["parent_id"], "lib1")


if __name__ == "__main__":
    unittest.main()
