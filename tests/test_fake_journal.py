"""The stand-ins' event journal, and the property that keeps it usable.

`tests/integration/_harness.py` records what every fake was asked to do into
one ordered log. The fakes already recorded *whether* — a list of commands, a
list of played urls, the last value written to an attribute. What none of
that could answer is **in which order**, which is the question this codebase's
worst bugs turn on, and two recorders on two objects cannot be compared at
all.

The journal is pure logic, so it is tested here rather than in the matrix.

**The load-bearing property is tolerance.** Assertions are subsequences, so
an event added later cannot fail an assertion written earlier. Get that wrong
and the journal becomes a tax paid on every change — and the way that tax
gets paid is by deleting assertions, which is worse than never having had
them. Half the tests below are about the tolerance rather than the ordering.
"""

import threading
import unittest

from tests.integration._harness import Journal, fake_mpv_class


class MatchingTest(unittest.TestCase):
    """A pattern is a prefix of the rendered event, cut at a component
    boundary — so one event can be asked about at four levels of precision."""

    def setUp(self):
        self.journal = Journal()
        self.journal.record("mpv", "set", "keepaspect", True)

    def test_every_level_of_precision_finds_it(self):
        for pattern in ("mpv", "mpv.set", "mpv.set:keepaspect",
                        "mpv.set:keepaspect=True"):
            with self.subTest(pattern=pattern):
                self.journal.happened(pattern)

    def test_a_pattern_does_not_run_past_a_component(self):
        """`mpv.set:sub` must not match `sub_scale`. A bare `startswith`
        matches it, and the mistake is invisible: the assertion passes, on
        the wrong property."""
        journal = Journal()
        journal.record("mpv", "set", "sub_scale", 1.0)
        journal.never("mpv.set:sub")
        journal.happened("mpv.set:sub_scale")

    def test_a_wrong_value_is_a_different_event(self):
        self.journal.never("mpv.set:keepaspect=False")


class ToleranceTest(unittest.TestCase):
    """The reason assertions are subsequences.

    Every test here writes an assertion, then adds events the assertion
    never mentioned, and requires it to still hold. That is what makes it
    safe to record something new in the fakes without auditing every test
    that reads the journal.
    """

    def _ordered(self):
        journal = Journal()
        journal.record("mpv", "set", "volume", 100)
        journal.record("mpv", "play", "http://x")
        return journal

    def test_events_added_between_do_not_break_an_ordering(self):
        journal = self._ordered()
        journal.order("mpv.set:volume", "mpv.play")

        later = Journal()
        later.record("mpv", "set", "volume", 100)
        later.record("mpv", "set", "hwdec", "no")     # new, unmentioned
        later.record("menu", "hide")                  # new, another source
        later.record("mpv", "play", "http://x")
        later.order("mpv.set:volume", "mpv.play")

    def test_events_added_before_and_after_do_not_break_an_ordering(self):
        journal = Journal()
        journal.record("mpv", "observe", "duration")  # new, before
        journal.record("mpv", "set", "volume", 100)
        journal.record("mpv", "play", "http://x")
        journal.record("mpv", "set", "pause", False)  # new, after
        journal.order("mpv.set:volume", "mpv.play")

    def test_a_whole_new_source_does_not_break_anything(self):
        journal = self._ordered()
        journal.record("downloads", "enqueue", "item-1")
        journal.order("mpv.set:volume", "mpv.play")
        journal.never("menu")

    def test_the_same_event_happening_again_does_not_break_an_ordering(self):
        """A retry, a re-arm, a second episode. An assertion about the first
        one must not start failing because there is now a second."""
        journal = self._ordered()
        journal.record("mpv", "set", "volume", 100)
        journal.record("mpv", "play", "http://y")
        journal.order("mpv.set:volume", "mpv.play")


class OrderingTest(unittest.TestCase):

    def _journal(self):
        journal = Journal()
        journal.record("menu", "hide")
        journal.record("mpv", "set", "volume", 100)
        journal.record("mpv", "play", "http://x")
        return journal

    def test_the_wrong_order_fails(self):
        journal = self._journal()
        with self.assertRaises(AssertionError):
            journal.order("mpv.play", "mpv.set:volume")

    def test_a_missing_event_fails(self):
        journal = self._journal()
        with self.assertRaises(AssertionError):
            journal.order("mpv.set:volume", "mpv.terminate")

    def test_the_failure_message_carries_the_journal(self):
        """A bare "expected X before Y" is unactionable: what the reader
        needs is what DID happen, which is the whole point of keeping a log
        at all."""
        journal = self._journal()
        with self.assertRaises(AssertionError) as caught:
            journal.order("mpv.play", "menu.hide")
        message = str(caught.exception)
        self.assertIn("menu.hide", message)
        self.assertIn("mpv.play:http://x", message)

    def test_a_repeat_means_and_then_another(self):
        journal = Journal()
        journal.record("mpv", "play", "a")
        journal.record("mpv", "play", "b")
        journal.order("mpv.play", "mpv.play")

        single = Journal()
        single.record("mpv", "play", "a")
        with self.assertRaises(AssertionError):
            single.order("mpv.play", "mpv.play")

    def test_an_ordering_of_one_is_refused(self):
        """It is satisfied by anything that happened at all, so it is a test
        that cannot fail. `happened` is the honest way to say it."""
        journal = self._journal()
        with self.assertRaises(AssertionError) as caught:
            journal.order("mpv.play")
        self.assertIn("at least two", str(caught.exception))


class MarkerTest(unittest.TestCase):

    def test_a_mark_takes_its_place_in_the_sequence(self):
        """What lets an ordering claim name a moment nothing else names —
        "this happened after the thing I did", where the only thing that
        identifies "the thing I did" is the test."""
        journal = Journal()
        journal.record("mpv", "set", "keepaspect", False)
        journal.mark("handed to video")
        journal.record("mpv", "set", "keepaspect", True)
        journal.order("mpv.set:keepaspect=False", "test:handed to video",
                      "mpv.set:keepaspect=True")

    def test_since_narrows_the_journal_to_what_followed(self):
        """"...and then it never happened again", which `never` over the
        whole log cannot say: the event usually did happen, which is why
        there is something to be after."""
        journal = Journal()
        journal.record("mpv", "set", "background_color", "#141414")
        journal.mark("handed over")
        journal.record("mpv", "set", "background_color", "#000000")

        journal.happened("mpv.set:background_color='#141414'")
        journal.since("test:handed over").never(
            "mpv.set:background_color='#141414'")
        journal.since("test:handed over").happened(
            "mpv.set:background_color='#000000'")

    def test_since_a_marker_that_never_happened_is_an_error(self):
        """Not an empty journal. Every assertion on a view of nothing
        passes, so the vacuous answer is a test that cannot fail and reads
        exactly like one that does."""
        journal = Journal()
        journal.record("mpv", "play", "a")
        with self.assertRaises(AssertionError):
            journal.since("test:never marked")

    def test_a_mark_cannot_be_confused_with_a_fake(self):
        journal = Journal()
        journal.mark("play")
        journal.never("mpv")
        journal.happened("test:play")


class ConcurrencyTest(unittest.TestCase):
    def test_every_record_from_every_thread_lands(self):
        """The harness fires observers off other threads on purpose — that
        is what makes the player's races reproducible — so the journal is
        written concurrently by construction. A list append is atomic under
        the GIL today; the lock is what stops that being the reason."""
        journal = Journal()
        threads = [threading.Thread(target=lambda n=n: [
            journal.record("t%d" % n, "tick", i) for i in range(50)])
            for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(journal.entries()), 400)


class FakeMPVJournalTest(unittest.TestCase):
    """The fake writes what the shim did, and only what the shim did."""

    def setUp(self):
        self.player = fake_mpv_class()()

    def test_the_constructors_own_defaults_are_not_events(self):
        """Two dozen entries every test has to read past, none of which
        anything did."""
        self.assertEqual(self.player.journal.entries(), [])

    def test_a_property_write_is_recorded(self):
        self.player.keepaspect = False
        self.player.journal.happened("mpv.set:keepaspect=False")

    def test_the_fakes_own_bookkeeping_is_not(self):
        self.player._sections["x"] = {}
        self.player.journal.never("mpv.set:_sections")

    def test_a_write_and_a_notification_are_different_events(self):
        """`set:` is the shim writing a property; `prop:` is mpv reporting
        one. Opposite directions through the same name, and a test about one
        must not match the other -- `fire_property` has to assign the
        attribute to be useful, which is exactly how they would collide."""
        self.player.fire_property("pause", True)
        self.player.journal.happened("mpv.prop:pause=True")
        self.player.journal.never("mpv.set:pause")

        self.player.pause = False
        self.player.journal.happened("mpv.set:pause=False")

    def test_the_fs_alias_is_recorded_under_the_property_it_writes(self):
        self.player.fs = True
        self.player.journal.happened("mpv.set:fullscreen=True")

    def test_commands_and_playback_share_the_stream(self):
        self.player.command("stop")
        self.player.play("http://x")
        self.player.journal.order("mpv.cmd:stop", "mpv.play:http://x")


if __name__ == "__main__":
    unittest.main()
