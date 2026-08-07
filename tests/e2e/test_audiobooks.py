"""Audiobook playback, resume and finishing, against a real server.

An `AudioBook` is an ordinary `Audio` item — so playback needed no new code
and there is nothing here about streams or transcodes. What *is* different,
and what this suite exists for, is the **user data**: Jellyfin has a resume
rule specific to `AudioBook` that behaves nothing like the video one, and
every "where was I in this book" question in the browser is built on it.

``UserDataManager.UpdatePlayState`` (verified against ``~/Desktop/jellyfin``
and empirically below):

* video is clamped by *percentages* — `MinResumePct` 5, `MaxResumePct` 90,
  plus a `MinResumeDurationSeconds` of 300;
* an **audiobook is clamped by MINUTES** — `MinAudiobookResume` 5 and
  `MaxAudiobookResume` 5. A position less than five minutes in is discarded;
  a position with less than five minutes left is discarded *and the book is
  marked finished*.

The consequence is sharp and non-obvious: **an audiobook shorter than ten
minutes can never hold a resume position at all**, and one shorter than five
minutes can never even be marked finished by playing it. That is why this
suite uses the long fixtures and the short ones are pinned as the negative
case rather than quietly avoided — a test written against a four-minute book
fails in a way that reads as a client bug and is not one. It cost an
afternoon to find; the tests below are what stop it costing another.

Fixtures, all from stdjflib's `Books` library, by name:

* `The Overnight Vigil` — one 24-minute `.m4b` with 6 embedded chapters.
  Long enough to hold a position (6:00), to be finished by playing it
  (20:00), and to have a position ignored as "just started" (2:00).
* `The Slow Crossing Part 01..03` — a 12-minute-per-part rip, joined only by
  `Album`. Each part is long enough to be finished by playback, which is
  what makes *folder-level* resume — the first part not yet finished —
  testable at all.
* `The Lantern Keeper` (4 min) and `Chapter 01..06` (20 s) — the short pair,
  here only to pin that they cannot resume.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LIBRARY = "Books"
TICKS = 10_000_000
MINUTE = 60 * TICKS

LONG_M4B = "The Overnight Vigil"          # 24 min, 6 chapters, one file
RIP_PARTS = ["The Slow Crossing Part %02d" % i for i in (1, 2, 3)]
SHORT_M4B = "The Lantern Keeper"          # 4 min: too short to ever resume

#: An author folder holding three DIFFERENT books, one file each. The shape
#: the "is this one book or several?" rule exists for, and the one no
#: fixture had until it was asked for.
SEVERAL = ["The Copper Bell", "The Winter Ferry", "The Paper Bridge"]
#: ...and one holding two loose books AND a rip in a subfolder, so its
#: children are not even all audiobooks.
MIXED = ["The Glass Orchard", "The Quiet Ledger"]


class _AudiobookCase(unittest.TestCase):

    ACCOUNT = "qa-user"

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session(cls.ACCOUNT)
        cls.source = cls.session.library_source()
        cls.source.get_libraries(_e2e.SOURCE_UUID)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    @property
    def uuid(self):
        return _e2e.SOURCE_UUID

    def audiobooks(self, fields="Path,Album,ParentId"):
        return {i["Name"]: i for i in self.session.find_all(
            library=LIBRARY, item_type="AudioBook", fields=fields)}

    def book(self, name, fields="Path,Album,ParentId"):
        found = self.audiobooks(fields).get(name)
        if found is None:
            self.skipTest(
                "no audiobook named %r — this library predates stdjflib's "
                "long audiobook fixtures, which the resume rule needs; "
                "rebuild it with `stdjflib build`" % name)
        return found

    # -- server state ------------------------------------------------------

    def clear(self, *item_ids):
        """Back to untouched: no position AND not finished.

        Two calls, because they are two different endpoints. Posting
        ``{"Played": false}`` to `UserItems/{id}/UserData` does NOT un-play
        an item — `DELETE /UserPlayedItems/{id}` is what does, and a suite
        that used only the merge left every test after the first
        "finished" one running against a book that was already read.
        """
        for item_id in item_ids:
            # Close the playback session FIRST. A progress report opens one
            # on the server, and while it is open the item's state is
            # rewritten from it -- so clearing user data under a live
            # session for the same item is undone a moment later, and the
            # next test reads back the position (or the finished flag) the
            # PREVIOUS one reported. That is what made the test after a
            # "finished" one fail deterministically, and it looked exactly
            # like a resume-rule bug.
            self.session.api.session_stop(
                {"ItemId": item_id, "PositionTicks": 0})
            self.session.api.item_played(item_id, False)
            self.session.api.update_userdata_for_item(
                item_id, {"PlaybackPositionTicks": 0})
            # Wait for the clear to be VISIBLE, not merely sent. The
            # server's user-data writes are not ordered against a read that
            # follows immediately, so a previous test's "finished" write
            # could land after this one and leave the next test running
            # against a book that was already read -- which it did, as an
            # intermittent failure in whichever test happened to run after
            # the finishing one. A precondition that is not waited for is
            # the same class of mistake as a wait satisfied by stale
            # evidence.
            settled = _e2e.wait_for(
                lambda i=item_id: self.stored(i) == (0, False), timeout=10)
            self.assertTrue(
                settled,
                "could not return %s to an untouched state" % item_id)

    def seed(self, item_id, position=None, played=None):
        """Put the server in a state playback would have produced.

        A *write*, not a report: the report path is what the resume rule
        clamps, and several tests here need a book already sitting at a
        position so the browser can be asked what it makes of one.
        """
        if played is not None:
            # The played flag has its own endpoint; the merge does not
            # clear it (see clear()).
            self.session.api.item_played(item_id, bool(played))
        if position is not None:
            self.session.api.update_userdata_for_item(
                item_id, {"PlaybackPositionTicks": int(position)})

    def report(self, item_id, position):
        """Report progress the way the player does."""
        self.session.api.session_progress({
            "ItemId": item_id, "PositionTicks": int(position),
            "IsPaused": False, "PlayMethod": "DirectPlay", "CanSeek": True})

    def stored(self, item_id):
        data = self.session.user_data(item_id)
        return (data.get("PlaybackPositionTicks") or 0,
                bool(data.get("Played")))


@_e2e.require_server
class ResumeRuleTest(_AudiobookCase):
    """The rule everything else is built on, pinned to the minute.

    Not a re-implementation of the server: three reports and what came
    back. If Jellyfin ever changes these thresholds — or makes them
    percentages like the video ones — this is what says so, rather than the
    browser quietly resuming books in the wrong place.
    """

    def setUp(self):
        self.item = self.book(LONG_M4B)
        self.clear(self.item["Id"])
        self.addCleanup(self.clear, self.item["Id"])

    def test_the_fixture_is_long_enough_to_have_a_resume_window(self):
        """The premise of every other test in this file. A book needs more
        than ten minutes for a resumable position to exist at all —
        `MinAudiobookResume` and `MaxAudiobookResume` are five minutes each
        and they eat the whole thing from both ends."""
        minutes = (self.item.get("RunTimeTicks") or 0) / MINUTE
        self.assertGreater(
            minutes, 10,
            "%s is %.1f minutes; the server can hold no resume position "
            "for a book that short" % (LONG_M4B, minutes))

    def test_a_position_near_the_start_is_discarded(self):
        self.report(self.item["Id"], 2 * MINUTE)
        position, played = self.stored(self.item["Id"])
        self.assertEqual(position, 0,
                         "a position 2 minutes in was kept; MinAudiobookResume "
                         "is no longer 5 minutes")
        self.assertFalse(played)

    def test_a_position_in_the_middle_is_kept(self):
        self.report(self.item["Id"], 6 * MINUTE)
        position, played = self.stored(self.item["Id"])
        self.assertAlmostEqual(position / MINUTE, 6, places=1)
        self.assertFalse(played, "a book 6 minutes in was marked finished")

    def test_a_position_near_the_end_finishes_the_book(self):
        """Under five minutes remaining, so the position is dropped AND the
        book is marked read. This is how an audiobook gets finished by
        playing it — there is no percentage rule for one."""
        runtime = self.item["RunTimeTicks"]
        self.report(self.item["Id"], runtime - 2 * MINUTE)
        position, played = self.stored(self.item["Id"])
        self.assertEqual(position, 0)
        self.assertTrue(played, "playing to the end did not finish the book")

    def test_a_short_book_can_never_resume(self):
        """The negative case, pinned rather than avoided.

        `The Lantern Keeper` is four minutes, so *every* position in it is
        both under five minutes in and under five minutes from the end. A
        test written against it fails in a way that reads as a client bug,
        which is exactly what happened.
        """
        short = self.book(SHORT_M4B)
        self.clear(short["Id"])
        self.addCleanup(self.clear, short["Id"])
        runtime = short["RunTimeTicks"]
        self.assertLess(runtime / MINUTE, 10)
        for fraction in (0.1, 0.5, 0.9, 0.99):
            self.report(short["Id"], int(runtime * fraction))
            position, _played = self.stored(short["Id"])
            self.assertEqual(
                position, 0,
                "a %.0f%% position stuck on a %.1f-minute book — the server's "
                "audiobook resume window has changed and the short fixtures "
                "are now usable for resume tests"
                % (fraction * 100, runtime / MINUTE))


@_e2e.require_server
class ChapteredBookTest(_AudiobookCase):
    """A single `.m4b` is ONE item whose chapters are rows in the database.

    Chapter extraction is switched on for this item type and no other
    (`AudioFileProber`: `ExtractChapters = item is AudioBook`), and it does
    nothing but add `-show_chapters` to ffprobe — so markers the container
    does not carry are markers that do not exist.
    """

    def test_the_chapters_are_real_rows(self):
        item = {i["Name"]: i for i in self.session.find_all(
            library=LIBRARY, item_type="AudioBook",
            fields="Chapters")}.get(LONG_M4B)
        if item is None:
            self.skipTest("no %r" % LONG_M4B)
        chapters = item.get("Chapters") or []
        self.assertGreaterEqual(len(chapters), 2)
        starts = [c.get("StartPositionTicks") or 0 for c in chapters]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(starts[0], 0, "the first chapter does not start at 0")
        self.assertLess(starts[-1], item["RunTimeTicks"],
                        "a chapter starts after the book ends")

    def test_a_rip_has_no_chapters_of_its_own(self):
        """The other shape: there a chapter is a FILE, and the item carries
        no markers at all. Same user gesture, two code paths — and only the
        chaptered one reaches the audio bar's chapter controls."""
        parts = {i["Name"]: i for i in self.session.find_all(
            library=LIBRARY, item_type="AudioBook", fields="Chapters")}
        part = parts.get(RIP_PARTS[0])
        if part is None:
            self.skipTest("no long rip fixture")
        self.assertFalse(part.get("Chapters"),
                         "a rip part now carries chapter markers, so the "
                         "browser would offer two ways through one book")


@_e2e.require_server
class FolderResumeTest(_AudiobookCase):
    """Resuming a multi-file book, which is the shape that needs help.

    A rip's position does not live on the book — there is no book, only N
    items — so "where was I" is *which part am I on, and where in it*. The
    browser answers that from the parts' own user data (`BooksPage`), and
    this is that answer computed against a real server rather than a fake.
    """

    def setUp(self):
        self.parts = [self.book(name) for name in RIP_PARTS]
        self.ids = [p["Id"] for p in self.parts]
        self.clear(*self.ids)
        self.addCleanup(self.clear, *self.ids)

    def tracks(self):
        """The parts as the page loads them: the folder's children, in
        order, with fresh user data."""
        parent = self.parts[0]["ParentId"]
        items, _total = self.source.get_library_items(
            self.uuid, parent, limit=50, collection_type="books")
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import _track_order
        return sorted(items, key=_track_order)

    def resume_at(self):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage
        return BooksPage._resume_at(self.tracks())

    def test_an_untouched_book_resumes_at_the_first_part(self):
        index, offset = self.resume_at()
        self.assertEqual(index, 0)
        self.assertIsNone(offset)

    def test_a_finished_part_is_skipped(self):
        self.seed(self.ids[0], played=True)
        index, offset = self.resume_at()
        self.assertEqual(index, 1, "resume did not move past a finished part")
        self.assertIsNone(offset)

    def test_a_part_in_progress_resumes_inside_it(self):
        """Two numbers, not one: the part AND the offset within it. Dropping
        the second restarts a twelve-minute chapter."""
        self.seed(self.ids[0], played=True)
        self.seed(self.ids[1], position=6 * MINUTE)
        index, offset = self.resume_at()
        self.assertEqual(index, 1)
        self.assertIsNotNone(offset, "the part's own position was dropped")
        self.assertAlmostEqual(offset / MINUTE, 6, places=1)

    def test_a_finished_book_has_nothing_to_resume(self):
        for item_id in self.ids:
            self.seed(item_id, played=True)
        self.assertIsNone(self.resume_at(),
                          "a book with every part finished still offered a "
                          "resume point")

    def test_playing_a_part_to_its_end_advances_the_resume_point(self):
        """The property over several steps rather than one.

        Each part is long enough to be finished by a report (12 minutes,
        so 10:00 leaves under five remaining). Walking the whole book has to
        move the resume point forward each time and then stop offering one —
        a one-step test cannot see a resume point that fails to advance.
        """
        seen = []
        for i, item_id in enumerate(self.ids):
            resume = self.resume_at()
            self.assertIsNotNone(resume, "no resume point at part %d" % i)
            seen.append(resume[0])
            runtime = self.parts[i]["RunTimeTicks"]
            self.report(item_id, runtime - 2 * MINUTE)
            _position, played = self.stored(item_id)
            self.assertTrue(played, "part %d did not finish" % (i + 1))
        self.assertEqual(seen, [0, 1, 2],
                         "the resume point did not walk the book")
        self.assertIsNone(self.resume_at())

    def test_the_folder_tracks_progress_across_its_parts(self):
        """The shelf's half of the same question. A container has no
        position of its own, so the tile's progress bar and its finished
        tick both come from the folder's UserData — which the server
        maintains across the children."""
        from jellyfin_mpv_shim.mpvtk_browser import components

        parent = self.parts[0]["ParentId"]

        def folder():
            return self.session.api.get_item(parent)

        before = folder()
        self.assertFalse(components.is_watched(before))
        self.assertEqual(
            (before.get("UserData") or {}).get("UnplayedItemCount"),
            len(self.ids))

        for item_id in self.ids:
            self.seed(item_id, played=True)
        after = folder()
        self.assertEqual(
            (after.get("UserData") or {}).get("UnplayedItemCount"), 0,
            "the folder did not notice its parts being finished")
        self.assertTrue(
            components.is_watched(after),
            "a book with every part finished shows no tick on the shelf")

    def test_marking_the_folder_finished_cascades_to_the_parts(self):
        """Which is what makes the tile menu's entry honest — it marks the
        book, not a row in a table nobody else reads."""
        parent = self.parts[0]["ParentId"]
        self.addCleanup(self.session.api.item_played, parent, False)
        self.session.api.item_played(parent, True)
        for item_id in self.ids:
            self.assertTrue(self.session.user_data(item_id).get("Played"),
                            "marking the folder left a part unfinished")


@_e2e.require_server
class OneBookOrSeveralTest(_AudiobookCase):
    """A folder of audiobooks is a chapter list only when they are chapters
    of one book — and `Album` is the only field that can say so.

    Unit tests can assert the rule; only a real library can say whether the
    *shapes* it is meant to tell apart actually come back from a scan
    looking the way the rule assumes. Both of these folders exist because
    this test needed them.
    """

    def children(self, parent_id):
        items, total = self.source.get_library_items(
            self.uuid, parent_id, limit=50, collection_type="books")
        self.assertEqual(total, len(items), "the folder paged")
        return items

    def folder_of(self, name):
        return self.book(name)["ParentId"]

    def tracks_for(self, parent_id):
        """What `BooksPage` would draw, run against real children."""
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import (
            _track_order, one_book)
        from jellyfin_mpv_shim.books import is_audiobook

        items = self.children(parent_id)
        if not items or not all(is_audiobook(i) for i in items):
            return None
        if not one_book(items):
            return None
        return sorted(items, key=_track_order)

    def test_a_rip_is_one_book(self):
        parent = self.folder_of(RIP_PARTS[0])
        tracks = self.tracks_for(parent)
        self.assertIsNotNone(tracks, "a rip is not being drawn as one book")
        self.assertEqual(len(tracks), len(RIP_PARTS))

    def test_an_author_folder_of_different_books_is_not(self):
        """Three books, one file each, in one directory. Drawn as a chapter
        list they would look like one book with three chapters and two of
        them would be unopenable."""
        first = self.audiobooks().get(SEVERAL[0])
        if first is None:
            self.skipTest("no several-books fixture; rebuild the library")
        items = self.children(first["ParentId"])
        self.assertEqual(len(items), len(SEVERAL))
        self.assertEqual({i.get("Album") for i in items}, set(SEVERAL),
                         "the three books do not carry three albums, so "
                         "nothing could tell them apart")
        self.assertIsNone(self.tracks_for(first["ParentId"]))

    def test_a_folder_that_is_not_all_audiobooks_is_not_either(self):
        """Two loose books plus a rip in a subfolder: its children include
        a `Folder`, which no chapter list has a row for."""
        first = self.audiobooks().get(MIXED[0])
        if first is None:
            self.skipTest("no mixed fixture; rebuild the library")
        items = self.children(first["ParentId"])
        self.assertIn("Folder", {i.get("Type") for i in items},
                      "the mixed fixture has no subfolder any more")
        self.assertIsNone(self.tracks_for(first["ParentId"]))

    def test_a_single_book_in_an_author_folder_is_one_book(self):
        """`AudioResolver` resolves a one-book directory INTO the audiobook
        -- there is no per-book Folder for it at all -- so the item is
        parented to the author. One album, so: one book."""
        item = self.book(LONG_M4B)
        items = self.children(item["ParentId"])
        self.assertEqual([i["Name"] for i in items], [LONG_M4B])
        self.assertIsNotNone(self.tracks_for(item["ParentId"]))


@_e2e.require_server
class DescriptionTest(_AudiobookCase):
    """Where an audiobook's description comes from.

    It is written into the FILE's tags, so it is on the chapter items and
    not on the directory -- which is why the page falls through to the
    first track. A folder CAN carry one (nothing on disk gives it one, but
    the metadata editor does), and when it does it must win.
    """

    def overview_for(self, folder_name_of):
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import BooksPage

        item = self.book(folder_name_of, fields="Path,Album,ParentId,Overview")
        parent = item["ParentId"]
        folder = self.session.api.get_item(parent)
        items, _total = self.source.get_library_items(
            self.uuid, parent, limit=50, collection_type="books")
        from jellyfin_mpv_shim.mpvtk_browser.pages.books import _track_order
        return BooksPage._overview(folder, sorted(items, key=_track_order))

    def test_a_books_description_comes_off_its_files(self):
        text = self.overview_for(LONG_M4B)
        self.assertTrue(text, "%s has no description; the fixture regressed"
                        % LONG_M4B)

    def test_the_grid_query_carries_it(self):
        """The whole point of BOOKS_GRID_FIELDS: GRID_FIELDS drops Overview
        because a hundred-item grid does not draw one, so without the books
        variant the fallback above has nothing to fall through to."""
        item = self.book(LONG_M4B)
        items, _total = self.source.get_library_items(
            self.uuid, item["ParentId"], limit=50, collection_type="books")
        self.assertTrue(any(i.get("Overview") for i in items),
                        "the books grid query did not ask for Overview")

    def test_a_folders_own_description_wins(self):
        """The rip's folder carries one and its parts carry different ones,
        so this can only pass by preferring the folder."""
        part = self.book(RIP_PARTS[0])
        folder = self.session.api.get_item(part["ParentId"])
        folder_text = (folder.get("Overview") or "").strip()
        if not folder_text:
            self.skipTest("the rip's folder has no description")
        self.assertNotEqual(folder_text, (part.get("Overview") or "").strip())
        self.assertEqual(self.overview_for(RIP_PARTS[0]), folder_text)

    def test_a_book_with_no_description_anywhere_shows_none(self):
        # The short rip is deliberately blank, so "no blurb" stays reachable.
        chapters = self.audiobooks(
            fields="Path,Album,ParentId,Overview").get("Chapter 01")
        if chapters is None:
            self.skipTest("no short rip fixture")
        self.assertEqual(self.overview_for("Chapter 01"), "")


@_e2e.require_server_and_mpv
class AudiobookPlaybackTest(_e2e.E2ETestCase):
    """The reporting loop, with a real mpv and a real stream in it.

    Everything above drives the server directly. This is the half that
    proves the *shim* takes part: that an audiobook plays, that the position
    it reports is the one the server stores, and that playing to the end
    finishes the book. None of it can be seen from a fake — the fake agrees
    by construction — and none of it could be tested at all until the
    library grew a book longer than the server's resume window.
    """

    LIBRARY = LIBRARY

    def setUp(self):
        super().setUp()
        self.item = self.session.find(LONG_M4B, library=self.LIBRARY)
        self.session.reset_played(self.item["Id"])
        self.addCleanup(self.session.reset_played, self.item["Id"])

    def _play(self):
        media = _e2e.build_media(self.session, [self.item["Id"]])
        video = media.video
        self.assertIsNotNone(video, "Media built no video for an audiobook")
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(self.pm._player.duration,
                        "real mpv never reported a duration for the book")
        return video

    def test_an_audiobook_plays_at_all(self):
        video = self._play()
        self.assertFalse(video.is_transcode,
                         "an audiobook needed a transcode; the timings below "
                         "stop being meaningful if so")
        self.pm.send_timeline()
        playing = _e2e.wait_for(
            lambda: (self.session.my_session() or {}).get("NowPlayingItem"))
        self.assertTrue(playing, "the server never saw the book playing")
        self.assertEqual(playing["Id"], self.item["Id"])

    def test_mpv_knows_the_chapters(self):
        """The audio bar's chapter controls read mpv's list, not the item's:
        a `.m4b`'s markers live in the FILE, so mpv is the only thing that
        can enumerate them during playback."""
        self._play()
        chapters = self.pm._player.chapter_list or []
        self.assertGreaterEqual(
            len(chapters), 2,
            "mpv sees no chapters in a book the server says has six")

    def test_stopping_midway_leaves_a_resume_position(self):
        """Six minutes in: past `MinAudiobookResume`, and with eighteen
        left, well clear of the finish rule."""
        self._play()
        target = 6 * 60.0
        self.pm.seek(target, absolute=True, exact=True)
        arrived = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) >= target - 5,
            timeout=30)
        self.assertTrue(arrived, "mpv never reached the seek target")
        self.pm.send_timeline()
        self.pm.stop()

        ticks = _e2e.wait_for(
            lambda: self.session.user_data(self.item["Id"])
            .get("PlaybackPositionTicks"))
        self.assertTrue(ticks, "no resume position was recorded for the book")
        seconds = ticks / TICKS
        self.assertGreater(seconds, target - 60)
        self.assertLess(seconds, target + 60)
        self.assertFalse(self.session.user_data(self.item["Id"]).get("Played"),
                         "a book stopped a quarter of the way in was finished")

    def test_stopping_near_the_end_finishes_the_book(self):
        """The other side of the same rule, through the player: under five
        minutes remaining and the server marks it read and drops the
        position. This is the only way an audiobook gets finished by
        listening to it."""
        self._play()
        target = (self.item["RunTimeTicks"] / TICKS) - 90.0
        self.pm.seek(target, absolute=True, exact=True)
        arrived = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) >= target - 5,
            timeout=30)
        self.assertTrue(arrived, "mpv never reached the seek target")
        self.pm.send_timeline()
        self.pm.stop()

        played = _e2e.wait_for(
            lambda: self.session.user_data(self.item["Id"]).get("Played"))
        self.assertTrue(played, "listening to the end did not finish the book")
        self.assertFalse(
            self.session.user_data(self.item["Id"])
            .get("PlaybackPositionTicks"),
            "a finished book kept a resume position")

    def test_a_stored_position_is_where_playback_starts(self):
        """The round trip closed: what the browser hands the player as a
        resume offset is where mpv actually begins. A resume that reports
        correctly and then starts from zero is the failure this catches."""
        offset = 8 * 60 * TICKS
        self.session.api.update_userdata_for_item(
            self.item["Id"], {"PlaybackPositionTicks": offset})
        media = _e2e.build_media(self.session, [self.item["Id"]])
        self.pm.play(media.video, is_initial_play=True,
                     offset=offset / TICKS)
        started = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) >= (8 * 60) - 30,
            timeout=30)
        self.assertTrue(
            started,
            "playback resumed at %.0fs rather than near 8 minutes"
            % (self.pm._player.playback_time or 0))


if __name__ == "__main__":
    unittest.main()
