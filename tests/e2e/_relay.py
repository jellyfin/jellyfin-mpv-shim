"""A TCP relay the test owns, so a test can take the network away.

The app is pointed at the relay's address, never at the server's, and the
relay forwards to the server. Three modes:

* ``pass``  — forward both ways.
* ``cut``   — every open connection is closed, and a new one is accepted and
  closed at once. Fails fast on Linux **and** Windows, where a *closed* port
  does not: Windows retries a refused loopback SYN for about two seconds.
* ``stall`` — every open connection stops forwarding, and a new one is
  accepted and held without being connected upstream. This is an established
  read timeout, not a SYN drop; nothing here claims to be one.

Leaving ``stall`` resets every stalled connection rather than delivering what
it buffered: late bytes would arrive at a moment no test chose. A request
forwarded before the stall began cannot be taken back, and tests must not
assume it can.

Plain HTTP only, like the QA server, so the relay can read what passes:
``requests`` is every HTTP request line the app sent, in order, and
``redirects`` is every 3xx the server answered (a redirect could move the app
off the relay's address). The plan this implements is
~/Desktop/mpv-shim-offline-e2e-plan.md, "Architecture" item 5.
"""

import re
import socket
import threading
import time

_REQUEST_LINE = re.compile(
    rb"(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) (\S+) HTTP/1\.[01]\r\n")
_STATUS_LINE = re.compile(rb"^HTTP/1\.[01] (3\d\d) ")
_TOKEN = re.compile(rb'(?:Token="([^"]+)"|X-Emby-Token: *([^\r\n]+)|'
                    rb'[?&]api_key=([^&\s]+))', re.I)


class _Flow:
    def __init__(self, relay, client):
        self.relay = relay
        self.client = client
        self.upstream = None
        self.closed = False
        self.held = False

    def start(self):
        try:
            self.upstream = socket.create_connection(self.relay.upstream,
                                                     timeout=10)
            self.upstream.settimeout(None)
        except OSError:
            self.close()
            return
        for src, dst, outbound in ((self.client, self.upstream, True),
                                   (self.upstream, self.client, False)):
            threading.Thread(target=self._pump, args=(src, dst, outbound),
                             daemon=True, name="relay-pump").start()

    def _pump(self, src, dst, outbound):
        while not self.closed:
            try:
                data = src.recv(65536)
            except OSError:
                break
            if not data:
                break
            # Held here while stalled, and dropped: a stall that later
            # delivered what it buffered would release bytes at a moment
            # no test chose.
            while self.relay.mode == "stall" and not self.closed:
                time.sleep(0.02)
            if self.closed or self.relay.mode != "pass":
                break
            if outbound and self.relay._should_hold(data):
                # hold(): this request waits here, not forwarded, until
                # drop_held() closes it (or close()).
                self.held = True
                while self.held and not self.closed:
                    time.sleep(0.02)
                if self.closed:
                    break
            self.relay._observe(data, outbound)
            try:
                dst.sendall(data)
            except OSError:
                break
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        for s in (self.client, self.upstream):
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        self.relay._forget(self)


class Relay:
    """``Relay(("127.0.0.1", 8096))``; ``relay.address`` is what the app is
    given. ``close()`` when done (also a context manager)."""

    def __init__(self, upstream, host="127.0.0.1"):
        self.upstream = upstream
        self.mode = "pass"
        self.requests = []          # [(method, path)] in the order sent
        self.redirects = []         # [status] of every 3xx seen
        self.request_tokens = []    # [(path, token or None)] in order sent
        self._hold = None           # compiled pattern for hold()
        self._flows = set()
        self._held = []             # sockets accepted while stalled
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, 0))
        self._listener.listen(64)
        self.port = self._listener.getsockname()[1]
        self.address = "http://%s:%d" % (host, self.port)
        self._running = True
        threading.Thread(target=self._accept, daemon=True,
                         name="relay-accept").start()

    # -- modes ---------------------------------------------------------

    def cut(self):
        self.mode = "cut"
        self._close_all()

    def stall(self):
        self.mode = "stall"

    def hold(self, path_pattern):
        """Hold every request whose path matches ``path_pattern`` (a regex):
        it is not forwarded until drop_held(). Everything else passes."""
        self._hold = re.compile(path_pattern.encode()
                                if isinstance(path_pattern, str)
                                else path_pattern)

    def held_count(self):
        with self._lock:
            return sum(1 for f in self._flows if f.held)

    def drop_held(self):
        """Close every held request's connection and stop holding. A dropped
        request never reaches the server; the client sees the connection
        die, which is what a network failure mid-request looks like."""
        self._hold = None
        with self._lock:
            held = [f for f in self._flows if f.held]
        for flow in held:
            flow.close()

    def _should_hold(self, data):
        pattern = self._hold
        if pattern is None:
            return False
        m = _REQUEST_LINE.search(data)
        return bool(m and pattern.search(m.group(2)))

    def restore(self):
        """Back to ``pass``. Anything held or stalled is reset, not
        delivered; the app reconnects on its own."""
        # Close BEFORE leaving stall: a pump holding a buffer checks the mode,
        # and seeing "pass" first would deliver its bytes late.
        if self.mode == "stall":
            self._close_all()
        self.mode = "pass"

    # -- confirming a fault is really in effect ------------------------

    def probe_refused(self, timeout=3.0):
        """True when a new connection is closed on us: ``cut`` is live."""
        try:
            s = socket.create_connection(("127.0.0.1", self.port),
                                         timeout=timeout)
        except OSError:
            return True
        try:
            s.settimeout(timeout)
            s.sendall(b"GET /System/Info/Public HTTP/1.1\r\nHost: x\r\n\r\n")
            return s.recv(1) == b""
        except OSError:
            return True
        finally:
            s.close()

    def probe_silent(self, wait=1.0):
        """True when a new connection gets no HTTP answer in ``wait``
        seconds: ``stall`` is live. It may well connect -- that is the
        point of a stall."""
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        except OSError:
            return False
        try:
            s.settimeout(wait)
            s.sendall(b"GET /System/Info/Public HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                s.recv(1)       # any answer, even a close, is not silence
            except socket.timeout:
                return True
            except OSError:
                pass
            return False
        finally:
            s.close()

    # -- internals -----------------------------------------------------

    def _accept(self):
        while self._running:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if self.mode == "cut":
                client.close()
                continue
            if self.mode == "stall":
                with self._lock:
                    self._held.append(client)
                continue
            flow = _Flow(self, client)
            with self._lock:
                self._flows.add(flow)
            flow.start()

    def _observe(self, data, outbound):
        with self._lock:
            if outbound:
                for m in _REQUEST_LINE.finditer(data):
                    self.requests.append((m.group(1).decode(),
                                          m.group(2).decode("latin-1")))
                    tail = data[m.start():m.start() + 4096]
                    t = _TOKEN.search(tail)
                    token = next((g for g in t.groups() if g), None) if t \
                        else None
                    self.request_tokens.append(
                        (m.group(2).decode("latin-1"),
                         token.decode("latin-1").strip() if token else None))
            else:
                m = _STATUS_LINE.match(data)
                if m:
                    self.redirects.append(int(m.group(1)))

    def _forget(self, flow):
        with self._lock:
            self._flows.discard(flow)

    def _close_all(self):
        with self._lock:
            flows, self._flows = list(self._flows), set()
            held, self._held = self._held, []
        for flow in flows:
            flow.close()
        for s in held:
            try:
                s.close()
            except OSError:
                pass

    def open_flows(self):
        with self._lock:
            return len(self._flows)

    def close(self):
        self._running = False
        try:
            self._listener.close()
        except OSError:
            pass
        self._close_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
