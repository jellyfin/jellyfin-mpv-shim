"""Which keys currently mean pause/seek/fullscreen (#16).

The shim bound `space`, `f` and the arrows unconditionally, which swallowed
mpv's defaults and, worse, whatever the user had put on those keys. The
answer is to ask mpv rather than to parse a config: `input-bindings` is the
*resolved* set, so there is no precedence model of ours to drift.

Two properties matter more than the parsing: **under-claiming is the safe
direction** (a key we fail to claim costs a report; a key we claim wrongly
is one we stole for no reason, which is what #16 removes), and a claim must
carry the command that was already bound so re-issuing it preserves the
user's meaning.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import keysweep                        # noqa: E402
from jellyfin_mpv_shim.keysweep import (                      # noqa: E402
    FULLSCREEN, PAUSE, SEEK)


def weak(key, cmd, priority=-1):
    return {"key": key, "cmd": cmd, "section": "default",
            "is_weak": True, "priority": priority}


def strong(key, cmd, priority=0):
    return {"key": key, "cmd": cmd, "section": "default",
            "is_weak": False, "priority": priority}


class ClassifyTest(unittest.TestCase):
    def test_the_three_semantics(self):
        self.assertEqual(keysweep.classify("cycle pause"), PAUSE)
        self.assertEqual(keysweep.classify("set pause yes"), PAUSE)
        self.assertEqual(keysweep.classify("cycle fullscreen"), FULLSCREEN)
        self.assertEqual(keysweep.classify("seek  5"), SEEK)
        self.assertEqual(keysweep.classify("seek -60 exact"), SEEK)

    def test_prefixes_are_stripped_not_matched(self):
        # `no-osd cycle pause` means the same thing to us as `cycle pause`,
        # and a real input.conf is full of these.
        self.assertEqual(keysweep.classify("no-osd cycle pause"), PAUSE)
        self.assertEqual(keysweep.classify("repeatable seek 5"), SEEK)
        self.assertEqual(keysweep.classify("osd-msg-bar seek -5"), SEEK)

    def test_anything_unrecognised_is_left_alone(self):
        """Under-claiming is the safe direction. Every one of these touches
        playback in some way and none of them is the thing we need to hear
        about; claiming one would steal a key for no reason."""
        for cmd in ("frame-step", "frame-back-step", "playlist-next",
                    "quit", "cycle mute", "add volume 5",
                    "script-binding uosc/menu", "cycle-values loop-file",
                    "revert-seek", "ab-loop", "", None,
                    'cycle-values sub-ass-override "force" "scale"'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(keysweep.classify(cmd))

    def test_an_unbalanced_quote_is_unreadable_not_interesting(self):
        self.assertIsNone(keysweep.classify('cycle pause "'))

    def test_a_property_verb_with_no_property_is_not_a_claim(self):
        self.assertIsNone(keysweep.classify("cycle"))
        self.assertIsNone(keysweep.classify("set"))


class PrecedenceTest(unittest.TestCase):
    def test_the_users_binding_beats_mpvs_default(self):
        """The whole point: follow a remapped key. Somebody who moved pause
        to `p` should get SyncPlay-aware pause on `p`."""
        # The winner is listed FIRST, so list order contradicts the answer.
        # Ordered the other way these pass against no precedence logic at
        # all -- last-one-wins gives the same result, which is how the first
        # version of this file survived having _rank stubbed out.
        bindings = [strong("SPACE", "cycle mute"),
                    weak("SPACE", "cycle pause"),
                    strong("p", "cycle pause")]
        got = dict((k, s) for k, s, _c in
                   keysweep.sweep(bindings, {PAUSE}))
        self.assertEqual(got, {"p": PAUSE},
                         "SPACE was rebound to mute and must not be claimed")

    def test_a_key_the_user_disabled_is_not_claimed(self):
        # `LEFT ignore` is how somebody turns a default off.
        bindings = [strong("LEFT", "ignore"), weak("LEFT", "seek -5")]
        self.assertEqual(keysweep.sweep(bindings, {SEEK}), [])

    def test_higher_priority_wins_among_equals(self):
        bindings = [strong("f", "cycle mute", priority=7),
                    strong("f", "cycle fullscreen", priority=0)]
        self.assertEqual(keysweep.sweep(bindings, {FULLSCREEN}), [])

    def test_is_mpv_default_answers_the_migrations_question(self):
        bindings = [weak("f", "cycle fullscreen"),
                    strong("SPACE", "cycle pause"),
                    weak("SPACE", "cycle pause")]
        self.assertTrue(keysweep.is_mpv_default(bindings, "f"))
        self.assertFalse(keysweep.is_mpv_default(bindings, "SPACE"),
                         "the user bound this one; writing over it would be "
                         "the same rudeness in a new place")
        self.assertFalse(keysweep.is_mpv_default(bindings, "nosuchkey"))


class ActionTest(unittest.TestCase):
    """The *intent*, not just the category. A claim substitutes the shim's
    own SyncPlay-aware operation for the binding, so the two have to mean
    the same thing."""

    def test_a_play_only_key_is_not_a_toggle(self):
        # PLAYONLY is `set pause no`. Answering it with a toggle would
        # pause a playing file from the key whose entire job is not to.
        self.assertEqual(keysweep.action("set pause no"), (PAUSE, False))
        self.assertEqual(keysweep.action("set pause yes"), (PAUSE, True))
        self.assertEqual(keysweep.action("cycle pause"), (PAUSE, None))

    def test_a_seek_carries_its_amount_and_exactness(self):
        self.assertEqual(keysweep.action("seek -5"), (SEEK, (-5.0, False)))
        self.assertEqual(keysweep.action("seek 1 exact"), (SEEK, (1.0, True)))

    def test_an_absolute_seek_is_left_alone(self):
        """The shim's seek is relative; there is no honest translation, so
        the key stays the user's."""
        for cmd in ("seek 50 absolute-percent", "seek 0 absolute",
                    "seek 10 relative-percent"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(keysweep.action(cmd))

    def test_an_unparseable_amount_is_left_alone(self):
        self.assertIsNone(keysweep.action("seek ${duration}"))
        self.assertIsNone(keysweep.action("seek"))

    def test_a_set_to_something_that_is_not_a_bool_is_left_alone(self):
        self.assertIsNone(keysweep.action("set pause ${x}"))


class SectionTest(unittest.TestCase):
    def test_the_key_travels_in_the_message(self):
        """So the handler can re-issue what was bound to it. Without the
        key, a claim substitutes our verb for theirs, which is the thing
        this whole exercise removes."""
        claims = [("SPACE", PAUSE, None)]
        line = keysweep.section_lines(claims, "jms-key")
        self.assertEqual(line, "SPACE script-message jms-key pause SPACE")

    def test_a_key_name_with_a_space_survives(self):
        claims = [("Shift+LEFT", SEEK, (-5.0, False))]
        self.assertIn("Shift+LEFT", keysweep.section_lines(claims, "jms-key"))

    def test_it_is_stable_across_calls(self):
        # The section is redefined whenever a claim changes; an unstable
        # order would make every redefine look like a change.
        claims = keysweep.sweep(
            [weak("f", "cycle fullscreen"), weak("SPACE", "cycle pause"),
             weak("UP", "seek 60")], {PAUSE, SEEK, FULLSCREEN})
        self.assertEqual([c[0] for c in claims], ["f", "SPACE", "UP"])


class AgainstRealMpvTest(unittest.TestCase):
    """The sweep against a real mpv's real defaults.

    Pinned because the payoff is bigger than the set the shim hard-coded,
    and that is the argument for the whole approach: SyncPlay hears none of
    these today.
    """

    def setUp(self):
        try:
            import mpv
        except OSError:                                  # pragma: no cover
            self.skipTest("libmpv not loadable")
        self.mpv = mpv.MPV(vo="null", config=False, idle=True)
        self.addCleanup(self.mpv.terminate)

    def test_it_finds_more_than_the_shim_hard_coded(self):
        claims = keysweep.sweep(
            self.mpv.input_bindings, {PAUSE, SEEK, FULLSCREEN})
        keys = {k for k, _s, _c in claims}
        # The set the shim bound by hand...
        self.assertTrue({"SPACE", "f", "LEFT", "RIGHT", "UP", "DOWN"} <= keys)
        # ...and the ones it never did. A user pausing with `p`, the media
        # key or the mouse is not reported to a SyncPlay group today.
        for missed in ("p", "PLAYPAUSE", "REWIND", "Shift+LEFT"):
            self.assertIn(missed, keys)
        # ...but NOT the pointer. That belongs to the renderer -- see
        # _is_pointer, and #1, whose whole subject was that ownership.
        self.assertFalse([k for k in keys if k.startswith(("MBTN_", "WHEEL_"))],
                         "a claim on the pointer would fight mpvtk_mouse")

    def test_it_does_not_claim_the_whole_keyboard(self):
        claims = keysweep.sweep(
            self.mpv.input_bindings, {PAUSE, SEEK, FULLSCREEN})
        # 192 bindings in a stock mpv; a sweep claiming a large fraction of
        # them would mean classify() is too loose, which is the failure
        # direction that costs the user their config.
        self.assertLess(len(claims), len(self.mpv.input_bindings) // 4)


if __name__ == "__main__":
    unittest.main()
