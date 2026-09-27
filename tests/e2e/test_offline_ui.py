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
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
import _relay  # noqa: E402

#: 12 seconds, and no other test uses it (docs/testing.md section 11).
FILM_NAME = "The Only Film In Its Set"
FILM_QUERY = "Only Film"
DOWNLOADED_TILE = "row-downloaded-movies#0-%s"


def _watched_fill(frame):
    """Whether the Watched toggle wears the accent fill -- the same fill as
    Play (controls.action_btn: ``on`` shares ``primary``'s fill) -- or None
    when either is not on screen."""
    toggle = _app.node(frame, "act-watched")
    play = _app.node(frame, "btn-play")
    if toggle is None or play is None:
        return None
    return toggle.get("fill") == play.get("fill")


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
        on = _watched_fill(frame)
        self.assertIsNotNone(on, "no Watched or Play button on screen")
        return on

    def toggle_watched(self):
        self.app.move_to("act-watched")
        was = _watched_fill(self.app.frame())
        self.app.key("ENTER")
        # The toggle's own frame, not just a newer one (see App.move_to).
        # Module-level: other classes borrow this method unbound.
        return self.app.wait_for(
            lambda f: _watched_fill(f) not in (None, was),
            timeout=10, what="the Watched toggle to flip")

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



@_e2e.require_server
class PlayOfflineTest(_OfflineCase):
    """Scenario 2. The downloaded copy plays offline, and what it records
    belongs to the person signed in.

    Ruling (offline-sync.md section 1): offline, the actor is the active
    profile's account on the row's server. `@none` is for progress nobody
    can be named for -- and here somebody can. Izzie's own catalog carried
    offline progress under `@none` (B4)."""

    def test_the_local_copy_plays_and_is_filed_under_this_person(self):
        self.open_film()
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        self.assertTrue(_in_store(self.app, self.film,
                                  self.app.playing_path()),
                        "offline, mpv was not handed the downloaded file")
        # 12 seconds of film, played to its end by the app's own reporter.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.played_here():
                break
            time.sleep(0.5)
        self.assertEqual(1, self.played_here(),
                         "playing the local copy to the end left no watched "
                         "mark for this person\n" + self.log_tail())
        nobody = [k for k in self.catalog.userdata(self.film)
                  if "@none" in k]
        self.assertEqual([], nobody,
                         "offline progress filed under nobody: %r" % nobody)
        queued = self.catalog.pending(self.film)
        self.assertTrue(queued, "nothing queued for the server")
        self.assertEqual({self.me[1]}, {p["user_id"] for p in queued},
                         "queued as someone else: %r" % queued)


def _in_store(app, item_id, path):
    """Whether mpv's `path` is this item's file in the download store."""
    store = os.path.join(app.config_dir, "offline", "server", item_id)
    try:
        return os.path.commonpath([os.path.realpath(path),
                                   os.path.realpath(store)]) \
            == os.path.realpath(store)
    except ValueError:          # a URL, or another drive
        return False


@_e2e.require_server
class _LocalCopyCase(unittest.TestCase):
    """Row 58, online: with a copy downloaded, pressing Play hands mpv the
    FILE, not the stream -- and prefer_downloaded decides. Rebuilds
    test_download_lifecycle.TheLocalCopyStandsInTest, which asserted the
    type a factory returned when called by hand (the factory the app never
    registered), with a Mock for a parent: it passed through every seam this
    walks. Here: the shipped app, a real download, mpv's own `path`, and the
    relay's request log as a second witness."""

    CONF = {}

    def setUp(self):
        session = _e2e.Session()
        films = [i for i in session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.film = films[0]["Id"]
        session.reset_played(self.film)
        self.addCleanup(session.reset_played, self.film)
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend(), conf=dict(self.CONF))
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.app.start()
        _flows.login(self.app, self.relay)
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.download_open_item(self.app, self.catalog, self.film)

    def play(self):
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        return self.app.playing_path()

    def streamed(self):
        return [p for _m, p in list(self.relay.requests)
                if self.film in p and ("/stream" in p.lower()
                                       or "/Videos/" in p)]



class TheLocalCopyStandsInTest(_LocalCopyCase):
    def test_the_downloaded_copy_plays_instead_of_the_stream(self):
        path = self.play()
        self.assertTrue(_in_store(self.app, self.film, path),
                        "online, mpv was handed %r, not the download" % path)
        self.assertEqual([], self.streamed(),
                         "the server was asked for the stream anyway")
        self.assertEqual(0, self.app.quit(timeout=30))


class TurningThePreferenceOffStreamsTest(_LocalCopyCase):
    """The other half: prefer_downloaded off streams, and keeps the copy."""

    CONF = {"prefer_downloaded": False}

    def test_it_streams_and_the_copy_stays(self):
        path = self.play()
        self.assertFalse(_in_store(self.app, self.film, path),
                         "prefer_downloaded is off and the copy played")
        self.assertTrue(path.startswith("http"), path)
        self.assertEqual("complete",
                         (self.catalog.download(self.film) or {})
                         .get("status"), "the copy was dropped")
        self.assertEqual(0, self.app.quit(timeout=30))


@_e2e.require_server
class RestartOfflineTest(_OfflineCase):
    """Scenario 9. Quit while offline and start again, still offline: what
    was recorded is still there and still this person's."""

    def test_a_mark_and_its_queue_entry_survive_a_restart(self):
        self.open_film()
        self.toggle_watched()
        self.assertEqual(1, self.played_here())
        self.app = _flows.relaunch(self.app, self.relay)   # still cut
        self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=90, what="the offline library after a restart")
        f = self.open_film()
        self.assertTrue(self.watched_on_screen(f),
                        "the mark did not survive the restart on screen")
        self.assertEqual(1, self.played_here())
        queued = [p for p in self.catalog.pending(self.film)
                  if p["user_id"] == self.me[1]]
        self.assertTrue(queued and queued[-1]["played"] == 1,
                        "the queued mark did not survive: %r"
                        % self.catalog.pending(self.film))


@_e2e.require_server
class ANewProfileStartsWithAnEmptyLoginTest(unittest.TestCase):
    """Scenario 4a. Profiles are people (offline-sync.md section 1), so a
    new profile's login form must not arrive filled in with the previous
    profile's server, username and password: one Connect then signed the
    new profile in AS the other person (B8, found building scenario 4)."""

    def setUp(self):
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.app.start()
        _flows.login(self.app, self.relay)

    def test_switching_to_a_new_profile_carries_no_credentials(self):
        _flows.add_profile(self.app, "Bob")
        self.app.wait_for(lambda f: _app.node(f, "nav-user"), timeout=15,
                          what="the profile switcher")
        _flows.switch_profile(self.app, "Bob")
        f = self.app.wait_for(lambda f: _app.shown(f, "login-connect"),
                              timeout=30, what="Bob's login screen")
        for field in ("login-server", "login-user", "login-pass"):
            self.assertFalse((_app.node(f, field) or {}).get("text"),
                             "%s arrived pre-filled for the new profile"
                             % field)
        self.app.move_to("login-connect")
        self.app.key("ENTER")
        time.sleep(5)
        self.assertEqual([], _flows.credentials(self.app.config_dir)
                         .get("Bob"),
                         "one keypress signed the new profile in as someone "
                         "else")


class _TwoProfilesCase(unittest.TestCase):
    """The default profile (Alice, qa-user) downloads the film; Bob is added
    and signed in as qa-admin; the app relaunches offline with Bob active."""

    def setUp(self):
        self.session = _e2e.Session()
        self.admin = _e2e.Session("qa-admin")
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.film = films[0]["Id"]
        for s in (self.session, self.admin):
            s.reset_played(self.film)
            self.addCleanup(s.reset_played, self.film)
        self.server = self.session.server_id()
        self.alice = (self.server, self.session.user_id)     # qa-user
        self.bob = (self.server, self.admin.user_id)         # qa-admin

        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)

        self.app.start()
        _flows.login(self.app, self.relay)                      # (default)
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.download_open_item(self.app, self.catalog, self.film)
        _flows.add_profile(self.app, "Bob")
        self.app.wait_for(lambda f: _app.node(f, "nav-user"), timeout=15,
                          what="the profile switcher")
        _flows.switch_profile(self.app, "Bob")
        _flows.add_server_from_anywhere(self.app)
        _flows.login(self.app, self.relay, account="qa-admin")
        self.app = _flows.relaunch(self.app, self.relay, cut=True)
        self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=90, what="the film in Bob's offline library")

    def _played(self, actor):
        row = self.catalog.userdata(self.film).get(actor)
        return row and row.get("played")

    def _queued_for(self, actor):
        return [p for p in self.catalog.pending(self.film)
                if (p["server_id"], p["user_id"]) == actor]

    def _switch_offline(self, name):
        self.app.key("ESC")
        self.app.wait_for(lambda f: _app.node(f, "nav-user"), timeout=15,
                          what="the profile switcher")
        _flows.switch_profile(self.app, name)


@_e2e.require_server
class TwoProfilesOfflineTest(_TwoProfilesCase):
    """Scenario 4. Two profiles, two people, one downloaded film, offline.

    Rulings (offline-sync.md section 1): watched state belongs to a person;
    offline the actor is the ACTIVE profile's account on the row's server;
    D3 case 6 (Izzie, 2026-09-26): the profile switcher shows offline and
    switching changes whose ticks are shown. B2 (no switcher offline) and B4
    (offline progress under @none) are what this reproduces."""

    def test_ticks_follow_the_profile_offline(self):
        case = _OfflineCase
        case.open_film(self)
        case.toggle_watched(self)
        self.assertEqual(1, self._played(self.bob),
                         "Bob's offline mark is not filed under Bob")
        self.assertFalse(self._played(self.alice),
                         "Bob's mark landed on Alice")
        nobody = [k for k in self.catalog.userdata(self.film)
                  if "@none" in k]
        self.assertEqual([], nobody, "filed under nobody (B4): %r" % nobody)

        # Back to the default profile, offline, through the switcher.
        self.app.key("ESC")
        self.app.wait_for(lambda f: _app.node(f, "nav-user"), timeout=15,
                          what="the profile switcher while offline (B2)")
        _flows.switch_profile(self.app, "(default)")
        self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=30, what="the default profile's offline library")
        f = case.open_film(self)
        self.assertFalse(case.watched_on_screen(self, f),
                         "the default profile shows Bob's tick")

    def test_playing_offline_is_filed_under_whoever_played_it(self):
        """Row 60: the PLAYER's offline writer, not the toggle's. Rebuilds
        test_download_lifecycle.TwoAccountsDoNotLeakTest, which wrote the
        rows under an actor it chose itself and read them back, so no
        production writer ever decided whose viewing it was."""
        case = _OfflineCase
        case.open_film(self)
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        self.assertTrue(_in_store(self.app, self.film,
                                  self.app.playing_path()))
        deadline = time.monotonic() + 90      # 12 s of film, to its end
        while time.monotonic() < deadline and not self._played(self.bob):
            time.sleep(0.5)
        self.assertEqual(1, self._played(self.bob),
                         "Bob played it to the end and it is not his")
        self.assertFalse(self._played(self.alice),
                         "Bob's viewing landed on the other profile's person")
        self.assertEqual([], [k for k in self.catalog.userdata(self.film)
                              if "@none" in k], "filed under nobody")
        self.assertEqual({self.bob[1]},
                         {p["user_id"] for p in self.catalog.pending(self.film)},
                         "queued for the server as someone else")


@_e2e.require_server
class ReconnectDeliversAsEachPersonTest(_TwoProfilesCase):
    """Scenario 5. Reconnecting delivers the queue as the person who made
    each entry -- and only the signed-in person's.

    Ruling (offline-sync.md section 3, "Lazy, per connected account"): an
    account that is not connected is not asked and not waited for; its
    entries keep until it signs in. So after reconnecting as Alice, Bob's
    queued mark must still be queued and qa-admin's server state unchanged
    (a drain through Alice's login would be B4's shape: one person's
    viewing filed as another's). Codex round 1, finding 8."""

    def _server_played(self, session):
        return bool((session.user_data(self.film) or {}).get("Played"))

    def test_only_the_signed_in_person_is_delivered_until_the_other_signs_in(
            self):
        case = _OfflineCase
        case.open_film(self)                 # Bob, offline
        case.toggle_watched(self)
        self.assertEqual(1, self._played(self.bob))
        self._switch_offline("(default)")              # Alice, offline
        self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=30, what="Alice's offline library")
        f = case.open_film(self)
        reg = _flows.users(self.app.config_dir) or {}
        active = [u.get("name") for u in reg.get("users", [])
                  if u.get("id") == reg.get("active")]
        self.assertFalse(case.watched_on_screen(self, f),
                         "after switching to Alice the screen shows a tick "
                         "(active profile on disk: %r)" % active)
        after = case.toggle_watched(self)
        self.assertTrue(case.watched_on_screen(self, case.repaint(self)),
                        "Alice's toggle did not register on screen "
                        "(active on disk: %r)" % active)
        self.assertEqual(1, self._played(self.alice),
                         "Alice's offline mark is not filed under Alice: "
                         "userdata=%r pending=%r"
                         % (self.catalog.userdata(self.film),
                            self.catalog.pending(self.film)))

        self.relay.restore()
        self.app.key("ESC")
        self.app.move_to("banner-retry")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(lambda: self._server_played(
            self.session), timeout=120),
            "Alice's queued mark never reached the server as qa-user")
        self.assertTrue(_e2e.wait_for(
            lambda: not self._queued_for(self.alice), timeout=30),
            "Alice's entry stayed queued after delivery")
        # Lazy per account: Bob is not signed in, so nothing speaks for him.
        self.assertTrue(self._queued_for(self.bob),
                        "Bob's entry was drained without Bob signed in")
        self.assertFalse(self._server_played(self.admin),
                         "Bob's mark reached qa-admin through Alice's login")

        _flows.switch_profile(self.app, "Bob")   # online now
        self.assertTrue(_e2e.wait_for(lambda: self._server_played(
            self.admin), timeout=120),
            "Bob's queued mark never reached the server once he signed in")
        self.assertTrue(_e2e.wait_for(
            lambda: not self._queued_for(self.bob), timeout=30))


@_e2e.require_server
class UnwatchOnlineReachesTheCopyTest(unittest.TestCase):
    """Scenario 7, this app's half. Online, with the film downloaded:
    unmarking it -- from the library, or from the player's "Quit and Mark
    Unplayed" -- must reach the copy on disk at once, not only the server.

    Ruling (offline-sync.md section 1): a deliberate mark is verbatim both
    ways, at every call site, whether or not the item is downloaded. B3 was
    Izzie's report that an online unwatch left the copy watched; B6 is the
    player path writing nothing to the copy (offline_media.set_played passes
    played=None for an unwatch)."""

    def setUp(self):
        self.session = _e2e.Session()
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
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
        # Watched, from the detail page, online.
        _OfflineCase.toggle_watched(self)
        self.assertTrue(_e2e.wait_for(lambda: self._server_played(),
                                      timeout=30))
        self.assertTrue(_e2e.wait_for(lambda: self._copy_played() == 1,
                                      timeout=15),
                        "the online mark did not reach the copy")

    def _server_played(self):
        return bool((self.session.user_data(self.film) or {}).get("Played"))

    def _copy_played(self):
        row = self.catalog.userdata(self.film).get(self.me)
        return row and row.get("played")

    def test_unmarking_from_the_library_reaches_the_copy(self):
        _OfflineCase.toggle_watched(self)
        self.assertTrue(_e2e.wait_for(lambda: not self._server_played(),
                                      timeout=30))
        self.assertTrue(_e2e.wait_for(lambda: self._copy_played() == 0,
                                      timeout=15),
                        "the copy on disk still says watched (B3)")

    def test_another_clients_watch_arrives_by_push(self):
        """Row 62, A2:85: a watch made in another client reaches the copy
        from the websocket push, at once -- advancing is what the push path
        is for (F47 keeps retreats for the sweep). Through the app's own
        socket, so the push is filed by the registry's answer for that
        socket, the branch the old PushedUserDataReachesTheCatalogTest never
        ran (it called the handler directly with an unregistered client)."""
        _OfflineCase.toggle_watched(self)                 # start unwatched
        self.assertTrue(_e2e.wait_for(lambda: self._copy_played() == 0,
                                      timeout=15))
        self.session._request("/UserPlayedItems/%s" % self.film, "POST")
        self.assertTrue(_e2e.wait_for(lambda: self._copy_played() == 1,
                                      timeout=20),
                        "another client's watch did not reach the copy by "
                        "push")
        nobody = [k for k in self.catalog.userdata(self.film)
                  if "@none" in k]
        self.assertEqual([], nobody, "the push was filed under nobody")
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_another_clients_unwatch_arrives_with_the_sweep(self):
        """F47 (ratified): an unwatch made in ANOTHER client is not applied
        from the websocket's announcement; it reaches the copy at the next
        sweep. A sweep runs at launch (after the 60 s settle), so the step
        that triggers it is a relaunch -- online, nothing cut."""
        self.session._request("/UserPlayedItems/%s" % self.film, "DELETE")
        self.assertFalse(self._server_played())
        time.sleep(3)
        self.assertEqual(1, self._copy_played(),
                         "the copy retreated before a sweep: the websocket "
                         "path is advance-only by ruling (F47)")
        self.app = _flows.relaunch(self.app)
        self.assertTrue(_e2e.wait_for(lambda: self._copy_played() == 0,
                                      timeout=180),
                        "the sweep after relaunch did not bring the other "
                        "client's unwatch to the copy")

    def test_quit_and_mark_unplayed_reaches_the_copy(self):
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: f.get("phud_mode") or not _app.shown(
            f, "btn-play"), timeout=30, what="playback to start")
        time.sleep(2)
        self.app.key("u")                       # kb_unwatched
        self.assertTrue(_e2e.wait_for(lambda: not self._server_played(),
                                      timeout=30),
                        "the server was not told")
        self.assertTrue(_e2e.wait_for(lambda: self._copy_played() == 0,
                                      timeout=15),
                        "the copy on disk still says watched (B6): %r"
                        % self.catalog.userdata(self.film))


@_e2e.require_server
class ConflictsAtReconnectTest(_OfflineCase):
    """Scenario 6. What the offline session sends when the server has moved
    on meanwhile (G2; D1 ruled 2026-09-26).

    Each case sets the server state it needs through the API -- another
    client's doing -- then reconnects with the relay restored and Retry,
    and reads the server as the only judge."""

    TICKS = 10_000_000

    def _server(self):
        return self.session.user_data(self.film) or {}

    def _mark_on_server(self, played):
        self.session._request("/UserPlayedItems/%s" % self.film,
                              "POST" if played else "DELETE")

    def _reconnect(self):
        self.relay.restore()
        self.app.key("ESC")
        self.app.move_to("banner-retry")
        self.app.key("ENTER")
        # Delivered when the queue has drained for this person.
        self.assertTrue(_e2e.wait_for(
            lambda: not [p for p in self.catalog.pending(self.film)
                         if p["user_id"] == self.me[1]], timeout=120),
            "the queue never drained after reconnecting: %r"
            % self.catalog.pending(self.film))

    def test_an_offline_unwatch_beats_an_older_server_watch(self):
        self._mark_on_server(True)            # before the unwatch
        time.sleep(1.2)
        self.open_film()
        self.toggle_watched()                 # watched (locally)
        self.toggle_watched()                 # deliberately unwatched
        self.assertEqual(0, self.played_here())
        self._reconnect()
        self.assertFalse(self._server().get("Played"),
                         "the older server watch survived a later unwatch")

    def test_a_newer_server_watch_beats_an_offline_unwatch(self):
        self.open_film()
        self.toggle_watched()
        self.toggle_watched()                 # deliberately unwatched
        time.sleep(1.2)
        self._mark_on_server(True)            # another device, afterwards
        self._reconnect()
        self.assertTrue(self._server().get("Played"),
                        "an older offline unwatch undid a newer watch")

    def test_ordinary_offline_progress_does_not_clear_a_server_watch(self):
        self._mark_on_server(True)
        self.open_film()
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        time.sleep(4)
        self.app.key("q")                      # stop partway (kb_stop)
        self.app.wait_for(lambda f: _app.shown(f, "btn-play"), timeout=30,
                          what="the detail page after stopping")
        self._reconnect()
        self.assertTrue(self._server().get("Played"),
                        "offline progress (no deliberate mark) cleared a "
                        "watched mark on the server")

    def test_a_stale_offline_position_does_not_rewind_the_server(self):
        far = 9 * self.TICKS
        self.session._request(
            "/UserItems/%s/UserData" % self.film, "POST",
            {"PlaybackPositionTicks": far})
        self.open_film()
        self.app.move_to("btn-play")
        self.app.key("ENTER")
        time.sleep(3)
        self.app.key("q")
        self.app.wait_for(lambda f: _app.shown(f, "btn-play"), timeout=30,
                          what="the detail page after stopping")
        self._reconnect()
        self.assertGreaterEqual(
            self._server().get("PlaybackPositionTicks") or 0, far,
            "the offline position rewound the server's newer progress")


@_e2e.require_server
class AnInactiveProfilesDownloadTest(unittest.TestCase):
    """Scenario 6b. D2, ruled 2026-09-26: a download queued by a profile
    that is no longer active is fetched with THAT profile's saved credential
    -- "just a credential", no websocket -- and never with anyone else's.

    The relay holds the default profile's file request; the app switches to
    Bob and quits with it still held; the request is dropped and the app
    relaunched with Bob active. The row is then picked up fresh with nobody
    signed in for it, so the only way it finishes is D2 -- and the relay's
    token record settles whose credential fetched it. Checked to fail with
    D2 unwired."""

    DOWNLOAD = r"^/Items/[^/?]+/Download"

    def setUp(self):
        self.session = _e2e.Session()
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.film = films[0]["Id"]
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.app.start()
        _flows.login(self.app, self.relay)                   # (default)
        _flows.add_profile(self.app, "Bob")
        self.app.wait_for(lambda f: _app.node(f, "nav-user"), timeout=15,
                          what="the profile switcher")
        _flows.switch_profile(self.app, "Bob")
        _flows.add_server_from_anywhere(self.app)
        _flows.login(self.app, self.relay, account="qa-admin")
        _flows.switch_profile(self.app, "(default)")

    def _tokens(self):
        reg = _flows.users(self.app.config_dir) or {}
        out = {}
        for u in reg.get("users", []):
            for c in u.get("credentials") or ():
                out[u.get("name")] = c.get("AccessToken")
        return out

    def queue_then_leave_as_bob(self):
        """The default profile queues the film; the app switches to Bob and
        quits with the file request held, then comes back as Bob. Why the
        quit: see the first test."""
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="the default profile's home")
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        self.relay.hold(self.DOWNLOAD)
        self.app.move_to("act-download")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "dl-ok"), timeout=15,
                          what="the download dialog")
        self.app.move_to("dl-ok")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(lambda: self.relay.held_count() > 0,
                                      timeout=60),
                        "the file request never reached the relay")
        _flows.switch_profile(self.app, "Bob")
        self.app.quit(timeout=90)
        self.relay.drop_held()

    def relaunch(self):
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.assertEqual("Bob", _flows.active_profile(self.app.config_dir))

    def test_a_revoked_login_waits_and_says_so(self):
        """6b's other half: the default profile's login is signed out on
        the server (as signing out elsewhere does), so D2 is refused. The
        download waits, and the Downloads screen says for whom (Izzie,
        2026-09-26: "Might be worth making it say 'waiting...'")."""
        self.queue_then_leave_as_bob()
        token = self._tokens()["(default)"]
        req = urllib.request.Request(
            _e2e.SERVER + "/Sessions/Logout", method="POST", data=b"",
            headers={"Authorization": 'MediaBrowser Token="%s"' % token})
        urllib.request.urlopen(req, timeout=15).read()
        self.relaunch()
        _flows.open_settings_tab(self.app, "downloads")
        want = "Waiting for (default) to sign in"
        self.app.wait_for(
            lambda f: any(want in (t or "") for t in _app.texts(f)),
            timeout=90, what="the download to say whose sign-in it waits for")
        self.assertNotEqual("complete",
                            (self.catalog.download(self.film) or {})
                            .get("status"))
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_it_is_finished_as_the_person_who_queued_it(self):
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="the default profile's home")
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        self.relay.hold(self.DOWNLOAD)
        self.app.move_to("act-download")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "dl-ok"), timeout=15,
                          what="the download dialog")
        self.app.move_to("dl-ok")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(lambda: self.relay.held_count() > 0,
                                      timeout=60),
                        "the file request never reached the relay")
        _flows.switch_profile(self.app, "Bob")
        # Still holding, so no retry can finish on the old session: a
        # stopped client's HTTP session keeps working, and a retry inside
        # the same download finished the file as the default profile
        # without D2 ever being asked (this test's first version did exactly
        # that, and passed with D2 unwired). Quit with the request held,
        # drop it, and start again as Bob: the row is then picked up fresh,
        # with nobody signed in for it.
        self.app.quit(timeout=90)
        self.relay.drop_held()
        # Left "downloading"; startup requeues it (SyncManager.start).
        self.assertNotEqual("complete",
                            (self.catalog.download(self.film) or {})
                            .get("status"),
                            "the download finished before the quit")
        self.relay.request_tokens.clear()
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.assertEqual("Bob", _flows.active_profile(self.app.config_dir))

        deadline = time.monotonic() + 120
        row = None
        while time.monotonic() < deadline:
            row = self.catalog.download(self.film)
            if row and row.get("status") == "complete":
                break
            time.sleep(0.5)
        self.assertEqual("complete", (row or {}).get("status"),
                         "the default profile's queued download never "
                         "finished while Bob was active: %r" % row)
        self.assertEqual(self.session.user_id, row["requested_user_id"])
        tokens = self._tokens()
        fetched = [tok for path, tok in self.relay.request_tokens
                   if "/Download" in path]
        self.assertIn(tokens["(default)"], fetched,
                      "the file was not fetched with the default profile's "
                      "own credential")
        self.assertNotIn(tokens["Bob"], fetched,
                         "the default profile's file was fetched as Bob")


@_e2e.require_server
class _OfflineEntryCase(unittest.TestCase):
    OFFLINE_ENTRY = "Offline"
    CONF = {}

    def setUp(self):
        session = _e2e.Session()
        films = [i for i in session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.film = films[0]["Id"]
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend(), conf=dict(self.CONF))
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.app.start()
        _flows.login(self.app, self.relay)

    def entries(self):
        return _flows.items(self.app.frame(), "nav-server")

    def download_the_film(self):
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.download_open_item(self.app, self.catalog, self.film)
        self.app.move_to("nav-home")
        self.app.press_until("ENTER",
                             lambda f: _app.shown(f, "row-libs"),
                             what="Home")

    def pick_offline(self):
        entries = self.app.wait_for(
            lambda f: self.OFFLINE_ENTRY in _flows.items(f, "nav-server"),
            timeout=30, what="an Offline entry in the server drop-down")
        _flows.pick(self.app, "nav-server",
                    _flows.items(entries, "nav-server")
                    .index(self.OFFLINE_ENTRY))
        return self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=30, what="the offline library after picking Offline")



class TheOfflineEntryTest(_OfflineEntryCase):
    """Scenario 11, D3 (ruled 2026-09-26): drop to offline while a server is
    unreliable and come back, without a restart or faking an outage. The
    server drop-down carries an Offline entry (folder icon) once something
    is downloaded; picking it is the whole-browser switch, and picking the
    server again reconnects -- or stays offline and says why. Asserts the
    requirement through the widget Izzie chose; the widget is not sacred."""

    def test_offline_is_offered_picked_and_left_without_a_restart(self):
        self.assertNotIn(self.OFFLINE_ENTRY, self.entries(),
                         "Offline is offered with nothing downloaded "
                         "(D3 case 4)")
        self.download_the_film()
        pid = self.app.proc.pid
        f = self.pick_offline()                                  # case 2
        # Picked, so it is the server being browsed, not a fallback: no
        # banner (Izzie, 2026-09-26).
        self.assertFalse(_app.shown(f, "banner-retry")
                         or _app.shown(f, "banner-servers"),
                         "a chosen Offline shows the fallback banner")
        self.assertEqual("pass", self.relay.mode)
        self.assertEqual(pid, self.app.proc.pid, "the app restarted")
        entries = _flows.items(f, "nav-server")
        self.assertEqual(entries.index(self.OFFLINE_ENTRY),
                         _flows.selected(f, "nav-server"),
                         "offline, but the drop-down does not say so")
        # Back: the server is right there, so this reconnects (case 3).
        server = next(i for i, e in enumerate(entries)
                      if e != self.OFFLINE_ENTRY)
        _flows.pick(self.app, "nav-server", server)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs")
                          and not _app.shown(f, DOWNLOADED_TILE % self.film),
                          timeout=60, what="the server's Home again")
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_picking_the_server_while_it_is_down_stays_offline(self):
        self.download_the_film()
        f = self.pick_offline()
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        entries = _flows.items(f, "nav-server")
        server = next(i for i, e in enumerate(entries)
                      if e != self.OFFLINE_ENTRY)
        _flows.pick(self.app, "nav-server", server)
        # Case 3's failure half: told why, and still in the offline library.
        f = self.app.wait_for(lambda f: f.get("modal_open"), timeout=60,
                              what="a message saying the server is down")
        self.assertTrue(_app.shown(f, DOWNLOADED_TILE % self.film)
                        or _app.node(f, DOWNLOADED_TILE % self.film),
                        "left the offline library for a server that is down")
        self.app.key("ESC")
        f = self.app.wait_for(lambda f: not f.get("modal_open"), timeout=10,
                              what="the message dismissed")
        self.assertTrue(_app.shown(f, DOWNLOADED_TILE % self.film))
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_the_switcher_does_not_keep_showing_the_server_that_refused(self):
        """A refusal answers with the value the pick began on, which mpvtk
        took for a stale repaint, so the refused server stayed drawn as
        chosen over the downloads. The app now answers through the
        Dropdown's `ack` (mpvtk GUIDE section 2)."""
        self.download_the_film()
        f = self.pick_offline()
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        entries = _flows.items(f, "nav-server")
        _flows.pick(self.app, "nav-server",
                    next(i for i, e in enumerate(entries)
                         if e != self.OFFLINE_ENTRY))
        self.app.wait_for(lambda f: f.get("modal_open"), timeout=60,
                          what="the refusal")
        self.app.key("ESC")
        f = self.app.wait_for(lambda f: not f.get("modal_open"), timeout=10,
                              what="the message dismissed")
        self.assertEqual(entries.index(self.OFFLINE_ENTRY),
                         _flows.selected(f, "nav-server"))
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_launching_with_the_server_down_selects_offline(self):
        self.download_the_film()
        self.app = _flows.relaunch(self.app, self.relay, cut=True)
        f = self.app.wait_for(
            lambda f: _app.shown(f, DOWNLOADED_TILE % self.film),
            timeout=90, what="the offline library at launch")   # case 5
        f = self.app.wait_for(
            lambda f: self.OFFLINE_ENTRY in _flows.items(f, "nav-server"),
            timeout=30, what="the Offline entry at launch")
        self.assertEqual(_flows.items(f, "nav-server")
                         .index(self.OFFLINE_ENTRY),
                         _flows.selected(f, "nav-server"))
        self.assertEqual(0, self.app.quit(timeout=30))


class ABounceDoesNotPullYouOutTest(_OfflineEntryCase):
    """Not ruled in D3; inferred (register, "Scenario 11"): Offline is what
    you pick while a server keeps bouncing, so its coming back is not you
    leaving. The bounce that matters is the one clientManager sees -- the
    health check drops the server, then reconnects it, and the second half
    is what rebuilt the live source (ui._on_server_connected). A bare
    websocket redial is not it: the apiclient redials inside WSClient and
    tells nobody, and a first version of this test bounced that way and
    passed with the guard removed. Carve-out: health_check_interval seeded
    short, as in scenario 8."""

    CONF = {"health_check_interval": 10}

    def server_label(self):
        return next((e for e in self.entries() if e != self.OFFLINE_ENTRY),
                    "")

    def test_the_server_coming_back_leaves_you_offline(self):
        self.download_the_film()
        self.pick_offline()
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        self.app.wait_for(
            lambda f: "needs reconnect" in self.server_label(), timeout=90,
            what="a health check to drop the server")
        self.relay.restore()
        # The reconnect lands, and the library must still be there after.
        self.app.wait_for(
            lambda f: self.server_label()
            and "needs reconnect" not in self.server_label(), timeout=90,
            what="a health check to bring the server back")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            f = self.app.frame()
            self.assertTrue(_app.shown(f, DOWNLOADED_TILE % self.film),
                            "the server coming back took the offline "
                            "library away")
            time.sleep(0.25)
        self.assertEqual(_flows.items(f, "nav-server")
                         .index(self.OFFLINE_ENTRY),
                         _flows.selected(f, "nav-server"))
        self.assertEqual(0, self.app.quit(timeout=30))


class AMidPageDropTest(_OfflineEntryCase):
    """D3 case 1: the server drops while you are on a page. Nothing switches
    by itself; the server's entry reads "needs reconnect"; the next fetch
    shows its load error AND the retry/offline banner (Izzie, 2026-09-26:
    "Once the user tries to do something and the request fails hard it
    should probably show the retry/offline banner"). A search is that fetch:
    a failed Home drops to the downloads plus the offline banner instead,
    also ruled. Carve-out: health_check_interval seeded short, as in
    scenario 8."""

    CONF = {"health_check_interval": 10}

    def test_nothing_switches_and_the_entry_says_so(self):
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.download_open_item(self.app, self.catalog, self.film)
        self.app.wait_for(
            lambda f: self.OFFLINE_ENTRY in _flows.items(f, "nav-server"),
            timeout=30, what="the Offline entry once downloaded")
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        self.app.wait_for(
            lambda f: any("needs reconnect" in e
                          for e in _flows.items(f, "nav-server")),
            timeout=60, what="the server's entry to read needs reconnect")
        f = self.app.frame()
        self.assertTrue(_app.shown(f, "btn-play"),
                        "the page did not stay put when the server dropped")
        self.assertNotEqual(_flows.items(f, "nav-server")
                            .index(self.OFFLINE_ENTRY),
                            _flows.selected(f, "nav-server"),
                            "switched to Offline by itself")
        self.app.clear_field("nav-search")
        self.app.type_into("nav-search", "Only")
        self.app.key("ENTER")
        f = self.app.wait_for(lambda f: _app.shown(f, "route-retry"),
                              timeout=120, what="the search's load error")
        self.assertTrue(_app.shown(f, "banner-unreachable-retry"),
                        "a hard failure did not raise the retry banner")
        self.assertTrue(_app.shown(f, "banner-unreachable-offline"),
                        "the banner offers no way to the downloads")
        self.assertEqual(0, self.app.quit(timeout=30))


@_e2e.require_server
class AStalledNetworkTest(unittest.TestCase):
    """Scenario 8. The network stops answering without closing anything (a
    dropped Wi-Fi, a sleeping router): the app must keep answering keys, and
    quitting must end cleanly -- inside the exit watchdog's 20 s, rc 0.

    Carve-out: ``health_check_interval`` is seeded short so a health check
    is in flight when the person quits, which the 300 s default makes a
    five-minute wait. Found by measurement: a check parked in its /Sessions
    retries (about 80 s under a stall) held the shutdown until the watchdog
    killed it.

    Not asserted yet: how soon the app SAYS it is offline. No budget has
    been ruled (register, "Scenario 8 measured")."""

    CHECK_EVERY = 15

    def setUp(self):
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend(),
                            conf={"health_check_interval": self.CHECK_EVERY})
        self.addCleanup(lambda: self.app.close())
        self.app.start()
        _flows.login(self.app, self.relay)

    def _checks_started(self):
        try:
            with open(self.app.log_path, encoding="utf-8",
                      errors="replace") as fh:
                return fh.read().count("Performing client health check")
        except OSError:
            return 0

    def test_keys_answer_and_quit_is_clean_while_stalled(self):
        before = self._checks_started()
        self.relay.stall()
        self.assertTrue(self.relay.probe_silent(), "the stall is not in effect")
        worst = 0.0
        for key in ("RIGHT", "LEFT", "DOWN", "UP") * 3:
            rev = self.app.frame().get("rev", 0)
            start = time.monotonic()
            self.app.key(key)
            self.app.after(rev, timeout=5)
            worst = max(worst, time.monotonic() - start)
        self.assertLess(worst, 1.0, "a key took %.2fs to answer while the "
                                    "network was stalled" % worst)
        # Quit while a health check is inside its network call: the log line
        # is written as the check starts, and its first request then waits
        # out a 10 s read timeout.
        self.assertTrue(
            _e2e.wait_for(lambda: self._checks_started() > before,
                          timeout=self.CHECK_EVERY * 3),
            "no health check started during the stall")
        start = time.monotonic()
        rc = self.app.quit(timeout=40)
        took = time.monotonic() - start
        self.assertEqual(0, rc, "quitting while stalled did not exit cleanly "
                                "(%.1fs; the watchdog's forced exit is 1)"
                                % took)
        self.assertLess(took, 20.0)


if __name__ == "__main__":
    unittest.main()
