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

    FRESH_LOGIN = True

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
    """Row 51: a second launch on the same config exits by itself; over a
    film, the first only raises its window and the film keeps it."""

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
        # Over a film, a second launch only raises the window: the film
        # keeps it, and the library never comes up (Izzie, 2026-09-27).
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            f = self.app.frame()
            self.assertTrue(f.get("phud_mode") and not _app.shown(
                f, "nav-settings"), "the second launch took the window "
                                    "from the film (rev %s)" % f.get("rev"))
            time.sleep(0.1)
        with open(self.app.log_path, encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.read().splitlines()
        asked = max(i for i, l in enumerate(lines)
                    if "asked it to show its window" in l)
        self.assertFalse(any("on_browse_enter" in l for l in lines[asked:]),
                         "the first instance entered browse over the film")
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


class ThePasswordDoesNotOutliveTheSignInTest(_LifecycleCase):
    """B8 from the Add Server door: after a sign-in, the form it reuses
    opens with no password. (What a person can see. The post-sign-in clear
    itself has no path to the screen -- every entry resets the form first,
    and a mutant dropping it passes here -- so it is pinned in
    tests/test_login_form_reset.py.)"""

    # The form this reads is the one the sign-in in THIS process used; a
    # launch already signed in never typed a password, so it would pass.
    FRESH_LOGIN = True

    def test_add_server_opens_with_no_password(self):
        _flows.open_settings_tab(self.app, "servers")
        self.app.move_to("sv-add")
        f = self.app.press_until("ENTER", lambda f: _app.shown(f, "login-pass"),
                                 what="the add-server form")
        typed = _app.fields(f).get("login-pass")
        shown = (_app.node(f, "login-pass") or {}).get("text")
        self.assertFalse(typed or shown,
                         "the form still holds the last password (%d chars)"
                         % len(typed or shown or ""))
        self.assertEqual(0, self.app.quit(timeout=30))


class AnotherLanguageTest(_LifecycleCase):
    """Izzie's gap from the v3.0.0 hand pass: language selection was never
    tested. Chosen in Settings by keys, it asks for a restart (on purpose:
    22 strings are translated at import, so a live switch would leave the
    UI half in the old language), is saved, and after the relaunch the UI
    is in that language. Needs compiled catalogs (gen_pkg.sh --skip-build)."""

    LANGUAGE = "Deutsch"
    HOME_LABEL = "Startseite"          # "Home" in de/base.po

    def test_it_asks_for_a_restart_and_comes_back_in_that_language(self):
        import json
        mo = os.path.join(os.path.dirname(_app.RUN_PY), "jellyfin_mpv_shim",
                          "messages", "de", "LC_MESSAGES", "base.mo")
        if not os.path.exists(mo):
            self.fail("no compiled catalogs: run ./gen_pkg.sh --skip-build")
        _flows.open_settings_tab(self.app, "general")
        f = self.app.wait_for(lambda f: _flows.items(f, "set-lang"),
                              timeout=15, what="the Language drop-down")
        entries = _flows.items(f, "set-lang")
        index = next((i for i, e in enumerate(entries)
                      if self.LANGUAGE in e), None)
        self.assertIsNotNone(index, "no %s in %r" % (self.LANGUAGE,
                                                     entries[:8]))
        _flows.pick(self.app, "set-lang", index)
        self.app.wait_for(lambda f: _app.shown(f, "banner-restart"),
                          timeout=15, what="the restart banner")
        with open(os.path.join(self.app.config_dir, "conf.json"),
                  encoding="utf-8") as fh:
            self.assertEqual("de", json.load(fh).get("lang"))
        self.app = _flows.relaunch(self.app)
        f = self.app.wait_for(lambda f: _app.shown(f, "row-libs"),
                              timeout=60, what="Home after the relaunch")
        self.assertIn(self.HOME_LABEL, _app.texts(f),
                      "the relaunched UI is not in %s" % self.LANGUAGE)
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
