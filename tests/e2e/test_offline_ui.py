"""Offline sync, done the way a person does it: at the keyboard of the
shipped app, with the network taken away by a relay the test owns.

Slice S1 of the release-gate plan (~/Desktop/mpv-shim-offline-e2e-plan.md).
Each scenario asserts the rulings of docs/offline-sync.md on three layers --
what the screen shows, what the catalog holds for that person, and what the
server was told -- because the bugs this exists for are exactly those three
disagreeing.

Offline is reached by relaunching with the relay cut (download online, quit,
cut, start again): a path the app has today, so a scenario is red only for
its own bug and not for the missing "pick Offline" entry (B5), which has a
scenario of its own.

A scenario that reproduces a bug is red until its fix lands in this slice;
none is marked. (An expectedFailure would also swallow an unrelated failure
in the same test.)
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
import _relay  # noqa: E402

#: 12 seconds, and no other test uses it (docs/testing.md section 11).
FILM_NAME = "The Only Film In Its Set"
FILM_QUERY = "Only Film"
DOWNLOADED_TILE = "row-downloaded-movies#0-%s"


def _backend():
    return os.environ.get("JMS_TEST_BACKEND") or "libmpv"


class _OfflineCase(unittest.TestCase):
    """Download the film online as qa-user, then relaunch with the network
    cut. Leaves ``self.app`` on the offline Home with the film listed."""

    def setUp(self):
        self.session = _e2e.Session()
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.assertEqual(1, len(films))
        self.film = films[0]["Id"]
        self.session.reset_played(self.film)
        self.addCleanup(self.session.reset_played, self.film)
        self.me = (self.session.server_id(), self.session.user_id)

        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)

        self.app.start()
        _flows.login(self.app, self.relay)
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.download_open_item(self.app, self.catalog, self.film)
        self.app = _flows.relaunch(self.app, self.relay, cut=True)
        self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=90, what="the film in the offline library")

    def open_film(self):
        self.app.move_to(DOWNLOADED_TILE % self.film)
        self.app.key("ENTER")
        return self.app.wait_for(lambda f: _app.shown(f, "act-watched"),
                                 timeout=30, what="the offline detail page")

    def watched_on_screen(self, frame):
        """The Watched toggle wears the accent fill when on -- the same fill
        as Play (controls.action_btn: ``on`` shares ``primary``'s fill)."""
        toggle = _app.node(frame, "act-watched")
        play = _app.node(frame, "btn-play")
        self.assertIsNotNone(toggle, "no Watched button on screen")
        self.assertIsNotNone(play, "no Play button on screen")
        return toggle.get("fill") == play.get("fill")

    def toggle_watched(self):
        self.app.move_to("act-watched")
        rev = self.app.frame()["rev"]
        self.app.key("ENTER")
        return self.app.after(rev)

    def repaint(self):
        """A frame drawn after something unrelated moved -- so a state the
        screen shows has survived a rebuild, not just the optimistic flip."""
        before = self.app.frame()["nav"]
        self.app.move_to("act-fav" if before != "act-fav" else "btn-play")
        return self.app.frame()

    def played_here(self):
        row = self.catalog.userdata(self.film).get(self.me)
        return row and row.get("played")

    def log_tail(self):
        try:
            with open(self.app.log_path, encoding="utf-8",
                      errors="replace") as fh:
                return "".join(fh.readlines()[-40:])
        except OSError:
            return "(no log)"


@_e2e.require_server
class MarkWatchedOfflineTest(_OfflineCase):
    """Scenario 3. Mark played and Mark unplayed, offline.

    Ruling (offline-sync.md section 1): a deliberate mark is the one signal
    authoritative in BOTH directions, and a deliberate unwatch wins over the
    server (Q3; Izzie 2026-09-26: queued as played = 0, replayed as
    mark-unplayed when the server's mark is older -- D1)."""

    def test_marking_played_offline_holds_on_screen_and_in_the_catalog(self):
        f = self.open_film()
        self.assertFalse(self.watched_on_screen(f))
        self.toggle_watched()
        f = self.repaint()
        self.assertTrue(self.watched_on_screen(f),
                        "the tick did not survive a repaint")
        self.assertEqual(1, self.played_here(),
                         "the catalog does not say this person watched it")
        queued = [p for p in self.catalog.pending(self.film)
                  if p["user_id"] == self.me[1]]
        self.assertTrue(queued and queued[-1]["played"] == 1,
                        "no watched mark queued for the server as this "
                        "person: %r" % self.catalog.pending(self.film))

    def test_marking_unplayed_offline_holds_and_is_queued(self):
        self.open_film()
        self.toggle_watched()                          # played
        self.assertEqual(1, self.played_here())
        self.toggle_watched()                          # deliberately unplayed
        f = self.repaint()
        self.assertFalse(self.watched_on_screen(f),
                         "the un-mark flipped back on screen\n"
                         + self.log_tail())
        self.assertEqual(0, self.played_here(),
                         "the catalog still says watched")
        queued = [p for p in self.catalog.pending(self.film)
                  if p["user_id"] == self.me[1]]
        self.assertTrue(queued and queued[-1]["played"] == 0,
                        "no deliberate unwatch queued: %r" % queued)


if __name__ == "__main__":
    unittest.main()
