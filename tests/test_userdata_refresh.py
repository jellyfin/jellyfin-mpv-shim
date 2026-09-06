"""Watched state reaches every screen that shows it, not only Home (#722).

A season fully watched in the web UI still read "3 episodes remaining" on
the shim's series page, and opening the season showed every episode ticked.
The badge is `UserData.UnplayedItemCount` on the PARENT -- computed by the
server, so there is nothing to patch client-side -- and the only thing that
refreshed on a `UserDataChanged` event was Home. A restart fixed it; the
next episode brought it back.

#560 built the right mechanism and wired it to the screen that was reported.
This widens what it covers, and collapses the second copy of it that had
grown alongside: `refresh_live_tv` was the same rules written again, and the
two had drifted -- only the Live TV one took the `_refreshing` marker that
stops a scroll paging in against a list about to be replaced. Widening the
Home gate would have made that three copies and carried the missing marker
onto a paged grid, which is where its absence stops being harmless.

The server filter here is a pre-existing latent bug that this promotes:
`_user_data_changed` discarded its client and refreshed whatever was on
screen. Fair for Home, which is assembled from every logged-in server.
Not fair for a Series page, which belongs to one.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser.app import (  # noqa: E402
    LIVE_KINDS, USERDATA_KINDS, MpvtkBrowser)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


def _browser(kind="series", server="srv1"):
    """A browser parked on ``kind`` with its loader replaced by a recorder.

    The stub goes in BEFORE `navigate`, not after: navigating runs the real
    loader for the destination, and several of these kinds need route keys
    this test has no reason to invent (a grid wants `parent_id`). What is
    under test is who ASKS for a load, so the loader is never wanted here.
    """
    b = MpvtkBrowser(app=None, source=FakeSource(),
                     controller=FakeController())
    b._pool = _SyncPool()
    b.server = "srv1"
    loads = []
    b._load_route = lambda route, epoch=None: loads.append(route)
    b.navigate({"kind": kind, "server": server, "item_id": "sh1"})
    del loads[:]                 # the navigation itself is not a refresh
    b._browsing = True
    return b, loads


class WidenedKindsTest(unittest.TestCase):
    def test_a_series_page_re_reads(self):
        """The reported screen: the per-season remaining count."""
        b, loads = _browser("series")
        b.refresh_userdata(now=True)
        self.assertEqual(len(loads), 1)

    def test_a_season_page_re_reads(self):
        b, loads = _browser("season")
        b.refresh_userdata(now=True)
        self.assertEqual(len(loads), 1)

    def test_home_still_does(self):
        """#560 must not regress on the way past."""
        b, loads = _browser("home")
        b.refresh_userdata(now=True)
        self.assertEqual(len(loads), 1)

    def test_a_grid_still_does_not(self):
        """Deliberately out until its refresh-versus-page-in interleaving
        is checked -- a paged screen is where the `_refreshing` marker
        starts mattering."""
        b, loads = _browser("grid")
        b.refresh_userdata(now=True)
        self.assertEqual(loads, [])

    def test_the_kinds_are_disjoint_from_live_tv(self):
        """Two events, two sets. An overlap would mean a timer event and a
        watched-state event both re-reading the same screen, at different
        cadences, through one `_refreshing` marker."""
        self.assertFalse(USERDATA_KINDS & LIVE_KINDS)


class ServerFilterTest(unittest.TestCase):
    def test_another_servers_event_leaves_a_series_page_alone(self):
        b, loads = _browser("series", server="srv1")
        b.refresh_userdata("srv2", now=True)
        self.assertEqual(loads, [])

    def test_its_own_servers_event_re_reads_it(self):
        b, loads = _browser("series", server="srv1")
        b.refresh_userdata("srv1", now=True)
        self.assertEqual(len(loads), 1)

    def test_home_answers_any_server(self):
        """Home is assembled from every logged-in server, so an event from
        any of them can change what it shows."""
        b, loads = _browser("home", server="srv1")
        b.refresh_userdata("srv2", now=True)
        self.assertEqual(len(loads), 1)

    def test_an_unresolved_client_still_refreshes(self):
        """None means "could not tell which server". Refreshing is the old
        behaviour and no worse than it; refusing would make an unresolvable
        client silently stop updating anything."""
        b, loads = _browser("series", server="srv1")
        b.refresh_userdata(None, now=True)
        self.assertEqual(len(loads), 1)


class CoalescedBurstTest(unittest.TestCase):
    """A burst carries one server per event, and the debounce keeps one.

    The refresh became server-FILTERED when #722 widened it past Home. The
    debounce did not follow: the first arrival takes the single slot and
    its server is captured in the closure, so in a two-server session an
    event from B could take the slot, coalesce away A's event three hundred
    milliseconds later, and then filter ITSELF out against the A page on
    screen. The badge stayed stale until something else re-read the page.

    Home never showed this, because Home is exempt from the filter -- which
    is exactly why widening the kinds is what exposed it.
    """

    def _burst(self, page_server, event_servers, kind="series"):
        b, loads = _browser(kind=kind, server=page_server)
        b.USERDATA_DEBOUNCE = 0.01
        for server in event_servers:
            b.refresh_userdata(server)
        thread = b._userdata_thread
        if thread is not None:
            thread.join(timeout=2)
        return loads

    def test_the_relevant_event_survives_an_earlier_irrelevant_one(self):
        loads = self._burst("srv1", ["srv2", "srv1"])
        self.assertEqual(len(loads), 1,
                         "the event for the page on screen was coalesced "
                         "away behind another server's")

    def test_a_burst_for_another_server_alone_still_refreshes_nothing(self):
        """The filter is the point of #722 and must survive the fix."""
        self.assertEqual(self._burst("srv1", ["srv2", "srv2"]), [])

    def test_home_still_refreshes_from_any_server(self):
        """Home is assembled from every logged-in server, so it is exempt
        from the filter and a burst from anywhere has to reach it."""
        self.assertEqual(len(self._burst("srv1", ["srv2"], kind="home")), 1)

    def test_an_unresolved_client_still_refreshes(self):
        """`None` means the event could not be attributed to a server; it
        refreshes unfiltered, as the pre-#722 path did."""
        self.assertEqual(len(self._burst("srv1", ["srv2", None])), 1)


class SharedRulesTest(unittest.TestCase):
    """The rules Live TV had and Home did not, now that both go through one
    implementation. Each of these used to hold for `refresh_live_tv` only."""

    def test_it_marks_the_route_refreshing(self):
        """`pagination` reads this to refuse a page-in against a list about
        to be replaced. Home never set it."""
        b, _loads = _browser("series")
        b.refresh_userdata(now=True)
        self.assertTrue(b.route.get("_refreshing"))

    def test_a_refresh_already_in_flight_is_not_doubled(self):
        b, loads = _browser("series")
        b.route["_refreshing"] = True
        b.refresh_userdata(now=True)
        self.assertEqual(loads, [])

    def test_a_load_in_flight_is_not_doubled(self):
        b, loads = _browser("series")
        b.route["_loading"] = True
        b.refresh_userdata(now=True)
        self.assertEqual(loads, [])

    def test_an_open_menu_defers_it(self):
        b, loads = _browser("series")
        b._menu = {"kind": "history"}
        b.refresh_userdata(now=True)
        self.assertEqual(loads, [])

    def test_an_open_dialog_defers_it(self):
        b, loads = _browser("series")
        b._dialog = lambda: None
        b.refresh_userdata(now=True)
        self.assertEqual(loads, [])

    def test_playback_owning_the_window_defers_it(self):
        b, loads = _browser("series")
        b._browsing = False
        b.refresh_userdata(now=True)
        self.assertEqual(loads, [])

    def test_live_tv_now_gets_the_browsing_guard_too(self):
        """The one rule that moved the other way: `refresh_live_tv` had no
        `_browsing` check, so a websocket event could fetch a guide nobody
        was looking at. Its own poller already declined to, which is what
        made the omission invisible."""
        b, loads = _browser("livetv")
        b._browsing = False
        b.refresh_live_tv()
        self.assertEqual(loads, [])

    def test_live_tv_still_refreshes_when_it_is_up(self):
        b, loads = _browser("livetv")
        b.refresh_live_tv()
        self.assertEqual(len(loads), 1)


class RepeatedEventsTest(unittest.TestCase):
    """The property over several steps, not the mechanics of one.

    Watched state is exactly the shape that feeds back into its own input:
    a refresh re-reads UserData, which the next event is about. A marker
    left set, or an epoch bumped, shows up on the third pass and not the
    first.
    """

    def test_three_rounds_each_re_read_once(self):
        b, loads = _browser("series")
        for round_no in range(1, 4):
            with self.subTest(round=round_no):
                b.refresh_userdata(now=True)
                self.assertEqual(len(loads), round_no,
                                 "round %d did not re-read" % round_no)
                # what _route_async's `always` does when the load settles
                b.route.pop("_refreshing", None)

    def test_the_epoch_does_not_walk(self):
        """A **load, not a reload**: bumping the epoch would cancel work in
        flight and blink a spinner over what is being read."""
        b, _loads = _browser("series")
        before = b._epoch
        for _ in range(3):
            b.refresh_userdata(now=True)
            b.route.pop("_refreshing", None)
        self.assertEqual(b._epoch, before)

    def test_a_stuck_marker_stops_everything_after_it(self):
        """States the cost of the marker so nobody 'simplifies' the
        `always` that clears it: one unreleased marker and the screen never
        refreshes again for the rest of its life."""
        b, loads = _browser("series")
        b.refresh_userdata(now=True)
        self.assertEqual(len(loads), 1)
        for _ in range(3):           # marker deliberately not cleared
            b.refresh_userdata(now=True)
        self.assertEqual(len(loads), 1)


if __name__ == "__main__":
    unittest.main()
