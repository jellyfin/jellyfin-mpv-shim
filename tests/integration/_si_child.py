"""Child process used by test_single_instance_multiproc.

Parameters come via env (SI_HOLD seconds, SI_WEDGE=1, SI_ACTIVATE_LOG path);
config dir via XDG_CONFIG_HOME. argv is deliberately kept clean: the app parses
sys.argv the first time it resolves the config dir, so any extra token there
would blow up the arg parser.

Attempts the single-instance election, prints exactly one line (``PRIMARY`` or
``SECONDARY``) and flushes it, then — if primary and asked to hold — keeps the
lock for SI_HOLD seconds so the parent can race other launches against a live
primary. SI_WEDGE closes the activation socket right after winning, modelling a
primary whose listener died (the election lock, not the handoff, must still
block duplicates).

SI_ACTIVATE_LOG names a file the primary appends to from its ``on_activate``
and ``on_stop`` handlers — one line per delivery. That is the half of this
protocol the election tests never exercised: a blocked launch is supposed to
*surface the running copy's window*, and a primary that elects correctly but
never runs the handler looks identical from the outside (#718).
"""

import os
import socket
import sys
import time

# Keep argv clean so the app's argparse (invoked lazily by conffile) is happy.
sys.argv = [sys.argv[0]]

# Ensure the package is importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from jellyfin_mpv_shim.single_instance import SingleInstance  # noqa: E402


def main():
    hold = float(os.environ.get("SI_HOLD", "0") or "0")
    wedge = os.environ.get("SI_WEDGE") == "1"

    si = SingleInstance()

    log_path = os.environ.get("SI_ACTIVATE_LOG")
    if log_path:
        def record(what):
            # Opened per delivery and O_APPEND: the listener serves each
            # connection on its own thread, so two near-simultaneous
            # activations would interleave on a shared handle. A short
            # append under O_APPEND is atomic enough for one line.
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(what + "\n")
                fh.flush()

        # Installed BEFORE acquire(): the listener thread starts inside it,
        # so a handler assigned afterwards can miss a fast activation.
        si.on_activate = lambda: record("SHOW")
        si.on_stop = lambda: record("STOP")

    ok = si.acquire()
    if ok and wedge and si._sock is not None:
        # **shutdown BEFORE close, or the listener does not die.** A plain
        # close() leaves the thread already blocked in accept() able to
        # complete the connection anyway (measured: the client connects and
        # is served), so SI_WEDGE modelled nothing and the test named after
        # it passed on the election alone. shutdown() breaks the accept and
        # the port then refuses, which is the state being modelled: guard-fd
        # lock still held, activation socket gone.
        try:
            si._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            si._sock.close()
        except OSError:
            pass
    sys.stdout.write("PRIMARY\n" if ok else "SECONDARY\n")
    sys.stdout.flush()
    if ok and hold:
        time.sleep(hold)
    si.release()


if __name__ == "__main__":
    main()
