"""Discovery, against a server that really answers the broadcast.

`tests/test_server_discovery.py` covers the screen: given a list of servers,
what is drawn and what a press does. Every one of its cases hands the UI a
dict this repo wrote, so the whole exchange it is named after -- a UDP
broadcast, a real Jellyfin answering it, and the apiclient parsing the reply --
is assumed rather than tested. That is the shape `docs/testing.md` calls a
stand-in written from the same reading of the protocol the code is.

Three things only a live server can answer:

* **it answers at all**, and with the ServerId of the server this suite is
  talking to -- matched on the id, because a developer's LAN has other
  Jellyfins on it and the test must not care;
* **the reply carries what the screen draws.** `_discovered_rows` needs
  `Address` and shows `Name`; a reply missing either draws a row with no
  evidence on it, and no fixture can disagree because the fixture supplies
  both by construction;
* **the advertised address is usable.** What a user does next is press *Use*,
  so an address that answers discovery and then refuses a connection is the
  failure this feature would be reported for. A multi-homed server
  advertising the interface it was reached on rather than one the client can
  route to is the way that happens.

**The server must be started with `--autodiscovery`** (stdjflib; off by
default, because a QA server that answers broadcasts appears in every Jellyfin
client on the network). Without it nothing answers and these skip, saying so:
a configuration this test needs and cannot create is a skip, not a failure.

Likewise skipped where the installed apiclient has no `discovery` module --
it landed in 1.19.0 and the shim's floor is deliberately older, which is the
`try` in `ServersMixin.discover_servers` and the reason this suite must not
assume it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _discovery_available():
    try:
        import jellyfin_apiclient_python.discovery  # noqa: F401
    except ImportError:
        return False
    return True


@_e2e.require_server
class LiveDiscoveryTest(unittest.TestCase):

    #: One scan for the whole class. It blocks for its entire timeout --
    #: nothing says when the last server has answered -- so paying it per
    #: test would be four seconds of nothing.
    found = None

    @classmethod
    def setUpClass(cls):
        if not _discovery_available():
            raise unittest.SkipTest(
                "this apiclient has no discovery module (added in 1.19.0); "
                "the shim's floor is >=1.18.0 on purpose")
        cls.session = _e2e.Session()
        cls.server_id = cls.session.server_id()
        # Through the shim's own gateway, not the apiclient directly: the
        # `try`/`except` and the list() around it are the code under test,
        # and a test that called the library would pass with the gateway
        # deleted.
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        cls.found = PlayerGateway().discover_servers(timeout=2.0)
        cls.ours = [s for s in cls.found if s.get("Id") == cls.server_id]
        if not cls.ours:
            raise unittest.SkipTest(
                "no server answered UDP 7359 with ServerId %s -- start the "
                "QA server with `--autodiscovery --on-host` (it is off by "
                "default, and the container publishes no UDP port). %d other "
                "server(s) answered." % (cls.server_id, len(cls.found)))

    @classmethod
    def tearDownClass(cls):
        session = getattr(cls, "session", None)
        if session is not None:
            session.stop()

    def test_the_server_we_are_talking_to_answers_the_broadcast(self):
        """Matched on ServerId, so another Jellyfin on the LAN neither
        satisfies this nor breaks it."""
        self.assertEqual(
            1, len(self.ours),
            "one server answered twice with the same id: %r" % (self.ours,))

    def test_the_reply_carries_what_the_login_screen_draws(self):
        """`_discovered_rows` drops a reply with no `Address` and shows the
        `Name` beside it. Both come off the wire, and a fixture supplies both
        by construction, so this is the only place the real shape is checked.
        """
        reply = self.ours[0]

        self.assertTrue(reply.get("Address"),
                        "no Address, so the screen would drop this row")
        self.assertTrue(reply.get("Name"),
                        "no Name, so the row would show its address twice")

    def test_the_advertised_address_is_one_a_client_can_use(self):
        """The press after the one this feature exists for.

        Discovery hands back an address and *Use* puts it in the form, so an
        address that answers the broadcast and then refuses a connection is
        exactly what this would be reported as broken for. Asserting the id
        matches as well as that something answered: a different Jellyfin on
        that address would be a wrong answer that still returns 200.
        """
        import json
        import urllib.request

        address = self.ours[0]["Address"].rstrip("/")
        with urllib.request.urlopen(
                address + "/System/Info/Public", timeout=10) as resp:
            public = json.loads(resp.read()) or {}

        self.assertEqual(
            self.server_id, public.get("Id"),
            "the address discovery advertises (%s) does not lead back to the "
            "server that advertised it" % address)

    def test_the_screen_builds_a_row_from_the_real_reply(self):
        """The seam the two suites leave between them.

        The unit tests draw from a dict this repo wrote; this draws from the
        wire. If a real reply ever stops carrying what `_discovered_rows`
        reads, that is the failure nothing else in the project can see.
        """
        from tests._shell_harness import FakeSource, _SyncPool, build_scene, ids

        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        browser = MpvtkBrowser(app=None, source=FakeSource())
        browser._pool = _SyncPool()
        self.addCleanup(browser.shutdown)
        browser.show_login()
        # The real replies, put where a real scan would have left them. The
        # scan itself is covered above; what is under test here is the draw.
        browser._discovered = list(self.found)

        nodes, _h = build_scene(browser, size=(1600, 900))
        texts = [n.get("text") for n in nodes if n.get("t") == "text"]

        self.assertIn("login-found-row-0", ids(nodes),
                      "a real discovery reply drew no row")
        self.assertIn(self.ours[0]["Address"], texts,
                      "the address is not on screen, and it is the only part "
                      "of an unauthenticated reply a user can judge")


if __name__ == "__main__":
    unittest.main()
