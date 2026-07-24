"""Concurrency tests for ClientManager's per-server connect/disconnect registry.

ClientManager is one of the lock-heavy singletons: ``_client_lock`` guards the
``clients`` registry and the ``_connecting`` reservation set, the lock is
deliberately *not* held across the (slow) network authenticate, and ``stop()``
must win against an in-flight connect. These tests force the exact interleavings
with barriers and a blocking fake ``authenticate`` rather than sleeping and
hoping.

No mpv, no server, no real network: ``client_factory`` and ``setup_client`` are
replaced with fakes, so the tests exercise pure registry/lock logic.
"""

import os
import sys
import threading
import unittest
from unittest import mock

from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

from jellyfin_mpv_shim import clients as clients_module  # noqa: E402
from jellyfin_mpv_shim.clients import ClientManager  # noqa: E402


class FakeJellyfin:
    def __init__(self, sessions):
        self._sessions = sessions

    def _http(self, method, path, opts):
        return self._sessions

    def post_capabilities(self, caps):
        pass


class FakeConfig:
    def __init__(self):
        self.data = {}


class FakeClient:
    """Minimal JellyfinClient stand-in. ``authenticate`` can block on an event
    so a test can pin a connect mid-flight and race stop()/reconnect against
    it."""

    _counter = 0

    def __init__(self, sessions=None, auth_state="SignedIn", on_authenticate=None):
        FakeClient._counter += 1
        self.id = FakeClient._counter
        self.jellyfin = FakeJellyfin(sessions if sessions is not None else [])
        self.config = FakeConfig()
        self._auth_state = auth_state
        self._on_authenticate = on_authenticate
        self.started = False
        self.stopped = False
        self.callback = None
        self.callback_ws = None

    def authenticate(self, creds, discover=False):
        if self._on_authenticate is not None:
            self._on_authenticate(self)
        return {"State": CONNECTION_STATE[self._auth_state]}

    def start(self, websocket=True):
        self.started = True

    def stop(self):
        self.stopped = True


DEVICE_ID = "test-device-uuid"


def make_manager(factory, *, sessions=None):
    """Build a ClientManager with the health-check thread disabled and the
    network seams replaced. ``factory`` returns the next FakeClient."""
    with mock.patch.object(clients_module.settings, "health_check_interval", None):
        cm = ClientManager()
    cm.client_factory = factory
    # setup_client normally starts the websocket and spawns the cast verifier
    # thread; keep the registry logic under test but skip the I/O + threads.
    cm.setup_client = lambda client, server, do_retries=True: setattr(
        client, "started", True)
    return cm


def server(uuid="s1"):
    return {"uuid": uuid, "Id": uuid, "address": "http://x", "username": "u"}


class ConnectRegistryTest(unittest.TestCase):
    def setUp(self):
        # settings.client_uuid gates validate_client's device match.
        self._p = mock.patch.object(clients_module.settings, "client_uuid",
                                    DEVICE_ID)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_concurrent_connect_same_server_builds_one_client(self):
        # RACE: many connectors (health check, ws reconnect, cast verifier) can
        # call connect_client for the same server at once. The _connecting
        # reservation must let exactly one build a client; the rest see it
        # in-flight (False) or already registered (True). No duplicate, no leak.
        made = []
        gate = h.spin_barrier(8)

        def factory():
            c = FakeClient()
            made.append(c)
            return c

        cm = make_manager(factory)
        srv = server()

        def connect():
            gate.wait()
            return cm.connect_client(srv)

        results = h.run_concurrently(connect, 8)

        self.assertEqual(len(cm.clients), 1)
        # Only one client instance should have been authenticated + registered.
        registered = cm.clients[srv["uuid"]]
        self.assertTrue(registered.started)
        # Any other client objects that were built must not linger unstopped;
        # in practice the reservation means only one is ever built.
        self.assertEqual(len(made), 1, "duplicate client built under the lock")
        self.assertTrue(any(results), "no connector reported success")
        self.assertEqual(cm._connecting, set(), "_connecting reservation leaked")

    def test_stop_during_inflight_connect_leaves_no_client(self):
        # RACE: stop() flags shutdown and drains the registry while a connect is
        # mid-authenticate. The connect must not resurrect a client stop() can
        # no longer see — it should stop the fresh client and register nothing.
        entered = threading.Event()
        release = threading.Event()

        def blocking_auth(_client):
            entered.set()
            release.wait(5)

        cm = make_manager(lambda: FakeClient(on_authenticate=blocking_auth))
        srv = server()

        result = {}
        t = threading.Thread(
            target=lambda: result.__setitem__("ok", cm.connect_client(srv)))
        t.start()
        self.assertTrue(entered.wait(5), "connect never reached authenticate")

        # stop() runs while the connect is parked inside authenticate.
        cm.stop()
        release.set()
        t.join(5)

        self.assertFalse(t.is_alive())
        self.assertFalse(result.get("ok"), "connect registered despite stop()")
        self.assertEqual(cm.clients, {}, "a client survived stop()")
        self.assertEqual(cm._connecting, set())

    def test_already_connected_connect_is_noop_returns_true(self):
        cm = make_manager(lambda: FakeClient())
        srv = server()
        self.assertTrue(cm.connect_client(srv))
        first = cm.clients[srv["uuid"]]
        # A second connect for an already-registered server must not rebuild.
        self.assertTrue(cm.connect_client(srv))
        self.assertIs(cm.clients[srv["uuid"]], first)


class DisconnectIdentityRaceTest(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(clients_module.settings, "client_uuid",
                                    DEVICE_ID)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_validate_probe_does_not_tear_down_a_reconnected_replacement(self):
        # AUDIT RACE (validate_client vs reconnect identity): a health check
        # finds the device missing from the server session list and moves to
        # disconnect the client — but a reconnect may have already swapped in a
        # healthy replacement. The expected_client identity check must spare the
        # replacement and only stop the stale handle.
        cm = make_manager(lambda: FakeClient())
        srv = server()

        stale = FakeClient(sessions=[])       # not in the session list -> "dead"
        replacement = FakeClient(sessions=[{"DeviceId": DEVICE_ID}])
        cm.clients[srv["uuid"]] = stale

        swapped = threading.Event()

        # Simulate the reconnect landing precisely between validate_client's
        # "not connected" decision and its _disconnect_client call by swapping
        # the registered client the first time _disconnect_client runs.
        orig_disconnect = cm._disconnect_client

        def racing_disconnect(*args, **kwargs):
            if not swapped.is_set():
                cm.clients[srv["uuid"]] = replacement
                swapped.set()
            return orig_disconnect(*args, **kwargs)

        cm._disconnect_client = racing_disconnect

        # stale is not in its (empty) session list -> validate_client tries to
        # disconnect it, but the replacement is now registered.
        result = cm.validate_client(stale, server=srv)

        self.assertFalse(result)
        self.assertIs(cm.clients[srv["uuid"]], replacement,
                      "reconnected replacement was torn down")
        self.assertTrue(stale.stopped, "stale handle not stopped")
        self.assertFalse(replacement.stopped, "replacement wrongly stopped")

    def test_disconnect_with_expected_client_mismatch_is_noop(self):
        cm = make_manager(lambda: FakeClient())
        srv = server()
        current = FakeClient()
        cm.clients[srv["uuid"]] = current
        other = FakeClient()
        # Asking to remove `other` must not touch `current`.
        removed = cm._disconnect_client(server=srv, expected_client=other)
        self.assertFalse(removed)
        self.assertIs(cm.clients[srv["uuid"]], current)
        self.assertFalse(current.stopped)


class _DynamicJellyfin:
    """Jellyfin stub whose session list follows a shared mutable flag, so a
    single client's health check can be flipped from failing to passing between
    ticks (models a server dropping off the LAN and coming back)."""

    def __init__(self, sessions_fn):
        self._sessions_fn = sessions_fn

    def _http(self, method, path, opts):
        return self._sessions_fn()

    def post_capabilities(self, caps):
        pass


class _DynamicClient:
    """FakeClient whose auth result and session visibility both track a shared
    ``up`` flag: signed-in and present in the session list while up, unavailable
    and absent while down."""

    _counter = 0

    def __init__(self, up, sessions_fn):
        _DynamicClient._counter += 1
        self.id = _DynamicClient._counter
        self._up = up
        self.jellyfin = _DynamicJellyfin(sessions_fn)
        self.config = FakeConfig()
        self.started = False
        self.stopped = False
        self.callback = None
        self.callback_ws = None

    def authenticate(self, creds, discover=False):
        state = "SignedIn" if self._up["val"] else "Unavailable"
        return {"State": CONNECTION_STATE[state]}

    def start(self, websocket=True):
        self.started = True

    def stop(self):
        self.stopped = True


class HealthCheckReconnectTest(unittest.TestCase):
    """Issue #295 / #344: a failed health check must reconnect *in the same
    process* — no app restart. Guards the dead-code-reconnect fix (077a42d),
    where validate_client's force-reconnect path called client.callback two
    lines after nulling it, so a dropped server lost remote control until the
    user restarted the shim."""

    def setUp(self):
        self._p = mock.patch.object(clients_module.settings, "client_uuid",
                                    DEVICE_ID)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_failed_health_check_reconnects_without_restart(self):
        up = {"val": True}

        def sessions():
            return [{"DeviceId": DEVICE_ID}] if up["val"] else []

        built = []

        def factory():
            c = _DynamicClient(up, sessions)
            built.append(c)
            return c

        cm = make_manager(factory)
        srv = server()
        cm.credentials = [srv]

        with mock.patch.object(clients_module.settings, "work_offline", False):
            # Initial connect while the server is reachable.
            self.assertTrue(cm.connect_client(srv))
            first = cm.clients[srv["uuid"]]
            self.assertTrue(cm.validate_client(first, server=srv))

            # Server drops off: the device no longer shows in its session list
            # and re-auth fails. One health-check tick must retire the stale
            # client (drop + stop) and, finding the server unreachable, leave it
            # disconnected — no zombie, no duplicate.
            up["val"] = False
            cm.check_all_clients()
            self.assertNotIn(srv["uuid"], cm.clients,
                             "stale client not dropped on failed health check")
            self.assertTrue(first.stopped, "stale client handle never stopped")

            # Server returns: the very next tick's credential-retry pass
            # reconnects it — WITHOUT any restart. This is the flagship
            # regression: pre-fix, this reconnect was dead code.
            up["val"] = True
            cm.check_all_clients()

        self.assertIn(srv["uuid"], cm.clients,
                      "server never reconnected after coming back (needs restart?)")
        reconnected = cm.clients[srv["uuid"]]
        self.assertIsNot(reconnected, first,
                         "reconnect reused the dead client instead of rebuilding")
        self.assertTrue(cm.validate_client(reconnected, server=srv),
                        "reconnected client does not pass validation")
        self.assertEqual(cm._connecting, set(), "_connecting reservation leaked")

    def test_background_reconnect_notifies_ui(self):
        # Regression: a server offline at startup reconnected fine in the
        # registry, but the browser was never told — it kept browsing the
        # other server until an app restart. A successful health-check
        # reconnect must fire on_server_connected (full servers push); ticks
        # that reconnect nothing must stay quiet.
        up = {"val": False}

        def factory():
            return _DynamicClient(up, lambda: [{"DeviceId": DEVICE_ID}])

        cm = make_manager(factory)
        srv = server()
        cm.credentials = [srv]
        notified = []
        cm.on_server_connected = lambda: notified.append(True)

        with mock.patch.object(clients_module.settings, "work_offline", False):
            cm.check_all_clients()   # server still down
            self.assertEqual(notified, [],
                             "notified the UI without a reconnect")
            up["val"] = True
            cm.check_all_clients()   # server back: reconnect + notify
        self.assertIn(srv["uuid"], cm.clients)
        self.assertEqual(notified, [True],
                         "UI was not told about the background reconnect")


class ConnectDisconnectStressTest(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(clients_module.settings, "client_uuid",
                                    DEVICE_ID)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_interleaved_connect_and_disconnect_keep_registry_consistent(self):
        # RACE: connect and disconnect for the same server hammered together.
        # Invariant: the registry never holds more than one client for the
        # server, _connecting never leaks, and every client object ends either
        # registered or stopped (never orphaned running).
        built = []

        def factory():
            c = FakeClient(sessions=[{"DeviceId": DEVICE_ID}])
            built.append(c)
            return c

        cm = make_manager(factory)
        srv = server()

        def connector():
            for _ in range(20):
                cm.connect_client(srv)

        def disconnector():
            for _ in range(20):
                cm._disconnect_client(server=srv)

        threads = ([threading.Thread(target=connector) for _ in range(3)] +
                   [threading.Thread(target=disconnector) for _ in range(3)])
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertFalse(any(t.is_alive() for t in threads))

        # At most one registered client; reservation drained.
        self.assertLessEqual(len(cm.clients), 1)
        self.assertEqual(cm._connecting, set())
        # No client is both unregistered and left running.
        registered = set(id(c) for c in cm.clients.values())
        for c in built:
            if id(c) not in registered:
                self.assertTrue(c.stopped,
                                "an unregistered client was left running")

    def test_concurrent_health_checks_reconnect_once(self):
        # RACE: the health-check tick reconnects credentials that aren't
        # currently connected. Several ticks overlapping (or a tick overlapping
        # a manual connect) must not build duplicate clients for one server —
        # the _connecting reservation serialises the rebuild.
        built = []

        def factory():
            c = FakeClient(sessions=[{"DeviceId": DEVICE_ID}])
            built.append(c)
            return c

        cm = make_manager(factory)
        srv = server()
        cm.credentials = [srv]  # a saved-but-disconnected server

        with mock.patch.object(clients_module.settings, "work_offline", False):
            h.run_concurrently(cm.check_all_clients, 8)

        self.assertEqual(len(cm.clients), 1)
        self.assertEqual(len(built), 1, "health checks built duplicate clients")
        self.assertEqual(cm._connecting, set())


class RemovedServerTombstoneTest(unittest.TestCase):
    """A health-check tick that captured the credentials list before the user
    removed a server must not re-register the deleted server afterwards (a
    zombie session that outlives its credential and is never validated)."""

    def setUp(self):
        self._p = mock.patch.object(clients_module.settings, "client_uuid",
                                    DEVICE_ID)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_connect_after_remove_is_refused(self):
        built = []

        def factory():
            c = FakeClient(sessions=[{"DeviceId": DEVICE_ID}])
            built.append(c)
            return c

        cm = make_manager(factory)
        cm.save_credentials = lambda: None  # keep the real cred.json untouched
        srv = server()
        cm.credentials = [srv]
        cm.remove_client(srv["uuid"])

        # The stale iteration (old list snapshot) reaches the removed entry.
        self.assertFalse(cm.connect_client(srv))
        self.assertNotIn(srv["uuid"], cm.clients,
                         "removed server was resurrected by a stale reconnect")
        # The client built mid-connect must have been stopped, not leaked.
        self.assertTrue(all(c.stopped for c in built))


class StaleDisconnectEventTest(unittest.TestCase):
    """A stale WSClient thread (its client already replaced in the registry)
    fires one final WebSocketDisconnect on exit; the reconnect handler must
    ignore it instead of tearing down the healthy replacement."""

    def setUp(self):
        self._p = mock.patch.object(clients_module.settings, "client_uuid",
                                    DEVICE_ID)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_stale_disconnect_leaves_replacement_alone(self):
        cm = make_manager(lambda: FakeClient())
        srv = server()
        stale = FakeClient()
        with mock.patch.object(clients_module.settings, "work_offline", False):
            cm.setup_client = ClientManager.setup_client.__get__(cm)
            cm.setup_client(stale, srv, do_retries=False)

        replacement = FakeClient()
        cm.clients[srv["uuid"]] = replacement

        # The stale thread's final event: identity check must reject it.
        stale.callback("WebSocketDisconnect", None)

        self.assertIs(cm.clients.get(srv["uuid"]), replacement,
                      "stale disconnect tore down the healthy replacement")
        self.assertFalse(replacement.stopped)

    def test_intentional_disconnect_silences_final_event(self):
        cm = make_manager(lambda: FakeClient())
        srv = server()
        victim = FakeClient()
        with mock.patch.object(clients_module.settings, "work_offline", False):
            cm.setup_client = ClientManager.setup_client.__get__(cm)
            cm.setup_client(victim, srv, do_retries=False)
        cm.clients[srv["uuid"]] = victim

        self.assertTrue(cm._disconnect_client(server=srv))
        # The WSClient thread will still fire its final callback on exit; it
        # must be a no-op now, not the reconnect handler.
        victim.callback("WebSocketDisconnect", None)
        self.assertNotIn(srv["uuid"], cm.clients)


class WebSocketErrorBackoffTest(unittest.TestCase):
    """Regression: the apiclient's WSClient redials in a tight loop with no
    delay while the server is unreachable — the shim spammed a down server
    with tens of thousands of connection attempts. The WebSocketError handler
    (which runs on that redial thread) must block with growing backoff, reset
    once traffic flows again, and stay interruptible for shutdown."""

    class RecordingEvent:
        """Stands in for cm._stop_event: records wait() durations, never set."""

        def __init__(self):
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            return False

        def is_set(self):
            return False

    def _wired_event_handler(self):
        cm = make_manager(lambda: FakeClient())
        recorder = self.RecordingEvent()
        cm._stop_event = recorder
        client = FakeClient()
        # The real setup_client wires client.callback to its internal event fn.
        with mock.patch.object(clients_module.settings, "work_offline", False):
            cm.setup_client = ClientManager.setup_client.__get__(cm)
            cm.setup_client(client, server(), do_retries=False)
        return cm, client, recorder

    def test_repeated_errors_back_off_exponentially(self):
        cm, client, recorder = self._wired_event_handler()
        for _ in range(4):
            client.callback("WebSocketError", "connection refused")
        self.assertEqual(recorder.waits, [1, 2, 4, 8])

    def test_backoff_resets_on_reconnect(self):
        cm, client, recorder = self._wired_event_handler()
        client.callback("WebSocketError", "refused")
        client.callback("WebSocketError", "refused")
        client.callback("WebSocketConnect", None)  # actually reconnected
        client.callback("WebSocketError", "refused")
        self.assertEqual(recorder.waits, [1, 2, 1])

    def test_http_layer_failure_events_do_not_reset_backoff(self):
        # The HTTP layer routes "ServerUnreachable" (and other events) through
        # the same client.callback from OTHER threads while the server is
        # still down; resetting on those would restart the backoff at 1s for
        # the whole outage (and race the generator cell cross-thread). Only a
        # real reconnect (WebSocketConnect, same WS thread) may reset.
        cm, client, recorder = self._wired_event_handler()
        client.callback("WebSocketError", "refused")
        client.callback("WebSocketError", "refused")
        client.callback("ServerUnreachable", {"ServerId": "x"})
        client.callback("WebSocketError", "refused")
        self.assertEqual(recorder.waits, [1, 2, 4],
                         "an HTTP-layer failure event reset the ws backoff")

    def test_backoff_is_capped(self):
        cm, client, recorder = self._wired_event_handler()
        for _ in range(10):
            client.callback("WebSocketError", "refused")
        self.assertEqual(max(recorder.waits), 60)


if __name__ == "__main__":
    unittest.main()
