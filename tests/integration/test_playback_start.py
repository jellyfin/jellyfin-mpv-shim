"""What happens between ``play()`` and playing, driven against a fake mpv.

None of this could be asserted before, and the reason is worth stating: the
load wait is ``mpv_events.wait_property``, which calls
``unbind_property_observer`` on its way out -- and ``FakeMPV`` did not have
one. Every fake-backed load therefore raised AttributeError from inside
``_play_media``, so nothing in this suite had ever completed a start. The
real-mpv leg covers the happy path, which is why the hole went unnoticed;
what a real mpv *cannot* be asked for is the three ways a start fails. It
will not time out on demand, will not refuse a file at a chosen moment, and
cannot be cancelled at the instant the duration lands.

Those four outcomes are asserted as four, because they are not one failure
with three messages. A timeout offers a retry, a refusal offers a retry with
a cause, and a cancellation must offer **nothing** -- the user already moved
on, and a dialog about the film they just dismissed is the bug that
separation exists to prevent.

The rest of the module covers the other paths the same gap had closed: the
window geometry the shim re-arms before every load, and the chapters and
seekable ranges the now-playing bar draws. Every one of those reads sits
inside a broad ``except Exception``, so a fake missing the property did not
fail the test -- it took the "mpv would not answer" branch and left the
assertion looking satisfied.
"""

import contextlib
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as h  # noqa: E402

import jellyfin_mpv_shim.mpv_events as mpv_events  # noqa: E402

player_module = h.import_player_with_fake_mpv()


def make_video(**kw):
    """A video complete enough for ``_play_media`` to run start to finish.

    The state-machine module's ``FakeVideo`` covers the finish path; a start
    reads three more things off it. Kept here rather than added there so the
    two modules' stand-ins stay answerable to the paths they are used on.
    """
    from tests.integration.test_player_state_machine import FakeVideo

    video = FakeVideo(**kw)
    video.item = {"MediaType": "Video"}
    video.get_proper_title = lambda: "A Title"
    video.get_current_streams = lambda: (None, None)
    return video


def build(**player_kw):
    pm = h.build_player(player_module, **player_kw)
    pm.action_trigger = threading.Event()
    pm.timeline_trigger = threading.Event()
    return pm


class LoadOutcomeTest(unittest.TestCase):
    """The four ways ``_play_media`` can end, told apart by what each leaves
    behind for the user: the video, the retry, and the error notice."""

    def setUp(self):
        self.pm = build()
        self.notices = []
        # The one collaborator that has to be watched rather than inferred:
        # a timeout and a refusal both stop playback and both stash a retry,
        # and `timed_out` is the only thing that differs.
        self.pm._notify_load_error = (
            lambda video, detail, timed_out=False:
            self.notices.append((video, detail, timed_out)))

    def _play(self, video, timeout=1):
        with mock.patch.object(player_module.settings,
                               "playback_timeout", timeout):
            self.pm._play_media(video, "http://example.invalid/s.mkv",
                                is_initial_play=True)

    def _during_load(self, action, delay=0.05):
        """Run ``action`` once the load wait is under way."""
        timer = threading.Timer(delay, action)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)

    def _answer_duration(self, delay=0.05, value=100.0):
        """mpv reporting a duration, which is what "the file opened" means
        here -- the same property and the same route through the observer
        that a real load takes."""
        self._during_load(
            lambda: self.pm._player.fire_property("duration", value), delay)

    def test_a_load_the_player_answers_starts_the_video(self):
        video = make_video()
        self._answer_duration()
        self._play(video)

        self.assertIs(self.pm._video, video)
        self.assertEqual(self.pm._player.played,
                         ["http://example.invalid/s.mkv"])
        self.assertIsNone(self.pm._failed_playback,
                          "a start that succeeded still offered a retry")
        self.assertEqual(self.notices, [])

    def test_a_load_nothing_answers_is_reported_as_a_timeout(self):
        video = make_video()
        self._play(video)          # no duration ever arrives

        self.assertIsNone(self.pm._video)
        self.assertEqual(self.pm._failed_playback, (video, 0),
                         "a timed-out start left nothing to retry")
        self.assertEqual(len(self.notices), 1)
        _v, detail, timed_out = self.notices[0]
        self.assertTrue(timed_out)
        self.assertIsNone(detail, "a timeout has no cause to report")

    def test_a_load_mpv_refuses_is_reported_with_its_cause(self):
        video = make_video()
        # mpv failing the file: fast, and with something to say. The retry
        # is offered either way; what must differ is that the user is told
        # why rather than being left to conclude the app hung.
        #
        # Armed on a timer rather than up front because the start CLEARS
        # both on its way in -- a stale failure from the previous item must
        # not fail this one -- so a flag set beforehand is simply gone by
        # the time the wait begins.
        self._during_load(lambda: (
            setattr(self.pm, "_load_error_detail", "Unrecognized file format"),
            self.pm._load_failed.set()))
        self._play(video, timeout=30)

        self.assertIsNone(self.pm._video)
        self.assertEqual(self.pm._failed_playback, (video, 0))
        self.assertEqual(self.notices,
                         [(video, "Unrecognized file format", False)])

    def test_a_cancelled_load_reports_nothing_and_offers_no_retry(self):
        video = make_video()
        # Through `cancel_load`, not by poking the flag it sets. The flag on
        # its own leaves the wait running: what makes the button work is
        # that it *also* trips the abort the end-file handler uses, and the
        # case worth cancelling is precisely the one where mpv would sit on
        # a stalled stream for the full timeout.
        self.pm._start_in_progress = True        # what play() sets
        self._during_load(self.pm.cancel_load)
        started = time.monotonic()
        self._play(video, timeout=30)
        elapsed = time.monotonic() - started

        self.assertIsNone(self.pm._video)
        self.assertIsNone(self.pm._failed_playback,
                          "a start the user abandoned still offered to "
                          "replay it")
        self.assertEqual(self.notices, [],
                         "the user was shown an error for a load they "
                         "cancelled themselves")
        self.assertLess(elapsed, 5.0,
                        "Cancel did not end the wait; it ran out the "
                        "playback timeout, which is the thing the button "
                        "exists to avoid")

    def test_a_successful_start_clears_the_retry_the_failure_left(self):
        """One play cannot see this.

        ``_failed_playback`` starts empty, so a success that never clears it
        is indistinguishable from one that does -- which is how the
        assertion in the happy-path test above passes against a player that
        does not clear it at all. What it costs is a Retry offering the film
        that failed an hour ago, on a player already playing something else.
        """
        self._play(make_video(item_id="doomed"))       # times out
        self.assertIsNotNone(self.pm._failed_playback)

        wanted = make_video(item_id="fine")
        self._answer_duration()
        self._play(wanted)

        self.assertIs(self.pm._video, wanted)
        self.assertIsNone(self.pm._failed_playback,
                          "a start that succeeded left the previous "
                          "failure's retry armed")

    def test_the_cancel_flag_does_not_survive_into_the_next_start(self):
        """A cancel is consumed by the start it cancels.

        One step cannot see this: the flag is read and cleared on the same
        path, so a version that left it set passes any single-play test and
        then refuses every subsequent play for the life of the process.
        """
        self.pm._start_in_progress = True
        self._during_load(self.pm.cancel_load)
        self._play(make_video(), timeout=30)

        wanted = make_video(item_id="second")
        self._answer_duration()
        self._play(wanted)

        self.assertIs(self.pm._video, wanted,
                      "a cancelled start poisoned the next one")


class StartOrderTest(unittest.TestCase):
    """The order `_play_media` does things in, which is stated in its own
    comments and checked by nothing.

    Every one of these is a *sequence* claim, and until the fakes shared a
    journal there was no way to make one: the volume write and the play call
    landed in two different recorders on the same object, the menu's hide
    landed in a third on another object, and nothing could line them up.
    Asserted as subsequences, so adding a step to the start path cannot fail
    them -- see `tests/test_fake_journal.py`.
    """

    def _start(self, video=None, **settings_kw):
        pm = build()
        timer = threading.Timer(
            0.05, lambda: pm._player.fire_property("duration", 100.0))
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(player_module.settings, "playback_timeout", 2):
            with contextlib.ExitStack() as stack:
                for key, value in settings_kw.items():
                    stack.enter_context(
                        mock.patch.object(player_module.settings, key, value))
                pm._play_media(video or make_video(),
                               "http://example.invalid/s.mkv",
                               is_initial_play=True)
        return pm

    def test_the_volume_is_applied_before_the_file_is_handed_over(self):
        """The persisted per-type volume, set *before* playback starts "so
        the track never briefly blares at the default while mpv
        probes/loads". Writing it afterwards still ends at the right volume,
        so every non-ordering assertion passes -- and the user still gets a
        second of the default one.

        The value is pinned as well as the position, and the two defaults
        are made to differ to do it: `music_volume` and `video_volume` are
        both 100 out of the box, so a version that read the wrong one wrote
        the same number and the journal could not tell.
        """
        pm = self._start(music_volume=40, video_volume=90)
        pm.journal.order("mpv.set:volume=90", "mpv.play")

    def test_a_song_gets_the_music_volume_and_a_film_the_video_one(self):
        """Which of the two, decided from the item about to play rather than
        from what is playing now -- there is nothing playing yet."""
        song = make_video(item_id="song")
        song.item = {"MediaType": "Audio", "Type": "Audio"}
        self._start(song, music_volume=40, video_volume=90).journal.happened(
            "mpv.set:volume=40")
        self._start(music_volume=40, video_volume=90).journal.happened(
            "mpv.set:volume=90")

    def test_the_menu_is_taken_down_before_the_file_is_handed_over(self):
        self._start().journal.order("menu.hide", "mpv.play")

    def test_the_window_geometry_is_armed_before_the_file_is_handed_over(self):
        """Re-arming is a no-op on a window that already has that size and a
        resize command on one that does not, so doing it after the file is
        loaded moves the window under the video that just started."""
        self._start().journal.order("mpv.set:geometry", "mpv.play")

    def test_the_load_wait_is_registered_after_the_file_and_taken_back(self):
        """Ordering *and* pairing in one claim: observing before the play
        would wait on the previous file's duration, and never unobserving is
        the leak `LoadObserverLeakTest` counts. The sequence says both."""
        self._start().journal.order("mpv.play", "mpv.observe:duration",
                                    "mpv.prop:duration", "mpv.unobserve:duration")

    def test_the_title_is_set_only_once_the_file_has_loaded(self):
        """`force_media_title` is written after the duration arrives. Set it
        before and a load that times out leaves the failed item's name on a
        window showing nothing."""
        self._start().journal.order("mpv.prop:duration",
                                    "mpv.set:force_media_title")


class LoadObserverLeakTest(unittest.TestCase):
    """Each load registers a property observer and must take it back.

    Over one play a leak is invisible: the wait works, the file plays, and
    the only trace is one extra handler. It is a queue -- twelve episodes,
    an album, a slideshow -- that turns that into every duration change
    being delivered to a growing list of dead closures. This is also the
    only assertion that ``unbind_property_observer`` / ``unobserve_property``
    is reached with something that actually matches what was registered:
    an id that does not resolve, or a handler compared by identity against
    a copy, both unregister nothing and raise nothing.
    """

    def test_repeated_starts_do_not_accumulate_property_observers(self):
        pm = build()
        counts = []
        for index in range(4):
            timer = threading.Timer(
                0.05, lambda: pm._player.fire_property("duration", 100.0))
            timer.daemon = True
            timer.start()
            self.addCleanup(timer.cancel)
            with mock.patch.object(player_module.settings,
                                   "playback_timeout", 1):
                pm._play_media(make_video(item_id="v%d" % index),
                               "http://example.invalid/%d.mkv" % index)
            counts.append(len(pm._player._property_observers.get("duration",
                                                                 [])))

        self.assertEqual(counts, [0, 0, 0, 0],
                         "the load wait left its observer behind: %r" % counts)


class ObserverDispatchTest(unittest.TestCase):
    """Which registration API the shim reaches for, and the trap in the
    other one.

    ``mpv_events`` picks by asking the *class* whether it has
    ``bind_property_observer``. A fake carrying both surfaces answers yes on
    both matrix legs, which is what this suite did until the fake was split
    -- the leg named "libmpv" was exercising jsonipc's branch and the libmpv
    branch was dead code in every run.
    """

    def test_this_leg_has_exactly_one_of_the_two_surfaces(self):
        player = h.fake_mpv_class()()
        ext = hasattr(type(player), "bind_property_observer")
        self.assertEqual(ext, h.BACKEND == "jsonipc",
                         "the fake's surface does not match the backend this "
                         "leg is named after, so the dispatch under test is "
                         "not the one that runs in production")
        self.assertNotEqual(
            ext, hasattr(type(player), "observe_property"),
            "the fake has both backends' observer APIs; whichever branch "
            "loses is untested on every leg")

    def test_a_bound_method_survives_registration_and_removal(self):
        """The reason ``mpv_events.observe`` exists at all.

        python-mpv's ``property_observer`` decorator writes an attribute onto
        the callback it is handed, and a bound method has no ``__dict__`` to
        take it. Everything the player registers is a bound method, so a
        change back to the decorator breaks one backend at runtime and
        nothing else.
        """
        player = h.fake_mpv_class()()
        seen = []

        class Listener:
            def on_change(self, name, value):
                seen.append((name, value))

        listener = Listener()
        token = mpv_events.observe(player, "duration", listener.on_change)
        player.fire_property("duration", 42.0)
        mpv_events.unobserve(player, "duration", listener.on_change, token)
        player.fire_property("duration", 43.0)

        self.assertEqual(seen, [("duration", 42.0)])

    @unittest.skipUnless(h.BACKEND == "libmpv",
                         "the decorator trap is python-mpv's")
    def test_python_mpvs_decorator_still_rejects_a_bound_method(self):
        """The guard on the guard.

        If the fake ever accepts a bound method here, the test above stops
        meaning anything -- it would pass just as well against the decorator
        this codebase went out of its way to stop using.
        """
        player = h.fake_mpv_class()()

        class Listener:
            def on_change(self, name, value):
                pass

        with self.assertRaises(AttributeError):
            player.property_observer("duration")(Listener().on_change)


class WindowGeometryTest(unittest.TestCase):
    """``_sync_window_geometry`` re-arms mpv's geometry option at the window's
    *current* size before each load, so X11's re-apply on reconfig is a no-op
    instead of a resize command.

    Every read it makes (``fullscreen``, ``window_maximized``, ``osd_width``,
    ``osd_height``) was absent from the fake, and the whole body is wrapped in
    one ``except Exception: return`` -- so this method has never done anything
    in this suite. It returned at the first read, every time, and looked fine.
    """

    def setUp(self):
        self.pm = build()
        self.player = self.pm._player

    def test_the_geometry_is_rearmed_at_the_live_window_size(self):
        self.player.osd_width, self.player.osd_height = 1600, 900
        self.pm._sync_window_geometry()
        self.assertEqual(self.player.geometry, "1600x900")

    def test_a_fullscreen_window_is_left_alone(self):
        """...and fullscreen is asked about the way the shim sets it.

        ``set_fullscreen`` writes ``fs``; this method reads ``fullscreen``.
        They are one property under two names on a real mpv, so a fake with
        two independent attributes would have the window agreeing it is not
        fullscreen immediately after being made fullscreen -- and this test
        would pass while asserting the opposite of its name.
        """
        self.player.osd_width, self.player.osd_height = 1600, 900
        self.pm.set_fullscreen(True)
        self.pm._sync_window_geometry()
        self.assertIsNone(self.player.geometry,
                          "a resize was commanded on a fullscreen window")

    def test_a_maximized_window_is_left_alone(self):
        self.player.osd_width, self.player.osd_height = 1600, 900
        self.player.window_maximized = True
        self.pm._sync_window_geometry()
        self.assertIsNone(self.player.geometry,
                          "re-arming a maximized window un-maximizes it")

    def test_an_unmapped_window_keeps_the_last_armed_size(self):
        """A window being built or torn down reports nonsense. Arming that is
        worse than arming nothing: the value is what mpv resizes to next."""
        self.player.osd_width, self.player.osd_height = 1600, 900
        self.pm._sync_window_geometry()
        self.player.osd_width, self.player.osd_height = 0, 0
        self.pm._sync_window_geometry()
        self.assertEqual(self.player.geometry, "1600x900")

    def test_the_armed_size_follows_the_window_over_several_resizes(self):
        """The value is cached (``_geometry_armed``) to skip redundant writes,
        and a cache compared against the wrong thing pins the first answer
        forever -- which one resize cannot show."""
        seen = []
        for size in ((1600, 900), (1280, 720), (1920, 1080)):
            self.player.osd_width, self.player.osd_height = size
            self.pm._sync_window_geometry()
            seen.append(self.player.geometry)
        self.assertEqual(seen, ["1600x900", "1280x720", "1920x1080"])


class PlaystateSnapshotTest(unittest.TestCase):
    """What the now-playing bar is handed on each state change.

    ``chapter_list`` and ``demuxer_cache_state`` were both absent from the
    fake and both are read inside their own ``except Exception``, so the
    snapshot has only ever been built with no chapters and no buffered
    ranges. Chapters are what make an audiobook navigable -- a single .m4b
    is one item whose chapters live in the file, so mpv is the only thing
    that knows them.
    """

    def setUp(self):
        self.pm = build()
        self.player = self.pm._player
        self.states = []
        self.pm.on_playstate = self.states.append
        self.pm._video = make_video()
        self.player.playback_abort = False

    def test_the_files_chapters_reach_the_bar(self):
        self.player.chapter_list = [
            {"title": "Chapter One", "time": 0.0},
            {"title": "Chapter Two", "time": 930.5},
        ]
        self.pm.push_playstate()
        self.assertEqual(self.states[-1]["chapters"],
                         [{"title": "Chapter One", "time": 0.0},
                          {"title": "Chapter Two", "time": 930.5}])

    def test_a_chapter_without_a_title_still_gets_a_tick(self):
        """mpv reports untitled chapters with no ``title`` key at all. The
        bar draws a tick per chapter, so dropping one loses the position,
        not just the label."""
        self.player.chapter_list = [{"time": 0.0}, {"time": 60.0}]
        self.pm.push_playstate()
        self.assertEqual([c["time"] for c in self.states[-1]["chapters"]],
                         [0.0, 60.0])
        self.assertEqual([c["title"] for c in self.states[-1]["chapters"]],
                         ["", ""])

    def test_the_buffered_ranges_reach_the_bar(self):
        self.player.demuxer_cache_state = {
            "seekable-ranges": [{"start": 0.0, "end": 120.25}]}
        self.pm.push_playstate()
        self.assertEqual(self.states[-1]["ranges"], [[0.0, 120.25]])

    def test_the_snapshot_follows_the_file_over_several_items(self):
        """Chapters ride the snapshot instead of being read per frame, which
        is a cache by another name: the risk is the previous item's chapters
        outliving it, and one push cannot show that."""
        seen = []
        for index, chapters in enumerate(([{"title": "A", "time": 0.0}],
                                          [],
                                          [{"title": "C", "time": 5.0}])):
            self.pm._video = make_video(item_id="v%d" % index)
            self.player.chapter_list = chapters
            self.pm.push_playstate()
            seen.append([c["title"] for c in self.states[-1]["chapters"]])
        self.assertEqual(seen, [["A"], [], ["C"]])


if __name__ == "__main__":
    unittest.main()
