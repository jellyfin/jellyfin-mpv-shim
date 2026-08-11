"""Offline watched state, against a real server, in both directions.

`tests/test_playstate_mirror.py` is thorough about the *rules* -- advance
only, the queue is offline-only, a pull that changed nothing does not
redraw -- and every one of those tests answers the server from a mock that
returns what the test told it to. All of them would stay green if this
shim's beliefs about the server were wrong, and this feature is almost
entirely beliefs about the server. `tests/integration/test_e2e_offline.py`
goes further (real files, real catalog, real mpv) and stops at exactly this
line: its own docstring says the Jellyfin client is the one thing it fakes,
"because a real server is not available here".

This is that boundary, tested. What it pins:

* **The push half.** A change made with the server away reaches it once the
  server is back, and does not rewind anything that moved on in the
  meantime.
* **The pull half.** A change made somewhere else reaches the catalog --
  the copy on disk, which is what offline browsing reads, and which
  nothing else in this app will ever go and check.
* **The push the server makes.** `UserDataChanged` carries the new values,
  so the ordinary case costs no request at all. The whole redesign of this
  feature rests on that message's shape and on when it is sent, both of
  which are somebody else's code and neither of which is documented. Two
  answers here are not the intuitive ones and both are pinned below: a
  progress report announces nothing *even when it finishes the item*, and
  the message is not filtered by which session caused it -- this app is
  told about its own marks as well as another device's.
* **That the trimmed request still answers.** The sweep asks with
  ``Fields=""`` because measuring showed the default was 6x the server
  time for identical UserData. An empty Fields is exactly the kind of
  parameter a server is free to reinterpret, and if it ever stops
  returning UserData the sweep silently stops working -- the catalog would
  simply never change, which looks like "nothing has been watched".

Files are not downloaded here. Catalog rows are written directly, because
what is under test is the synchronisation of *state* and a real transfer
would add minutes to say nothing extra. Everything below the row is real:
a real SyncDB on disk, the real SyncManager methods, a real client, a real
websocket.

**On fixture durations.** stdjflib's clips are ~20 seconds, and the
server's resume rule discards a position on anything shorter than
`MinResumeDurationSeconds` (300) -- marking it *played* instead. That rule
lives in `UserDataManager.UpdatePlayState`, which is the **playback
reporting** path only. `POST UserItems/{id}/UserData`, which is what both
this suite and `_sync_playstate` write with, stores what it is given. So
exact positions are assertable here, and a test that reported playback
instead would fail in a way that reads as a client bug. (It is the same
trap that made audiobook resume look broken until the fixtures grew.)

Run: JMS_E2E_SERVER=http://127.0.0.1:8096 python3 -m unittest \\
     tests.e2e.test_offline_sync
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SERVER_UUID = "srv-e2e"

#: Well inside the server's resume band and nowhere near either end, so a
#: value that comes back changed has been changed by something this suite
#: is about rather than by a rule it is not.
POSITION = 5 * 10_000_000          # 5 seconds
LATER = 9 * 10_000_000
EARLIER = 2 * 10_000_000


def _catalog_row(item, path="x.mp4"):
    """A complete download row for a real server item.

    `userdata_json` starts at the download-time snapshot -- unwatched, at
    zero -- which is the state every one of these tests has to move away
    from for its assertion to mean anything.
    """
    return {
        "item_id": item["Id"],
        "server_uuid": SERVER_UUID,
        "server_id": item.get("ServerId") or "s1",
        "name": item.get("Name") or "",
        "type": item.get("Type") or "Movie",
        "status": "complete",
        "file_path": path,
        "size_bytes": 1,
        "downloaded_bytes": 1,
        "runtime_ticks": item.get("RunTimeTicks") or 0,
        "item_json": json.dumps(item),
        "userdata_json": json.dumps({"Played": False,
                                     "PlaybackPositionTicks": 0}),
    }


@_e2e.require_server
class OfflineStateSyncTest(unittest.TestCase):
    """One catalog, one server, both directions."""

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session("qa-user")
        # Two items, because several assertions here are "this one moved and
        # that one did not" -- a single-item test cannot tell a working sync
        # from one that writes every row it sees.
        cls.items = cls.session.find_all(library="Movies",
                                         item_type="Movie")[:2]
        if len(cls.items) < 2:
            raise unittest.SkipTest("need two movies in the QA library")

    @classmethod
    def tearDownClass(cls):
        cls.session.stop()

    def setUp(self):
        from jellyfin_mpv_shim.sync.db import SyncDB
        from jellyfin_mpv_shim.sync.manager import SyncManager

        self.tmp = tempfile.mkdtemp(prefix="jms-e2e-sync-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        for item in self.items:
            self.db.upsert(_catalog_row(item))

        # A real manager, constructed rather than started: `start()` would
        # put a worker thread on the same catalog, and every assertion here
        # is about what one specific pass did. The methods under test are
        # the ones that pass calls.
        self.mgr = SyncManager()
        self.mgr.db = self.db
        self.mgr.root = self.tmp
        self.mgr.get_client = lambda uuid: (
            self.session.client if uuid == SERVER_UUID else None)

        self.ids = [i["Id"] for i in self.items]
        self.addCleanup(self.session.reset_played, *self.ids)
        self.session.reset_played(*self.ids)

    # -- helpers -----------------------------------------------------------

    def stored(self, item_id):
        """The catalog's copy of this item's userdata, off disk."""
        row = self.db.get(item_id) or {}
        return json.loads(row.get("userdata_json") or "{}")

    def elsewhere(self, item_id, **userdata):
        """Make a change the way another client would."""
        self.session.api.update_userdata_for_item(item_id, userdata)

    # -- push: what we did offline, once the server is back ----------------

    def test_a_mark_made_offline_reaches_the_server(self):
        item_id = self.ids[0]
        self.db.upsert_playstate(SERVER_UUID, item_id, played=True)
        self.mgr._sync_playstate()
        self.assertTrue(self.session.user_data(item_id).get("Played"))

    def test_a_position_reached_offline_reaches_the_server(self):
        item_id = self.ids[0]
        self.db.upsert_playstate(SERVER_UUID, item_id, position_ticks=POSITION)
        self.mgr._sync_playstate()
        self.assertEqual(
            self.session.user_data(item_id).get("PlaybackPositionTicks"),
            POSITION)

    def test_it_does_not_rewind_what_moved_on_without_us(self):
        """The whole point of the queue being advance-only: this client was
        off for a week, and the phone is further ahead than we are."""
        item_id = self.ids[0]
        self.elsewhere(item_id, PlaybackPositionTicks=LATER)
        self.db.upsert_playstate(SERVER_UUID, item_id,
                                 position_ticks=EARLIER)
        self.mgr._sync_playstate()
        self.assertEqual(
            self.session.user_data(item_id).get("PlaybackPositionTicks"),
            LATER, "an offline client rewound another device's progress")

    def test_a_replayed_change_is_not_replayed_forever(self):
        """Multi-step, per the repo rule. The failure this catches is the
        queue never draining: every later pass would re-push a stale value
        and undo whatever had happened since."""
        item_id = self.ids[0]
        self.db.upsert_playstate(SERVER_UUID, item_id, position_ticks=POSITION)
        self.mgr._sync_playstate()
        self.assertEqual(self.db.list_playstate(), [],
                         "the queue still holds a change already sent")
        # Somebody else moves on; three more passes must not touch it.
        self.elsewhere(item_id, PlaybackPositionTicks=LATER)
        for _ in range(3):
            self.mgr._sync_playstate()
        self.assertEqual(
            self.session.user_data(item_id).get("PlaybackPositionTicks"),
            LATER)

    def test_only_the_queued_item_is_touched(self):
        self.db.upsert_playstate(SERVER_UUID, self.ids[0], played=True)
        self.mgr._sync_playstate()
        self.assertFalse(self.session.user_data(self.ids[1]).get("Played"))

    # -- pull: what somebody else did, into the catalog ---------------------

    def test_the_sweep_brings_another_device_s_mark_down(self):
        item_id = self.ids[0]
        self.session.api.item_played(item_id, True)
        self.mgr._refresh_userdata()
        self.assertTrue(self.stored(item_id).get("Played"),
                        "the catalog still says unwatched, so offline this "
                        "episode is still offering to be watched")

    def test_the_sweep_brings_a_position_down(self):
        item_id = self.ids[0]
        self.elsewhere(item_id, PlaybackPositionTicks=LATER)
        self.mgr._refresh_userdata()
        self.assertEqual(
            self.stored(item_id).get("PlaybackPositionTicks"), LATER)

    def test_the_trimmed_request_still_carries_userdata(self):
        """`Fields=""` is what makes the sweep cheap, and an empty Fields is
        exactly the sort of parameter a server may reinterpret. If UserData
        ever stops coming back the sweep does not fail, it silently stops
        working -- so this asserts on the wire, not on the catalog."""
        result = self.session.api.get_items(self.ids, fields="") or {}
        items = result.get("Items") or []
        self.assertEqual(len(items), len(self.ids))
        for item in items:
            self.assertIn("UserData", item)
            self.assertIn("PlaybackPositionTicks", item["UserData"])

    def test_the_sweep_leaves_untouched_items_alone(self):
        self.session.api.item_played(self.ids[0], True)
        self.mgr._refresh_userdata()
        self.assertFalse(self.stored(self.ids[1]).get("Played"))

    def test_the_two_directions_converge(self):
        """Several rounds of both halves, per the repo's multi-step rule.

        The shape of bug this catches is the two directions feeding each
        other: a pull that re-queues what it just learned, or a push that
        leaves a row behind for the next pull to undo. One round of each
        cannot see it -- it needs the third pass to diverge.
        """
        item_id = self.ids[0]
        self.db.upsert_playstate(SERVER_UUID, item_id, position_ticks=POSITION)
        for _ in range(3):
            self.mgr._sync_playstate()
            self.mgr._refresh_userdata()
        self.assertEqual(
            self.session.user_data(item_id).get("PlaybackPositionTicks"),
            POSITION)
        self.assertEqual(
            self.stored(item_id).get("PlaybackPositionTicks"), POSITION)
        self.assertEqual(self.db.list_playstate(), [])


@_e2e.require_server
class PushedUserDataReachesTheCatalogTest(unittest.TestCase):
    """The message the redesign rests on, from a real server.

    Everything here runs through the app's own `EventHandler` rather than
    calling the manager directly, because the handler discarding the
    payload is precisely the bug this replaced -- for a long time it took
    `_arguments` and dropped it, and a five-minute sweep re-read the whole
    catalog to learn what this message had already said.
    """

    @classmethod
    def setUpClass(cls):
        # A socket on one session and the changes made from another, which
        # is the real arrangement: the shim listening, a phone acting.
        cls.watcher = _e2e.Session("qa-user", websocket=True)
        cls.actor = _e2e.Session("qa-user", device_id="jms-e2e-udactor")
        cls.items = cls.actor.find_all(library="Movies",
                                       item_type="Movie")[:1]
        if not cls.items:
            cls.watcher.stop()
            cls.actor.stop()
            raise unittest.SkipTest("need a movie in the QA library")
        # The socket is up a moment before the server will send to it.
        time.sleep(1.5)

    @classmethod
    def tearDownClass(cls):
        cls.watcher.stop()
        cls.actor.stop()

    def setUp(self):
        from unittest import mock

        from jellyfin_mpv_shim import event_handler as eh
        from jellyfin_mpv_shim.sync import manager as mgr
        from jellyfin_mpv_shim.sync.db import SyncDB
        from jellyfin_mpv_shim.sync.manager import SyncManager

        self.tmp = tempfile.mkdtemp(prefix="jms-e2e-ws-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        self.item = self.items[0]
        self.item_id = self.item["Id"]
        self.db.upsert(_catalog_row(self.item))

        self.mgr = SyncManager()
        self.mgr.db = self.db
        #: Any call here is a request the push was supposed to make
        #: unnecessary, so it is an assertion rather than a stub.
        self.asked = []
        self.mgr.get_client = lambda uuid: self.asked.append(uuid)

        patcher = mock.patch.object(mgr, "syncManager", self.mgr)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.handler = eh.EventHandler()
        self.handler.user_data_changed = None
        # Feed every UserDataChanged the real socket delivers through the
        # real handler, which is what the app does via ClientManager.
        self.watcher.listeners.append(self._on_event)
        self.addCleanup(self.watcher.listeners.remove, self._on_event)

        self.actor.reset_played(self.item_id)
        self.addCleanup(self.actor.reset_played, self.item_id)

    def _on_event(self, name, data):
        if name == "UserDataChanged":
            self.handler.user_data_change(self.watcher.client, name, data)

    def stored(self):
        row = self.db.get(self.item_id) or {}
        return json.loads(row.get("userdata_json") or "{}")

    def test_a_mark_elsewhere_reaches_the_catalog_over_the_socket(self):
        self.actor.api.item_played(self.item_id, True)
        _e2e.wait_for(lambda: self.stored().get("Played") or None, timeout=15)
        self.assertTrue(self.stored().get("Played"))

    def test_a_position_set_elsewhere_reaches_the_catalog(self):
        self.actor.api.update_userdata_for_item(
            self.item_id, {"PlaybackPositionTicks": LATER})
        _e2e.wait_for(
            lambda: self.stored().get("PlaybackPositionTicks") or None,
            timeout=15)
        self.assertEqual(self.stored().get("PlaybackPositionTicks"), LATER)

    def test_it_costs_no_request_at_all(self):
        """The reason this replaced the poll. The values are in the message,
        so nothing needs to be asked."""
        self.actor.api.item_played(self.item_id, True)
        _e2e.wait_for(lambda: self.stored().get("Played") or None, timeout=15)
        self.assertEqual(self.asked, [],
                         "applying a pushed change went back to the server")

    def test_our_own_actions_come_back_to_us_too(self):
        """The message is broadcast to every session the *user* has, not to
        every session except the one that caused it.

        Worth pinning because the natural assumption is the opposite -- that
        a client is not told about its own doing -- and two things here read
        differently depending on which is true. `apply_userdata_event` will
        be handed this app's own playback marks (harmless: advance-only,
        and `_record_progress` has already written the same values), and
        Home's refresh fires on our own start and stop as well as on
        somebody else's. If the server ever starts excluding the origin,
        neither breaks, but both stop being reachable this way and the
        local paths become the only ones -- which is a thing to know before
        trusting the socket for it.

        Measured symmetric: same result whichever session acts.
        """
        self.watcher.events.clear()
        self.watcher.api.item_played(self.item_id, True)
        data = self.watcher.wait_for_event("UserDataChanged", timeout=15)
        self.assertTrue(
            [e for e in (data.get("UserDataList") or [])
             if e.get("ItemId") == self.item_id],
            "the session that made the change was not told about it")

    def test_the_payload_names_the_item_and_its_parent(self):
        """The shape `apply_userdata_event` is written against, and the
        reason it must tolerate ids it has never heard of: the server adds
        each changed item's parent for its own indicator refresh."""
        self.watcher.events.clear()
        self.actor.api.item_played(self.item_id, True)
        data = self.watcher.wait_for_event("UserDataChanged", timeout=15)
        entries = data.get("UserDataList") or []
        self.assertTrue(entries, "no UserDataList in the message")
        mine = [e for e in entries if e.get("ItemId") == self.item_id]
        self.assertEqual(len(mine), 1)
        self.assertIn("Played", mine[0])
        self.assertIn("PlaybackPositionTicks", mine[0])
        self.assertGreater(
            len(entries), 1,
            "the parent entry is gone: apply_userdata_event tolerates "
            "unknown ids because of it, and this is where that is checked")

    def _progress(self, ticks):
        self.actor.api.session_progress({
            "ItemId": self.item_id, "PlayMethod": "DirectPlay",
            "PositionTicks": int(ticks), "CanSeek": True, "IsPaused": False})

    def _settle(self, seconds=2.5):
        """Long enough that anything the server meant to send has been sent.

        The notifier coalesces on a 500 ms timer, so this is five times the
        window it batches over -- which is what makes asserting a *negative*
        here meaningful rather than a race.
        """
        time.sleep(seconds)
        return [d for n, d in list(self.watcher.events)
                if n == "UserDataChanged"]

    def _start_playback_and_quiesce(self):
        """Open a playback session and get back to a genuinely empty inbox.

        Both halves matter, and both were learned the hard way here -- this
        is the repo's standing rule for live tests, that a wait must never
        be satisfiable by evidence that predates the action.

        The mark is taken **before** `session_playing`, because `setUp`'s
        own `reset_played` emits a UserDataChanged of its own: waiting
        without clearing first returns *that* one instantly, and the start's
        event then lands in the list a moment after it was emptied -- which
        is indistinguishable from a progress report having emitted it, and
        is what the first version of this suite reported.

        And it settles **after** the wait, because the start can be followed
        by more than one message; clearing the instant the first arrives
        leaves the rest to appear inside the window under test.
        """
        self.watcher.events.clear()
        self.actor.api.session_playing({
            "ItemId": self.item_id, "PlayMethod": "DirectPlay",
            "PositionTicks": 0, "CanSeek": True, "IsPaused": False})
        self.watcher.wait_for_event("UserDataChanged", timeout=15)
        self._settle(1.5)
        self.watcher.events.clear()

    def test_progress_reports_send_no_event(self):
        """A belief, held by three separate pieces of this app, that is
        false in the obvious direction and would be expensive to be wrong
        about.

        The server drops `PlaybackProgress` saves before it builds this
        message (`UserDataChangeNotifier`), so a client streaming elsewhere
        does not narrate itself position by position. Everything
        downstream is sized for that: Home's debounce is settling a handful
        of events rather than a stream, and `apply_userdata_event` walks
        the list on the websocket thread rather than handing it off.

        If a future server starts sending one per progress report, this
        goes red and both of those want re-reading -- which is the entire
        reason to assert a negative. (The comment this replaced claimed the
        opposite, and had for years.)

        The mark at the end is not decoration. A negative assertion over a
        socket passes just as well when the socket is dead, the listener
        was never attached, or the item id was wrong -- so the same window
        is made to produce an event on demand before the absence of one is
        believed.
        """
        runtime = self.item.get("RunTimeTicks") or 0
        self.assertTrue(runtime, "fixture has no runtime; the server's "
                                 "resume rules cannot be reasoned about")
        self._start_playback_and_quiesce()
        # Under MinResumePct, so the server stores nothing and this stays a
        # test about announcements rather than about the resume rule.
        for fraction in (0.01, 0.02, 0.03):
            self._progress(runtime * fraction)
            time.sleep(0.6)
        pushed = self._settle()
        self.actor.api.session_stop({"ItemId": self.item_id,
                                     "PositionTicks": 0})
        self.assertEqual(
            pushed, [],
            "the server now pushes UserDataChanged for progress reports; "
            "see this test's docstring for what assumed otherwise")
        # The control: this socket, this listener, this item.
        self.watcher.events.clear()
        self.actor.api.item_played(self.item_id, True)
        self.assertTrue(
            self._settle(),
            "nothing announced a plain mark either, so the silence above "
            "was the socket rather than the server's filter")

    def test_not_even_the_one_that_finishes_it(self):
        """The exception that isn't, and the reason the sweep survives.

        The obvious guess -- and the first version of this suite's guess --
        is that a progress report which carries an item *past the
        completion threshold* must announce itself, since it is no longer
        merely progress. It does not. `SessionManager.OnPlaybackProgress`
        saves the completion under `PlaybackProgress` like any other, and
        the only thing it does differently is
        `Video.PropagatePlayedState`, which returns immediately for a video
        with no alternate versions and skips the item itself in any case.

        So: another device can play something all the way through, the
        server can record it as watched, and **this client is told
        nothing** until that device sends its stop. A client that is killed
        mid-playback never sends one. That gap is exactly what
        `_refresh_userdata` is left in the design to cover, and it is why
        making the sweep cheap mattered more than making it rare.

        Both halves are asserted here because the first version of this
        file pinned the opposite, on evidence that turned out to be the
        previous action's event arriving after the inbox was cleared.
        """
        runtime = self.item.get("RunTimeTicks") or 0
        self.assertTrue(runtime)
        self._start_playback_and_quiesce()
        self._progress(runtime * 0.95)
        pushed = self._settle()
        self.assertEqual(pushed, [],
                         "a completing progress report now announces "
                         "itself; the sweep's justification has changed")
        # The server did take it -- so this is silence about a real change,
        # not silence about nothing happening.
        self.assertTrue(
            self.actor.api.get_item(self.item_id).get("UserData", {})
            .get("Played"),
            "the server did not record the completion, so this test is "
            "asserting silence about an event that never occurred")
        self.assertFalse(self.stored().get("Played"))
        # ...and the stop is what finally tells us, which is the path the
        # catalog actually gets "finished on another device" from.
        self.actor.api.session_stop({"ItemId": self.item_id,
                                     "PositionTicks": 0})
        _e2e.wait_for(lambda: self.stored().get("Played") or None, timeout=15)
        self.assertTrue(self.stored().get("Played"))


if __name__ == "__main__":
    unittest.main()
