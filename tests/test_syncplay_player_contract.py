"""What SyncPlayManager expects of a player, and everyone who has to honour it.

SyncPlay reaches into the player through `self.playerManager.<something>` and
nothing declares what that set is. Two ways for it to rot, and both are quiet:

* **The real player drifts.** `player.py` is the most-edited module here and
  `syncplay.py` is one of the least. Rename or drop something SyncPlay calls
  and nothing complains until a group is actually running -- the call sites
  are on paths (a group Stop, a PrepareSession, a queue update that changes
  the item) that no other test reaches and that a person only reaches by
  watching something with someone else.

* **A stand-in drifts.** Every SyncPlay test drives a fake player, and a fake
  that implements a *subset* of the contract does not leave those paths
  untested -- it makes them raise AttributeError somewhere the test is not
  looking, or never reaches them at all while reporting a pass. All four
  stand-ins were missing `has_video`, `send_timeline`, `timeline_handle` and
  `upd_player_hide` when this test was written, so the entire
  `prepare_session` path was untestable in principle and nothing said so.

The contract is extracted from the source rather than listed here, so it
cannot be wrong about what SyncPlay calls -- only about who answers.
"""

import ast
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (module, class) of every stand-in that is handed to a SyncPlayManager.
STAND_INS = [
    ("tests/test_syncplay_disable.py", "FakePlayer"),
    ("tests/test_syncplay_protocol.py", "FakePlayer"),
    ("tests/_syncplay_network.py", "FakePlayer"),
    ("tests/e2e/_syncplay_live.py", "LivePlayer"),
]


def _parse(rel_path):
    with open(os.path.join(ROOT, rel_path)) as fh:
        return ast.parse(fh.read())


def player_contract():
    """Every `self.playerManager.X` in syncplay.py, read from the source."""
    names = set()
    for node in ast.walk(_parse("jellyfin_mpv_shim/syncplay.py")):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "playerManager"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"):
            names.add(node.attr)
    return names


def instance_attrs(*rel_paths):
    """Every `self.x = ...` in a module.

    `hasattr` on a class cannot see an attribute the constructor creates --
    `PlayerManager.menu` is one, and so is most of SyncPlayManager's state --
    and whitelisting the ones that trip the check turns the guard into a list
    of exceptions that nobody re-derives. Reading the assignments keeps the
    answer true by construction.
    """
    found = set()
    for rel in rel_paths:
        for node in ast.walk(_parse(rel)):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        found.add(target.attr)
            elif (isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Attribute)
                    and isinstance(node.target.value, ast.Name)
                    and node.target.value.id == "self"):
                found.add(node.target.attr)
    return found


#: PlayerManager is composed of mixins, so its attributes are spread across
#: the files that make it up.
PLAYER_SOURCES = ("jellyfin_mpv_shim/player.py",
                  "jellyfin_mpv_shim/player_audio.py",
                  "jellyfin_mpv_shim/player_reporting.py",
                  "jellyfin_mpv_shim/player_window.py")


def class_members(rel_path, class_name):
    """Methods, class attributes and `self.x = ...` of one class.

    Source-level rather than `dir()` on an import: `tests/e2e/_syncplay_live`
    pulls in `_e2e`, which repoints XDG_CONFIG_HOME at a temp directory on
    import. That is correct for the e2e suite and would be poison here.
    """
    for node in ast.walk(_parse(rel_path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            found = {n.name for n in node.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"):
                            found.add(target.attr)
                        elif isinstance(target, ast.Name):
                            found.add(target.id)
            return found
    raise AssertionError("no class %r in %s" % (class_name, rel_path))


class TheContractIsNotEmpty(unittest.TestCase):
    """A guard on the guard: if the extraction ever stops finding anything,
    every test below passes by checking nothing."""

    def test_the_extraction_still_finds_the_contract(self):
        contract = player_contract()
        self.assertGreater(
            len(contract), 10,
            "only found %r in syncplay.py -- the extraction has stopped "
            "matching how the player is reached" % sorted(contract))
        # A few that must always be in it, so a *partial* extraction shows up
        # as well as an empty one.
        for expected in ("set_paused", "seek", "get_time", "get_video"):
            self.assertIn(expected, contract)


class TheRealPlayerHonoursIt(unittest.TestCase):
    def test_player_manager_provides_everything_syncplay_calls(self):
        provided = instance_attrs(*PLAYER_SOURCES)
        missing = sorted(name for name in player_contract()
                         if not hasattr(PlayerManager, name)
                         and name not in provided)
        self.assertEqual(
            missing, [],
            "SyncPlayManager calls PlayerManager.%s, which does not exist. "
            "Every SyncPlay test drives a fake player, so this is invisible "
            "until a real group is running -- and the call sites are on paths "
            "(a group Stop, a PrepareSession, a queue update) nothing else "
            "reaches." % ", PlayerManager.".join(missing))


class TheStandInsHonourIt(unittest.TestCase):
    """Each fake player must implement the whole contract, not the part its
    own tests happen to reach.

    Deliberately strict. The looser rule -- "implement what you need" -- is
    what left every stand-in missing the same four methods, and it means a
    test can never discover that it has started exercising a new path: the
    path raises instead of running.
    """

    def test_every_stand_in_covers_the_contract(self):
        contract = player_contract()
        for rel_path, class_name in STAND_INS:
            with self.subTest(stand_in="%s:%s" % (rel_path, class_name)):
                missing = sorted(contract - class_members(rel_path, class_name))
                self.assertEqual(
                    missing, [],
                    "%s in %s is missing %s. A stand-in that is a subset of "
                    "the real player does not leave those paths untested, it "
                    "makes them raise where nothing is looking."
                    % (class_name, rel_path, missing))


class TheOtherDirection(unittest.TestCase):
    """And what the player calls on syncplay.

    The same rot, the other way round: `player.py` reaches into
    `self.syncplay.<something>` from a dozen places, and a method renamed on
    the SyncPlay side breaks a path only a group ever runs.
    """

    def syncplay_contract(self):
        names = set()
        for rel in ("jellyfin_mpv_shim/player.py",
                    "jellyfin_mpv_shim/player_reporting.py"):
            for node in ast.walk(_parse(rel)):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Attribute)
                        and node.value.attr == "syncplay"
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "self"):
                    names.add(node.attr)
        return names

    def test_syncplay_provides_everything_the_player_calls(self):
        from jellyfin_mpv_shim.syncplay import SyncPlayManager

        contract = self.syncplay_contract()
        self.assertGreater(len(contract), 5, "extraction found %r" % contract)
        provided = instance_attrs("jellyfin_mpv_shim/syncplay.py")
        missing = sorted(name for name in contract
                         if not hasattr(SyncPlayManager, name)
                         and name not in provided)
        self.assertEqual(
            missing, [],
            "player.py calls syncplay.%s, which SyncPlayManager does not "
            "provide" % ", syncplay.".join(missing))


if __name__ == "__main__":
    unittest.main()
