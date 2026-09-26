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
  episodes (PlayerGateway._queue_offline_watched).
* backdrop_spec keys header art by the real backdrop tag per source.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from jellyfin_mpv_shim.users import UserManager
from jellyfin_mpv_shim.clients import ClientManager
from jellyfin_mpv_shim.mpvtk_browser import gateway as browser_gw
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

    def test_a_uuid_already_filed_is_replaced_rather_than_duplicated(self):
        """Two credentials under one uuid is never a valid state, however it
        is reached: the server switcher keys by uuid so the duplicate is
        invisible, every save writes both, and every connect races them.

        Reached for real by re-authenticating while the user switches
        accounts. `_finalize_login` replaces in place on its own branch, but
        the after-a-switch branch files through here instead -- so the rule
        was applied at one of its two sites, which is the shape this repo
        keeps producing. Fixing it here covers both.
        """
        um = self.fresh()
        other = um.add_user("other")
        first = {"uuid": "u1", "address": "http://old", "username": "a"}
        self.assertTrue(um.append_credentials_for(other["id"], first))
        again = {"uuid": "u1", "address": "http://new", "username": "a"}
        self.assertTrue(um.append_credentials_for(other["id"], again))
        stored = self.fresh().get(other["id"])["credentials"]
        self.assertEqual(stored, [again],
                         "one uuid, two credentials: %r" % (stored,))

    def test_but_a_different_uuid_still_appends(self):
        """The negative control. This is still how a SECOND server reaches a
        user who was switched away mid-login."""
        um = self.fresh()
        other = um.add_user("other")
        um.append_credentials_for(other["id"], {"uuid": "u1", "address": "a"})
        um.append_credentials_for(other["id"], {"uuid": "u2", "address": "b"})
        stored = self.fresh().get(other["id"])["credentials"]
        self.assertEqual([c["uuid"] for c in stored], ["u1", "u2"])


class AtomicSaveTest(UserManagerTestBase):
    def test_users_save_leaves_no_temp_file(self):
        um = self.fresh()
        um.add_user("other")
        path = os.path.join(self.tmp, "users.json")
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path + ".tmp"))
        with open(path, encoding="utf-8") as f:
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
            with open(cfg, encoding="utf-8") as f:
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
    read them back out of the catalog — the fan-out rule (a series or season
    id expands to its downloaded episodes) is the thing under test either
    way."""
    browser_gw.PlayerGateway._queue_offline_watched(
        server_uuid, item_id, True)
    return [(p["item_id"], p["user_id"])
            for p in _sync().db.list_playstate()]


class OfflineWatchTargetsTest(unittest.TestCase):
    """The fan-out, against a real catalog on disk.

    Built on a real ``SyncDB`` rather than a mock that models
    ``watched_targets``: the rule is a query now, scoped like every other
    content read, and a double standing in for the query can only restate
    whatever this file already believes. The resolver is real too — see
    tests/test_offline_actor_e2e.py for why a stand-in for it is the one
    thing that may not appear.
    """

    #: A saved login, the Jellyfin server behind it, and the person. Named
    #: rather than left to fall through to the unattributed sentinel: the
    #: replay queue refuses an entry nobody can be named for, so without
    #: this every assertion below would be about a mark production drops.
    LOGIN, SERVER_ID, USER_ID = "srv", "SRV", "U1"

    def setUp(self):
        from jellyfin_mpv_shim.sync.db import SyncDB
        from jellyfin_mpv_shim.users import userManager

        self.tmp = tempfile.mkdtemp(prefix="jms-watch-targets-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        for item_id, series, season in (("e1", "S", "sea1"),
                                        ("e2", "S", "sea2"),
                                        ("m1", None, None)):
            self.db.upsert({"item_id": item_id, "status": "complete",
                            "type": "Episode" if series else "Movie",
                            "name": item_id, "series_id": series,
                            "season_id": season,
                            "server_uuid": self.LOGIN,
                            "content_server_id": self.SERVER_ID,
                            "file_path": item_id + ".mkv"})
        for attr, value in (
                ("users", [{"id": "local", "credentials": [
                    {"uuid": self.LOGIN, "Id": self.SERVER_ID,
                     "UserId": self.USER_ID}]}]),
                ("active_id", "local")):
            patch = mock.patch.object(userManager, attr, value)
            self.addCleanup(patch.stop)
            patch.start()
        patch = mock.patch.object(_sync(), "db", self.db)
        self.addCleanup(patch.stop)
        patch.start()

    def test_leaf_item(self):
        self.assertEqual(_watch_targets("m1", self.LOGIN),
                         [("m1", self.USER_ID)])

    def test_series_fans_out(self):
        self.assertEqual({t[0] for t in _watch_targets("S", self.LOGIN)},
                         {"e1", "e2"})

    def test_season_fans_out(self):
        self.assertEqual([t[0] for t in _watch_targets("sea2", self.LOGIN)],
                         ["e2"])

    def test_unknown_id_yields_nothing(self):
        self.assertEqual(_watch_targets("??", self.LOGIN), [])

    def test_another_servers_rows_are_not_reachable(self):
        """The scope, which the fan-out did not have. A login on another
        server marking a series it does not own used to fan out over this
        server's episodes and file the marks under an account that exists
        on neither."""
        from jellyfin_mpv_shim.users import userManager
        with mock.patch.object(userManager, "users", [
                {"id": "local", "credentials": [
                    {"uuid": "other", "Id": "OTHER", "UserId": "U2"}]}]):
            self.assertEqual(_watch_targets("S", "other"), [])
        self.assertEqual(self.db.userdata_actors("e1"), [])


def _episode(eid, sid, season, played, season_name=None, pidx=1, idx=1):
    return {"Id": eid, "Type": "Episode", "SeriesId": sid, "SeriesName": "Show",
            "SeasonId": season, "SeasonName": season_name,
            "ParentIndexNumber": pidx, "IndexNumber": idx,
            "UserData": {"Played": played}}


def _offline_source(items, series_ids=()):
    src = OfflineLibrarySource.__new__(OfflineLibrarySource)
    src.catalog_path = None
    src.root = None
    src._snap = _OfflineSnapshot(items=items, series_ids=series_ids)
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

    def test_a_synthesized_series_is_poster_shaped(self):
        """GridPage._grid_shape takes the median PrimaryImageAspectRatio
        across the row and falls back to SQUARE when nothing carries one, so
        a downloaded-shows grid came out as square cards with the posters
        letterboxed inside them."""
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        src = _offline_source([_episode("e1", "S", "sea1", played=False)])
        ratio = src._series_list()[0].get("PrimaryImageAspectRatio")
        self.assertIsNotNone(ratio, "no shape for the grid to read")
        self.assertLess(ratio, TileRenderer.SQUARE_RATIO,
                        "a downloaded show is not square")

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
            series_ids={"S"})
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
    """The header banner's fallback chain: backdrop, parent backdrop, thumb,
    parent thumb, then the item's own still IF it is landscape."""

    def test_own_backdrop_tag_wins(self):
        item = {"Id": "i", "BackdropImageTags": ["t1"],
                "ParentBackdropImageTags": ["p1"], "ParentBackdropItemId": "P",
                "ImageTags": {"Thumb": "th"}}
        self.assertEqual(LibrarySource.backdrop_spec(item),
                         ("i", "Backdrop", "t1"))

    def test_parent_backdrop_fallback(self):
        item = {"Id": "i", "ParentBackdropImageTags": ["p1"],
                "ParentBackdropItemId": "P"}
        self.assertEqual(LibrarySource.backdrop_spec(item),
                         ("P", "Backdrop", "p1"))

    def test_a_thumb_beats_having_nothing(self):
        item = {"Id": "i", "ImageTags": {"Thumb": "th", "Primary": "p"}}
        self.assertEqual(LibrarySource.backdrop_spec(item),
                         ("i", "Thumb", "th"))

    def test_a_parent_thumb_is_next(self):
        item = {"Id": "i", "ParentThumbItemId": "P",
                "ParentThumbImageTag": "pt"}
        self.assertEqual(LibrarySource.backdrop_spec(item),
                         ("P", "Thumb", "pt"))

    def test_a_home_video_uses_its_own_still(self):
        """The case this chain was extended for: video scanned out of a
        folder has no backdrop and never will, and its header was a blank
        grey box. The still the server extracted is a frame of the thing
        itself."""
        for ratio in (16 / 9, 4 / 3, 1.0):
            with self.subTest(ratio=round(ratio, 2)):
                item = {"Id": "i", "Type": "Video",
                        "PrimaryImageAspectRatio": ratio,
                        "ImageTags": {"Primary": "p"}}
                self.assertEqual(LibrarySource.backdrop_spec(item),
                                 ("i", "Primary", "p"))

    def test_a_poster_is_not_a_banner(self):
        """A 2:3 poster cropped into a 2.67:1 banner is a strip through the
        middle of the key art -- worse than the placeholder, because it reads
        as a rendering fault rather than as missing artwork."""
        item = {"Id": "i", "Type": "Movie", "PrimaryImageAspectRatio": 2 / 3,
                "ImageTags": {"Primary": "p"}}
        self.assertIsNone(LibrarySource.backdrop_spec(item))

    def test_an_unmeasured_primary_is_not_used_either(self):
        """Unknown shape, and the downside is asymmetric: a wrong guess here
        mutilates the header, while the placeholder is merely plain."""
        item = {"Id": "i", "ImageTags": {"Primary": "p"}}
        self.assertIsNone(LibrarySource.backdrop_spec(item))

    def test_no_backdrop(self):
        self.assertIsNone(LibrarySource.backdrop_spec({"Id": "i"}))

    def test_offline_sentinel_keys_apart_from_online_tags(self):
        # The offline spec must never collide with a real server tag, so a
        # source switch can't serve the other source's cached bitmap.
        self.assertEqual(OfflineLibrarySource.backdrop_spec({"Id": "i"}),
                         ("i", "offline", "offline"))


if __name__ == "__main__":
    unittest.main()
