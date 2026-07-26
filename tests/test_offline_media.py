import json
import os
import tempfile
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
