"""Live library-browser UI behaviours, driven in-process under a real display.

``gui_mgr`` normally spawns ``BrowserApp`` in a child process, but the app is
directly constructible: we build a real ``BrowserApp`` (real Tk widgets, the real
``run_async`` -> ``_ui_queue`` -> pump path) against a **fake in-memory
LibrarySource**, and pump the UI by hand instead of entering ``mainloop()``.

Gated on tkinter being importable *and* a usable display. The integration runner
wraps this leg in ``xvfb-run`` when headless; a bare machine with no display and
no xvfb self-skips.

The behaviours covered here are the recently-fixed, bug-prone ones:

* navigation stack (navigate / open_item / go_back) updates ``current_view``;
* a stale ``run_async`` result is dropped when the user navigated away
  (``BaseView.run_async`` current-view guard) or a newer request superseded it
  (the epoch guard);
* a ``sync_state`` push swaps the Detail download button *in place* (no full
  rebuild), and ``DownloadsPanel`` coalesces a burst of ``sync_state`` into one
  refresh while ``on_download_progress`` still lands immediately;
* the server switcher is keyed by uuid, so two same-named servers stay distinct;
* the offline-banner Retry does not clear ``work_offline`` without a confirmed
  reconnect.

Determinism: the fake source gates its data methods on ``threading.Event``s so a
race (navigate-away / supersede) is constructed by hand, and UI callbacks are
delivered by draining ``_ui_queue`` explicitly rather than sleeping.
"""

import os
import queue
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

# Keep image_cache / confdir writes off the real profile, and prime the arg
# parser so confdir() doesn't choke on the test runner's argv (the app parses
# sys.argv the first time it resolves the config dir).
_CONF_DIR = tempfile.mkdtemp(prefix="jms-browser-conf-")
os.environ.setdefault("XDG_CONFIG_HOME", _CONF_DIR)
h.prime_args(_CONF_DIR)


def _display_ok():
    """tkinter importable AND a Tk root actually constructs (a DISPLAY env var
    can be set but dead). Cached so we probe once."""
    try:
        import tkinter as tk
    except Exception:
        return False
    if not (h.HAVE_DISPLAY):
        return False
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


HAVE_TK = _display_ok()
require_tk = unittest.skipUnless(HAVE_TK, "library browser UI needs tkinter + a display")


# --------------------------------------------------------------------------
# Fake in-memory LibrarySource
# --------------------------------------------------------------------------

class FakeSource:
    """Implements the LibrarySource surface the views call. Data is in-memory;
    ``image_spec`` returns None so no artwork fetch (network / thumbnail store)
    is ever triggered. Data methods block on ``self.gate_*`` events (open by
    default) so a test can park a request and force a race."""

    def __init__(self, servers, *_a, offline=False, **_k):
        norm = []
        for s in (servers or []):
            if isinstance(s, dict):
                uuid = s.get("uuid") or s.get("Id")
                name = s.get("name") or s.get("Name") or uuid
            else:
                uuid, name = s, s
            if uuid:
                norm.append({"uuid": uuid, "name": name})
        self._servers = norm
        self.offline = offline
        self.gate_item = threading.Event(); self.gate_item.set()
        self.gate_grid = threading.Event(); self.gate_grid.set()
        self.calls = {}

    def _tick(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def servers(self):
        return list(self._servers)

    def stop(self):
        pass

    def reload(self):
        pass

    def get_libraries(self, _server):
        self._tick("get_libraries")
        return [{"Id": "lib1", "Name": "Movies", "Type": "CollectionFolder"}]

    def get_home_rows(self, _server):
        self._tick("get_home_rows")
        return [{"title": "Recently Added",
                 "items": [{"Id": "m1", "Name": "Movie One", "Type": "Movie"}]}]

    def get_library_items(self, _server, _parent, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100):
        self._tick("get_library_items")
        self.gate_grid.wait(5)
        items = [{"Id": "m1", "Name": "Movie One", "Type": "Movie"},
                 {"Id": "m2", "Name": "Movie Two", "Type": "Movie"},
                 {"Id": "m3", "Name": "Movie Three", "Type": "Movie"}]
        if start_index:
            return [], 3
        return items, 3

    def get_seasons(self, _server, _series_id):
        self._tick("get_seasons")
        return [{"Id": "sea1", "Name": "Season 1", "Type": "Season"}]

    def get_episodes(self, _server, _series_id, _season_id):
        return [{"Id": "e1", "Name": "Ep 1", "Type": "Episode"}]

    def get_item(self, _server, item_id):
        self._tick("get_item")
        self.gate_item.wait(5)
        if item_id == "series1":
            return {"Id": "series1", "Name": "A Show", "Type": "Series"}
        return {"Id": item_id, "Name": "Movie One", "Type": "Movie",
                "MediaSources": [{"Id": "src1", "MediaStreams": []}],
                "UserData": {}}

    def get_series_queue(self, *_a, **_k):
        return [{"Id": "e1"}]

    def get_next_up(self, *_a, **_k):
        return {"Id": "e1", "Name": "Ep 1", "Type": "Episode"}

    def search(self, _server, _term, limit=60):
        return [{"Id": "m1", "Name": "Movie One", "Type": "Movie"}]

    def image_spec(self, _item, _image_type="Primary", _width=280):
        return None  # no artwork -> no thumbnail store / network activity

    def image_url(self, *_a, **_k):
        return None

    def backdrop_url(self, *_a, **_k):
        return None


TWO_SERVERS = [
    {"uuid": "srv-a", "name": "Home", "Id": "srv-a", "address": "http://a"},
    {"uuid": "srv-b", "name": "Home", "Id": "srv-b", "address": "http://b"},
]
ONE_SERVER = [{"uuid": "srv-a", "name": "Home", "Id": "srv-a", "address": "http://a"}]


# --------------------------------------------------------------------------
# UI pump helpers (drain _ui_queue explicitly instead of mainloop())
# --------------------------------------------------------------------------

def deliver(app, expect=1, timeout=4):
    """Wait for at least ``expect`` UI callbacks to be posted by run_async
    workers, then run all currently-queued callbacks (mirroring _pump). Blocks
    on the queue (event-driven), so no arbitrary sleeps."""
    got = []
    end = time.time() + timeout
    while len(got) < expect and time.time() < end:
        try:
            got.append(app._ui_queue.get(timeout=max(0.0, end - time.time())))
        except queue.Empty:
            break
    while True:
        try:
            got.append(app._ui_queue.get_nowait())
        except queue.Empty:
            break
    for cb in got:
        cb()
    app.root.update()
    return len(got)


@require_tk
class BrowserUITest(unittest.TestCase):
    def _build_app(self, servers=ONE_SERVER, *, work_offline=False,
                   catalog_path=None, settled_home=True):
        import jellyfin_mpv_shim.library_browser.app as app_mod
        from unittest import mock

        # Swap both source classes for the in-memory fake. The online factory
        # reflects the servers it is handed (so _rebuild_live_source([]) yields
        # an empty source); the offline factory yields the "Downloaded" server.
        self._p1 = mock.patch.object(
            app_mod, "LibrarySource",
            lambda servers, *a, **k: FakeSource(servers))
        self._p2 = mock.patch.object(
            app_mod, "OfflineLibrarySource",
            lambda catalog_path=None, *a, **k: FakeSource(
                [{"uuid": "offline", "name": "Downloaded"}], offline=True))
        self._p1.start(); self.addCleanup(self._p1.stop)
        self._p2.start(); self.addCleanup(self._p2.stop)

        cmd_q, r_q = queue.Queue(), queue.Queue()
        options = {
            "device_id": "dev", "player_name": "mpv-shim",
            "server_list": list(servers), "verify_ssl": True,
            "page_size": 100, "image_width": 200,
            "settings": {"work_offline": work_offline},
            "settings_schema": {}, "sync_state": {},
            "catalog_path": catalog_path,
        }
        app = app_mod.BrowserApp(cmd_q, r_q, list(servers), options)
        self.app = app
        self.cmd_q, self.r_q = cmd_q, r_q
        self.addCleanup(self._teardown_app, app)
        if settled_home:
            deliver(app, expect=1)  # settle the initial Home fetch
        return app

    def _teardown_app(self, app):
        app._closing = True
        # Cancel any pending after() scripts (the _pump loop, thumbnail settles)
        # so they don't fire into a destroyed interpreter and spew Tcl
        # "invalid command name" noise across later tests.
        try:
            for aid in app.root.tk.call("after", "info"):
                try:
                    app.root.after_cancel(aid)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            app._shutdown()
        except Exception:
            try:
                app.root.destroy()
            except Exception:
                pass

    # -- navigation --------------------------------------------------------

    def test_navigate_open_item_and_go_back(self):
        app = self._build_app()
        from jellyfin_mpv_shim.library_browser.views import HomeView, SeriesView

        self.assertIsInstance(app.current_view, HomeView)
        depth = len(app.nav_stack)

        app.open_item({"Id": "series1", "Name": "A Show", "Type": "Series"})
        self.assertEqual(app.nav_stack[-1]["kind"], "series")
        self.assertIsInstance(app.current_view, SeriesView)
        self.assertEqual(len(app.nav_stack), depth + 1)
        deliver(app, expect=1)  # settle the series fetch

        app.go_back()
        self.assertEqual(app.nav_stack[-1]["kind"], "home")
        self.assertIsInstance(app.current_view, HomeView)
        self.assertEqual(len(app.nav_stack), depth)

    # -- stale result guards -----------------------------------------------

    def test_stale_result_dropped_after_navigating_away(self):
        # DetailView's build fetch parks in get_item; we navigate away before it
        # returns. The current-view guard in BaseView.run_async must drop the
        # done callback so it can't render into the torn-down view.
        app = self._build_app()
        from jellyfin_mpv_shim.library_browser.views import DetailView, HomeView

        app.source.gate_item.clear()  # park get_item
        app.navigate({"kind": "detail", "item_id": "m1", "title": "Movie One"})
        detail = app.current_view
        self.assertIsInstance(detail, DetailView)
        self.assertIsNone(detail.item)

        # User navigates home while the detail fetch is still in flight.
        app.navigate({"kind": "home"}, reset=True)
        self.assertIsInstance(app.current_view, HomeView)

        app.source.gate_item.set()      # release the parked get_item
        deliver(app, expect=1)          # deliver detail done (should be dropped)

        self.assertIsNone(detail.item,
                          "stale detail result rendered after navigating away")
        self.assertIsInstance(app.current_view, HomeView)

    def test_superseded_request_dropped_by_epoch(self):
        # GridView: a page fetch is parked; a sort-reset bumps the view's request
        # epoch and starts a fresh fetch. When both return, only the current
        # epoch's result may apply — the grid must show one page, not two.
        app = self._build_app()
        from jellyfin_mpv_shim.library_browser.views import GridView

        app.source.gate_grid.clear()  # park the first page fetch
        app.navigate({"kind": "grid", "parent_id": "lib1", "title": "Movies"})
        grid = app.current_view
        self.assertIsInstance(grid, GridView)

        # Supersede: bump epoch and start a second fetch (like a sort change).
        grid._reset_and_load()

        app.source.gate_grid.set()    # release both parked fetches
        deliver(app, expect=2)        # both done callbacks posted

        # Exactly one page (3 items) applied; the superseded fetch was dropped.
        self.assertEqual(grid.loaded, 3,
                         "superseded page result was not dropped (double-applied)")

    # -- sync_state: in-place download button ------------------------------

    def test_sync_state_swaps_detail_download_button_in_place(self):
        app = self._build_app()
        from jellyfin_mpv_shim.library_browser.views import DetailView

        app.navigate({"kind": "detail", "item_id": "m1", "title": "Movie One"})
        deliver(app, expect=1)
        detail = app.current_view
        self.assertIsInstance(detail, DetailView)
        self.assertIsNotNone(detail.item)

        row_before = detail._actions_row
        item_before = detail.item
        old_btn = detail._download_btn
        self.assertNotIn("Remove", old_btn.cget("text"))

        # A sync_state push marking m1 downloaded should swap only the button.
        app._handle_cmd("sync_state", {"items": ["m1"]})

        self.assertIs(detail.item, item_before, "detail view was rebuilt, not patched")
        self.assertIs(detail._actions_row, row_before, "actions row rebuilt")
        self.assertFalse(old_btn.winfo_exists(), "old download button not destroyed")
        self.assertIn("Remove", detail._download_btn.cget("text"),
                      "download button did not update to the downloaded state")

    # -- DownloadsPanel coalescing -----------------------------------------

    def test_downloads_panel_coalesces_sync_state_and_keeps_progress(self):
        app = self._build_app()
        from jellyfin_mpv_shim.library_browser.views import SettingsView

        app.navigate({"kind": "settings", "tab": "downloads"})
        deliver(app, expect=1)  # settle the panel's initial catalog read
        view = app.current_view
        self.assertIsInstance(view, SettingsView)
        panel = view.downloads_panel
        self.assertIsNotNone(panel)

        refreshes = []
        panel.refresh = lambda: refreshes.append(True)

        # A burst of sync_state (one per item in a batch download) must schedule
        # at most one refresh, not one per push.
        for _ in range(5):
            app._handle_cmd("sync_state", {"items": []})
        self.assertEqual(refreshes, [], "sync_state refreshed synchronously")
        self.assertIsNotNone(panel._refresh_after, "no coalesced refresh scheduled")

        # Meanwhile a progress update for a visible row lands immediately.
        lbl = app.tk.Label(app.root, text="")
        panel._rows = {"m1": lbl}
        panel.on_download_progress({"item_id": "m1", "downloaded": 1, "total": 2})
        self.assertIn("50%", lbl.cget("text"),
                      "on_download_progress dropped during coalescing")

        # When the coalesced timer fires, exactly one refresh runs.
        panel._run_scheduled_refresh()
        self.assertEqual(len(refreshes), 1, "burst did not coalesce to one refresh")
        self.assertIsNone(panel._refresh_after)

    # -- server switcher keyed by uuid -------------------------------------

    def test_server_switcher_keyed_by_uuid_two_same_named_servers(self):
        app = self._build_app(servers=TWO_SERVERS)

        # Both same-named servers are present and distinct in the switcher.
        self.assertEqual(len(app._switcher_servers), 2)
        uuids = {s["uuid"] for s in app._switcher_servers}
        self.assertEqual(uuids, {"srv-a", "srv-b"})

        # Selecting the second entry selects srv-b (not collapsed by name).
        app.server_box.current(1)
        app._on_server_change(None)
        self.assertEqual(app.current_server, app._switcher_servers[1]["uuid"])

        app.server_box.current(0)
        app._on_server_change(None)
        self.assertEqual(app.current_server, app._switcher_servers[0]["uuid"])

    # -- offline banner Retry does not clear work_offline prematurely ------

    def test_banner_retry_keeps_work_offline_until_confirmed_reconnect(self):
        app = self._build_app(servers=TWO_SERVERS, work_offline=True,
                              catalog_path="/nonexistent/catalog.db",
                              settled_home=True)
        self.assertTrue(app.is_offline)
        self.assertTrue(app.settings_values.get("work_offline"))

        # Retry: the user asks to go back online.
        app._on_banner_retry()
        self.assertTrue(app._clear_offline_on_reconnect)
        self.assertTrue(app.settings_values.get("work_offline"),
                        "work_offline cleared before reconnect confirmed")

        # A FAILED reconnect (no connected servers) must not clear the setting.
        app._on_connection_settled({"all": TWO_SERVERS, "connected": []})
        self.assertTrue(app.settings_values.get("work_offline"),
                        "work_offline cleared on a failed reconnect")
        self.assertFalse(app._clear_offline_on_reconnect)

        # A later, user-requested, SUCCESSFUL reconnect clears it and persists.
        app._clear_offline_on_reconnect = True
        app._on_connection_settled({"all": TWO_SERVERS, "connected": TWO_SERVERS})
        self.assertFalse(app.settings_values.get("work_offline"),
                         "work_offline not cleared after a confirmed reconnect")
        saved = self._drain_queue(self.r_q)
        self.assertIn(("save_settings", {"work_offline": False}), saved,
                      "work_offline=False was not persisted to the main process")

    @staticmethod
    def _drain_queue(q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                break
        return out


if __name__ == "__main__":
    unittest.main()
