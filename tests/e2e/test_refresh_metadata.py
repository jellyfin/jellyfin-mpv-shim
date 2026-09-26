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
* **`Recursive` is not a name this endpoint knows**, from the wire instead of
  from the source. Not by comparing two successes -- `ReplaceAllMetadata=true`
  is recognised *and* honoured and answers the same 204, so that comparison
  passes for a parameter that plainly does something. By the contrast this
  repo already prescribes: a bogus value for a name the endpoint knows is a
  400, and `Recursive` with an equally bogus value is accepted, which it
  could only be if nothing ever read it;
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


#: A parameter name the endpoint **knows**, and the positive half of the
#: control below. Taken from the request the code builds rather than written
#: out, so a rename in `refresh_item` fails here instead of quietly turning
#: the control into two requests that both succeed.
RECOGNISED = "MetadataRefreshMode"

#: The name under test. Named once and used in both the request and the
#: message about it -- item 9 of this round was two functions three lines
#: apart that disagreed after one of them was edited.
UNRECOGNISED = "Recursive"


def _assert_recursive_is_not_a_parameter(test, address, token, path, params):
    """The inertness claim, measured instead of read from the server's source.

    Two equal successes cannot tell an ignored parameter from an honoured
    one, and that is not a hypothetical here: `ReplaceAllMetadata=true` is
    recognised *and* behaviour-changing, and sending it against this same
    baseline also answers 204. So a comparison of statuses passes for a
    parameter that unquestionably does something.

    What can fail is the contrast CLAUDE.md already prescribes -- "the
    evidence that a value parses is the contrast with a deliberately bogus
    one". The endpoint **does** validate the names it knows: an unparseable
    value for `RECOGNISED` is a 400. `Recursive` with an equally unparseable
    value is accepted, which it could only be if the name never reached a
    binder at all.

    `POST /Items/{id}/Refresh` answers 204 with no body and
    `DateLastRefreshed` is in no DTO the API returns, so there is no
    after-state to observe. This is the observable that exists: it is on the
    request, not on the result.

    Stated once and called from both majors' classes, because a rule written
    out twice is how the two drift apart.
    """
    test.assertIn(RECOGNISED, params,
                  "the request no longer carries %s, so this control is "
                  "asking about a name the endpoint may not know either"
                  % RECOGNISED)

    known_but_bogus = _post(address, token, path,
                            dict(params, **{RECOGNISED: "NotARealMode"}))
    test.assertEqual(
        400, known_but_bogus,
        "%s is a name this endpoint knows, so an unparseable value for it "
        "must be refused. It answered %s, which means this endpoint "
        "validates nothing and the contrast below proves nothing"
        % (RECOGNISED, known_but_bogus))

    unknown_and_bogus = _post(address, token, path,
                              dict(params, **{UNRECOGNISED: "NotABool"}))
    test.assertIn(
        unknown_and_bogus, (200, 202, 204),
        "`%s` with a value nothing could parse answered %s. The endpoint "
        "refuses bad values for names it knows, so being accepted is what "
        "says the name is dropped before anything reads it -- and %s says "
        "it is not"
        % (UNRECOGNISED, unknown_and_bogus, unknown_and_bogus))


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

    def test_recursive_is_not_a_parameter_this_endpoint_knows(self):
        """The claim the corrected docstring rests on, measured.

        `Recursive` was sent for a while and named as the mechanism. If the
        endpoint had *rejected* it the feature would have been visibly broken;
        that it is dropped is why nobody noticed, and this says so from the
        wire rather than from the server's source.

        The status comparison is kept and is no longer what carries the
        claim -- see `_assert_recursive_is_not_a_parameter` for why two equal
        successes cannot establish it.
        """
        without = _post(self.admin.address, self.admin.token,
                        self._path_for(self.series_id), self.params)
        with_it = _post(self.admin.address, self.admin.token,
                        self._path_for(self.series_id),
                        dict(self.params, Recursive=True))

        self.assertEqual(without, with_it,
                         "the server tells the two requests apart, so "
                         "`Recursive` is not inert after all")

        _assert_recursive_is_not_a_parameter(
            self, self.admin.address, self.admin.token,
            self._path_for(self.series_id), self.params)

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

    def test_and_does_not_know_the_same_parameter_either(self):
        """The same control, because the claim is about both majors.

        Strengthened here too rather than only on the primary server: a rule
        applied at one site of two is the shape this whole round is about,
        and leaving the weaker assertion here would have reproduced it inside
        its own fix.
        """
        path = self.path.replace("ITEM", self.series_id)

        without = _post(self.session.address, self.session.token,
                        path, self.params)
        with_it = _post(self.session.address, self.session.token,
                        path, dict(self.params, Recursive=True))

        self.assertEqual(without, with_it)

        _assert_recursive_is_not_a_parameter(
            self, self.session.address, self.session.token, path, self.params)


if __name__ == "__main__":
    unittest.main()
