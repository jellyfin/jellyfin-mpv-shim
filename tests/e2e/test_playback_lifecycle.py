"""Slice S2: the playback lifecycle, at the keyboard of the shipped app.

Critical-path inventory rows 2, 3, 4 and 6. What these replace all shared one
shape: the test posted its own progress report before stopping, or called
the reporting function directly, so the server held the right answer
whether or not the app ever sent one (audit part 2: EndOfQueueTest,
ResumePositionTest, StrmResumeTest, AbortReportedPositionTest, the
_close_child abort modes). Here nothing is reported but by the app: keys go
in, and the server and mpv are read.

- the last item of a queue, played to its natural end, is marked watched,
  with force_set_played off and on;
- stopped midway, the server holds the position, and reopening resumes
  there -- a file, and a `.strm`;
- stepping back from an episode does not mark it watched, and the
  mark-watched key does;
- closing while paused with the network gone exits cleanly and leaves no
  mpv behind.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
import _relay  # noqa: E402
import _strm  # noqa: E402

#: Ten minutes: long enough for a resume position (the server keeps none
#: below 300 s, and only between 5 % and 90 %).
LONG_NAME = "Eat for Health"
SECOND_LONG_NAME = "About Bananas"


def _backend():
    return os.environ.get("JMS_TEST_BACKEND", "libmpv")


@_e2e.require_server
class _PlaybackCase(unittest.TestCase):
    CONF = {}

    def setUp(self):
        self.session = _e2e.Session()
        self.before_login()
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend(), conf=dict(self.CONF),
                            files=dict(getattr(self, "FILES", {})))
        self.addCleanup(lambda: self.app.close())
        self.app.start()
        _flows.login(self.app, self.relay)

    def before_login(self):
        """Server-side setup the app must find when it signs in. One
        Session per test: a second login for the account revokes it."""

    def assertEventually(self, fn, msg, timeout=30):
        got = _e2e.wait_for(fn, timeout=timeout)
        self.assertTrue(got, msg)
        return got

    def stop_report(self, item_id):
        for path, body in list(self.relay.bodies):
            if path.startswith("/Sessions/Playing/Stopped"):
                try:
                    data = json.loads(bytes(body) or b"{}")
                except ValueError:
                    continue
                if data.get("ItemId") == item_id and \
                        data.get("PositionTicks") is not None:
                    return data
        return None

    def marked(self, item_id):
        """Explicit watched marks the app sent for this item."""
        return [p for m, p in list(self.relay.requests)
                if m == "POST" and "PlayedItems/" + item_id in p]

    def fresh(self, *item_ids):
        for item_id in item_ids:
            self.session.reset_played(item_id)
            self.addCleanup(self.session.reset_played, item_id)

    def movie(self, name):
        found = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == name]
        self.assertEqual(1, len(found), name)
        return found[0]["Id"]

    def server(self, item_id):
        return self.session.user_data(item_id) or {}

    def play(self):
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        return self.app.playing_path()

    def time_pos(self):
        return self.app.prop("time-pos") or 0

    def seek_to(self, seconds, key="UP", timeout=60):
        """Forward by keys, the way a person skips ahead."""
        deadline = time.monotonic() + timeout
        while self.time_pos() < seconds and time.monotonic() < deadline:
            self.app.key(key)
            time.sleep(0.4)
        self.assertGreaterEqual(self.time_pos(), seconds,
                                "the %s key did not seek" % key)


class TheLastItemIsMarkedWatchedTest(_PlaybackCase):
    """Row 2. A film is a queue whose last item is itself. Played out to its
    natural end -- skipped close to it by keys, then left to finish -- with
    no report but the app's own, it is watched on the server.

    Ten minutes long on purpose: the server marks an item under 300 s
    played from progress reports alone, so a short one proves nothing about
    what the app sends at the end. A long one is marked only by a stop at
    or past 90 %, which is the end-of-queue report EndOfQueueTest's own
    progress post stood in for. The server's flag alone proves little
    (see below), so the stop report the app sent is asserted too."""

    def test_played_to_its_end_it_is_watched(self):
        film = self.movie(LONG_NAME)
        self.fresh(film)
        _flows.open_by_search(self.app, LONG_NAME, film)
        self.play()
        self.seek_to(580)
        self.assertFalse(self.server(film).get("Played"),
                         "watched before it ended")
        self.assertTrue(
            _e2e.wait_for(lambda: self.server(film).get("Played"),
                          timeout=120),
            "played to its end and not watched on the server: %r"
            % self.server(film))
        self.app.wait_for(lambda f: self.app.prop("idle-active") is True,
                          timeout=30, what="mpv idle after the last item")
        # The server marks a long item watched from a progress report past
        # 90 % too (measured on 12.0), so its flag cannot say whether the
        # app reported the end. The stop report can.
        stop = self.assertEventually(lambda: self.stop_report(film),
                                     "the end of the queue sent no stop")
        runtime = (self.server(film).get("RunTimeTicks")
                   or self.session._request("/Items/%s?UserId=%s" % (
                       film, self.session.user_id))["RunTimeTicks"])
        self.assertGreaterEqual(stop["PositionTicks"], 0.9 * runtime,
                                "the end was reported short of the end")
        self.check_mark(film)
        self.assertEqual(0, self.app.quit(timeout=30))

    def check_mark(self, film):
        """force_set_played off: the stop report does it, no explicit mark."""


class TheLastItemWithForceSetPlayedTest(TheLastItemIsMarkedWatchedTest):
    """The same, with force_set_played on: the app also marks it itself."""

    CONF = {"force_set_played": True}

    def check_mark(self, film):
        self.assertTrue([p for m, p in list(self.relay.requests)
                         if m == "POST" and "PlayedItems/" + film in p],
                        "force_set_played sent no watched mark")


class StopAndResumeTest(_PlaybackCase):
    """Row 3. Stopped midway with the stop key, the server holds where it
    stopped -- from the app's stop report alone -- and reopening offers
    Resume, which starts there."""

    NAME = LONG_NAME
    AT = 240

    def open(self):
        self.item = self.movie(self.NAME)
        self.fresh(self.item)
        _flows.open_by_search(self.app, self.NAME, self.item)

    def test_the_server_holds_the_position_and_resume_starts_there(self):
        self.open()
        self.play()
        # Ask before reporting: the URL is resolved (PlaybackInfo, which is
        # what teaches the server a .strm's runtime) before the session is
        # opened -- the order test_strm_source only claimed.
        def index_of(prefix):
            order = [p for m, p in list(self.relay.requests) if m == "POST"]
            return next((i for i, p in enumerate(order)
                         if p.startswith(prefix)), None)

        # The session is opened asynchronously, after mpv has the file.
        _e2e.wait_for(lambda: index_of("/Sessions/Playing") is not None,
                      timeout=15)
        asked = index_of("/Items/%s/PlaybackInfo" % self.item)
        opened = index_of("/Sessions/Playing")
        self.assertIsNotNone(asked, "the URL was never resolved")
        self.assertIsNotNone(opened, "no session was opened")
        self.assertLess(asked, opened, "reported before it asked")
        self.seek_to(self.AT)
        time.sleep(1)
        stopped_at = self.time_pos()
        self.app.key("q")
        # Until it matches the stop: a progress report from mid-seek can be
        # what the server holds for a moment first.
        _e2e.wait_for(
            lambda: abs((self.server(self.item).get("PlaybackPositionTicks")
                         or 0) / 1e7 - stopped_at) < 10, timeout=30)
        self.assertTrue(self.server(self.item).get("PlaybackPositionTicks"),
                        "no position on the server after stopping: %r"
                        % self.server(self.item))
        held = self.server(self.item)["PlaybackPositionTicks"] / 1e7
        self.assertLess(abs(held - stopped_at), 10,
                        "stopped at %.0f s, the server holds %.0f s"
                        % (stopped_at, held))
        self.assertFalse(self.server(self.item).get("Played"))
        # Reopened: the page you land on after stopping is not re-read (a
        # detail route is not in USERDATA_KINDS yet -- app.py says why).
        self.app.move_to("nav-home")
        self.app.press_until("ENTER", lambda f: _app.shown(f, "row-libs"),
                             what="Home")
        _flows.open_by_search(self.app, self.NAME, self.item)
        self.app.wait_for(lambda f: _app.shown(f, "btn-resume"), timeout=30,
                          what="Resume on the reopened detail page")
        self.app.move_to("btn-resume")
        self.app.key("ENTER")
        self.app.playing_path()
        self.assertTrue(_e2e.wait_for(lambda: self.time_pos() > 5,
                                      timeout=30))
        self.assertLess(abs(self.time_pos() - held), 10,
                        "resumed at %.0f s, the server holds %.0f s"
                        % (self.time_pos(), held))
        self.assertEqual(0, self.app.quit(timeout=30))


@_strm.require_origin(_strm.LONG_MOVIE)
class StrmStopAndResumeTest(StopAndResumeTest):
    """The same through a `.strm` (StrmResumeTest's claim): the media source
    is a URL the server hands on, not a file it serves."""

    NAME = _strm.LONG_MOVIE
    AT = 120


class StepsAndMarksTest(_PlaybackCase):
    """Row 4. Next and previous report the item left where it was left, and
    send no watched mark for it; the mark-watched key sends one. Witnessed
    at the relay: what the app SAID.

    A playlist of two ten-minute films, made for the test and removed after.
    Not the library's episodes: they run ten seconds, and the app counts
    anything within ten seconds of the end as finished (player
    `_finished_at_eof`, which absorbs the timeline tick) -- every position
    in them is "the end"."""

    def before_login(self):
        # Before sign-in: Home's library row is read then, and a Playlists
        # shelf that did not exist yet is not on it.
        self.first, self.second = self.movie(LONG_NAME), self.movie(
            SECOND_LONG_NAME)
        self.fresh(self.first, self.second)
        made = self.session._request("/Playlists", method="POST", body={
            "Name": "jms-e2e-steps", "Ids": [self.first, self.second],
            "UserId": self.session.user_id, "MediaType": "Video"})
        self.playlist = made["Id"]
        self.addCleanup(self.session._request, "/Items/%s" % self.playlist,
                        "DELETE")

    def open_playlist(self):
        views = self.session._request("/Users/%s/Views"
                                      % self.session.user_id)["Items"]
        shelf = next(v["Id"] for v in views
                     if v.get("CollectionType") == "playlists")
        f = self.app.frame()
        first = next(n["id"] for n in f["nodes"]
                     if (n.get("id") or "").startswith("row-libs-"))
        self.app.move_to(first)
        self.app.move_to("row-libs-" + shelf, key="RIGHT")
        tile = "grid-0-" + self.playlist
        self.app.press_until("ENTER", lambda f: _app.shown(f, tile),
                             what="the Playlists shelf")
        self.app.move_to(tile)
        self.app.press_until("ENTER", lambda f: _app.shown(f, "pl-play"),
                             what="the playlist")

    def pause_inside(self):
        self.assertTrue(_e2e.wait_for(lambda: self.time_pos() > 5,
                                      timeout=30))
        self.app.key("SPACE")
        self.assertTrue(_e2e.wait_for(
            lambda: self.app.prop("pause") is True, timeout=10))
        return self.time_pos()

    def left_where_it_was(self, item_id, at):
        stop = self.assertEventually(lambda: self.stop_report(item_id),
                                     "no stop was reported for %s" % item_id)
        said = stop["PositionTicks"] / 1e7
        self.assertLess(abs(said - at), 3,
                        "the stop says %.1f s; it was left at %.1f s"
                        % (said, at))
        self.assertEqual([], self.marked(item_id),
                         "leaving it marked it watched")

    def test_next_and_prev_leave_it_where_it_was_and_w_marks(self):
        self.open_playlist()
        self.app.move_to("pl-play")
        self.app.key("ENTER")
        self.assertIn(self.first, self.app.playing_path())
        at = self.pause_inside()
        self.app.key(">")
        self.assertTrue(_e2e.wait_for(
            lambda: self.second in (self.app.prop("path") or ""),
            timeout=30), "next did not go to the second film")
        self.left_where_it_was(self.first, at)

        at = self.pause_inside() if self.app.prop("pause") is not True \
            else self.time_pos()
        self.app.key("<")
        self.assertTrue(_e2e.wait_for(
            lambda: self.first in (self.app.prop("path") or ""),
            timeout=30), "previous did not go back to the first film")
        self.left_where_it_was(self.second, at)

        self.app.key("w")
        self.assertEventually(lambda: self.marked(self.first),
                              "the mark-watched key sent no mark")
        self.assertEqual(0, self.app.quit(timeout=30))



class CloseWhilePausedWithTheServerGoneTest(_PlaybackCase):
    """Row 6. Paused, then the network goes, then the window is closed: the
    app exits cleanly inside the watchdog's budget and leaves no mpv. The
    _close_child abort modes passed when no stop report was sent at all."""

    def test_it_exits_cleanly_and_leaves_nothing_behind(self):
        item = self.movie(LONG_NAME)
        self.fresh(item)
        _flows.open_by_search(self.app, LONG_NAME, item)
        self.play()
        self.assertTrue(_e2e.wait_for(lambda: self.time_pos() > 2,
                                      timeout=30))
        self.app.key("SPACE")
        self.assertTrue(_e2e.wait_for(
            lambda: self.app.prop("pause") is True, timeout=10))
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        pgid = self.app.proc.pid
        start = time.monotonic()
        rc = self.app.quit(timeout=40)
        self.assertEqual(0, rc)
        self.assertLess(time.monotonic() - start, 20.0)
        if os.name == "posix":
            time.sleep(1)
            with self.assertRaises(ProcessLookupError,
                                   msg="something of the app's outlived "
                                       "it (an mpv?)"):
                os.killpg(pgid, 0)


if __name__ == "__main__":
    unittest.main()
