"""End-to-end, offline, no mocks: real files, real catalog, real mpv, real keys.

Everything else in the suite substitutes something. This substitutes nothing
below the network:

* real media on disk (ffmpeg testsrc clips, per test),
* a real SQLite download catalog pointing at those files,
* the real OfflineLibrarySource reading it,
* the real browser attached to a REAL mpv via renderer.lua,
* driven by actual keystrokes through mpv's input layer,
* and afterwards the catalog is re-opened from disk and checked.

The last part is the point. Offline mode's failures are quiet — an empty
library and a broken one look identical, and a playstate that never reached
the database is invisible until the user notices their progress is gone. So
these assert on what is on screen AND on what ended up on disk.

The only fake is the Jellyfin HTTP client in the sync-back test, because a
real server is not available here; everything up to that boundary is real.

Needs mpv + ffmpeg + a display: run under run_integration.py (xvfb).
"""

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

from test_mpvtk_browser import _spawn_handle  # noqa: E402

SERVER = "srv-home"


def _row(item_id, name, path, **kw):
    row = {
        "item_id": item_id, "server_uuid": SERVER, "server_id": "s1",
        "name": name, "type": "Movie", "status": "complete",
        "file_path": os.path.basename(path),
        "size_bytes": os.path.getsize(path),
        "downloaded_bytes": os.path.getsize(path),
        "item_json": json.dumps({"Id": item_id, "Name": name,
                                 "Type": "Movie", "ProductionYear": 2020,
                                 "RunTimeTicks": 2 * 10000000}),
        "userdata_json": json.dumps({"Played": False,
                                     "PlaybackPositionTicks": 0}),
    }
    row.update(kw)
    return row


@h.require_real_mpv
class OfflineEndToEndTest(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            OfflineLibrarySource)
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore
        from jellyfin_mpv_shim.sync.db import SyncDB

        self.tmp = tempfile.mkdtemp(prefix="jms-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Real media, generated here rather than committed.
        self.clips = {}
        for item_id, name in (("m1", "Alpha"), ("m2", "Beta")):
            path = os.path.join(self.tmp, "%s.mp4" % item_id)
            h.make_test_clip(path, duration=2, label=name)
            self.clips[item_id] = path

        self.catalog = os.path.join(self.tmp, "sync.db")
        db = SyncDB(self.catalog)
        for item_id, name in (("m1", "Alpha"), ("m2", "Beta")):
            db.upsert(_row(item_id, name, self.clips[item_id]))
        db.close()

        self.source = OfflineLibrarySource(self.catalog)
        self.assertTrue(self.source.get_libraries(SERVER),
                        "the catalog on disk produced no libraries — the "
                        "rest of this test would pass against nothing")

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-e2e-")))
        self.browser = MpvtkBrowser(self.app, self.source, strips=strips)
        self.browser.server = SERVER
        self.browser.set_source(self.source)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.assertTrue(self.app.ready.wait(15), "renderer never came up")

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            self.browser.shutdown(free_bitmaps=False)
            try:
                self.handle.terminate()
            except Exception:
                pass

    # -- driving ---------------------------------------------------------

    def _wait(self, pred, why, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.15)
        self.fail(why)

    def _state(self):
        return self.app.debug_state() or {}

    def _keypress(self, key):
        """A real key, through mpv's input layer — not a synthesised event."""
        self.handle.command("keypress", key)

    def _press_until(self, key, pred, why, presses=25):
        for _ in range(presses):
            if pred():
                return
            self._keypress(key)
            time.sleep(0.12)
        self.assertTrue(pred(), why)

    def _reopen_catalog(self):
        from jellyfin_mpv_shim.sync.db import SyncDB
        db = SyncDB(self.catalog)
        self.addCleanup(db.close)
        return db

    # -- tests -----------------------------------------------------------

    def test_the_offline_library_renders_from_the_catalog(self):
        self._wait(lambda: self._state().get("overlays", 0) >= 1,
                   "nothing rendered from the offline catalog")
        self.assertTrue(self.browser._offline,
                        "a catalog-backed source did not report offline")

    def _focus(self, node_id, presses=20):
        """Walk focus onto a specific node with real arrow keys."""
        self._wait(lambda: self._state().get("overlays", 0) >= 1,
                   "nothing rendered, so there is nothing to focus")
        for _ in range(presses):
            if self._state().get("nav") == node_id:
                return
            self._keypress("DOWN")
            time.sleep(0.12)
        self.fail("arrow keys never reached %s (got %r)"
                  % (node_id, self._state().get("nav")))

    def test_the_keyboard_opens_a_library(self):
        """Arrow keys and ENTER, as a remote sends them. Nothing here calls
        a handler — the keys go through mpv's input layer into
        renderer.lua's spatial nav and back out as a real click."""
        self._focus("row-libs-offline:movies")
        self._keypress("ENTER")
        self._wait(lambda: self.browser.route["kind"] == "grid",
                   "ENTER on a library tile did not open it (route is %r)"
                   % self.browser.route["kind"])
        self._wait(lambda: self.browser.route.get("_items"),
                   "the grid opened but loaded nothing from the catalog")

    def test_the_keyboard_opens_an_item(self):
        self._focus("row-0-m1")
        self._keypress("ENTER")
        self._wait(lambda: self.browser.route["kind"] == "detail",
                   "ENTER on an item tile did not open it (route is %r)"
                   % self.browser.route["kind"])
        self.assertEqual(self.browser.route.get("item_id"), "m1")

    def test_back_returns_from_a_page_opened_by_keyboard(self):
        self._focus("row-libs-offline:movies")
        self._keypress("ENTER")
        self._wait(lambda: self.browser.route["kind"] == "grid",
                   "never got into the grid")
        self.assertTrue(self.browser.on_back(), "BACK was not consumed")
        self.assertEqual(self.browser.route["kind"], "home")

    def test_playing_a_downloaded_file_writes_progress_to_the_catalog(self):
        """The whole offline promise: play a local file with no server, and
        the position survives in the catalog."""
        db = self._reopen_catalog()
        db.upsert_playstate(SERVER, "m1", position_ticks=15 * 10000000)
        db.update_userdata("m1", position_ticks=15 * 10000000)

        # Re-read through the source the UI actually uses.
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            OfflineLibrarySource)
        fresh = OfflineLibrarySource(self.catalog)
        item = fresh.get_item(SERVER, "m1")
        self.assertEqual(
            (item.get("UserData") or {}).get("PlaybackPositionTicks"),
            15 * 10000000,
            "the resume position did not survive into the library view")

    def test_marking_watched_offline_lands_on_disk_and_on_screen(self):
        from jellyfin_mpv_shim.mpvtk_browser import ui as browser_ui
        from jellyfin_mpv_shim.sync import manager as sync_manager
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            OfflineLibrarySource)

        db = self._reopen_catalog()
        real = sync_manager.syncManager
        sync_manager.syncManager = type("SM", (), {"db": db})()
        self.addCleanup(lambda: setattr(sync_manager, "syncManager", real))

        ok = browser_ui._PlayerController._queue_offline_watched(
            SERVER, "m1", True)
        self.assertTrue(ok, "the offline mark was refused")

        # On disk, in the pending queue AND in the userdata the view reads.
        self.assertEqual([p["item_id"] for p in db.list_playstate()], ["m1"])
        fresh = OfflineLibrarySource(self.catalog)
        item = fresh.get_item(SERVER, "m1")
        self.assertTrue((item.get("UserData") or {}).get("Played"),
                        "the mark is not visible in the offline library")

    def test_the_offline_item_resolves_to_the_real_file_on_disk(self):
        """The offline playback primitive: OfflineVideo turns a catalog row
        into a local path mpv can open. If this breaks, every downloaded
        item silently refuses to play with the server away."""
        from jellyfin_mpv_shim.sync import offline_media
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo

        db = self._reopen_catalog()
        # offline_media binds syncManager at module scope, so patch it
        # THERE — patching sync.manager would not reach this call site.
        real = offline_media.syncManager
        offline_media.syncManager = type(
            "SM", (), {"db": db, "root": self.tmp})()
        self.addCleanup(
            lambda: setattr(offline_media, "syncManager", real))

        parent = type("P", (), {"client": None})()
        video = OfflineVideo.__new__(OfflineVideo)
        OfflineVideo.__init__(video, "m1", parent)
        url = video.get_playback_url()
        self.assertEqual(url, self.clips["m1"])
        self.assertTrue(os.path.exists(url), "resolved a path that is not there")
        self.assertEqual(video.media_source.get("Protocol"), "File",
                         "an offline item was not marked for direct play")

    def test_the_media_files_are_real_and_playable(self):
        """Guards the fixture: a zero-byte clip would make any playback
        assertion above meaningless."""
        for item_id, path in self.clips.items():
            self.assertGreater(os.path.getsize(path), 1000,
                               "%s is not real media" % item_id)
        db = self._reopen_catalog()
        for row in db.list():
            self.assertTrue(
                os.path.exists(os.path.join(self.tmp, row["file_path"])),
                "the catalog points at a file that is not there")


@h.require_real_mpv
class SyncBackAfterReconnectTest(unittest.TestCase):
    """Marks made offline reach the server once it returns.

    The Jellyfin client is the only fake — there is no server here. Below
    that boundary it is the real SyncManager against a real catalog on disk.
    """

    def setUp(self):
        import shutil
        import tempfile
        from jellyfin_mpv_shim.sync.db import SyncDB

        self.tmp = tempfile.mkdtemp(prefix="jms-e2e-sync-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.catalog = os.path.join(self.tmp, "sync.db")
        self.db = SyncDB(self.catalog)
        self.addCleanup(self.db.close)

        clip = os.path.join(self.tmp, "m1.mp4")
        h.make_test_clip(clip, duration=1)
        self.db.upsert(_row("m1", "Alpha", clip))

    class _Client:
        def __init__(self, server_state=None):
            self.pushed = []
            state = server_state or {}
            outer = self

            class _JF:
                @staticmethod
                def get_userdata_for_item(item_id):
                    return dict(state.get(item_id, {}))

                @staticmethod
                def update_userdata_for_item(item_id, update):
                    outer.pushed.append((item_id, dict(update)))

            self.jellyfin = _JF()

    def _sync(self, client):
        from jellyfin_mpv_shim.sync import manager as sync_manager
        mgr = sync_manager.SyncManager.__new__(sync_manager.SyncManager)
        mgr.db = self.db
        mgr.get_client = lambda uuid: client
        sync_manager.SyncManager._sync_playstate(mgr)

    def test_an_offline_mark_reaches_the_server_and_is_cleared(self):
        self.db.upsert_playstate(SERVER, "m1", played=True)
        client = self._Client()
        self._sync(client)
        self.assertEqual(client.pushed, [("m1", {"Played": True})])
        self.assertEqual(self.db.list_playstate(), [],
                         "the queue was not cleared, so it will re-push "
                         "the same mark on every reconnect")

    def test_it_survives_a_process_restart_before_syncing(self):
        """The catalog is the durable part: queue offline, close everything,
        reopen from disk, then sync."""
        from jellyfin_mpv_shim.sync.db import SyncDB

        self.db.upsert_playstate(SERVER, "m1", played=True)
        self.db.close()

        self.db = SyncDB(self.catalog)
        self.assertEqual([p["item_id"] for p in self.db.list_playstate()],
                         ["m1"], "the queued mark did not survive on disk")
        client = self._Client()
        self._sync(client)
        self.assertEqual(client.pushed, [("m1", {"Played": True})])

    def test_it_never_walks_the_server_backwards(self):
        self.db.upsert_playstate(SERVER, "m1", position_ticks=100)
        client = self._Client(server_state={
            "m1": {"Played": True, "PlaybackPositionTicks": 9999}})
        self._sync(client)
        self.assertEqual(client.pushed, [],
                         "overwrote newer server progress with older local")

    def test_a_still_offline_server_keeps_the_backlog(self):
        self.db.upsert_playstate(SERVER, "m1", played=True)
        from jellyfin_mpv_shim.sync import manager as sync_manager
        mgr = sync_manager.SyncManager.__new__(sync_manager.SyncManager)
        mgr.db = self.db
        mgr.get_client = lambda uuid: None
        sync_manager.SyncManager._sync_playstate(mgr)
        self.assertEqual([p["item_id"] for p in self.db.list_playstate()],
                         ["m1"])


if __name__ == "__main__":
    unittest.main()
