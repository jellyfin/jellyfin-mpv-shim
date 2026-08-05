"""Stopping playback while in a SyncPlay group: halt, or leave? And which of
the three "is SyncPlay on" questions each caller is entitled to ask.

Halting is jellyfin-web's behaviour and the one we want -- membership is not a
property of playback. But it is only tolerable where the SyncPlay menu is
still reachable afterwards, because that menu is the only way out of a group.
Two surfaces have no such menu: no GUI at all, and a cast to a shim whose
browser was never opened (stopping puts the window away rather than showing
the library). This pins which of the two answers each surface gets, plus the
one stop that never halts: the window closing.
"""

import ast
import os
import sys
import types
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class FakeSyncPlay:
    def __init__(self, in_group=True, following=True):
        self._in_group = in_group
        self.following = following
        self.halted = False
        self.left = False

    def in_group(self):
        return self._in_group

    def is_enabled(self):
        return self._in_group and self.following

    def halt_group_playback(self):
        self.halted = True

    def disable_sync_play(self, from_server):
        assert from_server is False
        self.left = True


def release(reachable, **kw):
    """Run PlayerManager._release_syncplay against the least player it needs.

    Unbound rather than a real PlayerManager: constructing one opens an mpv
    window, and this decision reads two attributes."""
    sp = FakeSyncPlay(**kw)
    player = types.SimpleNamespace(syncplay=sp, syncplay_menu_reachable=reachable)
    PlayerManager._release_syncplay(player)
    return sp


class ReleaseOnStopTests(unittest.TestCase):
    def test_the_browser_halts_rather_than_leaving(self):
        sp = release(lambda: True)
        self.assertTrue(sp.halted)
        self.assertFalse(sp.left, "going back to the library dropped the group")

    def test_no_gui_leaves(self):
        """CLI never sets the hook. A halted group there would be one the user
        has no surface to leave from -- and the OSD menu only exists while
        something is playing, which after a stop it is not."""
        sp = release(None)
        self.assertTrue(sp.left)
        self.assertFalse(sp.halted)

    def test_an_unreachable_menu_leaves(self):
        sp = release(lambda: False)
        self.assertTrue(sp.left)
        self.assertFalse(sp.halted)

    def test_an_already_halted_member_is_left_alone(self):
        """A halted member can go on to play something of their own. That
        video ending must not reach the group at all -- and on a surface
        where we would have LEFT, it would have left mid-film.

        This is why the guard is ``is_enabled`` (SyncPlay is driving this
        player) and not ``in_group`` (we are a member of something)."""
        sp = release(lambda: False, following=False)
        self.assertFalse(sp.left)
        self.assertFalse(sp.halted)

    def test_a_broken_predicate_leaves(self):
        """Fails safe: the recoverable end is leaving a group, not being stuck
        in one."""
        def boom():
            raise RuntimeError("no")

        sp = release(boom)
        self.assertTrue(sp.left)


class ClosingTheWindowLeaves(unittest.TestCase):
    """A closing window always leaves the group, never halts it.

    Halting is for a stop you can come back from -- back to the library,
    where the SyncPlay menu is. When the window goes away the app is quitting
    or going to the tray, and a halted membership there is one nobody can
    see, leave or resume while the group waits on them.
    """

    def close(self, **kw):
        sp = FakeSyncPlay(**kw)
        stopped = []
        player = types.SimpleNamespace(
            syncplay=sp, stop=lambda: stopped.append(1),
            # Deliberately the *opposite* of what the branch needs: if this
            # ever starts deciding the question, the test says so.
            syncplay_menu_reachable=lambda: True)
        PlayerManager.stop_for_window_close(player)
        return sp, stopped

    def test_it_leaves_rather_than_halting(self):
        sp, stopped = self.close()
        self.assertTrue(sp.left, "closing the window only halted the group")
        self.assertFalse(sp.halted)
        self.assertEqual(stopped, [1], "playback was not stopped")

    def test_a_halted_membership_is_left_too(self):
        """The case that most needs it: already backed out, still a member,
        and now the window is going. Nothing would ever leave for us."""
        sp, _stopped = self.close(following=False)
        self.assertTrue(sp.left)

    def test_no_group_is_left_alone(self):
        sp, stopped = self.close(in_group=False)
        self.assertFalse(sp.left)
        self.assertEqual(stopped, [1])


class WhichPredicateThePlayerAsks(unittest.TestCase):
    """``in_group()`` is opt-in by name, and this is what keeps it that way.

    Halting split one question into two: "are we a member" (``in_group``) and
    "is SyncPlay driving this player" (``is_enabled``). Almost every caller in
    player.py wants the second, and one that asks the first instead forwards a
    halted user's private pause/seek to the group -- a bug that is invisible
    locally and only shows up on somebody else's screen.

    So the safe spelling kept the familiar name, and the exceptions are
    enumerated here. A new ``in_group()`` in player.py has to be added below
    with a reason, which is the review this distinction needs.
    """

    #: enclosing function -> why membership rather than "driving" is right
    MEMBERSHIP_CALLERS = {
        "terminate": "shutdown must leave a group we are in whether or not we "
                     "were watching it -- a halted membership is exactly the "
                     "one stop() deliberately left alone",
        "stop_for_window_close": "same reason as terminate: the window is "
                                 "going away, so a halted membership becomes "
                                 "one nobody can see, leave or resume",
    }

    def _syncplay_calls(self):
        """(enclosing function, method) for every ``self.syncplay.x()`` in
        player.py."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "jellyfin_mpv_shim", "player.py")) as fh:
            tree = ast.parse(fh.read())
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def enclosing(node):
            while node is not None:
                if isinstance(node, ast.FunctionDef):
                    return node.name
                node = parent.get(node)
            return "<module>"

        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "syncplay"):
                yield enclosing(node), node.func.attr

    def test_only_the_listed_callers_ask_about_membership(self):
        asked = {fn for fn, method in self._syncplay_calls()
                 if method == "in_group"}
        self.assertEqual(
            asked, set(self.MEMBERSHIP_CALLERS),
            "player.py asks syncplay.in_group() somewhere new. If that code "
            "acts on the group's behalf it wants is_enabled(): in_group() is "
            "true of a halted session, and driving the group from one sends a "
            "user's own pause/seek to everybody else. If it really is a "
            "membership question, add it to MEMBERSHIP_CALLERS with the "
            "reason.")

    def test_the_player_does_ask_the_other_one(self):
        """Positive control: if is_enabled were renamed out from under this,
        the check above would pass by finding nothing."""
        asked = {fn for fn, method in self._syncplay_calls()
                 if method == "is_enabled"}
        self.assertGreater(len(asked), 10,
                           "player.py stopped asking syncplay.is_enabled(); "
                           "this guard is measuring nothing")


if __name__ == "__main__":
    unittest.main()
