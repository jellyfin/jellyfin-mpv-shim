"""Servers on the local network, offered on the add-server form (#755).

Somebody adding their first server has to type a URL they may not know. The
server broadcasts an answer to anyone who asks on the LAN, and the apiclient
gained `discovery.discover_servers` for it.

Three things here are requirements rather than decoration:

* **the block is optional at the module level.** `discover_servers` arrived in
  apiclient 1.19.0 and the shim's floor is older, so the import is guarded and
  the block simply is not drawn on an older one. That is the project's
  optional-dependency rule applied one step wider than usual (a module, not a
  package), and it is the half a developer with the new apiclient installed
  would never otherwise exercise;
* **the address is shown and nothing is selected.** A discovery reply is not
  authenticated: anything on the network can answer with any name and any
  address, so the name is whatever the answering machine chose to call itself
  and the address is the only part a user can judge;
* **the scan does not run on the loop thread.** It blocks for its whole
  timeout, because nothing says when the last server has answered.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from tests._shell_harness import (FakeController, FakeSource,    # noqa: E402
                                  _DeferredPool, _SyncPool,
                                  build_scene, ids)

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402

FOUND = [{"Id": "abc", "Name": "Living Room",
          "Address": "http://10.0.0.5:8096"}]


class _Discovering(FakeController):
    def __init__(self, found=None, boom=False):
        super().__init__()
        self.found = FOUND if found is None else found
        self.boom = boom
        self.scans = 0

    def discover_servers(self, timeout=1.0):
        self.scans += 1
        if self.boom:
            raise RuntimeError("no network")
        return list(self.found)


def _login(ctl, **kw):
    b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
    b._pool = _SyncPool()
    b.show_login(**kw)
    return b


class TheScanDoesNotBlockTheLoopTest(unittest.TestCase):
    """This file's third stated requirement, which had prose and no test.

    Every other case here installs `_SyncPool`, which runs submitted work
    inline -- so a `discover_servers()` called straight from the loop thread
    and one handed to the pool are indistinguishable, and the whole file would
    stay green with the login form frozen for the scan's entire timeout.

    `_DeferredPool` is what tells them apart: it holds submitted work until
    `drain()`, so the scan has not run yet at the point the screen first
    draws -- unless it never went to the pool at all.
    """

    def _deferred_login(self):
        ctl = _Discovering()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        pool = _DeferredPool()
        b._pool = pool
        b.show_login()
        return b, ctl, pool

    def test_the_scan_has_not_run_when_the_screen_first_draws(self):
        b, ctl, pool = self._deferred_login()

        nodes, _h = build_scene(b, size=(1600, 900))

        self.assertEqual(
            0, ctl.scans,
            "discover_servers ran before the first draw, so it ran on the "
            "loop thread -- the login form blocks for its whole timeout")
        self.assertTrue(
            pool.queued, "no work reached the pool at all")
        self.assertNotIn("login-found-row-0", ids(nodes))

    def test_and_the_servers_arrive_once_it_does(self):
        """The other half: deferred is not the same as dropped."""
        b, ctl, pool = self._deferred_login()
        build_scene(b, size=(1600, 900))

        pool.drain()
        nodes, _h = build_scene(b, size=(1600, 900))

        self.assertEqual(1, ctl.scans)
        self.assertIn("login-found-row-0", ids(nodes))


class TheDiscoveredBlockTest(unittest.TestCase):
    def test_a_server_that_answered_is_offered(self):
        b = _login(_Discovering())

        nodes, _h = build_scene(b, size=(1600, 900))

        self.assertIn("login-found-row-0", ids(nodes))

    def test_the_address_is_on_screen_and_not_only_the_name(self):
        """The reply is unauthenticated, so the address is the evidence."""
        b = _login(_Discovering())

        nodes, _h = build_scene(b, size=(1600, 900))
        texts = [n.get("text") for n in nodes if n.get("t") == "text"]

        self.assertIn("http://10.0.0.5:8096", texts)
        self.assertIn("Living Room", texts)

    def test_nothing_is_selected_for_the_user(self):
        """Discovery fills no field. An unauthenticated reply that typed
        itself into the form would be one press from a login attempt against
        whatever answered."""
        b = _login(_Discovering())

        build_scene(b, size=(1600, 900))

        self.assertEqual("", b._login["server"])

    def test_pressing_use_fills_the_address_in(self):
        b = _login(_Discovering())
        _n, handlers = build_scene(b, size=(1600, 900))

        handlers["login-found-0"]["click"]()

        self.assertEqual("http://10.0.0.5:8096", b._login["server"])

    def test_nothing_answering_draws_no_block(self):
        b = _login(_Discovering(found=[]))

        self.assertNotIn("login-found-row-0",
                         ids(build_scene(b, size=(1600, 900))[0]))

    def test_a_reply_with_no_address_is_not_a_row(self):
        """The apiclient drops those, and this does not depend on it: a row
        whose Use button fills in nothing is worse than no row."""
        b = _login(_Discovering(found=[{"Id": "x", "Name": "Nameless"}]))

        self.assertNotIn("login-found-row-0",
                         ids(build_scene(b, size=(1600, 900))[0]))

    def test_a_failed_scan_is_not_an_error_on_screen(self):
        b = _login(_Discovering(boom=True))

        self.assertNotIn("login-found-row-0",
                         ids(build_scene(b, size=(1600, 900))[0]))
        self.assertIsNone(b._login_error)

    def test_re_authenticating_does_not_offer_other_servers(self):
        """Every row here fills in a DIFFERENT server's address, which is the
        one edit that turns the re-auth form back into "add a server" without
        saying so. Same reason the saved-server list is hidden there."""
        ctl = _Discovering()

        b = _login(ctl, reauth={"uuid": "srv2", "name": "Away",
                                "address": "http://a", "username": "guest"})

        self.assertEqual(0, ctl.scans, "a re-auth scanned the network")
        self.assertNotIn("login-found-row-0",
                         ids(build_scene(b, size=(1600, 900))[0]))

    def test_a_controller_without_discovery_draws_no_block(self):
        """An older apiclient, from the shell's point of view."""
        b = _login(FakeController())

        self.assertNotIn("login-found-row-0",
                         ids(build_scene(b, size=(1600, 900))[0]))


class TheGatewayDegradesTest(unittest.TestCase):
    """The half that is about the apiclient rather than the screen."""

    def _gateway(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway.servers import (
            ServersMixin)

        return ServersMixin.__new__(ServersMixin)

    def test_an_apiclient_without_the_module_answers_empty(self):
        """`discovery` landed in 1.19.0 and the shim's floor is older, so
        this is the state a supported install can be in -- and the one a
        developer with the new apiclient never reaches by accident."""
        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def refuse(name, *args, **kw):
            if name == "jellyfin_apiclient_python.discovery":
                raise ImportError("no discovery here")
            return real_import(name, *args, **kw)

        with mock.patch("builtins.__import__", refuse):
            self.assertEqual([], self._gateway().discover_servers())

    def _with_discovery(self, fn):
        """Stand the module in through `sys.modules`, because the apiclient
        installed here does not have it.

        That is not a gap in the fixture, it is the shipped state: the module
        landed in 1.19.0 and the floor is older, so on this box the guarded
        import above is the LIVE path and these two tests are the only way to
        reach the other one.
        """
        import types

        module = types.ModuleType("jellyfin_apiclient_python.discovery")
        module.discover_servers = fn
        return mock.patch.dict(
            sys.modules, {"jellyfin_apiclient_python.discovery": module})

    def test_a_broadcast_that_raises_answers_empty(self):
        def boom(timeout=1.0):
            raise OSError("network unreachable")

        with self._with_discovery(boom):
            self.assertEqual([], self._gateway().discover_servers())

    def test_the_replies_are_passed_through(self):
        with self._with_discovery(lambda timeout=1.0: list(FOUND)):
            self.assertEqual(FOUND, self._gateway().discover_servers())

    def test_the_timeout_is_handed_on(self):
        """It is the whole cost of the call -- the broadcast blocks for all of
        it -- so a caller that wants a shorter wait has to be able to ask."""
        seen = []

        def record(timeout=1.0):
            seen.append(timeout)
            return []

        with self._with_discovery(record):
            self._gateway().discover_servers(timeout=0.25)

        self.assertEqual([0.25], seen)


if __name__ == "__main__":
    unittest.main()
