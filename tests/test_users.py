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
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim.users import UserManager, _hash_pin
from jellyfin_mpv_shim.conf import settings


class UserManagerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Redirect both users.json and the legacy cred.json into the tempdir.
        patcher = mock.patch(
            "jellyfin_mpv_shim.users.conffile.get",
            side_effect=lambda app, conf_file, create=False: os.path.join(
                self.tmp, conf_file),
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_cred_json(self, data):
        with open(os.path.join(self.tmp, "cred.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read_users_json(self):
        with open(os.path.join(self.tmp, "users.json"), encoding="utf-8") as f:
            return json.load(f)

    def fresh(self):
        um = UserManager()
        um.load()
        return um


class MigrationTest(UserManagerTestBase):
    def test_migrates_flat_cred_list_into_default_user(self):
        creds = [{"uuid": "u1", "username": "alice", "address": "http://s:8096"}]
        self.write_cred_json(creds)
        um = self.fresh()

        self.assertEqual(len(um.users), 1)
        default = um.active_user
        self.assertTrue(default["default"])
        # The default user keeps the config's device id so existing sessions
        # and tokens keep working untouched.
        self.assertEqual(default["device_id"], settings.client_uuid)
        self.assertEqual(default["credentials"], creds)
        # users.json was written.
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "users.json")))

    def test_migrates_legacy_servers_dict(self):
        self.write_cred_json({"Servers": [{"Id": "srv", "address": "http://s"}]})
        um = self.fresh()
        creds = um.active_user["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertIn("uuid", creds[0])   # a fresh uuid was assigned
        self.assertEqual(creds[0]["username"], "")

    def test_no_cred_json_yields_empty_default_user(self):
        um = self.fresh()
        self.assertEqual(len(um.users), 1)
        self.assertTrue(um.active_user["default"])
        self.assertEqual(um.active_user["credentials"], [])

    def test_existing_users_json_wins_over_cred_json(self):
        self.write_cred_json([{"uuid": "ignored"}])
        um = self.fresh()  # migrates + writes users.json
        um.add_user("Kids")
        # A brand new manager should read users.json, not re-migrate cred.json.
        um2 = self.fresh()
        names = sorted(u["name"] for u in um2.users)
        self.assertEqual(names, sorted([um.users[0]["name"], "Kids"]))


class DeviceIdentityTest(UserManagerTestBase):
    def test_new_user_has_unique_non_config_device_id(self):
        um = self.fresh()
        default_id = um.active_user["device_id"]
        kid = um.add_user("Kids")
        self.assertNotEqual(kid["device_id"], settings.client_uuid)
        self.assertNotEqual(kid["device_id"], default_id)
        self.assertFalse(kid["default"])

    def test_device_name_appends_for_non_default(self):
        um = self.fresh()
        default = um.active_user
        kid = um.add_user("Kids")
        self.assertEqual(um.device_name_for(default), settings.player_name)
        self.assertEqual(
            um.device_name_for(kid),
            "{0} (Kids)".format(settings.player_name))


class SwitchTest(UserManagerTestBase):
    def test_set_active_switches_and_persists(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        um.set_active(kid["id"])
        self.assertEqual(um.active_id, kid["id"])
        self.assertEqual(self.read_users_json()["active"], kid["id"])

    def test_credentials_for_active_is_a_copy(self):
        creds = [{"uuid": "u1", "username": "a"}]
        self.write_cred_json(creds)
        um = self.fresh()
        copy = um.credentials_for_active()
        copy[0]["username"] = "mutated"
        # The store must not have been mutated through the returned copy.
        self.assertEqual(um.active_user["credentials"][0]["username"], "a")

    def test_set_active_credentials_round_trips(self):
        um = self.fresh()
        um.set_active_credentials([{"uuid": "x", "connected": True}])
        um2 = self.fresh()
        self.assertEqual(um2.active_user["credentials"],
                         [{"uuid": "x", "connected": True}])


class UserLifecycleTest(UserManagerTestBase):
    def test_cannot_delete_active_user(self):
        um = self.fresh()
        um.add_user("Kids")
        ok, err = um.delete_user(um.active_id)
        self.assertFalse(ok)
        self.assertIsNotNone(err)

    def test_cannot_delete_last_user(self):
        um = self.fresh()
        # Only the default user exists; even a non-active guard aside, the last
        # user can't go.
        ok, _err = um.delete_user(um.active_id)
        self.assertFalse(ok)

    def test_delete_non_active_user(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        ok, _err = um.delete_user(kid["id"])
        self.assertTrue(ok)
        self.assertIsNone(um.get(kid["id"]))

    def test_rename(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        self.assertTrue(um.rename_user(kid["id"], "Children"))
        self.assertEqual(um.get(kid["id"])["name"], "Children")


class RegistryDurabilityTest(UserManagerTestBase):
    """R12 -- "if there is a way it can get corrupted we need to fix that".

    `users.json` is the one file whose loss starts the whole chain: without a
    registry nothing can say who a queued offline viewing belongs to. Ruling
    R12 asked for three things and the file had none of them -- the write was
    atomic but not durable, there was no copy of the last good bytes, and the
    only thing resembling a restore path rescued the *corrupt* bytes.

    """

    def a_saved_registry(self, name="Casper"):
        """One user holding one credential, saved the ordinary way.

        **cred.json is removed afterwards, and that is the point.** Left in
        place it is a second way for a credential to come back -- `load`
        falls through to the legacy migration whenever the registry has no
        users -- so a test named after the backup passes without one. The
        `{}` case below did exactly that against the unfixed code.
        """
        self.write_cred_json([{"Name": name, "Id": "srv1", "uuid": "u1"}])
        um = self.fresh()
        um.save()
        os.remove(os.path.join(self.tmp, "cred.json"))
        return um

    def backup_path(self):
        return os.path.join(self.tmp, "users.json.bak")

    def test_the_bytes_are_flushed_before_the_rename_and_the_directory_after(
            self):
        """The whole point of the change, and not observable any other way
        short of cutting the power: `os.replace` is atomic with respect to
        readers and says nothing about the contents having reached the disk.
        Ordering is the assertion because either fsync on its own leaves a
        window."""
        import os as real_os

        journal = []
        true_fsync, true_replace = real_os.fsync, real_os.replace

        def note_fsync(fd):
            journal.append("fsync")
            return true_fsync(fd)

        def note_replace(src, dst):
            journal.append("replace:" + os.path.basename(dst))
            return true_replace(src, dst)

        self.write_cred_json([{"Name": "Casper", "Id": "srv1", "uuid": "u1"}])
        um = UserManager()
        um.load()
        journal.clear()
        with mock.patch("os.fsync", note_fsync), \
                mock.patch("os.replace", note_replace):
            um.save()

        first = journal.index("replace:users.json")
        self.assertIn("fsync", journal[:first],
                      "the temp file was renamed over users.json before its "
                      "bytes were flushed, so a power cut can leave an "
                      "atomically renamed empty file")
        self.assertIn("fsync", journal[first + 1:],
                      "the directory entry was never flushed, so the rename "
                      "itself need not survive a power cut")

    def test_a_save_leaves_a_backup_of_what_it_wrote(self):
        um = self.a_saved_registry()
        self.assertTrue(os.path.exists(self.backup_path()))
        with open(self.backup_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), self.read_users_json())
        um.add_user("Second")
        with open(self.backup_path(), encoding="utf-8") as f:
            self.assertEqual(
                json.load(f), self.read_users_json(),
                "the backup did not follow the save that came after it")

    def test_a_torn_primary_is_restored_from_the_backup(self):
        """The power-cut case R12 names. A zero-length users.json is what
        ext4 can leave behind after an atomic rename with no fsync, which is
        exactly the state this branch shipped."""
        self.a_saved_registry()
        with open(os.path.join(self.tmp, "users.json"), "w",
                  encoding="utf-8"):
            pass                                    # truncate to nothing

        um = UserManager()
        um.load()
        self.assertFalse(
            um.load_failed,
            "a registry restored from its backup must not report itself "
            "unreadable -- that withholds the catalog's actor resolver and "
            "strands every queued offline viewing")
        self.assertEqual(
            ["u1"], [c["uuid"] for c in um.users[0]["credentials"]],
            "the saved login did not come back")
        self.assertEqual(
            um.users[0]["credentials"],
            self.read_users_json()["users"][0]["credentials"],
            "the recovered registry was not written back, so the next "
            "launch would restore it again")

    def test_a_registry_emptied_without_a_parse_error_is_restored_too(self):
        """`{}` parses. Treated as a first run it would migrate cred.json
        over the top and then overwrite the backup with the result -- the
        loss the backup exists to prevent, arrived at through the recovery
        path itself."""
        self.a_saved_registry()
        with open(os.path.join(self.tmp, "users.json"), "w",
                  encoding="utf-8") as f:
            f.write("{}")

        um = UserManager()
        um.load()
        self.assertFalse(um.load_failed)
        self.assertEqual(["u1"],
                         [c["uuid"] for c in um.users[0]["credentials"]])

    def test_with_no_usable_backup_the_refusal_stands(self):
        """The backup is an addition, not a replacement: with nothing to
        restore from, an unreadable registry still says so rather than
        reporting an empty one, because `_actor_resolver` reads that flag to
        decide whether the catalog may attribute anything."""
        self.a_saved_registry()
        for name in ("users.json", "users.json.bak"):
            with open(os.path.join(self.tmp, name), "w",
                      encoding="utf-8") as f:
                f.write("{ this is not json")

        um = UserManager()
        um.load()
        self.assertTrue(um.load_failed)
        self.assertTrue(
            any(n.startswith("users.json.unreadable-")
                for n in os.listdir(self.tmp)),
            "the corrupt bytes were not kept aside")

    def test_a_missing_primary_is_restored_from_the_backup(self):
        """**The recovery path opens this window itself.** `_set_aside`
        renames the damaged primary before `save()` rewrites it, so an
        interruption or a failed write between those two leaves only the
        backup on disk. The guard that consults the backup sat under
        `if os.path.exists(path)`, so the next launch skipped it, took the
        first-run path, and `save()` overwrote the one remaining copy of the
        user's servers -- the loss the backup exists to prevent, reached
        through the recovery path, which is the same shape as the `{}` case
        two tests above.
        """
        self.a_saved_registry()
        os.remove(os.path.join(self.tmp, "users.json"))

        um = UserManager()
        um.load()
        self.assertFalse(
            um.load_failed,
            "a registry restored from its backup must not report itself "
            "unreadable")
        self.assertEqual(
            ["u1"], [c["uuid"] for c in um.users[0]["credentials"]],
            "the saved login did not come back from the backup")
        self.assertEqual(
            ["u1"],
            [c["uuid"] for c in
             self.read_users_json()["users"][0]["credentials"]],
            "the recovered registry was not written back")

    def test_a_registry_that_parses_to_nobody_is_damage_not_a_first_run(self):
        """The second damage case the block's own comment names, and
        `load_failed` was set only for the first.

        `{"users": []}` parses, so it reached neither the restore branch nor
        the `data is None` refusal: it fell through to the first-run
        migration, and `save()` rewrote `users.json` **and** its backup with a
        default profile. `load_failed` stayed False, so
        `sync.manager._registry_unreadable()` answered False and
        `SyncManager.start()` migrated the catalog with a resolver that
        answers None for every login -- the precise condition R12 and
        `_actor_resolver`'s "cannot ask, so has not been told nobody"
        distinction exist to make unreachable.
        """
        self.a_saved_registry()
        for name in ("users.json", "users.json.bak"):
            with open(os.path.join(self.tmp, name), "w",
                      encoding="utf-8") as f:
                f.write('{"active": null, "users": []}')

        um = UserManager()
        um.load()
        self.assertTrue(
            um.load_failed,
            "a registry that parses to nobody reported itself readable, so "
            "the catalog would migrate with a resolver that names nobody")
        self.assertTrue(
            any(n.startswith("users.json.unreadable-")
                for n in os.listdir(self.tmp)),
            "the damaged registry was not kept aside")

    def test_a_first_run_with_neither_file_is_still_a_first_run(self):
        """The negative control the two above need. "Damaged" is about a file
        that is *there* and unusable; no registry at all is the ordinary
        first launch, and it must still migrate `cred.json` rather than
        refusing to start."""
        self.write_cred_json([{"Name": "Casper", "Id": "srv1", "uuid": "u1"}])
        um = UserManager()
        um.load()
        self.assertFalse(um.load_failed, "a first run reported itself broken")
        self.assertEqual(["u1"],
                         [c["uuid"] for c in um.users[0]["credentials"]])

    def test_a_primary_that_could_not_be_written_does_not_advance_the_backup(
            self):
        """The invariant the two writes exist to keep: never both bad. If the
        real file did not land, the copy must still hold the payload that
        did."""
        self.a_saved_registry()
        with open(self.backup_path(), encoding="utf-8") as f:
            before = json.load(f)

        um = UserManager()
        um.load()
        um.add_user("Second")                       # saves; still fine
        with mock.patch.object(UserManager, "_write_durably",
                               return_value=False):
            um.add_user("Third")

        with open(self.backup_path(), encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(
            2, len(after["users"]),
            "the backup advanced past a save that never landed")
        self.assertNotEqual(before, after)          # the good save did land


class KnownServersTest(UserManagerTestBase):
    def test_known_servers_deduped_across_users(self):
        self.write_cred_json([
            {"uuid": "a", "address": "http://home:8096/", "Name": "Home"},
        ])
        um = self.fresh()
        kid = um.add_user("Kids")
        um.set_active(kid["id"])
        um.set_active_credentials([
            {"uuid": "b", "address": "http://home:8096", "Name": "Home"},
            {"uuid": "c", "address": "http://remote", "Name": "Remote"},
        ])
        known = {k["address"]: k["name"] for k in um.known_servers()}
        # http://home:8096 appears in two users but is de-duplicated (and its
        # trailing slash normalized away).
        self.assertEqual(known, {
            "http://home:8096": "Home",
            "http://remote": "Remote",
        })

    def test_known_servers_in_public_state(self):
        self.write_cred_json([{"uuid": "a", "address": "http://s", "Name": "S"}])
        um = self.fresh()
        self.assertEqual(um.public_state()["known_servers"],
                         [{"address": "http://s", "name": "S"}])


class ServerNameTest(UserManagerTestBase):
    """What to call a server on screen, from any saved credential.

    Exists for one caller: two servers can hold a playlist with the same id
    **and the same name**, because Jellyfin derives the id from the name, so
    the offline library's two tiles need telling apart by something.
    """

    def test_the_credentials_name_wins(self):
        self.write_cred_json([{"uuid": "a", "Id": "SRV",
                               "address": "http://home:8096", "Name": "Home"}])
        self.assertEqual(self.fresh().server_name_for("SRV"), "Home")

    def test_the_address_is_the_fallback(self):
        """A server that never told us a name still has somewhere it lives."""
        self.write_cred_json([{"uuid": "a", "Id": "SRV",
                               "address": "http://home:8096"}])
        self.assertEqual(self.fresh().server_name_for("SRV"),
                         "http://home:8096")

    def test_an_unknown_server_is_none_and_not_its_id(self):
        """A raw ServerId on a tile is worse than an unqualified one, so the
        caller is handed nothing to put there rather than the id."""
        self.write_cred_json([{"uuid": "a", "Id": "SRV", "Name": "Home"}])
        um = self.fresh()
        self.assertIsNone(um.server_name_for("SOMEWHERE-ELSE"))
        self.assertIsNone(um.server_name_for(None))

    def test_it_spans_profiles(self):
        """Like `server_id_for`, and for its reason: the download catalog is
        one store per machine, so a row can belong to a server only another
        local profile has a login for."""
        self.write_cred_json([{"uuid": "a", "Id": "MINE", "Name": "Mine"}])
        um = self.fresh()
        kid = um.add_user("Kids")
        um.set_active(kid["id"])
        um.set_active_credentials([{"uuid": "b", "Id": "THEIRS",
                                    "Name": "Theirs"}])
        self.assertEqual(um.server_name_for("MINE"), "Mine",
                         "the other profile's server could not be named")
        self.assertEqual(um.server_name_for("THEIRS"), "Theirs")


class PinTest(UserManagerTestBase):
    def test_set_and_verify_pin(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        self.assertFalse(um.is_locked(kid["id"]))
        um.set_pin(kid["id"], "1234", require_startup=True)
        self.assertTrue(um.is_locked(kid["id"]))
        self.assertTrue(um.verify_pin(kid["id"], "1234"))
        self.assertFalse(um.verify_pin(kid["id"], "0000"))
        self.assertFalse(um.verify_pin(kid["id"], None))

    def test_pin_is_hashed_not_plaintext(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        # Not "1234": a four-digit PIN turns up inside a random hex salt or
        # hash about once every seventy runs, and this test used to search
        # the whole serialized entry for it. A PIN that cannot appear in hex
        # makes the search mean what it says.
        pin = "97xz"
        um.set_pin(kid["id"], pin)
        stored = self.read_users_json()
        entry = next(u for u in stored["users"] if u["id"] == kid["id"])
        self.assertNotIn(pin, json.dumps(entry))
        self.assertIsNotNone(entry["pin_hash"])
        self.assertIsNotNone(entry["pin_salt"])
        # And the hash actually corresponds to the PIN + salt.
        self.assertEqual(entry["pin_hash"], _hash_pin(pin, entry["pin_salt"]))

    def test_clear_pin(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        um.set_pin(kid["id"], "1234", require_startup=True)
        um.set_pin(kid["id"], "")
        self.assertFalse(um.is_locked(kid["id"]))
        self.assertFalse(um.get(kid["id"])["require_pin_startup"])

    def test_startup_needs_unlock(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        um.set_active(kid["id"])
        self.assertFalse(um.startup_needs_unlock())  # no pin yet
        um.set_pin(kid["id"], "1234", require_startup=False)
        self.assertFalse(um.startup_needs_unlock())   # locked but not startup
        um.set_pin(kid["id"], "1234", require_startup=True)
        self.assertTrue(um.startup_needs_unlock())

    def test_public_state_hides_secrets(self):
        um = self.fresh()
        kid = um.add_user("Kids")
        um.set_pin(kid["id"], "1234")
        state = um.public_state()
        blob = json.dumps(state)
        self.assertNotIn("pin_hash", blob)
        self.assertNotIn("pin_salt", blob)
        self.assertNotIn("credentials", blob)
        entry = next(u for u in state["users"] if u["id"] == kid["id"])
        self.assertTrue(entry["locked"])


if __name__ == "__main__":
    unittest.main()
