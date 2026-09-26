"""The shipped app, driven only through its keyboard, from login to a film
played to the end.

This is the harness proving itself (step 1 of the release-gate plan) on a
path that already works, before any scenario leans on it to find a bug. It
is also the one test in the tree where nothing between the keypress and the
server is the test's: `run.py` as a subprocess, the real gateway, client
manager, timeline reporter and profile store, a fresh config directory, and
the network going through a relay the test owns.

What it asserts is what a person would check by hand: the fields took what
was typed, the library appeared, search found the film, the detail page
offered Play, and after playing it to the end the SERVER says it was
watched -- reported by the app's own timeline, which no other e2e test runs
(they replace it and send the report themselves; see the e2e audit).

`JMS_TEST_BACKEND` picks the backend, as for every other e2e leg, but here
it is applied the way a user would choose it: through `conf.json`
(`mpv_ext`), not by swapping a module.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
import _relay  # noqa: E402

#: 12 seconds, and referenced by no other test, so this module owns its
#: watched state (docs/testing.md section 11).
FILM_NAME = "The Only Film In Its Set"


def _known_bug_on(backend, bug):
    """`expectedFailure` on one backend only, citing the bug. On the other
    backend the test runs, and must pass, as normal."""
    def wrap(fn):
        if (os.environ.get("JMS_TEST_BACKEND") or "libmpv") == backend:
            fn = unittest.expectedFailure(fn)
            fn.__doc__ = (fn.__doc__ or "") + "\n\nKnown bug: %s" % bug
        return fn
    return wrap


@_e2e.require_server
class KeyboardSmokeTest(unittest.TestCase):

    def setUp(self):
        self.session = _e2e.Session()
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.assertEqual(1, len(films),
                         "the QA library should hold exactly one %r"
                         % FILM_NAME)
        self.film = films[0]["Id"]
        self.session.reset_played(self.film)
        self.addCleanup(self.session.reset_played, self.film)

        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        backend = os.environ.get("JMS_TEST_BACKEND") or "libmpv"
        self.app = _app.App(backend=backend)
        self.addCleanup(self.app.close)
        self.app.start()

    def _log_on_failure(self):
        """The app's own log, because a failure here is inside a process
        the test cannot see into."""
        try:
            with open(self.app.log_path, encoding="utf-8",
                      errors="replace") as fh:
                return "".join(fh.readlines()[-60:])
        except OSError:
            return "(no log.txt)"

    def _login_search_and_play(self):
        """The whole keyboard flow, up to the detail page coming back after
        the film ended. Returns nothing; asserts as it goes."""
        app = self.app
        try:
            _flows.login(app, self.relay)
            _flows.open_by_search(app, "Only Film", self.film)
            app.move_to("btn-play")
            app.key("ENTER")

            # Played to the end by the app's own timeline, which reports it;
            # nothing here sends a report.
            played = _e2e.wait_for(
                lambda: (self.session.user_data(self.film) or {})
                .get("Played"), timeout=90)
            self.assertTrue(played,
                            "the film played to the end and the server does "
                            "not say it was watched")
            # And the app came back to the library rather than sitting on
            # the last frame.
            app.wait_for(lambda f: _app.shown(f, "btn-play"), timeout=60,
                         what="the detail page after playback")
        except (AssertionError, _app.AppError):
            print("\n--- app log (last 60 lines) ---\n"
                  + self._log_on_failure(), file=sys.stderr)
            raise

    def test_login_search_and_play_a_film_to_the_end(self):
        self._login_search_and_play()
        self.assertEqual([], self.relay.redirects)
        self.assertTrue(any(p.startswith("/Sessions/Playing")
                            for _m, p in self.relay.requests),
                        "no playback report went through the relay")

    @_known_bug_on("jsonipc", "B7")
    def test_closing_the_window_after_playback_exits_the_app(self):
        """What a person does when they are done. On the external-mpv
        backend the app treats mpv's exit as a crash ("connection LOST; dead
        until the next play") and stays up with no window -- B7, found by
        this harness on its first run, on Linux and Windows. Expected to fail
        there until it is fixed in slice S6; the day it passes, the manifest
        check reports an unexpected success and this marker has to go."""
        self._login_search_and_play()
        self.assertEqual(0, self.app.quit(), "the app did not exit cleanly")

if __name__ == "__main__":
    unittest.main()
