"""The server fact the deliberate-unwatch replay rule (D1) stands on.

D1: a deliberate offline unwatch is replayed as mark-unplayed only if the
server's `LastPlayedDate` is before the entry's `marked_at` -- so a watch
made elsewhere AFTER the unwatch survives. That needs the server to date a
watched mark, however it was made. Measured 2026-09-26 on 12.0.0 and
10.11.11: marking played sets `LastPlayedDate` to the moment of the request,
unmarking clears it to null, and re-marking dates it afresh.

If a server ever stopped dating a mark, "older" could not be decided and D1
would silently degrade to one of its two wrong halves. This fails first.
Run against both majors: the primary server, and JMS_E2E_SERVER_ALT.
"""

import datetime
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

#: Referenced by no other test, so this module owns its watched state.
FILM_NAME = "Named Editions Movie"
ALT = (os.environ.get("JMS_E2E_SERVER_ALT") or "").rstrip("/")


def _parse(stamp):
    """Jellyfin's UTC stamp, which carries seven fractional digits."""
    if not stamp:
        return None
    head, _, frac = stamp.rstrip("Z").partition(".")
    return datetime.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(
        microsecond=int((frac + "000000")[:6] or 0),
        tzinfo=datetime.timezone.utc)


class _Contract:
    address = None

    def setUp(self):
        self.session = _e2e.Session(address=self.address)
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.assertEqual(1, len(films))
        self.film = films[0]["Id"]
        self.session.reset_played(self.film)
        self.addCleanup(self.session.reset_played, self.film)

    def _mark(self, played):
        self.session._request("/UserPlayedItems/%s" % self.film,
                              "POST" if played else "DELETE")
        return self.session.user_data(self.film) or {}

    def test_a_watched_mark_is_dated_and_an_unmark_clears_it(self):
        before = self.session._request("/System/Info/Public") or {}
        self.assertTrue(before.get("Version"), "no server version")
        dates = []
        for _ in range(3):     # several steps: the date must MOVE each time
            ud = self._mark(True)
            self.assertTrue(ud.get("Played"))
            stamp = _parse(ud.get("LastPlayedDate"))
            self.assertIsNotNone(stamp, "a watched mark carries no "
                                        "LastPlayedDate on %s"
                                 % before.get("Version"))
            dates.append(stamp)
            ud = self._mark(False)
            self.assertFalse(ud.get("Played"))
            self.assertIsNone(ud.get("LastPlayedDate"),
                              "an unmark left a LastPlayedDate behind")
            time.sleep(1.1)
        self.assertEqual(sorted(dates), dates)
        self.assertEqual(3, len(set(dates)),
                         "re-marking did not date the mark afresh")


@_e2e.require_server
class PrimaryServerTest(_Contract, unittest.TestCase):
    address = None


@unittest.skipUnless(ALT, "JMS_E2E_SERVER_ALT is not set")
class OtherMajorTest(_Contract, unittest.TestCase):
    address = ALT


if __name__ == "__main__":
    unittest.main()
