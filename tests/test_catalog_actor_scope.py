"""Per-actor watched state and resume positions.

The catalog is one store per machine; Jellyfin's watched flag and resume
position are per *user*. That mismatch is why a single `userdata_json` blob
per item could not stay honest: two accounts on one box shared one resume
position, so pressing Resume on the second account told that account's
server it had watched what the first account watched.

Content is scoped by server (tests/test_catalog_content_scope.py); this is
the other half.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim.sync.db import (COLUMNS, NO_ACTOR, STATUS_COMPLETE,
                                       SyncDB)

SERVER = "97fd53974c4c4b7cbac55e437dcc094f"
OTHER = "8178115f38154c1ebc64afe778261fda"
ALICE = "jf-alice"
BOB = "jf-bob"

#: uuid -> (ServerId, Jellyfin UserId), the shape a saved credential gives.
ACTORS = {
    "login-a": (SERVER, ALICE),
    "login-b": (SERVER, BOB),
    "login-far": (OTHER, ALICE),
}


def actor_for(server_uuid):
    return ACTORS.get(server_uuid)


def _row(item_id, content_server_id=SERVER, status=STATUS_COMPLETE):
    row = {c: None for c in COLUMNS}
    row.update({"item_id": item_id, "status": status, "type": "Movie",
                "name": item_id, "file_path": "%s/f.mkv" % item_id,
                "content_server_id": content_server_id,
                "item_json": json.dumps({"Id": item_id, "Type": "Movie"})})
    return row


class TheBlobIsNotMigratedTest(unittest.TestCase):
    """R24: the `userdata_json` blob is **not** carried into the per-actor
    table, and this class replaces the eight tests that pinned the opposite.

    The migration filed each blob under the login that downloaded the row,
    because the blob itself names nobody — its keys are PlaybackPositionTicks,
    PlayCount, IsFavorite, LastPlayedDate, Played, Key and ItemId, and the last
    two are item identity. That is a **read** rather than a guess, which is why
    it was written and why its docstring argued for it.

    What settled it against: attributing a blob to the downloader is the same
    wrong attribution `docs/rulings-log.md` R2 rejects, and on a machine two
    people share it
    files one person's viewing under the other's account. [iw]: *"I think it's
    fine for a one-time watch position loss for already deleted servers on a
    one-time migration where we basically recohered the offline sync feature."*

    **Seven of the eight tests this replaces would now pass trivially** —
    they asserted that various rows were *dropped*, and nothing is written for
    any row at all. Kept as a smaller class asserting what is true, because a
    suite of tests that cannot fail is worse than none.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "catalog.db")

    #: The blob a build with no per-actor table wrote at download time.
    BLOB = json.dumps({
        "Played": True, "PlaybackPositionTicks": 1230000000,
        "PlayCount": 3, "IsFavorite": True,
        "LastPlayedDate": "2026-07-14T03:00:06.0510158Z"})

    def _seed(self, **over):
        """A completed row carrying a userdata blob, as a build with no
        per-actor table wrote one.

        The blob goes in with **raw SQL and a column this build has to add
        back**, because `userdata_json` is gone from `downloads` as of 3.0.0
        (CX8) -- so `upsert` would discard it and every test here would be
        seeded with nothing, which is the shape that passes for the wrong
        reason.
        """
        db = SyncDB(self.path)
        row = {c: None for c in COLUMNS}
        row.update(dict({
            "item_id": "film",
            "server_uuid": "login-a",
            "content_server_id": SERVER,
            "status": STATUS_COMPLETE,
            "item_json": json.dumps({"Id": "film", "ServerId": SERVER}),
        }, **over))
        db.upsert(row)
        db.close()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("ALTER TABLE downloads ADD COLUMN userdata_json TEXT")
            conn.execute("UPDATE downloads SET userdata_json = ? "
                         "WHERE item_id = 'film'", (self.BLOB,))
            conn.commit()
        finally:
            conn.close()

    def _reopen(self, resolver=actor_for):
        db = SyncDB(self.path, actor_for=resolver)
        self.addCleanup(db.close)
        return db

    def test_no_account_is_given_the_downloader_s_watched_state(self):
        """Including the downloader's own. Alice downloaded it, and on a shared
        machine the person who watched it may be Bob."""
        self._seed()
        db = self._reopen()
        self.assertEqual([], db.userdata_actors("film"))
        self.assertFalse(db.userdata("film", actor=(SERVER, ALICE))["played"])
        self.assertFalse(db.userdata("film", actor=(SERVER, BOB))["played"])

    def test_the_blob_is_not_interpreted_on_the_way_out(self):
        """It was *left alone* when R24 dropped the migration, and **dropped**
        when CX8 took the column with the downgrade guarantee it was being kept
        for. What has never happened is the thing R24 refused: reading it and
        filing it under the login that downloaded the row.

        So the assertion is about the destination, not the column: the blob
        says Played with a position, and no account has either.
        """
        self._seed()
        db = self._reopen()
        self.assertEqual([], db.userdata_actors("film"))
        self.assertNotIn("userdata_json", db.get("film").keys(),
                         "the column came back")
        for actor in ((SERVER, ALICE), (SERVER, BOB)):
            state = db.userdata("film", actor=actor)
            self.assertFalse(state["played"])
            self.assertFalse(state["position_ticks"])

    def test_the_server_is_what_restores_it(self):
        """Where the state comes back from, which is the whole reason the loss
        is bounded: the first sweep asks the server and files the answer under
        the account that is signed in. Only a server already removed has no
        second source, and that is the loss [iw] accepted.
        """
        self._seed()
        db = self._reopen()
        self.assertFalse(db.userdata("film", actor=(SERVER, BOB))["played"])
        # What `_refresh_userdata` does with what the server said.
        db.update_userdata("film", actor=(SERVER, BOB), played=True,
                           allow_retreat=True)
        self.assertTrue(db.userdata("film", actor=(SERVER, BOB))["played"],
                        "a sweep could not restore the state")

    def test_a_catalog_carrying_a_blob_still_opens(self):
        """The migration is gone, not skipped: a pre-branch catalog must not
        need it to open, and nothing may raise on a column nobody reads."""
        self._seed()
        db = self._reopen()
        self.assertTrue(db.healthy())
        self.assertEqual("film", db.get("film")["item_id"])

    def test_without_a_resolver_it_is_the_same_answer(self):
        """It used to matter -- "cannot ask" was not "told nobody", so the work
        waited for a handle that could resolve. With no work to do, the two
        handles now agree, and saying so is what stops the next reader looking
        for the difference."""
        self._seed()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual([], db.userdata_actors("film"))


class TheRequestingAccountIsRecordedTest(unittest.TestCase):
    """**Who asked** for a download, as an account rather than as a login.

    `server_uuid` names the login that enqueued it, and a login is a delivery
    address: one account reachable at two addresses is two uuids, and deleting
    a server connection and adding it back mints a third. R14's actual bug is
    the first of those -- unattended fetching keyed on sign-ins is "on,
    configured, and silently does nothing" where one server answers at two
    addresses -- and R19 is the durable fix: record the `(ServerId, UserId)`
    pair at enqueue, the same shape `pending_playstate` already carries.

    `server_uuid` keeps existing and stops being an identity. Two uses of one
    record, and the distinction is the point: this is *who asked*, while
    resolving a live **client** by it is the defect F48 names.
    Ruled 2026-09-19; docs/rulings-log.md R19.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "catalog.db")

    def _reopen(self, resolver=actor_for):
        db = SyncDB(self.path, actor_for=resolver)
        self.addCleanup(db.close)
        return db

    def _seed_preR19(self, uuid="login-a", content_server_id=SERVER):
        """A row written before the columns existed: it names its login and
        its content server, and nothing about the person."""
        db = SyncDB(self.path)
        row = _row("film", content_server_id=content_server_id)
        row["server_uuid"] = uuid
        db.upsert(row)
        db.close()

    def test_the_pair_is_backfilled_from_the_enqueuing_login(self):
        """Measured available before it was ruled: 14/14 rows in the live
        catalog resolve to a saved login (2026-09-19). So this is a durability
        change rather than a rescue."""
        self._seed_preR19()
        row = self._reopen().get("film")
        self.assertEqual(SERVER, row["requested_server_id"])
        self.assertEqual(ALICE, row["requested_user_id"])

    def test_a_login_that_no_longer_resolves_keeps_the_server_half(self):
        """The R18 shape, applied to downloads: the person is unrecoverable,
        the server is not -- it is on the row already. A download is a file on
        disk, so it is never dropped for being unattributable."""
        self._seed_preR19(uuid="login-deleted-and-recreated")
        row = self._reopen().get("film")
        self.assertEqual(SERVER, row["requested_server_id"],
                         "the server half was on the row and was thrown away")
        self.assertEqual(NO_ACTOR, row["requested_user_id"])

    def test_an_unhomed_row_records_no_account_at_all(self):
        """No content server means no account to have asked, and nowhere to
        file the answer if there were."""
        self._seed_preR19(uuid="login-deleted", content_server_id=None)
        row = self._reopen().get("film")
        self.assertEqual(NO_ACTOR, row["requested_server_id"])
        self.assertEqual(NO_ACTOR, row["requested_user_id"])

    def test_the_backfill_is_idempotent(self):
        """It runs on every open, so a second pass must not undo the first --
        and an unresolvable row must not be retried into a different answer."""
        self._seed_preR19(uuid="login-gone")
        first = self._reopen().get("film")
        again = self._reopen().get("film")
        self.assertEqual((first["requested_server_id"],
                          first["requested_user_id"]),
                         (again["requested_server_id"],
                          again["requested_user_id"]))

    def test_a_new_row_records_it_at_enqueue_without_a_backfill(self):
        """The point of writing it at enqueue rather than deriving it later:
        the derivation is what stops working when a connection is recreated."""
        from jellyfin_mpv_shim.sync.manager import SyncManager

        db = self._reopen()
        mgr = SyncManager()
        mgr.db = db
        mgr.root = self.dir
        with mock.patch("jellyfin_mpv_shim.sync.manager._actor_for",
                        side_effect=actor_for):
            ok = mgr._add_row("login-b", {"Id": "new", "Type": "Movie",
                                          "Name": "New", "ServerId": SERVER})
        self.assertTrue(ok, "_add_row refused a DTO that names its server")
        row = db.get("new")
        self.assertEqual((SERVER, BOB), (row["requested_server_id"],
                                         row["requested_user_id"]))

    def test_an_enqueue_whose_login_cannot_be_placed_still_writes_the_row(self):
        """A download is a file on disk. Refusing to enqueue because the person
        cannot be named would be a worse failure than not naming them."""
        from jellyfin_mpv_shim.sync.manager import SyncManager

        db = self._reopen()
        mgr = SyncManager()
        mgr.db = db
        mgr.root = self.dir
        with mock.patch("jellyfin_mpv_shim.sync.manager._actor_for",
                        side_effect=lambda uuid: None):
            ok = mgr._add_row("login-unknown",
                              {"Id": "new", "Type": "Movie", "Name": "New",
                               "ServerId": SERVER})
        self.assertTrue(ok)
        row = db.get("new")
        self.assertEqual(SERVER, row["requested_server_id"],
                         "the server is on the DTO; only the person is unknown")
        self.assertEqual(NO_ACTOR, row["requested_user_id"])

    def test_a_row_upserted_without_the_pair_self_heals_on_the_next_open(self):
        """`upsert` writes every column in COLUMNS and passes None for anything
        the caller omitted -- so a writer that does not know the account (the
        orphan adopter, whose item comes off the disk with no login) leaves the
        DDL default rather than NULL.

        That default is `''`, which is exactly what the backfill selects on, so
        such a row is repaired on the next open instead of staying blank
        forever. Pinned because it is the interaction, not either half.

        **The `''` rather than a NOT NULL failure is `INSERT OR REPLACE`**:
        SQLite resolves a NOT NULL violation under REPLACE by substituting the
        column default. A plain INSERT here would raise on every writer that
        omits the pair.
        """
        db = SyncDB(self.path)
        row = _row("film")
        row["server_uuid"] = "login-a"
        db.upsert(row)
        self.assertEqual("", db.get("film")["requested_user_id"],
                         "the default was not applied; NOT NULL would have "
                         "raised without INSERT OR REPLACE")
        db.close()
        healed = self._reopen().get("film")
        self.assertEqual((SERVER, ALICE), (healed["requested_server_id"],
                                           healed["requested_user_id"]))

    def test_the_recorded_server_never_disagrees_with_the_rows_own(self):
        """The invariant that keeps the pair from becoming a second source of
        truth. You can only download from a server you are signed in to, and
        the item belongs to that server, so these two are the same fact twice
        -- which is worth having as one comparison for the actor helpers, and
        worth checking rather than trusting.

        **The fixture is the test.** This used to seed a row whose content
        server and whose credential `Id` were the same value, so it passed
        whichever of the two the backfill preferred -- it could not fail, and
        the backfill was in fact preferring the credential. The two are given
        different values here, and where they differ the row's own is the
        answer: `_add_row` takes the server half off the DTO and says so, and
        a credential `Id` can differ from the content server its rows were
        written with (a regenerated ServerId, or a credential filed under
        another profile).

        What it costs when it is wrong: `_refresh_userdata` groups by
        `content_server_id` and narrows with `asked_by == actor`, whose server
        half is always the content server -- so a row backfilled off the
        credential can never match, and is swept only if the account is in the
        auto-download allow-list or the pair is unattributed. A silently
        unswept subset of the catalog, which is the R16/R21 failure the
        narrowing was written to avoid, reached from the other side.
        """
        self._seed_preR19(content_server_id=OTHER)   # login-a is on SERVER
        db = self._reopen()
        row = db.get("film")
        self.assertEqual(OTHER, row["content_server_id"],
                         "the fixture no longer poses the question")
        self.assertEqual(row["content_server_id"], row["requested_server_id"],
                         "the server half came off the credential, not off "
                         "the row")
        self.assertEqual(ALICE, row["requested_user_id"],
                         "the person half is the credential's, and stays so")


class NoActorSentinelTest(unittest.TestCase):
    """Progress recorded when nobody can be named.

    [iw]'s ruling: record it locally so resume works, never queue it,
    because there is no account to sync it as. That needs a representable
    "no actor" -- and it must not be NULL, because `NULL = NULL` is false in
    SQL and a NULL inside the primary key would insert a duplicate row on
    every write, which is the bug already sitting in `pending_playstate`.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "status": STATUS_COMPLETE})

    def test_the_sentinel_is_not_null_and_not_empty(self):
        """Pinned as a value, because "" is falsy and half this codebase
        tests truthiness."""
        self.assertIsNotNone(NO_ACTOR)
        self.assertTrue(NO_ACTOR)

    def test_the_sentinel_cannot_be_a_jellyfin_user_id(self):
        """Jellyfin ids are 32 hex characters, so a sentinel outside that
        alphabet can never collide with a real person."""
        self.assertFalse(all(c in "0123456789abcdefABCDEF" for c in NO_ACTOR))

    def test_writing_twice_with_no_actor_keeps_one_row(self):
        for ticks in (10, 20):
            self.db.set_userdata("film", actor=(SERVER, NO_ACTOR),
                                 position_ticks=ticks)
        self.assertEqual([(SERVER, NO_ACTOR)],
                         self.db.userdata_actors("film"))
        self.assertEqual(20, self.db.userdata(
            "film", actor=(SERVER, NO_ACTOR))["position_ticks"])

    def test_unattributed_progress_is_not_another_actors_progress(self):
        self.db.set_userdata("film", actor=(SERVER, NO_ACTOR),
                             position_ticks=20)
        self.assertEqual(0, self.db.userdata(
            "film", actor=(SERVER, ALICE))["position_ticks"])


class PartialWriteTest(unittest.TestCase):
    """A write touches only the fields it names."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "status": STATUS_COMPLETE, "runtime_ticks": 10**10})

    def test_a_position_report_does_not_blank_the_watched_flag(self):
        """The reason this is an upsert and not INSERT OR REPLACE: replace
        blanks every column the call did not name, so the position report
        that lands after a watched mark would un-watch the item."""
        self.db.set_userdata("film", actor=(SERVER, ALICE),
                             played=True)
        self.db.set_userdata("film", actor=(SERVER, ALICE),
                             position_ticks=42)
        got = self.db.userdata("film", actor=(SERVER, ALICE))
        self.assertTrue(got["played"], "the position report un-watched it")
        self.assertEqual(42, got["position_ticks"])

    def test_mark_unplayed_is_verbatim(self):
        """`set_watched` is the deliberate-choice path, so it must be able
        to move backwards -- Mark unplayed is the only signal in the app
        authoritative in both directions."""
        self.db.set_watched("film", True, actor=(SERVER, ALICE))
        self.db.set_watched("film", False, actor=(SERVER, ALICE))
        self.assertFalse(
            self.db.userdata("film", actor=(SERVER, ALICE))["played"])

    def test_playback_only_ever_moves_forward(self):
        """`advance_userdata` is the playback path. Progress arrives out of
        order -- a stop report can land after the sweep that recorded the
        finish -- and retreating a resume position is the error a person
        notices immediately."""
        self.db.update_userdata("film", actor=(SERVER, ALICE),
                                position_ticks=900)
        self.db.update_userdata("film", actor=(SERVER, ALICE),
                                position_ticks=100)
        self.assertEqual(900, self.db.userdata(
            "film", actor=(SERVER, ALICE))["position_ticks"])

    def test_watched_sticks_through_playback(self):
        self.db.update_userdata("film", actor=(SERVER, ALICE),
                                played=True)
        self.db.update_userdata("film", actor=(SERVER, ALICE),
                                position_ticks=10)
        self.assertTrue(self.db.userdata(
            "film", actor=(SERVER, ALICE))["played"])


class TheRowDecidesItsServerTest(unittest.TestCase):
    """Two things could say which server a row's state belongs to, and they
    can disagree.

    The row's own `content_server_id`, and whatever server the writing
    login happens to be on -- a watched mark fanned out over a series that
    spans servers, or a push whose payload names one while the row belongs
    to another. Written under one key and read back under the other, the
    mark lands and is invisible, which is the worst shape: no error, no
    missing write, just a tick that never appears.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "status": STATUS_COMPLETE})

    def test_a_write_naming_the_wrong_server_still_lands_where_it_is_read(self):
        self.db.set_watched("film", True, actor=(SERVER, ALICE))
        self.assertTrue(
            self.db.userdata("film", actor=(SERVER, ALICE))["played"],
            "the mark was filed under a server the row is not on")

    def test_and_it_is_stored_under_the_row_s_server(self):
        self.db.set_watched("film", True, actor=(SERVER, ALICE))
        self.assertEqual([(SERVER, ALICE)], self.db.userdata_actors("film"))

    def test_playback_progress_too(self):
        self.db.update_userdata("film", actor=(SERVER, ALICE),
                                position_ticks=42)
        self.assertEqual(42, self.db.userdata(
            "film", actor=(SERVER, ALICE))["position_ticks"])

    def test_a_reading_cursor_too(self):
        self.db.set_reading_position("film", 7, actor=(SERVER, ALICE))
        self.assertEqual(7, self.db.userdata(
            "film", actor=(SERVER, ALICE))["position_ticks"])

    def test_a_row_with_no_server_files_under_the_sentinel_on_both_halves(self):
        """An orphan is a local file, and both halves of its key say so.

        This used to expect `(@none, ALICE)` -- the row's server unknown but
        a real person on the other half -- and that pairing *was the defect*:
        no reader could ever name it, because a reader with no server cannot
        name the person either, so it looked under `(@none, @none)`. The mark
        landed, was invisible, and reported success.

        [iw]'s ruling is one machine-wide bucket: no server means no account,
        so the honest key has the sentinel on both halves. Re-derived from
        that rather than edited to match the code.
        docs/offline-sync.md section 1.
        """
        self.db.upsert({"item_id": "odd", "status": STATUS_COMPLETE})
        self.db.set_watched("odd", True, actor=(SERVER, ALICE))
        self.assertEqual([(NO_ACTOR, NO_ACTOR)],
                         self.db.userdata_actors("odd"))
        # Every actor reads the one bucket, which is what makes it visible.
        for who in ((SERVER, ALICE), (OTHER, BOB), None):
            self.assertTrue(self.db.userdata("odd", actor=who)["played"])


class AggregateReadsTest(unittest.TestCase):
    """Deletion stays machine-global, by [iw]'s ruling: "any actor".

    One file, so one decision. The named future work is a claim system
    recording who *requested* each download, which is a third key again.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "status": STATUS_COMPLETE})

    def test_nobody_has_played_it_yet(self):
        self.assertFalse(self.db.played_by_anyone("film"))

    def test_one_actor_is_enough(self):
        self.db.set_userdata("film", actor=(SERVER, BOB),
                             played=True)
        self.assertTrue(self.db.played_by_anyone("film"))

    def test_an_unattributed_viewing_counts_too(self):
        """It is still a viewing on this machine, and the rule is any."""
        self.db.set_userdata("film", actor=(SERVER, NO_ACTOR),
                             played=True)
        self.assertTrue(self.db.played_by_anyone("film"))

    def test_a_position_alone_is_not_watched(self):
        self.db.set_userdata("film", actor=(SERVER, BOB),
                             position_ticks=500)
        self.assertFalse(self.db.played_by_anyone("film"))


class TheWatcherOwnsTheProgressTest(unittest.TestCase):
    """D4, and the reason the whole table exists.

    Alice downloads a film and watches half of it. Bob, on the same machine
    and the same server, opens it. Bob must start at the beginning, and what
    Bob watches must be filed under Bob -- because the next thing that
    happens is that Bob's progress is pushed to Bob's Jellyfin account.

    Before the split there was one blob per item, so Bob resumed at Alice's
    fifty minutes and then told his own server he had watched them.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        from jellyfin_mpv_shim.sync.manager import SyncManager
        from jellyfin_mpv_shim.users import userManager
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.mgr = SyncManager()
        self.mgr.db = self.db
        self.mgr.root = self.dir
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "server_uuid": "login-a", "status": STATUS_COMPLETE,
                        "runtime_ticks": 100 * 10 ** 7})
        patch = mock.patch.object(userManager, "users", [{
            "id": "local", "credentials": [
                {"uuid": "login-a", "Id": SERVER, "UserId": ALICE},
                {"uuid": "login-b", "Id": SERVER, "UserId": BOB},
            ]}])
        self.addCleanup(patch.stop)
        patch.start()

    def test_progress_is_filed_under_whoever_is_playing(self):
        self.mgr.mirror_playstate("film", 50 * 10 ** 7,
                                  server_uuid="login-b")
        self.assertEqual(50 * 10 ** 7, self.db.userdata(
            "film", actor=(SERVER, BOB))["position_ticks"])

    def test_and_not_under_whoever_downloaded_it(self):
        self.mgr.mirror_playstate("film", 50 * 10 ** 7,
                                  server_uuid="login-b")
        self.assertEqual(0, self.db.userdata(
            "film", actor=(SERVER, ALICE))["position_ticks"],
            "the downloader was credited with somebody else's viewing")

    def test_one_persons_finish_does_not_watch_it_for_the_other(self):
        self.mgr.mirror_playstate("film", None, played=True,
                                  server_uuid="login-a")
        self.assertTrue(self.db.userdata("film", actor=(SERVER, ALICE))["played"])
        self.assertFalse(self.db.userdata("film", actor=(SERVER, BOB))["played"])

    def test_but_the_shared_copy_still_counts_as_watched_for_deletion(self):
        """The deliberate asymmetry. Watched state is per person; the file
        on disk is not, and [iw]'s ruling is that any actor finishing it
        makes it reapable."""
        self.mgr.mirror_playstate("film", None, played=True,
                                  server_uuid="login-a")
        self.assertTrue(self.db.played_by_anyone("film"))

    def test_a_mark_is_filed_under_whoever_made_it(self):
        self.mgr.mirror_watched("film", True, server_uuid="login-b")
        self.assertTrue(self.db.userdata("film", actor=(SERVER, BOB))["played"])
        self.assertFalse(self.db.userdata("film", actor=(SERVER, ALICE))["played"])


class TheReplayQueueIsActorKeyedTest(unittest.TestCase):
    """What we owe each server, and on whose behalf.

    `pending_playstate` is the list of changes a server has not been told
    about. It was keyed on the saved login that happened to be current when
    the change was made, which is wrong twice: a login is a delivery handle
    rather than a person, and `NULL = NULL` is false in SQL, so a row queued
    with no login inserted a fresh duplicate on every single write.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "status": STATUS_COMPLETE})

    def test_one_row_per_item_and_actor(self):
        for ticks in (10, 20):
            self.db.upsert_playstate("film", actor=(SERVER, ALICE),
                                     position_ticks=ticks)
        rows = self.db.list_playstate()
        self.assertEqual(1, len(rows))
        self.assertEqual(20, rows[0]["position_ticks"])

    def test_two_people_owe_the_server_separately(self):
        self.db.upsert_playstate("film", actor=(SERVER, ALICE),
                                 position_ticks=10)
        self.db.upsert_playstate("film", actor=(SERVER, BOB),
                                 played=True)
        self.assertEqual(
            {(ALICE, 10, None), (BOB, None, 1)},
            {(r["user_id"], r["position_ticks"], r["played"])
             for r in self.db.list_playstate()})

    def test_the_position_still_only_advances(self):
        self.db.upsert_playstate("film", actor=(SERVER, ALICE),
                                 position_ticks=900)
        self.db.upsert_playstate("film", actor=(SERVER, ALICE),
                                 position_ticks=100)
        self.assertEqual(900, self.db.list_playstate()[0]["position_ticks"])

    def test_unattributed_progress_is_never_queued(self):
        """[iw]'s ruling: record it locally so resume works, never queue it,
        because there is no account to send it as. Refusing at the queue is
        what makes that true -- an entry nobody can be named for could never
        be drained, so it would sit in the table forever."""
        for ticks in (10, 20, 30):
            self.db.upsert_playstate("film", actor=(SERVER, NO_ACTOR), position_ticks=ticks)
        self.assertEqual([], self.db.list_playstate())

    def test_and_that_is_what_used_to_grow_without_bound(self):
        """The old shape, pinned as the thing not to go back to: keyed on a
        NULL login, every write inserted rather than updating, because
        `NULL = NULL` is false in SQL."""
        for _ in range(3):
            self.db.upsert_playstate("film", actor=(None, None),
                                     position_ticks=10)
        self.assertEqual([], self.db.list_playstate())


class TheReplayDrainsThroughAnyOfTheirLoginsTest(unittest.TestCase):
    """A person can hold several logins for one server.

    A LAN address and a remote one are two, and the client that comes back
    is often not the one they were signed in as offline. Keyed on the login
    that queued it, such an entry stayed pending with a perfectly good route
    open beside it.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        from jellyfin_mpv_shim.sync.manager import SyncManager
        from jellyfin_mpv_shim.users import userManager
        self.db = SyncDB(os.path.join(self.dir, "catalog.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert({"item_id": "film", "content_server_id": SERVER,
                        "status": STATUS_COMPLETE})
        self.mgr = SyncManager()
        self.mgr.db = self.db
        # Alice, twice: the same account on the same server, reached at two
        # addresses. Two saved logins, one person.
        patch = mock.patch.object(userManager, "users", [{
            "id": "local", "credentials": [
                {"uuid": "alice-lan", "Id": SERVER, "UserId": ALICE},
                {"uuid": "alice-wan", "Id": SERVER, "UserId": ALICE},
                {"uuid": "bob-lan", "Id": SERVER, "UserId": BOB},
            ]}])
        self.addCleanup(patch.stop)
        patch.start()

    def _online(self, *uuids):
        clients = {u: object() for u in uuids}
        self.mgr.get_clients = lambda: dict(clients)
        return clients

    def test_the_other_address_can_deliver_it(self):
        clients = self._online("alice-wan")
        self.assertIs(clients["alice-wan"],
                      self.mgr._client_for_actor(SERVER, ALICE),
                      "queued on the LAN, reconnected over the internet, "
                      "and the entry had nowhere to go")

    def test_a_different_person_cannot(self):
        self._online("bob-lan")
        self.assertIsNone(self.mgr._client_for_actor(SERVER, ALICE))

    def test_nobody_signed_in_is_not_a_route(self):
        self._online()
        self.assertIsNone(self.mgr._client_for_actor(SERVER, ALICE))

    def test_the_unattributed_sentinel_is_never_a_route(self):
        """It cannot be: there is no account behind it. Guarded here as
        well as at the queue, because a route for it would resurrect the
        entries the queue refuses."""
        self._online("alice-lan", "bob-lan")
        self.assertIsNone(self.mgr._client_for_actor(SERVER, NO_ACTOR))
        self.assertIsNone(self.mgr._client_for_actor(NO_ACTOR, NO_ACTOR))


class TheReplayQueueMigrationTest(unittest.TestCase):
    """Entries queued before the queue knew about people."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "catalog.db")

    #: `pending_playstate` before it knew about people. Written out rather
    #: than made with SyncDB, which would create today's shape: the bug this
    #: pins is that opening an EXISTING catalog ran a CREATE INDEX over
    #: columns the migration had not added yet, and a fresh catalog cannot
    #: show it.
    _OLD_QUEUE = """
    CREATE TABLE pending_playstate (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_uuid TEXT,
        item_id TEXT,
        position_ticks INTEGER,
        played INTEGER,
        created_at INTEGER
    );
    """

    def test_a_catalog_predating_the_actor_columns_still_opens(self):
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.executescript(self._OLD_QUEUE)
        conn.execute(
            "INSERT INTO pending_playstate (server_uuid, item_id, "
            "position_ticks, played, created_at) VALUES (?,?,?,?,?)",
            ("login-a", "film", 1234, 1, 0))
        conn.commit()
        conn.close()
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertTrue(db.healthy())
        rows = db.list_playstate()
        self.assertEqual([(SERVER, ALICE)],
                         [(r["server_id"], r["user_id"]) for r in rows])

    def test_the_duplicates_the_old_key_produced_are_folded(self):
        """The old key allowed NULL and `NULL = NULL` is false, so a write
        inserted instead of advancing. A catalog that ran that build carries
        one row per report -- and they all collide on the new unique key, so
        folding them is not tidiness, it is what lets the catalog open."""
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.executescript(self._OLD_QUEUE)
        for pos, played in ((500, None), (900, 1), (700, None)):
            conn.execute(
                "INSERT INTO pending_playstate (server_uuid, item_id, "
                "position_ticks, played, created_at) VALUES (?,?,?,?,?)",
                ("login-a", "film", pos, played, 0))
        conn.commit()
        conn.close()
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        rows = db.list_playstate()
        self.assertEqual(1, len(rows), "the duplicates were not folded")
        self.assertEqual(900, rows[0]["position_ticks"],
                         "folding lost the furthest position")
        self.assertTrue(rows[0]["played"], "folding lost a watched mark")

    def _legacy(self, server_uuid):
        db = SyncDB(self.path)
        db._conn.execute(
            "INSERT INTO pending_playstate (server_uuid, item_id, "
            "position_ticks, played, created_at) VALUES (?,?,?,?,?)",
            (server_uuid, "film", 1234, 1, 0))
        db._conn.commit()
        db.close()

    def test_an_entry_whose_login_we_know_is_carried_over(self):
        self._legacy("login-a")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        rows = db.list_playstate()
        self.assertEqual(1, len(rows))
        self.assertEqual((SERVER, ALICE),
                         (rows[0]["server_id"], rows[0]["user_id"]))
        self.assertEqual(1234, rows[0]["position_ticks"])

    def _held(self, item_id="film", server_id=SERVER):
        """A download row for the item a queued entry is about.

        R18 recovers the entry's **server** half from it, so a fixture without
        one cannot show the recovery -- and every realistic case has one: you
        watched a downloaded episode offline.
        """
        db = SyncDB(self.path)
        db.upsert(dict({c: None for c in COLUMNS},
                       item_id=item_id, content_server_id=server_id,
                       status=STATUS_COMPLETE, type="Episode", name=item_id))
        db.close()

    def test_an_entry_we_cannot_attribute_keeps_its_local_half(self):
        """**R18, and it reverses what this test used to assert.** It said the
        entry was dropped because "it could never be sent" -- true, and not the
        whole of what it is worth. Measured on a real catalog: the delete took
        a local watched mark whose file is still on disk.

        So the viewing survives, with its server recovered from the item's own
        download row and its person left as the sentinel: "watched" on screen,
        `played_by_anyone` and the reaper's grace period all read it, and
        nothing sends it.

        **Where it survives changed on 2026-09-19**, and the assertions moved
        with it: R18 kept the row in `pending_playstate`, which none of the
        three readers it named consults, so the mark lives in `item_userdata`
        now. The property is R18's; the table is not.
        """
        self._held()
        self._legacy(None)
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertTrue(db.played_by_anyone("film"),
                        "the local watched mark was lost")
        self.assertEqual([(SERVER, NO_ACTOR)], db.userdata_actors("film"),
                         "the server half was not recovered from the row, or "
                         "the person half named somebody")
        self.assertEqual(
            1234,
            db.userdata("film", actor=(SERVER, NO_ACTOR))["position_ticks"])

    def test_a_removed_server_s_entry_keeps_it_too(self):
        """The case R18 was ruled on: you watched an episode offline, then
        removed that server from the app and added it back."""
        self._held()
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertEqual([(SERVER, NO_ACTOR)], db.userdata_actors("film"))
        self.assertTrue(db.played_by_anyone("film"))

    def test_and_no_named_account_reads_it(self):
        """What "local only" means, and the half R15 and R2 both turn on: the
        bucket is not an answer to a *named* server asking who watched this.
        """
        self._held()
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertFalse(db.userdata("film", actor=(SERVER, ALICE))["played"],
                         "a named account read the anonymous mark")

    def test_the_local_mark_is_where_the_local_readers_look(self):
        """**R18's local half lives in `item_userdata`** -- ruled 2026-09-19,
        superseding the mechanism R18 itself described.

        `docs/offline-sync.md` section 3a named three local readers for the
        kept entry -- "watched" on screen, `played_by_anyone`, the reaper's
        grace period. All three read `item_userdata`, and nothing outside
        `sync/db.py` reads `pending_playstate` at all, so an entry kept in
        the queue was worth exactly nothing locally -- which is the one thing
        R18 said it was worth.
        """
        self._held()
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertTrue(db.played_by_anyone("film"),
                        "the local watched mark is not where the readers "
                        "look for it")
        self.assertEqual([(SERVER, NO_ACTOR)], db.userdata_actors("film"),
                         "the server half was not recovered onto the key")

    def test_and_the_startup_prune_does_not_take_it(self):
        """The half the keep never survived. `manager._open_and_run` runs
        `drop_unsyncable_playstate()` on every launch, and for an orphan row
        it deleted the kept entry on the `content_server_id IS NULL` arm --
        three statements after the migration logged that it had kept it.

        An orphan row on purpose: that is the arm that deletes. A missing row
        is not, by `drop_unsyncable_playstate`'s own rule.
        """
        self._held("film", None)
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertTrue(db.played_by_anyone("film"))
        db.drop_unsyncable_playstate()
        self.assertTrue(db.played_by_anyone("film"),
                        "the startup prune took the local mark with it")

    def test_and_nothing_unsendable_is_left_in_the_queue(self):
        """The other half of the ruling. The queue is advance-only and drains
        oldest-first, so an entry that can never go does not wait -- it
        blocks the accounting and reappears in every log line forever
        (`drop_unsyncable_playstate`'s own docstring).
        """
        self._held()
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertEqual([], db.list_playstate(),
                         "an entry nobody can ever send is still queued")

    def test_the_position_comes_across_with_the_watched_mark(self):
        """Not just the flag: the entry carried a resume point, and losing it
        is the same loss in a quieter spelling."""
        self._held()
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertEqual(
            1234,
            db.userdata("film", actor=(SERVER, NO_ACTOR))["position_ticks"],
            "the queued resume point did not come across")

    def test_the_server_comes_from_this_item_s_row_and_not_another_s(self):
        """A catalog with one download row cannot show the difference, and the
        mutation round said so: reading *any* row's server passed every check
        above. Two items on two servers is what makes the join observable --
        and **the other item is inserted first on purpose**, so an unfiltered
        read returns its server rather than stumbling onto the right one (a
        table scan is rowid order, which is insertion order here)."""
        self._held("other-film", OTHER)
        self._held("film", SERVER)
        self._legacy("login-gone")          # about "film"
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertEqual([(SERVER, NO_ACTOR)], db.userdata_actors("film"),
                         "the viewing took another item's server")
        self.assertEqual([], db.userdata_actors("other-film"),
                         "it landed on the wrong item entirely")

    def test_an_entry_about_an_item_we_do_not_hold_is_kept_unnamed(self):
        """No download row, so there is no server to recover -- and it is
        **still** kept. Deciding deliverability is
        `drop_unsyncable_playstate`'s job, not this migration's, and "a
        missing row is not a third kind" is that function's own rule.
        """
        self._legacy("login-gone")
        db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        self.assertEqual([(NO_ACTOR, NO_ACTOR)], db.userdata_actors("film"),
                         "no server means no account, so both halves are the "
                         "sentinel -- one machine-wide bucket")
        self.assertTrue(db.played_by_anyone("film"))

    def test_the_fold_considers_the_rows_it_did_not_select(self):
        """The fold merges among the rows it **selected** (`user_id = ''`) and
        never against a row already carrying the pair it is about to write, so
        its UPDATE can produce a duplicate the unique index then refuses.

        Narrowed twice while sorting, and both narrowings are in the fixture:
        on a **first** open the index does not exist yet, so the statement
        that raises is the `CREATE UNIQUE INDEX` three statements later in the
        same pass -- which is why this seeds a catalog the migration has never
        run on; and the loss is permanent only for a session that never gets
        the server back, because the surviving row is deliverable.

        **The `assertNoLogs` is the point of the test, not decoration.** Every
        pass of `_migrate` shares one transaction, so this rollback also takes
        `_migrate_playlist_scopes` and `_migrate_drop_vestigial` with it, on
        this open and on every later one -- and nothing above DEBUG reaches
        the UI. A test asserting only that the rows are right would pass on a
        build that swallowed the failure.
        """
        self._held()
        db = SyncDB(self.path)        # no resolver: the migration is a no-op,
        db.upsert_playstate("film", actor=(SERVER, ALICE),  # and no index yet
                            position_ticks=500, played=True)
        db._conn.execute(
            "INSERT INTO pending_playstate (server_uuid, item_id, "
            "position_ticks, played, created_at) VALUES (?,?,?,?,?)",
            ("login-a", "film", 1234, 1, 0))
        db._conn.commit()
        db.close()

        # `sync.db`, not the dotted package path: this tree's loggers are
        # named from the package-relative module, and watching the wrong one
        # makes `assertNoLogs` pass on the failure it exists to catch.
        with self.assertNoLogs("sync.db", level="ERROR"):
            db = SyncDB(self.path, actor_for=actor_for)
        self.addCleanup(db.close)
        rows = db.list_playstate()
        self.assertEqual(1, len(rows),
                         "the legacy entry and the row it resolves to are one "
                         "actor's queue entry for one item")
        self.assertEqual((SERVER, ALICE),
                         (rows[0]["server_id"], rows[0]["user_id"]))
        self.assertEqual(1234, rows[0]["position_ticks"],
                         "the merge lost the furthest position")

    def test_without_a_resolver_nothing_is_dropped(self):
        """A handle that cannot ask who somebody is has not been told
        nobody -- same rule as the userdata migration."""
        self._legacy("login-a")
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(1, len(db.list_playstate()))


class TheResolverAnswersForTheActingPersonTest(unittest.TestCase):
    """``SyncManager.actor_of``: which of three possible answers wins.

    The ordering was only ever pinned by cases where the acting login and
    the active profile named the SAME person, so "try the server before the
    login" survived every one of them. These say which is right, because
    that is the thing worth writing down.

    ALICE is the person at the keyboard (the active local profile). BOB is a
    second saved login on the same server -- the shape a shared machine has,
    and the one where a login read off a catalog row wins over the person
    actually watching.
    """

    def setUp(self):
        from jellyfin_mpv_shim.users import userManager
        for attr, value in (
                ("users", [
                    {"id": "alice", "credentials": [
                        {"uuid": "login-a", "Id": SERVER, "UserId": ALICE},
                        {"uuid": "login-far", "Id": OTHER, "UserId": ALICE}]},
                    {"id": "bob", "credentials": [
                        {"uuid": "login-b", "Id": SERVER, "UserId": BOB}]}]),
                ("active_id", "alice")):
            patch = mock.patch.object(userManager, attr, value)
            self.addCleanup(patch.stop)
            patch.start()

    @staticmethod
    def _actor_of(**kw):
        from jellyfin_mpv_shim.sync.manager import SyncManager
        return SyncManager.actor_of(**kw)

    def test_an_announced_pair_wins_outright(self):
        """The websocket already names both halves; nothing may second-guess
        a server that has told us whose change this is."""
        self.assertEqual(self._actor_of(acting_login="login-a",
                                        server_id=OTHER, user_id=BOB),
                         (OTHER, BOB))

    def test_the_acting_login_outranks_the_active_profile(self):
        """BOB is signed in and doing this; ALICE is merely the profile the
        machine is unlocked as. The act belongs to whoever performed it."""
        self.assertEqual(self._actor_of(acting_login="login-b",
                                        server_id=SERVER),
                         (SERVER, BOB))

    def test_with_no_acting_login_the_active_profile_answers(self):
        """The offline case: no client, so nobody to ask, and the person at
        the keyboard is whoever this profile is."""
        self.assertEqual(self._actor_of(server_id=SERVER), (SERVER, ALICE))

    def test_the_pseudo_server_names_nobody_and_falls_through(self):
        """What the downloads screen passes. It is not a saved login, so it
        resolves to no credential -- and the row's server is then what finds
        the person. No special case for it anywhere."""
        self.assertEqual(self._actor_of(acting_login="offline",
                                        server_id=SERVER),
                         (SERVER, ALICE))

    def test_a_login_on_another_server_answers_as_itself(self):
        """Superseding this module's own earlier answer, and the reason is
        worth keeping.

        `login-far` is a real credential of a real person on a DIFFERENT
        server. The previous round made the resolver *pass it over* and
        substitute the active profile's account on `server_id` -- and its
        docstring described the bug correctly: a pair that exists nowhere.
        But the substitute pair **matches the row**, so the store's check
        accepts it and the mark lands on a film that account has never
        opened. The record became consistent and more wrong.

        So the resolver answers as itself and the refusal happens in the
        store, which is the only place that knows the row.
        `TheActingIdentityReachesTheStoreTest` asserts the other end.
        """
        self.assertEqual(self._actor_of(acting_login="login-far",
                                        server_id=SERVER),
                         (OTHER, ALICE))

    def test_and_still_answers_for_its_own_server(self):
        """The symmetric direction: the answer does not depend on which
        server was asked about, only on who is acting."""
        self.assertEqual(self._actor_of(acting_login="login-far",
                                        server_id=OTHER),
                         (OTHER, ALICE))

    def test_an_unknown_login_with_no_server_names_nobody(self):
        """NO_ACTOR rather than a guess: recorded locally, never queued."""
        self.assertEqual(self._actor_of(acting_login="login-gone"),
                         (NO_ACTOR, NO_ACTOR))

    def test_a_server_nobody_has_an_account_on_names_nobody(self):
        self.assertEqual(self._actor_of(server_id="srv-unknown"),
                         ("srv-unknown", NO_ACTOR))

    def test_a_row_login_cannot_be_passed_positionally(self):
        """The rename is the enforcement, and keyword-only is what makes it
        one: every stale site had to say `acting_login=` out loud."""
        with self.assertRaises(TypeError):
            self._actor_of_positional("login-b")

    @staticmethod
    def _actor_of_positional(value):
        from jellyfin_mpv_shim.sync.manager import SyncManager
        return SyncManager.actor_of(value)

if __name__ == "__main__":
    unittest.main()


class RowSyncStateMatrixTest(unittest.TestCase):
    """The whole of C1, as a table, because the recurring defect here is a
    right rule applied at N-1 of N sites and a table cannot have N-1 rows.

    Three states the row can be in crossed with three the actor can be in.
    Each cell states the expected verdict, **the exact physical
    `item_userdata` key**, and whether the queue may be written -- not "the
    write and the read agree", which is internal coherence and cannot tell a
    consistent wrong answer from a right one.

    docs/offline-sync.md section 1.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._n = 0
        self.db = self._fresh()

    def _fresh(self):
        """A catalog per case, not per class.

        Three of the cases resolve to the *same* physical key -- every actor
        against an orphan files under `(@none, @none)`, which is the ruling.
        Sharing one catalog, the second and third then wrote nothing new and
        a delta-based assertion passed while measuring nothing.
        """
        self._n += 1
        db = SyncDB(os.path.join(self.tmp, "c%d.db" % self._n))
        self.addCleanup(db.close)
        db.upsert(_row("homed", content_server_id=SERVER))
        db.upsert(_row("orphan", content_server_id=None))
        return db

    #: (item, actor) -> (state, expected key or None, queue allowed)
    CASES = [
        # A row whose server we know.
        ("homed", (SERVER, ALICE), "sync", (SERVER, ALICE), True),
        ("homed", (OTHER, BOB), "refuse", None, False),
        ("homed", (SERVER, None), "local", (SERVER, NO_ACTOR), False),
        ("homed", None, "local", (SERVER, NO_ACTOR), False),
        # A row whose server we do not. It is a local file: it plays, it
        # records locally, it never syncs -- [iw]'s ruling, 11c.
        ("orphan", (SERVER, ALICE), "local", (NO_ACTOR, NO_ACTOR), False),
        ("orphan", (OTHER, BOB), "local", (NO_ACTOR, NO_ACTOR), False),
        ("orphan", None, "local", (NO_ACTOR, NO_ACTOR), False),
        # No row at all. Distinct from an orphan, and that is the point:
        # `_server_of` answers NO_ACTOR for both, so before this an item the
        # catalog had never held got userdata filed under the orphan key.
        ("missing", (SERVER, ALICE), "no_row", None, False),
        ("missing", None, "no_row", None, False),
    ]

    def test_the_verdict_and_the_physical_key(self):
        for item_id, actor, want_state, want_key, _queue in self.CASES:
            with self.subTest(item=item_id, actor=actor):
                state, key = self.db._row_sync_state(item_id, actor)
                self.assertEqual(state, want_state)
                self.assertEqual(key, want_key)

    def test_a_write_lands_on_exactly_the_key_the_verdict_names(self):
        """The verdict is not advice. Whatever it says the key is, that is
        where the row physically appears -- and where nothing appears when
        it says there is no key."""
        for item_id, actor, _state, want_key, _queue in self.CASES:
            with self.subTest(item=item_id, actor=actor):
                db = self._fresh()
                db.set_userdata(item_id, actor=actor, played=True)
                written = set(db.all_userdata())
                if want_key is None:
                    self.assertEqual(written, set(),
                                     "a refused or row-less write must "
                                     "write nothing at all")
                else:
                    self.assertEqual(
                        written, {(item_id,) + want_key},
                        "the write must land on the key the verdict names, "
                        "and on no other")

    def test_the_queue_is_written_only_where_the_verdict_allows_it(self):
        for item_id, actor, _state, _key, queue_ok in self.CASES:
            with self.subTest(item=item_id, actor=actor):
                db = self._fresh()
                db.upsert_playstate(item_id, actor=actor, played=True)
                self.assertEqual(
                    bool(db.list_playstate()), queue_ok,
                    "an orphan and a row we do not hold must never be "
                    "queued for a server; there is no account to send as")

    def test_a_read_looks_where_the_write_landed(self):
        """The half that was actually broken: a mark written under
        `(@none, a real person)` and read back under `(@none, @none)` lands,
        is invisible, and reports success."""
        for item_id, actor, _state, want_key, _queue in self.CASES:
            with self.subTest(item=item_id, actor=actor):
                db = self._fresh()
                db.set_userdata(item_id, actor=actor, played=True)
                got = db.userdata(item_id, actor=actor)
                self.assertEqual(bool(got["played"]), want_key is not None)


class TheActingIdentityReachesTheStoreTest(unittest.TestCase):
    """`actor_of` must not normalise a mismatch away before the store can
    see it.

    The store's check compares the acting actor's server against the row's.
    But `actor_of(acting_login=B, server_id=A)` used to pass B over and fall
    to "the active profile's account on A" -- which *matches* A, so the check
    accepted and the write landed on A's row anyway. A check cannot catch a
    mismatch that was resolved away one frame up the stack; this is the half
    the first draft of the plan got wrong.

    The substitution itself is right and stays: with **no** acting login --
    offline, where the browser browses a pseudo-server -- the person at the
    keyboard is the active profile, and that is the best available answer.
    What must not happen is substituting over a login that *did* answer.
    """

    def setUp(self):
        from jellyfin_mpv_shim.sync.manager import SyncManager
        from jellyfin_mpv_shim.users import userManager
        self.actor_of = SyncManager.actor_of
        for attr, value in (
                ("users", [{"id": "local", "credentials": [
                    {"uuid": "on-a", "Id": SERVER, "UserId": ALICE},
                    {"uuid": "on-b", "Id": OTHER, "UserId": BOB}]}]),
                ("active_id", "local")):
            patch = mock.patch.object(userManager, attr, value)
            self.addCleanup(patch.stop)
            patch.start()

    def test_a_login_on_another_server_answers_as_itself(self):
        """Streaming from B while a row from A shares the id. The pair that
        comes back must be B's, so the store can refuse it."""
        self.assertEqual(self.actor_of(acting_login="on-b", server_id=SERVER),
                         (OTHER, BOB))

    def test_and_the_store_then_refuses_it(self):
        """The end-to-end shape of the corrupting write, asserted on the
        absence of the write rather than on what the resolver returned."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = SyncDB(os.path.join(tmp, "c.db"))
        self.addCleanup(db.close)
        db.upsert(_row("film", content_server_id=SERVER))
        db.set_userdata("film",
                        actor=self.actor_of(acting_login="on-b",
                                            server_id=SERVER),
                        played=True)
        self.assertEqual(db.all_userdata(), {})

    def test_with_no_login_the_active_profile_still_answers(self):
        """The fallback is the offline case and must survive: there is no
        client to name the person, so their credential for the row's server
        does."""
        self.assertEqual(self.actor_of(server_id=SERVER), (SERVER, ALICE))

    def test_a_login_on_the_right_server_answers_normally(self):
        self.assertEqual(self.actor_of(acting_login="on-a", server_id=SERVER),
                         (SERVER, ALICE))


class WatchedStateRecordedWithoutAServerDoesNotSurviveHomingTest(
        unittest.TestCase):
    """R15/R2: the `(@none, @none)` bucket is dropped when the row is homed.

    An orphan records under `(@none, @none)` -- no server, so no account, so
    one machine-wide bucket -- and every read for that row looks there. The
    moment the server is known, every read looks somewhere else, and this is
    what settles where the old bucket goes: nowhere.

    It used to be carried onto `(server, @none)`, which is per server and is
    read by every local profile holding no account there -- so the carry
    handed those profiles marks that a *named* account's viewing may have
    left, at the one moment the machine starts being able to tell them apart.
    The loss where nothing supersedes the bucket is ruled and accepted
    ([iw], R15 and R32): *"the view can't go anywhere... So it would be
    lost."*
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "c.db"))
        self.addCleanup(self.db.close)
        self.db.upsert(_row("x", content_server_id=None))

    def test_state_recorded_while_orphaned_is_dropped_at_the_homing(self):
        self.db.set_userdata("x", actor=None, played=True)
        self.assertEqual(self.db.userdata_actors("x"),
                         [(NO_ACTOR, NO_ACTOR)])

        self.assertTrue(self.db.home_content_server("x", SERVER))

        self.assertEqual(self.db.userdata_actors("x"), [],
                         "the anonymous bucket outlived the homing")
        self.assertFalse(self.db.userdata("x", actor=(SERVER, None))["played"])

    def test_it_is_dropped_rather_than_stranded_under_the_old_key(self):
        """The distinction the actor list is there to make. Leaving the row
        where it was reads identically through `userdata` -- nothing looks
        under `(@none, @none)` for a homed row -- and would come back the
        next time a restore orphaned the row again."""
        self.db.set_userdata("x", actor=None, played=True)
        self.db.home_content_server("x", SERVER)
        self._orphan_again()
        self.assertFalse(self.db.userdata("x", actor=None)["played"],
                         "the bucket was left behind, not dropped")

    def test_the_person_half_is_not_invented_on_the_way(self):
        """Learning the server does not learn who watched it, which is the
        reason the bucket cannot be carried anywhere at all."""
        self.db.set_userdata("x", actor=None, played=True)
        self.db.home_content_server("x", SERVER)
        self.assertFalse(self.db.userdata("x", actor=(SERVER, ALICE))["played"])

    def test_homing_to_nothing_is_refused_rather_than_half_done(self):
        """A falsy server used to make the move raise and roll the whole
        transaction back -- including the `downloads` update -- while the
        caller was told it had worked. The row stayed an orphan and
        `claim_identity` answered `rehomed`. The drop cannot raise, so this
        now pins the refusal itself: nothing is homed, so nothing is lost."""
        self.db.set_userdata("x", actor=None, played=True)
        self.assertFalse(self.db.home_content_server("x", None))
        self.assertFalse(self.db.home_content_server("x", ""))
        self.assertIsNone(self.db.get("x")["content_server_id"])
        self.assertEqual(self.db.userdata_actors("x"),
                         [(NO_ACTOR, NO_ACTOR)],
                         "a refused homing must not drop anything either")

    def test_the_row_learns_it_too(self):
        self.db.home_content_server("x", SERVER)
        self.assertEqual(self.db.get("x")["content_server_id"], SERVER)

    def _orphan_again(self):
        """Put the row back to having no server, the way a catalog restore
        or a `_reconcile_disk` re-adoption does, so state accumulates under
        `(@none, @none)` a second time."""
        self.db._conn.execute(
            "UPDATE downloads SET content_server_id=NULL WHERE item_id=?",
            ("x",))
        self.db._conn.commit()

    def test_the_homed_bucket_is_left_where_it_is(self):
        """The half a "nothing is left" assertion cannot see: the drop is
        keyed on the sentinel pair, not on the item.

        `(server, @none)` is a different bucket with its own writers -- R28
        folds a queued viewing into it -- and homing has nothing to say about
        it. A `DELETE ... WHERE item_id=?` passes every test above and takes
        this with it."""
        self.db.home_content_server("x", SERVER)
        self.db.set_userdata("x", actor=(SERVER, None), played=True,
                             position_ticks=999)
        self._orphan_again()
        self.db.set_userdata("x", actor=None, position_ticks=5)

        self.assertTrue(self.db.home_content_server("x", SERVER))

        self.assertEqual(self.db.userdata_actors("x"), [(SERVER, NO_ACTOR)])
        state = self.db.userdata("x", actor=(SERVER, None))
        self.assertTrue(state["played"], "the homed bucket was dropped too")
        self.assertEqual(state["position_ticks"], 999,
                         "the orphan bucket overwrote the homed one")

    def test_a_named_accounts_state_is_left_where_it_is(self):
        """The same, for the key that actually reaches a server."""
        self.db.home_content_server("x", SERVER)
        self.db.set_userdata("x", actor=(SERVER, ALICE), played=True,
                             position_ticks=77)
        self._orphan_again()
        self.db.set_userdata("x", actor=None, played=True)

        self.db.home_content_server("x", SERVER)

        state = self.db.userdata("x", actor=(SERVER, ALICE))
        self.assertTrue(state["played"])
        self.assertEqual(state["position_ticks"], 77)


class ThePullMayRetreatWhereThePushMayNotTest(unittest.TestCase):
    """The direction split, at the store where it is enforced.

    [iw]: *"server can sync unwatched state back to the offline store if it
    was previously observed as being watched last time it checked. Locally
    though an unwatched state shouldn't clobber a remote watched state unless
    the user was recorded deliberately marking something as unwatched, as
    stale unwatched state could sync up to the server and destroy recorded
    progress otherwise."*

    "Recorded deliberately" is the queue: a `played = 1` entry is a mark this
    machine made and has not delivered, so the server's "unwatched" predates
    it. **Not any pending entry** -- `played` is nullable and ordinary offline
    progress queues position-only rows, which say nothing about an unsent
    mark. The three cases below are the same row and the same actor, differing
    only in what is queued, because a set that varied anything else would
    prove nothing about the gate.
    """

    ACTOR = (SERVER, ALICE)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "c.db"),
                         actor_for=actor_for)
        self.addCleanup(self.db.close)
        self.db.upsert(_row("film"))
        self.db.set_watched("film", True, actor=self.ACTOR)

    def _pull(self, played=False, position_ticks=None):
        return self.db.update_userdata("film", actor=self.ACTOR,
                                       played=played,
                                       position_ticks=position_ticks,
                                       allow_retreat=True)

    def _played(self):
        return bool(self.db.userdata("film", actor=self.ACTOR)["played"])

    def test_with_nothing_queued_the_server_wins(self):
        self.assertTrue(self._pull())
        self.assertFalse(self._played())

    def test_an_undelivered_watched_mark_holds_it(self):
        self.db.upsert_playstate("film", actor=self.ACTOR, played=True)
        self.assertFalse(self._pull(), "nothing should have moved")
        self.assertTrue(self._played(),
                        "the user's own viewing was deleted on the strength "
                        "of a server that has not been told about it yet")

    def test_a_position_only_entry_does_not_hold_it(self):
        """The overbroad reading of the gate. Ordinary offline progress
        queues `played IS NULL`, which says nothing about an unsent mark --
        and blocking on it would make the retreat unreachable for anyone who
        watches anything offline."""
        self.db.upsert_playstate("film", actor=self.ACTOR,
                                 position_ticks=500)
        self.assertTrue(self._pull())
        self.assertFalse(self._played())

    def test_and_the_queued_position_survives_the_retreat(self):
        self.db.upsert_playstate("film", actor=self.ACTOR,
                                 position_ticks=500)
        self._pull()
        self.assertEqual(
            [(e["item_id"], e["position_ticks"], e["played"])
             for e in self.db.list_playstate()],
            [("film", 500, None)])

    def test_another_persons_undelivered_mark_is_not_this_persons(self):
        self.db.upsert_playstate("film", actor=(SERVER, BOB), played=True)
        self.assertTrue(self._pull())
        self.assertFalse(self._played())

    def test_the_playback_writer_still_cannot_retreat(self):
        """The push half, unchanged and load-bearing: `allow_retreat`
        defaults False, so every other caller of this method keeps the
        sticking rule. A stale local unwatched reaching the server is what
        would destroy progress recorded elsewhere."""
        self.assertFalse(self.db.update_userdata("film", actor=self.ACTOR,
                                                 played=False))
        self.assertTrue(self._played())

    def test_the_retreat_frees_the_position_the_finish_had_pinned(self):
        """A finish clears the resume point and then refuses to store a
        near-end one. Taking the finish away has to take that refusal with
        it, or the item comes back unwatched at position zero."""
        self.db.upsert(dict(_row("film"), runtime_ticks=100))
        self.assertTrue(self._pull(played=False, position_ticks=99))
        self.assertFalse(self._played())
        self.assertEqual(
            self.db.userdata("film", actor=self.ACTOR)["position_ticks"], 99)

    def test_an_absent_answer_is_not_an_unwatched_answer(self):
        """`None` is "the server said nothing about it", which is what a
        caller passes when it is only reporting a position. Reading that as
        a retreat would un-watch an item on every progress report."""
        self._pull(played=None, position_ticks=1)
        self.assertTrue(self._played())


class TheSweepAsksAsWhoeverIsSignedInTest(unittest.TestCase):
    """C5's scope half, and the two-people case behind it.

    Observed on the QA server: `qa-admin` and `qa-user` are two accounts on
    one ServerId, and the same film comes back `Played=False` for one and
    `Played=True` for the other. So *whose* answer the sweep is storing is
    not a detail -- and it used to be decided by `downloads.server_uuid`,
    the login that fetched the copy, which on a shared machine is whoever
    downloaded it rather than whoever is signed in.

    `clients._connect_all` groups credentials by ServerId into one fallback
    chain, so at most one of a server's accounts is ever connected. That is
    the ratified connection model and it is what makes
    "whichever account is connected" single-valued.

    **Bob has auto-download on for this server, and that is load-bearing in
    the fixture** (D1). The sweep is scoped to the union R21 names, so the
    only configuration in which it touches a row *somebody else* downloaded
    is the one where this account fetches from that server unattended --
    which is exactly the case R21 kept when it checked its four. Without it
    the pull asks nothing here and all four tests below measure silence.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from jellyfin_mpv_shim.sync.manager import SyncManager
        from jellyfin_mpv_shim.users import userManager
        self.db = SyncDB(os.path.join(self.tmp, "c.db"), actor_for=actor_for)
        self.addCleanup(self.db.close)
        # Downloaded by Alice, who is not the one signed in.
        self.db.upsert(dict(_row("film"), server_uuid="login-a"))
        self.db.set_watched("film", True, actor=(SERVER, ALICE))

        patch = mock.patch.object(userManager, "users", [{
            "id": "local",
            # See the class docstring: this is what puts another account's
            # row inside the signed-in account's sweep.
            "auto_download": [[SERVER, BOB]],
            "credentials": [
                {"uuid": uuid, "Id": server_id, "UserId": user_id}
                for uuid, (server_id, user_id) in ACTORS.items()]}])
        self.addCleanup(patch.stop)
        patch.start()

        self.answers = {"Played": False, "PlaybackPositionTicks": 42}
        answers = self.answers

        class Api:
            @staticmethod
            def get_items(ids, fields=""):
                return {"Items": [{"Id": i, "UserData": dict(answers)}
                                  for i in ids]}

        class Client:
            jellyfin = Api()

        self.m = SyncManager.__new__(SyncManager)
        self.m.db = self.db
        self.m._stop = False
        self.m._wake = mock.Mock()
        self.m._notify_change = lambda: None
        self.m._answered_accounts = set()
        self.m.get_clients = lambda: {"login-b": Client()}
        self.m.get_client = lambda uuid: Client() if uuid == "login-b" else None

    def test_the_answer_is_filed_under_the_connected_account(self):
        self.m._refresh_userdata()
        self.assertEqual(
            self.db.userdata("film", actor=(SERVER, BOB))["position_ticks"],
            42)

    def test_and_not_under_the_login_that_downloaded_it(self):
        self.m._refresh_userdata()
        self.assertEqual(
            self.db.userdata("film", actor=(SERVER, ALICE))["position_ticks"],
            0, "the downloader was credited with the viewer's position")

    def test_a_deferred_account_keeps_the_state_it_had(self):
        """Only the connected account is pulled. Alice's watched
        mark is not refreshed *and not cleared* -- the retreat applies to
        the account being asked about, and nobody asked about Alice."""
        self.m._refresh_userdata()
        self.assertTrue(self.db.userdata("film",
                                         actor=(SERVER, ALICE))["played"])

    def test_the_server_saying_unwatched_retreats_the_connected_account(self):
        self.db.set_watched("film", True, actor=(SERVER, BOB))
        self.m._refresh_userdata()
        self.assertFalse(self.db.userdata("film",
                                          actor=(SERVER, BOB))["played"])


class BothHomingSitesDropTheStateTest(unittest.TestCase):
    """There are two places a row learns its server, and R15 governs both.

    `home_content_server` is the manifest pass's;
    `_backfill_content_server_id` is the migration's, and it runs on **every
    open**. A rule holding at N-1 of N sites is this repository's recurring
    shape and the reason both go through one helper -- which is what this
    class pins, since the drop is otherwise invisible from the migration
    side.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "c.db")

    def _seed_orphan_with_state(self, **over):
        """A row whose DTO names a server but whose column does not -- which
        is what the backfill exists to fix -- carrying local watched state
        filed under the orphan key."""
        db = SyncDB(self.path)
        row = _row("film", content_server_id=None)
        row["item_json"] = json.dumps({"Id": "film", "Type": "Movie",
                                       "ServerId": SERVER})
        db.upsert(row)
        db.set_userdata("film", actor=None, played=True, position_ticks=42)
        db._conn.execute(
            "UPDATE downloads SET content_server_id=NULL WHERE item_id=?",
            ("film",))
        db._conn.commit()
        self.assertEqual(db.userdata_actors("film"), [(NO_ACTOR, NO_ACTOR)])
        db.close()

    def test_the_open_that_homes_the_row_drops_its_orphan_state_too(self):
        self._seed_orphan_with_state()

        db = SyncDB(self.path)          # the migration runs here
        self.addCleanup(db.close)

        self.assertEqual(db.get("film")["content_server_id"], SERVER)
        self.assertEqual(db.userdata_actors("film"), [],
                         "the orphan bucket outlived the migration's homing")
        self.assertFalse(db.userdata("film", actor=(SERVER, None))["played"])

    def test_a_later_open_does_not_drop_state_the_homed_row_has_since(self):
        """The backfill runs on every open and selects only rows that are
        still orphans. This is the regression guard on that pair: once the
        first open has homed the row, a second must leave the state the homed
        row has acquired since exactly where it is."""
        self._seed_orphan_with_state()
        db = SyncDB(self.path)          # homes the row, drops the bucket
        db.set_userdata("film", actor=(SERVER, None), played=True,
                        position_ticks=9)
        db.close()

        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.userdata_actors("film"), [(SERVER, NO_ACTOR)])
        state = db.userdata("film", actor=(SERVER, None))
        self.assertTrue(state["played"])
        self.assertEqual(state["position_ticks"], 9)


class ARefusalIsAudibleTest(unittest.TestCase):
    """A refused write used to be completely silent.

    Refusing is right -- the row belongs to another content server and ids
    are not unique across servers -- but nothing said so anywhere:
    `_write_userdata` returned, `upsert_playstate` answered False, `userdata`
    answered "nothing stored", and the log was clean. The symptom a user
    reports is "watched state stopped syncing", and before this there was no
    evidence in their log to act on.

    Two levels, and the split is the requirement rather than a style: every
    refusal is DEBUG so the item ids are recoverable on request, and a
    rate-limited summary is INFO **because the default configuration logs
    INFO** (`log_utils.root_logger.level`). A fix that only logged at DEBUG
    would leave every default-config issue report exactly as empty as before.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._n = 0
        self.db = self._fresh()

    def _fresh(self):
        self._n += 1
        db = SyncDB(os.path.join(self.tmp, "c%d.db" % self._n))
        self.addCleanup(db.close)
        db.upsert(_row("homed", content_server_id=SERVER))
        db.upsert(_row("orphan", content_server_id=None))
        return db

    @contextlib.contextmanager
    def _records(self):
        """Every `sync.db` record, without asserting that there are any.

        `assertLogs` fails an empty capture, which is the negative controls
        below -- and they are the half that matters, because a refusal log
        that also fires for the ordinary verdicts is a log nobody reads.
        """
        logger = logging.getLogger("sync.db")
        captured = []

        class _Sink(logging.Handler):
            def emit(self, record):
                captured.append(record)

        sink = _Sink()
        logger.addHandler(sink)
        was, propagated = logger.level, logger.propagate
        # setLevel, never `logger.level = ...`: the assignment leaves
        # `isEnabledFor`'s cache holding the old answer, so DEBUG records
        # are dropped before any handler sees them.
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            yield captured
        finally:
            logger.removeHandler(sink)
            logger.setLevel(was)
            logger.propagate = propagated

    @staticmethod
    def _at(records, level):
        return [r.getMessage() for r in records if r.levelno == level]

    def test_a_refused_write_says_so_at_info(self):
        with self._records() as records:
            self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        summary = self._at(records, logging.INFO)
        self.assertEqual(len(summary), 1, "one summary, not one per field")
        self.assertIn(SERVER, summary[0], "the row's content server")
        self.assertIn(OTHER, summary[0], "the server the account is on")

    def test_and_names_the_item_at_debug(self):
        with self._records() as records:
            self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        self.assertTrue(any("homed" in m
                            for m in self._at(records, logging.DEBUG)),
                        "DEBUG is where the item ids are recoverable")

    def test_a_refused_queue_write_is_audible_too(self):
        """Both doors go through the one verdict, so neither can be the
        quiet one."""
        with self._records() as records:
            self.db.upsert_playstate("homed", actor=(OTHER, BOB), played=True)
        self.assertEqual(len(self._at(records, logging.INFO)), 1)

    def test_a_refused_read_is_audible_too(self):
        with self._records() as records:
            self.db.userdata("homed", actor=(OTHER, BOB))
        self.assertEqual(len(self._at(records, logging.INFO)), 1)

    def test_the_summary_is_rate_limited_but_the_detail_is_not(self):
        """A mismatch fires on every progress report for that item -- one
        every ten seconds while it plays -- so the INFO line has to be
        bounded or it is the flood it was meant to replace."""
        with self._records() as records:
            for _ in range(5):
                self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        self.assertEqual(len(self._at(records, logging.INFO)), 1)
        self.assertEqual(len(self._at(records, logging.DEBUG)), 5)

    def test_the_next_summary_carries_what_was_suppressed(self):
        """Rate-limiting may not lose the count, or the second summary
        understates a fault that has been firing for ten minutes."""
        self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        for _ in range(4):
            self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        shape = (SERVER, OTHER)
        count, last = self.db._refusals[shape]
        self.db._refusals[shape] = (
            count, last - self.db._REFUSAL_SUMMARY_INTERVAL - 1)

        with self._records() as records:
            self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        summary = self._at(records, logging.INFO)
        self.assertEqual(len(summary), 1)
        self.assertIn("5 watched-state", summary[0],
                      "the four suppressed plus the one that reopened it")

    def test_a_different_pair_of_servers_is_its_own_summary(self):
        """Rate-limiting per shape, not globally: a second mismatch is a
        second fault and the first one's timer must not hide it."""
        third = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        self.db.set_userdata("homed", actor=(OTHER, BOB), played=True)
        with self._records() as records:
            self.db.set_userdata("homed", actor=(third, BOB), played=True)
        self.assertEqual(len(self._at(records, logging.INFO)), 1)

    def test_an_item_we_hold_no_copy_of_is_not_a_refusal(self):
        """The negative control with teeth. Every `UserDataChanged` the
        server pushes about anything undownloaded lands here, so if `no_row`
        logged at INFO the default log would be nothing else.
        """
        with self._records() as records:
            self.db.set_userdata("never-held", actor=(SERVER, ALICE),
                                 played=True)
        self.assertEqual(self._at(records, logging.INFO), [])
        self.assertEqual(self._at(records, logging.DEBUG), [])

    def test_a_write_that_lands_is_silent(self):
        with self._records() as records:
            self.db.set_userdata("homed", actor=(SERVER, ALICE), played=True)
        self.assertEqual(records, [])

    def test_an_orphan_is_kept_locally_and_says_so_only_at_debug(self):
        """11c: unattributable progress is recorded and never queued. That
        is a ruling, not a fault, so it may not reach INFO -- but it is the
        answer to "my offline progress never reaches the server", so it may
        not be silent either."""
        with self._records() as records:
            self.db.upsert_playstate("orphan", actor=(SERVER, ALICE),
                                     played=True)
        self.assertEqual(self._at(records, logging.INFO), [])
        self.assertTrue(any("orphan" in m
                            for m in self._at(records, logging.DEBUG)))


if __name__ == "__main__":
    unittest.main()
