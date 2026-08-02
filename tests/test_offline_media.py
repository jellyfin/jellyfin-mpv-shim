import json
import os
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.sync import offline_media
from jellyfin_mpv_shim.sync.db import COLUMNS, SyncDB, STATUS_COMPLETE


class FakeParent:
    """Stand-in for media.Media: the factory only reads ``.client``."""

    def __init__(self, client=None):
        self.client = client
        self.queue = [{"PlaylistItemId": "p0", "Id": "item1"}]
        self.seq = 0


class FakeSync:
    def __init__(self, db, root):
        self.db = db
        self.root = root


def make_db(path):
    db = SyncDB(path)
    return db


def add_row(db, item_id, file_path, userdata=None):
    row = {c: None for c in COLUMNS}
    row.update({
        "item_id": item_id,
        "server_uuid": "srv",
        "type": "Episode",
        "file_path": file_path,
        "status": STATUS_COMPLETE,
        "item_json": json.dumps({"Type": "Episode", "Name": "Ep"}),
        "source_json": json.dumps({"Id": "src", "MediaStreams": []}),
        "userdata_json": json.dumps(userdata or {}),
    })
    db.upsert(row)


class UpdateUserdataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_db(os.path.join(self.tmp.name, "cat.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _userdata(self, item_id):
        return json.loads(self.db.get(item_id)["userdata_json"])

    def test_played_sticks_true(self):
        add_row(self.db, "a", "a/file.mkv", {"Played": False})
        self.db.update_userdata("a", played=True)
        self.assertTrue(self._userdata("a")["Played"])

    def test_position_advances_only(self):
        add_row(self.db, "a", "a/file.mkv", {"PlaybackPositionTicks": 500})
        self.db.update_userdata("a", position_ticks=1000)
        self.assertEqual(self._userdata("a")["PlaybackPositionTicks"], 1000)
        # A stale, earlier position must not overwrite a later one.
        self.db.update_userdata("a", position_ticks=200)
        self.assertEqual(self._userdata("a")["PlaybackPositionTicks"], 1000)

    def test_missing_item_is_noop(self):
        # No row for "ghost" — must not raise.
        self.db.update_userdata("ghost", played=True, position_ticks=10)

    def test_delete_watched_sees_offline_play(self):
        # The end-to-end point of fix S9: after offline playback marks an item
        # played via update_userdata, the download row's userdata reflects it.
        add_row(self.db, "a", "a/file.mkv", {"Played": False})
        self.db.update_userdata("a", played=True)
        userdata = json.loads(self.db.get("a")["userdata_json"])
        self.assertTrue(userdata.get("Played"))


class FactoryFileExistsGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = make_db(os.path.join(self.root, "cat.db"))
        self.sync = FakeSync(self.db, self.root)
        self._patch = mock.patch.object(offline_media, "syncManager", self.sync)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def test_missing_file_returns_none_when_offline(self):
        add_row(self.db, "a", "a/file.mkv")  # row present, file never created
        video = offline_media.offline_video_factory("a", FakeParent(client=None))
        self.assertIsNone(video)

    def test_existing_file_returns_offline_video(self):
        os.makedirs(os.path.join(self.root, "a"))
        with open(os.path.join(self.root, "a", "file.mkv"), "wb") as fh:
            fh.write(b"x")
        add_row(self.db, "a", "a/file.mkv")
        video = offline_media.offline_video_factory("a", FakeParent(client=None))
        self.assertIsInstance(video, offline_media.OfflineVideo)


class OfflineSegmentsTest(unittest.TestCase):
    """Skip Intro/Credits over a downloaded file.

    The segments come from a plugin on the server, so they are cached beside
    the media at download time (SyncManager._download_segments) and read back
    here. Before this there was simply no offline detection at all, which is
    what "works, except for downloaded files" meant.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = make_db(os.path.join(self.root, "cat.db"))
        self.sync = FakeSync(self.db, self.root)
        self._patch = mock.patch.object(offline_media, "syncManager", self.sync)
        self._patch.start()
        os.makedirs(os.path.join(self.root, "a"))
        with open(os.path.join(self.root, "a", "file.mkv"), "wb") as fh:
            fh.write(b"x")
        add_row(self.db, "a", "a/file.mkv")

    def tearDown(self):
        self._patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def _write(self, segments):
        with open(os.path.join(self.root, "a", "segments.json"), "w") as fh:
            json.dump(segments, fh)

    def _video(self):
        return offline_media.offline_video_factory("a", FakeParent(client=None))

    def test_cached_segments_are_read_back(self):
        self._write([{"Type": "Intro", "StartTicks": 100000000,
                      "EndTicks": 900000000}])
        video = self._video()
        video.get_intro("src")
        self.assertEqual([(i.type, i.start, i.end) for i in video.intros],
                         [("Intro", 10.0, 90.0)])
        self.assertEqual(video.get_current_intro(30.0)[1].type, "Intro")

    def test_an_item_downloaded_before_this_shipped_has_none(self):
        """No file is the same as no segments, not an error."""
        video = self._video()
        video.get_intro("src")
        self.assertEqual(video.intros, [])

    def test_a_malformed_entry_is_skipped_not_fatal(self):
        self._write([{"Type": "Intro"},
                     {"Type": "Outro", "StartTicks": 100000000,
                      "EndTicks": 200000000}])
        video = self._video()
        video.get_intro("src")
        self.assertEqual([i.type for i in video.intros], ["Outro"])

    def test_only_the_wanted_types_are_read_back(self):
        """Every type is on DISK -- what is stored outlives the settings
        that wrote it -- but only the wanted ones reach self.intros, which
        is where online filters too (include_segment_types).

        Letting an "off" type through gives a downloaded file two behaviours
        a streamed one cannot have: player.update sets is_in_intro for
        whatever get_current_intro returns *before* consulting the action,
        so skip_intro_on_seek eats a forward seek inside a segment the
        viewer turned off; and an off-typed segment masks a wanted one where
        they overlap, so the Skip button never appears.
        """
        self._write([{"Type": t, "StartTicks": 0, "EndTicks": 10000000}
                     for t in ("Intro", "Recap", "Commercial")])
        with mock.patch.object(settings, "segment_intro", "ask"), \
                mock.patch.object(settings, "segment_recap", "off"), \
                mock.patch.object(settings, "segment_commercial", "off"):
            video = self._video()
            video.get_intro("src")
        self.assertEqual([i.type for i in video.intros], ["Intro"])

    def test_turning_one_on_later_needs_no_redownload(self):
        """The other half: the file still holds every type, so the filter
        is the only thing between a setting and a segment."""
        self._write([{"Type": t, "StartTicks": 0, "EndTicks": 10000000}
                     for t in ("Intro", "Recap")])
        with mock.patch.object(settings, "segment_intro", "off"), \
                mock.patch.object(settings, "segment_recap", "ask"):
            video = self._video()
            video.get_intro("src")
        self.assertEqual([i.type for i in video.intros], ["Recap"])


if __name__ == "__main__":
    unittest.main()
