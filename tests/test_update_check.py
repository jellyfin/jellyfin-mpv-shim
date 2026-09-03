"""Unit tests for update-notice routing.

When a UI is running (it sets ``playerManager.notify_update``) the update
notice must go to that callback (the browser banner); otherwise it falls back
to an MPV OSD toast. These exercise the routing without any network or Tk.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest import mock

import jellyfin_mpv_shim.update_check as uc
from jellyfin_mpv_shim.update_check import (
    UpdateChecker, parse_release_url, release_url)


class _Resp:
    """Stand-in for the GitHub /releases/latest redirect."""
    def __init__(self, version):
        self.status_code = 302
        self.headers = {"location": release_url + "tag/v" + version}


class FakePlayer:
    def __init__(self, with_ui):
        self.osd_calls = []
        self.ui_calls = []
        if with_ui:
            self.notify_update = lambda version, url: self.ui_calls.append(
                (version, url))
        # else: attribute absent, mirroring a CLI player

    def show_text(self, text, duration, level):
        self.osd_calls.append((text, duration, level))


class UpdateNoticeRoutingTest(unittest.TestCase):
    def test_routes_to_ui_when_callback_present(self):
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()
        self.assertEqual(player.ui_calls, [("2.9.0", release_url + "latest")])
        self.assertEqual(player.osd_calls, [])

    def test_falls_back_to_osd_without_ui(self):
        player = FakePlayer(with_ui=False)
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()
        self.assertEqual(player.ui_calls, [])
        self.assertEqual(len(player.osd_calls), 1)
        self.assertIn("2.9.0", player.osd_calls[0][0])

    def test_first_check_notifies_when_update_found(self):
        # Regression: _check_updates() used to `return` inside the for loop, so
        # a found update (which `break`s) returned None and check() skipped the
        # notify on the run that discovered it -- the daily throttle then hid it
        # until the next day. The first check must notify immediately.
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            rq.get.return_value = _Resp("99.0.0")
            chk.check()
        self.assertEqual(chk.new_version, "99.0.0")
        self.assertEqual(player.ui_calls, [("99.0.0", release_url + "latest")])

    def test_no_notify_when_up_to_date(self):
        from jellyfin_mpv_shim.constants import CLIENT_VERSION
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            rq.get.return_value = _Resp(CLIENT_VERSION)
            chk.check()
        self.assertIsNone(chk.new_version)
        self.assertEqual(player.ui_calls, [])

    def test_no_notify_when_running_ahead_of_the_latest_release(self):
        # The regression: string inequality told anyone on a pre-release that
        # the older stable tag was an update, every day, forever.
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc, "CLIENT_VERSION", "3.0.0pre8"), \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            rq.get.return_value = _Resp("2.10.0")
            chk.check()
        self.assertIsNone(chk.new_version)
        self.assertEqual(player.ui_calls, [])

    def test_notifies_when_a_pre_release_is_superseded(self):
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc, "CLIENT_VERSION", "3.0.0pre8"), \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            rq.get.return_value = _Resp("3.0.0")
            chk.check()
        self.assertEqual(chk.new_version, "3.0.0")

    def test_version_is_taken_from_the_last_path_segment(self):
        # Not a fixed offset into the URL: the tags dropping their "v" must
        # not silently start reporting "v3.0.0" as the available version.
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        resp = _Resp("99.0.0")
        resp.headers["location"] = release_url + "tag/99.0.0"
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", False):
            rq.get.return_value = resp
            chk.check()
        self.assertEqual(chk.new_version, "99.0.0")

    def test_osd_fallback_when_ui_callback_raises(self):
        player = FakePlayer(with_ui=True)
        player.notify_update = lambda *_a: (_ for _ in ()).throw(RuntimeError())
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()  # must not raise; falls back to the OSD
        self.assertEqual(len(player.osd_calls), 1)


class _Redirect:
    """A bare redirect response, for driving a chain by URL."""
    def __init__(self, location, status=302):
        self.status_code = status
        self.headers = {"location": location}


class _NotFound:
    status_code = 404
    headers: dict = {}


class _Chain:
    """Stand-in for ``requests``: answers each URL from a table, and records
    the order they were asked for so a test can see what was *not* followed."""

    def __init__(self, table):
        self.table = table
        self.asked = []
        self.kwargs = []

    def get(self, url, **kw):
        # The kwargs are RECORDED, not swallowed. `requests.get(url)` with no
        # `allow_redirects=False` hands the whole hop-by-hop owner check to
        # requests, which follows the chain itself and lands on a 200 -- so
        # the update check would stop working forever, and a fake that
        # discarded kwargs would report a pass throughout.
        self.asked.append(url)
        self.kwargs.append(kw)
        try:
            return self.table[url]
        except KeyError:
            return _NotFound()


GH = "https://github.com/"
MOVED = GH + "jellyfin-labs/jellyfin-mpv-shim/releases/"


class ReleaseUrlParsingTest(unittest.TestCase):
    """What the update checker is willing to be redirected to.

    The owner allow-list is the whole control here: a redirect says where the
    account holding the old name is pointing *today*, so following one on
    faith hands anyone who takes that account over the update notice of every
    install ever made.
    """

    def test_the_current_home_parses(self):
        base, rest = parse_release_url(release_url + "latest")
        self.assertEqual((base, rest), (release_url, "latest"))

    def test_a_rename_inside_an_allowed_owner_is_followed(self):
        # The repository name is deliberately not pinned -- a rename is the
        # ordinary case this exists for.
        self.assertIsNotNone(
            parse_release_url(GH + "jellyfin/jf-mpv-shim/releases/latest"))

    def test_the_two_moves_we_would_accept(self):
        for owner in ("jellyfin-labs", "iwalton3"):
            with self.subTest(owner=owner):
                self.assertIsNotNone(parse_release_url(
                    "%s%s/jellyfin-mpv-shim/releases/latest" % (GH, owner)))

    def test_any_other_owner_is_refused(self):
        for owner in ("jellyfin-mpv-shim", "jellyf1n", "not-jellyfin",
                      "jellyfin-labs-evil"):
            with self.subTest(owner=owner):
                self.assertIsNone(parse_release_url(
                    "%s%s/jellyfin-mpv-shim/releases/latest" % (GH, owner)))

    def test_another_host_is_refused_however_it_is_spelled(self):
        for url in (
                "https://gitlab.com/jellyfin/jellyfin-mpv-shim/releases/latest",
                "http://github.com/jellyfin/jellyfin-mpv-shim/releases/latest",
                # The userinfo trick: the real host is the one after the @,
                # which a startswith() against our own URL reads as ours.
                "https://github.com@evil.invalid/jellyfin/x/releases/latest",
                "https://github.com.evil.invalid/jellyfin/x/releases/latest",
                "https://raw.github.com/jellyfin/x/releases/latest"):
            with self.subTest(url=url):
                self.assertIsNone(parse_release_url(url))

    def test_a_name_outside_what_github_allows_is_refused(self):
        """The base is rebuilt out of these two segments and then requested,
        so they are held to the shape of a real GitHub name rather than
        re-emitted as whatever a server sent."""
        for url in (GH + "jellyfin/jellyfin mpv shim/releases/latest",
                    GH + "jellyfin/..%2fevil/releases/latest",
                    GH + "-jellyfin/x/releases/latest"):
            with self.subTest(url=url):
                self.assertIsNone(parse_release_url(url))

    def test_the_base_is_rebuilt_not_echoed(self):
        """The caller only ever requests a URL this function composed. Pinned
        with an input where the rebuild DIFFERS from the string handed in, or
        a version that echoed a slice of the server's own reply would pass."""
        base, rest = parse_release_url(
            "https://u:p@GitHub.com/jellyfin/x/releases/latest?a=1#f")
        self.assertEqual(base, GH + "jellyfin/x/releases/")
        self.assertEqual(rest, "latest")

    def test_an_allowed_owner_in_any_casing_is_followed(self):
        """GitHub owner names are case-insensitive, so refusing a differently
        cased one would orphan installs on a purely cosmetic difference."""
        self.assertIsNotNone(
            parse_release_url(GH + "JellyFin/x/releases/latest"))

    def test_a_repository_of_nothing_but_dots_is_refused(self):
        for repo in (".", ".."):
            with self.subTest(repo=repo):
                self.assertIsNone(parse_release_url(
                    "%sjellyfin/%s/releases/latest" % (GH, repo)))

    def test_a_port_is_refused(self):
        self.assertIsNone(
            parse_release_url(GH[:-1] + ":8443/jellyfin/x/releases/latest"))

    def test_a_malformed_authority_answers_none_rather_than_raising(self):
        """Documented contract: it returns None for anything it will not
        vouch for. `urlsplit` raises on some of those."""
        self.assertIsNone(
            parse_release_url("https://[::ffff:github.com]/jellyfin/x/"
                              "releases/latest"))

    def test_a_non_release_path_is_refused(self):
        for path in ("jellyfin/jellyfin-mpv-shim",
                     "jellyfin/jellyfin-mpv-shim/issues/1",
                     "jellyfin/releases/latest"):
            with self.subTest(path=path):
                self.assertIsNone(parse_release_url(GH + path))


class RepositoryMoveTest(unittest.TestCase):
    """A rename or a transfer must not orphan everyone already installed."""

    def _check(self, table):
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        chain = _Chain(table)
        with mock.patch.object(uc, "requests", chain), \
                mock.patch.object(uc, "CLIENT_VERSION", "3.0.0"), \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            chk.check()
        return chk, player, chain

    def test_a_permanent_redirect_to_an_allowed_owner_is_followed(self):
        chk, player, chain = self._check({
            release_url + "latest": _Redirect(MOVED + "latest", status=301),
            MOVED + "latest": _Redirect(MOVED + "tag/v3.1.0"),
        })
        self.assertEqual(chk.new_version, "3.1.0")
        # And the notice points at where the release actually is, not at a URL
        # that only works while GitHub keeps redirecting it.
        self.assertEqual(player.ui_calls, [("3.1.0", MOVED + "latest")])
        self.assertEqual(chk.release_url, MOVED)

    def test_a_rename_is_followed_too(self):
        renamed = GH + "jellyfin/jellyfin-mpv-client/releases/"
        chk, _player, _chain = self._check({
            release_url + "latest": _Redirect(renamed + "latest", status=301),
            renamed + "latest": _Redirect(renamed + "tag/v3.1.0"),
        })
        self.assertEqual(chk.new_version, "3.1.0")

    def test_a_move_to_anyone_else_is_refused(self):
        """A hostile takeover of the name must not re-home the update check."""
        hostile = GH + "definitely-jellyfin/jellyfin-mpv-shim/releases/"
        chk, player, chain = self._check({
            release_url + "latest": _Redirect(hostile + "latest", status=301),
            hostile + "latest": _Redirect(hostile + "tag/v9.9.9"),
        })
        self.assertIsNone(chk.new_version)
        self.assertEqual(player.ui_calls, [])
        self.assertEqual(chk.release_url, release_url)
        # Refused, not merely disbelieved: the second hop is never requested.
        self.assertEqual(chain.asked, [release_url + "latest"])

    def test_the_tag_hop_is_checked_as_well_as_the_move(self):
        """The last hop is the one that carries the version, so an allowed
        first hop does not buy the second one any trust."""
        hostile = GH + "someone-else/jellyfin-mpv-shim/releases/"
        chk, _player, _chain = self._check({
            release_url + "latest": _Redirect(hostile + "tag/v9.9.9"),
        })
        self.assertIsNone(chk.new_version)

    def test_a_relative_location_is_resolved_against_the_url_asked_for(self):
        chk, _player, _chain = self._check({
            release_url + "latest": _Redirect(
                "/jellyfin-labs/jellyfin-mpv-shim/releases/latest", status=301),
            MOVED + "latest": _Redirect(MOVED + "tag/v3.1.0"),
        })
        self.assertEqual(chk.new_version, "3.1.0")

    def test_every_request_declines_to_follow_and_carries_a_timeout(self):
        """Both are load-bearing and neither is visible in a result. Without
        `allow_redirects=False` requests follows the chain itself, past every
        owner check, and lands on a 200 -- the check then never finds a
        version again. Without a timeout the daily check can hang a thread
        that is holding the player lock."""
        _chk, _player, chain = self._check({
            release_url + "latest": _Redirect(MOVED + "latest", status=301),
            MOVED + "latest": _Redirect(MOVED + "tag/v3.1.0"),
        })
        self.assertEqual(len(chain.kwargs), 2)
        for kw in chain.kwargs:
            self.assertIs(kw.get("allow_redirects"), False)
            self.assertIsNotNone(kw.get("timeout"))

    def test_a_redirect_loop_is_caught_by_the_loop_detector(self):
        """Asserted as *two* requests, not "at most MAX_HOPS": the budget and
        the loop detector are two independent stoppers, and an at-most
        assertion lets either one cover for the other's removal."""
        chk, _player, chain = self._check({
            release_url + "latest": _Redirect(MOVED + "latest", status=301),
            MOVED + "latest": _Redirect(release_url + "latest", status=301),
        })
        self.assertIsNone(chk.new_version)
        self.assertEqual(len(chain.asked), 2)

    def test_a_chain_that_never_reaches_a_tag_gives_up(self):
        """Every hop is a legal move, so nothing refuses it -- the hop budget
        is what stops this, and it must stop."""
        table = {}
        owners = ("jellyfin", "jellyfin-labs", "iwalton3")
        for i in range(10):
            here = "%s%s/shim-%d/releases/" % (GH, owners[i % 3], i)
            there = "%s%s/shim-%d/releases/" % (GH, owners[(i + 1) % 3], i + 1)
            table[here + "latest"] = _Redirect(there + "latest", status=301)
        table[release_url + "latest"] = _Redirect(
            "%s%s/shim-0/releases/latest" % (GH, owners[0]), status=301)
        chk, _player, chain = self._check(table)
        self.assertIsNone(chk.new_version)
        # A literal, not `uc.MAX_HOPS`: an assertion that reads the constant
        # under test passes at any value it is given, including 500.
        self.assertLessEqual(len(chain.asked), 4)

    def test_the_move_is_adopted_even_when_we_are_up_to_date(self):
        """The menu item and the notice open ``release_url``, so it has to
        follow the repository whatever the version comparison said -- not only
        on the run that happens to find an update."""
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        chain = _Chain({
            release_url + "latest": _Redirect(MOVED + "latest", status=301),
            MOVED + "latest": _Redirect(MOVED + "tag/v1.0.0"),
        })
        with mock.patch.object(uc, "requests", chain), \
                mock.patch.object(uc, "CLIENT_VERSION", "3.0.0"), \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            chk.check()
        self.assertIsNone(chk.new_version)
        self.assertEqual(chk.release_url, MOVED)

    def test_a_non_redirect_status_is_refused(self):
        """A 200 carrying a plausible Location is not an answer. Without the
        status check this reads the header anyway and reports a version."""
        chk, _player, _chain = self._check({
            release_url + "latest": _Redirect(release_url + "tag/v9.9.9",
                                              status=200),
        })
        self.assertIsNone(chk.new_version)

    def test_a_redirect_that_is_neither_latest_nor_a_tag_is_refused(self):
        """The two shapes this understands are a move (`latest`) and an answer
        (`tag/...`). Anything else is somewhere it does not know how to be."""
        for rest in ("", "download/v3.1.0/x.exe", "expanded_assets/v3.1.0"):
            with self.subTest(rest=rest):
                chk, _player, _chain = self._check({
                    release_url + "latest": _Redirect(release_url + rest),
                    release_url + rest: _Redirect(release_url + "tag/v9.9.9"),
                })
                self.assertIsNone(chk.new_version)

    def test_a_tag_that_is_not_a_version_is_refused(self):
        """`is_newer` sorts anything it cannot parse as NEWER, so a release
        named `stable` -- or a bare `/releases/tag/` -- announced an update to
        a version with no number in it, permanently (has_notified latches)."""
        for rest in ("tag/", "tag//", "tag/stable", "tag/v", "tag/latest"):
            with self.subTest(rest=rest):
                chk, player, _chain = self._check({
                    release_url + "latest": _Redirect(release_url + rest),
                })
                self.assertIsNone(chk.new_version)
                self.assertEqual(player.ui_calls, [])

    def test_the_whole_chain_is_bounded_in_wall_clock(self):
        """The hop budget bounds the number of requests; this bounds the TIME.
        They are not the same guarantee: `check()` runs inside `_play_media`
        with the player lock held, so four sequential 13-second stalls is most
        of a minute of dead transport controls."""
        table = {}
        owners = ("jellyfin", "jellyfin-labs", "iwalton3")
        for i in range(10):
            here = "%s%s/shim-%d/releases/" % (GH, owners[i % 3], i)
            there = "%s%s/shim-%d/releases/" % (GH, owners[(i + 1) % 3], i + 1)
            table[here + "latest"] = _Redirect(there + "latest", status=301)
        table[release_url + "latest"] = _Redirect(
            "%s%s/shim-0/releases/latest" % (GH, owners[0]), status=301)
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        chain = _Chain(table)
        with mock.patch.object(uc, "requests", chain), \
                mock.patch.object(uc, "CHAIN_BUDGET", 0.0), \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            chk.check()
        self.assertIsNone(chk.new_version)
        # The first hop always goes out with its full read timeout; it is the
        # ones after it that the budget stops.
        self.assertEqual(len(chain.asked), 1)

    def test_a_dead_url_is_not_a_version(self):
        chk, _player, _chain = self._check({})
        self.assertIsNone(chk.new_version)



if __name__ == "__main__":
    unittest.main()
