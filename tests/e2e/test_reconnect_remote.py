"""Slice S4, `reconnect_remote`: remote control, and getting it back.

Critical-path inventory rows 7 and 57. No e2e test issued a remote Play
before this (the inventory's grep); the integration tests of the landing
use fakes. Here the SERVER sends the command -- the test asks it to, as a
phone would -- and the app's own websocket carries it.

- row 7: Play onto an idle app, and onto one already playing: the right
  item, at the offset asked, and not skipped a moment later by the old
  item's stale finish;
- row 57: the network goes, the health check drops the server, the
  network comes back -- and a remote Play reaches the app with no
  restart. That chain (health check -> reconnect -> websocket
  re-registration -> command delivered) "was fully broken before".
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402
from test_playback_lifecycle import (  # noqa: E402
    LONG_NAME, SECOND_LONG_NAME, _PlaybackCase)

TICKS = 10_000_000


class _RemoteCase(_PlaybackCase):

    def device_id(self):
        with open(os.path.join(self.app.config_dir, "conf.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)["client_uuid"]

    def app_session(self, timeout=30):
        """The app's session on the server: what a phone's cast list
        shows, and what a remote command is addressed to."""
        device = self.device_id()
        found = {}

        def look():
            for s in self.session.api.sessions() or []:
                if s.get("DeviceId") == device and s.get(
                        "SupportsRemoteControl"):
                    found["id"] = s["Id"]
                    return True
            return False
        self.assertTrue(_e2e.wait_for(look, timeout=timeout),
                        "the app has no remote-controllable session")
        return found["id"]

    def remote_play(self, item_id, at_seconds):
        self.session.api.remote_play_media(
            self.app_session(), [item_id],
            params={"startPositionTicks": int(at_seconds * TICKS)})

    def playing(self, item_id):
        return item_id in (self.app.prop("path") or "")

    def assert_lands(self, item_id, at_seconds, what):
        self.assertTrue(_e2e.wait_for(
            lambda: self.playing(item_id)
            and (self.time_pos() or 0) > at_seconds - 5, timeout=45),
            "%s: never playing %s near %d s (path %r, at %r)"
            % (what, item_id, at_seconds, self.app.prop("path"),
               self.time_pos()))
        self.assertLess(abs(self.time_pos() - at_seconds), 15,
                        "%s: started at %.0f s, asked for %d s"
                        % (what, self.time_pos(), at_seconds))
        # Not skipped a moment later by the previous item's finish.
        time.sleep(10)
        self.assertTrue(self.playing(item_id),
                        "%s: the cast item was replaced within 10 s (path "
                        "%r)" % (what, self.app.prop("path")))


class RemotePlayTest(_RemoteCase):
    """Row 7."""

    def test_onto_an_idle_app(self):
        film = self.movie(LONG_NAME)
        self.fresh(film)
        self.remote_play(film, 120)
        self.assert_lands(film, 120, "idle")
        self.assertEqual(0, self.app.quit(timeout=30))

    def test_onto_an_app_already_playing(self):
        first, second = self.movie(LONG_NAME), self.movie(SECOND_LONG_NAME)
        self.fresh(first, second)
        from test_input_live import _InputCase
        _InputCase.open_film(self)
        self.play()
        self.assertTrue(_e2e.wait_for(lambda: self.playing(first)
                                      and self.time_pos() > 2, timeout=30))
        # The cast lands as the first film ENDS: its finish is what could
        # skip the new item (FinishedCallbackTest's cast-at-EOF case, which
        # only a fake had driven). Setup seek, not the claim.
        end = self.app.prop("duration")
        self.app.mpv.command("seek", str(end - 3), "absolute")
        self.remote_play(second, 90)
        self.assert_lands(second, 90, "already playing")
        self.assertEqual(0, self.app.quit(timeout=30))


class RemoteControlComesBackTest(_RemoteCase):
    """Row 57: cut past the health check, restore, and a remote Play
    lands -- no restart. health_check_interval seeded short (as S1's
    scenario 8); the log is the witness that the drop really happened."""

    CONF = {"health_check_interval": 10}

    def log(self):
        try:
            with open(self.app.log_path, encoding="utf-8",
                      errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def test_remote_play_after_the_server_comes_back(self):
        film = self.movie(LONG_NAME)
        self.fresh(film)
        self.app_session()                       # registered, before
        connects = self.log().count("WebSocket connected")
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        self.assertTrue(_e2e.wait_for(
            lambda: "treating as disconnected" in self.log(), timeout=90),
            "no health check dropped the server while it was gone")
        self.relay.restore()
        self.assertTrue(_e2e.wait_for(
            lambda: self.log().count("WebSocket connected") > connects,
            timeout=120), "the websocket never came back")
        # A phone sending Play as soon as the app shows up again. Resent if
        # the first lands in the moment before capabilities are posted --
        # the claim is "without a restart", not "on the first message".
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not self.playing(film):
            self.remote_play(film, 60)
            _e2e.wait_for(lambda: self.playing(film), timeout=15)
        self.assert_lands(film, 60, "after the outage")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
