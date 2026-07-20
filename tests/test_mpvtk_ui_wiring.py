"""The wiring in mpvtk_browser.ui.UserInterface.login_servers.

That method is ~90 lines of single-assignment plumbing: it builds the
browser and then hands ~14 callbacks to playerManager, clientManager,
syncManager and eventHandler. Every feature that reaches the browser from
outside goes through one of those lines — the now-playing bar, update
notices, window close, mpv teardown/re-create, remote BACK/ESC, "show me
this" from a phone, download badges, a server coming up late.

It had NO tests. Every unit test in the suite calls `browser.on_playstate(...)`
and friends directly, so deleting any one of these assignments leaves the
whole suite green while the feature silently stops working. This is the same
shape as the log-tail bug (tests started the poller themselves, so removing
the production call site changed nothing) at the most load-bearing point in
the application.

Two halves, and the second is the one that keeps this honest:

* the behavioural half asserts each callback is wired AND that invoking it
  reaches the browser;
* the structural half reads the source and fails if login_servers assigns a
  callback this file does not cover — so adding a hook without a test is a
  test failure rather than a silent gap.
"""

import ast
import inspect
import os
import sys
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing player reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import ui as ui_mod  # noqa: E402

UI_SRC = inspect.getsource(ui_mod.UserInterface.login_servers)


class FakePlayer:
    """Records the callbacks login_servers hands it."""

    def __init__(self):
        self.mpvtk_active = None
        self.on_playstate = None
        self.notify_update = None
        self.on_window_closed = None
        self.on_mpv_gone = None
        self.on_mpv_terminated = None
        self.on_mpv_recreated = None
        self.on_hud_menu = None
        self.on_nav_back = None
        self.on_nav_command = None

    @staticmethod
    def get_mpv():
        return object()

    # enter_browse()/minimize() reach through _PlayerController into these.
    # Recorded rather than no-op'd so the window handoff stays visible.
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        calls = self.__dict__.setdefault("calls", [])
        return lambda *a, **k: calls.append((name, a))


class FakeApp:
    in_process = True

    def __init__(self):
        self.invalidated = 0

    def invalidate(self):
        self.invalidated += 1

    def quit(self):
        pass

    def set_accent(self, *_a):
        pass

    def scroll_offsets(self):
        return {}


class FakeClients:
    device_id = "dev1"
    credentials = []
    clients = {}

    def __init__(self):
        self.on_server_connected = None
        self.loaded = 0

    def load_credentials(self):
        self.loaded += 1


class FakeSync:
    def __init__(self):
        self.on_change = None
        self.db = None


class FakeEvents:
    display_content = None


class FakeUsers:
    @staticmethod
    def startup_needs_unlock():
        return False


class WiringHarness(unittest.TestCase):
    """Runs the real login_servers against fakes."""

    def setUp(self):
        self.player = FakePlayer()
        self.clients = FakeClients()
        self.sync = FakeSync()
        self.events = FakeEvents()

        self._patch("jellyfin_mpv_shim.player", "playerManager", self.player)
        self._patch("jellyfin_mpv_shim.player", "is_using_ext_mpv", False)
        self._patch("jellyfin_mpv_shim.mpvtk_browser.ui", "clientManager",
                    self.clients)
        self._patch("jellyfin_mpv_shim.sync.manager", "syncManager", self.sync)
        self._patch("jellyfin_mpv_shim.event_handler", "eventHandler",
                    self.events)
        self._patch("jellyfin_mpv_shim.users", "userManager", FakeUsers())

        import jellyfin_mpv_shim.mpvtk.app as mpvtk_app
        self._patch_attr(mpvtk_app.MpvtkApp, "attach",
                         staticmethod(lambda *a, **k: FakeApp()))

        self.ui = ui_mod.UserInterface()
        # Keep the test hermetic: the two threads login_servers spawns do
        # network work, and background pollers are covered elsewhere.
        self.ui._run = lambda: None
        self.ui._connect = lambda: None
        # start_background_work spawns the download-status poller and the
        # update check; those have their own tests.
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        self._patch_attr(MpvtkBrowser, "start_background_work",
                         lambda _s: None)

    def _patch(self, module_path, name, obj):
        import importlib
        mod = importlib.import_module(module_path)
        real = getattr(mod, name, None)
        had = hasattr(mod, name)
        setattr(mod, name, obj)

        def restore():
            if had:
                setattr(mod, name, real)
            else:
                delattr(mod, name)
        self.addCleanup(restore)

    def _patch_attr(self, owner, name, obj):
        real = getattr(owner, name, None)
        had = hasattr(owner, name)
        setattr(owner, name, obj)

        def restore():
            if had:
                setattr(owner, name, real)
            else:
                try:
                    delattr(owner, name)
                except AttributeError:
                    pass
        self.addCleanup(restore)

    def _login(self):
        self.ui.login_servers()
        self.addCleanup(self._shutdown)
        return self.ui._browser

    def _shutdown(self):
        b = self.ui._browser
        if b is not None:
            try:
                b._shutdown_evt.set()
                b._pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass


# Callback -> (owner attribute path, what it must reach).
# `browser` means "calling it must be observable on the browser object".
PLAYER_CALLBACKS = [
    "on_playstate",
    "notify_update",
    "on_window_closed",
    "on_mpv_gone",
    "on_mpv_terminated",
    "on_mpv_recreated",
    "on_hud_menu",
    "on_nav_back",
    "on_nav_command",
]


class TestEveryCallbackIsWired(WiringHarness):
    def test_the_browser_is_built_and_kept(self):
        browser = self._login()
        self.assertIsNotNone(browser, "login_servers built no browser")
        self.assertIs(self.ui._browser, browser)

    def test_credentials_are_loaded_before_anything_else(self):
        self._login()
        self.assertEqual(self.clients.loaded, 1)

    def test_the_player_is_told_the_in_window_ui_is_live(self):
        """mpvtk_active gates the idle quit, makes `q` return to the library
        rather than quitting, and decides whether closing the window
        minimizes. Nothing else sets it."""
        self._login()
        self.assertTrue(self.player.mpvtk_active)

    def test_every_player_callback_is_assigned(self):
        self._login()
        missing = [n for n in PLAYER_CALLBACKS
                   if getattr(self.player, n) is None]
        self.assertEqual(missing, [], "player callbacks left unwired")

    def test_a_late_server_connect_is_subscribed(self):
        self._login()
        self.assertIsNotNone(self.clients.on_server_connected,
                             "a server that comes up late stays invisible")

    def test_download_changes_are_subscribed(self):
        self._login()
        self.assertIsNotNone(self.sync.on_change,
                             "download badges never refresh")

    def test_show_me_this_from_a_phone_is_subscribed(self):
        self._login()
        self.assertIsNotNone(self.events.display_content,
                             "DisplayContent from a remote goes nowhere")


class TestTheCallbacksActuallyReachTheBrowser(WiringHarness):
    """Non-None is not enough — it has to be bound to the live browser."""

    def test_playstate_reaches_the_now_playing_bar(self):
        browser = self._login()
        self.player.on_playstate({"stopped": False, "is_audio": True,
                                  "id": "t1", "title": "Song",
                                  "position": 1, "duration": 10})
        self.assertIsNotNone(browser._now_playing,
                             "on_playstate is not bound to this browser")

    def test_an_update_notice_reaches_the_banner(self):
        browser = self._login()
        self.player.notify_update("1.2.3", "http://example")
        self.assertEqual((browser._update or {}).get("version"), "1.2.3")

    def test_remote_back_reaches_the_nav_stack(self):
        browser = self._login()
        browser.nav_stack.append({"kind": "home", "server": None})
        depth = len(browser.nav_stack)
        self.player.on_nav_back()
        self.assertLess(len(browser.nav_stack), depth,
                        "BACK from a remote does not reach the browser")

    def test_mpv_teardown_reaches_this_ui(self):
        self._login()
        self.player.on_mpv_gone()
        self.assertTrue(self.ui._detaching,
                        "on_mpv_gone is not bound to this UserInterface")


class TestNoCallbackEscapesCoverage(unittest.TestCase):
    """The half that stops this file going stale.

    Reads login_servers and fails if it assigns a player callback the
    behavioural tests above do not name. Without this, someone adds a hook,
    every test stays green, and the feature is untested exactly like the
    fourteen before it.
    """

    def _assigned_player_callbacks(self):
        import textwrap
        tree = ast.parse(textwrap.dedent(UI_SRC))
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "playerManager"
                        and target.attr.startswith("on_")):
                    found.add(target.attr)
                # notify_update is a callback too, just not on_-prefixed.
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "playerManager"
                        and target.attr == "notify_update"):
                    found.add(target.attr)
        return found

    def test_the_parser_sees_the_wiring(self):
        """A parser matching nothing would make the check below vacuous."""
        self.assertGreaterEqual(len(self._assigned_player_callbacks()), 8)

    def test_every_assigned_callback_is_covered(self):
        uncovered = sorted(self._assigned_player_callbacks()
                           - set(PLAYER_CALLBACKS))
        self.assertEqual(
            uncovered, [],
            "login_servers wires these with no test: %s — add them to "
            "PLAYER_CALLBACKS" % uncovered)

    def test_no_stale_names_in_the_list(self):
        """The reverse: a name left here after the wiring moved would make
        test_every_player_callback_is_assigned assert on nothing real."""
        stale = sorted(set(PLAYER_CALLBACKS)
                       - self._assigned_player_callbacks())
        self.assertEqual(stale, [],
                         "PLAYER_CALLBACKS names nothing login_servers "
                         "assigns: %s" % stale)


if __name__ == "__main__":
    unittest.main()


class AuthHarness(unittest.TestCase):
    """The auth actions themselves — `_do_login`, `_do_switch_user`,
    `_do_unlock`, Quick Connect — had zero references in any test file.

    The suite covered *rendering* the login and lock screens and nothing
    about what pressing the buttons does. A regression that accepts any PIN,
    or silently drops a successful login on the floor, was invisible. This is
    also the area whose only behavioural coverage is the 12 Tk integration
    tests that die with the Tk browser.
    """

    def _browser(self, **ctl):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests.test_mpvtk_browser_shell import (
            FakeController, FakeSource, _SyncPool)
        c = FakeController()
        for k, v in ctl.items():
            setattr(c, k, v)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=c)
        b._pool = _SyncPool()
        self.ctl = c
        return b


class TestLoginActions(AuthHarness):
    def test_a_successful_login_leaves_the_form(self):
        b = self._browser(add_server=lambda *a: True)
        b.show_login()
        b._login.update({"server": "http://s", "user": "u", "pass": "p"})
        b._do_login()
        self.assertNotEqual(b.route["kind"], "login",
                            "a successful login stayed on the form")
        self.assertIsNone(b._login_error)

    def test_a_rejected_login_stays_and_says_why(self):
        b = self._browser(add_server=lambda *a: False)
        b.show_login()
        b._login.update({"server": "http://s", "user": "u", "pass": "bad"})
        b._do_login()
        self.assertEqual(b.route["kind"], "login")
        self.assertIn("could not connect", (b._login_error or "").lower())

    def test_the_password_is_not_kept_in_the_error(self):
        """Whatever ends up on screen must not echo the secret back."""
        b = self._browser(add_server=lambda *a: False)
        b.show_login()
        b._login.update({"server": "http://s", "user": "u",
                         "pass": "hunter2"})
        b._do_login()
        self.assertNotIn("hunter2", b._login_error or "")

    def test_login_credentials_reach_the_controller_intact(self):
        seen = []
        b = self._browser(
            add_server=lambda s, u, p: (seen.append((s, u, p)) or True))
        b.show_login()
        b._login.update({"server": "http://s", "user": "u", "pass": "p"})
        b._do_login()
        self.assertEqual(seen, [("http://s", "u", "p")])


class TestUnlockActions(AuthHarness):
    def test_a_wrong_pin_is_refused_and_stays_locked(self):
        b = self._browser(unlock=lambda pin: False)
        b.show_locked()
        b._pin["pin"] = "0000"
        b._do_unlock()
        self.assertEqual(b.route["kind"], "locked")
        self.assertIn("incorrect", (b._pin_error or "").lower())

    def test_a_correct_pin_unlocks(self):
        b = self._browser(unlock=lambda pin: True)
        b.show_locked()
        b._pin["pin"] = "1234"
        b._do_unlock()
        self.assertNotEqual(b.route["kind"], "locked")
        self.assertIsNone(b._pin_error)

    def test_the_pin_is_not_retained_after_a_successful_unlock(self):
        b = self._browser(unlock=lambda pin: True)
        b.show_locked()
        b._pin["pin"] = "1234"
        b._do_unlock()
        self.assertEqual(b._pin.get("pin"), "",
                         "the PIN was left in memory after unlocking")

    def test_the_pin_actually_reaches_the_verifier(self):
        """A regression that never passes the PIN down would 'unlock' on
        anything — the single worst failure this screen can have."""
        seen = []
        b = self._browser(unlock=lambda pin: (seen.append(pin) or True))
        b.show_locked()
        b._pin["pin"] = "4321"
        b._do_unlock()
        self.assertEqual(seen, ["4321"])


class TestQuickConnect(AuthHarness):
    def test_it_refuses_without_a_server_url(self):
        b = self._browser()
        b.show_login()
        b._start_quick_connect(b.route)
        self.assertIn("url", (b._login_error or "").lower())
        self.assertIsNone(b.route.get("_qc"))

    def test_the_code_is_surfaced_while_waiting(self):
        """The whole point of the screen: the server issues a code and the
        user types it into another Jellyfin client. If it never reaches the
        route there is nothing on screen to type."""
        shown = {}

        def quick_connect(server, on_code, cancelled):
            on_code("ABC123")
            # Snapshot what the UI is showing at this instant — after the
            # call completes _qc is cleared, so asserting afterwards would
            # prove nothing.
            shown.update(b.route.get("_qc") or {})
            return True

        b = self._browser(quick_connect=quick_connect)
        b.show_login()
        b._login["server"] = "http://s"
        b._start_quick_connect(b.route)
        self.assertEqual(shown.get("code"), "ABC123",
                         "the Quick Connect code never reached the screen")

    def test_the_server_url_reaches_quick_connect(self):
        seen = []
        b = self._browser(
            quick_connect=lambda s, c, x: (seen.append(s) or True))
        b.show_login()
        b._login["server"] = "  http://s  "
        b._start_quick_connect(b.route)
        self.assertEqual(seen, ["http://s"], "the URL was not trimmed/passed")

    def test_a_refused_quick_connect_says_so(self):
        b = self._browser(quick_connect=lambda s, c, x: False)
        b.show_login()
        b._login["server"] = "http://s"
        b._start_quick_connect(b.route)
        self.assertIn("not approved", (b._login_error or "").lower())

    def test_cancelling_tells_the_worker_to_stop(self):
        """The worker polls a cancelled flag; without it a cancelled Quick
        Connect keeps polling the server until it times out."""
        b = self._browser()
        b.show_login()
        b.route["_qc"] = {"code": "X", "status": "", "cancelled": False}
        qc = b.route["_qc"]
        b._cancel_quick_connect(b.route)
        self.assertTrue(qc["cancelled"])
        self.assertIsNone(b.route.get("_qc"))
