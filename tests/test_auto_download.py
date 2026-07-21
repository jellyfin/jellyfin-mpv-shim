"""Auto-download: what it fetches, and — more importantly — what it deletes.

This is the only feature that writes to and deletes from the user's disk
without being asked, so most of what is pinned here is restraint: it never
touches a download the user requested, it never runs while something is
playing, and it will not reap an item whose watched state it could not
confirm.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.sync.auto import AutoDownloader  # noqa: E402
from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    SyncDB, STATUS_COMPLETE, STATUS_PENDING, ORIGIN_USER,
    ORIGIN_AUTO_NEXT_UP, ORIGIN_AUTO_LOOKAHEAD,
)

GB = 1 << 30


class FakeApi:
    """Only the two calls the planner makes."""

    def __init__(self, next_up=(), episodes=()):
        self._next_up = list(next_up)
        self._episodes = list(episodes)
        self.calls = []

    def get_next(self, limit=1):
        self.calls.append(("get_next", limit))
        return {"Items": list(self._next_up)}

    def shows(self, handler, params=None):
        self.calls.append(("shows", handler, params))
        return {"Items": list(self._episodes)}

    def get_userdata_for_item(self, item_id):
        return None       # "server reachable but says nothing"


class FakeClient:
    def __init__(self, api):
        self.jellyfin = api


class FakeManager:
    """Real SyncDB, recorded enqueues, recorded deletes."""

    def __init__(self, db, clients=None):
        self.db = db
        self.enqueued = []
        self.deleted = []
        self._clients = clients or {}

    def get_client(self, server_uuid):
        return self._clients.get(server_uuid)

    def enqueue(self, server_uuid, item_id, item_type, origin=ORIGIN_USER):
        self.enqueued.append((server_uuid, item_id, item_type, origin))
        return 1

    def delete(self, item_id=None, **kw):
        self.deleted.append(item_id)
        self.db.delete(item_id)


def row(item_id, origin=ORIGIN_AUTO_NEXT_UP, size=1 * GB, status=STATUS_COMPLETE,
        completed_at=None, played=False, series_id="s1", season=1, ep=1,
        server_uuid="srv"):
    return {
        "item_id": item_id, "server_id": "S", "server_uuid": server_uuid,
        "type": "Episode", "name": item_id, "series_id": series_id,
        "series_name": "Show", "season_id": "sea1", "parent_index": season,
        "index_number": ep, "media_source_id": "ms", "file_path": "f",
        "ext": "mkv", "size_bytes": size, "downloaded_bytes": size,
        "status": status, "runtime_ticks": 1, "item_json": "{}",
        "source_json": "{}",
        "userdata_json": '{"Played": %s}' % ("true" if played else "false"),
        "added_at": 1000, "origin": origin,
        "completed_at": completed_at if completed_at is not None else 1000,
    }


class AutoTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        self._saved = {k: getattr(settings, k) for k in dir(settings)
                       if k.startswith("auto_download_")}
        self.addCleanup(self._restore)
        settings.auto_download_enable = True
        settings.auto_download_next_up = True
        settings.auto_download_lookahead = 2
        settings.auto_download_max_gb = 10
        settings.auto_download_delete_watched = True
        settings.auto_download_keep_days = 30
        settings.auto_download_interval_mins = 60
        settings.auto_download_next_up_limit = 10
        settings.auto_download_servers = None

    def _restore(self):
        for k, v in self._saved.items():
            setattr(settings, k, v)

    def _auto(self, clients=None, is_busy=None, now=None):
        mgr = FakeManager(self.db, clients)
        self.mgr = mgr
        return AutoDownloader(mgr, get_clients=lambda: clients or {},
                              is_busy=is_busy or (lambda: False),
                              now=now or (lambda: 100000.0))


class SchedulingTest(AutoTest):

    def test_disabled_never_runs(self):
        settings.auto_download_enable = False
        self.assertFalse(self._auto().due())

    def test_it_stands_down_while_playing(self):
        """The whole point of scheduling it: never compete with streaming."""
        self.assertFalse(self._auto(is_busy=lambda: True).due())

    def test_it_runs_when_idle(self):
        self.assertTrue(self._auto().due())

    def test_it_waits_for_the_interval(self):
        auto = self._auto()
        auto.last_run = 100000.0 - 60      # 1 minute ago, interval is 60 min
        self.assertFalse(auto.due())
        auto.last_run = 100000.0 - 3601
        self.assertTrue(auto.due())

    def test_tick_swallows_failures(self):
        """It runs on the shared download worker; raising here would stop the
        user's own downloads too."""
        auto = self._auto()
        auto.run = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertIsNone(auto.tick())

    def test_a_busy_tick_does_not_consume_the_interval(self):
        auto = self._auto(is_busy=lambda: True)
        auto.tick()
        self.assertEqual(auto.last_run, 0.0, "a skipped run reset the clock")


class ReapProtectionTest(AutoTest):
    """What must never be deleted."""

    def test_user_downloads_are_never_reaped_when_watched(self):
        self.db.upsert(row("u1", origin=ORIGIN_USER, played=True))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, [])

    def test_user_downloads_are_never_reaped_for_the_cap(self):
        settings.auto_download_max_gb = 1
        for i in range(5):
            self.db.upsert(row("u%d" % i, origin=ORIGIN_USER, size=2 * GB))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, [],
                         "the cap evicted downloads the user asked for")

    def test_user_downloads_do_not_count_against_the_cap(self):
        """Otherwise one big manual download switches auto-download off."""
        self.db.upsert(row("u1", origin=ORIGIN_USER, size=100 * GB))
        self.assertEqual(self.db.auto_size(), 0)
        self.assertGreater(self._auto().free_budget(), 0)

    def test_an_incomplete_auto_row_is_not_reaped(self):
        self.db.upsert(row("a1", status=STATUS_PENDING, played=True))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, [])


class ReapPolicyTest(AutoTest):

    def test_watched_is_reaped(self):
        self.db.upsert(row("a1", played=True))
        self.assertEqual(self._auto().reap(), 1)
        self.assertEqual(self.mgr.deleted, ["a1"])

    def test_watched_is_kept_when_that_is_switched_off(self):
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        self.db.upsert(row("a1", played=True))
        self.assertEqual(self._auto().reap(), 0)

    def test_aged_out_unwatched_is_reaped(self):
        old = 100000.0 - (31 * 86400)
        self.db.upsert(row("a1", completed_at=int(old)))
        self.assertEqual(self._auto().reap(), 1)

    def test_zero_days_means_never_expire_on_age(self):
        settings.auto_download_keep_days = 0
        self.db.upsert(row("a1", completed_at=1))
        self.assertEqual(self._auto().reap(), 0)

    def test_over_cap_evicts_oldest_watched_first(self):
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 2
        self.db.upsert(row("old", size=1 * GB, completed_at=10, played=True))
        self.db.upsert(row("mid", size=1 * GB, completed_at=20, played=True))
        self.db.upsert(row("new", size=1 * GB, completed_at=30, played=True))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, ["old"])

    def test_the_cap_never_evicts_something_unwatched(self):
        """Otherwise it trades the episode you are about to watch for one
        further ahead — churn, and the user asked for watched-only."""
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 1
        self.db.upsert(row("a1", size=2 * GB, played=False))
        self.assertEqual(self._auto().reap(), 0)
        self.assertEqual(self.mgr.deleted, [])

    def test_staying_over_the_cap_stops_the_fill(self):
        """When the watched items are not enough, skip rather than reclaim
        space destructively."""
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 1
        self.db.upsert(row("a1", size=2 * GB, played=False))
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.run()
        self.assertEqual(self.mgr.enqueued, [])

    def test_an_unconfirmable_watched_state_is_not_reaped(self):
        """No client, and a snapshot that says unwatched: keep it. Being
        wrong in the deleting direction costs the user a re-download."""
        self.db.upsert(row("a1", played=False))
        settings.auto_download_keep_days = 0
        self.assertEqual(self._auto(clients={}).reap(), 0)

    def test_the_server_overrides_a_stale_unwatched_snapshot(self):
        """userdata_json is captured at download time, so it says unwatched
        forever if trusted alone."""
        self.db.upsert(row("a1", played=False))
        api = FakeApi()
        api.get_userdata_for_item = lambda item_id: {"Played": True}
        auto = self._auto(clients={"srv": FakeClient(api)})
        self.assertEqual(auto.reap(), 1)


class FillTest(AutoTest):

    def test_next_up_items_are_queued_as_auto(self):
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        settings.auto_download_lookahead = 0
        auto = self._auto(clients={"srv": FakeClient(api)})
        self.assertEqual(auto.fill(10 * GB), 1)
        self.assertEqual(self.mgr.enqueued,
                         [("srv", "e1", "Episode", ORIGIN_AUTO_NEXT_UP)])

    def test_already_known_items_are_skipped(self):
        self.db.upsert(row("e1"))
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        settings.auto_download_lookahead = 0
        auto = self._auto(clients={"srv": FakeClient(api)})
        self.assertEqual(auto.fill(10 * GB), 0)
        self.assertEqual(self.mgr.enqueued, [])

    def test_the_budget_stops_the_fill(self):
        items = [{"Id": "e%d" % i, "Type": "Episode",
                  "MediaSources": [{"Size": 4 * GB}]} for i in range(5)]
        api = FakeApi(next_up=items)
        settings.auto_download_lookahead = 0
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(10 * GB)
        self.assertEqual(len(self.mgr.enqueued), 3,
                         "the budget did not bound the queue")

    def test_next_up_can_be_switched_off(self):
        settings.auto_download_next_up = False
        settings.auto_download_lookahead = 0
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(10 * GB)
        self.assertEqual(self.mgr.enqueued, [])
        self.assertEqual(api.calls, [], "it asked anyway")

    def test_lookahead_starts_from_the_furthest_episode_held(self):
        self.db.upsert(row("s1e1", season=1, ep=1))
        self.db.upsert(row("s1e5", season=1, ep=5))
        api = FakeApi(episodes=[{"Id": "s1e5", "Type": "Episode"},
                                {"Id": "s1e6", "Type": "Episode"},
                                {"Id": "s1e7", "Type": "Episode"}])
        settings.auto_download_next_up = False
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(10 * GB)
        params = next(c[2] for c in api.calls if c[0] == "shows")
        self.assertEqual(params["StartItemId"], "s1e5")
        # The first result is the episode we already hold.
        self.assertEqual([e[1] for e in self.mgr.enqueued], ["s1e6", "s1e7"])

    def test_the_frontier_ignores_other_servers(self):
        self.db.upsert(row("other", server_uuid="elsewhere", ep=9))
        self.db.upsert(row("mine", server_uuid="srv", ep=2))
        auto = self._auto()
        self.assertEqual(auto._series_frontier("srv"), {"s1": "mine"})

    def test_reaping_watched_frees_room_for_the_same_pass(self):
        """The reaper runs before the planner so a pass that starts over
        budget can still do useful work."""
        settings.auto_download_max_gb = 2
        self.db.upsert(row("done", size=2 * GB, played=True))
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        settings.auto_download_lookahead = 0
        auto = self._auto(clients={"srv": FakeClient(api)})
        result = auto.run()
        self.assertEqual(result["reaped"], 1)
        self.assertEqual([e[1] for e in self.mgr.enqueued], ["e1"])


class MigrationTest(unittest.TestCase):
    """The catalog predates these columns and has no migration framework."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "catalog.db")

    #: The `downloads` table exactly as it shipped before origin/completed_at.
    #: Written out in full rather than trimmed: _SCHEMA indexes series_id, so
    #: a shortened fixture fails to open for a reason real catalogs never hit.
    _LEGACY_DDL = """
    CREATE TABLE downloads (
        item_id TEXT PRIMARY KEY, server_id TEXT, server_uuid TEXT,
        type TEXT, name TEXT, series_id TEXT, series_name TEXT,
        season_id TEXT, parent_index INTEGER, index_number INTEGER,
        media_source_id TEXT, file_path TEXT, ext TEXT,
        size_bytes INTEGER DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0,
        status TEXT, runtime_ticks INTEGER, item_json TEXT,
        source_json TEXT, userdata_json TEXT, added_at INTEGER
    )"""

    def _legacy_db(self):
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.execute(self._LEGACY_DDL)
        conn.execute(
            "INSERT INTO downloads (item_id, server_uuid, status, "
            "downloaded_bytes, added_at) VALUES ('old','srv','complete',5,1)")
        conn.commit()
        conn.close()

    def test_the_columns_are_added_to_an_existing_catalog(self):
        self._legacy_db()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(downloads)")}
        self.assertIn("origin", cols)
        self.assertIn("completed_at", cols)

    def test_pre_existing_downloads_are_marked_user_owned(self):
        """The one that matters: defaulting these to 'auto' would let the
        first reaper run delete a library the user built by hand."""
        self._legacy_db()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.get("old")["origin"], ORIGIN_USER)

    def test_migrating_twice_is_a_no_op(self):
        self._legacy_db()
        SyncDB(self.path).close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.get("old")["origin"], ORIGIN_USER)


class ServerScopeTest(AutoTest):
    """A logged-in server may be a friend's; unattended downloads should not
    be pointed at someone else's hardware without being asked."""

    def _two_servers(self):
        a = FakeApi(next_up=[{"Id": "a1", "Type": "Episode"}])
        b = FakeApi(next_up=[{"Id": "b1", "Type": "Episode"}])
        settings.auto_download_lookahead = 0
        return a, b, {"mine": FakeClient(a), "friend": FakeClient(b)}

    def test_empty_means_every_server(self):
        a, b, clients = self._two_servers()
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual({e[0] for e in self.mgr.enqueued}, {"mine", "friend"})

    def test_only_the_listed_servers_are_swept(self):
        a, b, clients = self._two_servers()
        settings.auto_download_servers = "mine"
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual([e[0] for e in self.mgr.enqueued], ["mine"])
        self.assertEqual(b.calls, [], "the excluded server was queried anyway")

    def test_whitespace_and_blanks_are_tolerated(self):
        a, b, clients = self._two_servers()
        settings.auto_download_servers = " mine , , "
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual([e[0] for e in self.mgr.enqueued], ["mine"])


class BudgetAccountingTest(AutoTest):

    def test_next_up_is_bounded_by_the_limit(self):
        """It pulled 50 on a real library; Next Up is as long as your
        started-series count."""
        settings.auto_download_next_up_limit = 10
        settings.auto_download_lookahead = 0
        api = FakeApi()
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(100 * GB)
        self.assertEqual([c for c in api.calls if c[0] == "get_next"],
                         [("get_next", 10)])

    def test_an_unknown_size_still_costs_budget(self):
        """Counting these as free let an unbounded number through: the cap is
        checked against anticipated bytes, and the reaper only evicts watched
        items, so nothing corrects the overshoot afterwards."""
        items = [{"Id": "e%d" % i, "Type": "Episode"} for i in range(10)]
        api = FakeApi(next_up=items)
        settings.auto_download_lookahead = 0
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(5 * GB)
        self.assertLess(len(self.mgr.enqueued), 10,
                        "unsized items were queued for free")

    def test_a_pass_is_capped_in_item_count(self):
        items = [{"Id": "e%d" % i, "Type": "Episode",
                  "MediaSources": [{"Size": 1}]} for i in range(200)]
        api = FakeApi(next_up=items)
        settings.auto_download_lookahead = 0
        settings.auto_download_max_gb = 0        # unlimited
        auto = self._auto(clients={"srv": FakeClient(api)})
        queued = auto.fill(auto.free_budget())
        self.assertLessEqual(queued, 20, "one pass stampeded the queue")


if __name__ == "__main__":
    unittest.main()
