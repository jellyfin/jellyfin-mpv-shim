"""Stopping an apiclient client never raises out of ClientManager.

The apiclient's `WSClient` assigns `self.keepalive = KeepAlive(...)` and only
then calls `.start()` (ws_client.py, both 1.18 and 1.19). A `stop_client()`
landing between those two lines joins a thread that never started, and
`Thread.join` raises `RuntimeError("cannot join thread before it is
started")`. Seen once in a Windows e2e run of test_login_lifecycle, out of
`remove_client`.

The stand-in raises through the apiclient's REAL `KeepAlive`, unstarted, so
the exception is the one the race produces rather than a guess at it.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import inspect
import sys
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_apiclient_python.keepalive import KeepAlive  # noqa: E402

import jellyfin_mpv_shim.clients as clients  # noqa: E402


class _RacingClient:
    """A client stopped inside the keepalive window: its websocket holds a
    KeepAlive that was assigned and never started.

    When the join raises, the websocket thread's next line -- `.start()` --
    is modelled as having run, which is what a retry will find. `torn_down`
    is the rest of the apiclient's `stop()`: closing the websocket, the HTTP
    session and the ping thread, all of which a raise skips."""

    def __init__(self):
        self.keepalive = KeepAlive(10, ws=None)
        self.stop_calls = 0
        self.torn_down = False
        self.callback = None
        self.callback_ws = None

    def stop(self):
        self.stop_calls += 1
        try:
            self.keepalive.stop()
        except RuntimeError:
            self.keepalive.start()   # halt is already set, so it exits at once
            raise
        self.torn_down = True


class _Client:
    def __init__(self):
        self.stop_calls = 0
        self.callback = None
        self.callback_ws = None

    def stop(self):
        self.stop_calls += 1


def _manager(**clients_by_uuid):
    mgr = clients.ClientManager.__new__(clients.ClientManager)
    mgr._client_lock = threading.RLock()
    mgr._switch_lock = threading.RLock()
    mgr._removed_uuids = set()
    mgr._server_on_lan = {}
    mgr._lan_probe = {}
    mgr._connect_failures = {}
    mgr._connecting = set()
    mgr.usernames = {}
    mgr.credentials = [{"uuid": u} for u in clients_by_uuid]
    mgr.save_credentials = lambda: None
    mgr.clients = dict(clients_by_uuid)
    return mgr


class TheStandInIsTheRealRaceTest(unittest.TestCase):
    def test_an_unstarted_keepalive_raises_on_stop(self):
        """If the apiclient ever guards this itself, the tests below still
        pass but stop proving anything; this says so."""
        with self.assertRaises(RuntimeError):
            KeepAlive(10, ws=None).stop()


class RemovingAServerTest(unittest.TestCase):
    def test_removal_survives_a_client_caught_mid_start(self):
        racing = _RacingClient()
        mgr = _manager(u1=racing)

        mgr.remove_client("u1")

        self.assertTrue(racing.torn_down,
                        "the stop raised and nothing finished the teardown: "
                        "websocket, session and ping thread left running")
        self.assertNotIn("u1", mgr.clients)
        self.assertEqual([], mgr.credentials)


class StoppingEveryClientTest(unittest.TestCase):
    def test_one_client_that_raises_does_not_strand_the_rest(self):
        """A profile switch and shutdown both go through here. Unguarded, the
        first raise left every later client running, websocket and all."""
        racing, a, b = _RacingClient(), _Client(), _Client()
        mgr = _manager(u0=racing, u1=a, u2=b)

        mgr.stop_all_clients()

        self.assertTrue(racing.torn_down)
        self.assertEqual([1, 1], [c.stop_calls for c in (a, b)])
        self.assertEqual({}, mgr.clients)


class NoBareStopTest(unittest.TestCase):
    """The race is in the apiclient, so every site that stops one of its
    clients is exposed, not just the one the e2e run happened to hit. Five
    sites did; one was guarded."""

    def test_clients_py_stops_clients_only_through_the_helper(self):
        tree = ast.parse(inspect.getsource(clients))
        bare = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name == "_stop_client":
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "stop"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "client"):
                    bare.append("%s:%d" % (fn.name, node.lineno))
        self.assertTrue(
            hasattr(clients.ClientManager, "_stop_client"),
            "the helper this audit exempts does not exist")
        self.assertEqual([], bare,
                         "client.stop() outside _stop_client: %s" % bare)


if __name__ == "__main__":
    unittest.main()
