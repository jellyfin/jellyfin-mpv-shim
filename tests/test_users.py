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
        with open(os.path.join(self.tmp, "cred.json"), "w") as f:
            json.dump(data, f)

    def read_users_json(self):
        with open(os.path.join(self.tmp, "users.json")) as f:
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
