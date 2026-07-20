"""Regression tests for the 2026-07 UI-layer review fixes.

Each test class is anchored to one confirmed finding:

* switch_result must always be sent — a silent drop wedges the browser's
  modal PIN dialog forever. (Tk-only; removed with it.)
* A login that finishes after a user switch must file its credential under
  the initiating user, not the now-active one (clients._finalize_login).
* Stale browser_died notices (raced by a tray relaunch) must not tear down
  the replacement browser. (Tk-only; removed with it.)
* A second Quick Connect must supersede (cancel) the first flow.
* Settings/users saves are atomic (temp file + os.replace) and serialized.
* The offline source publishes one immutable snapshot, synthesizes UserData
  for series/seasons, and memoizes artwork path resolution.
* Offline watched-marks fan out from a series/season to its downloaded
  episodes (_PlayerController._queue_offline_watched).
* backdrop_spec keys header art by the real backdrop tag per source.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from jellyfin_mpv_shim.users import UserManager
from jellyfin_mpv_shim.clients import ClientManager
from jellyfin_mpv_shim.mpvtk_browser import ui as browser_ui
from jellyfin_mpv_shim.mpvtk_browser.repository import (
    LibrarySource, OfflineLibrarySource, _OfflineSnapshot,
)


class UserManagerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patcher = mock.patch(
            "jellyfin_mpv_shim.users.conffile.get",
            side_effect=lambda app, conf_file, create=False: os.path.join(
                self.tmp, conf_file),
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fresh(self):
        um = UserManager()
        um.load()
        return um


class AppendCredentialsForTest(UserManagerTestBase):
    def test_appends_to_named_user_and_persists(self):
        um = self.fresh()
        other = um.add_user("other")
        cred = {"uuid": "u1", "address": "http://x", "username": "a"}
        self.assertTrue(um.append_credentials_for(other["id"], cred))
        # Active (default) user untouched; target user got the credential.
        self.assertEqual(um.credentials_for_active(), [])
        um2 = self.fresh()
        target = um2.get(other["id"])
        self.assertEqual(target["credentials"], [cred])

    def test_missing_user_returns_false(self):
        um = self.fresh()
        self.assertFalse(um.append_credentials_for("nope", {"uuid": "u1"}))


class AtomicSaveTest(UserManagerTestBase):
    def test_users_save_leaves_no_temp_file(self):
        um = self.fresh()
        um.add_user("other")
        path = os.path.join(self.tmp, "users.json")
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path + ".tmp"))
        with open(path) as f:
            json.load(f)  # must be valid JSON

    def test_settings_save_is_atomic_and_serialized(self):
        from jellyfin_mpv_shim import conf
        cfg = os.path.join(self.tmp, "conf.json")
        with mock.patch.object(conf, "config_path", cfg):
            settings = conf.Settings()
            # Hammer save from several threads; with the lock + os.replace
            # the result must always be complete, valid JSON.
            threads = [threading.Thread(target=settings.save)
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            with open(cfg) as f:
                data = json.load(f)
            self.assertIn("player_name", data)
            self.assertFalse(os.path.exists(cfg + ".tmp"))


def _bare_client_manager():
    """A ClientManager without __init__ (no health-check thread), with just
    the state _finalize_login touches."""
    cm = ClientManager.__new__(ClientManager)
    cm.credentials = []
    cm.clients = {}
    cm.usernames = {}
    cm._client_lock = threading.RLock()
    cm._switch_lock = threading.RLock()
    cm._removed_uuids = set()
    cm.connect_client = mock.Mock(return_value=True)
    cm._disconnect_client = mock.Mock()
    cm.save_credentials = mock.Mock()
    return cm


def _fake_authed_client():
    client = mock.Mock()
    client.auth.credentials.get_credentials.return_value = {
        "Servers": [{"Id": "srv-id", "address": "http://x"}]
    }
    return client


class FinalizeLoginOwnerTest(unittest.TestCase):
    def test_same_owner_appends_and_connects(self):
        cm = _bare_client_manager()
        with mock.patch("jellyfin_mpv_shim.clients.userManager") as um:
            um.active_id = "user-a"
            self.assertTrue(cm._finalize_login(_fake_authed_client(), "alice",
                                               owner_id="user-a"))
            um.append_credentials_for.assert_not_called()
        self.assertEqual(len(cm.credentials), 1)
        self.assertEqual(cm.credentials[0]["username"], "alice")
        cm.save_credentials.assert_called_once()
        cm.connect_client.assert_called_once()

    def test_switched_owner_files_under_original_user(self):
        cm = _bare_client_manager()
        with mock.patch("jellyfin_mpv_shim.clients.userManager") as um:
            um.active_id = "user-b"  # switched away mid-login
            um.append_credentials_for.return_value = True
            self.assertTrue(cm._finalize_login(_fake_authed_client(), "alice",
                                               owner_id="user-a"))
            um.append_credentials_for.assert_called_once()
            (owner, cred), _kw = um.append_credentials_for.call_args
            self.assertEqual(owner, "user-a")
            self.assertEqual(cred["username"], "alice")
        # Must NOT leak into the (now user-b) live list or start a client.
        self.assertEqual(cm.credentials, [])
        cm.save_credentials.assert_not_called()
        cm.connect_client.assert_not_called()


def _sync():
    from jellyfin_mpv_shim.sync import manager
    return manager.syncManager


def _watch_targets(item_id, server_uuid):
    """What the in-window browser would mark watched, offline.

    _queue_offline_watched applies the marks rather than returning them, so
    record what it wrote — the fan-out rule (a series or season id expands
    to its downloaded episodes) is the thing under test either way."""
    written = []
    db = _sync().db
    db.upsert_playstate.side_effect = (
        lambda srv, iid, played=None: written.append((iid, srv)))
    browser_ui._PlayerController._queue_offline_watched(
        server_uuid, item_id, True)
    return written


class OfflineWatchTargetsTest(unittest.TestCase):
    def _db(self):
        db = mock.Mock()
        db.list.return_value = [
            {"item_id": "e1", "series_id": "S", "season_id": "sea1",
             "server_uuid": "srv"},
            {"item_id": "e2", "series_id": "S", "season_id": "sea2",
             "server_uuid": "srv"},
            {"item_id": "m1", "series_id": None, "season_id": None,
             "server_uuid": "srv"},
        ]
        return db

    def test_leaf_item(self):
        db = self._db()
        db.is_complete.return_value = True
        with mock.patch.object(_sync(), "db", db):
            targets = _watch_targets("m1", "srv")
        self.assertEqual(targets, [("m1", "srv")])

    def test_series_fans_out(self):
        db = self._db()
        db.is_complete.return_value = False
        with mock.patch.object(_sync(), "db", db):
            targets = _watch_targets("S", "srv")
        self.assertEqual({t[0] for t in targets}, {"e1", "e2"})

    def test_season_fans_out(self):
        db = self._db()
        db.is_complete.return_value = False
        with mock.patch.object(_sync(), "db", db):
            targets = _watch_targets("sea2", "srv")
        self.assertEqual([t[0] for t in targets], ["e2"])

    def test_unknown_id_yields_nothing(self):
        db = self._db()
        db.is_complete.return_value = False
        with mock.patch.object(_sync(), "db", db):
            targets = _watch_targets("??", "srv")
        self.assertEqual(targets, [])


def _episode(eid, sid, season, played, season_name=None, pidx=1, idx=1):
    return {"Id": eid, "Type": "Episode", "SeriesId": sid, "SeriesName": "Show",
            "SeasonId": season, "SeasonName": season_name,
            "ParentIndexNumber": pidx, "IndexNumber": idx,
            "UserData": {"Played": played}}


def _offline_source(items, series_server=None):
    src = OfflineLibrarySource.__new__(OfflineLibrarySource)
    src.catalog_path = None
    src.root = None
    src._snap = _OfflineSnapshot(items=items,
                                 series_server=series_server or {})
    return src


class OfflineUserdataAggregationTest(unittest.TestCase):
    def test_series_list_aggregates_watched_state(self):
        src = _offline_source([
            _episode("e1", "S", "sea1", played=True),
            _episode("e2", "S", "sea1", played=True, idx=2),
            _episode("e3", "T", "sea9", played=False),
        ])
        series = {s["Id"]: s for s in src._series_list()}
        self.assertTrue(series["S"]["UserData"]["Played"])
        self.assertEqual(series["S"]["UserData"]["UnplayedItemCount"], 0)
        self.assertFalse(series["T"]["UserData"]["Played"])
        self.assertEqual(series["T"]["UserData"]["UnplayedItemCount"], 1)

    def test_get_seasons_aggregates_watched_state(self):
        src = _offline_source([
            _episode("e1", "S", "sea1", played=True),
            _episode("e2", "S", "sea2", played=False, pidx=2),
        ])
        seasons = {s["Id"]: s for s in src.get_seasons("offline", "S")}
        self.assertTrue(seasons["sea1"]["UserData"]["Played"])
        self.assertFalse(seasons["sea2"]["UserData"]["Played"])

    def test_get_item_series_fallback_carries_userdata(self):
        src = _offline_source(
            [_episode("e1", "S", "sea1", played=True)],
            series_server={"S": "srv"})
        item = src.get_item("offline", "S")
        self.assertEqual(item["Type"], "Series")
        self.assertTrue(item["UserData"]["Played"])


class ArtPathMemoTest(unittest.TestCase):
    def test_resolution_is_cached_per_snapshot(self):
        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        src.catalog_path = "/tmp/cat.db"
        src.root = "/tmp"
        snap = _OfflineSnapshot(rows={"m1": {"item_id": "m1",
                                             "file_path": "srv/movie/m1/f.mkv"}})
        src._snap = snap
        with mock.patch("jellyfin_mpv_shim.mpvtk_browser.repository."
                        "os.path.exists", return_value=True) as exists:
            first = src._art_path("m1", "Primary")
            calls = exists.call_count
            second = src._art_path("m1", "Primary")
            self.assertEqual(exists.call_count, calls)  # served from memo
        self.assertEqual(first, second)
        self.assertIn(("m1", "Primary"), snap.art_cache)

    def test_reload_invalidates_by_replacing_snapshot(self):
        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        src.catalog_path = None
        src.root = None
        src._snap = _OfflineSnapshot()
        src._snap.art_cache[("x", "Primary")] = "/stale"
        src.reload()  # empty catalog -> fresh snapshot
        self.assertNotIn(("x", "Primary"), src._snap.art_cache)


class BackdropSpecTest(unittest.TestCase):
    def test_own_backdrop_tag_wins(self):
        item = {"Id": "i", "BackdropImageTags": ["t1"],
                "ParentBackdropImageTags": ["p1"], "ParentBackdropItemId": "P"}
        self.assertEqual(LibrarySource.backdrop_spec(item), ("i", "t1"))

    def test_parent_backdrop_fallback(self):
        item = {"Id": "i", "ParentBackdropImageTags": ["p1"],
                "ParentBackdropItemId": "P"}
        self.assertEqual(LibrarySource.backdrop_spec(item), ("P", "p1"))

    def test_no_backdrop(self):
        self.assertIsNone(LibrarySource.backdrop_spec({"Id": "i"}))

    def test_offline_sentinel_keys_apart_from_online_tags(self):
        # The offline spec must never collide with a real server tag, so a
        # source switch can't serve the other source's cached bitmap.
        self.assertEqual(OfflineLibrarySource.backdrop_spec({"Id": "i"}),
                         ("i", "offline"))


if __name__ == "__main__":
    unittest.main()
