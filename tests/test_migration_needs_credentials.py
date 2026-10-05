"""The catalog migration must not attribute rows before the logins are loaded.

`SyncDB`'s migrations fold per-row state onto the account that owns it, using the
resolver `SyncManager` hands them (`sync/manager.py:_actor_for`). That resolver
reads `userManager.users`, which is empty until `userManager.load()` runs -- and
`load()`'s only caller is `clientManager.load_credentials`, reached from
`login_servers()` at `mpv_shim.py:324`, while `syncManager.start()` at `:314`
opens the catalog **synchronously**.

So on a real launch the migration ran against a resolver that was *present and
answered None for everything*, and `_migrate_playstate_actors` drops what it
cannot attribute: **every queued offline playstate entry was deleted on upgrade.**
The suite covered a resolver that answers and a resolver that is absent; nobody
wrote the third case, which is the only one production had.

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
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.sync import manager as sync_manager
from jellyfin_mpv_shim.sync.db import SyncDB
from jellyfin_mpv_shim.users import userManager

LOGIN = "1a5abbb8-f597-4dff-9ead-71655f941c3e"
SERVER = "97fd53974c4c4b7cbac55e437dcc094f"
ACCOUNT = "5e1f0c9a2b3d4e5f6a7b8c9d0e1f2a3b"

#: The shipped schema, as the reference install actually has it: no
#: `content_server_id`, no `item_userdata`, no `watched_at`. Every prior
#: migration test started from a branch intermediate instead.
_SHIPPED = """
CREATE TABLE downloads (item_id TEXT PRIMARY KEY, server_id TEXT, server_uuid TEXT,
 type TEXT, name TEXT, series_id TEXT, series_name TEXT, season_id TEXT,
 parent_index INTEGER, index_number INTEGER, media_source_id TEXT, file_path TEXT,
 ext TEXT, size_bytes INTEGER DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0,
 status TEXT, runtime_ticks INTEGER, item_json TEXT, source_json TEXT,
 userdata_json TEXT, added_at INTEGER, origin TEXT, completed_at INTEGER,
 library_id TEXT);
CREATE TABLE pending_playstate (id INTEGER PRIMARY KEY AUTOINCREMENT,
 server_uuid TEXT, item_id TEXT, position_ticks INTEGER, played INTEGER,
 created_at INTEGER);
"""


class MigrationNeedsCredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.catalog = os.path.join(self.tmp, "catalog.db")
        c = sqlite3.connect(self.catalog)
        c.executescript(_SHIPPED)
        c.execute("INSERT INTO downloads (item_id, server_uuid, status, item_json,"
                  " origin) VALUES (?,?,?,?,?)",
                  ("film", LOGIN, "complete",
                   json.dumps({"Id": "film", "ServerId": SERVER}), "user"))
        c.execute("INSERT INTO pending_playstate (server_uuid, item_id,"
                  " position_ticks, played, created_at) VALUES (?,?,?,?,?)",
                  (LOGIN, "film", 5000, 1, 0))
        c.commit()
        c.close()

        # A users.json holding exactly the login the rows are keyed to, and a
        # registry that has NOT read it yet -- production's state at the moment
        # `syncManager.start()` opens the catalog.
        users_file = os.path.join(self.tmp, "users.json")
        with open(users_file, "w", encoding="utf-8") as fh:
            json.dump({"active": "p1", "users": [{
                "id": "p1", "name": "(default)", "device_id": "dev",
                "default": True, "last_server": None,
                "credentials": [{"uuid": LOGIN, "Id": SERVER,
                                 "UserId": ACCOUNT,
                                 "address": "http://s", "AccessToken": "t"}],
            }]}, fh)
        self.enterPatches(users_file)

    def enterPatches(self, users_file):
        for target, value in (("_path", lambda: users_file),
                              ("_loaded", False),
                              ("users", []),
                              ("active_id", None)):
            p = mock.patch.object(userManager, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_queued_offline_playstate_survives_the_upgrade(self):
        """The data-loss case, through the real resolver and the real migration.

        A position recorded offline is the one thing in this catalog the server
        does not already know, so dropping it loses something unrecoverable.
        """
        db = SyncDB(self.catalog, actor_for=sync_manager._actor_for)
        self.addCleanup(db.close)
        self.assertEqual(
            1, len(db.list_playstate()),
            "the queued offline position was deleted by the migration")

    def test_it_is_attributed_rather_than_merely_kept(self):
        """Kept is not enough: an entry nobody owns cannot be replayed."""
        db = SyncDB(self.catalog, actor_for=sync_manager._actor_for)
        self.addCleanup(db.close)
        entry = db.list_playstate()[0]
        self.assertEqual(SERVER, entry["server_id"])
        self.assertEqual(ACCOUNT, entry["user_id"])

    def test_the_resolver_does_not_trust_a_caller_to_have_loaded_the_logins(self):
        """The invariant, pinned separately from its consequence.

        The consequence above is a data loss in one migration; this is the rule
        that stops the next migration re-discovering it. `_actor_for` is the one
        resolver every catalog-open is handed, so it is where the guarantee
        belongs -- not in whichever order `mpv_shim.main` happens to call things.
        """
        with mock.patch.object(userManager, "load") as load:
            sync_manager._actor_for(LOGIN)
        load.assert_called()



class AnUnreadableRegistryIsNotAnEmptyOneTest(unittest.TestCase):
    """`users.json` exists and does not parse: two things must not happen.

    `UserManager.load` handles "first run" and "corrupt file" in one branch --
    build a `(default)` user from `cred.json` (absent on a modern install, so
    with no credentials), mark itself loaded, and `save()`. On a corrupt file
    that overwrites the unreadable bytes, and the registry then answers `None`
    for every login while claiming to be authoritative, so the migration's drop
    branch fires and every queued offline position goes.

    Same loss as this module's other class by a different route, which is why it
    needs its own test: fixing *when* the resolver is asked said nothing about
    what a `None` answer means. The migration already draws the right
    distinction -- `if self._actor_for is None: return`, "cannot ask, so has not
    been told nobody" -- keyed on the resolver being absent rather than unable.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.catalog = os.path.join(self.tmp, "catalog.db")
        c = sqlite3.connect(self.catalog)
        c.executescript(_SHIPPED)
        c.execute("INSERT INTO downloads (item_id, server_uuid, status,"
                  " item_json, origin) VALUES (?,?,?,?,?)",
                  ("film", LOGIN, "complete",
                   json.dumps({"Id": "film", "ServerId": SERVER}), "user"))
        c.execute("INSERT INTO pending_playstate (server_uuid, item_id,"
                  " position_ticks, played, created_at) VALUES (?,?,?,?,?)",
                  (LOGIN, "film", 5000, 1, 0))
        c.commit()
        c.close()

        self.users_file = os.path.join(self.tmp, "users.json")
        self.corrupt = '{"users": [{"id": "p1", "credentials": [ TRUNCATED'
        with open(self.users_file, "w", encoding="utf-8") as fh:
            fh.write(self.corrupt)
        for target, value in (("_path", lambda: self.users_file),
                              ("_loaded", False), ("users", []),
                              ("active_id", None), ("load_failed", False)):
            patch = mock.patch.object(userManager, target, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)

    def _open(self):
        """Exactly what `SyncManager._open_writable` does."""
        return SyncDB(self.catalog, actor_for=sync_manager._actor_resolver())

    def test_queued_progress_survives_a_registry_that_cannot_be_read(self):
        db = self._open()
        self.addCleanup(db.close)
        self.assertEqual(
            1, len(db.list_playstate()),
            "an unreadable registry cannot say whose this is, and 'cannot "
            "ask' is not 'has been told nobody'")

    def test_the_unreadable_file_is_kept_rather_than_overwritten(self):
        userManager.load()
        # Named, not "any users.json.* sibling": a save now also leaves a
        # `users.json.bak` (the restore path R12 asked for), and a listing
        # that takes whichever came first was picking that instead.
        aside = [n for n in os.listdir(self.tmp)
                 if n.startswith("users.json.unreadable-")]
        self.assertTrue(
            aside, "those bytes were the user's only record of their servers; "
                   "they must survive somewhere")
        with open(os.path.join(self.tmp, aside[0]), encoding="utf-8") as fh:
            self.assertEqual(self.corrupt, fh.read())

    def test_a_broken_registry_does_not_open_the_catalog_at_all(self):
        """R12, and the reason it comes before every other step: the two
        findings both reviewers reached need a catalog that opened with
        nobody to ask. Refusing to open removes the state rather than
        guarding the sites that read it."""
        syncManager = sync_manager.SyncManager()
        # The seeded catalog, not a fresh one beside it: a store the test
        # never wrote to could not be harmed and the assertion below would
        # hold whatever the code did.
        with mock.patch.object(sync_manager.settings, "sync_path", self.tmp):
            syncManager.start(lambda uuid: None)
        self.addCleanup(syncManager.stop)
        self.assertIsNone(
            syncManager.db,
            "the catalog was opened with a registry that cannot say who "
            "anything belongs to")
        self.assertTrue(syncManager.unavailable,
                        "nothing recorded why the subsystem is not running, "
                        "so the UI can only show an empty list")

    def test_the_queued_entry_is_untouched_because_nothing_opened_it(self):
        """T2 dissolved rather than repaired. The queued row is the only
        state here that is not a clone of the server's, and with the catalog
        unopened neither the migration's empty-actor stamp nor the sweep that
        deletes what does not match a real server id can reach it."""
        before = self._pending_rows()
        syncManager = sync_manager.SyncManager()
        # The seeded catalog, not a fresh one beside it: a store the test
        # never wrote to could not be harmed and the assertion below would
        # hold whatever the code did.
        with mock.patch.object(sync_manager.settings, "sync_path", self.tmp):
            syncManager.start(lambda uuid: None)
        self.addCleanup(syncManager.stop)
        self.assertEqual(before, self._pending_rows(),
                         "the queued offline progress did not survive a "
                         "launch that could not read the registry")

    def test_a_registry_that_reads_still_starts_with_nothing_connected(self):
        """The clarification [iw] added, as a check: the gate is about the
        file being corrupt, **not** about being offline. Nothing is
        connected here and no client answers, which is exactly a launch on a
        train, and the subsystem must still come up -- that launch is when
        the downloads matter most."""
        os.remove(self.users_file)                  # a genuine first run
        userManager.load()
        syncManager = sync_manager.SyncManager()
        # The seeded catalog, not a fresh one beside it: a store the test
        # never wrote to could not be harmed and the assertion below would
        # hold whatever the code did.
        with mock.patch.object(sync_manager.settings, "sync_path", self.tmp):
            syncManager.start(lambda uuid: None, get_clients=lambda: {})
        self.addCleanup(syncManager.stop)
        # getattr, so this passes before the gate exists as well as after:
        # a control that only the new code can satisfy controls nothing.
        self.assertIsNone(getattr(syncManager, "unavailable", None))
        self.assertIsNotNone(
            syncManager.db,
            "a readable registry with no server connected is an ordinary "
            "offline launch, and it must still open the catalog")

    def test_moving_the_download_folder_is_refused_rather_than_raising(self):
        """`relocate` reads `self.root`, which a refused start never set."""
        syncManager = sync_manager.SyncManager()
        # The seeded catalog, not a fresh one beside it: a store the test
        # never wrote to could not be harmed and the assertion below would
        # hold whatever the code did.
        with mock.patch.object(sync_manager.settings, "sync_path", self.tmp):
            syncManager.start(lambda uuid: None)
        self.addCleanup(syncManager.stop)
        self.assertTrue(
            getattr(syncManager, "unavailable", None),
            "not in the refused state, so this exercises the ordinary move "
            "path and says nothing about the one it is named after")
        ok, message = syncManager.relocate(os.path.join(self.tmp, "elsewhere"))
        self.assertFalse(ok)
        self.assertTrue(message, "a refusal with no message reads as a "
                                 "button that does nothing")

    def _pending_rows(self):
        c = sqlite3.connect(self.catalog)
        try:
            return c.execute("SELECT id, server_uuid, item_id, position_ticks,"
                             " played FROM pending_playstate"
                             " ORDER BY id").fetchall()
        finally:
            c.close()

    def test_a_genuine_first_run_still_starts_clean(self):
        """The half that must keep working: no file at all is not a failure."""
        os.remove(self.users_file)
        userManager.load()
        self.assertFalse(getattr(userManager, "load_failed", False))
        self.assertEqual(1, len(userManager.users))
        self.assertIsNotNone(sync_manager._actor_resolver())


if __name__ == "__main__":
    unittest.main()
