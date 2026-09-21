"""The refresh request, against the servers it is aimed at.

Everything this feature rests on was read rather than measured, and one of the
readings was wrong: the code sent `Recursive: True` and the docstring and
`docs/PERMISSION_GAPS.md` §7 both named it as what carries a series refresh
down to its episodes. It is not a parameter of the endpoint on either
supported major. A review round caught that by reading the server's source,
and `tests/test_shell_refresh_metadata.py` could not have, because it asserts
our own outgoing dict.

So this module replays **the request the code actually builds** -- captured
from `EditingMixin.refresh_item` the same way the unit test captures it,
rather than hand-copied here where it could drift -- against a live server,
and asks three things a fake cannot:

* **the server accepts it.** An unrecognised parameter is dropped, but a
  malformed enum value is a 400, and nothing had ever sent this exact set to
  a real server;
* **`Recursive` really is inert**, from the wire instead of from the source:
  the same request with and without it answers identically. That is this
  repo's standing claim about Jellyfin ("it drops what it does not
  recognise") applied to the one parameter that was wrong;
* **an ordinary account is refused.** §7 calls the endpoint
  "administrator-only by construction" and gates the menu entry on
  `IsAdministrator` -- and `user_policy.may_refresh_metadata` *fails open*, so
  a policy fetch that failed shows this entry to a non-admin. What the server
  then does is the whole justification for tolerating that, and it was never
  checked.

**What this does NOT cover, deliberately:** that the refresh reaches the
episodes under a series. The recursion is real -- `ProviderManager.RefreshItem`
calls `Folder.ValidateChildren`, whose `recursive` defaults to true -- but
observing it needs a metadata provider that returns something, and the QA
library's metadata comes from NFO files on disk. Asserting it would mean
mutating the fixture library mid-suite. The claim is sourced in
`gateway/editing.py`; it is not measured here and this paragraph is the
record of that.
"""

import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _accounts  # noqa: E402
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _request_the_code_builds():
    """``(path, params)`` as `EditingMixin.refresh_item` sends them.

    Taken from the method rather than written out here: a copy would keep
    passing after the real request changed, which is the failure this module
    exists to make impossible.
    """
    from jellyfin_mpv_shim.mpvtk_browser.gateway.editing import EditingMixin

    sent = {}

    class _JF:
        @staticmethod
        def items(path, action="GET", params=None, **kw):
            sent.update({"path": path, "action": action,
                         "params": dict(params or {})})

    gateway = EditingMixin.__new__(EditingMixin)
    gateway._edit = lambda _server, fn: fn(_JF())
    gateway.refresh_item("srv", "ITEM")
    assert sent.get("action") == "POST", sent
    # The apiclient prefixes `/Items`; `refresh_item` passes the rest.
    return "/Items" + sent["path"], sent["params"]


def _post(address, token, path, params):
    """POST and return the status, or the status of the HTTPError."""
    url = "%s%s?%s" % (address.rstrip("/"), path,
                       urllib.parse.urlencode(params))
    req = urllib.request.Request(
        url, method="POST", data=b"",
        headers={"Authorization": 'MediaBrowser Token="%s"' % token,
                 "Content-Length": "0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as error:
        return error.code


@_e2e.require_server
class TheRefreshRequestTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.admin = _e2e.Session(_accounts.ADMIN_ACCOUNT)
        cls.user = _e2e.Session()
        series = cls.admin.find_all(library="Shows", item_type="Series")
        if not series:
            raise unittest.SkipTest("need a series in the QA library")
        cls.series_id = series[0]["Id"]
        cls.path, cls.params = _request_the_code_builds()

    @classmethod
    def tearDownClass(cls):
        for session in (getattr(cls, "admin", None), getattr(cls, "user", None)):
            if session is not None:
                session.stop()

    def _path_for(self, item_id):
        return self.path.replace("ITEM", item_id)

    def test_the_server_accepts_the_request_the_code_builds(self):
        """Accepted, not merely "did not raise": a 400 here would mean a
        value the endpoint cannot parse, which no fake can report."""
        status = _post(self.admin.address, self.admin.token,
                       self._path_for(self.series_id), self.params)

        self.assertIn(status, (200, 202, 204),
                      "the server refused the refresh this code sends: "
                      "%s %s" % (status, self.params))

    def test_an_unrecognised_parameter_changes_nothing(self):
        """The claim the corrected docstring rests on, measured.

        `Recursive` was sent for a while and named as the mechanism. If the
        endpoint had *rejected* it the feature would have been visibly broken;
        that it is dropped is why nobody noticed, and this is the assertion
        that says so from the wire rather than from the server's source.
        """
        without = _post(self.admin.address, self.admin.token,
                        self._path_for(self.series_id), self.params)
        with_it = _post(self.admin.address, self.admin.token,
                        self._path_for(self.series_id),
                        dict(self.params, Recursive=True))

        self.assertEqual(without, with_it,
                         "the server tells the two requests apart, so "
                         "`Recursive` is not inert after all")

    def test_an_ordinary_account_is_refused(self):
        """`user_policy.may_refresh_metadata` fails open, so a failed policy
        fetch shows this entry to a non-administrator. §7 accepts that on the
        grounds that the server refuses them -- which is this."""
        status = _post(self.user.address, self.user.token,
                       self._path_for(self.series_id), self.params)

        self.assertIn(status, (401, 403),
                      "an ordinary account could refresh metadata (%s), so "
                      "the gate is the only thing stopping them" % status)


@_e2e.require_server
class TheOtherMajorAgreesTest(unittest.TestCase):
    """The claim is about *either* supported major, so it is asked of both.

    `JMS_E2E_SERVER_ALT` is the second server `test_filter_matrix` uses; with
    it unset this skips rather than quietly asserting half of what it says.
    """

    @classmethod
    def setUpClass(cls):
        alt = (os.environ.get("JMS_E2E_SERVER_ALT") or "").rstrip("/")
        if not alt:
            raise unittest.SkipTest(
                "set JMS_E2E_SERVER_ALT to the other major's QA server")
        cls.session = _e2e.Session(_accounts.ADMIN_ACCOUNT, address=alt)
        series = cls.session.find_all(library="Shows", item_type="Series")
        if not series:
            raise unittest.SkipTest("need a series on the alternate server")
        cls.series_id = series[0]["Id"]
        cls.path, cls.params = _request_the_code_builds()

    @classmethod
    def tearDownClass(cls):
        session = getattr(cls, "session", None)
        if session is not None:
            session.stop()

    def test_it_accepts_the_same_request(self):
        path = self.path.replace("ITEM", self.series_id)

        status = _post(self.session.address, self.session.token,
                       path, self.params)

        self.assertIn(status, (200, 202, 204),
                      "%s refused the refresh: %s"
                      % (_e2e.public_version(self.session.address), status))

    def test_and_drops_the_same_unrecognised_parameter(self):
        path = self.path.replace("ITEM", self.series_id)

        without = _post(self.session.address, self.session.token,
                        path, self.params)
        with_it = _post(self.session.address, self.session.token,
                        path, dict(self.params, Recursive=True))

        self.assertEqual(without, with_it)


if __name__ == "__main__":
    unittest.main()
