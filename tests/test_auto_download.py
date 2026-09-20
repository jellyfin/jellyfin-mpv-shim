"""Auto-download: what it fetches, and — more importantly — what it deletes.

This is the only feature that writes to and deletes from the user's disk
without being asked, so most of what is pinned here is restraint: it never
touches a download the user requested, it never runs while something is
playing, and it will not reap an item whose watched state it could not
confirm.
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
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.sync.auto import AutoDownloader  # noqa: E402
from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    ANY_SERVER, NO_ACTOR, SyncDB, STATUS_COMPLETE, STATUS_PENDING, STATUS_ERROR,
    ORIGIN_USER,
    ORIGIN_AUTO_NEXT_UP, ORIGIN_AUTO_LOOKAHEAD,
)
from jellyfin_mpv_shim.sync.manager import SyncManager  # noqa: E402

GB = 1 << 30


class FakeApi:
    """Only the calls the planner makes.

    ``series`` models a real series listing — ``get_episodes`` slices it from
    ``StartItemId``, which is what makes the lookahead *window* observable
    rather than just its first request.
    """

    def __init__(self, next_up=(), episodes=(), series=None,
                 watching=None):
        self._next_up = list(next_up)
        self._episodes = list(episodes)
        #: {series_id: [episode DTO, ...]}, in broadcast order.
        self._series = {k: list(v) for k, v in (series or {}).items()}
        #: {series_id: id of the next episode to watch} — the server's answer
        #: to /Shows/NextUp?seriesId=. Absent means "nothing next".
        self.watching = dict(watching or {})
        self.calls = []

    def get_next(self, index=None, limit=1, series_id=None, fields=None,
                 enable_image_types=None):
        # fields is load-bearing on the library-wide call: without
        # MediaSources every Next Up candidate is charged the unknown-size
        # fallback.
        self.calls.append(("get_next", "/NextUp",
                           {"Limit": limit, "Fields": fields,
                            "SeriesId": series_id}))
        if series_id is not None:
            nxt = self.watching.get(series_id)
            return {"Items": [{"Id": nxt, "Type": "Episode"}] if nxt else []}
        return {"Items": list(self._next_up)}

    def get_episodes(self, series_id, season_id=None, start_item_id=None,
                     fields=None, limit=None):
        self.calls.append(("get_episodes", "/%s/Episodes" % series_id,
                           {"StartItemId": start_item_id, "Limit": limit,
                            "Fields": fields}))
        items = self._series.get(series_id)
        if items is None:
            items = list(self._episodes)
        elif start_item_id is not None:
            ids = [i["Id"] for i in items]
            # StartItemId is inclusive.
            items = (items[ids.index(start_item_id):]
                     if start_item_id in ids else [])
        return {"Items": items[:limit] if limit else list(items)}

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

    @staticmethod
    def content_id_for(server_uuid):
        """Which server a login speaks for. Every login in these tests is on
        the one server the fixtures' rows belong to, so the held set and the
        tombstones resolve to the same content key the rows carry."""
        return CONTENT_SERVER

    def enqueue(self, server_uuid, item_id, item_type, origin=ORIGIN_USER):
        self.enqueued.append((server_uuid, item_id, item_type, origin))
        # Write the row the real one would. Recording the call and nothing
        # else made every pass see a virgin catalog, so no test could observe
        # what a *second* pass does with what the first one queued — which is
        # the only place several of these bugs live.
        # Pending, so no watched state to seed alongside it -- unlike the
        # test case's own `_add`, which mirrors a completed row's.
        self.db.upsert(row(item_id, origin=origin, size=0,
                           status=STATUS_PENDING, server_uuid=server_uuid))
        return 1

    def fail_pending(self, permanent=True):
        """What the download worker does to everything queued, when the
        server refuses all of it.

        ``permanent`` picks which branch: a 4xx or a repeatedly truncated
        response (the code has judged the item itself unfetchable), versus the
        catch-all — a full disk, a bug — which is not the item's fault. The
        tombstone half calls the real `_record_permanent_failure` unbound on
        this fake, so this cannot drift from the code it models.
        """
        for pending in self.db.list(status=STATUS_PENDING):
            self.db.update(pending["item_id"], status=STATUS_ERROR)
            if permanent:
                SyncManager._record_permanent_failure(
                    self, self.db.get(pending["item_id"]))

    def delete(self, item_id=None, only_if_auto=False, **kw):
        """Models `only_if_auto`, which is the whole subject of the reaper's
        claim: a fake that ignored it would delete rows the real manager
        refuses to, making the race untestable while reporting a pass."""
        if only_if_auto:
            if self.db.delete_if_auto(item_id) is None:
                return False
            self.deleted.append(item_id)
            return True
        self.deleted.append(item_id)
        self.db.delete(item_id)
        return True


#: The Jellyfin ServerId these fixtures' rows belong to. Set deliberately:
#: a downloads row with no content server is an ORPHAN, and the orphan path
#: is a distinct contract (docs/offline-sync.md section 1).
#: A fixture that omits this silently tests the orphan path under another
#: name -- which is what every row in this file used to do.
CONTENT_SERVER = "srv-content"


def row(item_id, origin=ORIGIN_AUTO_NEXT_UP, size=1 * GB, status=STATUS_COMPLETE,
        completed_at=None, played=False, series_id="s1", season=1, ep=1,
        server_uuid="srv"):
    return {
        "item_id": item_id, "server_uuid": server_uuid,
        "content_server_id": CONTENT_SERVER,
        "type": "Episode", "name": item_id, "series_id": series_id,
        "series_name": "Show", "season_id": "sea1", "parent_index": season,
        "index_number": ep, "media_source_id": "ms", "file_path": "f",
        "ext": "mkv", "size_bytes": size, "downloaded_bytes": size,
        "status": status, "runtime_ticks": 1, "item_json": "{}",
        "source_json": "{}",
        # Fixture metadata, not a column: `_add` reads it to seed the per-actor
        # table. It used to be spelled `userdata_json`, which was a real column
        # until CX8 dropped it -- and `upsert` ignores what it does not know, so
        # a fixture can go on naming a column that is gone and seed nothing.
        "_played": played,
        "added_at": 1000, "origin": origin,
        "completed_at": completed_at if completed_at is not None else 1000,
    }


#: Login uuid -> the account its saved credential names, as
#: ``(ServerId, UserId)``. ``lan`` and ``wan`` are **two addresses for one
#: server**, which is the configuration the allow-list is keyed on the
#: account for (R14): the connect path registers the live client under
#: whichever of them answered first, so a list of uuids stopped applying
#: whenever that was the other one.
ACCOUNTS = {
    "srv": ("S-srv", "U-izzie"),
    "mine": ("S-mine", "U-izzie"),
    "friend": ("S-friend", "U-izzie"),
    "lan": ("S-mine", "U-izzie"),
    "wan": ("S-mine", "U-izzie"),
}


class AutoTest(unittest.TestCase):

    def _allow(self, *uuids):
        """Stand up a credential registry where ``uuids`` are ticked.

        **Every known login gets a credential whether or not it is ticked**,
        so a server left out of a pass is left out because it is unticked --
        not because the registry could not name its account, which would
        satisfy the same assertion for the wrong reason.
        """
        from jellyfin_mpv_shim.users import userManager

        users = [{"id": "local0",
                  "credentials": [{"uuid": u, "Id": a[0], "UserId": a[1]}
                                  for u, a in ACCOUNTS.items()],
                  "auto_download": [list(ACCOUNTS[u]) for u in uuids]}]
        patcher = mock.patch.object(userManager, "users", users)
        self.addCleanup(patcher.stop)
        patcher.start()
        return users[0]

    def _add(self, record):
        """Insert a catalog row **and** the watched state that goes with it.

        `row()` carries the flag as `_played`, which is not a column at all:
        watched state lives in the per-actor `item_userdata` table and the
        reaper reads that. It used to be spelled `userdata_json`, a real column
        until CX8 dropped it -- and `upsert` ignores what it does not know, so a
        fixture naming a column that is gone seeds nothing and says nothing
        about it.

        These fixtures name no credential, so the actor is the unattributed
        sentinel -- which is the right key for them, and the deletion rule
        is "any actor" regardless.
        """
        self.db.upsert(record)
        self.db.set_watched(record["item_id"], bool(record.get("_played")),
                            actor=(CONTENT_SERVER, NO_ACTOR))
        return record

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
        # The allow-list lives in the user registry now, not the config.
        self._allow("srv")

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
        self._add(row("u1", origin=ORIGIN_USER, played=True))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, [])

    def test_user_downloads_are_never_reaped_for_the_cap(self):
        settings.auto_download_max_gb = 1
        for i in range(5):
            self._add(row("u%d" % i, origin=ORIGIN_USER, size=2 * GB))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, [],
                         "the cap evicted downloads the user asked for")

    def test_user_downloads_do_not_count_against_the_cap(self):
        """Otherwise one big manual download switches auto-download off."""
        self._add(row("u1", origin=ORIGIN_USER, size=100 * GB))
        self.assertEqual(self.db.auto_size(), 0)
        self.assertGreater(self._auto().free_budget(), 0)

    def test_an_incomplete_auto_row_is_not_reaped(self):
        self._add(row("a1", status=STATUS_PENDING, played=True))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, [])


    def test_a_download_claimed_mid_pass_is_not_reaped(self):
        """The user presses Download while the reaper is already walking.

        `reap()` snapshots the complete auto rows and then spends *seconds* in
        the retention loop -- one blocking get_userdata_for_item per row, tens
        of round trips on a real Next Up list (since deleted; the
        snapshot the bug is about is still there). `_delete` then deletes by
        item_id, re-reading nothing. Meanwhile `enqueue` promotes an already
        complete auto row with `set_origin(iid, ORIGIN_USER)`, whose own
        comment states the contract: "A user asking for something the
        scheduler already fetched takes ownership of it, so the reaper stops
        considering it."

        Nothing enforced that. The snapshot still said `auto:` and the episode
        was deleted out from under a user who had just been told it was
        downloading -- with one INFO line as the only trace.

        The promotion happens inside the *delete* of the row before it,
        which is where the window is now that the reaper makes no network
        call: the rows were listed once, up front, and every one of them is
        acted on against that snapshot. It used to be the blocking userdata
        call -- wider, and gone with it -- but the snapshot is what the
        bug was about and the snapshot is still there.
        """
        settings.auto_download_keep_watched_hours = 0
        self._add(row("a1", played=True))
        self._add(row("a2", played=True))
        auto = self._auto()
        claimed = []

        real_delete = self.mgr.delete

        def delete(item_id=None, **kw):
            # The user claims a2 while a1 is being removed.
            if item_id == "a1" and not claimed:
                claimed.append(item_id)
                self.db.set_origin("a2", ORIGIN_USER)
            return real_delete(item_id=item_id, **kw)

        self.mgr.delete = delete
        auto.reap()

        self.assertIsNone(self.db.get("a1"),
                          "the watched auto-download should still be reaped")
        self.assertIsNotNone(
            self.db.get("a2"),
            "the reaper deleted a download the user claimed mid-pass, on the "
            "strength of an origin it read before the claim")

    def test_a_row_promoted_before_the_pass_is_still_safe(self):
        """The static case, kept distinct: this one the snapshot already sees,
        so it passes with or without the atomic re-check and is not evidence
        about the race above."""
        self._add(row("a1", origin=ORIGIN_USER, played=True))
        api = FakeApi()
        api.get_userdata_for_item = lambda item_id: {"Played": True}
        self._auto(clients={"srv": FakeClient(api)}).reap()
        self.assertIsNotNone(self.db.get("a1"))


class ReapPolicyTest(AutoTest):

    def test_watched_is_reaped(self):
        # Explicit, because the default grace is 24h now and
        # this test is about the watched rule rather than about the window.
        settings.auto_download_keep_watched_hours = 0
        self._add(row("a1", played=True))
        self.assertEqual(self._auto().reap(), 1)
        self.assertEqual(self.mgr.deleted, ["a1"])

    def test_watched_is_kept_when_that_is_switched_off(self):
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        self._add(row("a1", played=True))
        self.assertEqual(self._auto().reap(), 0)

    def test_aged_out_unwatched_is_reaped(self):
        old = 100000.0 - (31 * 86400)
        self._add(row("a1", completed_at=int(old)))
        self.assertEqual(self._auto().reap(), 1)

    def test_zero_days_means_never_expire_on_age(self):
        settings.auto_download_keep_days = 0
        self._add(row("a1", completed_at=1))
        self.assertEqual(self._auto().reap(), 0)

    def test_over_cap_evicts_oldest_watched_first(self):
        """Oldest first, and down to *strictly* under the cap.

        Three 1 GB rows in a 2 GB budget. Stopping at exactly 2 GB would
        leave `free_budget()` at zero and the planner queueing nothing, which
        is where a capped folder settles -- so the loop runs one row further.
        `test_the_cap_never_evicts_something_unwatched` is the
        limit on how far that goes.
        """
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 2
        self._add(row("old", size=1 * GB, completed_at=10, played=True))
        self._add(row("mid", size=1 * GB, completed_at=20, played=True))
        self._add(row("new", size=1 * GB, completed_at=30, played=True))
        self._auto().reap()
        self.assertEqual(self.mgr.deleted, ["old", "mid"])

    def test_the_cap_never_evicts_something_unwatched(self):
        """Otherwise it trades the episode you are about to watch for one
        further ahead — churn, and the user asked for watched-only."""
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 1
        self._add(row("a1", size=2 * GB, played=False))
        self.assertEqual(self._auto().reap(), 0)
        self.assertEqual(self.mgr.deleted, [])

    def test_staying_over_the_cap_stops_the_fill(self):
        """When the watched items are not enough, skip rather than reclaim
        space destructively."""
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 1
        self._add(row("a1", size=2 * GB, played=False))
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.run()
        self.assertEqual(self.mgr.enqueued, [])

    def test_a_watched_download_can_be_kept_for_a_while(self):
        """The grace period at its simplest: still here on the pass right
        after it was watched, gone once the window is up."""
        settings.auto_download_keep_watched_hours = 6
        self._add(row("a1", played=True))
        self.assertEqual(self._auto(now=lambda: 100000.0).reap(), 0)
        self.assertIsNotNone(self.db.get("a1"))
        later = 100000.0 + 7 * 3600
        self.assertEqual(self._auto(now=lambda: later).reap(), 1)
        self.assertEqual(self.mgr.deleted, ["a1"])

    def test_an_unconfirmable_watched_state_is_not_reaped(self):
        """No client, and a snapshot that says unwatched: keep it. Being
        wrong in the deleting direction costs the user a re-download."""
        self._add(row("a1", played=False))
        settings.auto_download_keep_days = 0
        self.assertEqual(self._auto(clients={}).reap(), 0)

    def test_the_reaper_asks_nobody(self):
        """It used to ask the server per row, because the catalog's userdata
        was a download-time snapshot. It is not one any more -- the sweep,
        the websocket and `mirror_playstate` all write into it -- and the
        pass now runs *behind* a sweep instead (docs/offline-sync.md section 4,
        `SyncManager._auto_after_sweep`).

        Replaces `test_the_server_overrides_a_stale_unwatched_snapshot`,
        which pinned the branch this deletes. Asserted as "no call" rather
        than "the snapshot wins": a fallback that is merely preferred is
        still a second authority over watched state, and with the per-actor
        split it is one that answers as whoever *downloaded* the copy.
        """
        self._add(row("a1", played=False))
        api = FakeApi()
        asked = []
        api.get_userdata_for_item = lambda item_id: asked.append(item_id)
        auto = self._auto(clients={"srv": FakeClient(api)})
        self.assertEqual(auto.reap(), 0)
        self.assertEqual(asked, [], "the reaper made a network call")


class TheWatchedGracePeriodTest(AutoTest):
    """`auto_download_keep_watched_hours` holds a finished episode on disk for
    a while instead of deleting it on the next check.

    The clock it measures from is the catalog's `watched_at`, stamped the
    first pass that sees the item played. That is state written by the reaper
    and read by the reaper on the pass after, which is the feedback shape this
    repo's testing rules are about -- so almost everything here runs several
    passes over a moving clock rather than asserting one call.
    """

    def setUp(self):
        super().setUp()
        self.clock = [100000.0]
        settings.auto_download_keep_watched_hours = 24
        # Off, so nothing here can be deleted for its age and the only rule
        # under test is the watched one.
        settings.auto_download_keep_days = 0

    def _reap_at(self, when):
        self.clock[0] = when
        return self._auto(now=lambda: self.clock[0]).reap()

    def test_the_deadline_does_not_move_when_passes_keep_running(self):
        """**The one that matters.** The stamp is written by the pass that
        first sees the item watched and read by every pass after it. Rewritten
        each time -- which is what a stamp-on-every-observation would do --
        the deadline walks ahead of the clock by one interval per pass and the
        item is never deleted at all, silently, for as long as the app keeps
        running."""
        self._add(row("a1", played=True))
        start = 100000.0
        # A check every hour, the default cadence, across the whole window.
        for hour in range(1, 24):
            self.assertEqual(self._reap_at(start + hour * 3600), 0,
                             "deleted %d h into a 24 h window" % hour)
        self.assertEqual(self._reap_at(start + 25 * 3600), 1,
                         "23 hourly passes pushed the deadline out of reach")

    def test_the_stamp_does_not_survive_the_setting_being_turned_off(self):
        """The clock is cleared when an item goes un-watched -- but that
        clearing only ran while `auto_download_delete_watched` was on, and
        nothing else in the app touches `watched_at`. So a stamp could
        outlive the un-watch that should have voided it, and the next time
        the item was watched its window was measured from a viewing the user
        had already taken back: it is deleted on the first pass instead of
        getting the grace the setting promises.

        Several passes on each side of the toggle, because the bug is the
        stamp persisting ACROSS them rather than anything one call does.
        """
        start = 100000.0
        self._add(row("a1", played=True))
        # Watched, so the window opens and the stamp is written.
        self.assertEqual(self._reap_at(start), 0)
        self.assertIsNotNone(self.db.get("a1")["watched_at"])

        # The user turns the whole rule off and re-watches the episode, so
        # it is no longer played as far as the server is concerned.
        settings.auto_download_delete_watched = False
        self.db.set_watched("a1", False, actor=(CONTENT_SERVER, NO_ACTOR))
        for hour in (1, 2, 3):
            self._reap_at(start + hour * 3600)
        self.assertIsNone(
            self.db.get("a1")["watched_at"],
            "the clock survived the un-watch because the rule was off, so "
            "it is still counting from a viewing that was taken back")

        # Rule back on, finished again a week later: a full window.
        settings.auto_download_delete_watched = True
        later = start + 7 * 86400
        self.db.set_watched("a1", True, actor=(CONTENT_SERVER, NO_ACTOR))
        self.assertEqual(self._reap_at(later), 0,
                         "deleted the instant it was watched again, with "
                         "none of the grace the setting promises")
        self.assertEqual(self._reap_at(later + 23 * 3600), 0)
        self.assertEqual(self._reap_at(later + 25 * 3600), 1,
                         "and then never deleted at all")

    def test_the_window_is_measured_from_watching_not_from_downloading(self):
        """A series binged months after it was fetched gets the same window as
        one watched the day it arrived. `completed_at` is when we got it and
        answers a different question."""
        self._add(row("a1", played=True,
                           completed_at=int(100000.0 - 200 * 86400)))
        self.assertEqual(self._reap_at(100000.0), 0,
                         "an old download was reaped the moment it was "
                         "watched, so the grace period follows the wrong "
                         "clock")
        self.assertEqual(self._reap_at(100000.0 + 25 * 3600), 1)

    def test_zero_hours_is_exactly_the_old_behaviour(self):
        settings.auto_download_keep_watched_hours = 0
        self._add(row("a1", played=True))
        self.assertEqual(self._reap_at(100000.0), 1)

    def test_a_hand_typed_negative_means_no_grace(self):
        """It is one number with nothing to contradict, so there is only one
        reading of it -- unlike the cap, where a negative is the real
        instruction "allow nothing"."""
        settings.auto_download_keep_watched_hours = -5
        self._add(row("a1", played=True))
        self.assertEqual(self._reap_at(100000.0), 1)

    def test_un_watching_inside_the_window_restarts_it(self):
        """Otherwise an episode un-watched to watch again is deleted part way
        through, on a deadline set by the viewing that was taken back."""
        self._add(row("a1", played=True))
        self._reap_at(100000.0)                       # stamps it
        self.assertIsNotNone(self.db.get("a1")["watched_at"])
        self.db.set_watched("a1", False, actor=(CONTENT_SERVER, NO_ACTOR))
        self._reap_at(100000.0 + 10 * 3600)
        self.assertIsNone(self.db.get("a1")["watched_at"],
                          "the old deadline survived the item being "
                          "un-watched")
        self.db.set_watched("a1", True, actor=(CONTENT_SERVER, NO_ACTOR))
        self._reap_at(100000.0 + 12 * 3600)           # stamps it again
        self.assertEqual(self._reap_at(100000.0 + 30 * 3600), 0,
                         "deleted on the first viewing's deadline")
        self.assertEqual(self._reap_at(100000.0 + 37 * 3600), 1)

    def test_an_item_inside_its_window_is_not_deleted_for_its_age_instead(self):
        """The age rule calls what it deletes "unwatched", and a watched item
        held on purpose is the one thing it definitely is not. Falling through
        to it deletes the item the grace period exists to keep, and logs a
        reason that is false."""
        settings.auto_download_keep_days = 1
        self._add(row("a1", played=True,
                           completed_at=int(100000.0 - 30 * 86400)))
        self.assertEqual(self._reap_at(100000.0), 0,
                         "the grace period was overruled by the age rule")

    def test_running_out_of_space_still_evicts_inside_the_window(self):
        """The decision behind the setting: retention waits, the cap does not.
        A grace period the cap respected would stop automatic downloading
        altogether for the length of the window whenever the budget was
        full."""
        settings.auto_download_max_gb = 1
        self._add(row("a1", size=2 * GB, played=True))
        self.assertEqual(self._reap_at(100000.0), 1,
                         "a full budget was held open by the grace period")

    def test_an_unwatched_item_is_untouched_by_any_of_this(self):
        self._add(row("a1", played=False))
        for hour in (1, 48, 500):
            self.assertEqual(self._reap_at(100000.0 + hour * 3600), 0)
        self.assertIsNone(self.db.get("a1")["watched_at"],
                          "an unwatched item was given a watched time")


class FillTest(AutoTest):

    def test_next_up_items_are_queued_as_auto(self):
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        settings.auto_download_lookahead = 0
        auto = self._auto(clients={"srv": FakeClient(api)})
        self.assertEqual(auto.fill(10 * GB), 1)
        self.assertEqual(self.mgr.enqueued,
                         [("srv", "e1", "Episode", ORIGIN_AUTO_NEXT_UP)])

    def test_already_known_items_are_skipped(self):
        self._add(row("e1"))
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

    def _binge(self, watching, held=("s1e1",), count=6):
        """A six-episode series, some of it held, the user part way through."""
        series = {"s1": [{"Id": "s1e%d" % i, "Type": "Episode"}
                         for i in range(1, count + 1)]}
        for item_id in held:
            self._add(row(item_id, season=1,
                               ep=int(item_id.rsplit("e", 1)[-1])))
        settings.auto_download_next_up = False
        api = FakeApi(series=series, watching={"s1": watching})
        return api, self._auto(clients={"srv": FakeClient(api)})

    def test_lookahead_starts_from_the_episode_you_are_watching(self):
        """Not from the furthest episode held — see the next test for why."""
        api, auto = self._binge(watching="s1e2", held=("s1e1", "s1e5"))
        auto.fill(10 * GB)
        params = next(c[2] for c in api.calls if c[0] == "get_episodes")
        self.assertEqual(params["StartItemId"], "s1e2")
        # Window is [s1e2, s1e3]: the next one to watch, and one past it.
        self.assertEqual([e[1] for e in self.mgr.enqueued], ["s1e2", "s1e3"])

    def test_the_window_does_not_walk_the_series_on_its_own(self):
        """The bug this anchoring exists to prevent: anchored on what is on
        disk, every pass starts where the last one finished downloading, so
        an unwatched series is eventually downloaded whole."""
        api, auto = self._binge(watching="s1e1")
        for _pass in range(4):
            # What a pass queued is on disk by the time the next one runs.
            for _srv, item_id, _type, _origin in self.mgr.enqueued:
                self._add(row(item_id, season=1,
                                   ep=int(item_id[-1])))
            self.mgr.enqueued.clear()
            auto.fill(10 * GB)
        self.assertEqual([e[1] for e in self.mgr.enqueued], [],
                         "the window advanced without anybody watching")
        self.assertEqual(sorted(r["item_id"] for r in self.db.list()),
                         ["s1e1", "s1e2"])

    def test_the_window_advances_when_you_watch(self):
        api, auto = self._binge(watching="s1e1")
        auto.fill(10 * GB)
        self.assertEqual([e[1] for e in self.mgr.enqueued], ["s1e2"])
        for _srv, item_id, _type, _origin in self.mgr.enqueued:
            self._add(row(item_id, season=1, ep=int(item_id[-1])))
        self.mgr.enqueued.clear()
        api.watching["s1"] = "s1e2"     # finished s1e1
        auto.fill(10 * GB)
        self.assertEqual([e[1] for e in self.mgr.enqueued], ["s1e3"])

    def test_a_series_with_nothing_next_is_left_alone(self):
        """Finished, or a server that will not say: either way, guessing is
        how the window runs away."""
        api, auto = self._binge(watching=None)
        auto.fill(10 * GB)
        self.assertEqual(self.mgr.enqueued, [])
        self.assertEqual([c[0] for c in api.calls], ["get_next"],
                         "it asked for episodes without an anchor")

    def test_followed_series_ignores_other_servers(self):
        self._add(row("other", server_uuid="elsewhere", series_id="s2"))
        self._add(row("mine", server_uuid="srv", series_id="s1"))
        auto = self._auto()
        self.assertEqual(auto._followed_series("srv"), {"s1"})

    def test_reaping_watched_frees_room_for_the_same_pass(self):
        """The reaper runs before the planner so a pass that starts over
        budget can still do useful work.

        **Do not give this an explicit `auto_download_keep_watched_hours`.**
        The three other tests that broke when the default moved off zero
        (the 24h grace default) were setup drift and got one; this one is not. It asserts
        what a capped store does with the shipped default in force -- exactly
        full, everything watched, and still able to fetch -- and pinning the
        grace to zero here would hide the behaviour the cap rule exists to
        guarantee rather than test it.
        """
        settings.auto_download_max_gb = 2
        self._add(row("done", size=2 * GB, played=True))
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

    def test_empty_means_no_server(self):
        """A logged-in server may be a friend's. Enabling the feature seeds
        the one you were looking at; nothing else is implied."""
        a, b, clients = self._two_servers()
        self._allow()
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual(self.mgr.enqueued, [])
        self.assertEqual(a.calls + b.calls, [])

    def test_both_servers_when_both_are_listed(self):
        a, b, clients = self._two_servers()
        self._allow("mine", "friend")
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual({e[0] for e in self.mgr.enqueued}, {"mine", "friend"})

    def test_only_the_listed_servers_are_swept(self):
        a, b, clients = self._two_servers()
        self._allow("mine")
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual([e[0] for e in self.mgr.enqueued], ["mine"])
        self.assertEqual(b.calls, [], "the excluded server was queried anyway")

    def test_a_second_address_for_one_server_still_runs(self):
        """The whole reason the allow-list is keyed on the account. The user
        ticked the server while on the LAN; the pass runs with the client
        registered under the remote address, because that is the one that
        answered. Keyed on the uuid this fetched nothing, silently, for as
        long as the trip lasted -- and the "no servers selected" warning
        could not fire, because the list was not empty."""
        a = FakeApi(next_up=[{"Id": "a1", "Type": "Episode"}])
        settings.auto_download_lookahead = 0
        self._allow("lan")
        auto = self._auto(clients={"wan": FakeClient(a)})
        auto.fill(100 * GB)
        self.assertEqual([e[0] for e in self.mgr.enqueued], ["wan"])

    def test_the_legacy_config_key_is_not_consulted(self):
        """It is adopted into the registry at load and cleared; a reader left
        behind here would restore the uuid keying for anyone whose clear
        never landed."""
        a, b, clients = self._two_servers()
        self._allow()
        settings.auto_download_servers = "mine,friend"
        self.addCleanup(setattr, settings, "auto_download_servers", None)
        auto = self._auto(clients=clients)
        auto.fill(100 * GB)
        self.assertEqual(self.mgr.enqueued, [])

    def test_a_registry_that_will_not_answer_fetches_nothing(self):
        """Cannot say is not everybody. An exception here reaching the pass
        as "no filter" would point unattended downloads at every logged-in
        server, including a friend's."""
        from jellyfin_mpv_shim.users import userManager

        a, b, clients = self._two_servers()
        auto = self._auto(clients=clients)
        with mock.patch.object(userManager, "auto_download_accounts",
                               side_effect=RuntimeError("no registry")):
            auto.fill(100 * GB)
        self.assertEqual(self.mgr.enqueued, [])


class BudgetAccountingTest(AutoTest):

    def test_next_up_is_bounded_by_the_limit(self):
        """It pulled 50 on a real library; Next Up is as long as your
        started-series count."""
        settings.auto_download_next_up_limit = 10
        settings.auto_download_lookahead = 0
        api = FakeApi()
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(100 * GB)
        nextup = [c for c in api.calls if c[1] == "/NextUp"]
        self.assertEqual(len(nextup), 1)
        self.assertEqual(nextup[0][2]["Limit"], 10)

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


class NextUpSizingTest(AutoTest):
    """Next Up is a list query and omits MediaSources unless asked. It was
    not asked, so every candidate fell back to the unknown-size charge and
    the cap was spent against a guess for 100% of them."""

    def test_media_sources_are_requested(self):
        settings.auto_download_lookahead = 0
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(100 * GB)
        params = next(c[2] for c in api.calls if c[1] == "/NextUp")
        self.assertIn("MediaSources", params.get("Fields", ""))

    def test_a_real_size_is_charged_not_the_fallback(self):
        settings.auto_download_lookahead = 0
        items = [{"Id": "e%d" % i, "Type": "Episode",
                  "MediaSources": [{"Size": 1 * GB}]} for i in range(6)]
        api = FakeApi(next_up=items)
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(5 * GB)
        # At the 2 GB unknown-size fallback only 3 would fit; at their real
        # 1 GB, 5 do.
        self.assertEqual(len(self.mgr.enqueued), 5)


class DiscardMemoryTest(AutoTest):
    """Dropping an unwatched episode on age is pointless if the next pass
    re-downloads it -- and it would, because it is still Next Up."""

    def setUp(self):
        super().setUp()
        self.old = 100000.0 - (31 * 86400)

    def test_an_aged_out_item_is_tombstoned(self):
        self._add(row("a1", completed_at=int(self.old)))
        self._auto().reap()
        self.assertIn("a1", self.db.discarded_ids(server_id=CONTENT_SERVER))

    def test_a_tombstoned_item_is_not_fetched_again(self):
        self.db.mark_discarded("e1", server_id=CONTENT_SERVER)
        settings.auto_download_lookahead = 0
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        auto = self._auto(clients={"srv": FakeClient(api)})
        auto.fill(100 * GB)
        self.assertEqual(self.mgr.enqueued, [])

    def test_a_watched_reap_leaves_no_tombstone(self):
        """enqueue already skips watched items, and the user may rewatch."""
        self._add(row("a1", played=True))
        self._auto().reap()
        self.assertEqual(self.db.discarded_ids(server_id=CONTENT_SERVER), set())

    def test_a_cap_eviction_leaves_no_tombstone(self):
        """Space pressure is not a judgement that the user does not want
        the episode."""
        settings.auto_download_delete_watched = False
        settings.auto_download_keep_days = 0
        settings.auto_download_max_gb = 1
        self._add(row("a1", size=2 * GB, played=True))
        self._auto().reap()
        self.assertEqual(self.db.discarded_ids(server_id=CONTENT_SERVER), set())


class TombstoneScopeTest(AutoTest):
    """A tombstone outlives the row it is about, so it has to say which
    server it was about.

    Item ids are not unique across servers (docs/jellyfin-api-notes.md 13b),
    and `auto_discarded` was keyed on the id alone. So after server A's copy
    of an id is discarded *and deleted*, server B's different film with the
    same id was invisible to the scheduler for good, with nothing left in the
    catalog to explain why. docs/offline-sync.md section 4.
    """

    def test_a_tombstone_binds_only_the_server_it_was_made_for(self):
        self.db.mark_discarded("x", server_id="A")
        self.assertEqual(self.db.discarded_ids(server_id="A"), {"x"})
        self.assertEqual(self.db.discarded_ids(server_id="B"), set())

    def test_two_servers_hold_their_own_tombstones_for_one_id(self):
        """Three steps, because two is not enough: a schema with room for
        only one tombstone per id passes the A-then-check-B test and then
        loses A's the moment B records one."""
        self.db.mark_discarded("x", server_id="A")
        self.db.mark_discarded("x", server_id="B")
        self.assertEqual(self.db.discarded_ids(server_id="A"), {"x"})
        self.assertEqual(self.db.discarded_ids(server_id="B"), {"x"})

    def test_clearing_one_server_leaves_the_other(self):
        self.db.mark_discarded("x", server_id="A")
        self.db.mark_discarded("x", server_id="B")
        self.db.clear_discarded("x", server_id="A")
        self.assertEqual(self.db.discarded_ids(server_id="A"), set())
        self.assertEqual(self.db.discarded_ids(server_id="B"), {"x"})

    def test_clearing_with_no_server_leaves_every_servers_tombstone(self):
        """The exact mirror of `mark_discarded`, which writes the *unscoped*
        row for a falsy server and never a scoped one.

        Reached whenever `content_id_for` cannot resolve the login -- it
        answers None for "cannot tell" rather than raising -- and it used to
        `DELETE FROM auto_discarded_scoped WHERE item_id=?`, wiping every
        server's tombstone for a film the user asked for from one of them.
        """
        self.db.mark_discarded("x", server_id="A")
        self.db.mark_discarded("x", server_id="B")
        self.db.mark_discarded("x", server_id=None)     # the broad one

        self.db.clear_discarded("x", server_id=None)

        self.assertEqual(self.db.discarded_ids(server_id="A"), {"x"},
                         "server A's tombstone was cleared by a request "
                         "that could not say which server it was about")
        self.assertEqual(self.db.discarded_ids(server_id="B"), {"x"})

    def test_the_broad_tombstone_is_what_a_falsy_clear_does_remove(self):
        """...and it does have to remove that one, or a request nobody can
        attribute could never override a suppression that binds it."""
        self.db.mark_discarded("x", server_id=None)
        self.db.clear_discarded("x", server_id=None)
        self.assertEqual(self.db.discarded_ids(server_id="A"), set())

    def test_a_discard_that_cannot_name_a_server_records_nothing(self):
        """**This asserted the opposite until step 5**, and the reason it did
        was sound as far as it went: we cannot say which server the row was
        about, so suppressing everywhere beat suppressing nowhere.

        What it cost is the bug the scoped table was added for -- a tombstone
        binding every server hides a *different* server's film of the same id
        from the scheduler, forever, with nothing left in the catalog to
        explain it. So nothing is recorded. The cost of that is one redundant
        fetch: the planner only asks on behalf of a connected server, so the
        candidate it is no longer suppressed from names one, and the row that
        comes back does too -- making the next discard scoped and permanent.
        """
        self.db.mark_discarded("x", server_id=None)
        self.assertEqual(self.db.discarded_ids(server_id="A"), set())
        self.assertEqual(self.db.discarded_ids(server_id="B"), set())
        self.assertEqual(self.db.discarded_ids(server_id=ANY_SERVER), set(),
                         "a row was written somewhere after all")

    def test_a_scoped_discard_is_still_recorded_and_still_scoped(self):
        """The control: "records nothing" must not have become "records
        nothing, ever"."""
        self.db.mark_discarded("x", server_id="A")
        self.assertEqual(self.db.discarded_ids(server_id="A"), {"x"})
        self.assertEqual(self.db.discarded_ids(server_id="B"), set())


class InterruptionTest(AutoTest):
    """A pass is dozens of blocking HTTP calls. stop() joins the worker with
    a short timeout and closes the catalog regardless, so a pass that
    ignores shutdown gets its writes dropped and its deletes applied to a
    catalog that can no longer record them."""

    def test_shutdown_stops_the_reaper(self):
        # Explicit: with the 24h default grace these five rows are
        # inside their window, so the pass would have deleted nothing whether
        # or not it obeyed the flag -- the assertion could not fail.
        settings.auto_download_keep_watched_hours = 0
        for i in range(5):
            self._add(row("a%d" % i, played=True))
        mgr = FakeManager(self.db)
        auto = AutoDownloader(mgr, get_clients=lambda: {},
                              should_stop=lambda: True,
                              now=lambda: 100000.0)
        self.assertEqual(auto.reap(), 0)
        self.assertEqual(mgr.deleted, [])

    def test_the_workers_predicate_beats_the_constructors(self):
        """A superseded worker's `_stop` is down again -- the worker that
        replaced it cleared the flag -- so the constructor's predicate says
        "carry on" and only the one the worker hands `tick` knows better.
        Nothing called `tick` with an argument until the worker did, so this
        pins the override rather than the flag."""
        settings.auto_download_keep_watched_hours = 0
        for i in range(5):
            self._add(row("a%d" % i, played=True))
        mgr = FakeManager(self.db)
        auto = AutoDownloader(mgr, get_clients=lambda: {},
                              should_stop=lambda: False,
                              now=lambda: 100000.0)
        self.assertEqual(auto.tick(should_stop=lambda: True),
                         {"queued": 0, "reaped": 0})
        self.assertEqual(mgr.deleted, [])

        # The control: the same pass without the override does delete, or the
        # assertion above would hold for a fixture that had nothing to reap.
        auto.last_run = 0.0
        auto.tick()
        self.assertEqual(len(mgr.deleted), 5)

    def test_playback_starting_stops_the_fill(self):
        """is_busy was sampled once in due(); a pass then ran for minutes,
        queueing downloads that competed with the stream."""
        settings.auto_download_lookahead = 0
        api = FakeApi(next_up=[{"Id": "e%d" % i, "Type": "Episode"}
                               for i in range(5)])
        auto = self._auto(clients={"srv": FakeClient(api)},
                          is_busy=lambda: True)
        self.assertEqual(auto.fill(100 * GB), 0)


class NegativeCapTest(AutoTest):
    def test_a_negative_cap_allows_nothing(self):
        """Hand-editing -1 means "off"; clamping it up to 0 would hand the
        user unlimited instead."""
        settings.auto_download_max_gb = -1
        self.assertEqual(self._auto().free_budget(), 0)

    def test_zero_is_still_unlimited(self):
        settings.auto_download_max_gb = 0
        self.assertEqual(self._auto().free_budget(), float("inf"))


class FailedRowReclaimTest(AutoTest):
    def test_error_rows_are_reclaimed(self):
        """They hold .part bytes and count against the cap, so nothing else
        would."""
        self._add(row("bad", status=STATUS_ERROR))
        self.assertEqual(self._auto().reap(), 1)
        self.assertEqual(self.mgr.deleted, ["bad"])

    def _five_passes(self, permanent):
        settings.auto_download_lookahead = 0
        api = FakeApi(next_up=[{"Id": "e1", "Type": "Episode"}])
        auto = self._auto(clients={"srv": FakeClient(api)})
        for _pass in range(5):
            auto.run()
            self.mgr.fail_pending(permanent=permanent)
        return [e[1] for e in self.mgr.enqueued]

    def test_a_permanently_failed_item_is_not_fetched_every_pass(self):
        """Reclaiming the row destroys the only record that we already tried
        this — and reap runs one call before fill, in the same pass. The item
        is still unwatched and still Next Up, so without a tombstone it comes
        straight back: five passes, five attempts at a download that cannot
        succeed, for as long as the app runs.
        """
        self.assertEqual(self._five_passes(permanent=True), ["e1"])

    def test_a_failure_that_was_not_the_items_fault_is_retried(self):
        """The deliberate asymmetry. A full disk or a bug in us ends the
        moment the environment is fixed, and blacklisting every episode that
        met a full disk would quietly gut auto-download with nothing to show
        for it. Only the branches that judged the *item* unfetchable
        tombstone; do not 'fix' this one by widening that.
        """
        self.assertEqual(self._five_passes(permanent=False), ["e1"] * 5)

    def test_asking_for_it_by_hand_clears_the_failure(self):
        """The one signal that outranks the scheduler's decision, and the
        user's only way back from a wrong call."""
        self._five_passes(permanent=True)
        self.assertEqual(self.db.discarded_ids(server_id=CONTENT_SERVER), {"e1"})
        self.db.clear_discarded("e1", server_id=CONTENT_SERVER)
        self.assertEqual(self.db.discarded_ids(server_id=CONTENT_SERVER), set())
