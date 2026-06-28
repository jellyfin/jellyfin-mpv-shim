"""Single-instance guard.

Keeps a loopback socket in the primary process and records its port + a random
token in a per-user lock file. A second launch reads the lock file, connects,
and (if the token matches a live primary) asks it to raise its window, then
exits. The token guards against a stale lock file whose port has been recycled
by an unrelated process. If anything goes wrong we fail open and run normally.
"""

import logging
import os
import secrets
import socket
import threading

from . import conffile
from .constants import APP_NAME

log = logging.getLogger("single_instance")

_MAGIC = b"JMS1"


class SingleInstance:
    def __init__(self):
        # Set by the owner once the UI exists; called when a second launch is
        # blocked, on the listener thread.
        self.on_activate = lambda: None
        self._sock = None
        self._token = secrets.token_hex(16).encode("ascii")
        self._lockpath = conffile.get(APP_NAME, "instance.lock")

    def acquire(self) -> bool:
        """Return True if we are the primary instance. Return False if another
        instance is already running (which we've asked to show its window)."""
        if self._handoff_to_existing():
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(5)
        except OSError:
            log.warning("Single-instance socket unavailable; continuing without "
                        "the guard.", exc_info=True)
            return True  # fail open
        self._sock = sock
        port = sock.getsockname()[1]
        try:
            with open(self._lockpath, "w") as fh:
                fh.write("%d\n%s\n" % (port, self._token.decode("ascii")))
        except OSError:
            log.debug("Could not write instance lock file", exc_info=True)
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def release(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            os.remove(self._lockpath)
        except OSError:
            pass

    # -- internals ---------------------------------------------------------

    def _read_lock(self):
        try:
            with open(self._lockpath) as fh:
                port = int(fh.readline().strip())
                token = fh.readline().strip().encode("ascii")
            return port, token
        except (OSError, ValueError):
            return None

    def _handoff_to_existing(self) -> bool:
        info = self._read_lock()
        if not info:
            return False
        port, token = info
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                conn.sendall(_MAGIC + token + b"\n")
                reply = conn.recv(8)
        except OSError:
            return False  # stale lock or nobody home — we'll take over
        if reply.startswith(_MAGIC):
            log.info("%s is already running; asked it to show its window.",
                     APP_NAME)
            return True
        return False

    def _serve(self):
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # socket closed on release()
            try:
                data = conn.recv(64)
                if data.startswith(_MAGIC) and \
                        data[len(_MAGIC):].strip() == self._token:
                    conn.sendall(_MAGIC + b"OK")
                    try:
                        self.on_activate()
                    except Exception:
                        log.error("Single-instance activate handler failed",
                                  exc_info=True)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
