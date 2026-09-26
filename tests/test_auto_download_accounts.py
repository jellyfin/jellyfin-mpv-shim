"""Who unattended downloading is turned on for, and where that is stored.

R14: the allow-list was a comma-separated list of **login uuids** in the
global config, and it is a list of **accounts** in each profile's entry in
`users.json` now. Both halves of that move are pinned here, because both
have a failure that is silent:

- keyed on the login, a server answering at two addresses is two uuids and
  one account, and `clients._connect_all` registers the live client under
  whichever address answered first -- so unattended fetching was configured,
  enabled, and doing nothing for the whole of a trip, with no warning,
  because the allow-list was not empty;
- stored globally, one profile's ticks applied to every profile on the
  machine.

The adoption is one-way and runs at load. Its marker is per profile (`None`
never adopted, `[]` adopted and empty) rather than the config key being
empty, so a clear that could not be written does not undo an untick on the
next launch.
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
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.users import UserManager

#: One account reached at two addresses -- the configuration R14 is about.
LAN = {"uuid": "lan", "Id": "SERVER-A", "UserId": "izzie",
       "address": "http://10.0.0.5:8096"}
WAN = {"uuid": "wan", "Id": "SERVER-A", "UserId": "izzie",
       "address": "https://jf.example.com"}
#: A different box, so "only the ticked one" has something to exclude.
FRIEND = {"uuid": "friend", "Id": "SERVER-B", "UserId": "izzie",
          "address": "http://friend:8096"}
#: Somebody else's profile on the same machine (a shared HTPC).
THEIRS = {"uuid": "theirs", "Id": "SERVER-A", "UserId": "sam",
          "address": "http://10.0.0.5:8096"}


class AutoDownloadStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch(
            "jellyfin_mpv_shim.users.conffile.get",
            side_effect=lambda app, conf_file, create=False: os.path.join(
                self.tmp, conf_file))
        self.addCleanup(patcher.stop)
        patcher.start()
        # conf.json has no path under test, so a real save raises; the
        # adoption's clear is best effort and would only log. Recorded
        # instead, because "the legacy key was cleared" is an assertion.
        self.saves = []
        saver = mock.patch.object(settings, "save",
                                  lambda: self.saves.append(1))
        self.addCleanup(saver.stop)
        saver.start()
        self._legacy = settings.auto_download_servers
        self.addCleanup(setattr, settings, "auto_download_servers",
                        self._legacy)
        settings.auto_download_servers = None

    def write_users(self, *profiles, active=0):
        """One argument per local profile, each a list of credentials.

        Written as a file rather than assigned onto a manager, so `load`
        does the reading and the adoption runs where it really runs.
        """
        users = [{"id": "local%d" % n, "name": "P%d" % n,
                  "device_id": "dev%d" % n, "credentials": list(creds)}
                 for n, creds in enumerate(profiles)]
        path = os.path.join(self.tmp, "users.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"active": users[active]["id"], "users": users}, f)
        return users

    def read_users(self):
        with open(os.path.join(self.tmp, "users.json"), encoding="utf-8") as f:
            return {u["id"]: u for u in json.load(f)["users"]}

    def fresh(self):
        um = UserManager()
        um.load()
        return um


class AdoptionTest(AutoDownloadStoreTest):
    def test_each_profile_takes_only_its_own_share(self):
        """The list was global and the credentials are not, so the uuid is
        what says which profile a tick belonged to. A migration that filed
        everything under the active profile would turn one person's setting
        into everybody's."""
        self.write_users([LAN], [THEIRS, FRIEND])
        settings.auto_download_servers = "lan,friend"
        um = self.fresh()
        stored = self.read_users()
        self.assertEqual(stored["local0"]["auto_download"],
                         [["SERVER-A", "izzie"]])
        self.assertEqual(stored["local1"]["auto_download"],
                         [["SERVER-B", "izzie"]])
        self.assertEqual(um.auto_download_accounts(),
                         {("SERVER-A", "izzie"), ("SERVER-B", "izzie")})

    def test_two_addresses_for_one_server_adopt_as_one_account(self):
        self.write_users([LAN, WAN])
        settings.auto_download_servers = "lan,wan"
        self.fresh()
        self.assertEqual(self.read_users()["local0"]["auto_download"],
                         [["SERVER-A", "izzie"]])

    def test_the_legacy_key_is_cleared(self):
        self.write_users([LAN])
        settings.auto_download_servers = "lan"
        self.fresh()
        self.assertIsNone(settings.auto_download_servers)
        self.assertTrue(self.saves, "the cleared config was never written")

    def test_a_ticked_login_that_no_longer_exists_is_dropped(self):
        """Removing a server does not come back through here, so a uuid
        naming no credential is a tick with nothing behind it. Dropped, and
        said out loud: the symptom otherwise is the feature enabled and
        fetching nothing, which is the bug this whole move is about."""
        self.write_users([LAN])
        settings.auto_download_servers = "lan,gone"
        with self.assertLogs("users", level="WARNING") as caught:
            um = self.fresh()
        self.assertEqual(um.auto_download_accounts(), {("SERVER-A", "izzie")})
        self.assertTrue([m for m in caught.output if "gone" in m],
                        "the lost server was dropped silently")

    def test_a_credential_naming_no_account_is_reported(self):
        self.write_users([{"uuid": "lan", "Id": "SERVER-A"}])
        settings.auto_download_servers = "lan"
        with self.assertLogs("users", level="WARNING"):
            um = self.fresh()
        self.assertEqual(um.auto_download_accounts(), set())

    def test_nothing_ticked_leaves_the_profiles_unadopted(self):
        """An empty key is not an adoption: `None` has to keep meaning "not
        migrated", or the next launch after an upgrade that happened to find
        the list empty would never adopt it."""
        self.write_users([LAN])
        settings.auto_download_servers = ""
        um = self.fresh()
        # In memory, because an adoption that changed nothing does not write
        # the file -- so the key is absent on disk, which `_normalize` reads
        # back as None on the next launch either way.
        self.assertIsNone(um.users[0]["auto_download"])
        self.assertNotIn("auto_download", self.read_users()["local0"])

    def test_an_untick_survives_a_key_that_came_back(self):
        """The clear is best effort -- a session that could not *read*
        conf.json refuses to write it -- so the per-profile marker is what
        makes this idempotent. Without it, unticking the only server would
        be undone on every launch."""
        self.write_users([LAN, WAN])
        settings.auto_download_servers = "lan"
        um = self.fresh()
        self.assertTrue(um.set_auto_download("lan", False))
        settings.auto_download_servers = "lan"      # as if the clear failed
        again = self.fresh()
        self.assertEqual(again.auto_download_accounts(), set())

    def test_a_uuid_already_filed_is_not_reported_lost(self):
        """Reachable only with a clear that did not land and a profile that
        joined after the adoption -- narrow, but the warning tells the user
        their server is gone, and being wrong about that is worse than saying
        nothing. The count is over every profile's credentials, not over the
        ones being adopted on this pass."""
        self.write_users([LAN], [FRIEND])
        path = os.path.join(self.tmp, "users.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["users"][0]["auto_download"] = [["SERVER-A", "izzie"]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        settings.auto_download_servers = "lan"      # as if the clear failed
        with self.assertNoLogs("users", level="WARNING"):
            um = self.fresh()
        self.assertEqual(um.auto_download_accounts(), {("SERVER-A", "izzie")})

    def test_a_first_run_upgrade_adopts_too(self):
        """cred.json plus a configured allow-list is the ordinary upgrade
        path, and it takes the other branch of `load` -- the one that folds
        the legacy credentials into a "(default)" profile and returns early.
        Adopting on only the usual branch would lose the list for exactly
        the installs that have one."""
        with open(os.path.join(self.tmp, "cred.json"), "w",
                  encoding="utf-8") as f:
            json.dump([LAN], f)
        settings.auto_download_servers = "lan"
        um = self.fresh()
        self.assertEqual(um.auto_download_accounts(), {("SERVER-A", "izzie")})


class ReadingTest(AutoDownloadStoreTest):
    def test_a_second_address_is_the_same_permission(self):
        """R14's actual bug. The connect path groups a server's credentials
        by `Id` and registers the client under whichever address answered,
        so asking about the uuid that was ticked is asking about something
        the user does not choose."""
        self.write_users([LAN, WAN])
        settings.auto_download_servers = "lan"
        um = self.fresh()
        self.assertTrue(um.auto_download_on("lan"))
        self.assertTrue(um.auto_download_on("wan"),
                        "the other address for the same account reads as off")

    def test_another_account_on_the_same_server_is_not_included(self):
        """The negative control for the case above: same `Id`, different
        person. Matching on the server alone would have swept it in, which
        is the download explosion single-account auto-download avoids."""
        self.write_users([LAN], [THEIRS])
        settings.auto_download_servers = "lan"
        um = self.fresh()
        self.assertFalse(um.auto_download_on("theirs"))

    def test_an_unticked_server_is_not_included(self):
        self.write_users([LAN, FRIEND])
        settings.auto_download_servers = "lan"
        um = self.fresh()
        self.assertFalse(um.auto_download_on("friend"))

    def test_the_answer_does_not_depend_on_who_is_active(self):
        """R14 asked for a picker gate and then withdrew it: the constraint
        is resource usage, not liveness. So a profile that is not the one in
        the picker still has its accounts in the list."""
        users = self.write_users([LAN], [FRIEND], active=0)
        settings.auto_download_servers = "friend"
        um = self.fresh()
        self.assertEqual(um.active_id, users[0]["id"])
        self.assertEqual(um.auto_download_accounts(), {("SERVER-B", "izzie")})
        self.assertTrue(um.auto_download_on("friend"))

    def test_a_hand_edited_entry_is_skipped_rather_than_raised(self):
        """Read from the download scheduler's thread, where raising would
        stop the whole pass. A two-character string is in here on purpose:
        it unpacks into a pair of characters if the check is only on
        length."""
        self.write_users([LAN])
        um = self.fresh()
        um.users[0]["auto_download"] = [
            ["SERVER-A", "izzie"], "ab", None, ["SERVER-A"], ["", "izzie"],
            ["SERVER-A", "izzie", "extra"]]
        self.assertEqual(um.auto_download_accounts(),
                         {("SERVER-A", "izzie")})

    def test_an_unknown_login_is_off_rather_than_an_error(self):
        self.write_users([LAN])
        um = self.fresh()
        self.assertFalse(um.auto_download_on("nobody"))
        self.assertFalse(um.auto_download_on(None))


class WritingTest(AutoDownloadStoreTest):
    def test_on_is_filed_under_the_profile_holding_the_credential(self):
        """Derivable rather than guessed: a uuid appears in exactly one
        profile's list. Filing it under whoever is active would put the
        setting on the wrong person the moment a switch is in flight."""
        self.write_users([LAN], [FRIEND], active=0)
        um = self.fresh()
        self.assertTrue(um.set_auto_download("friend", True))
        stored = self.read_users()
        self.assertEqual(stored["local1"]["auto_download"],
                         [["SERVER-B", "izzie"]])
        self.assertFalse(stored["local0"].get("auto_download"),
                         "the active profile took the other one's tick")

    def test_off_clears_every_profile_that_holds_the_account(self):
        """Two profiles holding one account hold one fact about it, not two
        -- otherwise unticking the box leaves the account still swept and
        the checkbox is lying."""
        self.write_users([LAN], [WAN])
        settings.auto_download_servers = "lan,wan"
        um = self.fresh()
        self.assertEqual(um.auto_download_accounts(), {("SERVER-A", "izzie")})
        self.assertTrue(um.set_auto_download("wan", False))
        self.assertEqual(um.auto_download_accounts(), set())
        stored = self.read_users()
        self.assertEqual(stored["local0"]["auto_download"], [])
        self.assertEqual(stored["local1"]["auto_download"], [])

    def test_a_write_does_not_mark_another_profile_adopted(self):
        """`[]` means adopted, so writing one into a profile that has not
        been through the migration would throw away its share of the legacy
        list on the next launch."""
        self.write_users([LAN], [FRIEND])
        um = self.fresh()
        um.set_auto_download("lan", True)
        self.assertIsNone(self.read_users()["local1"]["auto_download"])

    def test_ticking_twice_changes_nothing_the_second_time(self):
        self.write_users([LAN])
        um = self.fresh()
        self.assertTrue(um.set_auto_download("lan", True))
        self.assertFalse(um.set_auto_download("lan", True))
        self.assertEqual(self.read_users()["local0"]["auto_download"],
                         [["SERVER-A", "izzie"]])

    def test_an_unresolvable_login_writes_nothing(self):
        self.write_users([{"uuid": "lan", "address": "http://h"}])
        um = self.fresh()
        self.assertFalse(um.set_auto_download("lan", True))
        self.assertFalse(um.set_auto_download("nobody", True))
        self.assertFalse(self.read_users()["local0"].get("auto_download"))

    def test_a_login_removed_and_added_again_keeps_the_setting(self):
        """A behaviour change, and **ruled** -- R22, *"The re-add keeping the
        setting is fine"*.

        Keyed on the uuid, removing a server and adding it back gave it a
        fresh uuid, so unattended downloading came back **off**; keyed on the
        account it stays on, because deleting a connection is not turning the
        setting off. It was put up as a derived consequence of R14's *"the
        account that turns it on gets it"* and confirmed, so this is a pin
        rather than an open question. Re-authentication was never affected
        either way: `open_reauth` keeps the uuid on purpose so the downloads
        survive.
        """
        self.write_users([LAN])
        settings.auto_download_servers = "lan"
        um = self.fresh()
        um.users[0]["credentials"] = [dict(LAN, uuid="lan-again")]
        um.save()
        again = self.fresh()
        self.assertTrue(again.auto_download_on("lan-again"))

    def test_it_survives_a_reload(self):
        """The one that would catch a setting kept only in memory: the write
        has to be in the file the next launch reads."""
        self.write_users([LAN, FRIEND])
        um = self.fresh()
        um.set_auto_download("friend", True)
        self.assertEqual(self.fresh().auto_download_accounts(),
                         {("SERVER-B", "izzie")})


if __name__ == "__main__":
    unittest.main()


class _CountingLock:
    """A lock that records how often it was entered, and still locks."""

    def __init__(self, inner):
        self.inner, self.count = inner, 0

    def __enter__(self):
        self.count += 1
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)


class TheRegistryAnswersEveryLoginInOneLockTest(AutoDownloadStoreTest):
    """**Why there is a batched read at all**, and it is not a micro-optimisation.

    `UserManager._lock` is held by `save()` across two durable file writes and
    two directory fsyncs, which is why `server_id_for` and `actor_for` are
    documented as deliberately never taking it. `auto_download_on` does take
    it, once per call, and the Servers tab asked it once per credential per
    repaint -- so ticking the checkbox (which calls `save()`) blocked the next
    frame's render thread for the whole of that write, once per row.

    The screen-level half is `TheServersScreenAsksTheRegistryOncePerBuildTest`
    in tests/test_server_recovery.py, where the controller is a stand-in and
    the lock cannot be seen.
    """

    def _logins(self, n):
        """``n`` credentials on ``n`` servers, all one person, all ticked."""
        creds = [{"uuid": "u%d" % i, "Id": "SERVER-%d" % i, "UserId": "izzie",
                  "address": "http://h%d" % i} for i in range(n)]
        self.write_users(creds)
        settings.auto_download_servers = ",".join(c["uuid"] for c in creds)
        return self.fresh(), [c["uuid"] for c in creds]

    def _locks(self, um, work):
        counter = _CountingLock(um._lock)
        with mock.patch.object(um, "_lock", counter):
            work()
        return counter.count

    def test_asking_per_login_takes_the_lock_per_login(self):
        """The control, and the shape the render path had. Without this the
        test below could pass against a registry that never locked at all.
        """
        counts = []
        for n in (2, 8):
            um, uuids = self._logins(n)
            counts.append(self._locks(
                um, lambda: [um.auto_download_on(u) for u in uuids]))
        self.assertLess(counts[0], counts[1],
                        "asking per login did not cost per login, so there "
                        "was nothing here to batch")

    def test_and_the_batched_answer_takes_it_once(self):
        counts = []
        for n in (2, 8):
            um, _uuids = self._logins(n)
            counts.append(self._locks(um, um.auto_download_logins))
        self.assertEqual(counts[0], counts[1],
                         "the batched read still costs per login")

    def test_and_it_is_the_same_answer_login_for_login(self):
        """The effect, not the call. R14's point is that the stored unit is
        the **account**: one server at two addresses is two uuids and one
        permission. A batched read keyed on the uuid instead would pass the
        two tests above and undo the ruling, so the parity is asserted against
        `auto_download_on` itself.
        """
        self.write_users([LAN, WAN, FRIEND], [THEIRS])
        settings.auto_download_servers = "lan"
        um = self.fresh()
        self.assertEqual(
            {u for u in ("lan", "wan", "friend", "theirs")
             if um.auto_download_on(u)},
            um.auto_download_logins())
        self.assertEqual({"lan", "wan"}, um.auto_download_logins(),
                         "the second address for one server answered "
                         "differently from the first")
