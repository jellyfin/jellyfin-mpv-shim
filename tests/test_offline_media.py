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
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.sync import offline_media
from jellyfin_mpv_shim.sync.db import NO_ACTOR, COLUMNS, SyncDB, STATUS_COMPLETE


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


def add_row(db, item_id, file_path, content_server_id="SRV",
            size_bytes=None, media_source_id="src"):
    row = {c: None for c in COLUMNS}
    row.update({
        "item_id": item_id,
        # Written HERE rather than by a follow-up `upsert`: that is
        # INSERT OR REPLACE, so a second call resets every column it does not
        # name -- status and content_server_id included -- and the row stops
        # being a completed download at all. See NOT_UPSERTED in sync/db.py.
        "size_bytes": size_bytes,
        "media_source_id": media_source_id,
        "server_uuid": "srv",
        # The scoping key. A real row always has it -- `_add_row` takes it
        # from the item and `_adopt_orphan` from the manifest -- so a fixture
        # that left it NULL would make every scoped read answer "cannot
        # tell" and pass whatever it was asked.
        "content_server_id": content_server_id,
        "type": "Episode",
        "file_path": file_path,
        "status": STATUS_COMPLETE,
        "item_json": json.dumps({"Type": "Episode", "Name": "Ep"}),
        "source_json": json.dumps({"Id": "src", "MediaStreams": []}),
    })
    db.upsert(row)


class UpdateUserdataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = make_db(os.path.join(self.tmp.name, "cat.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _userdata(self, item_id):
        """What the catalog holds for the person who played it.

        Watched state moved off the download row into the per-actor table
        (and CX8 has since dropped the column it was in); these fixtures name
        no credential, so the actor is the unattributed sentinel. The old key
        names are kept so these tests keep asserting the advance-only rule
        rather than the storage."""
        got = self.db.userdata(item_id, actor=(None, NO_ACTOR))
        return {"Played": got["played"],
                "PlaybackPositionTicks": got["position_ticks"]}

    def test_played_sticks_true(self):
        # No seeded state: a fresh row has nothing in `item_userdata`, which is
        # the "unwatched" this used to spell as a `{"Played": False}` blob on
        # the download row.
        add_row(self.db, "a", "a/file.mkv")
        self.db.update_userdata("a", played=True,
        actor=(None, NO_ACTOR))
        self.assertTrue(self._userdata("a")["Played"])

    def test_position_advances_only(self):
        add_row(self.db, "a", "a/file.mkv")
        self.db.update_userdata("a", position_ticks=500,
                                actor=(None, NO_ACTOR))
        self.db.update_userdata("a", position_ticks=1000,
        actor=(None, NO_ACTOR))
        self.assertEqual(self._userdata("a")["PlaybackPositionTicks"], 1000)
        # A stale, earlier position must not overwrite a later one.
        self.db.update_userdata("a", position_ticks=200,
        actor=(None, NO_ACTOR))
        self.assertEqual(self._userdata("a")["PlaybackPositionTicks"], 1000)

    def test_missing_item_is_noop(self):
        # No row for "ghost" — must not raise.
        self.db.update_userdata("ghost", played=True, position_ticks=10,
        actor=(None, NO_ACTOR))

    def test_delete_watched_sees_offline_play(self):
        """The end-to-end point of fix S9: offline playback marks an item
        played, and "delete watched downloads" can see that it did.

        Asserted through `played_by_anyone`, which is what the delete paths
        actually ask now. [iw]'s ruling is that deletion stays machine-wide
        -- one file, so one decision -- so the question is whether *anyone*
        watched it, not whether a particular account did.
        """
        add_row(self.db, "a", "a/file.mkv")
        self.assertFalse(self.db.played_by_anyone("a"))
        self.db.update_userdata("a", played=True,
                                actor=(None, NO_ACTOR))
        self.assertTrue(self.db.played_by_anyone("a"))


class FactoryFileExistsGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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


class ADownloadAnswersOnlyForItsOwnServerTest(unittest.TestCase):
    """Jellyfin derives an item id from the media's path with no server
    component in it, so two installs both mounting their library at
    `/media` hand out the same id for *different files*
    (docs/jellyfin-api-notes.md 13b). The catalog holds one row per id, so
    an unscoped answer played one server's file for another server's item.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "a"))
        with open(os.path.join(self.root, "a", "file.mkv"), "wb") as fh:
            fh.write(b"x")
        self.db = make_db(os.path.join(self.root, "cat.db"))
        self.addCleanup(self.db.close)
        add_row(self.db, "a", "a/file.mkv")          # server_uuid "srv"
        self._patch = mock.patch.object(offline_media, "syncManager",
                                        FakeSync(self.db, self.root))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _registry(self, mapping):
        """Live clients by saved-login uuid, plus the credentials that say
        which Jellyfin server each of those logins is on -- the catalog
        scopes by the second, so a registry alone cannot answer."""
        from jellyfin_mpv_shim import clients
        from jellyfin_mpv_shim.users import userManager

        patch = mock.patch.object(clients.clientManager, "clients", mapping)
        patch.start()
        self.addCleanup(patch.stop)
        creds = mock.patch.object(userManager, "users", [{
            "id": "local", "credentials": [
                {"uuid": "srv", "Id": "SRV"},
                {"uuid": "other", "Id": "OTHER"},
            ]}])
        creds.start()
        self.addCleanup(creds.stop)

    def test_another_servers_item_does_not_play_the_local_file(self):
        other = object()
        self._registry({"srv": object(), "other": other})
        video = offline_media.offline_video_factory("a", FakeParent(other))
        self.assertIsNone(video,
                          "an item from another server played the copy "
                          "downloaded from this one")

    def test_its_own_server_still_does(self):
        owner = object()
        self._registry({"srv": owner})
        self.assertIsInstance(
            offline_media.offline_video_factory("a", FakeParent(owner)),
            offline_media.OfflineVideo)

    def test_fully_offline_still_resolves(self):
        """The floor. With no client there is no second server to confuse
        the row with, and the catalog is the only source of items there is
        -- so refusing here would take offline playback away entirely."""
        self._registry({})
        self.assertIsInstance(
            offline_media.offline_video_factory("a", FakeParent(client=None)),
            offline_media.OfflineVideo)

    def test_an_unhomed_row_does_not_substitute_for_a_named_server(self):
        """Substitution needs an *exact* server match, so the NULL branch
        every content read relies on must not reach it.

        A row whose `content_server_id` is NULL answers content reads on
        purpose: it cannot be shown to be somebody else's, and refusing would
        make a download on disk invisible and undeletable. That is a
        **visibility** answer. Handing the same permission to substitution
        lets an unattributed file play for a server that never held it.
        """
        add_row(self.db, "b", "a/file.mkv", content_server_id=None)
        owner = object()
        self._registry({"srv": owner})
        self.assertIsNone(
            offline_media.offline_video_factory("b", FakeParent(owner)),
            "a row belonging to no known server was played for a server "
            "that named itself")

    def test_a_login_that_will_not_resolve_does_not_substitute(self):
        """"I could not establish who is asking" must not authorise the
        broadest possible match.

        `_asking_server` answers `ANY_SERVER` both when nothing asked and
        when a lookup *raised*, so a rule reading its answer cannot tell them
        apart. The route is narrow and real: a registry that parses but holds
        a non-dict credential (the registry gate at `4ad0b234` removed the
        broader one, since a registry that will not parse no longer starts the
        subsystem).
        """
        from jellyfin_mpv_shim import clients
        from jellyfin_mpv_shim.users import userManager

        owner = object()
        patch = mock.patch.object(clients.clientManager, "clients",
                                  {"srv": owner})
        patch.start()
        self.addCleanup(patch.stop)
        creds = mock.patch.object(userManager, "users", [
            {"id": "local", "credentials": ["not-a-dict"]}])
        creds.start()
        self.addCleanup(creds.stop)

        self.assertIsNone(
            offline_media.offline_video_factory("a", FakeParent(owner)),
            "a client whose server could not be established was allowed to "
            "substitute a local file")

    def test_the_catalog_still_answers_that_lookup_when_it_fails(self):
        """The other half of the same state, and the control that keeps the
        fix honest: refusing *substitution* must not empty the library.

        Visibility stays permissive when the registry cannot be read --
        otherwise a resolution failure makes the user's downloads disappear,
        which is the failure that matters more than playing the wrong file.
        """
        from jellyfin_mpv_shim.sync.db import ANY_SERVER
        self.assertTrue(self.db.is_complete("a", server_id=ANY_SERVER))
        self.assertIn("a", self.db.downloaded_item_ids(server_id=ANY_SERVER))

    def _in_a_group(self, yes=True):
        """Stand in for a running SyncPlay group without importing the player.

        `_in_group` asks `sys.modules` on purpose -- importing `player.py`
        opens a real mpv window -- so the fake goes in the same place the
        production code looks.
        """
        import sys as _sys

        class _Syncplay:
            @staticmethod
            def in_group():
                return yes

        class _PM:
            syncplay = _Syncplay()

        class _Mod:
            playerManager = _PM()

        patch = mock.patch.dict(_sys.modules,
                                {"jellyfin_mpv_shim.player": _Mod()})
        patch.start()
        self.addCleanup(patch.stop)

    def _server_says(self, owner, size, source_id="src"):
        """Give the live client a MediaSources answer for item "a"."""
        class _Jf:
            @staticmethod
            def get_item(item_id):
                return {"MediaSources": [{"Id": source_id, "Size": size}]}

        owner.jellyfin = _Jf()

    def test_in_a_group_a_matching_size_still_plays_the_local_copy(self):
        """The control: the check must not simply refuse in every group."""
        add_row(self.db, "a", "a/file.mkv", size_bytes=1234)
        owner = mock.Mock()
        self._registry({"srv": owner})
        self._in_a_group()
        self._server_says(owner, 1234)
        self.assertIsInstance(
            offline_media.offline_video_factory("a", FakeParent(owner)),
            offline_media.OfflineVideo)

    def test_in_a_group_a_different_size_falls_back_to_the_server(self):
        """The same item id on the same server, and a file that is not the one
        downloaded -- a replaced encode or a different cut. Alone that is a
        surprise; in a group every member shares one timeline, so it desyncs
        everybody."""
        add_row(self.db, "a", "a/file.mkv", size_bytes=1234)
        owner = mock.Mock()
        self._registry({"srv": owner})
        self._in_a_group()
        self._server_says(owner, 9999)
        self.assertIsNone(
            offline_media.offline_video_factory("a", FakeParent(owner)),
            "a group played a local file the server no longer holds")

    def test_in_a_group_an_unanswerable_size_falls_back_to_the_server(self):
        """Unverified is not good enough inside a group, and refusing costs a
        fallback rather than the film: a group implies a reachable server."""
        add_row(self.db, "a", "a/file.mkv", size_bytes=1234)
        owner = mock.Mock()
        self._registry({"srv": owner})
        self._in_a_group()

        class _Jf:
            @staticmethod
            def get_item(item_id):
                raise RuntimeError("server went away mid-group")

        owner.jellyfin = _Jf()
        self.assertIsNone(
            offline_media.offline_video_factory("a", FakeParent(owner)))

    def test_outside_a_group_the_size_is_not_asked_about(self):
        """Ordinary playback does not pay for this. An identical item id means
        an identical media path, so a matching id is unlikely to name a
        different video -- which is the reason the check is scoped to groups."""
        add_row(self.db, "a", "a/file.mkv", size_bytes=1234)
        owner = mock.Mock()
        self._registry({"srv": owner})
        self._in_a_group(yes=False)
        self._server_says(owner, 9999)
        self.assertIsInstance(
            offline_media.offline_video_factory("a", FakeParent(owner)),
            offline_media.OfflineVideo,
            "the size check leaked outside a SyncPlay group")

    def test_still_substitutable_answers_for_the_video_in_hand(self):
        """The same rule asked again later, which is what a mid-film group join
        needs. It must answer True for anything that is not a local
        substitution, so a caller need not know what it is holding."""
        add_row(self.db, "a", "a/file.mkv", size_bytes=1234)
        owner = mock.Mock()
        self._registry({"srv": owner})

        # Not a local substitution at all.
        self.assertTrue(offline_media.still_substitutable(None))
        self.assertTrue(offline_media.still_substitutable(object()))

        self._in_a_group(yes=False)
        self._server_says(owner, 9999)
        video = offline_media.offline_video_factory("a", FakeParent(owner))
        self.assertIsInstance(video, offline_media.OfflineVideo)
        # Outside a group the size is not asked about, so it still stands.
        self.assertTrue(offline_media.still_substitutable(video))

    def test_still_substitutable_turns_false_once_a_group_exists(self):
        """The whole point: the answer changes without the video changing.

        The film was cleared for playback under rules that did not include a
        group, and joining one is what makes the earlier answer wrong.
        """
        add_row(self.db, "a", "a/file.mkv", size_bytes=1234)
        owner = mock.Mock()
        self._registry({"srv": owner})
        self._in_a_group(yes=False)
        self._server_says(owner, 9999)
        video = offline_media.offline_video_factory("a", FakeParent(owner))
        self.assertTrue(offline_media.still_substitutable(video))

        self._in_a_group(yes=True)
        self.assertFalse(
            offline_media.still_substitutable(video),
            "joining a group did not invalidate a local copy the server no "
            "longer holds")

    def test_work_offline_plays_the_local_copy_even_with_a_client(self):
        """A control for the ruling, not a new behaviour.

        `work_offline` is the offline switch, and the code tests it *beside*
        `client is None`, so "client present and working offline" is a state it
        already expects. Gating there would refuse the local copy and fall
        back to streaming, against the setting the user just turned on.
        """
        add_row(self.db, "c", "a/file.mkv", content_server_id=None)
        owner = object()
        self._registry({"srv": owner})
        with mock.patch.object(settings, "work_offline", True):
            self.assertIsInstance(
                offline_media.offline_video_factory("c", FakeParent(owner)),
                offline_media.OfflineVideo)

    # -- `prefer_downloaded`, the setting that decides the ONLINE case ------
    # It had no test at all, in this module or anywhere else, so every
    # assertion above passed only because its default is True. What the
    # three below fix is that the *other* value was unpinned in both
    # directions: turning it off did not have to stream, and it could have
    # taken offline playback with it.

    def test_turning_the_preference_off_streams_instead(self):
        """`prefer_downloaded = False` with the server in reach: the factory
        answers None so `build_video` falls through to the remote source.
        The copy on disk is not deleted or hidden -- it is simply not
        substituted for a stream the user can have."""
        owner = object()
        self._registry({"srv": owner})
        with mock.patch.object(settings, "prefer_downloaded", False):
            self.assertIsNone(
                offline_media.offline_video_factory("a", FakeParent(owner)),
                "the local copy was substituted against the setting")

    def test_but_it_does_not_reach_a_fully_offline_session(self):
        """The half that would be a disaster rather than a preference. With
        no client there is nothing to fall through *to*, so the setting must
        not apply: `client is None` is tested beside it, not after it."""
        self._registry({})
        with mock.patch.object(settings, "prefer_downloaded", False):
            self.assertIsInstance(
                offline_media.offline_video_factory("a",
                                                    FakeParent(client=None)),
                offline_media.OfflineVideo,
                "turning off a preference took offline playback away")

    def test_nor_does_it_override_work_offline(self):
        """Both settings say something about the same choice and the user can
        hold them at once. `work_offline` is the explicit instruction, so it
        wins: a client in reach does not make it stream."""
        owner = object()
        self._registry({"srv": owner})
        with mock.patch.object(settings, "prefer_downloaded", False), \
                mock.patch.object(settings, "work_offline", True):
            self.assertIsInstance(
                offline_media.offline_video_factory("a", FakeParent(owner)),
                offline_media.OfflineVideo,
                "Work Offline streamed anyway")


class OfflineTrickplayTest(unittest.TestCase):
    """``OfflineVideo`` is a drop-in for ``media.Video``, and the trickplay
    worker calls it through exactly the same names.

    It stopped being one when the worker learned to fetch a *window* of the
    tiles: ``Video.get_hls_tile_images`` grew a ``start`` and this one did
    not, so every downloaded item raised TypeError and lost its previews
    while the online path was fine. That is the repo's recurring shape --
    the right mechanism applied to one of two implementations.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.db = make_db(os.path.join(self.root, "cat.db"))
        self.sync = FakeSync(self.db, self.root)
        self._patch = mock.patch.object(offline_media, "syncManager",
                                        self.sync)
        self._patch.start()
        self.item_dir = os.path.join(self.root, "a")
        os.makedirs(self.item_dir)
        with open(os.path.join(self.item_dir, "file.mkv"), "wb") as fh:
            fh.write(b"x")
        add_row(self.db, "a", "a/file.mkv")

    def tearDown(self):
        self._patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def _video(self, tiles=4):
        with open(os.path.join(self.item_dir, "trickplay.json"), "w",
                       encoding="utf-8") as fh:
            json.dump({"width": 320,
                       "data": {"Width": 320, "Height": 180, "TileWidth": 2,
                                "TileHeight": 2, "ThumbnailCount": tiles * 4,
                                "Interval": 10000}}, fh)
        tp_dir = os.path.join(self.item_dir, "trickplay", "320")
        os.makedirs(tp_dir)
        for i in range(tiles):
            with open(os.path.join(tp_dir, "%d.jpg" % i), "wb") as fh:
                fh.write(b"tile%d" % i)
        return offline_media.OfflineVideo("a", FakeParent(client=None))

    def test_the_trickplay_surface_matches_the_online_one(self):
        """Signatures, not a call: this is the check that generalises to the
        next parameter the worker starts passing."""
        import inspect

        from jellyfin_mpv_shim.media import Video

        for name in ("get_bif", "get_hls_tile_images", "get_chapters"):
            self.assertEqual(
                inspect.signature(getattr(offline_media.OfflineVideo, name)),
                inspect.signature(getattr(Video, name)),
                "OfflineVideo.%s has drifted from media.Video.%s -- the "
                "trickplay worker calls whichever it is handed" % (name, name))

    def test_a_window_reads_the_tiles_it_asked_for(self):
        """``start`` is the whole point: without it a mid-film window is
        answered with the beginning of the film."""
        video = self._video()
        self.assertEqual(list(video.get_hls_tile_images(320, 2, start=2)),
                         [b"tile2", b"tile3"])

    def test_a_missing_tile_ends_the_run_rather_than_raising(self):
        """A partial download is a short run, which the decoder reports as
        frames written -- an over-reported count is a read past EOF in mpv."""
        video = self._video(tiles=3)
        self.assertEqual(list(video.get_hls_tile_images(320, 4, start=2)),
                         [b"tile2"])

    def test_no_downloaded_trickplay_yields_nothing(self):
        video = offline_media.OfflineVideo("a", FakeParent(client=None))
        self.assertIsNone(video.get_bif())
        self.assertEqual(list(video.get_hls_tile_images(320, 2, start=1)), [])


class OfflineSegmentsTest(unittest.TestCase):
    """Skip Intro/Credits over a downloaded file.

    The segments come from a plugin on the server, so they are cached beside
    the media at download time (SyncManager._download_segments) and read back
    here. Before this there was simply no offline detection at all, which is
    what "works, except for downloaded files" meant.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
        with open(os.path.join(self.root, "a", "segments.json"), "w",
                       encoding="utf-8") as fh:
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


class OfflineTrackPrecedenceTest(unittest.TestCase):
    """A remembered track must outrank language_config for a downloaded item.

    `play()` resolves the rule and then applies the remembered choice over it,
    both before `get_playback_url`. `Video.map_streams` skips the rule
    afterwards (`_tracks_resolved`) so it cannot overwrite the memory.
    `OfflineVideo.map_streams` is a full override and had no such guard, and
    its `resolve_tracks_for_negotiation` was a deliberate no-op -- so the flag
    was never set, the rule ran again from `get_playback_url`, and it
    overwrote the track the user picked on the previous episode. Downloaded
    items only; the online path was covered and correct.
    """

    STREAMS = [
        {"Index": 0, "Type": "Video", "Codec": "h264"},
        {"Index": 1, "Type": "Audio", "Codec": "aac", "Language": "eng"},
        {"Index": 2, "Type": "Audio", "Codec": "aac", "Language": "jpn"},
    ]

    def _video(self):
        from jellyfin_mpv_shim.sync import offline_media

        v = offline_media.OfflineVideo.__new__(offline_media.OfflineVideo)
        v.item = {"Id": "ep1", "MediaSources": []}
        v._source = {"Id": "s", "MediaStreams": [dict(x) for x in self.STREAMS]}
        v.media_source = None
        v.explicit_tracks = False
        v.aid = v.sid = None
        return v

    def _rules(self):
        from jellyfin_mpv_shim.language_config import parse_language_config
        return parse_language_config([{"alang": "jpn"}])

    def test_the_hook_settles_the_rule_and_marks_it_done(self):
        from jellyfin_mpv_shim.conf import settings

        v = self._video()
        with mock.patch.object(settings, "language_config", self._rules()):
            v.resolve_tracks_for_negotiation()
        self.assertEqual(v.aid, 2, "the rule did not apply offline")
        self.assertTrue(v._tracks_resolved)

    def test_map_streams_does_not_overwrite_a_remembered_track(self):
        from jellyfin_mpv_shim.conf import settings

        v = self._video()
        with mock.patch.object(settings, "language_config", self._rules()):
            v.resolve_tracks_for_negotiation()   # rule picks jpn (2)
            v.aid = 1                            # ...then memory picks eng
            v.media_source = v._source
            v.map_streams()
        self.assertEqual(
            v.aid, 1,
            "language_config overwrote the track carried over from the "
            "previous episode, on a downloaded item")

    def test_the_rule_still_applies_with_no_memory(self):
        """The control: guarding must not make the rule inert offline."""
        from jellyfin_mpv_shim.conf import settings

        v = self._video()
        with mock.patch.object(settings, "language_config", self._rules()):
            v.resolve_tracks_for_negotiation()
            v.media_source = v._source
            v.map_streams()
        self.assertEqual(v.aid, 2)
