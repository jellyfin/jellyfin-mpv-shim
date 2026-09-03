"""Playstate has to reach the local catalog, online as well as off (#9).

The catalog's `userdata_json` was written at download time and thereafter
only by the *offline* playback paths, on the reasoning that "the timeline
reports progress when there is a server". True, and beside the point: the
timeline reports to the **server**, and the catalog is what the app reads
when the server is not there.

So watching a downloaded episode online left the catalog saying unwatched at
position 0 — which is what you were shown next time you opened it on a
train — and silently broke "delete watched downloads", which reads
`userdata_json` with no server fallback (unlike the auto-download reaper,
which pays a round trip per row precisely to work around this).

Multi-step by the repo's standing rule: state feeding back into the input
that produced it is the bug shape here, so the tests drive several reports
and assert the stored value tracks rather than latching.
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
import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()


class FakeDb:
    """Enough of sync.db for the mirror: advance-only userdata per item.

    ``held`` is the set of item ids the catalog has a download row for, and
    modelling it is not decoration. The real ``update_userdata`` answers
    False for an item it holds no row for, and two callers lean on exactly
    that: `mirror_playstate` is called for *every* item played, and
    `apply_userdata_event` is handed the parent of every changed item by
    the server. A fake that quietly creates a row for whatever it is asked
    about cannot fail either of those tests -- it would report a pass while
    the shim wrote catalog rows for things nobody has downloaded.

    None means "holds everything", which is what the mirror tests want:
    their item is downloaded by construction.
    """

    def __init__(self, held=None):
        self.userdata = {}
        self.playstate = []
        self.held = held

    def update_userdata(self, item_id, played=None, position_ticks=None):
        if self.held is not None and item_id not in self.held:
            return False
        data = self.userdata.setdefault(item_id, {})
        changed = False
        if played:
            if not data.get("Played"):
                data["Played"] = True
                changed = True
            if data.get("PlaybackPositionTicks"):
                data["PlaybackPositionTicks"] = 0
                changed = True
        elif position_ticks is not None:
            if position_ticks > (data.get("PlaybackPositionTicks") or 0):
                data["PlaybackPositionTicks"] = position_ticks
                changed = True
        return changed

    def upsert_playstate(self, server_uuid, item_id, **kw):
        self.playstate.append((server_uuid, item_id, kw))


class _RecordingWake:
    """Stands in for the manager's `_wake` event during a sweep.

    Records what it was asked to wait for and returns immediately. `set` is
    modelled too because the manager's own `stop()` calls it, so a test that
    stops mid-sweep goes through the same object.
    """

    def __init__(self):
        self.waits = []
        self.is_set_flag = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set_flag

    def set(self):
        self.is_set_flag = True

    def clear(self):
        self.is_set_flag = False


def _video(online=True):
    from jellyfin_mpv_shim.sync import offline_media

    v = offline_media.OfflineVideo.__new__(offline_media.OfflineVideo)
    v.item_id = "ep1"
    v._server_uuid = "srv"
    v.client = mock.Mock() if online else None
    return v


class MirrorsWhileOnlineTest(unittest.TestCase):

    def setUp(self):
        from jellyfin_mpv_shim.sync import offline_media
        self.db = FakeDb()
        patcher = mock.patch.object(offline_media, "syncManager")
        self.sm = patcher.start()
        self.addCleanup(patcher.stop)
        self.sm.db = self.db

    def test_progress_reaches_the_catalog_with_a_server_present(self):
        v = _video(online=True)
        v.record_offline_progress(5 * 10_000_000)
        self.assertEqual(
            self.db.userdata["ep1"]["PlaybackPositionTicks"], 50_000_000)

    def test_and_it_tracks_across_several_reports(self):
        """The one-step version passes against an implementation that
        writes once and latches."""
        v = _video(online=True)
        for secs in (5, 60, 900):
            v.record_offline_progress(secs * 10_000_000)
        self.assertEqual(
            self.db.userdata["ep1"]["PlaybackPositionTicks"], 9_000_000_000)

    def test_finishing_online_marks_it_watched_locally(self):
        v = _video(online=True)
        v.record_offline_progress(9_000_000_000, finished=True)
        self.assertTrue(self.db.userdata["ep1"]["Played"])
        # ...and leaves no resume point, matching the server: absent and 0
        # are the same thing here, and a finish on a fresh row never writes
        # one in the first place (update_userdata's played branch wins).
        self.assertFalse(
            self.db.userdata["ep1"].get("PlaybackPositionTicks"))

    def test_finishing_clears_a_resume_point_that_was_there(self):
        """The half the row above cannot show: it is the *clearing* that
        matters, or the browser offers "Resume from the very end"."""
        v = _video(online=True)
        v.record_offline_progress(60 * 10_000_000)
        self.assertTrue(self.db.userdata["ep1"]["PlaybackPositionTicks"])
        v.record_offline_progress(9_000_000_000, finished=True)
        self.assertEqual(
            self.db.userdata["ep1"]["PlaybackPositionTicks"], 0)

    def test_marking_watched_online_reaches_the_catalog(self):
        v = _video(online=True)
        v.set_played(True)
        self.assertTrue(self.db.userdata["ep1"]["Played"])
        v.client.jellyfin.item_played.assert_called_once()

    def test_the_replay_queue_stays_offline_only(self):
        """It is a list of changes the server has not been told about.
        Queueing while online would queue a write that already happened."""
        _video(online=True).record_offline_progress(5 * 10_000_000)
        self.assertEqual(self.db.playstate, [])

    def test_offline_still_queues_and_mirrors(self):
        v = _video(online=False)
        v.record_offline_progress(5 * 10_000_000)
        self.assertEqual(
            self.db.userdata["ep1"]["PlaybackPositionTicks"], 50_000_000)
        self.assertEqual(len(self.db.playstate), 1)

    def test_a_catalog_failure_never_breaks_playback_reporting(self):
        v = _video(online=True)
        self.db.update_userdata = mock.Mock(side_effect=RuntimeError("locked"))
        v.record_offline_progress(5 * 10_000_000)   # must not raise

    def test_unwatching_online_does_not_mark_it_watched(self):
        v = _video(online=True)
        v.set_played(False)
        self.assertFalse(self.db.userdata.get("ep1", {}).get("Played"))


class PullFromTheServerTest(unittest.TestCase):
    """The other half: what was watched on *another* device."""

    def _manager(self, items, rows=None, held=None):
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager.__new__(SyncManager)
        m.db = FakeDb(held=held)
        m._stop = False
        m._notify_change = mock.Mock()
        # The real manager always has this (it is built in __init__); the
        # sweep waits on it between requests so that stopping does not have
        # to sit out the delay. Recorded rather than really waited on: a
        # test that slept the spacing would take longer than the rest of
        # this file put together, and what is worth asserting is *that* the
        # requests were spaced, not that the process was idle for it.
        m._wake = _RecordingWake()
        m.db.list = lambda status=None: rows if rows is not None else [
            {"item_id": "ep1", "server_uuid": "srv"},
            {"item_id": "ep2", "server_uuid": "srv"},
        ]
        api = mock.Mock()
        api.get_items.return_value = {"Items": items}
        m.get_client = lambda uuid: (mock.Mock(jellyfin=api)
                                     if uuid == "srv" else None)
        m._api = api
        return m

    def test_it_stores_what_the_server_says(self):
        m = self._manager([{"Id": "ep1", "UserData": {"Played": True}}])
        m._refresh_userdata()
        self.assertTrue(m.db.userdata["ep1"]["Played"])

    def test_it_asks_in_one_batch_rather_than_per_item(self):
        """The auto-download reaper pays a round trip per row to work around
        the stale snapshot; this exists so it does not have to."""
        m = self._manager([])
        m._refresh_userdata()
        self.assertEqual(m._api.get_items.call_count, 1)

    def test_a_long_catalog_is_split(self):
        from jellyfin_mpv_shim.sync import manager as mgr
        rows = [{"item_id": "e%d" % i, "server_uuid": "srv"}
                for i in range(mgr.USERDATA_BATCH * 2 + 1)]
        m = self._manager([], rows=rows)
        m._refresh_userdata()
        # Ids travel in the query string, which proxies cap.
        self.assertEqual(m._api.get_items.call_count, 3)

    def test_an_unreachable_server_is_skipped_not_failed(self):
        m = self._manager([], rows=[{"item_id": "x", "server_uuid": "gone"}])
        m._refresh_userdata()
        self.assertEqual(m._api.get_items.call_count, 0)

    def test_a_pull_that_changed_nothing_does_not_redraw(self):
        m = self._manager([{"Id": "ep1", "UserData": {"Played": False}}])
        m._refresh_userdata()
        m._notify_change.assert_not_called()

    def test_a_pull_that_changed_something_does(self):
        m = self._manager([{"Id": "ep1", "UserData": {"Played": True}}])
        m._refresh_userdata()
        m._notify_change.assert_called_once()

    def test_a_server_error_does_not_take_the_worker_down(self):
        m = self._manager([])
        m._api.get_items.side_effect = RuntimeError("down")
        m._refresh_userdata()   # must not raise

    def test_it_asks_for_no_fields_it_does_not_use(self):
        """The whole request exists to read `UserData`, which comes back
        whatever Fields says.

        Left to the apiclient's default this sends info() -- 29 fields
        including MediaSources -- and measured against 12.0 that is 73 ms
        and 191 KB per 60 ids against 13 ms and 60 KB, for identical
        UserData. Asserted as `fields=""` rather than "not the default"
        because the default is what a dropped keyword silently restores.
        """
        m = self._manager([])
        m._refresh_userdata()
        self.assertEqual(m._api.get_items.call_args.kwargs.get("fields"), "")

    def test_requests_are_spaced_apart(self):
        """A few hundred downloads is several requests, and nothing is
        waiting on them; they go out spread rather than as a burst."""
        from jellyfin_mpv_shim.sync import manager as mgr
        rows = [{"item_id": "e%d" % i, "server_uuid": "srv"}
                for i in range(mgr.USERDATA_BATCH * 3)]
        m = self._manager([], rows=rows)
        m._refresh_userdata()
        self.assertEqual(m._api.get_items.call_count, 3)
        # Between the requests, so one fewer than there are requests --
        # a sweep that paused before the first would delay the only case
        # that has somebody waiting on it (Home asking on the way in).
        self.assertEqual(m._wake.waits,
                         [mgr.USERDATA_BATCH_PAUSE] * 2)

    def test_a_single_batch_waits_for_nothing(self):
        m = self._manager([])
        m._refresh_userdata()
        self.assertEqual(m._wake.waits, [])

    def test_two_servers_are_spaced_from_each_other_too(self):
        """Neither server sees a burst, but this machine's uplink does."""
        m = self._manager([], rows=[{"item_id": "a", "server_uuid": "srv"},
                                    {"item_id": "b", "server_uuid": "srv2"}])
        api = m._api
        m.get_client = lambda uuid: mock.Mock(jellyfin=api)
        m._refresh_userdata()
        self.assertEqual(api.get_items.call_count, 2)
        self.assertEqual(len(m._wake.waits), 1)

    def test_stopping_during_the_pause_ends_the_sweep(self):
        """Shutdown must not wait out the spacing, and must not keep
        asking after the catalog has been told to close."""
        from jellyfin_mpv_shim.sync import manager as mgr
        rows = [{"item_id": "e%d" % i, "server_uuid": "srv"}
                for i in range(mgr.USERDATA_BATCH * 3)]
        m = self._manager([], rows=rows)

        def stop_on_first_wait(timeout=None):
            m._wake.waits.append(timeout)
            m._stop = True
            return True

        m._wake.wait = stop_on_first_wait
        m._refresh_userdata()
        # One request went out, the pause after it observed the stop, and
        # the third batch was never asked for.
        self.assertEqual(m._api.get_items.call_count, 1)


class PushedUserDataTest(unittest.TestCase):
    """`UserDataChanged` carries the new values, so applying them is free.

    This is what replaced the five-minute sweep as the way watched state
    normally arrives. Measured against 10.11.11 and 12.0.0: the payload is
    `{UserId, ServerId, UserDataList: [UserItemDataDto, ...]}` and the
    server adds each changed item's *parent* to that list, so most entries
    are for things this catalog has never heard of.
    """

    def _manager(self, held=("ep1",)):
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager.__new__(SyncManager)
        m.db = FakeDb(held=set(held))
        m._stop = False
        m._wake = _RecordingWake()
        m._notify_change = mock.Mock()
        m.request_userdata_refresh = mock.Mock()
        m.get_client = lambda uuid: None
        return m

    @staticmethod
    def _event(*entries):
        return {"UserId": "u", "ServerId": "s",
                "UserDataList": list(entries)}

    def test_a_push_reaches_the_catalog(self):
        m = self._manager()
        m.apply_userdata_event(self._event(
            {"ItemId": "ep1", "Played": True, "PlaybackPositionTicks": 0}))
        self.assertTrue(m.db.userdata["ep1"]["Played"])

    def test_it_asks_the_server_for_nothing(self):
        """The point of the whole change: the values are in the message."""
        m = self._manager()
        m.get_client = mock.Mock()
        m.apply_userdata_event(self._event({"ItemId": "ep1", "Played": True}))
        m.get_client.assert_not_called()

    def test_an_item_we_hold_no_copy_of_is_dropped(self):
        """Most entries are: the server appends each item's parent, and a
        user's library is mostly things they have not downloaded."""
        m = self._manager(held=("ep1",))
        m.apply_userdata_event(self._event(
            {"ItemId": "some-series", "Played": True}))
        self.assertEqual(m.db.userdata, {})
        m._notify_change.assert_not_called()

    def test_a_position_from_another_client_lands(self):
        m = self._manager()
        m.apply_userdata_event(self._event(
            {"ItemId": "ep1", "Played": False,
             "PlaybackPositionTicks": 4500000000}))
        self.assertEqual(
            m.db.userdata["ep1"]["PlaybackPositionTicks"], 4500000000)

    def test_a_push_that_changed_nothing_does_not_redraw(self):
        m = self._manager()
        m.apply_userdata_event(self._event(
            {"ItemId": "ep1", "Played": False, "PlaybackPositionTicks": 0}))
        m._notify_change.assert_not_called()

    def test_a_push_that_changed_something_does(self):
        m = self._manager()
        m.apply_userdata_event(self._event({"ItemId": "ep1", "Played": True}))
        m._notify_change.assert_called_once()

    def test_several_pushes_track_rather_than_latch(self):
        """The repo's standing rule: drive it more than once.

        A position that only ever moves forward is the property; a single
        push cannot tell that from one that overwrites, and cannot see the
        stale re-send that a reconnect replays.
        """
        m = self._manager()
        for ticks in (1000, 5000, 9000):
            m.apply_userdata_event(self._event(
                {"ItemId": "ep1", "PlaybackPositionTicks": ticks}))
        self.assertEqual(m.db.userdata["ep1"]["PlaybackPositionTicks"], 9000)
        # ...and a late duplicate of an earlier one does not rewind it.
        m.apply_userdata_event(self._event(
            {"ItemId": "ep1", "PlaybackPositionTicks": 5000}))
        self.assertEqual(m.db.userdata["ep1"]["PlaybackPositionTicks"], 9000)

    def test_an_enormous_list_is_swept_instead_of_walked(self):
        """This runs on the websocket thread. A bulk mark is one message
        with hundreds of entries, and walking it here holds up every event
        queued behind it."""
        from jellyfin_mpv_shim.sync import manager as mgr
        m = self._manager()
        entries = [{"ItemId": "e%d" % i, "Played": True}
                   for i in range(mgr.USERDATA_EVENT_MAX + 1)]
        m.apply_userdata_event(self._event(*entries))
        m.request_userdata_refresh.assert_called_once()
        self.assertEqual(m.db.userdata, {})

    def test_a_list_at_the_limit_is_still_applied_inline(self):
        from jellyfin_mpv_shim.sync import manager as mgr
        m = self._manager()
        entries = [{"ItemId": "e%d" % i, "Played": True}
                   for i in range(mgr.USERDATA_EVENT_MAX)]
        m.apply_userdata_event(self._event(*entries))
        m.request_userdata_refresh.assert_not_called()

    def test_junk_is_survivable(self):
        """Straight off the network, onto the socket thread."""
        m = self._manager()
        for payload in (None, {}, {"UserDataList": None},
                        {"UserDataList": []}, {"UserDataList": [{}]},
                        {"UserDataList": [{"ItemId": None}]}):
            m.apply_userdata_event(payload)   # must not raise
        m._notify_change.assert_not_called()

    def test_a_closed_catalog_is_not_an_error(self):
        """Events keep arriving while the app is shutting down."""
        m = self._manager()
        m.db = None
        m.apply_userdata_event(self._event({"ItemId": "ep1", "Played": True}))


class TheEventHandlerFeedsItTest(unittest.TestCase):
    """At the caller, not the method.

    The same mistake this file's other suite documents: `user_data_change`
    took `_arguments` and dropped it for a long time, so a manager method
    that applies the payload is worth nothing until the handler passes it.
    """

    def test_the_websocket_event_hands_the_payload_to_the_catalog(self):
        from jellyfin_mpv_shim import event_handler as eh
        from jellyfin_mpv_shim.sync import manager as mgr

        handler = eh.EventHandler()
        handler.user_data_changed = None
        payload = {"UserDataList": [{"ItemId": "ep1", "Played": True}]}
        with mock.patch.object(mgr.syncManager, "apply_userdata_event") as ap:
            handler.user_data_change(mock.Mock(), "UserDataChanged", payload)
        ap.assert_called_once_with(payload)

    def test_a_catalog_failure_does_not_cost_the_browser_its_nudge(self):
        """A stale row is cosmetic; a dropped event is a screen that never
        updates at all. So the browser hook runs either way."""
        from jellyfin_mpv_shim import event_handler as eh
        from jellyfin_mpv_shim.sync import manager as mgr

        handler = eh.EventHandler()
        handler.user_data_changed = mock.Mock()
        with mock.patch.object(mgr.syncManager, "apply_userdata_event",
                               side_effect=RuntimeError("catalog gone")):
            handler.user_data_change(mock.Mock(), "UserDataChanged",
                                     {"UserDataList": []})
        handler.user_data_changed.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TheReportingPathsActuallyCallItTest(unittest.TestCase):
    """The half the first attempt missed entirely.

    `record_offline_progress` was made to write the catalog whether or not
    there is a server — and then never called when there was one, because
    all three of its call sites were gated on being offline. Hand-testing
    found it: watch a downloaded episode 30% in while online, go offline,
    and it resumes from 3 seconds.

    So these tests are at the **caller**, not the method. Testing the
    method alone is exactly what passed while the feature did nothing.
    """

    def _reporter(self, online=True):
        from jellyfin_mpv_shim.player_reporting import ReportingMixin

        import threading

        pm = ReportingMixin.__new__(ReportingMixin)
        # The mixin borrows these from PlayerManager (see its TYPE_CHECKING
        # block); the decorated methods take the lock on the way in.
        pm._tl_lock = threading.RLock()
        pm._lock = threading.RLock()
        pm.should_send_timeline = True
        pm._last_offline_record = 0.0
        # Terminating the transcode is submitted to a worker; the stop
        # paths call it and this test is not about it.
        pm._reporter = mock.Mock()
        pm.syncplay = mock.Mock()
        pm.syncplay.is_enabled.return_value = False
        video = mock.Mock()
        video.client = mock.Mock() if online else None
        video.is_photo = False
        video.playback_info = {"PlaySessionId": "s"}
        video.item_id = "ep1"
        video.is_transcode = False
        video.record_offline_progress = mock.Mock()
        pm._video = video
        pm.last_seek = 900.0
        pm.start_time = 0.0
        return pm, video

    def test_backing_out_records_locally_while_online(self):
        """The reported case: played, backed out, went offline."""
        pm, video = self._reporter(online=True)
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 9_000_000_000})
        pm.send_timeline_stopped(finished=False, client=None)
        video.record_offline_progress.assert_called_once()
        self.assertEqual(
            video.record_offline_progress.call_args.args[0], 9_000_000_000)

    def test_closing_the_window_records_locally_while_online(self):
        """Two paths can both record here, and that is fine.

        `_report_stopped_offline` records against the video it was *handed*
        — it runs on a daemon thread during teardown, when `self._video`
        may already be None — and then delegates to `send_timeline_stopped`,
        which records against `self._video` when there still is one. Both
        writes go through `db.update_userdata`, which is advance-only and
        idempotent for the same position, so the duplicate costs a query
        and nothing else. What matters is that at least one of them fires
        while online, which is what did not happen before.
        """
        pm, video = self._reporter(online=True)
        video.client.jellyfin.session_stop = mock.Mock()
        # `self._video` is already None, which is the state this path
        # actually runs in — it is handed the video precisely because the
        # player has let go of it. That also makes it the only recorder,
        # so this test sees its gating rather than the delegate's.
        pm._video = None
        pm._report_stopped_offline(video)
        self.assertTrue(
            video.record_offline_progress.called,
            "nothing recorded the position while the window closed")
        self.assertEqual(
            video.record_offline_progress.call_args.args[0], 9_000_000_000)

    def test_a_photo_still_reports_nothing(self):
        """The guard that predates this must survive it: a photo never went
        through PlaybackInfo, and reporting one puts every picture looked
        at into Continue Watching."""
        pm, video = self._reporter(online=True)
        video.is_photo = True
        pm._report_stopped_offline(video)
        video.record_offline_progress.assert_not_called()

    def test_an_online_video_without_the_method_is_left_alone(self):
        """A plain `media.Video` has no record_offline_progress; the
        hasattr check is what tells the two apart."""
        import threading

        from jellyfin_mpv_shim.player_reporting import ReportingMixin

        pm = ReportingMixin.__new__(ReportingMixin)
        pm._tl_lock = threading.RLock()
        pm._lock = threading.RLock()
        pm._reporter = mock.Mock()
        pm.syncplay = mock.Mock()
        pm.syncplay.is_enabled.return_value = False
        video = mock.Mock(spec=["client", "is_photo", "playback_info",
                                "item_id", "is_transcode"])
        video.client = mock.Mock()
        video.is_photo = False
        video.playback_info = {"PlaySessionId": "s"}
        pm._video = video
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 1})
        # Must not raise looking for a method that is not there.
        pm.send_timeline_stopped(finished=False, client=None)


class StreamingSomethingYouAlsoHoldTest(unittest.TestCase):
    """The gate after the gate.

    The suite above records the first bug: three call sites gated on being
    *offline*, so the method that wrote the catalog either way was never
    reached when there was a server. The fix left a second gate in the same
    three places -- `hasattr(video, "record_offline_progress")`, which is
    true only for a video played *from the downloaded file*.

    So watching a downloaded episode by streaming it (which is what
    happens whenever the server is reachable and the user presses play from
    the library rather than from Downloads) wrote nothing to the catalog.
    The copy on disk -- kept precisely because the network is about to go
    away -- was the one thing that never learned it had been watched.

    These are at the caller for the reason the docstring above gives, and
    the video here deliberately has **no** `record_offline_progress`: a
    stand-in carrying it is the version that cannot fail.
    """

    def _reporter(self):
        import threading

        from jellyfin_mpv_shim.player_reporting import ReportingMixin

        pm = ReportingMixin.__new__(ReportingMixin)
        pm._tl_lock = threading.RLock()
        pm._lock = threading.RLock()
        pm._lock = threading.RLock()
        pm.should_send_timeline = True
        pm._last_offline_record = 0.0
        pm._reporter = mock.Mock()
        pm.syncplay = mock.Mock()
        pm.syncplay.is_enabled.return_value = False
        # `terminate_transcode` is on the spec because the teardown path
        # calls it, not because this suite is about it -- a stand-in that
        # omits it makes _report_stopped_offline raise before it reaches
        # the line under test.
        video = mock.Mock(spec=["client", "is_photo", "playback_info",
                                "item_id", "is_transcode",
                                "terminate_transcode"])
        video.client = mock.Mock()
        video.is_photo = False
        video.is_transcode = False
        video.item_id = "ep1"
        video.playback_info = {"PlaySessionId": "s"}
        pm._video = video
        pm.last_seek = 900.0
        pm.start_time = 0.0
        return pm, video

    def setUp(self):
        from jellyfin_mpv_shim.sync import manager as mgr
        self.db = FakeDb(held={"ep1"})
        patcher = mock.patch.object(mgr.syncManager, "db", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_streamed_download_still_reaches_the_catalog(self):
        pm, _video = self._reporter()
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 9_000_000_000})
        pm.send_timeline_stopped(finished=False, client=None)
        self.assertEqual(
            self.db.userdata.get("ep1", {}).get("PlaybackPositionTicks"),
            9_000_000_000,
            "streaming an item you also have downloaded left the catalog "
            "at zero, so offline resume started it over")

    def test_finishing_it_marks_the_downloaded_copy_watched(self):
        pm, _video = self._reporter()
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 9_000_000_000})
        pm.send_timeline_stopped(finished=True, client=None)
        self.assertTrue(self.db.userdata.get("ep1", {}).get("Played"))

    def test_closing_the_window_records_it_too(self):
        pm, video = self._reporter()
        pm._video = None       # the state this path actually runs in
        pm._report_stopped_offline(video)
        self.assertEqual(
            self.db.userdata.get("ep1", {}).get("PlaybackPositionTicks"),
            9_000_000_000)

    def test_an_item_with_no_download_writes_nothing(self):
        """Called for everything played, so this is the common case: the
        catalog answers 'no row' and nothing is created."""
        pm, video = self._reporter()
        video.item_id = "not-downloaded"
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 9_000_000_000})
        pm.send_timeline_stopped(finished=False, client=None)
        self.assertEqual(self.db.userdata, {})

    def test_it_tracks_across_several_reports(self):
        """Multi-step, per the repo rule: the stored position must follow
        playback rather than latch on the first report."""
        pm, _video = self._reporter()
        for ticks in (1_000_000_000, 4_000_000_000, 9_000_000_000):
            pm._record_progress(pm._video, ticks)
        self.assertEqual(
            self.db.userdata["ep1"]["PlaybackPositionTicks"], 9_000_000_000)

    def test_a_catalog_failure_never_breaks_the_stop_report(self):
        from jellyfin_mpv_shim.sync import manager as mgr

        pm, video = self._reporter()
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 1})
        with mock.patch.object(mgr.syncManager, "mirror_playstate",
                               side_effect=RuntimeError("disk full")):
            pm.send_timeline_stopped(finished=False, client=None)

    def test_the_downloaded_path_still_owns_the_replay_queue(self):
        """The offline-only half must survive the gate coming off: an
        OfflineVideo still goes through `record_offline_progress`, which is
        what queues the change for replay when there is no server."""
        pm, _ = self._reporter()
        offline = mock.Mock(spec=["client", "is_photo", "playback_info",
                                  "item_id", "is_transcode",
                                  "record_offline_progress"])
        offline.client = None
        offline.is_photo = False
        offline.item_id = "ep1"
        offline.playback_info = {"PlaySessionId": "s"}
        pm._video = offline
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 5})
        pm.send_timeline_stopped(finished=False, client=None)
        offline.record_offline_progress.assert_called_once_with(5, False)


class DeliberateMarksReachTheCatalogTest(unittest.TestCase):
    """Mark Watched / Mark Unwatched, onto the copy on disk, at once.

    Everything else that writes this column is advance-only, and rightly:
    playback reports arrive out of order, a queue replayed after a week
    must not rewind another device, and a pull is a floor rather than a
    mirror. A person choosing "Mark unplayed" is none of those -- it is the
    one signal in the app that is authoritative in both directions -- and
    under the old rule it was the only deliberate action here that silently
    did nothing to the downloaded copy. Offline you were shown the tick you
    had just removed, and "delete watched downloads" (which reads
    `userdata_json` with no server fallback) would still have thrown it out.

    A real SyncDB rather than `FakeDb`, because the rule under test *is*
    what that column ends up holding.
    """

    def setUp(self):
        import tempfile
        from jellyfin_mpv_shim.sync.db import COLUMNS, SyncDB, STATUS_COMPLETE

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = SyncDB(os.path.join(self.tmp.name, "cat.db"))
        self.addCleanup(self.db.close)
        self.COLUMNS = COLUMNS
        self.STATUS_COMPLETE = STATUS_COMPLETE

    def row(self, item_id, userdata=None, **overrides):
        row = {c: None for c in self.COLUMNS}
        row["item_id"] = item_id
        row["server_uuid"] = "srv"
        row["status"] = self.STATUS_COMPLETE
        row["runtime_ticks"] = 100 * 10_000_000
        row["userdata_json"] = json.dumps(
            userdata if userdata is not None
            else {"Played": False, "PlaybackPositionTicks": 0})
        row.update(overrides)
        self.db.upsert(row)
        return row

    def stored(self, item_id):
        row = self.db.get(item_id) or {}
        return json.loads(row.get("userdata_json") or "{}")

    def manager(self):
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager.__new__(SyncManager)
        m.db = self.db
        m.on_change = mock.Mock()
        return m

    # -- the writer --------------------------------------------------------

    def test_marking_watched_stores_it_and_clears_the_resume_point(self):
        self.row("ep1", {"Played": False,
                         "PlaybackPositionTicks": 42 * 10_000_000})
        self.assertTrue(self.db.set_watched("ep1", True))
        self.assertEqual(self.stored("ep1")["Played"], True)
        self.assertEqual(self.stored("ep1")["PlaybackPositionTicks"], 0)

    def test_marking_unwatched_clears_it(self):
        """The direction no writer of this column had. Under the old rule
        `played=False` meant "leave it alone", so this was unreachable."""
        self.row("ep1", {"Played": True, "PlayCount": 3,
                         "PlaybackPositionTicks": 42 * 10_000_000})
        self.assertTrue(self.db.set_watched("ep1", False))
        stored = self.stored("ep1")
        self.assertEqual(stored["Played"], False)
        self.assertEqual(stored["PlaybackPositionTicks"], 0)
        self.assertEqual(stored["PlayCount"], 0)

    def test_it_stores_what_the_server_stores(self):
        """Measured against `BaseItem.MarkPlayed` / `ResetPlayedState`, with
        the controller passing `resetPosition: true`. If the two disagree
        the next sweep reads back as a change and the catalog flickers."""
        self.row("ep1", {"Played": False, "PlayCount": 0,
                         "LastPlayedDate": "2020-01-01T00:00:00Z"})
        self.db.set_watched("ep1", True)
        self.assertGreaterEqual(self.stored("ep1")["PlayCount"], 1)
        self.db.set_watched("ep1", False)
        self.assertIsNone(self.stored("ep1")["LastPlayedDate"])

    def test_a_stale_percentage_cannot_shadow_the_new_state(self):
        self.row("ep1", {"Played": True, "PlayedPercentage": 100})
        self.db.set_watched("ep1", False)
        self.assertNotIn("PlayedPercentage", self.stored("ep1"))

    def test_it_tracks_rather_than_latching(self):
        """Multi-step, per the repo rule: the failure shape here is a value
        that can only move one way, which one flip cannot see."""
        self.row("ep1")
        seen = []
        for played in (True, False, True, False):
            self.db.set_watched("ep1", played)
            seen.append(self.stored("ep1")["Played"])
        self.assertEqual(seen, [True, False, True, False])

    def test_nothing_moving_is_reported_as_nothing_moving(self):
        self.row("ep1", {"Played": True, "PlayCount": 1,
                         "PlaybackPositionTicks": 0})
        self.assertFalse(self.db.set_watched("ep1", True),
                         "an unchanged row asked the browser to redraw")

    def test_an_item_we_hold_no_copy_of_is_left_alone(self):
        self.assertFalse(self.db.set_watched("nothing-here", True))

    def test_playback_is_still_advance_only(self):
        """The guard on the change: `update_userdata` is the playback rule
        and must not have acquired this one."""
        self.row("ep1", {"Played": True})
        self.db.update_userdata("ep1", played=False)
        self.assertTrue(self.stored("ep1")["Played"])

    # -- the fan-out -------------------------------------------------------

    def test_a_series_mark_reaches_every_downloaded_episode(self):
        self.row("ep1", series_id="show")
        self.row("ep2", series_id="show")
        self.row("other")
        self.assertEqual(self.manager().mirror_watched("show", True), 2)
        self.assertTrue(self.stored("ep1")["Played"])
        self.assertTrue(self.stored("ep2")["Played"])
        self.assertFalse(self.stored("other")["Played"],
                         "the mark reached an item it does not cover")

    def test_a_season_mark_does_too(self):
        self.row("ep1", series_id="show", season_id="s1")
        self.row("ep2", series_id="show", season_id="s2")
        self.manager().mirror_watched("s1", True)
        self.assertTrue(self.stored("ep1")["Played"])
        self.assertFalse(self.stored("ep2")["Played"])

    def test_an_item_with_nothing_downloaded_is_a_no_op(self):
        """Called for every mark, downloaded or not -- which is what keeps
        it from being forgotten at a call site."""
        m = self.manager()
        self.assertEqual(m.mirror_watched("never-heard-of-it", True), 0)
        m.on_change.assert_not_called()

    def test_it_redraws_only_when_something_moved(self):
        self.row("ep1")
        m = self.manager()
        m.mirror_watched("ep1", True)
        m.on_change.assert_called_once()
        m.mirror_watched("ep1", True)
        m.on_change.assert_called_once()

    def test_a_catalog_failure_is_survivable(self):
        self.row("ep1")
        m = self.manager()
        m.db = mock.Mock()
        m.db.watched_targets.side_effect = RuntimeError("boom")
        self.assertEqual(m.mirror_watched("ep1", True), 0)


class TheUiMarksGoThroughItTest(unittest.TestCase):
    """The call sites. A rule with no caller is the shape this repo keeps
    finding (`mirror_playstate` existed before anything played through it),
    so the gateway the context menu uses and the player's own explicit
    marks are asserted here rather than assumed."""

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway import userdata as gw

        self.gw = gw

        class _Gateway(gw.UserDataMixin):
            pass

        self.controller = _Gateway()
        self.client = mock.Mock()
        patcher = mock.patch.object(gw, "deps")
        deps = patcher.start()
        self.addCleanup(patcher.stop)
        deps.clientManager.clients = {"srv": self.client}
        self.clients = deps.clientManager.clients

        from jellyfin_mpv_shim.sync import manager as mgr
        self.sm = mock.Mock()
        patcher = mock.patch.object(mgr, "syncManager", self.sm)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_marking_watched_online_writes_the_catalog_too(self):
        self.assertTrue(self.controller.set_watched("srv", "ep1", True))
        self.client.jellyfin.item_played.assert_called_once_with("ep1", True)
        self.sm.mirror_watched.assert_called_once_with("ep1", True)

    def test_marking_unwatched_online_does(self):
        """The case that could not previously reach the catalog at all: the
        socket announces it, and every path the announcement takes is
        advance-only."""
        self.assertTrue(self.controller.set_watched("srv", "ep1", False))
        self.sm.mirror_watched.assert_called_once_with("ep1", False)

    def test_a_server_that_refused_does_not_move_the_catalog(self):
        self.client.jellyfin.item_played.side_effect = RuntimeError("no")
        self.assertFalse(self.controller.set_watched("srv", "ep1", True))
        self.sm.mirror_watched.assert_not_called()

    def test_a_catalog_failure_does_not_lose_the_mark(self):
        self.sm.mirror_watched.side_effect = RuntimeError("boom")
        self.assertTrue(self.controller.set_watched("srv", "ep1", True),
                        "the server took the mark; the UI was told it did "
                        "not because the local copy failed")

    def test_offline_un_watching_is_still_refused(self):
        """Deliberately unchanged. The replay queue cannot carry an
        un-watch, so applying one locally would diverge from the server
        with nothing left to reconcile it -- and the UI rolls its optimistic
        tick back on the False."""
        self.clients.clear()
        self.assertFalse(self.controller.set_watched("srv", "ep1", False))
        self.sm.mirror_watched.assert_not_called()

    def test_the_player_s_own_mark_reaches_the_catalog(self):
        """"Quit and Mark Unwatched" on something you *streamed* while
        holding a downloaded copy -- the same gap `mirror_playstate` was
        added for, in the one direction it cannot express."""
        from jellyfin_mpv_shim import media

        video = media.Video.__new__(media.Video)
        video.item_id = "ep1"
        video.client = mock.Mock()
        video.set_played(False)
        video.client.jellyfin.item_played.assert_called_once_with("ep1", False)
        self.sm.mirror_watched.assert_called_once_with("ep1", False)

    def test_a_catalog_failure_does_not_break_the_player_s_mark(self):
        from jellyfin_mpv_shim import media

        self.sm.mirror_watched.side_effect = RuntimeError("boom")
        video = media.Video.__new__(media.Video)
        video.item_id = "ep1"
        video.client = mock.Mock()
        video.set_played(True)          # must not raise


class HomeAsksForAFreshPullTest(unittest.TestCase):
    """The home screen brings the pull forward.

    A five-minute background tick is right for a poll and wrong for the
    moment somebody opens the screen that draws watched state -- and, as
    [iw] found, for the moment they open it and then go offline: the stale
    answer is then what they have for as long as they are offline.
    """

    def _manager(self, last=0.0, due=False, connected=(), clients=("srv",),
                 started=0.0):
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager.__new__(SyncManager)
        m._last_userdata = last
        m._sweep_due = due
        m._connected_servers = set(connected)
        m._started_at = started
        # A client by default: a sweep with nobody to ask is now a distinct
        # state (it keeps the trigger and asks nothing), so a helper that
        # handed out an empty registry would make every test below a test
        # of *that* instead of of the floor it names.
        m.get_clients = lambda: {uuid: object() for uuid in clients}
        m._wake = mock.Mock()
        m._refresh_userdata = mock.Mock()
        return m

    # -- what asks for one -------------------------------------------------

    def test_a_request_marks_a_sweep_due_and_wakes_the_worker(self):
        m = self._manager()
        m.request_userdata_refresh()
        self.assertTrue(m._sweep_due)
        m._wake.set.assert_called_once()

    def test_it_does_not_block_whoever_asked(self):
        """Marks it due and returns -- the requests happen on the sync
        thread, not on the one that was loading a page."""
        m = self._manager()
        m.request_userdata_refresh()
        m._refresh_userdata.assert_not_called()

    def test_a_server_becoming_reachable_marks_one_due(self):
        """The trigger that replaced the interval."""
        m = self._manager(connected=())
        m.get_clients = lambda: {"srv": object()}
        m._note_connected_servers()
        self.assertTrue(m._sweep_due)

    def test_the_same_servers_still_being_there_does_not(self):
        """Otherwise this is an interval again, at the loop's own period."""
        m = self._manager(connected=("srv",))
        m.get_clients = lambda: {"srv": object()}
        for _ in range(5):
            m._note_connected_servers()
        self.assertFalse(m._sweep_due)

    def test_a_server_going_away_does_not(self):
        """There is nothing to catch up on with a server that just left."""
        m = self._manager(connected=("srv", "srv2"))
        m.get_clients = lambda: {"srv": object()}
        m._note_connected_servers()
        self.assertFalse(m._sweep_due)

    def test_a_server_coming_back_does(self):
        """Multi-step, and the case the whole trigger exists for: down for a
        while, so the socket delivered nothing, then back."""
        m = self._manager(connected=("srv",))
        clients = {"srv": object()}
        m.get_clients = lambda: dict(clients)
        m._note_connected_servers()
        self.assertFalse(m._sweep_due)
        clients.clear()                 # the server drops
        m._note_connected_servers()
        self.assertFalse(m._sweep_due)
        clients["srv"] = object()       # ...and comes back
        m._note_connected_servers()
        self.assertTrue(m._sweep_due)

    def test_a_manager_that_never_started_is_safe(self):
        """get_clients has a default because every test, and the CLI before
        start(), drives this without one."""
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager()
        m._note_connected_servers()     # must not raise
        self.assertEqual(m._connected_servers, set())

    def test_an_unreadable_client_list_is_not_fatal(self):
        m = self._manager()
        m.get_clients = mock.Mock(side_effect=RuntimeError("mid-switch"))
        m._note_connected_servers()     # must not raise

    def test_startup_sweeps_without_being_asked(self):
        """A fresh manager has been listening to nothing at all."""
        from jellyfin_mpv_shim.sync.manager import SyncManager

        self.assertTrue(SyncManager()._sweep_due)

    # -- and what actually runs one ----------------------------------------

    def test_nothing_due_means_no_requests_however_long_it_runs(self):
        """The property the interval used to break. An idle app with a live
        socket asks the server nothing, ever."""
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager(due=False, last=0.0)
        for tick in range(10):
            m._sweep_if_due(tick * mgr.USERDATA_SWEEP_FLOOR * 10)
        m._refresh_userdata.assert_not_called()

    def test_a_due_sweep_runs(self):
        m = self._manager(due=True, last=0.0)
        self.assertTrue(m._sweep_if_due(10_000.0))
        m._refresh_userdata.assert_called_once()

    def test_bouncing_in_and_out_of_home_does_not_hammer_it(self):
        """Without the floor this is a round trip per visit."""
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager(due=False, last=0.0)
        now = 10_000.0
        m.request_userdata_refresh()
        m._sweep_if_due(now)
        for i in range(5):
            m.request_userdata_refresh()
            m._sweep_if_due(now + i)
        self.assertEqual(m._refresh_userdata.call_count, 1)

    def test_a_deferred_request_is_not_lost(self):
        """The half a floor gets wrong. A suppressed trigger is a stretch of
        time nobody will look at again -- so it has to be delayed, not
        dropped, or a server that flaps inside the floor leaves the catalog
        stale until something else happens to ask."""
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager(due=True, last=0.0)
        self.assertTrue(m._sweep_if_due(10_000.0),
                        "the setup sweep did not run, so there is no floor "
                        "for the rest of this test to be inside")
        m.request_userdata_refresh()
        self.assertFalse(m._sweep_if_due(10_001.0),
                         "swept inside the floor")
        self.assertTrue(m._sweep_due, "the request was dropped, not deferred")
        self.assertTrue(
            m._sweep_if_due(10_000.0 + mgr.USERDATA_SWEEP_FLOOR + 1),
            "the deferred request never ran")

    # -- and when the first one is allowed to ---------------------------

    def test_nothing_sweeps_in_the_first_minute(self):
        """Startup is when every other part of the app wants the network,
        and the sweep is the only one of them nobody is waiting for."""
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager(due=True, started=10_000.0)
        self.assertFalse(m._sweep_if_due(10_000.0 + 1))
        self.assertFalse(m._sweep_if_due(
            10_000.0 + mgr.USERDATA_SWEEP_SETTLE - 1))
        m._refresh_userdata.assert_not_called()

    def test_the_settle_delays_the_sweep_and_does_not_drop_it(self):
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager(due=True, started=10_000.0)
        m._sweep_if_due(10_000.0 + 1)
        self.assertTrue(m._sweep_due, "the settle dropped the trigger")
        self.assertTrue(m._sweep_if_due(
            10_000.0 + mgr.USERDATA_SWEEP_SETTLE + 1))
        m._refresh_userdata.assert_called_once()

    def test_a_pass_with_nobody_to_ask_is_not_a_sweep(self):
        """The bug the settle was added around, and the more expensive half
        of it. The worker's first pass runs before `login_servers()` has
        registered anything, so it reaches no server at all -- and counting
        it as a sweep spent the startup trigger *and* started the floor."""
        m = self._manager(due=True, clients=())
        self.assertFalse(m._sweep_if_due(10_000.0))
        m._refresh_userdata.assert_not_called()
        self.assertTrue(m._sweep_due, "the trigger was spent on nobody")
        self.assertEqual(m._last_userdata, 0.0,
                         "the floor started counting from a sweep that "
                         "never asked anything")

    def test_an_unreadable_registry_holds_the_sweep_rather_than_spending_it(
            self):
        m = self._manager(due=True)
        m.get_clients = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertFalse(m._sweep_if_due(10_000.0))
        self.assertTrue(m._sweep_due)

    def test_the_launch_sequence_sweeps_a_minute_in(self):
        """The property, over the sequence a real launch produces.

        Multi-step because no single call can see it: the first pass (no
        clients yet), the server arriving a second later, and then every
        five seconds of the worker's idle loop. What this catches is the
        shipped behaviour -- the trigger burned at t=0 and the reconnect
        deferred behind a five-minute floor, so the first sweep of a
        session landed at t+300 rather than during the first screen.
        """
        from jellyfin_mpv_shim.sync import manager as mgr

        start = 40_000.0                # monotonic counts from boot
        m = self._manager(due=True, clients=(), started=start)
        clients = {}
        m.get_clients = lambda: dict(clients)

        m._note_connected_servers()
        m._sweep_if_due(start)          # worker's first pass, pre-login
        clients["srv"] = object()       # login_servers finishes

        swept_at = None
        for tick in range(0, 400, 5):   # the idle loop, for six minutes
            m._note_connected_servers()
            if m._sweep_if_due(start + tick) and swept_at is None:
                swept_at = tick
        self.assertIsNotNone(swept_at, "the session never swept at all")
        self.assertGreaterEqual(swept_at, mgr.USERDATA_SWEEP_SETTLE)
        self.assertLess(swept_at, mgr.USERDATA_SWEEP_SETTLE + 10,
                        "the first sweep of the session waited out the "
                        "floor instead of the settle")
        self.assertEqual(m._refresh_userdata.call_count, 1,
                         "the rest of the six minutes swept again")

    def test_a_slow_login_still_sweeps_when_it_lands(self):
        """The settle alone is not enough, which is what the client gate is
        for. A server behind `connect_retry_mins`, a laptop waking on a
        captive portal: login finishes *after* the settle, so the pass that
        the settle released had nobody to ask. Spending the trigger there
        puts the first sweep of the session a floor away from the login
        rather than a moment after it."""
        from jellyfin_mpv_shim.sync import manager as mgr

        start = 40_000.0
        m = self._manager(due=True, clients=(), started=start)
        clients = {}
        m.get_clients = lambda: dict(clients)

        swept_at = None
        for tick in range(0, 500, 5):
            if tick == 120:             # login finally completes
                clients["srv"] = object()
            m._note_connected_servers()
            if m._sweep_if_due(start + tick) and swept_at is None:
                swept_at = tick
        self.assertIsNotNone(swept_at, "the session never swept at all")
        self.assertLess(swept_at, 120 + mgr.USERDATA_SWEEP_FLOOR,
                        "the sweep waited out a floor started by a pass "
                        "that had no server to ask")
        self.assertGreaterEqual(swept_at, 120)

    def test_a_manager_that_was_never_started_serves_no_settle(self):
        """Every test in this file, and the integration harness, drives a
        manager built by hand. `_started_at` is zero there, which has to
        read as "not a client starting up" rather than as "started at the
        epoch, so wait a minute"."""
        m = self._manager(due=True)
        self.assertTrue(m._sweep_if_due(10_000.0))

    def test_a_flapping_server_costs_one_sweep_per_floor(self):
        """Multi-step: the failure is a sweep per reconnect, which is the
        hammering this whole change is about."""
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager(connected=("srv",), last=0.0)
        clients = {"srv": object()}
        m.get_clients = lambda: dict(clients)
        now = 10_000.0
        for i in range(20):
            clients.clear()
            m._note_connected_servers()
            clients["srv"] = object()
            m._note_connected_servers()
            m._sweep_if_due(now + i)        # 20 flaps inside one floor
        self.assertEqual(m._refresh_userdata.call_count, 1)
        # ...and the flapping is still noticed once the floor lifts.
        m._sweep_if_due(now + mgr.USERDATA_SWEEP_FLOOR + 1)
        self.assertEqual(m._refresh_userdata.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class ReplayAcknowledgesOnlyWhatItSentTest(unittest.TestCase):
    """Offline playstate replay must not delete progress it never uploaded.

    `_sync_playstate` snapshots the pending rows, does its network I/O, then
    deletes the ids it finished. `upsert_playstate` keeps **one row per item**
    and updates it in place -- same id, advanced values -- so a report landing
    during the upload changed the row the replay was about to delete by id.
    The newer position and a final `played` went with it, and the server never
    heard either.

    This is reachable without any thread scheduling luck: playback starts
    offline, the server comes back mid-session, and `OfflineVideo.client`
    stays the captured None while the sync worker has a live client.

    `app.py`'s `clear_status_if` is the in-tree model -- it acks **by value**.
    """

    SERVER = "srv-uuid"

    def _manager(self, db, client):
        from jellyfin_mpv_shim.sync import manager as sync_manager
        mgr = sync_manager.SyncManager.__new__(sync_manager.SyncManager)
        mgr.db = db
        mgr.get_client = lambda uuid: client
        return mgr

    def _db(self):
        import tempfile, shutil
        from jellyfin_mpv_shim.sync.db import SyncDB
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = SyncDB(os.path.join(tmp, "catalog.db"))
        self.addCleanup(db.close)
        return db

    def test_a_report_during_the_upload_is_not_acknowledged_away(self):
        db = self._db()
        db.upsert_playstate(self.SERVER, "ep1", position_ticks=10)
        pushed = []
        test = self

        class Api:
            def get_userdata_for_item(self, item_id):
                # The window: playback writes newer progress while the replay
                # is still talking to the server about the old value.
                db.upsert_playstate(test.SERVER, "ep1",
                                    position_ticks=100, played=True)
                return {}

            def update_userdata_for_item(self, item_id, data):
                pushed.append((item_id, dict(data)))

        class Client:
            jellyfin = Api()

        self._manager(db, Client())._sync_playstate()

        self.assertEqual(pushed, [("ep1", {"PlaybackPositionTicks": 10})],
                         "the replay should have sent the value it snapshotted")
        pending = db.list_playstate()
        self.assertEqual(len(pending), 1,
                         "the replay deleted a row that had moved on, so the "
                         "newer position and the watched mark are gone and "
                         "the server will never hear them")
        self.assertEqual(pending[0]["position_ticks"], 100)
        self.assertTrue(pending[0]["played"])

    def test_an_untouched_row_is_still_cleared(self):
        """The other half. Acking by value must not turn the queue into one
        that never drains -- that would re-push the same mark on every
        reconnect, forever."""
        db = self._db()
        db.upsert_playstate(self.SERVER, "ep1", played=True)

        class Api:
            def get_userdata_for_item(self, item_id):
                return {}

            def update_userdata_for_item(self, item_id, data):
                pass

        class Client:
            jellyfin = Api()

        self._manager(db, Client())._sync_playstate()
        self.assertEqual(db.list_playstate(), [],
                         "an unchanged row was not cleared, so it will be "
                         "re-pushed on every reconnect")

    def test_it_drains_over_repeated_sweeps(self):
        """Multi-step, per the standing rule: the row is rewritten during the
        first sweep and must still leave the queue on a later one rather than
        being replayed forever."""
        db = self._db()
        db.upsert_playstate(self.SERVER, "ep1", position_ticks=10)
        pushed = []
        test = self
        state = {"disturb": True}

        class Api:
            def get_userdata_for_item(self, item_id):
                if state["disturb"]:
                    state["disturb"] = False
                    db.upsert_playstate(test.SERVER, "ep1",
                                        position_ticks=100, played=True)
                return {}

            def update_userdata_for_item(self, item_id, data):
                pushed.append((item_id, dict(data)))

        class Client:
            jellyfin = Api()

        mgr = self._manager(db, Client())
        for _ in range(3):
            mgr._sync_playstate()

        self.assertEqual(db.list_playstate(), [],
                         "the queue never drained: acking by value must not "
                         "leave a row that is replayed on every sweep")
        self.assertIn(("ep1", {"Played": True,
                               "PlaybackPositionTicks": 100}), pushed)


class ExplicitMarksLandAfterTheStopReportTest(unittest.TestCase):
    """"Quit and Mark Unwatched" must not be undone by the stop it follows.

    `player.py`'s own comment states the contract: "Advance (which sends the
    final stop report at the current position) BEFORE marking played: the
    other order let the stop report land after set_played and overwrite the
    fully-watched state with mid-episode progress."

    That held while both were synchronous. `session_stop` now goes through the
    shared `SessionReporter` FIFO while `set_played` still calls
    `item_played` inline, so the *Python* order is preserved and the delivery
    order is not: with anything already queued -- a slow progress report, an
    unreachable server -- the mark is sent first and the stop overwrites it.

    A REAL reporter, deliberately. `tests/test_playstate_mirror.py`'s other
    helper sets `pm._reporter = mock.Mock()`, so the queued lambda never runs
    and the assertion there is over Python call order -- which is exactly the
    thing that is no longer the guarantee.
    """

    def _pm(self, sent):
        import threading

        from jellyfin_mpv_shim.player_reporting import ReportingMixin
        from jellyfin_mpv_shim.session_reporter import SessionReporter

        pm = ReportingMixin.__new__(ReportingMixin)
        pm._tl_lock = threading.RLock()
        pm._lock = threading.RLock()
        pm.should_send_timeline = True
        pm._last_offline_record = 0.0
        pm._reporter = SessionReporter("test-report")
        self.addCleanup(pm._reporter.stop)
        pm.syncplay = mock.Mock()
        pm.syncplay.is_enabled.return_value = False

        jf = mock.Mock()
        jf.session_stop = lambda opts: sent.append("stop")
        jf.item_played = lambda item_id, watched: sent.append(
            "mark(%s)" % watched)

        video = mock.Mock()
        video.client = mock.Mock()
        video.client.jellyfin = jf
        video.is_photo = False
        video.playback_info = {"PlaySessionId": "s"}
        video.item_id = "ep1"
        video.is_transcode = False
        video.record_offline_progress = mock.Mock()
        # The real one, so the mark goes wherever production sends it.
        from jellyfin_mpv_shim.media import Video
        video.set_played = lambda watched=True: Video.set_played(video, watched)
        pm._video = video
        pm.last_seek = 900.0
        pm.start_time = 0.0
        pm.get_timeline_options = mock.Mock(
            return_value={"PositionTicks": 950_000_000})
        return pm, video

    def _block_the_worker(self, pm):
        """Put a slow report in front, which is the whole point: with an empty
        queue the race cannot be observed and the old code passes."""
        import threading

        gate = threading.Event()
        pm._reporter.submit(lambda: gate.wait(5), "slow-earlier-report")
        return gate

    def test_an_unwatched_mark_is_delivered_after_the_stop(self):
        sent = []
        pm, video = self._pm(sent)
        gate = self._block_the_worker(pm)

        pm.send_timeline_stopped(finished=False)   # queues the stop
        pm.queue_played_mark(video, False)         # the explicit mark
        gate.set()
        self.assertTrue(pm._reporter.drain(5))

        self.assertEqual(
            sent, ["stop", "mark(False)"],
            "the mark reached the server before the stop it was meant to "
            "follow, so the stop's progress overwrites it")

    def test_a_watched_mark_is_too(self):
        sent = []
        pm, video = self._pm(sent)
        gate = self._block_the_worker(pm)

        pm.send_timeline_stopped(finished=True)
        pm.queue_played_mark(video, True)
        gate.set()
        self.assertTrue(pm._reporter.drain(5))

        self.assertEqual(sent, ["stop", "mark(True)"])

    def test_the_mark_still_arrives_with_nothing_queued(self):
        """The control: routing through the FIFO must not lose the mark when
        the queue is empty, which is the ordinary case."""
        sent = []
        pm, video = self._pm(sent)
        pm.send_timeline_stopped(finished=False)
        pm.queue_played_mark(video, False)
        self.assertTrue(pm._reporter.drain(5))
        self.assertIn("mark(False)", sent)

    def test_the_player_never_marks_inline(self):
        """The ordering above is only worth anything if the production paths
        actually go through the queue. Three of them mark deliberately -- the
        force_set_played finish, "Mark Watched and Skip", "Quit and Mark
        Unwatched" -- and each is preceded by a stop report on the FIFO.

        Source, because the property is "does not call it directly": an
        inline `video.set_played(...)` restores the bug while every ordering
        test above still passes, since those drive the helper.
        """
        import inspect

        from jellyfin_mpv_shim import player as player_mod

        for name in ("finished_callback", "watched_skip", "unwatched_quit"):
            src = inspect.getsource(getattr(player_mod.PlayerManager, name))
            self.assertNotIn(
                "video.set_played(", src,
                "%s marks the item inline, so it can overtake the stop "
                "report queued beside it; use queue_played_mark" % name)
            self.assertIn(
                "queue_played_mark", src,
                "%s no longer marks at all -- if that is deliberate, this "
                "test should go with it" % name)
