"""Child process used by test_single_instance_multiproc.

Parameters come via env (SI_HOLD seconds, SI_WEDGE=1); config dir via
XDG_CONFIG_HOME. argv is deliberately kept clean: the app parses sys.argv the
first time it resolves the config dir, so any extra token there would blow up
the arg parser.

Attempts the single-instance election, prints exactly one line (``PRIMARY`` or
``SECONDARY``) and flushes it, then — if primary and asked to hold — keeps the
lock for SI_HOLD seconds so the parent can race other launches against a live
primary. SI_WEDGE closes the activation socket right after winning, modelling a
primary whose listener died (the election lock, not the handoff, must still
block duplicates).
"""

import os
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
    ok = si.acquire()
    if ok and wedge and si._sock is not None:
        try:
            si._sock.close()   # listener dies; guard-fd lock is still held
        except OSError:
            pass
    sys.stdout.write("PRIMARY\n" if ok else "SECONDARY\n")
    sys.stdout.flush()
    if ok and hold:
        time.sleep(hold)
    si.release()


if __name__ == "__main__":
    main()
