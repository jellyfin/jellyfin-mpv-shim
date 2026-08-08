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

import json
import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()


class FakeDb:
    """Enough of sync.db for the mirror: advance-only userdata per item."""

    def __init__(self):
        self.userdata = {}
        self.playstate = []

    def update_userdata(self, item_id, played=None, position_ticks=None):
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

    def _manager(self, items, rows=None):
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager.__new__(SyncManager)
        m.db = FakeDb()
        m._stop = False
        m._notify_change = mock.Mock()
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


class HomeAsksForAFreshPullTest(unittest.TestCase):
    """The home screen brings the pull forward.

    A five-minute background tick is right for a poll and wrong for the
    moment somebody opens the screen that draws watched state -- and, as
    [iw] found, for the moment they open it and then go offline: the stale
    answer is then what they have for as long as they are offline.
    """

    def _manager(self, last=0.0, now=1000.0):
        from jellyfin_mpv_shim.sync.manager import SyncManager

        m = SyncManager.__new__(SyncManager)
        m._last_userdata = last
        m._wake = mock.Mock()
        return m

    def test_a_request_makes_the_pull_due_and_wakes_the_worker(self):
        import time as _time
        m = self._manager(last=0.0)
        with mock.patch.object(_time, "monotonic", return_value=1000.0):
            m.request_userdata_refresh()
        self.assertEqual(m._last_userdata, 0.0)
        m._wake.set.assert_called_once()

    def test_bouncing_in_and_out_of_home_does_not_hammer_it(self):
        """Without a floor this is a round trip per visit."""
        import time as _time
        from jellyfin_mpv_shim.sync import manager as mgr

        m = self._manager()
        with mock.patch.object(_time, "monotonic", return_value=1000.0):
            m._last_userdata = 1000.0 - (mgr.USERDATA_REQUEST_FLOOR / 2)
            m.request_userdata_refresh()
        self.assertNotEqual(m._last_userdata, 0.0)
        m._wake.set.assert_not_called()

    def test_it_does_not_block_whoever_asked(self):
        """Marks it due and returns -- the requests happen on the sync
        thread, not on the one that was loading a page."""
        import time as _time
        m = self._manager()
        m._refresh_userdata = mock.Mock()
        with mock.patch.object(_time, "monotonic", return_value=1000.0):
            m.request_userdata_refresh()
        m._refresh_userdata.assert_not_called()
