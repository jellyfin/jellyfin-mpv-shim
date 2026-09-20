"""Who an offline change is filed against, end to end through the real
resolver.

Nothing above the catalog and below the network is stubbed: a real
``SyncDB`` on disk, the real gateway, the real ``SyncManager``, the real
``OfflineVideo`` and the real offline library source. The one thing patched
is ``userManager``'s saved credentials, because those stand in for a login
this machine really did perform.

**The rule: no double for ``actor_of``, ``actor_for`` or ``actor_on``.**
Three findings in the 2026-09-12 round reached review past a test double
that answered with a real person where production answered with nobody --
the double asserted the plumbing while replacing the broken part. So the
resolver runs for real here and is handed only what production hands it.

Lives in the unit suite rather than tests/integration/ deliberately: none of
it needs mpv, a display or ffmpeg, and `unittest discover tests` does not
reach tests/integration -- which is also what `tools/mutate_round.py` runs,
so a killer parked there kills nothing.

"""

import json
import os
import types
import unittest

from jellyfin_mpv_shim.sync.db import NO_ACTOR


def _row(item_id, name, path, **kw):
    row = {
        "item_id": item_id, "server_uuid": None, "name": name,
        "type": "Movie", "status": "complete",
        "file_path": os.path.basename(path),
        "size_bytes": os.path.getsize(path),
        "downloaded_bytes": os.path.getsize(path),
        "item_json": json.dumps({"Id": item_id, "Name": name,
                                 "Type": "Movie",
                                 "RunTimeTicks": 120 * 10000000}),
    }
    row.update(kw)
    return row



class OfflineActorEndToEndTest(unittest.TestCase):
    """Who an offline change is filed against, through the real resolver.

    **Nothing here stubs ``actor_of``, ``actor_for`` or ``actor_on``.** The
    findings this class exists for all reached review past a double that
    answered with a real person where production answers with nobody, so a
    stand-in for the resolver is the one thing that may not appear. Saved
    credentials are patched onto ``userManager`` instead and the real
    resolution runs on them.

    Real catalog, real gateway, real offline source, no server and no mpv:
    everything these findings live in is above the player and below the
    network.
    """

    #: The Jellyfin server the rows belong to, and the saved logins two
    #: local profiles hold for it. Two uuids for one ServerId is the normal
    #: shape -- each profile logs in separately -- and it is what lets a
    #: mark be attributed to the downloader instead of the viewer.
    SERVER_ID = "srv-id"
    LOGIN_X, USER_X = "login-x", "user-x"
    LOGIN_Y, USER_Y = "login-y", "user-y"
    #: What the browser browses the downloads screen as. Not a login: there
    #: is no credential behind it, which is the whole difficulty.
    OFFLINE = "offline"

    def setUp(self):
        import shutil
        import tempfile
        from unittest import mock

        from jellyfin_mpv_shim.mpvtk_browser.gateway import deps as gw_deps
        from jellyfin_mpv_shim.sync import manager as sync_manager
        from jellyfin_mpv_shim.sync import offline_media
        from jellyfin_mpv_shim.sync.db import SyncDB
        from jellyfin_mpv_shim.users import userManager

        self.tmp = tempfile.mkdtemp(prefix="jms-e2e-actor-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.catalog = os.path.join(self.tmp, "sync.db")

        # Placeholder bytes rather than real media: nothing below plays.
        self.db = SyncDB(self.catalog)
        self.addCleanup(self.db.close)
        for item_id, name, extra in (
                ("m1", "Alpha", {}),
                ("m2", "Beta", {}),
                ("e1", "S1E1", {"type": "Episode", "series_id": "sh1",
                                "season_id": "sn1"}),
                ("e2", "S1E2", {"type": "Episode", "series_id": "sh1",
                                "season_id": "sn1"}),
                ("b1", "A Book", {"type": "Book"})):
            path = os.path.join(self.tmp, "%s.bin" % item_id)
            with open(path, "wb") as fh:
                fh.write(b"\0" * 64)
            # `server_uuid` is X's login on purpose: X is the profile that
            # downloaded these, and "the downloader wins" is the defect.
            self.db.upsert(_row(item_id, name, path,
                                server_uuid=self.LOGIN_X,
                                content_server_id=self.SERVER_ID, **extra))

        for attr, value in (
                ("users", [
                    {"id": "x", "credentials": [
                        {"uuid": self.LOGIN_X, "Id": self.SERVER_ID,
                         "UserId": self.USER_X}]},
                    {"id": "y", "credentials": [
                        {"uuid": self.LOGIN_Y, "Id": self.SERVER_ID,
                         "UserId": self.USER_Y}]}]),
                ("active_id", "x")):
            patch = mock.patch.object(userManager, attr, value)
            self.addCleanup(patch.stop)
            patch.start()

        # A real SyncManager over the real catalog -- `actor_of` and
        # `content_id_for` are the methods under test, so the instance has
        # to be the real class rather than a namespace carrying a `db`.
        mgr = sync_manager.SyncManager.__new__(sync_manager.SyncManager)
        mgr.db = self.db
        mgr.root = self.tmp
        for module in (sync_manager, offline_media):
            patch = mock.patch.object(module, "syncManager", mgr)
            self.addCleanup(patch.stop)
            patch.start()
        self.manager = mgr

        # Offline: no client for any server, so every gateway path takes
        # the queueing branch.
        patch = mock.patch.object(gw_deps, "clientManager",
                                  types.SimpleNamespace(clients={}))
        self.addCleanup(patch.stop)
        patch.start()

    # -- helpers ---------------------------------------------------------

    def _active(self, profile_id):
        from jellyfin_mpv_shim.users import userManager
        userManager.active_id = profile_id

    def _gateway(self):
        from jellyfin_mpv_shim.mpvtk_browser import gateway as browser_gw
        return browser_gw.PlayerGateway()

    def _source(self):
        """The offline library as the browser builds it: the real
        ``userManager.actor_on``, so it reads the shelf of whoever is the
        active profile right now."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            OfflineLibrarySource)
        from jellyfin_mpv_shim.users import userManager
        return OfflineLibrarySource(self.catalog,
                                    actor_on=userManager.actor_on)

    def _userdata(self, item_id):
        return ((self._source().get_item(self.OFFLINE, item_id) or {})
                .get("UserData") or {})

    def _queued(self):
        return [(p["server_id"], p["user_id"], p["item_id"])
                for p in self.db.list_playstate()]

    # -- the marks -------------------------------------------------------

    def test_an_offline_mark_belongs_to_the_person_at_the_keyboard(self):
        """The failing case the whole repair is for.

        Marking a downloaded film watched from the downloads screen, which
        is browsed as the pseudo-server. Nothing names a login, so the only
        thing that can name a person is the active profile's credential for
        the server the row belongs to."""
        self.assertTrue(
            self._gateway()._queue_offline_watched(self.OFFLINE, "m1", True),
            "the offline mark was refused outright")
        self.assertEqual(self._queued(),
                         [(self.SERVER_ID, self.USER_X, "m1")],
                         "the mark did not reach the replay queue as a "
                         "person the server could be told about")
        self.assertTrue(self._userdata("m1").get("Played"),
                        "the mark is not visible on the shelf that made it")

    def test_an_offline_series_mark_fans_out_to_every_episode(self):
        self.assertTrue(
            self._gateway()._queue_offline_watched(self.OFFLINE, "sh1", True))
        self.assertEqual(sorted(self._queued()),
                         [(self.SERVER_ID, self.USER_X, "e1"),
                          (self.SERVER_ID, self.USER_X, "e2")])
        for item_id in ("e1", "e2"):
            self.assertTrue(self._userdata(item_id).get("Played"), item_id)

    def test_an_offline_reading_position_round_trips(self):
        """Both halves. The cursor is read back by the reader, and the
        queued row is what the server eventually hears -- and the actor is
        resolved twice on this path, so a test that only reopens the book
        passes with the second resolution still wrong."""
        ticks = 42 * 10000000
        self.assertFalse(
            self._gateway().record_reading_position(self.OFFLINE, "b1", ticks),
            "there is no server here, so nothing should claim it took it")
        self.assertEqual(self._userdata("b1").get("PlaybackPositionTicks"),
                         ticks, "the cursor did not survive to the shelf")
        self.assertEqual(self._queued(), [(self.SERVER_ID, self.USER_X, "b1")],
                         "the page turn was never queued for the server")

    def test_a_mark_made_through_one_server_never_lands_on_another(self):
        """A login on server B, a row belonging to server A.

        The pair written used to be (server A, a user of server B): an
        actor that exists nowhere, invisible to every reader, and created
        without an error. It is also the only case here that tells a scoped
        ``watched_targets`` from a re-labelled one."""
        from unittest import mock

        from jellyfin_mpv_shim.users import userManager
        with mock.patch.object(userManager, "users", [
                {"id": "x", "credentials": [
                    {"uuid": self.LOGIN_X, "Id": self.SERVER_ID,
                     "UserId": self.USER_X},
                    {"uuid": "login-b", "Id": "srv-other",
                     "UserId": "user-b"}]}]):
            self._gateway()._queue_offline_watched("login-b", "m1", True)
        self.assertEqual(
            [a for a in self.db.userdata_actors("m1")
             if a[1] not in (NO_ACTOR,)], [],
            "a mark for another server's account landed on this row")
        self.assertEqual(self._queued(), [],
                         "and was queued to be sent as it")

    # -- two people, one copy --------------------------------------------

    def _play(self, item_id, ticks, finished=False):
        """Play a downloaded item offline, as the active profile."""
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo
        video = OfflineVideo.__new__(OfflineVideo)
        OfflineVideo.__init__(video, item_id,
                              types.SimpleNamespace(client=None))
        video.record_offline_progress(ticks, finished=finished)
        return video

    def test_two_local_profiles_do_not_share_one_copys_progress(self):
        """X downloaded it and watched half; Y plays the same file.

        Y starts at zero, Y's progress is filed under Y, and X still finds
        their own place on switching back. Everything that resolves the
        actor from the row instead of from the keyboard fails here, because
        the row records X."""
        self._active("x")
        self._play("m1", 30 * 10000000)
        self.assertEqual(self._userdata("m1").get("PlaybackPositionTicks"),
                         30 * 10000000)

        self._active("y")
        self.assertFalse(self._userdata("m1").get("PlaybackPositionTicks"),
                         "Y resumed into X's viewing of the same copy")
        self._play("m1", 5 * 10000000)
        self.assertEqual(self._userdata("m1").get("PlaybackPositionTicks"),
                         5 * 10000000)
        self.assertEqual(
            sorted(self._queued()),
            [(self.SERVER_ID, self.USER_X, "m1"),
             (self.SERVER_ID, self.USER_Y, "m1")],
            "the two viewings did not queue as two people")

        self._active("x")
        self.assertEqual(self._userdata("m1").get("PlaybackPositionTicks"),
                         30 * 10000000,
                         "X's place moved when Y watched the same file")

    def _with_a_snapshot(self, item_id="m3"):
        """A download row whose frozen DTO carries the server's `UserData`.

        What the previous test omits, and the omission is why it could not
        see this: `_add_row` stores the DTO verbatim (`json.dumps(item)`), so
        the watched flag and resume position the server reported **for the
        account that asked** are frozen into `item_json`. A fixture whose
        snapshot is blank makes the path unreachable while reporting a pass.
        """
        path = os.path.join(self.tmp, "%s.bin" % item_id)
        with open(path, "wb") as fh:
            fh.write(b"\0" * 64)
        self.db.upsert(_row(
            item_id, "Gamma", path, server_uuid=self.LOGIN_X,
            content_server_id=self.SERVER_ID,
            item_json=json.dumps({
                "Id": item_id, "Name": "Gamma", "Type": "Movie",
                "RunTimeTicks": 120 * 10000000,
                "UserData": {"Played": True, "PlayCount": 1,
                             "PlaybackPositionTicks": 45 * 10000000}})))
        return item_id

    def test_a_profile_with_no_state_does_not_inherit_the_downloaders(self):
        """**The snapshot is not this actor's state and was shown as if it
        were.** `_item_from_row` overlaid the browsing person's own state only
        `if userdata:`, so a profile with nothing recorded kept the frozen
        one -- saw the downloader's watched flag, and could press Resume into
        their position.

        `_userdata_for`'s docstring assumed the opposite: empty "is what the
        snapshot already says". That assumption is the defect.
        `docs/offline-sync.md` section 1 is the rule -- state is per actor and
        "there is no second copy".
        """
        item_id = self._with_a_snapshot()
        self._active("y")
        data = self._userdata(item_id)
        self.assertFalse(data.get("Played"),
                         "Y inherited the downloader's watched flag")
        self.assertFalse(data.get("PlaybackPositionTicks"),
                         "Y could resume from the downloader's position")
        self.assertFalse(data.get("PlayedPercentage"),
                         "the tile kept a progress bar for somebody else's "
                         "viewing")

    def test_and_the_downloader_sees_it_too_until_they_play_it_here(self):
        """**The stated cost of the ruling, pinned so it is not read as a
        regression.** Nobody inherits a snapshot, including the account that
        asked for the download: what it reported at download time is not a
        viewing on this machine. Their place comes back the moment they play
        it here, which is what `item_userdata` is for -- and the assertion
        below is that half, so this is not simply "everything reads zero".
        """
        item_id = self._with_a_snapshot()
        self._active("x")
        self.assertFalse(self._userdata(item_id).get("Played"))
        self._play(item_id, 30 * 10000000)
        self.assertEqual(30 * 10000000,
                         self._userdata(item_id).get("PlaybackPositionTicks"),
                         "X's own viewing on this machine did not come back")

    # -- deletion --------------------------------------------------------

    def test_deleting_a_download_takes_its_watched_state_with_it(self):
        """Watched state used to live on the row and died with it. It is a
        separate table now, and nothing purged it -- so a re-downloaded
        episode came back already watched, and "Remove Watched" deleted it
        again immediately."""
        self._gateway()._queue_offline_watched(self.OFFLINE, "m1", True)
        self.assertTrue(self.db.played_by_anyone("m1"))
        self.db.delete("m1")
        path = os.path.join(self.tmp, "m1.bin")
        self.db.upsert(_row("m1", "Alpha", path, server_uuid=self.LOGIN_X,
                            content_server_id=self.SERVER_ID))
        self.assertFalse(
            self.db.played_by_anyone("m1"),
            "a re-downloaded item came back already watched, so the "
            "watched-only sweep will delete it again at once")