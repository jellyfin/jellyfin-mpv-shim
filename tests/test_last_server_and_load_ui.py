"""The remembered server, and the browser's loading / failure screens.

Both exist because of the same class of problem: state the user can see was
being decided by whatever happened to come first. The default server was
"whichever connected first" (connection order sorts by network locality, so
it changed between launches), and a playback start showed a blank window
whether it was loading or had already failed.
"""

import sys
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the browser reaches args.get_args()

from jellyfin_mpv_shim.mpvtk.layout import layout  # noqa: E402
from jellyfin_mpv_shim.mpvtk.widgets import Busy  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import home_sections
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.users import UserManager  # noqa: E402


class FakeSource:
    def __init__(self, servers):
        self._servers = servers

    def servers(self):
        return list(self._servers)

    def get_libraries(self, server_uuid):
        return []

    def get_home_prefs(self, server_uuid, refresh=False):
        return list(home_sections.DEFAULT_LAYOUT), frozenset()

    def get_home_rows(self, server_uuid, libraries=None, sections=None,
                      layout=None, latest_excludes=None):
        return []


class FakeController:
    """Records what the browser asked the shim to persist / retry."""

    def __init__(self, last_server=None):
        self.last_server = last_server
        self.saved = []
        self.retries = []
        self.cancelled = False

    def get_last_server(self):
        return self.last_server

    def set_last_server(self, uuid):
        self.saved.append(uuid)
        self.last_server = uuid

    def retry_playback(self, force_transcode=False):
        self.retries.append(force_transcode)
        return True

    def cancel_load(self):
        self.cancelled = True
        return True

    # The browser touches these on browse/yield transitions.
    def on_browse_enter(self):
        pass

    def on_browse_leave(self):
        pass

    def use_hud(self):
        return False


SERVERS = [{"uuid": "srv1", "name": "First"},
           {"uuid": "srv2", "name": "Second"}]


def build_browser(controller, servers=SERVERS):
    return MpvtkBrowser(None, FakeSource(servers), controller=controller)


class RememberedServerTest(unittest.TestCase):
    def test_the_remembered_server_wins_over_connection_order(self):
        b = build_browser(FakeController(last_server="srv2"))
        self.assertEqual(b.server, "srv2")

    def test_falls_back_to_the_first_when_nothing_is_remembered(self):
        self.assertEqual(build_browser(FakeController()).server, "srv1")

    def test_a_remembered_server_that_is_gone_falls_back(self):
        """It may have been removed, or simply be down this launch — either
        way the browser must not open on a server that isn't there."""
        b = build_browser(FakeController(last_server="deleted-server"))
        self.assertEqual(b.server, "srv1")

    def test_no_servers_yields_no_selection(self):
        b = build_browser(FakeController(last_server="srv2"), servers=[])
        self.assertIsNone(b.server)

    def test_an_explicit_request_still_wins(self):
        """set_source passes a server through on reconnect; that must not be
        overridden by the remembered one."""
        c = FakeController(last_server="srv2")
        b = MpvtkBrowser(None, FakeSource(SERVERS), controller=c,
                         server_uuid="srv1")
        self.assertEqual(b.server, "srv1")

    def test_switching_servers_persists_the_choice(self):
        c = FakeController()
        b = build_browser(c)
        b._switch_server("srv2")
        self.assertEqual(c.saved, ["srv2"],
                         "the switch was not remembered for next launch")

    def test_a_controller_without_the_methods_is_tolerated(self):
        """Offline (controller=None) and the stub controllers in other tests
        must not start raising."""
        b = MpvtkBrowser(None, FakeSource(SERVERS), controller=None)
        self.assertEqual(b.server, "srv1")
        b._remember_server("srv2")   # must not raise


class LastServerStorageTest(unittest.TestCase):
    """Storage is per-user: with multiple profiles, a shared value would send
    one user to another's server."""

    def _manager(self, tmpdir):
        import jellyfin_mpv_shim.users as users_module

        mgr = UserManager()
        mgr._path = lambda: tmpdir + "/users.json"
        mgr.users = [users_module.UserManager._new_user("A", is_default=True),
                     users_module.UserManager._new_user("B")]
        mgr.active_id = mgr.users[0]["id"]
        mgr._loaded = True
        return mgr

    def test_round_trips_for_the_active_user(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            self.assertIsNone(mgr.get_last_server())
            mgr.set_last_server("srv2")
            self.assertEqual(mgr.get_last_server(), "srv2")

    def test_each_user_keeps_their_own(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            mgr.set_last_server("srv1")
            mgr.active_id = mgr.users[1]["id"]
            self.assertIsNone(mgr.get_last_server(),
                              "the second user inherited the first's server")
            mgr.set_last_server("srv2")
            mgr.active_id = mgr.users[0]["id"]
            self.assertEqual(mgr.get_last_server(), "srv1")

    def test_an_older_users_file_without_the_key_normalizes(self):
        self.assertIsNone(
            UserManager._normalize({"id": "u1", "name": "A"})["last_server"])


class NonPlaybackFailureIsAToastTest(unittest.TestCase):
    """A failure that never took the window (audio, with the library still on
    screen) must not knock the user out of the page they are using. The
    full-screen error is reserved for a failure that owns the window."""

    def setUp(self):
        self.browser = build_browser(FakeController())
        self.browser._browsing = True      # audio: the library stays up

    def test_an_audio_failure_toasts_instead_of_knocking_out(self):
        self.browser.on_load_error({"title": "Some Song",
                                    "detail": "tls: Error decoding",
                                    "timed_out": False, "can_transcode": True})
        self.assertIsNone(self.browser._load_error,
                          "a failed track took over the whole window")
        self.assertIn("Some Song", self.browser.status)
        self.assertIn("tls: Error decoding", self.browser.status)

    def test_the_library_is_still_what_renders(self):
        self.browser.on_load_error({"title": "Some Song", "timed_out": True,
                                    "can_transcode": False})
        self.assertTrue(self.browser._browsing)

    def test_a_timeout_toast_says_timed_out(self):
        self.browser.on_load_error({"title": "S", "timed_out": True,
                                    "can_transcode": False})
        self.assertIn("Timed out", self.browser.status)


class LoadingAndFailureScreenTest(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.browser = build_browser(self.controller)
        self.browser._browsing = False      # yielded to video, as during a load

    def _text(self):
        """Every string in the rendered scene."""
        scene = self.browser.build((1280, 720))
        found = []
        layout(scene, 1280, 720)

        def walk(node):
            text = getattr(node, "text", None)
            if isinstance(text, str):
                found.append(text)
            for child in (getattr(node, "children", None) or []):
                walk(child)

        walk(scene)
        return found

    def _slow(self, browser=None):
        """Age the in-flight load past the spinner's grace period."""
        b = browser or self.browser
        if b._starting is not None:
            b._starting["at"] = time.time() - (b.SPINNER_DELAY + 1)
        return b

    def _has_spinner(self):
        scene = self.browser.build((1280, 720))
        found = []

        def walk(node):
            if isinstance(node, Busy):
                found.append(node)
            for child in (getattr(node, "children", None) or []):
                walk(child)

        walk(scene)
        return bool(found)

    def test_a_load_in_flight_shows_a_spinner(self):
        """Busy animates renderer-side, so it can sit through a 30s stall
        without costing a single repaint from here."""
        self.browser.on_load_start({"title": "Some Movie"})
        self._slow()
        self.assertTrue(self._has_spinner(),
                        "a slow load shows no spinner")

    def test_a_second_load_still_gets_its_spinner(self):
        """The timer slot holds one thread, so a start beginning while one is
        pending has its own arm dropped. A flat sleep would then fire early
        against the new load, find it not yet due, and schedule nothing
        further — that load then never showed a spinner however long it ran.
        """
        b = self.browser
        b.on_load_start({"title": "First"})
        first_timer = b._spinner_timer
        self.assertIsNotNone(first_timer, "no spinner timer was armed")

        # A second start lands while the first timer is still pending.
        b._starting = None
        b.on_load_start({"title": "Second"})
        # Whether or not a fresh thread was started, the pending one must
        # keep waiting until THIS load is due rather than firing early.
        self._slow()
        self.assertTrue(self._has_spinner(),
                        "the second load never got a spinner")

    def test_the_timer_stops_waiting_when_the_load_resolves(self):
        b = self.browser
        b.on_load_start({"title": "Movie"})
        b.on_playstate({"stopped": False, "is_audio": False, "id": "m1",
                        "title": "Movie", "position": 1, "duration": 100})
        timer = b._spinner_timer
        if timer is not None:
            timer.join(5)
            self.assertFalse(timer.is_alive(),
                             "the spinner timer outlived the load")

    def test_a_quick_load_never_flashes_a_spinner(self):
        """Most starts land well inside the grace period, and a spinner that
        appears and vanishes reads worse than the brief nothing it
        replaces."""
        self.browser.on_load_start({"title": "Some Movie"})
        self.assertFalse(self._has_spinner(),
                         "the spinner flashed up on a fast load")

    def test_no_spinner_once_playback_reports_in(self):
        self.browser.on_load_start({"title": "Some Movie"})
        self._slow()
        self.browser.on_playstate({"stopped": False, "is_audio": False,
                                   "id": "m1", "title": "Some Movie",
                                   "position": 1, "duration": 100})
        self.assertFalse(self._has_spinner(),
                         "the spinner outlived the load")

    def test_no_spinner_on_the_error_screen(self):
        self.browser.on_load_error({"title": "X", "timed_out": True,
                                    "can_transcode": True})
        self.assertFalse(self._has_spinner(),
                         "a failed load must not look like it is still trying")

    def test_the_spinner_is_named_at_click_time(self):
        """_start runs on the click; on_load_start only lands after the
        PlaybackInfo round trip, which is itself part of the wait."""
        self.browser._start(audio=False, title="Clicked Movie")
        self.assertEqual((self.browser._starting or {}).get("title"),
                         "Clicked Movie")

    def test_starting_playback_does_not_yield_the_window_yet(self):
        """The yield blanks our scene (HUD mode is attached-but-blank), so
        yielding at play intent is what made the load show nothing."""
        b = build_browser(FakeController())
        b._browsing = True
        b._start(audio=False, title="Movie")
        self.assertIsNotNone(b._starting)
        self._slow(b)
        self.assertTrue(self._scene_has_spinner(b),
                        "the spinner was thrown away by an early yield")

    def test_the_window_is_handed_off_once_playback_reports_in(self):
        b = build_browser(FakeController())
        b._browsing = True
        b._start(audio=False, title="Movie")
        b.on_playstate({"stopped": False, "is_audio": False, "id": "m1",
                        "title": "Movie", "position": 1, "duration": 100})
        self.assertIsNone(b._starting)
        self.assertFalse(b._browsing, "the window was never yielded to video")
        self.assertFalse(self._scene_has_spinner(b),
                         "the spinner is still covering the picture")

    @staticmethod
    def _scene_has_spinner(b):
        scene = b.build((1280, 720))
        found = []

        def walk(node):
            if isinstance(node, Busy):
                found.append(node)
            for child in (getattr(node, "children", None) or []):
                walk(child)

        walk(scene)
        return bool(found)

    def test_the_player_title_does_not_blank_the_click_title(self):
        self.browser._start(audio=False, title="Clicked Movie")
        self.browser.on_load_start({})
        self.assertEqual((self.browser._starting or {}).get("title"),
                         "Clicked Movie")

    def test_a_load_in_flight_says_so(self):
        """The window used to go blank for the whole load — up to
        playback_timeout — with nothing distinguishing it from a failure."""
        self.browser.on_load_start({"title": "Some Movie"})
        self._slow()
        text = self._text()
        self.assertTrue(any("Loading" in t for t in text), text)
        self.assertIn("Some Movie", text)

    def test_a_failure_shows_the_reason_and_the_retries(self):
        self.browser.on_load_error({"title": "Some Movie",
                                    "detail": "tls: Error decoding",
                                    "timed_out": False, "can_transcode": True})
        text = self._text()
        self.assertTrue(any("Could not play" in t for t in text), text)
        self.assertTrue(any("tls: Error decoding" in t for t in text),
                        "the cause mpv logged never reached the user")
        self.assertTrue(any("Retry" == t for t in text), text)
        self.assertTrue(any("Transcode" in t for t in text), text)

    def test_a_timeout_is_labelled_as_one(self):
        self.browser.on_load_error({"title": "X", "timed_out": True,
                                    "can_transcode": True})
        self.assertTrue(any("Timed out" in t for t in self._text()))

    def test_no_transcode_retry_when_already_transcoding(self):
        """Re-requesting the same transcode would fail the same way."""
        self.browser.on_load_error({"title": "X", "timed_out": False,
                                    "can_transcode": False})
        self.assertFalse(any("Transcode" in t for t in self._text()))

    def test_retry_asks_the_player_and_shows_loading_again(self):
        self.browser.on_load_error({"title": "X", "timed_out": False,
                                    "can_transcode": True})
        self.browser._retry_playback(False)
        self.assertEqual(self.controller.retries, [False])
        self.assertIsNone(self.browser._load_error)
        self.assertIsNotNone(self.browser._starting,
                             "the error stayed up, so the button reads dead")

    def test_transcode_retry_passes_the_flag(self):
        self.browser.on_load_error({"title": "X", "timed_out": False,
                                    "can_transcode": True})
        self.browser._retry_playback(True)
        self.assertEqual(self.controller.retries, [True])

    def test_cancel_returns_to_browsing(self):
        self.browser.on_load_error({"title": "X", "timed_out": False,
                                    "can_transcode": True})
        self.browser._cancel_failed_playback()
        self.assertIsNone(self.browser._load_error)
        self.assertTrue(self.browser._browsing)

    def test_playback_starting_clears_the_loading_screen(self):
        self.browser.on_load_start({"title": "Some Movie"})
        self.browser.on_playstate({"stopped": False, "is_audio": False,
                                   "id": "m1", "title": "Some Movie",
                                   "position": 1, "duration": 100})
        self.assertIsNone(self.browser._starting)

    def test_the_stop_on_the_failure_path_does_not_bounce_to_the_library(self):
        """The regression that made a failed load look like an unexplained
        bounce back to the library: _play_media's failure path calls stop(),
        whose stopped playstate used to return the browser to browse and
        wipe the error before it was ever seen."""
        self.browser.on_load_error({"title": "X", "detail": "tls",
                                    "timed_out": True, "can_transcode": True})
        self.browser.on_playstate({"stopped": True})
        self.assertIsNotNone(self.browser._load_error,
                             "the error was erased by the failure's own stop")
        self.assertFalse(self.browser._browsing,
                         "the user was bounced back to the library")

    def test_a_video_failure_stays_a_knockout_even_after_returning_to_browse(self):
        """Ordering-independent: window ownership is latched when the load
        starts, not re-read when the error lands."""
        self.browser.on_load_start({"title": "Movie"})
        self.browser._browsing = True          # as stop() would leave it
        self.browser.on_load_error({"title": "Movie", "timed_out": True,
                                    "can_transcode": True})
        self.assertIsNotNone(self.browser._load_error,
                             "a video failure was downgraded to a toast")

    def test_cancel_stops_the_load_and_returns_to_browsing(self):
        self.browser.on_load_start({"title": "Movie"})
        self.browser._cancel_loading()
        self.assertTrue(self.controller.cancelled,
                        "the player was never told to abandon the load")
        self.assertIsNone(self.browser._starting)
        self.assertTrue(self.browser._browsing)

    def test_the_loading_screen_offers_a_cancel(self):
        self.browser.on_load_start({"title": "Movie"})
        self._slow()
        self.assertIn("Cancel", self._text())

    def test_a_stop_does_not_erase_the_error(self):
        """A failed load calls stop() on its way out. Clearing on any stopped
        playstate would wipe the error before its first frame."""
        self.browser.on_load_error({"title": "X", "timed_out": False,
                                    "can_transcode": True})
        self.browser.on_playstate({"stopped": True})
        self.assertIsNotNone(self.browser._load_error)


if __name__ == "__main__":
    unittest.main()
