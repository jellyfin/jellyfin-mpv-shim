"""Chapter navigation over media that really has chapters.

``chapter_target`` is pure and well covered by the fast suite. What is not
covered anywhere is everything on either side of it: the chapter list comes
out of **mpv**, not the server, and the answer goes back through ``seek``.
Both ends are where the two chapter bugs actually lived.

* **#614, the dead zone.** ``ch["time"] > pos + 0.5`` made the last half
  second of every chapter a stretch where the forward button did nothing.
  Half a second of real playback, which reads as "the button sometimes
  doesn't work" rather than as a rule. A synthetic chapter list can be
  positioned exactly; only real media makes the boundary a float that mpv
  chose.
* **#614 again, the negative first chapter.** A matroska chapter can start
  at a slightly negative timestamp -- container start-time offsets put the
  first one at -0.005 -- and mpv reads a negative *absolute* seek as the end
  of the file. So "previous chapter" reached EOF, the EOF observer advanced
  the queue, and the reported symptom was **prev-chapter playing the next
  episode**. Nothing short of a real container and a real mpv can produce
  that timestamp: it is an artefact of the file, not of the code.

The fixture is `Twelve chapters` (four minutes, twelve of them) from Test
Media, with `Three hours` (36) for the far end of a long file.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

LIBRARY = "Test Media"
#: How long mpv is given to complete an exact absolute seek.
SEEK_TIMEOUT = 15


class _ChapterTest(_e2e.E2ETestCase):
    """One chaptered item, playing, paused, with its chapter list read from
    mpv rather than from the DTO -- because that is where the navigation
    reads it, and the two do not have to agree."""

    ITEM = None

    def setUp(self):
        super().setUp()
        self.item = self.session.find(self.ITEM, library=LIBRARY)
        media = _e2e.build_media(self.session, [self.item["Id"]])
        self.video = media.video
        self.pm.play(self.video, is_initial_play=True)
        self.assertTrue(
            _e2e.wait_for(lambda: self.pm._player.duration),
            "mpv never opened %r" % self.ITEM)
        # Paused throughout: every assertion here is about *where* a jump
        # landed, and a playing file moves under the assertion. The seeks
        # themselves are unaffected -- mpv seeks a paused file.
        self.pm.set_paused(True)
        self.marks = [float(ch.get("time") or 0.0)
                      for ch in (self.pm._player.chapter_list or [])]
        self.assertGreaterEqual(
            len(self.marks), 4,
            "%r reported %d chapters to mpv; this test needs a chaptered "
            "file" % (self.ITEM, len(self.marks)))

    # -- driving -----------------------------------------------------------

    def goto(self, pos, delta=1.0):
        """Put the playhead at ``pos`` and wait for mpv to actually be there.

        Asserting on ``playback_time`` right after a seek reads the position
        the file was at *before* it -- an exact seek is not instant, and the
        flake that produces looks exactly like a chapter jump landing wrong.

        ``delta`` is how close is close enough. A whole second is fine when
        the next assertion is a boundary tens of seconds away; it is not
        when the position is being compared against the two-second grace,
        where landing on the wrong side of it changes the right answer.
        """
        self.pm.seek(pos, absolute=True)
        self.assertTrue(
            _e2e.wait_for(lambda: abs((self.pm._player.playback_time or -99)
                                      - pos) < delta, timeout=SEEK_TIMEOUT),
            "mpv never reached %.3fs (still at %s)"
            % (pos, self.pm._player.playback_time))

    def jump(self, direction):
        """A chapter jump, and where it left the playhead."""
        before = self.pm._player.playback_time
        self.pm.chapter_seek(direction)
        _e2e.wait_for(
            lambda: abs((self.pm._player.playback_time or 0) - (before or 0))
            > 0.5, timeout=SEEK_TIMEOUT)
        return self.pm._player.playback_time


@_e2e.require_server_and_mpv
class ChapterNavigationTest(_ChapterTest):
    """Twelve chapters, four minutes."""

    ITEM = "Twelve chapters"

    def test_mpv_and_the_server_describe_the_same_chapters(self):
        """The HUD's ticks come from mpv; the chapter *picker* lists the
        server's `Chapters`. They are two readings of one file, and a
        mismatch is a picker whose entries do not go where they say."""
        detail = self.session._request(
            "/Items/%s?Fields=Chapters" % self.item["Id"])
        server = [c.get("StartPositionTicks", 0) / 1e7
                  for c in (detail.get("Chapters") or [])]
        self.assertEqual(len(server), len(self.marks),
                         "the server lists %d chapters and mpv %d"
                         % (len(server), len(self.marks)))
        for index, (theirs, ours) in enumerate(zip(server, self.marks)):
            self.assertAlmostEqual(
                theirs, ours, delta=1.0,
                msg="chapter %d is at %.3fs per the server and %.3fs per "
                    "mpv" % (index, theirs, ours))

    def test_next_chapter_lands_on_the_following_boundary(self):
        start, boundary = self.marks[2], self.marks[3]
        self.goto((start + boundary) / 2.0)
        self.assertAlmostEqual(self.jump(1), boundary, delta=1.0)

    def test_next_chapter_still_works_in_the_last_half_second(self):
        """#614. The tolerance that used to sit here made the half second
        before every boundary a dead zone -- long enough to be noticed and
        short enough to look intermittent."""
        boundary = self.marks[3]
        self.goto(boundary - 0.25)
        landed = self.jump(1)
        self.assertAlmostEqual(
            landed, boundary, delta=1.0,
            msg="a quarter second before a boundary, Next Chapter left the "
                "playhead at %s" % landed)

    def test_next_chapter_at_the_end_of_the_file_does_not_move(self):
        """There is nowhere ahead, and a button that silently restarts the
        file (or seeks to 0) is worse than one that declines."""
        last = self.marks[-1]
        self.goto(last + 1.0)
        before = self.pm._player.playback_time
        self.pm.chapter_seek(1)
        self.assertFalse(
            _e2e.wait_for(lambda: abs((self.pm._player.playback_time or 0)
                                      - before) > 2.0, timeout=3),
            "Next Chapter moved the playhead from the last chapter")

    def test_previous_chapter_restarts_the_chapter_you_are_in(self):
        start = self.marks[3]
        self.goto(start + 5.0)
        self.assertAlmostEqual(self.jump(-1), start, delta=1.0)

    def test_previous_chapter_inside_the_grace_goes_back_one(self):
        """The asymmetry is deliberate and is what every player does: back
        restarts the current chapter unless you are in its first couple of
        seconds, where you meant the one before."""
        self.goto(self.marks[3] + 1.0)
        self.assertAlmostEqual(self.jump(-1), self.marks[2], delta=1.0)

    def test_previous_chapter_at_the_start_declines_rather_than_seeking(self):
        """Before the first boundary there is nowhere to go, and a button
        that quietly restarts the file is worse than one that declines.

        This is **not** the negative-timestamp half of #614, and it cannot
        be: both chaptered fixtures in this library start their first
        chapter at exactly 0.0, and no episode here has chapters at all.
        Reproducing that one needs a container whose start-time offset puts
        chapter zero at -0.005, which mpv then resolves as the END of the
        file -- so prev-chapter reached EOF and the queue advanced. Until
        such a fixture exists the clamp is covered only by the fast suite,
        against a synthetic list. Asserted on the observable anyway (a
        different item playing is what the user saw), so this test starts
        catching it the day the fixture lands.
        """
        self.goto(1.0, delta=0.3)
        started_on = self.video
        before = self.pm._player.playback_time
        self.assertLess(before, 2.0,
                        "this test has to start inside the two-second grace "
                        "or there IS an earlier chapter to go to")
        self.pm.chapter_seek(-1)
        for _ in range(3):
            self.pm.update()
        pos = self.pm._player.playback_time or 0.0
        self.assertIs(self.pm._video, started_on,
                      "previous chapter near the start of the file left a "
                      "different item playing")
        self.assertLess(
            pos, (self.pm._player.duration or 0) / 2.0,
            "previous chapter near the start of the file landed at %.1fs of "
            "%.1fs -- a negative seek resolved as the end of the file"
            % (pos, self.pm._player.duration or 0))
        self.assertFalse(bool(self.pm._player.eof_reached),
                         "previous chapter near the start reached EOF")
        # Against where it WAS, not against a constant: the point is that
        # nothing moved. Compared to 0.0 with any usable tolerance, a jump
        # to the start of the file from a position near it is dangerously
        # indistinguishable from having declined.
        self.assertAlmostEqual(
            pos, before, delta=0.3,
            msg="there was no earlier chapter to go to and the playhead "
                "moved anyway, from %.2fs to %.2fs" % (before, pos))

    def test_walking_forward_through_the_file_visits_each_boundary(self):
        """One jump cannot see a walk that stalls or doubles.

        The failure this catches is a jump computed from something other
        than the live position -- a captured start, a cached chapter index --
        which is right once and then either sticks or accelerates. Three
        presses in a row is the cheapest thing that tells them apart.
        """
        self.goto(self.marks[1] + 1.0)
        landed = [self.jump(1) for _ in range(3)]
        for got, want in zip(landed, self.marks[2:5]):
            self.assertAlmostEqual(got, want, delta=1.0)


@_e2e.require_server_and_mpv
class LongFileChapterTest(_ChapterTest):
    """Three hours, 36 chapters -- the far end of a real file.

    Separate from the four-minute case because the arithmetic is where it
    stops being interchangeable: mpv reports position as a float, and a jump
    computed by index rather than by time is right near the start and drifts
    where the numbers get large.
    """

    ITEM = "Three hours"

    def test_a_jump_deep_into_a_long_file_lands_on_its_boundary(self):
        target = self.marks[len(self.marks) // 2]
        self.goto(target - 30.0)
        self.assertAlmostEqual(self.jump(1), target, delta=1.5)

    def test_previous_chapter_deep_in_a_long_file_restarts_that_chapter(self):
        start = self.marks[len(self.marks) // 2]
        self.goto(start + 45.0)
        self.assertAlmostEqual(self.jump(-1), start, delta=1.5)


if __name__ == "__main__":
    unittest.main()
