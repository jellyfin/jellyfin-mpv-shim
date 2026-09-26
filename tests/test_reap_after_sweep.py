"""The reaper runs behind the sweep, and what "behind" is allowed to mean.

The reaper's own live "is this watched" query was deleted and replaced
it with an ordering: *reap after the sweep*. That is only worth anything if
"after" means the sweep actually landed -- `_sweep_if_due` clears its own
flag before asking anything and swallows each server's failures, so "a sweep
happened" is true of a pass that refreshed nothing.

The guarantee bounds it: *"succeeded, with a bound. When offline don't require a
sweep, let the offline watched grace period carry it."* So there are three
outcomes, not two -- run, hold, and hold-expired -- and the offline path must
be the *running* one, because sustained offline use is exactly when a reap is
still wanted and no sweep will ever arrive.

Both a request and a hold are needed and neither works alone; the two ways of
getting this wrong are recorded in docs/offline-sync.md section 4,
because both looked right.
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
import threading
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.sync import manager as mgr             # noqa: E402
from jellyfin_mpv_shim.sync.db import (COLUMNS, ORIGIN_AUTO_NEXT_UP,  # noqa: E402
                                       STATUS_COMPLETE, STATUS_PENDING,
                                       SyncDB)

SERVER = "0ccef36552284944ab0d183114fcbe92"     # the QA server's real ServerId
OTHER = "8178115f38154c1ebc64afe778261fda"
ALICE = "403227586bf64cd7943953120e56c1a3"      # qa-admin
BOB = "c9c0df6c120d4b2faf4c61c9b2676b82"        # qa-user


def _row(item_id, server_id=SERVER, origin=ORIGIN_AUTO_NEXT_UP,
         status=STATUS_COMPLETE, asked_by=None):
    """One catalog row, attributed to the account that asked for it.

    `asked_by` is not decoration: the sweep is scoped to the union R21 names,
    so a row belonging to nobody the machine is signed in as is deliberately
    **not** asked about -- and then a hold test would be measuring a sweep
    that never had anything to do. It defaults to Alice on this row's **own**
    server, which is the shape a real pair has: an account is
    (ServerId, UserId), so the server half always matches the row it asked
    for. Defaulting it to `SERVER` regardless put every row on `OTHER`
    outside every scope, which reads as "the second server answered" and
    quietly turned a hold test into its opposite.
    """
    asked_by = asked_by or (server_id, ALICE)
    row = {c: None for c in COLUMNS}
    row.update({"item_id": item_id, "status": status, "type": "Episode",
                "name": item_id, "file_path": "%s/f.mkv" % item_id,
                "content_server_id": server_id, "server_uuid": "login-a",
                "requested_server_id": asked_by[0],
                "requested_user_id": asked_by[1],
                "origin": origin, "downloaded_bytes": 1,
                "item_json": json.dumps({"Id": item_id})})
    return row


class FakeAuto:
    """The scheduler as the worker sees it: `due()` then `tick()`.

    Both, because the ordering is the subject. A stand-in with only `tick`
    would be answered AttributeError by the manager's own guard and read as
    "not due", so every hold test would pass for the wrong reason.
    """

    def __init__(self, due=True):
        self._due = due
        self.ticks = 0
        #: What the worker handed this pass as its stop predicate, per tick.
        #: Modelled because the real `tick` takes it: a stand-in that did not
        #: would be *more permissive* than the thing it stands for, and the
        #: call site that forgot to pass it would still pass here.
        self.stops = []

    def due(self):
        return self._due

    def tick(self, should_stop=None):
        self.ticks += 1
        self.stops.append(should_stop)


class ReapFixture(unittest.TestCase):
    """One catalog, one connected login, and a scheduler that records ticks.

    A base class rather than a mixin so the two suites below share it without
    either inheriting the other's tests -- subclassing a TestCase runs its
    cases again under the subclass's name, which reports one failure twice
    and doubles the module's count.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        self.db.upsert(_row("a1"))

        from jellyfin_mpv_shim.users import userManager
        patch = mock.patch.object(userManager, "users", [{
            "id": "local", "credentials": [
                {"uuid": "login-a", "Id": SERVER, "UserId": ALICE},
                {"uuid": "login-b", "Id": SERVER, "UserId": BOB},
                {"uuid": "login-far", "Id": OTHER, "UserId": ALICE},
            ]}])
        self.addCleanup(patch.stop)
        patch.start()

        self.asked = []
        m = mgr.SyncManager.__new__(mgr.SyncManager)
        m.db = self.db
        m._stop = False
        m._wake = threading.Event()
        m._notify_change = lambda: None
        m._sweep_due = True
        m._started_at = 0.0
        m._last_userdata = 0.0
        m._answered_accounts = set()
        m._reap_hold_until = None
        m.auto = FakeAuto()
        m.get_clients = lambda: {"login-a": self._client()}
        m.get_client = lambda uuid: (self._client() if uuid == "login-a"
                                     else None)
        self.m = m

    def _client(self):
        asked = self.asked

        class Api:
            @staticmethod
            def get_items(ids, fields=""):
                asked.append(list(ids))
                return {"Items": []}

        class Client:
            jellyfin = Api()

        return Client()


class ReapOrderTest(ReapFixture):

    # -- the seven cases -----------------------------------------------------

    def test_a_sweep_that_landed_lets_the_pass_run(self):
        self.assertIsNone(self.m._auto_after_sweep(1000.0, None))
        self.assertEqual(len(self.asked), 1, "the sweep did not happen")
        self.assertEqual(self.m.auto.ticks, 1)

    def test_the_startup_settle_holds_the_first_reap(self):
        """`last_run` starts at zero, so a pass is due the moment the worker
        starts -- while USERDATA_SWEEP_SETTLE holds the first sweep back a
        minute. Without the hold, the first reap of every session decides
        deletions from download-time snapshots."""
        self.m._started_at = 1000.0
        self.m._auto_after_sweep(1000.0 + 1, None)
        self.assertEqual(self.asked, [], "swept inside the settle")
        self.assertEqual(self.m.auto.ticks, 0, "reaped before any sweep")

    def test_and_releases_once_the_settle_is_over(self):
        self.m._started_at = 1000.0
        self.m._auto_after_sweep(1000.0 + 1, None)
        self.m._auto_after_sweep(1000.0 + mgr.USERDATA_SWEEP_SETTLE + 1, None)
        self.assertEqual(self.m.auto.ticks, 1)

    def test_the_floor_does_not_hold_a_pass_the_sweep_already_covered(self):
        """The floor stops a flapping server costing one sweep per flap. It
        must not also stop the *reap*: that server has been answered, and
        holding on a rate limit is how retention silently switches off."""
        self.m._auto_after_sweep(1000.0, None)
        self.m.auto = FakeAuto()
        self.m._auto_after_sweep(1000.0 + 1, None)
        self.assertEqual(len(self.asked), 1, "the floor should hold a sweep")
        self.assertEqual(self.m.auto.ticks, 1, "but not the reap")

    def test_a_runnable_row_defers_the_whole_thing(self):
        """Eligibility is checked *after* `_next_runnable`. Asking first
        would fire a network sweep ahead of a queued user download, for a
        pass that cannot run anyway."""
        row = self.db.get("a1")
        self.assertIs(self.m._auto_after_sweep(1000.0, row), row)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.m.auto.ticks, 0)

    def test_busy_playback_defers_it_too(self):
        """`auto.due()` is where playback is checked, so a busy machine
        never reaches the sweep request either."""
        self.m.auto = FakeAuto(due=False)
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.m.auto.ticks, 0)

    def test_offline_reaps_without_a_sweep(self):
        """Nobody is signed in, so no sweep is coming and holding
        for one would switch retention off for the whole of a trip. The
        watched grace period carries this case instead."""
        self.m.get_clients = lambda: {}
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.m.auto.ticks, 1)

    def test_a_client_list_failure_reads_as_offline(self):
        """We cannot tell whether it is offline or broken, and holding on
        what we cannot tell is the starvation direction -- the failure
        `_next_runnable` records having fixed once already."""
        def boom():
            raise RuntimeError("registry locked")
        self.m.get_clients = boom
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(self.m.auto.ticks, 1)

    def test_a_sweep_that_failed_holds_the_pass(self):
        """The case the whole mechanism is for: a client answers, so we are
        not offline, but the request raised. `_sweep_if_due` has already
        cleared its own flag and swallowed the error, so "did a sweep run"
        cannot tell this from success."""
        class Api:
            @staticmethod
            def get_items(ids, fields=""):
                raise RuntimeError("500")

        class Client:
            jellyfin = Api()

        self.m.get_clients = lambda: {"login-a": Client()}
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(self.m.auto.ticks, 0, "reaped on a failed sweep")

    # -- the bound -----------------------------------------------------------

    def test_a_half_answered_server_is_not_an_answered_one(self):
        """`_refresh_userdata` batches a server's ids and breaks out of that
        loop on the first failure, so a server can be half refreshed. Fifty
        rows fit one batch and are not this case; this is two batches with
        the second raising."""
        for i in range(mgr.USERDATA_BATCH + 1):
            self.db.upsert(_row("e%d" % i))
        calls = []

        class Api:
            @staticmethod
            def get_items(ids, fields=""):
                calls.append(list(ids))
                if len(calls) > 1:
                    raise RuntimeError("500")
                return {"Items": []}

        class Client:
            jellyfin = Api()

        self.m._wake = mock.Mock()      # do not really wait out the spacing
        self.m.get_clients = lambda: {"login-a": Client()}
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(len(calls), 2, "the second batch was never asked")
        self.assertEqual(self.m.auto.ticks, 0,
                         "half a server counted as an answered one")

    def test_the_hold_expires_so_a_broken_server_cannot_stop_retention(self):
        class Api:
            @staticmethod
            def get_items(ids, fields=""):
                raise RuntimeError("500")

        class Client:
            jellyfin = Api()

        self.m.get_clients = lambda: {"login-a": Client()}
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(self.m.auto.ticks, 0)
        # Still inside the window.
        self.m._auto_after_sweep(1000.0 + mgr.REAP_SWEEP_HOLD - 1, None)
        self.assertEqual(self.m.auto.ticks, 0)
        self.m._auto_after_sweep(1000.0 + mgr.REAP_SWEEP_HOLD, None)
        self.assertEqual(self.m.auto.ticks, 1)

    def test_the_deadline_starts_when_the_hold_does_not_when_the_app_did(self):
        """A session that ran for hours before a second server showed up
        broken gets the full window, not none of it."""
        self.m._auto_after_sweep(1000.0, None)          # succeeds
        self.assertEqual(self.m.auto.ticks, 1)

        class Api:
            @staticmethod
            def get_items(ids, fields=""):
                raise RuntimeError("500")

        class Client:
            jellyfin = Api()

        # A second server signs in, is asked, and refuses.
        self.db.upsert(_row("late", server_id=OTHER))
        self.m.get_clients = lambda: {"login-a": self._client(),
                                      "login-far": Client()}
        self.m._last_userdata = 0.0     # past the floor
        self.m.auto = FakeAuto()
        later = 1000.0 + 10 * mgr.REAP_SWEEP_HOLD
        self.m._auto_after_sweep(later, None)
        self.assertEqual(self.m.auto.ticks, 0)
        self.m._auto_after_sweep(later + mgr.REAP_SWEEP_HOLD - 1, None)
        self.assertEqual(self.m.auto.ticks, 0, "the window was measured from "
                         "the start of the session rather than the hold")
        self.m._auto_after_sweep(later + mgr.REAP_SWEEP_HOLD, None)
        self.assertEqual(self.m.auto.ticks, 1)

    def test_a_server_answered_once_is_never_waited_on_again(self):
        """The reading taken of "answered", stated so it can be argued with.

        A server counts as answered for the **session**, not for each pass --
        meaning the *hold* never fires for it again. The pass still requests
        a sweep and still gets one (the test below); what it does not do is
        wait for the result before reaping.

        The failure the ordering closes is a reaper deciding from a *download-time
        snapshot*; once a sweep has written into the catalog for a server the
        websocket keeps it live, so blocking on a second reading adds delay
        rather than safety. Per-pass would also make the hold routine instead
        of exceptional -- every pass whose hour fell inside the sweep floor
        would wait the floor out with nothing wrong.

        A profile switch invalidates it without anything having to say so,
        because the set is keyed on the **account** that answered and the new
        profile's accounts were never in it (D1).
        """
        self.m._auto_after_sweep(1000.0, None)
        self.m.auto = FakeAuto()
        # Inside the floor, so no sweep can go out at all -- which is what
        # makes this about the hold rather than about the request.
        self.m._auto_after_sweep(1000.0 + 1, None)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.m.auto.ticks, 1)

    def test_the_pass_asks_for_a_sweep_even_when_one_already_landed(self):
        """The request half, which the hold cannot stand in for.

        Hours into a session the trigger flag is down: a server *appearing*
        is what raises it and no server has appeared. The hold does not fire
        either, because this server was answered at launch. So without the
        request nothing asks, and the reap decides deletions from state that
        is hours old -- the same failure wearing a different hat, and the
        second of the two ways that section records getting this wrong.

        Survivor of the 2026-09-13 mutation round: `_sweep_due = True` could
        be deleted outright and every test here still passed, because they
        all start with the flag already up the way a fresh manager does.
        """
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(self.m._sweep_due,
                         "the sweep is supposed to consume its own flag")

        self.m.auto = FakeAuto()
        later = 1000.0 + 4 * 3600
        self.m._auto_after_sweep(later, None)
        self.assertEqual(len(self.asked), 2,
                         "the reap ran on state four hours old")
        self.assertEqual(self.m.auto.ticks, 1)

    def test_a_server_nothing_is_signed_in_for_is_not_waited_on(self):
        """A second server whose account is not connected -- the lazy case
        (docs/offline-sync.md section 3). Its rows keep the state they have
        and its absence must
        not hold the reap for the server that *is* connected."""
        self.db.upsert(_row("far", server_id=OTHER))
        self.m._auto_after_sweep(1000.0, None)
        self.assertEqual(self.m.auto.ticks, 1)

    def test_a_pass_that_queued_something_hands_the_row_back(self):
        """`tick()` can queue downloads, and the worker starts one in the
        same iteration rather than sleeping five seconds first."""
        def tick(should_stop=None):
            self.db.upsert(_row("queued", status=STATUS_PENDING))
            self.m.auto.ticks += 1
        self.m.auto.tick = tick
        row = self.m._auto_after_sweep(1000.0, None)
        self.assertIsNotNone(row)
        self.assertEqual(row["item_id"], "queued")


class ASwitchNeedsNoScheduleOfItsOwnTest(ReapFixture):
    """D1 deleted `request_profile_sweep`, and this is what has to be true
    for that to be safe rather than merely smaller.

    An early rule wanted a deferred account's state refreshed shortly after a
    switch. R16 in the narrow form removed the deferred model, so a switch is
    a reconnect: `stop_all_clients` + `connect_all` empties the connected set
    and refills it with the new profile's uuids, which is already a trigger.
    The old reason for not trusting that -- two profiles sharing a credential
    uuid -- rested on `force_unique`, a branch nothing passed and step 6
    deleted; a credential uuid is now always a fresh uuid4.

    Built on ReapOrderTest's fixture so the *hold* can be asserted, which is
    the half that would silently stop working.
    """

    def test_the_new_profile_does_not_inherit_the_old_ones_answer(self):
        """The single thing `request_profile_sweep` existed to prevent, now
        true by construction: Alice answered, Bob is at the keyboard, and the
        reap waits for a sweep as Bob rather than reading Alice's."""
        self.m._answered_accounts = {(SERVER, ALICE)}
        self.m.get_clients = lambda: {"login-b": self._client()}
        self.assertTrue(self.m._sweep_owed(1000.0),
                        "the reap ran on the other account's answer")

    def test_and_it_stops_waiting_once_that_account_has_answered(self):
        """The other direction, or the check above would pass against a hold
        that never releases."""
        self.m._answered_accounts = {(SERVER, BOB)}
        self.m.get_clients = lambda: {"login-b": self._client()}
        self.assertFalse(self.m._sweep_owed(1000.0))

    def test_the_same_account_through_a_second_address_is_still_answered(self):
        """The account is the key, so Alice reconnecting by another door is
        not a new person to wait for. Keyed on the login this held for
        REAP_SWEEP_HOLD on every address change."""
        self.db.upsert(_row("far", server_id=OTHER))
        self.m._answered_accounts = {(SERVER, ALICE), (OTHER, ALICE)}
        self.m.get_clients = lambda: {"login-far": self._client()}
        self.assertFalse(self.m._sweep_owed(1000.0))

    def test_the_new_profiles_servers_appearing_is_the_trigger(self):
        """The schedule itself, at the manager: a switch is visible here only
        as the connected set changing, and that has to raise the flag."""
        self.m._sweep_due = False
        self.m._connected_servers = {"login-a"}
        self.m.get_clients = lambda: {"login-b": self._client()}
        self.m._note_connected_servers()
        self.assertTrue(self.m._sweep_due)


class TheSwitchTriggersTheSweepByReconnectingTest(unittest.TestCase):
    """The chain above is worth nothing if the switch does not reconnect. The
    door is `clientManager.switch_user`, not the browser gateway: the gateway
    is today's only caller, and a rule parked at the only current call site is
    how this repository's recurring defect starts.

    It used to call `syncManager.request_profile_sweep()` outright. What
    replaces that is `connect_all`, which the swap already ran -- so what is
    left to pin is that it runs, and that it runs *after* the new profile has
    been adopted. Reconnecting the old profile's servers would make the
    trigger fire for the wrong person.
    """

    def _manager(self):
        from jellyfin_mpv_shim import clients

        cm = clients.ClientManager.__new__(clients.ClientManager)
        cm._switch_lock = threading.RLock()
        cm._client_lock = threading.RLock()
        cm._switching = threading.Event()
        cm._stop_event = threading.Event()      # is_stopping reads this
        cm.clients = {}
        cm._user_generation = 0
        cm._removed_uuids = set()
        # The per-uuid answers the switch drops. Declared because the real
        # object has them and `_forget_server_state` reaches all three.
        cm._connect_failures = {}
        cm._server_on_lan = {}
        cm._lan_probe = {}
        cm.save_credentials = lambda: None
        cm.stop_all_clients = lambda: None
        self.order = []
        cm._adopt_active_user = lambda: self.order.append("adopt")
        cm.connect_all = lambda: self.order.append("connect")
        return cm, clients

    def test_the_switch_reconnects_after_adopting_the_new_profile(self):
        cm, clients = self._manager()
        with mock.patch.object(clients, "userManager") as um:
            um.get.return_value = {"id": "other"}
            um.active_id = "local"
            self.assertTrue(cm.switch_user("other"))
        self.assertEqual(self.order, ["adopt", "connect"])

    def test_it_no_longer_reaches_into_sync_at_all(self):
        """Deliberately asserted as an absence. The call it replaced had to be
        wrapped in a try/except because a catalog failure must not fail a
        switch that already happened; there is nothing left to fail."""
        import jellyfin_mpv_shim.sync.manager as sync_manager
        cm, clients = self._manager()
        with mock.patch.object(clients, "userManager") as um:
            um.get.return_value = {"id": "other"}
            um.active_id = "local"
            with mock.patch.object(sync_manager, "syncManager") as sm:
                self.assertTrue(cm.switch_user("other"))
        self.assertEqual(sm.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
