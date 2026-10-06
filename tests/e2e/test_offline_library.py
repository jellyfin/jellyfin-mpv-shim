"""Row 63: browsing and playing the downloads with the network cut.

At the keyboard of the shipped app, against the real server for the setup
and then a relay that refuses everything. What had no honest test: the
route walk never walked offline, and the integration offline tests seed
hand-written catalog rows and write the position themselves.

- the next episode is not downloaded: playback stops, nothing hangs;
- Remove Watched takes what was watched offline, and only that;
- Shuffle over the downloads plays a downloaded copy;
- every offline route draws, with no "scene build failed" in the log;
- killed mid-film, the resume position survives into the next launch.
"""

import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
import _relay  # noqa: E402
from test_offline_ui import (  # noqa: E402
    DOWNLOADED_TILE, FILM_NAME, FILM_QUERY, _backend, _in_store)

#: The Standard Show, S1E1: ten seconds, and its E02 is never downloaded.
PILOT = "92fa76173b7f32908c46e09edaa52667"
#: Ten minutes in 44 MB: long enough for the 30 s offline progress record.
LONG_NAME = "Eat for Health"


def _started(test, *downloads):
    """Sign in, download each (query, item id, section), cut the network and
    relaunch: the app on its offline Home."""
    upstream = _e2e.SERVER.split("//", 1)[1]
    host, _, port = upstream.partition(":")
    test.relay = _relay.Relay((host, int(port or 80)))
    test.addCleanup(test.relay.close)
    test.app = _app.App(backend=_backend())
    test.addCleanup(lambda: test.app.close())
    test.catalog = _flows.Catalog(test.app.config_dir)
    test.app.start()
    _flows.login(test.app, test.relay)
    for query, item_id, section in downloads:
        _flows.open_by_search(test.app, query, item_id, section=section)
        _flows.download_open_item(test.app, test.catalog, item_id,
                                  timeout=300)
    test.app = _flows.relaunch(test.app, test.relay, cut=True)
    test.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=90,
                      what="the offline Home")


def _log(app):
    try:
        with open(app.log_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _app_errors(app):
    """ERROR lines from the app's own loggers. The apiclient logs a full
    traceback for every connect it cannot make, which offline is all of
    them and is not a fault."""
    return [line for line in _log(app).splitlines()
            if re.search(r"\[\s*ERROR\] (?!JELLYFIN\.|Jellyfin\.)", line)]


@_e2e.require_server
class TheOfflineLibraryTest(unittest.TestCase):

    def setUp(self):
        self.session = _e2e.Session()
        self.me = (self.session.server_id(), self.session.user_id)
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.film = films[0]["Id"]
        for item in (self.film, PILOT):
            self.session.reset_played(item)
            self.addCleanup(self.session.reset_played, item)
        _started(self, (FILM_QUERY, self.film, "Movies"),
                 ("Pilot", PILOT, "Episodes"))

    def play_open_item(self, item_id):
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        self.assertTrue(_in_store(self.app, item_id,
                                  self.app.playing_path()),
                        "offline, mpv was not handed the downloaded file")

    def test_the_next_episode_not_downloaded_stops_gracefully(self):
        _flows.open_by_search(self.app, "Pilot", PILOT, section="Episodes")
        self.play_open_item(PILOT)
        # Ten seconds of episode. Then nothing to go on to: mpv goes idle
        # and the library is back, with nothing asked of the dead network.
        self.app.wait_for(
            lambda f: self.app.prop("idle-active") is True
            and _app.shown(f, "btn-play"),
            timeout=90, what="playback to stop after the only episode")
        self.assertTrue(self.app.alive())
        rev = self.app.frame()["rev"]
        self.app.key("TAB")
        self.app.after(rev, timeout=5)      # still answering keys
        self.assertEqual([], _app_errors(self.app))
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_remove_watched_takes_what_was_watched_offline(self):
        """On a show (Remove Watched is a series and playlist gesture: a
        movie has plain Remove)."""
        _flows.open_by_search(self.app, "Pilot", PILOT, section="Episodes")
        self.play_open_item(PILOT)
        self.assertTrue(_e2e.wait_for(
            lambda: (self.catalog.userdata(PILOT).get(self.me) or {})
            .get("played"), timeout=90),
            "played to the end offline, and not marked watched")
        self.app.wait_for(lambda f: self.app.prop("idle-active") is True,
                          timeout=60, what="the episode to end")
        _flows.open_settings_tab(self.app, "downloads")
        f = self.app.wait_for(
            lambda f: any((n.get("id") or "").endswith("-rmw")
                          for n in f.get("nodes", [])),
            timeout=30, what="Remove Watched on the show's row")
        rmw = [n["id"] for n in f["nodes"]
               if (n.get("id") or "").endswith("-rmw")]
        self.assertEqual(1, len(rmw), "Remove Watched offered for %r" % rmw)
        self.app.move_to(rmw[0])
        self.app.key("ENTER")
        f = self.app.wait_for(
            lambda f: _app.shown(f, "dlg-ok")
            or self.catalog.download(PILOT) is None,
            timeout=15, what="a confirmation, or the delete itself")
        if _app.shown(f, "dlg-ok"):
            self.app.move_to("dlg-ok")
            self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(
            lambda: self.catalog.download(PILOT) is None, timeout=30),
            "the episode watched offline is still downloaded")
        store = os.path.join(self.app.config_dir, "offline", "server")
        self.assertFalse(os.path.exists(os.path.join(store, PILOT)),
                         "its files are still on disk")
        self.assertEqual("complete",
                         (self.catalog.download(self.film) or {})
                         .get("status"), "the unwatched film went too")
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_shuffle_over_the_downloads_plays_a_downloaded_copy(self):
        self.app.move_to("row-libs-offline:movies")
        self.app.press_until("ENTER",
                             lambda f: _app.shown(f, "grid-shuffle"),
                             what="the offline Movies library")
        self.app.move_to("grid-shuffle")
        self.app.key("ENTER")
        self.assertTrue(_in_store(self.app, self.film,
                                  self.app.playing_path()),
                        "Shuffle offline did not play the downloaded copy")
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_the_offline_routes_draw(self):
        """Every route a person reaches offline builds its scene. A route
        that fails to build looks like an ignored key, so the log is the
        witness: "scene build failed" is what the shell writes."""
        seen = []

        def visit(target, landed, what):
            self.app.move_to(target)
            self.app.press_until("ENTER", landed, what=what)
            seen.append(what)

        def back():
            self.app.move_to("nav-back")
            self.app.press_until("ENTER", lambda f: _app.shown(f, "row-libs"),
                                 what="Home again")

        visit("row-libs-offline:movies",
              lambda f: _app.shown(f, "grid-shuffle"), "Movies")
        back()
        visit("row-libs-offline:tv",
              lambda f: not _app.shown(f, "row-libs"), "TV")
        back()
        _flows.open_by_search(self.app, "Pilot", PILOT, section="Episodes")
        seen.append("an episode")
        for tab in ("general", "browse", "playback", "home", "servers",
                    "downloads", "logs"):
            _flows.open_settings_tab(self.app, tab)
            self.app.wait_for(lambda f: True, timeout=5)
            seen.append("settings/" + tab)
        self.app.key("ESC")
        text = _log(self.app)
        self.assertNotIn("scene build failed", text,
                         "a route failed to draw offline (walked %r)" % seen)
        self.assertEqual([], _app_errors(self.app))
        self.assertEqual(0, self.app.quit(timeout=30))


@_e2e.require_server
class AKillMidFilmKeepsTheResumeTest(unittest.TestCase):
    """Offline, the position is recorded every 30 s (player_reporting). Kill
    the app with no chance to report a stop, and the next launch still
    offers to resume from the last record: the write half of what the
    integration OfflineEndToEndTest only reads back."""

    def setUp(self):
        self.session = _e2e.Session()
        self.me = (self.session.server_id(), self.session.user_id)
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == LONG_NAME]
        self.film = films[0]["Id"]
        self.session.reset_played(self.film)
        self.addCleanup(self.session.reset_played, self.film)
        _started(self, (LONG_NAME, self.film, "Movies"))

    def recorded(self):
        row = self.catalog.userdata(self.film).get(self.me) or {}
        return (row.get("position_ticks") or 0) / 1e7

    def open_film(self):
        self.app.move_to(DOWNLOADED_TILE % self.film)
        self.app.press_until("ENTER", lambda f: _app.shown(f, "btn-play"),
                             what="the film's offline detail page")

    def test_the_position_survives_a_kill(self):
        self.open_film()
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        self.assertTrue(_in_store(self.app, self.film,
                                  self.app.playing_path()))
        # Forward by keys, the way a person skips ahead.
        deadline = time.monotonic() + 30
        while (self.app.prop("time-pos") or 0) < 90 \
                and time.monotonic() < deadline:
            self.app.key("RIGHT")
            time.sleep(0.3)
        self.assertGreaterEqual(self.app.prop("time-pos") or 0, 60,
                                "the arrow keys did not seek")
        self.assertTrue(_e2e.wait_for(lambda: self.recorded() >= 60,
                                      timeout=75),
                        "no offline progress record in 75 s (have %.0f s)"
                        % self.recorded())
        at = self.recorded()
        self.app.kill()
        self.assertGreaterEqual(self.recorded(), 60,
                                "the record did not survive the kill")
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=90, what="the offline library after the kill")
        self.open_film()
        self.app.wait_for(lambda f: _app.shown(f, "btn-resume"), timeout=15,
                          what="a Resume button after the kill")
        self.app.move_to("btn-resume")
        self.app.key("ENTER")
        self.app.playing_path()
        deadline = time.monotonic() + 30
        pos = None
        while time.monotonic() < deadline:
            pos = self.app.prop("time-pos")
            if pos and pos > 5:
                break
            time.sleep(0.3)
        self.assertIsNotNone(pos)
        self.assertLess(abs(pos - at), 15,
                        "resumed at %.0f s; the record was %.0f s"
                        % (pos, at))
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
