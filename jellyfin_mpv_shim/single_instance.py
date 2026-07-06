"""Single-instance guard.

Primary election is an OS file lock (flock on POSIX, msvcrt.locking on
Windows) held on a persistent fd for the process lifetime — atomic, and
released automatically if the process crashes, so there is no stale-lock
takeover logic to race. The lock lives in the config directory, so two
instances pointed at different config dirs coexist by design.

Two files are used so Windows' mandatory region locks never interact with
content reads: ``instance.lock.guard`` is only ever locked (never read), and
``instance.lock`` carries the primary's activation endpoint — a loopback
port and a random token. A second launch finds the guard held, connects, and
(if the token matches) asks the primary to raise its window, then exits. The
token protects against the port having been recycled by an unrelated
process.

If the guard file can't even be opened we fail open (run without the guard);
if the lock is held but the primary doesn't answer, we still refuse to run a
second instance — a wedged listener must not lead to two catalog writers.
"""

import logging
import os
import secrets
import socket
import sys
import threading

from . import conffile
from .constants import APP_NAME

log = logging.getLogger("single_instance")

_MAGIC = b"JMS1"


class SingleInstance:
    def __init__(self):
        # Set by the owner once the UI exists; called when a second launch is
        # blocked, on a listener thread.
        self.on_activate = lambda: None
        self._sock = None
        self._guard_fd = None
        self._token = secrets.token_hex(16).encode("ascii")
        self._lockpath = conffile.get(APP_NAME, "instance.lock")
        self._guardpath = self._lockpath + ".guard"

    def acquire(self) -> bool:
        """Return True if we are the primary instance. Return False if another
        instance is already running (which we've asked to show its window)."""
        fd = self._try_lock()
        if fd is None:
            log.warning(
                "Could not open the instance guard file; continuing without "
                "the single-instance guard."
            )
            return True  # fail open
        if fd is False:
            if not self._handoff_to_existing():
                log.warning(
                    "%s is already running (instance lock is held) but did "
                    "not respond to the activation request.", APP_NAME
                )
            return False

        self._guard_fd = fd
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(5)
        except OSError:
            # Still the primary (the lock enforces that); we just can't be
            # asked to raise the window.
            log.warning(
                "Single-instance activation socket unavailable; running "
                "without it.", exc_info=True
            )
            return True
        self._sock = sock
        port = sock.getsockname()[1]
        self._write_endpoint(port)
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def release(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        # Closing the fd releases the OS lock. The guard file itself is left
        # in place: unlinking it would let a new primary lock a fresh inode
        # while a concurrently-started process still holds the old one — two
        # primaries. An empty leftover file is harmless; the lock, not the
        # file's existence, decides who is primary.
        if self._guard_fd is not None:
            try:
                os.close(self._guard_fd)
            except OSError:
                pass
            self._guard_fd = None
        try:
            os.remove(self._lockpath)
        except OSError:
            pass

    # -- internals ---------------------------------------------------------

    def _try_lock(self):
        """Try to take the election lock on the guard file. Returns the open
        fd on success, False if another live process holds it, None if the
        file can't be opened at all."""
        try:
            fd = os.open(self._guardpath, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            if sys.platform.startswith(("win32", "cygwin")):
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        return fd

    def _write_endpoint(self, port: int):
        try:
            fd = os.open(
                self._lockpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(fd, "w") as fh:
                fh.write("%d\n%s\n" % (port, self._token.decode("ascii")))
        except OSError:
            log.debug("Could not write instance lock endpoint", exc_info=True)

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
            return False  # primary has no listener (or it's wedged)
        if reply.startswith(_MAGIC):
            log.info("%s is already running; asked it to show its window.",
                     APP_NAME)
            return True
        return False

    def _serve(self):
        while True:
            try:
                sock = self._sock
                if sock is None:
                    break
                conn, _addr = sock.accept()
            except OSError:
                break  # socket closed on release()
            # One thread per connection with a short timeout: a client that
            # connects and sends nothing must not wedge the listener for
            # everyone else.
            threading.Thread(
                target=self._serve_one, args=(conn,), daemon=True
            ).start()

    def _serve_one(self, conn):
        try:
            conn.settimeout(5)
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
