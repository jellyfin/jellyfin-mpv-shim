"""Slice S6, `app_lifecycle`: first run, relaunch, quitting, and the
app's own configuration -- on the shipped app, by keys.

Critical-path inventory rows 48, 51, 52 and 55:

- row 48: a fresh config is first run: the login form, then Home; a
  relaunch goes straight to Home, with no form. Launch-to-Home is printed
  per run (a number to watch across releases, not a gate);
- row 51: a second launch on the same config exits, and brings the first
  one's library up;
- row 52: quit during a film, during a comic, with the server gone, and
  mid-download: inside a budget, nothing of the app left running, and the
  catalog passes `PRAGMA integrity_check`;
- row 55: a binding in the user's own input.conf does its job.
"""

import os
import sqlite3
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
from test_playback_lifecycle import LONG_NAME, _PlaybackCase  # noqa: E402

QUIT_BUDGET = 20.0


class _LifecycleCase(_PlaybackCase):

    def open_film(self):
        film = self.movie(LONG_NAME)
        self.fresh(film)
        _flows.open_by_search(self.app, LONG_NAME, film)
        return film

    def quit_cleanly(self, what):
        """Quit by the window's close, inside the budget, nothing left
        behind, and a catalog that is whole."""
        pgid = self.app.proc.pid
        start = time.monotonic()
        rc = self.app.quit(timeout=40)
        took = time.monotonic() - start
        self.assertEqual(0, rc, "%s: quit returned %r" % (what, rc))
        self.assertLess(took, QUIT_BUDGET, "%s: quit took %.1f s"
                        % (what, took))
        if os.name == "posix":
            time.sleep(1)
            with self.assertRaises(ProcessLookupError,
                                   msg="%s: something of the app's outlived "
                                       "it (an mpv?)" % what):
                os.killpg(pgid, 0)
        db = os.path.join(self.app.config_dir, "offline", "catalog.db")
        if os.path.exists(db):
            conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            try:
                self.assertEqual(
                    "ok", conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "%s: the catalog is damaged" % what)
            finally:
                conn.close()


class FirstRunAndRelaunchTest(_LifecycleCase):
    """Row 48. setUp's login IS first run: a fresh config dir, the form,
    typed by keys, then Home."""

    def test_a_relaunch_goes_straight_home(self):
        self.assertTrue(_app.shown(self.app.frame(), "row-libs"))
        self.assertEqual(0, self.app.quit(timeout=30))
        app = _app.App(backend=self.app.backend,
                       config_dir=self.app.config_dir)
        self.app = app
        start = time.monotonic()
        app.start()
        seen_login = []

        def home(f):
            if _app.shown(f, "login-server"):
                seen_login.append(f.get("rev"))
            return _app.shown(f, "row-libs")
        app.wait_for(home, timeout=90, what="Home after the relaunch")
        took = time.monotonic() - start
        print("\nlaunch to Home (relaunch, %s): %.2f s"
              % (app.backend, took), flush=True)
        self.assertEqual([], seen_login, "the relaunch showed the login form")
        self.assertEqual(0, app.quit(timeout=30))


class ASecondLaunchSurfacesTheFirstTest(_LifecycleCase):
    """Row 51: a second launch on the same config exits by itself and
    brings the first one's library up -- here, over a film."""

    def test_the_second_launch_hands_over(self):
        self.open_film()
        self.play()
        self.app.wait_for(lambda f: f.get("phud_mode"), timeout=30,
                          what="the film to take the window")
        second = subprocess.Popen(
            [sys.executable, _app.RUN_PY, "--config", self.app.config_dir],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, JMS_TEST_OBSERVE="1"))
        try:
            rc = second.wait(timeout=30)
        except subprocess.TimeoutExpired:
            second.kill()
            self.fail("the second launch did not exit")
        out = second.stdout.read().decode("utf-8", "replace")
        self.assertEqual(0, rc, out[-800:])
        # The handover, read where it is settled: the first instance's log
        # (the two share it) shows the ask, then the first entering browse.
        # Not a frame: over a playing film the library is up for a moment
        # only, and on jsonipc that moment can fall between observer reads.
        def handed_over():
            with open(self.app.log_path, encoding="utf-8",
                      errors="replace") as fh:
                lines = fh.read().splitlines()
            asked = [i for i, l in enumerate(lines)
                     if "asked it to show its window" in l]
            return asked and any("on_browse_enter" in l
                                 for l in lines[asked[-1]:])
        self.assertTrue(_e2e.wait_for(handed_over, timeout=15),
                        "the first instance never entered browse after the "
                        "second launch (it said: %s)"
                        % out.strip().splitlines()[-2:])
        # Whether it then STAYS up over the still-playing film is open: the
        # film's next playstate takes the window back on both backends
        # (register, 2026-09-27, for Izzie). Not asserted either way.
        self.quit_cleanly("after a second launch")


class QuitInEachStateTest(_LifecycleCase):
    """Row 52."""

    def test_during_a_film(self):
        self.open_film()
        self.play()
        self.assertTrue(_e2e.wait_for(lambda: self.time_pos() > 2,
                                      timeout=30))
        self.quit_cleanly("during a film")

    def test_during_a_comic(self):
        comic = [i for i in self.session.find_all(library="Books")
                 if i.get("Name") == "A Test Comic 001"][0]["Id"]
        self.fresh(comic)
        _flows.open_by_search(self.app, "A Test Comic 001", comic,
                              section="Books", landed="bk-read")
        self.app.move_to("bk-read")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "cm-page"), timeout=30,
                          what="the comic reader")
        self.quit_cleanly("during a comic")

    def test_with_the_server_gone(self):
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        time.sleep(3)
        self.quit_cleanly("with the server gone")

    def test_mid_download(self):
        film = self.open_film()
        self.relay.throttle(2_000_000)
        catalog = _flows.Catalog(self.app.config_dir)
        self.app.move_to("act-download")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "dl-ok"), timeout=15,
                          what="the download dialog")
        self.app.move_to("dl-ok")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(
            lambda: (catalog.download(film) or {}).get("status")
            in ("downloading", "pending", "queued"), timeout=30),
            "no download in flight (row %r)" % (catalog.download(film),))
        time.sleep(3)
        self.quit_cleanly("mid-download")


class AUserInputConfTest(_LifecycleCase):
    """Row 55: the person's own input.conf is honoured -- a key they bound
    does what they bound it to, over the app's own bindings."""

    FILES = {"input.conf": "F9 cycle mute\n"}

    def test_their_binding_works(self):
        with open(os.path.join(self.app.config_dir, "input.conf"),
                  encoding="utf-8") as fh:
            self.assertIn("F9 cycle mute", fh.read(),
                          "the seeded input.conf was not kept")
        self.open_film()
        self.play()
        self.assertTrue(_e2e.wait_for(lambda: self.time_pos() > 1,
                                      timeout=30))
        was = self.app.prop("mute")
        self.app.key("F9")
        self.assertTrue(_e2e.wait_for(
            lambda: self.app.prop("mute") is (not was), timeout=10),
            "F9 from the user's input.conf did nothing")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
