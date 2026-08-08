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
