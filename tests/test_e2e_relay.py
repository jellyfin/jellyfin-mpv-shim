"""The e2e relay's fault contract (tests/e2e/_relay.py), against a local
HTTP server, so it is proven before any scenario leans on it.

What each mode must do to connections that already exist is the part that
matters: a cut that left the websocket up, or a stall that delivered its
buffer late on restore, would make "offline" a claim rather than a state.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import http.client
import importlib.util
import http.server
import os
import socket
import sys
import threading
import time
import unittest

# By file path, not by putting tests/e2e on sys.path: that directory's
# test_*.py names collide with this one's, and discovery then imports the
# wrong module under the right name.
_spec = importlib.util.spec_from_file_location(
    "_e2e_relay", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "e2e", "_relay.py"))
_relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_relay)
Relay = _relay.Relay


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://elsewhere.invalid/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ServerName": "fake"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.relay = Relay(("127.0.0.1", self.server.server_address[1]))
        self.addCleanup(self.relay.close)

    def _conn(self, timeout=2.0):
        conn = http.client.HTTPConnection("127.0.0.1", self.relay.port,
                                          timeout=timeout)
        self.addCleanup(conn.close)
        return conn

    def _get(self, conn, path="/System/Info/Public"):
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_pass_forwards_and_logs_the_request(self):
        status, body = self._get(self._conn())
        self.assertEqual((200, b'{"ServerName": "fake"}'), (status, body))
        self.assertIn(("GET", "/System/Info/Public"), self.relay.requests)

    def test_cut_closes_an_open_connection_and_refuses_new_ones_fast(self):
        conn = self._conn()
        self.assertEqual(200, self._get(conn)[0])   # keep-alive, now open
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        start = time.monotonic()
        with self.assertRaises((OSError, http.client.HTTPException)):
            self._get(conn)
        with self.assertRaises((OSError, http.client.HTTPException)):
            self._get(self._conn())
        self.assertLess(time.monotonic() - start, 1.5,
                        "a cut must fail fast, not time out")

    def test_stall_silences_new_and_established_connections(self):
        conn = self._conn(timeout=1.0)
        self.assertEqual(200, self._get(conn)[0])
        self.relay.stall()
        self.assertTrue(self.relay.probe_silent())
        with self.assertRaises(socket.timeout):
            self._get(conn)

    def test_restore_resets_a_stalled_connection_rather_than_delivering(self):
        raw = socket.create_connection(("127.0.0.1", self.relay.port),
                                       timeout=2)
        self.addCleanup(raw.close)
        self.relay.stall()
        time.sleep(0.1)     # let the accept thread take it
        raw.sendall(b"GET /System/Info/Public HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.2)
        self.relay.restore()
        try:
            got = raw.recv(64)
        except OSError:
            got = b""
        self.assertEqual(b"", got,
                         "a stalled connection delivered after restore")
        self.assertEqual(200, self._get(self._conn())[0],
                         "a fresh connection does not work after restore")

    def test_several_stall_cycles_leave_it_working(self):
        """Loop of three, per docs/testing.md: a relay that leaked a flow or
        a mode per cycle would pass one round and fail the third."""
        for _ in range(3):
            self.relay.stall()
            self.assertTrue(self.relay.probe_silent(wait=0.5))
            self.relay.restore()
            self.assertEqual(200, self._get(self._conn())[0])
        self.assertLessEqual(self.relay.open_flows(), 1)

    def test_a_held_request_waits_and_a_dropped_one_never_arrives(self):
        self.relay.hold(r"^/slow")
        conn = self._conn(timeout=0.5)
        with self.assertRaises(socket.timeout):
            self._get(conn, "/slow")
        self.assertEqual(1, self.relay.held_count())
        # Everything else still passes while one request is held.
        self.assertEqual(200, self._get(self._conn())[0])
        self.relay.drop_held()
        self.assertEqual(0, self.relay.held_count())
        self.assertNotIn(("GET", "/slow"), self.relay.requests,
                         "a held request was forwarded")

    def test_the_token_each_request_carried_is_recorded(self):
        conn = self._conn()
        conn.request("GET", "/System/Info/Public", headers={
            "Authorization": 'MediaBrowser Client="x", Token="tok-123"'})
        conn.getresponse().read()
        self.assertIn(("/System/Info/Public", "tok-123"),
                      self.relay.request_tokens)

    def test_a_redirect_is_recorded(self):
        self.assertEqual(302, self._get(self._conn(), "/redirect")[0])
        self.assertEqual([302], self.relay.redirects)


if __name__ == "__main__":
    unittest.main()
