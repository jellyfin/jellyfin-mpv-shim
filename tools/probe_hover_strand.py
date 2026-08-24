#!/usr/bin/env python3
"""Reproduce issue #700's stranded ``mouse-pos.hover`` against a real mpv.

Two attempts at #700 shipped to the reporter without anyone here having seen
the bug, because the window managers on hand (openbox, kwin) ungrab the
pointer before they resize and Cinnamon/muffin does not. This drives the grab
directly instead of hoping for a WM that does it, so the state is reachable on
any machine with an X server.

**What it shows.** mpv sets ``hover`` only from MOUSE_ENTER / MOUSE_LEAVE, and
``video/out/x11_common.c`` drops every crossing whose mode is not
NotifyNormal (mpv 30860f7b1). Grab the pointer, bring the window under it,
ungrab: the EnterNotify that would restore hover arrives as NotifyUngrab and
is discarded, so ``hover`` stays false for the rest of the session with the
pointer sitting in the middle of the window -- while the coordinates go on
updating correctly. Then it checks the repair ``renderer.lua`` issues,
``keypress MOUSE_ENTER``, and that it holds once the pointer stops moving.

Not part of any suite: it needs Xvfb, openbox, xdotool and python-xlib, and
none of those is a test dependency. See docs/testing.md section 10.

Usage: python3 tools/probe_hover_strand.py [--display :77]
Exit status is 0 only if the flag was stranded AND the repair took.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

#: Where the window is parked before the pointer is let near it, and the
#: geometry it is given afterwards -- big enough that the pointer, which does
#: not move during the grab, ends up well inside it.
START_GEOMETRY = "400x300+900+600"
MOVED_TO = (40, 40)
GROWN_TO = (1200, 800)


class Ipc:
    """The three JSON-IPC calls this needs, without adding a dependency."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.buf = b""

    def cmd(self, *args):
        self.sock.sendall(json.dumps({"command": list(args)}).encode() + b"\n")
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line)
                # Events have no `error` key; replies always do.
                if "error" in msg:
                    return msg
            self.buf += self.sock.recv(65536)

    def mouse_pos(self):
        return self.cmd("get_property", "mouse-pos").get("data")


class Session:
    """Xvfb + openbox + mpv, torn down by PID and never by pattern."""

    def __init__(self, display, sock):
        self.display = display
        self.sock = sock
        self.procs = []

    def _env(self):
        return dict(os.environ, DISPLAY=self.display)

    def spawn(self, *argv):
        proc = subprocess.Popen(argv, env=self._env(),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def x(self, *argv):
        return subprocess.run(argv, env=self._env(),
                              capture_output=True, text=True)

    def start(self):
        try:
            os.unlink(self.sock)
        except OSError:
            pass
        self.spawn("Xvfb", self.display, "-screen", "0", "1600x1000x24")
        time.sleep(1.5)
        # A WM is needed only so the window is managed and reparented the way
        # a real one is; openbox does NOT itself reproduce the bug.
        self.spawn("openbox")
        time.sleep(1.0)
        self.spawn("mpv", "--idle=yes", "--force-window=yes", "--vo=x11",
                   "--no-config", "--osc=no", "--no-input-default-bindings",
                   "--geometry=" + START_GEOMETRY,
                   "--input-ipc-server=" + self.sock)
        for _ in range(80):
            if os.path.exists(self.sock):
                break
            time.sleep(0.25)
        else:
            raise SystemExit("mpv never created its IPC socket")
        time.sleep(1.5)
        return Ipc(self.sock)

    def stop(self):
        for proc in reversed(self.procs):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def strand(session, window_id):
    """Grab, move the window under the motionless pointer, ungrab."""
    from Xlib import display as xdisplay, X

    disp = xdisplay.Display(session.display)
    root = disp.screen().root
    root.grab_pointer(False,
                      X.PointerMotionMask | X.ButtonPressMask |
                      X.ButtonReleaseMask,
                      X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE,
                      X.CurrentTime)
    disp.sync()
    session.x("xdotool", "windowmove", str(window_id), *map(str, MOVED_TO))
    session.x("xdotool", "windowsize", str(window_id), *map(str, GROWN_TO))
    time.sleep(0.8)
    disp.ungrab_pointer(X.CurrentTime)
    disp.sync()
    time.sleep(0.6)


def run(display):
    sock = "/tmp/mpv-hover-strand-%d.sock" % os.getpid()
    session = Session(display, sock)
    try:
        ipc = session.start()
        window_id = int(ipc.cmd("get_property", "window-id")["data"])
        print("window-id  :", hex(window_id))

        # Park the pointer clear of the window, so the crossing back in is
        # the one the grab is about to eat.
        session.x("xdotool", "mousemove", "100", "100")
        time.sleep(0.6)
        print("parked out :", ipc.mouse_pos())

        strand(session, window_id)

        # Move WITHIN the window. The coordinates track; the flag does not.
        for x, y in ((300, 300), (340, 320), (380, 340)):
            session.x("xdotool", "mousemove", str(x), str(y))
            time.sleep(0.35)
        stranded = ipc.mouse_pos()
        print("stranded   :", stranded)
        if not stranded or stranded.get("hover") is not False:
            print("\nRESULT: the flag was not stranded -- nothing to repair.")
            return 2
        if stranded.get("x", -1) <= 0:
            print("\nRESULT: no coordinates arrived; not the #700 state.")
            return 2

        reply = ipc.cmd("keypress", "MOUSE_ENTER")
        print("keypress   :", reply)
        time.sleep(0.4)
        repaired = ipc.mouse_pos()
        print("after fix  :", repaired)

        session.x("xdotool", "mousemove", "420", "360")
        time.sleep(0.4)
        moved = ipc.mouse_pos()
        # Well past the renderer's 0.2s leave grace: a repair that only held
        # while the pointer moved would be the bug all over again.
        time.sleep(1.5)
        parked = ipc.mouse_pos()
        print("moved on   :", moved)
        print("parked in  :", parked)

        # mouse-pos is WINDOW-relative, and the window is at MOVED_TO --
        # comparing it against the screen coordinates xdotool was handed is
        # how this probe first reported a failure it had not found.
        want = (420 - MOVED_TO[0], 360 - MOVED_TO[1])
        good = (repaired.get("hover") is True
                and moved.get("hover") is True
                and parked.get("hover") is True
                and (parked.get("x"), parked.get("y")) == want
                and parked == moved)
        print("\nRESULT:", "REPAIRED and held" if good else "FAILED")
        return 0 if good else 1
    finally:
        session.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", default=":77",
                        help="X display for the throwaway server")
    args = parser.parse_args()

    missing = [t for t in ("Xvfb", "openbox", "xdotool", "mpv")
               if not shutil.which(t)]
    try:
        import Xlib  # noqa: F401
    except ImportError:
        missing.append("python-xlib")
    if missing:
        print("missing: " + ", ".join(missing), file=sys.stderr)
        return 2
    return run(args.display)


if __name__ == "__main__":
    sys.exit(main())
