"""Live TV: the guide maths, the queries, the screens and the recording
actions.

Most of what follows pins things that fail *silently* rather than loudly, so
they are worth stating as tests rather than trusting to review:

* times. Guide data is UTC with .NET's seven fractional digits. Every wrong
  way of parsing it produces a plausible datetime that is out by the UTC
  offset, and the only symptom is a guide that is a few hours wrong;
* the window query. ``MaxStartDate``/``MinEndDate`` are asymmetric, and the
  server accepts an offset-less date without shifting it — so a bad query
  answers successfully with the wrong programmes;
* cell geometry. The time header and the cells are laid out by different
  code paths; if they disagree the 20:30 label sits over the 20:00 column,
  which reads as bad guide data rather than as a layout bug;
* the recording-state ladder. A cancelled timer leaves its ``TimerId`` on
  the DTO, so the naive test leaves a "Cancel Recording" button on a
  programme nothing is recording.
"""

import datetime
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import live_tv  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import guide_view  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    CHANNEL_PAGE, LibrarySource)
from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _DeferredPool, _SyncPool, build_scene, ids)

UTC = datetime.timezone.utc


def at(hour, minute=0, day=1):
    """A local aware datetime, for window arithmetic."""
    return datetime.datetime(2026, 7, day, hour, minute).astimezone()


def program(name, start, end, **extra):
    """A guide entry spanning local ``start``..``end`` (hours as floats)."""
    def iso(hours):
        when = at(0) + datetime.timedelta(hours=hours)
        return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    item = {"Id": name, "Name": name, "Type": "Program",
            "StartDate": iso(start), "EndDate": iso(end)}
    item.update(extra)
    return item


class TimeParsing(unittest.TestCase):

    def test_dotnet_ticks_are_accepted(self):
        # Seven fractional digits, which datetime.fromisoformat rejects.
        parsed = live_tv.parse_time("2026-07-28T18:30:00.0000000Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.astimezone(UTC).hour, 18)
        self.assertEqual(parsed.astimezone(UTC).minute, 30)

    def test_a_z_timestamp_is_utc_not_local(self):
        """The failure this exists for: treating the Z as local silently
        slides the whole guide by the UTC offset."""
        parsed = live_tv.parse_time("2026-07-28T18:00:00.0000000Z")
        expected = datetime.datetime(2026, 7, 28, 18, tzinfo=UTC)
        self.assertEqual(parsed, expected)

    def test_an_explicit_offset_is_honoured(self):
        parsed = live_tv.parse_time("2026-07-28T18:00:00-04:00")
        self.assertEqual(parsed,
                         datetime.datetime(2026, 7, 28, 22, tzinfo=UTC))

    def test_the_result_is_in_local_time(self):
        # Aware and local, so comparisons against live_tv.now() work.
        parsed = live_tv.parse_time("2026-07-28T18:00:00Z")
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(),
                         parsed.astimezone().utcoffset())

    def test_junk_is_none_not_an_exception(self):
        for value in (None, "", "yesterday", "2026-13-45T99:99:99Z", 17):
            with self.subTest(value=value):
                self.assertIsNone(live_tv.parse_time(value))

    def test_floor_rounds_down_to_the_half_hour(self):
        # Down, so what is on NOW is in the first column.
        self.assertEqual(live_tv.floor_to_cell(at(10, 29)), at(10, 0))
        self.assertEqual(live_tv.floor_to_cell(at(10, 31)), at(10, 30))
        self.assertEqual(live_tv.floor_to_cell(at(10, 30)), at(10, 30))


class AiringAndLabels(unittest.TestCase):

    def _p(self, minutes_from_now, length=30):
        start = live_tv.now() + datetime.timedelta(minutes=minutes_from_now)
        end = start + datetime.timedelta(minutes=length)
        return {"StartDate": start.astimezone(UTC).isoformat(),
                "EndDate": end.astimezone(UTC).isoformat()}

    def test_airing_now(self):
        self.assertTrue(live_tv.is_airing(self._p(-5)))
        self.assertFalse(live_tv.is_airing(self._p(5)))
        self.assertFalse(live_tv.is_airing(self._p(-90)))

    def test_a_program_with_no_times_is_not_airing(self):
        self.assertFalse(live_tv.is_airing({}))

    def test_progress_is_a_fraction_of_the_slot(self):
        self.assertAlmostEqual(live_tv.program_progress(self._p(-15, 30)),
                               0.5, places=1)
        self.assertEqual(live_tv.program_progress(self._p(30)), 0.0)

    def test_air_time_label_spans_start_to_end(self):
        item = program("x", 20, 21.5)
        start = live_tv.parse_time(item["StartDate"])
        end = live_tv.parse_time(item["EndDate"])
        self.assertEqual(live_tv.air_time_label(item),
                         "%s - %s" % (live_tv.fmt_time(start),
                                      live_tv.fmt_time(end)))

    def test_air_time_label_is_empty_without_a_start(self):
        # A finished recording is an ordinary item; it has no air time.
        self.assertEqual(live_tv.air_time_label({"Name": "Recording"}), "")


class SingleRecordingState(unittest.TestCase):
    """What the Record button asks — a different question from the icon's.

    jellyfin-web's ``recordingfields``: ``TimerId && Status !== 'Cancelled'``,
    which never consults SeriesTimerId.
    """

    def test_nothing_scheduled(self):
        self.assertIsNone(live_tv.single_timer_state({"Type": "Program"}))

    def test_a_scheduled_timer(self):
        self.assertEqual(
            live_tv.single_timer_state({"TimerId": "t1", "Status": "New"}),
            "timer")

    def test_recording_right_now(self):
        self.assertEqual(
            live_tv.single_timer_state({"TimerId": "t1",
                                        "Status": "InProgress"}),
            "recording")

    def test_a_cancelled_timer_is_not_a_timer(self):
        self.assertIsNone(
            live_tv.single_timer_state({"TimerId": "t1",
                                        "Status": "Cancelled"}))

    def test_a_series_rule_does_not_hide_this_showings_own_timer(self):
        """The bug this encodes. Keying the button off timer_state answered
        "series" here, so every episode of a series being recorded offered
        Record — a second timer for a programme that already had one, with
        no way to skip the showing."""
        item = {"TimerId": "t1", "SeriesTimerId": "s1", "Status": "New"}
        self.assertEqual(live_tv.timer_state(item), "series")
        self.assertEqual(live_tv.single_timer_state(item), "timer")

    def test_a_series_recording_in_progress_can_be_stopped(self):
        self.assertEqual(
            live_tv.single_timer_state({"TimerId": "t1",
                                        "SeriesTimerId": "s1",
                                        "Status": "InProgress"}),
            "recording")

    def test_a_rule_that_skipped_this_showing_offers_record(self):
        # The series rule exists but nothing is recording this showing, so
        # the single-recording button is a plain Record.
        self.assertIsNone(live_tv.single_timer_state({"SeriesTimerId": "s1"}))


class RecordingState(unittest.TestCase):
    """The ladder four screens ask their Record ICON about."""

    def test_nothing_scheduled(self):
        self.assertIsNone(live_tv.timer_state({"Type": "Program"}))

    def test_a_scheduled_timer(self):
        self.assertEqual(
            live_tv.timer_state({"TimerId": "t1", "Status": "New"}), "timer")

    def test_recording_right_now(self):
        self.assertEqual(
            live_tv.timer_state({"TimerId": "t1", "Status": "InProgress"}),
            "recording")

    def test_a_cancelled_timer_is_not_a_timer(self):
        """The bug this encodes: the fields survive cancellation, so testing
        TimerId alone leaves "Cancel Recording" on a programme nothing is
        recording. jellyfin-web's guide has exactly that behaviour."""
        self.assertIsNone(
            live_tv.timer_state({"TimerId": "t1", "Status": "Cancelled"}))

    def test_an_active_series_rule(self):
        self.assertEqual(
            live_tv.timer_state({"TimerId": "t1", "SeriesTimerId": "s1",
                                 "Status": "New"}), "series")

    def test_a_series_rule_that_skipped_this_showing(self):
        # Already in the library, or cancelled individually: the rule exists
        # but this showing is not scheduled.
        self.assertEqual(
            live_tv.timer_state({"SeriesTimerId": "s1"}), "series_inactive")

    def test_a_series_timer_item_is_always_a_series(self):
        self.assertEqual(live_tv.timer_state({"Type": "SeriesTimer"}),
                         "series")

    def test_every_state_has_an_icon(self):
        for state in ("timer", "recording", "series", "series_inactive"):
            self.assertIn(state, live_tv.STATE_ICONS)


class Preferences(unittest.TestCase):
    """Shared with jellyfin-web, in jellyfin-web's document."""

    def test_defaults_when_nothing_is_stored(self):
        prefs = live_tv.resolve_prefs({})
        self.assertEqual(prefs["order"], live_tv.ORDER_NUMBER)
        self.assertTrue(prefs["favorites_first"])
        self.assertFalse(prefs["color_coded"])
        self.assertEqual(prefs["indicators"], dict(live_tv.INDICATOR_DEFAULTS))

    def test_values_are_read_as_strings(self):
        # jellyfin-web stores "true"/"false", never JSON booleans.
        prefs = live_tv.resolve_prefs({
            "livetv-channelorder": "DatePlayed",
            "livetv-favoritechannelsattop": "false",
            "guide-colorcodedbackgrounds": "true",
            "guide-indicator-hd": "true"})
        self.assertEqual(prefs["order"], live_tv.ORDER_DATE_PLAYED)
        self.assertFalse(prefs["favorites_first"])
        self.assertTrue(prefs["color_coded"])
        self.assertTrue(prefs["indicators"]["hd"])

    def test_an_unknown_order_falls_back_to_channel_number(self):
        self.assertEqual(
            live_tv.resolve_prefs({"livetv-channelorder": "Nonsense"})["order"],
            live_tv.ORDER_NUMBER)

    def test_round_trip(self):
        prefs = live_tv.resolve_prefs({})
        prefs["color_coded"] = True
        prefs["indicators"]["repeat"] = True
        again = live_tv.resolve_prefs(live_tv.prefs_to_custom(prefs))
        self.assertEqual(again, prefs)

    def test_booleans_are_written_as_strings(self):
        """A JSON boolean here reads as false in jellyfin-web (=== 'true'),
        so the setting would appear to revert whenever the web client was
        opened."""
        out = live_tv.prefs_to_custom(live_tv.resolve_prefs({}))
        for key, value in out.items():
            with self.subTest(key):
                self.assertIsInstance(value, str)

    def test_sort_arguments(self):
        prefs = live_tv.resolve_prefs({"livetv-channelorder": "DatePlayed"})
        kwargs = live_tv.channel_sort_kwargs(prefs)
        self.assertEqual(kwargs["sort_by"], "DatePlayed")
        self.assertEqual(kwargs["sort_order"], "Descending")
        self.assertTrue(kwargs["enable_favorite_sorting"])

    def test_number_order_sends_no_sort(self):
        # The server's own ordering is by channel number; asking for it by
        # name is not a thing this endpoint accepts.
        kwargs = live_tv.channel_sort_kwargs(live_tv.resolve_prefs({}))
        self.assertNotIn("sort_by", kwargs)


class CategoryFilters(unittest.TestCase):

    def test_no_selection_means_no_filter(self):
        self.assertEqual(live_tv.category_kwargs(()), {})

    def test_all_four_also_means_no_filter(self):
        """The server ANDs IsMovie against the tag-backed three, so sending
        all four matches nothing — "everything" has to be *no* flags."""
        self.assertEqual(live_tv.category_kwargs(live_tv.CATEGORIES), {})

    def test_a_subset_becomes_flags(self):
        self.assertEqual(live_tv.category_kwargs(("movies", "news")),
                         {"is_movie": True, "is_news": True})

    def test_every_category_maps_to_a_real_query_parameter(self):
        """"movies" is the one whose flag is not its own name — it is
        IsMovie, singular. Deriving it as "is_" + name gave is_movies, and
        every category-filtered fetch died with a TypeError before it
        reached the server."""
        try:
            import inspect

            from jellyfin_apiclient_python.api import API
        except ImportError:            # pragma: no cover - apiclient absent
            self.skipTest("jellyfin-apiclient-python is not installed")
        for endpoint in (API.get_channels, API.get_programs):
            accepted = set(inspect.signature(endpoint).parameters)
            for name in live_tv.CATEGORIES:
                with self.subTest(endpoint=endpoint.__name__, category=name):
                    self.assertIn(live_tv.CATEGORY_FLAGS[name], accepted)

    def test_unknown_names_are_ignored(self):
        self.assertEqual(live_tv.category_kwargs(("opera",)), {})


class CategoryDisplay(unittest.TestCase):
    """Guide cells are filtered by drawing, not by the query."""

    def test_no_filter_shows_everything(self):
        self.assertTrue(live_tv.program_displayed({"IsMovie": True}, ()))
        self.assertTrue(live_tv.program_displayed({}, ()))
        self.assertTrue(live_tv.program_displayed({}, live_tv.CATEGORIES))

    def test_a_selected_category_is_shown(self):
        self.assertTrue(
            live_tv.program_displayed({"IsMovie": True}, ("movies",)))

    def test_an_unselected_category_is_hidden(self):
        self.assertFalse(
            live_tv.program_displayed({"IsMovie": True}, ("news",)))

    def test_the_ladder_is_first_match_wins(self):
        """A kids' sports programme is governed by the Kids checkbox, like
        jellyfin-web's displayInnerContent."""
        kids_sports = {"IsKids": True, "IsSports": True}
        self.assertTrue(live_tv.program_displayed(kids_sports, ("kids",)))
        self.assertFalse(live_tv.program_displayed(kids_sports, ("sports",)))

    def test_uncategorised_content_is_hidden_while_a_filter_is_on(self):
        """jellyfin-web only ever grants its "series" pseudo-category when
        all four boxes are ticked, i.e. when there is no filter at all."""
        for item in ({"IsSeries": True}, {}):
            with self.subTest(item=item):
                self.assertFalse(live_tv.program_displayed(item, ("news",)))


class Indicators(unittest.TestCase):

    def test_live_wins_over_premiere(self):
        badge = live_tv.program_indicators(
            {"IsLive": True, "IsPremiere": True},
            {"live": True, "premiere": True})
        self.assertEqual(badge, "Live")

    def test_a_disabled_indicator_is_not_drawn(self):
        self.assertEqual(
            live_tv.program_indicators({"IsLive": True}, {"live": False}), "")

    def test_a_new_episode_is_a_series_that_is_not_a_repeat(self):
        self.assertEqual(
            live_tv.program_indicators({"IsSeries": True}, {"new": True}),
            "New")
        self.assertEqual(
            live_tv.program_indicators({"IsSeries": True, "IsRepeat": True},
                                       {"new": True}), "")

    def test_categories_are_checked_in_order(self):
        # A kids' sports programme reads as kids, like jellyfin-web.
        self.assertEqual(
            live_tv.program_category({"IsKids": True, "IsSports": True}),
            "kids")
        self.assertIsNone(live_tv.program_category({}))


class RowLayout(unittest.TestCase):
    """The guide's cell geometry."""

    WIDTH = 800

    def segments(self, programs, start=at(20), end=at(22)):
        return live_tv.row_segments(programs, start, end, self.WIDTH)

    def test_the_row_spans_exactly_the_width(self):
        """Off-by-one here leaves the last cell short of the edge, which on
        a row of fixed-width boxes reads as a ragged margin."""
        for programs in ([], [program("a", 20, 21)],
                         [program("a", 20, 20.5), program("b", 21, 22)],
                         [program("a", 19, 23)]):
            with self.subTest(n=len(programs)):
                total = sum(w for _p, w in self.segments(programs))
                self.assertEqual(total, self.WIDTH)

    def test_a_full_window_programme_takes_the_whole_row(self):
        segments = self.segments([program("a", 19, 23)])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][1], self.WIDTH)

    def test_a_half_window_programme_takes_half(self):
        segments = self.segments([program("a", 20, 21)])
        self.assertEqual([w for _p, w in segments], [400, 400])
        self.assertIsNotNone(segments[0][0])
        self.assertIsNone(segments[1][0], "the empty half must be dead air")

    def test_gaps_become_dead_air(self):
        segments = self.segments([program("a", 20, 20.5),
                                  program("b", 21.5, 22)])
        self.assertEqual([p is None for p, _w in segments],
                         [False, True, False])

    def test_programmes_outside_the_window_are_dropped(self):
        segments = self.segments([program("a", 10, 11), program("b", 23, 24)])
        self.assertEqual(segments, [(None, self.WIDTH)])

    def test_overlapping_data_does_not_produce_a_negative_cell(self):
        # Providers round; a later programme swallowed by an earlier one is
        # dropped rather than drawn at zero (or negative) width.
        segments = self.segments([program("a", 20, 21.5),
                                  program("b", 20.5, 21)])
        self.assertTrue(all(w > 0 for _p, w in segments))
        self.assertEqual(sum(w for _p, w in segments), self.WIDTH)

    def test_input_order_does_not_matter(self):
        forward = self.segments([program("a", 20, 21), program("b", 21, 22)])
        reverse = self.segments([program("b", 21, 22), program("a", 20, 21)])
        self.assertEqual([(p or {}).get("Id") for p, _w in forward],
                         [(p or {}).get("Id") for p, _w in reverse])

    def test_a_programme_without_times_is_skipped(self):
        self.assertEqual(self.segments([{"Id": "x", "Name": "x"}]),
                         [(None, self.WIDTH)])

    def test_timeslots_are_half_hourly(self):
        slots = live_tv.timeslots(at(20), at(22))
        self.assertEqual(slots, [at(20), at(20, 30), at(21), at(21, 30)])

    def test_the_header_and_the_cells_agree(self):
        """Both are laid out from the same window, so a 2-hour window at
        800px has its 21:00 boundary at 400px in both."""
        header = guide_view.time_header(at(20), at(22), self.WIDTH)
        edges, x = [], 0
        for cell in header.children:
            x += cell.w
            edges.append(x)
        segments = self.segments([program("a", 20, 21), program("b", 21, 22)])
        self.assertIn(segments[0][1], edges)


class WindowSizing(unittest.TestCase):

    def test_columns_follow_the_width(self):
        self.assertEqual(live_tv.cells_for_width(600, min_cell_w=150), 4)
        self.assertEqual(live_tv.cells_for_width(320, min_cell_w=150), 2)

    def test_it_never_goes_below_an_hour_or_above_four(self):
        self.assertEqual(live_tv.cells_for_width(10), live_tv.MIN_CELLS)
        self.assertEqual(live_tv.cells_for_width(100000), live_tv.MAX_CELLS)

    def test_clamping_to_the_guide_range(self):
        info = {"StartDate": at(0).astimezone(UTC).isoformat(),
                "EndDate": at(0, day=3).astimezone(UTC).isoformat()}
        self.assertEqual(live_tv.clamp_window(at(6, day=1) - datetime.timedelta(days=5),
                                              info, 4),
                         at(0, day=1))
        # Past the end: pulled back so the window still shows data.
        far = live_tv.clamp_window(at(0, day=9), info, 4)
        self.assertLess(far, at(0, day=3))

    def test_no_guide_info_imposes_no_limit(self):
        self.assertEqual(live_tv.clamp_window(at(6, day=9), {}, 4),
                         at(6, day=9))


class RecordingApi(unittest.TestCase):
    """The queries. A wrong one here answers successfully with the wrong
    data, which is why they are asserted rather than reviewed."""

    class Api:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def record(*a, **kw):
                self.calls.append((name, a, kw))
                return {"Items": []}
            return record

    def source(self):
        api = self.Api()
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        src._live_tv_prefs = {}
        return src, api

    def kwargs(self, api, name):
        return next(kw for call, _a, kw in api.calls if call == name)

    def test_the_guide_window_goes_out_as_utc_with_a_z(self):
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22))
        kw = self.kwargs(api, "get_programs")
        for key in ("max_start_date", "min_end_date"):
            with self.subTest(key):
                self.assertTrue(kw[key].endswith("Z"), kw[key])

    def test_the_window_bounds_are_asymmetric(self):
        """MaxStartDate is the window END and MinEndDate its START — which is
        what includes a programme that began before the window opened."""
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22))
        kw = self.kwargs(api, "get_programs")
        self.assertGreater(kw["max_start_date"], kw["min_end_date"])

    def test_the_guide_asks_for_channel_info(self):
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22))
        self.assertIn("ChannelInfo", self.kwargs(api, "get_programs")["fields"])

    def test_program_lists_ask_for_the_channel_logo_as_well(self):
        """ChannelInfo alone gets the channel's NAME. Its logo is gated on
        ChannelImage — AddInfoToProgramDto only sets ChannelPrimaryImageTag
        under hasChannelImage — and that logo is the whole artwork fallback
        for guide data, so asking for only the first turns every listing
        row into a wall of letter glyphs."""
        src, api = self.source()
        src.get_programs("srv")
        src.get_recommended_programs("srv")
        src.search_live_tv("srv", "x")
        for call, _a, kw in api.calls:
            if "fields" in kw and "ChannelInfo" in (kw["fields"] or ""):
                with self.subTest(call=call):
                    self.assertIn("ChannelImage", kw["fields"])

    def test_a_paged_list_asks_for_the_page_it_was_given(self):
        """The "see all" behind a Programs or Recordings row is paginated,
        and these two branches of get_list took the start index and dropped
        it — so page 2 re-fetched page 1, and reporting ``len(items)`` as
        the total then shrank the page count back to one. A 40-programme
        listing rendered as its first twelve, labelled "1 / 1", with the
        rest unreachable.

        Asserted against the REAL source, because the bug was the source
        being less correct than the fake standing in for it: the harness's
        FakeSource.get_list slices by start_index, so every ListPage test
        exercised a source that did not have the bug.
        """
        for kind, call in (("programs", "get_programs"),
                           ("recordings", "get_live_tv_recordings")):
            with self.subTest(kind):
                src, api = self.source()
                src.get_list("srv", {"type": kind}, start_index=24, limit=12)
                kw = self.kwargs(api, call)
                self.assertEqual(kw["start_index"], 24)
                self.assertEqual(kw["limit"], 12)
                self.assertTrue(kw["enable_total_record_count"],
                                "without a count the paginator cannot page")

    def test_a_paged_list_reports_the_servers_total(self):
        src, api = self.source()
        api.calls = []
        src._conn = lambda _uuid: type("C", (), {"api": type("A", (), {
            "get_programs": staticmethod(
                lambda **kw: {"Items": [{"Id": "p"}], "TotalRecordCount": 40}),
        })()})()
        _items, total = src.get_list("srv", {"type": "programs"},
                                     start_index=0, limit=12)
        self.assertEqual(total, 40, "reported the page as the whole list")

    def test_a_row_still_pays_for_no_count(self):
        """The twelve-item rows never draw a total, and asking for one is a
        second, wider query server-side. Only the paged form buys it."""
        src, api = self.source()
        src.get_programs("srv")
        src.get_recordings("srv")
        for call in ("get_programs", "get_live_tv_recordings"):
            with self.subTest(call):
                self.assertFalse(self.kwargs(api, call)
                                 ["enable_total_record_count"])

    def test_the_guide_does_not_pay_for_a_logo_it_cannot_draw(self):
        # A guide cell is text; the channel column's art comes off the
        # channel DTO. Asking for the tag would cost a channel lookup per
        # programme across the whole window.
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22))
        self.assertNotIn("ChannelImage",
                         self.kwargs(api, "get_programs")["fields"])

    def test_the_hd_field_is_only_requested_when_it_is_drawn(self):
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22), want_hd=False)
        self.assertNotIn("IsHD", self.kwargs(api, "get_programs")["fields"])
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22), want_hd=True)
        self.assertIn("IsHD", self.kwargs(api, "get_programs")["fields"])

    def test_an_empty_channel_set_issues_no_request(self):
        src, api = self.source()
        self.assertEqual(src.get_guide("srv", [], at(20), at(22)), [])
        self.assertEqual(api.calls, [])

    def test_the_guide_fetch_carries_no_category_flags(self):
        """The categories filter the channel list and the drawing, never
        this query: the server ANDs IsMovie against the tag-backed three, so
        a two-category guide came back empty."""
        src, api = self.source()
        src.get_guide("srv", ["c1"], at(20), at(22))
        kw = self.kwargs(api, "get_programs")
        for key in ("is_movie", "is_sports", "is_kids", "is_news"):
            self.assertNotIn(key, kw)

    def test_the_channel_list_does_carry_them(self):
        src, api = self.source()
        src.get_channels("srv", categories=("movies",))
        self.assertEqual(self.kwargs(api, "get_channels")["is_movie"], True)

    def test_channels_are_paged(self):
        src, api = self.source()
        src.get_channels("srv")
        self.assertEqual(self.kwargs(api, "get_channels")["limit"],
                         CHANNEL_PAGE)

    def test_channel_sorting_comes_from_the_prefs(self):
        src, api = self.source()
        src.get_channels("srv",
                         prefs=live_tv.resolve_prefs(
                             {"livetv-channelorder": "DatePlayed"}))
        self.assertEqual(self.kwargs(api, "get_channels")["sort_by"],
                         "DatePlayed")

    def test_timers_come_back_in_start_order(self):
        src, api = self.source()
        api.get_live_tv_timers = lambda **kw: {"Items": [
            {"Id": "b", "StartDate": "2026-07-28T20:00:00Z"},
            {"Id": "a", "StartDate": "2026-07-28T18:00:00Z"}]}
        self.assertEqual([t["Id"] for t in src.get_timers("srv")], ["a", "b"])

    def test_saving_prefs_keeps_the_rest_of_the_document(self):
        """The DisplayPreferences document also holds the home layout and
        jellyfin-web's landing screens; there is no partial-update path, so
        a save that sent only our keys would drop all of it."""
        src, api = self.source()
        api.get_user_settings = lambda client=None: {
            "Id": "usersettings", "Client": "emby",
            "CustomPrefs": {"homesection0": "resume", "landing-movies": "x"}}
        written = {}
        api.update_user_settings = lambda dto, client=None: written.update(dto)
        src.save_live_tv_prefs("srv", live_tv.resolve_prefs({}))
        custom = written["CustomPrefs"]
        self.assertEqual(custom["homesection0"], "resume")
        self.assertEqual(custom["landing-movies"], "x")
        self.assertIn(live_tv.CHANNEL_ORDER_KEY, custom)

    def test_saving_prefs_updates_the_cache_before_it_writes(self):
        src, api = self.source()
        api.get_user_settings = lambda client=None: {"CustomPrefs": {}}
        seen = []
        wanted = dict(live_tv.resolve_prefs({}), color_coded=True)

        def update(dto, client=None):
            seen.append(src.get_live_tv_prefs("srv"))
        api.update_user_settings = update
        src.save_live_tv_prefs("srv", wanted)
        self.assertTrue(seen[0]["color_coded"])

    def test_the_cache_can_be_moved_without_any_io(self):
        """The loop thread's half of a save. It has to touch nothing on the
        wire: it runs on the render loop, where a round trip is a freeze."""
        src, api = self.source()
        api.get_user_settings = lambda client=None: {"CustomPrefs": {}}
        src.get_live_tv_prefs("srv")
        api.calls.clear()
        wanted = dict(live_tv.resolve_prefs({}), color_coded=True)
        src.cache_live_tv_prefs("srv", wanted)
        self.assertEqual(api.calls, [])
        self.assertTrue(src.get_live_tv_prefs("srv")["color_coded"])

    def test_a_failed_save_rolls_the_cache_back(self):
        src, api = self.source()
        api.get_user_settings = lambda client=None: {"CustomPrefs": {}}
        before = src.get_live_tv_prefs("srv")

        def boom(dto, client=None):
            raise OSError("down")
        api.update_user_settings = boom
        with self.assertRaises(OSError):
            src.save_live_tv_prefs(
                "srv", dict(before, color_coded=not before["color_coded"]))
        self.assertEqual(src.get_live_tv_prefs("srv"), before)

    def test_prefs_are_cached_after_the_first_read(self):
        src, api = self.source()
        reads = []
        api.get_user_settings = lambda client=None: (
            reads.append(client) or {"CustomPrefs": {}})
        src.get_live_tv_prefs("srv")
        src.get_live_tv_prefs("srv")
        self.assertEqual(len(reads), 1)

    def test_a_refresh_re_reads(self):
        src, api = self.source()
        reads = []
        api.get_user_settings = lambda client=None: (
            reads.append(client) or {"CustomPrefs": {}})
        src.get_live_tv_prefs("srv")
        src.get_live_tv_prefs("srv", refresh=True)
        self.assertEqual(len(reads), 2)

    def test_unreadable_prefs_fall_back_to_defaults(self):
        src, api = self.source()

        def boom(client=None):
            raise OSError("down")
        api.get_user_settings = boom
        self.assertEqual(src.get_live_tv_prefs("srv"),
                         live_tv.resolve_prefs({}))

    def test_program_sections_drop_empty_rows(self):
        src, api = self.source()
        seen = []

        def programs(**kw):
            seen.append(kw)
            return {"Items": [{"Id": "p"}] if kw.get("is_movie") else []}
        api.get_programs = programs
        api.get_recommended_programs = lambda **kw: {"Items": []}
        rows = src.get_program_sections("srv")
        self.assertEqual([r["key"] for r in rows], ["movies"])

    def test_every_program_query_is_one_the_apiclient_accepts(self):
        """The failure this exists for: ``has_aired`` was not a parameter of
        ``get_programs``, so five of the six Programs rows raised TypeError
        inside the fan-out, were logged and dropped — and the screen showed
        only "On Now", looking like a server with no upcoming listings.

        Checked against the real signature, because the fake below accepts
        anything and a fan-out that swallows failures cannot report this.
        """
        import inspect

        from jellyfin_apiclient_python.api import API

        allowed = set(inspect.signature(API.get_programs).parameters)
        for key, _title, query in LibrarySource.PROGRAM_SECTIONS:
            if key == "onnow":
                allowed_here = set(
                    inspect.signature(API.get_recommended_programs).parameters)
            else:
                allowed_here = allowed
            with self.subTest(key):
                self.assertLessEqual(set(query), allowed_here)

    def test_upcoming_rows_ask_for_what_has_not_aired(self):
        src, api = self.source()
        captured = []
        api.get_programs = lambda **kw: captured.append(kw) or {"Items": []}
        api.get_recommended_programs = lambda **kw: {"Items": []}
        src.get_program_sections("srv")
        self.assertTrue(captured)
        for kw in captured:
            with self.subTest(kw=sorted(kw)):
                self.assertIs(kw["has_aired"], False)

    def test_upcoming_episodes_excludes_the_other_categories(self):
        src, api = self.source()
        captured = []
        api.get_programs = lambda **kw: captured.append(kw) or {"Items": []}
        api.get_recommended_programs = lambda **kw: {"Items": []}
        src.get_program_sections("srv")
        episodes = next(kw for kw in captured if kw.get("is_series"))
        for key in ("is_movie", "is_sports", "is_kids", "is_news"):
            with self.subTest(key):
                self.assertIs(episodes[key], False)


def browser(source=None, controller=None):
    b = MpvtkBrowser(app=None, source=source or FakeSource(),
                     controller=controller or FakeController())
    b._pool = _SyncPool()
    b.server = "srv1"
    return b


def open_live_tv(b, tab="programs"):
    b.navigate({"kind": "livetv", "server": "srv1", "title": "Live TV",
                "_tab": tab})
    return b._page_for(b.route)


class Screens(unittest.TestCase):
    """Every tab draws, and draws the thing it is for."""

    def setUp(self):
        self.b = browser()

    def _scene(self, tab):
        open_live_tv(self.b, tab)
        return build_scene(self.b, (1280, 720))

    def test_every_tab_renders(self):
        for tab, _label in self.b._page_for(
                {"kind": "livetv"}).TABS:
            with self.subTest(tab=tab):
                nodes, _h = self._scene(tab)
                self.assertTrue(nodes)

    def test_the_tab_bar_is_always_present(self):
        nodes, _h = self._scene("guide")
        self.assertIn("lttab-guide", ids(nodes))
        self.assertIn("lttab-recordings", ids(nodes))

    def test_the_guide_offers_its_window_controls(self):
        nodes, _h = self._scene("guide")
        for node_id in ("lt-prevday", "lt-prevwin", "lt-nextwin", "lt-nextday",
                        "lt-now", "lt-guidecfg"):
            with self.subTest(node_id):
                self.assertIn(node_id, ids(nodes))

    def test_channel_paging_only_appears_when_there_is_more_than_a_page(self):
        nodes, _h = self._scene("guide")
        self.assertNotIn("lt-channext", ids(nodes),
                         "three channels do not need paging controls")

    def test_the_guide_starts_at_the_current_half_hour(self):
        page = open_live_tv(self.b, "guide")
        self.assertEqual(page.route["_start"],
                         live_tv.floor_to_cell(live_tv.now()))

    def test_moving_the_window_refetches(self):
        page = open_live_tv(self.b, "guide")
        first = page.route["_start"]
        page._move_window(datetime.timedelta(hours=1), {})
        self.assertEqual(page.route["_start"],
                         first + datetime.timedelta(hours=1))
        self.assertEqual(page.route["_data"]["start"], page.route["_start"])

    def test_the_guide_range_is_read_once(self):
        # It bounds the arrows and does not move while you look at it, so
        # paging must not cost a GuideInfo round trip per press.
        reads = []
        self.b.source.get_guide_info = lambda srv: reads.append(srv) or {}
        page = open_live_tv(self.b, "guide")
        page._move_window(datetime.timedelta(hours=1), {})
        page._move_window(datetime.timedelta(hours=1), {})
        self.assertEqual(len(reads), 1)

    def test_now_jumps_back(self):
        page = open_live_tv(self.b, "guide")
        page._move_window(datetime.timedelta(days=1), {})
        page._jump_to_now()
        self.assertEqual(page.route["_start"],
                         live_tv.floor_to_cell(live_tv.now()))

    def test_the_guide_fetch_covers_the_widest_window(self):
        """A narrower screen must not mean a narrower *fetch*, or every
        resize is a new guide request — including one from inside build()."""
        page = open_live_tv(self.b, "guide")
        data = page.route["_data"]
        self.assertEqual(
            data["end"] - data["start"],
            datetime.timedelta(minutes=live_tv.CELL_MINUTES
                               * live_tv.MAX_CELLS))

    def test_switching_tabs_caches_what_was_loaded(self):
        page = open_live_tv(self.b, "programs")
        loaded = page.route["_data"]
        page._set_tab("channels")
        page._set_tab("programs")
        self.assertEqual(page.route["_data"], loaded)

    def test_switching_to_a_new_tab_loads_it(self):
        page = open_live_tv(self.b, "programs")
        page._set_tab("series")
        self.assertEqual([t["Id"] for t in page.route["_data"]], ["st1"])

    def test_the_schedule_groups_timers_by_day(self):
        page = open_live_tv(self.b, "schedule")
        groups = page._group_by_day(page.route["_data"]["timers"])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0][0])

    def test_a_channel_tile_opens_the_channel_page(self):
        plays = []
        self.b.controller.play_list = lambda ids_, srv, i, **kw: plays.append(
            list(ids_))
        self.b._open_item({"Id": "c1", "Type": "TvChannel", "Name": "One"})
        self.assertEqual(self.b.route["kind"], "channel")
        self.assertEqual(plays, [])

    def test_a_guide_cell_opens_the_program_page(self):
        page = open_live_tv(self.b, "guide")
        page._open_program({"Id": "pr9", "Name": "Thing", "ChannelId": "c1"})
        self.assertEqual(self.b.route["kind"], "program")
        self.assertEqual(self.b.route["channel_id"], "c1")

    def _guide_cells(self):
        """The scene's guide cells, i.e. the right-clickable ones."""
        nodes, handlers = build_scene(self.b, (1280, 720))
        cells = [(n, handlers[n["id"]]) for n in nodes
                 if n.get("ctx") and n.get("sc") == "livetv-guide"]
        return nodes, cells

    def test_a_guide_cell_offers_the_recording_menu(self):
        """Recording from the guide is what a guide is for; without this the
        only way to set a timer is to open each programme in turn."""
        open_live_tv(self.b, "guide")
        _nodes, cells = self._guide_cells()
        self.assertTrue(cells, "no guide cell has a context menu")
        cells[0][1]["context"](400, 300)
        self.assertIsNotNone(self.b._menu)
        self.assertIn("tilemenu", ids(build_scene(self.b, (1280, 720))[0]))

    def test_a_filtered_out_cell_keeps_its_place_but_says_nothing(self):
        """jellyfin-web's displayInnerContent: the cell stays so the row is
        still a grid, and only its text goes. Filtering the *fetch* instead
        left dead air where the programmes were — and, because the server
        ANDs IsMovie against the other three, emptied the guide outright as
        soon as two categories were picked."""
        page = open_live_tv(self.b, "guide")
        _nodes, before = self._guide_cells()
        self.assertTrue(before)
        # The fixture's programmes are plain series, which jellyfin-web
        # hides whenever any category filter is on.
        page.route["_categories"] = ("news",)
        page._reload_tab()
        nodes, after = self._guide_cells()
        self.assertEqual(len(after), len(before),
                         "a filtered guide should keep its cells")
        self.assertNotIn("Program 0",
                         [n.get("text") for n in nodes if n.get("text")])


class SectionSnapping(unittest.TestCase):
    """The stacked-carousel tabs snap to section tops, like the home screen.

    Programs is six carousels and Schedule is one per day, each a composited
    strip — so a continuous offset repositions every visible strip on every
    frame, and it is easy to land with a caption band across the top of the
    window.
    """

    #: Rows whose artwork makes them different heights, which is the case a
    #: uniform snap pitch cannot serve.
    MIXED = (("onnow", 1.777), ("movies", 0.666), ("sports", 1.777))

    def _browser(self):
        source = FakeSource()
        source.get_program_sections = lambda srv, limit=12: [
            {"key": key, "title": key.title(),
             "items": [{"Id": "%s%d" % (key, i), "Type": "Program",
                        "Name": key, "PrimaryImageAspectRatio": ratio}
                       for i in range(6)]}
            for key, ratio in self.MIXED]
        b = browser(source=source)
        return b

    def _scroll_node(self, b, node_id):
        nodes, _h = build_scene(b, (1280, 720))
        return next(n for n in nodes if n.get("id") == node_id)

    def test_the_programs_tab_snaps_to_its_sections(self):
        b = self._browser()
        open_live_tv(b, "programs")
        snaps = self._scroll_node(b, "livetv-programs").get("snaps")
        self.assertEqual(len(snaps), len(self.MIXED))

    def test_the_first_stop_is_the_top(self):
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        b = self._browser()
        open_live_tv(b, "programs")
        snaps = self._scroll_node(b, "livetv-programs")["snaps"]
        self.assertEqual(snaps[0], chrome.CONTENT_PAD)

    def test_the_stops_are_where_the_sections_actually_start(self):
        """Measured, not stepped: an auto-shaped poster row is half again as
        tall as a landscape one, so a uniform pitch drifts out of alignment
        within two sections."""
        b = self._browser()
        page = open_live_tv(b, "programs")
        snaps = self._scroll_node(b, "livetv-programs")["snaps"]
        gaps = [b - a for a, b in zip(snaps, snaps[1:])]
        self.assertNotEqual(len(set(gaps)), 1,
                            "mixed-shape rows should not be evenly spaced")
        # And they agree with what the page laid out.
        from jellyfin_mpv_shim.mpvtk_browser import components
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        rows = [page._auto_row(row["title"], row["items"], "x-" + row["key"])
                for row in page.route["_data"]]
        self.assertEqual(
            snaps,
            components.section_offsets(rows, 16, pad=chrome.CONTENT_PAD))

    def test_the_schedule_and_recordings_tabs_snap_too(self):
        # Same shape of content — a stack of carousels — and Schedule grows
        # a section per day.
        for tab, node_id in (("recordings", "livetv-recordings"),
                             ("schedule", "livetv-schedule")):
            with self.subTest(tab):
                b = browser()
                open_live_tv(b, tab)
                self.assertTrue(self._scroll_node(b, node_id).get("snaps"))

    def test_the_guide_is_not_section_snapped(self):
        # Its rows are uniform, so it keeps the cheaper row-pitch snap.
        b = browser()
        open_live_tv(b, "guide")
        node = self._scroll_node(b, "livetv-guide")
        self.assertIsNone(node.get("snaps"))
        self.assertTrue(node.get("snap"))


class ProgramScreen(unittest.TestCase):

    def setUp(self):
        self.b = browser()

    def _open(self, item=None):
        source = self.b.source
        if item is not None:
            source.get_live_program = lambda srv, pid: item
        self.b.navigate({"kind": "program", "server": "srv1",
                         "item_id": "pr1", "title": "Thing"})
        return self.b._page_for(self.b.route)

    def _ids(self):
        nodes, _h = build_scene(self.b, (1280, 720))
        return ids(nodes)

    def test_it_renders(self):
        self._open()
        self.assertIn("pg-watch", self._ids())

    def test_an_unscheduled_programme_offers_record(self):
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1"})
        found = self._ids()
        self.assertIn("pg-record", found)
        self.assertNotIn("pg-cancel", found)

    def test_a_scheduled_programme_offers_cancel(self):
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1", "TimerId": "t1", "Status": "New"})
        found = self._ids()
        self.assertIn("pg-cancel", found)
        self.assertNotIn("pg-record", found)

    def test_a_series_offers_the_series_buttons(self):
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1", "IsSeries": True})
        self.assertIn("pg-recseries", self._ids())

    def test_an_active_series_rule_offers_its_options(self):
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1", "IsSeries": True,
                    "SeriesTimerId": "s1", "TimerId": "t1", "Status": "New"})
        found = self._ids()
        self.assertIn("pg-cancelseries", found)
        self.assertIn("pg-seriesopts", found)

    def test_a_showing_covered_by_a_series_rule_can_still_be_skipped(self):
        """Its own timer and the series rule are separate facts, and the
        page has to offer both. Keying the single-recording button off
        timer_state answered "series" here, so the page showed Record for a
        programme that already had a timer."""
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1", "IsSeries": True,
                    "SeriesTimerId": "s1", "TimerId": "t1", "Status": "New"})
        found = self._ids()
        self.assertIn("pg-cancel", found)
        self.assertIn("pg-cancelseries", found)
        self.assertNotIn("pg-record", found)

    def test_a_series_recording_in_progress_can_be_stopped(self):
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1", "IsSeries": True,
                    "SeriesTimerId": "s1", "TimerId": "t1",
                    "Status": "InProgress"})
        nodes, _h = build_scene(self.b, (1280, 720))
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("Stop Recording", texts)

    def test_a_rule_that_skipped_this_showing_offers_record(self):
        self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                    "ChannelId": "c1", "IsSeries": True,
                    "SeriesTimerId": "s1"})
        found = self._ids()
        self.assertIn("pg-record", found)
        self.assertNotIn("pg-cancel", found)

    def test_a_non_series_gets_no_series_buttons(self):
        self._open({"Id": "pr1", "Name": "Film", "Type": "Program",
                    "ChannelId": "c1"})
        self.assertNotIn("pg-recseries", self._ids())

    def test_watch_tunes_the_channel_not_the_programme(self):
        plays = []
        self.b.controller.play_list = lambda ids_, srv, i, **kw: plays.append(
            list(ids_))
        page = self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                           "ChannelId": "c9"})
        page._buttons(page.route["_data"]).children[0].on_click()
        self.assertEqual(plays, [["c9"]])

    def test_the_seed_draws_before_the_fetch_lands(self):
        """Clicking a tile must not show a spinner for a programme whose DTO
        the caller already had."""
        from tests._shell_harness import _NeverPool

        self.b._pool = _NeverPool()
        self.b.navigate({"kind": "program", "server": "srv1",
                         "item_id": "pr1", "title": "Thing",
                         "_seed": {"Id": "pr1", "Name": "Seeded",
                                   "Type": "Program"}})
        self.assertEqual(self.b.route["_data"]["Name"], "Seeded")

    def test_recording_actions_reach_the_gateway(self):
        calls = []
        self.b.controller.create_timer = lambda srv, pid: calls.append(
            ("timer", srv, pid))
        page = self._open({"Id": "pr1", "Name": "Thing", "Type": "Program",
                           "ChannelId": "c1"})
        self.b._actions.schedule_recording(page.route["_data"], "srv1")
        self.assertEqual(calls, [("timer", "srv1", "pr1")])

    def test_scheduling_a_series_uses_the_series_endpoint(self):
        calls = []
        self.b.controller.create_series_timer = lambda srv, pid: calls.append(
            pid)
        self.b._actions.schedule_recording(
            {"Id": "pr1", "IsSeries": True}, "srv1", series=True)
        self.assertEqual(calls, ["pr1"])

    def test_cancelling_asks_first(self):
        """Destructive and one stray click from Record, so it confirms —
        the same treatment removing a download gets."""
        calls = []
        self.b.controller.cancel_timer = lambda srv, tid: calls.append(tid)
        self.b._actions.cancel_timer("t1", "srv1")
        self.assertEqual(calls, [], "cancelled without confirming")
        self.assertIsNotNone(self.b._dialog)


class Dialogs(unittest.TestCase):

    def setUp(self):
        self.b = browser()

    def _ids(self):
        nodes, _h = build_scene(self.b, (1280, 720))
        return ids(nodes)

    def test_the_guide_settings_dialog_renders(self):
        page = open_live_tv(self.b, "guide")
        page._open_guide_settings()
        found = self._ids()
        self.assertIn("gs-order", found)
        self.assertIn("gs-ind-live", found)
        self.assertIn("gs-cat-movies", found)

    def test_saving_guide_settings_persists_and_applies(self):
        page = open_live_tv(self.b, "guide")
        page._open_guide_settings()
        self.b._guide_set("color_coded", True)
        self.b._guide_save()
        self.assertTrue(self.b.source.saved_live_tv_prefs["color_coded"])
        self.assertTrue(page.route["_prefs"]["color_coded"])
        self.assertIsNone(self.b._dialog, "the dialog stayed open after Save")

    def test_the_new_prefs_are_cached_before_the_guide_reloads(self):
        """The race this exists for. Saving repaints the guide, and
        repainting it re-fetches it — a pool job whose first act is
        get_live_tv_prefs, submitted BEFORE the save job. Adopting the new
        prefs anywhere inside the save (however early) loses, and the guide
        comes back drawn with the settings just changed away from.

        Asserted as an ordering rather than by racing threads: the shell's
        test pool is synchronous, so the two jobs would never overlap here
        even when the ordering is wrong.
        """
        source = self.b.source
        order = []
        source.cache_live_tv_prefs = lambda srv, prefs: order.append("cache")
        real_read = source.get_live_tv_prefs
        source.get_live_tv_prefs = lambda srv, refresh=False: (
            order.append("read") or real_read(srv))
        page = open_live_tv(self.b, "guide")
        order.clear()
        page._open_guide_settings()
        self.b._guide_set("color_coded", True)
        self.b._guide_save()
        self.assertIn("read", order, "the guide did not reload")
        self.assertEqual(order[0], "cache",
                         "the reload read the prefs before the save "
                         "published them")

    def test_deselecting_a_category_starts_from_all_of_them(self):
        # The empty set means "everything", so the boxes show all ticked;
        # un-ticking one from that state must leave the other three.
        page = open_live_tv(self.b, "guide")
        page._open_guide_settings()
        self.b._guide_toggle_category("news")
        self.b._guide_save()
        self.assertEqual(set(page.route["_categories"]),
                         set(live_tv.CATEGORIES) - {"news"})

    def test_selecting_every_category_is_stored_as_no_filter(self):
        page = open_live_tv(self.b, "guide")
        page._open_guide_settings()
        self.b._guide_toggle_category("news")   # -> three selected
        self.b._guide_toggle_category("news")   # -> back to all four
        self.b._guide_save()
        self.assertEqual(page.route["_categories"], ())

    def test_the_timer_editor_renders_a_series_rule(self):
        page = open_live_tv(self.b, "series")
        page._open_series_timer({"Id": "st1"})
        found = self._ids()
        for node_id in ("tm-showtype", "tm-channels", "tm-airtime", "tm-keep",
                        "tm-pre", "tm-post", "tm-save", "tm-cancel"):
            with self.subTest(node_id):
                self.assertIn(node_id, found)

    def test_a_single_timer_has_no_series_controls(self):
        page = open_live_tv(self.b, "schedule")
        page._open_timer({"Id": "tm1"})
        found = self._ids()
        self.assertIn("tm-pre", found)
        self.assertNotIn("tm-showtype", found)

    def test_saving_a_timer_sends_minutes_as_seconds(self):
        saved = []
        self.b.controller.update_series_timer = (
            lambda srv, tid, changes: saved.append((tid, changes)))
        page = open_live_tv(self.b, "series")
        page._open_series_timer({"Id": "st1"})
        self.b._timer_set("pre", "5")
        self.b._timer_set("keep_up_to", 3)
        self.b._timer_save()
        self.assertEqual(saved[0][0], "st1")
        self.assertEqual(saved[0][1]["PrePaddingSeconds"], 300)
        self.assertEqual(saved[0][1]["KeepUpTo"], 3)

    def test_keep_up_to_can_show_a_value_the_shim_does_not_list(self):
        """jellyfin-web offers every integer to 50 and this dropdown offers
        the round numbers, so a rule set to 12 there had no row to select
        and read back as "As many as possible" — a lie about somebody
        else's recording rule."""
        from jellyfin_mpv_shim.mpvtk_browser import livetv_dialogs

        choices = livetv_dialogs._keep_choices(12)
        self.assertIn(12, choices)
        self.assertEqual(sorted(choices), list(choices))
        self.assertLessEqual(set(livetv_dialogs.KEEP_UP_TO), set(choices))

    def test_an_unlisted_keep_up_to_survives_being_opened_and_saved(self):
        saved = []
        self.b.source.get_series_timer = lambda srv, tid: {
            "Id": tid, "Name": "A Series", "KeepUpTo": 12,
            "PrePaddingSeconds": 60, "PostPaddingSeconds": 120}
        self.b.controller.update_series_timer = (
            lambda srv, tid, changes: saved.append(changes))
        page = open_live_tv(self.b, "series")
        page._open_series_timer({"Id": "st1"})
        self.b._timer_save()
        self.assertEqual(saved[0]["KeepUpTo"], 12)

    def test_unparsable_padding_keeps_the_stored_value(self):
        saved = []
        self.b.controller.update_series_timer = (
            lambda srv, tid, changes: saved.append(changes))
        page = open_live_tv(self.b, "series")
        page._open_series_timer({"Id": "st1"})
        self.b._timer_set("pre", "abc")
        self.b._timer_save()
        # The fixture stores 60s of pre-padding; a stray keystroke must not
        # silently drop it to zero.
        self.assertEqual(saved[0]["PrePaddingSeconds"], 60)

    def test_the_editor_closes_before_it_confirms_a_cancellation(self):
        """There is one modal slot: leaving the editor open would have the
        confirmation replace it, and the editor would never come back."""
        page = open_live_tv(self.b, "series")
        page._open_series_timer({"Id": "st1"})
        self.b._timer_cancel()
        self.assertIsNone(self.b._timer_dlg)
        self.assertIn("dlg-cancel", self._ids(), "no confirmation was shown")


class TileMenu(unittest.TestCase):
    """Right-clicking a channel or a guide cell.

    This is where recording from a *listing* happens; without it the only
    way to set a timer is to open each programme in turn, which is not how
    anyone uses a guide.
    """

    def setUp(self):
        self.b = browser()

    def _actions(self, item):
        return [e[2] for e in self.b._tile_menu_entries(item)]

    def test_a_channel_offers_watch_and_favorite(self):
        acts = self._actions({"Id": "c1", "Type": "TvChannel"})
        self.assertEqual(acts, ["play", "favorite"])

    def test_a_channel_is_not_offered_a_download_or_a_queue(self):
        acts = self._actions({"Id": "c1", "Type": "TvChannel"})
        for absent in ("queue", "download", "addto", "watched"):
            with self.subTest(absent):
                self.assertNotIn(absent, acts)

    def test_an_unscheduled_program_offers_record(self):
        acts = self._actions({"Id": "p1", "Type": "Program",
                              "ChannelId": "c1"})
        self.assertEqual(acts, ["play", "record"])

    def test_a_scheduled_program_offers_cancellation(self):
        acts = self._actions({"Id": "p1", "Type": "Program", "ChannelId": "c1",
                              "TimerId": "t1", "Status": "New"})
        self.assertIn("unrecord", acts)
        self.assertNotIn("record", acts)

    def test_a_series_offers_the_series_entries(self):
        acts = self._actions({"Id": "p1", "Type": "Program", "ChannelId": "c1",
                              "IsSeries": True})
        self.assertIn("recordseries", acts)
        acts = self._actions({"Id": "p1", "Type": "Program", "ChannelId": "c1",
                              "IsSeries": True, "SeriesTimerId": "s1"})
        self.assertIn("unrecordseries", acts)

    def test_a_showing_covered_by_a_series_rule_can_still_be_skipped(self):
        """Both questions get an answer: this showing has its own timer AND
        the series is being recorded. Asking timer_state instead answered
        "series", so the menu offered Record for a programme that already
        had a timer and there was no way to drop one episode."""
        acts = self._actions({"Id": "p1", "Type": "Program", "ChannelId": "c1",
                              "IsSeries": True, "TimerId": "t1",
                              "SeriesTimerId": "s1", "Status": "New"})
        self.assertIn("unrecord", acts)
        self.assertIn("unrecordseries", acts)
        self.assertNotIn("record", acts)

    def test_a_series_recording_in_progress_offers_stop(self):
        labels = dict((e[2], e[0]) for e in self.b._tile_menu_entries(
            {"Id": "p1", "Type": "Program", "ChannelId": "c1",
             "IsSeries": True, "TimerId": "t1", "SeriesTimerId": "s1",
             "Status": "InProgress"}))
        self.assertEqual(labels["unrecord"], "Stop Recording")

    def test_watching_from_the_menu_tunes_the_channel(self):
        plays = []
        self.b.controller.play_list = lambda ids_, srv, i, **kw: plays.append(
            list(ids_))
        self.b._menu_play({"Id": "p1", "Type": "Program", "ChannelId": "c7"},
                          "srv1")
        self.assertEqual(plays, [["c7"]])

    def test_recording_from_the_menu_reaches_the_gateway(self):
        calls = []
        self.b.controller.create_timer = lambda srv, pid: calls.append(pid)
        self.b._live_menu_action("record", {"Id": "p1", "Type": "Program"},
                                 "srv1")
        self.assertEqual(calls, ["p1"])

    def test_the_menu_is_empty_when_recording_is_unavailable(self):
        ctl = FakeController()
        ctl.live_tv_apis = lambda: False
        b = browser(controller=ctl)
        acts = [e[2] for e in b._tile_menu_entries(
            {"Id": "p1", "Type": "Program", "ChannelId": "c1"})]
        self.assertEqual(acts, ["play"])


class Artwork(unittest.TestCase):
    """A SeriesTimer is not an item and has no ImageTags of its own.

    Every fixture below is a SeriesTimer, because that is the only timer DTO
    that carries these fields: ``ParentPrimaryImage*`` and ``ParentThumb*``
    are declared on ``SeriesTimerInfoDto`` alone, and a plain
    ``TimerInfoDto`` (which inherits only ``BaseTimerInfoDto``) has neither
    — its programme's art lives in a nested ``ProgramInfo``, and its tiles
    fall through to the channel logo. Pinning these branches with a
    ``Type: "Timer"`` shape would be pinning them against a DTO the server
    never sends.
    """

    def spec(self, item, image_type="Primary"):
        return LibrarySource.__new__(LibrarySource).image_spec(item,
                                                               image_type)

    def test_a_series_timer_uses_its_programmes_poster(self):
        # Without this the whole Series tab was placeholder glyphs.
        self.assertEqual(
            self.spec({"Id": "st1", "Type": "SeriesTimer",
                       "ParentPrimaryImageItemId": "prog1",
                       "ParentPrimaryImageTag": "tag"}),
            ("prog1", "Primary", "tag"))

    def test_a_landscape_row_prefers_the_parent_thumb(self):
        # Both fields are present on a series rule; a Thumb request means
        # a 16:9 tile, so the thumb is the right shape for it.
        self.assertEqual(
            self.spec({"Id": "st1", "Type": "SeriesTimer",
                       "ParentPrimaryImageItemId": "prog1",
                       "ParentPrimaryImageTag": "ptag",
                       "ParentThumbItemId": "prog1",
                       "ParentThumbImageTag": "ttag"}, "Thumb"),
            ("prog1", "Thumb", "ttag"))

    def test_a_landscape_row_still_falls_back_to_the_poster(self):
        self.assertEqual(
            self.spec({"Id": "st1", "Type": "SeriesTimer",
                       "ParentPrimaryImageItemId": "prog1",
                       "ParentPrimaryImageTag": "ptag"}, "Thumb"),
            ("prog1", "Primary", "ptag"))

    def test_the_parent_poster_beats_the_channel_logo(self):
        self.assertEqual(
            self.spec({"Id": "st1", "Type": "SeriesTimer",
                       "ParentPrimaryImageItemId": "prog1",
                       "ParentPrimaryImageTag": "ptag",
                       "ChannelId": "c1",
                       "ChannelPrimaryImageTag": "ctag"}),
            ("prog1", "Primary", "ptag"))

    def test_a_plain_timer_falls_through_to_the_channel_logo(self):
        """A TimerInfoDto has no image fields of its own — no ImageTags, no
        ParentPrimaryImage*, no ParentThumb*. The channel logo is all the
        Schedule tab has, which is also what jellyfin-web's getTimersHtml
        draws (showChannelLogo)."""
        self.assertEqual(
            self.spec({"Id": "t1", "Type": "Timer", "ChannelId": "c1",
                       "ChannelPrimaryImageTag": "ctag"}),
            ("c1", "Primary", "ctag"))

    def test_an_ordinary_item_is_unaffected(self):
        self.assertEqual(
            self.spec({"Id": "e1", "Type": "Episode", "SeriesId": "s1",
                       "SeriesPrimaryImageTag": "st",
                       "ParentPrimaryImageItemId": "x",
                       "ParentPrimaryImageTag": "y"}),
            ("s1", "Primary", "st"))


class RowShapes(unittest.TestCase):
    """A row's shape follows its artwork, like jellyfin-web's card builder.

    Its ``setCardData`` resolves ONE shape per row from the median
    ``PrimaryImageAspectRatio`` and applies it to every card — which is why
    the Programs screen shows posters for a row of films and landscape
    stills for the rest. Per row, not per tile: a strip is composited at one
    tile size, so this is reproducible exactly.
    """

    def setUp(self):
        self.b = browser()

    def shape(self, ratios, **kw):
        items = [{"Id": str(i), "PrimaryImageAspectRatio": r}
                 for i, r in enumerate(ratios)]
        return self.b.tiles.auto_geom(items, **kw)

    def test_posters_make_a_poster_row(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM

        geom, image_type = self.shape([0.666, 0.666, 0.7])
        self.assertIs(geom, POSTER_GEOM)
        self.assertEqual(image_type, "Primary")

    def test_stills_make_a_landscape_row(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM

        geom, image_type = self.shape([1.777, 1.777])
        self.assertIs(geom, LANDSCAPE_GEOM)
        # jellyfin-web's preferThumb:'auto' is exactly "thumbs iff backdrop".
        self.assertEqual(image_type, "Thumb")

    def test_square_art_makes_a_square_row(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import SQUARE_GEOM

        self.assertIs(self.shape([1.0, 1.0])[0], SQUARE_GEOM)

    def test_the_median_decides_not_one_outlier(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM

        # Nineteen posters and one banner is a poster row.
        self.assertIs(self.shape([0.666] * 19 + [3.5])[0], POSTER_GEOM)

    def test_items_without_a_ratio_fall_back(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM

        geom, image_type = self.b.tiles.auto_geom(
            [{"Id": "p1"}], default=LANDSCAPE_GEOM, default_type="Thumb")
        self.assertIs(geom, LANDSCAPE_GEOM)
        self.assertEqual(image_type, "Thumb")

    def test_a_banner_ratio_lands_in_landscape(self):
        # There is no banner tile here; a 3:1 image in a 16:9 frame is a far
        # smaller lie than one in a 2:3 frame.
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM

        self.assertIs(self.shape([3.5, 3.5])[0], LANDSCAPE_GEOM)

    def test_the_home_on_now_row_follows_the_same_rule(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            LANDSCAPE_GEOM, POSTER_GEOM)
        from tests._shell_harness import home_page

        page = home_page(self.b)
        posters = {"collection_type": "livetv",
                   "items": [{"Type": "Program",
                              "PrimaryImageAspectRatio": 0.666}]}
        self.assertIs(page._row_shape(posters)[0], POSTER_GEOM)
        stills = {"collection_type": "livetv",
                  "items": [{"Type": "Program",
                             "PrimaryImageAspectRatio": 1.777}]}
        self.assertIs(page._row_shape(stills)[0], LANDSCAPE_GEOM)

    def _program_rows(self, sections):
        self.b.source.get_program_sections = lambda srv, limit=12: sections
        page = open_live_tv(self.b, "programs")
        nodes, _h = build_scene(self.b, (1280, 720))
        del page
        return {n["id"]: n for n in nodes if n.get("id")}

    def test_the_programs_rows_are_shaped_individually(self):
        """Deliberately not the movies row, which is forced portrait — see
        below. Two auto rows whose artwork disagrees."""
        found = self._program_rows([
            {"key": "onnow", "title": "On Now",
             "items": [{"Id": "a", "Type": "Program",
                        "PrimaryImageAspectRatio": 1.777}]},
            {"key": "episodes", "title": "Upcoming Episodes",
             "items": [{"Id": "b", "Type": "Program",
                        "PrimaryImageAspectRatio": 0.666}]}])
        # The two carousels come out at different heights, which is the
        # whole observable difference between a poster row and a wide one.
        self.assertNotEqual(found["lt-onnow"]["h"], found["lt-episodes"]["h"])

    def test_upcoming_movies_is_portrait_whatever_its_artwork_says(self):
        """jellyfin-web forces this one row (livetvsuggested.js:87-91) rather
        than letting the median decide. Films are the one guide category that
        reliably carries poster art, and a median over a handful of them
        lands on landscape often enough to make the row change shape between
        refreshes."""
        found = self._program_rows([
            {"key": "episodes", "title": "Upcoming Episodes",
             "items": [{"Id": "a", "Type": "Program",
                        "PrimaryImageAspectRatio": 0.666}]},
            # Landscape artwork: an auto row would go wide here.
            {"key": "movies", "title": "Upcoming Movies",
             "items": [{"Id": "b", "Type": "Program",
                        "PrimaryImageAspectRatio": 1.777}]}])
        self.assertEqual(found["lt-movies"]["h"], found["lt-episodes"]["h"],
                         "the movies row followed its artwork")


class RecordingIndicator(unittest.TestCase):
    """What is being taped right now reads as red, everywhere it appears."""

    def setUp(self):
        self.b = browser()

    def _tile(self, item):
        return self.b.tiles._tile(item, self.b.geom)

    def test_a_programme_being_recorded_is_flagged(self):
        tile = self._tile({"Id": "p1", "Type": "Program", "TimerId": "t1",
                           "Status": "InProgress"})
        self.assertTrue(tile.recording)
        self.assertEqual(tile.record, "recording")

    def test_a_merely_scheduled_programme_is_not(self):
        tile = self._tile({"Id": "p1", "Type": "Program", "TimerId": "t1",
                           "Status": "New"})
        self.assertFalse(tile.recording)
        self.assertEqual(tile.record, "timer")

    def test_a_series_covered_programme_airing_now_is_both(self):
        """The bug behind the two symbols: "which glyph" and "is it taping"
        are different questions. A series-covered programme airing now has
        state "series" — so keying the bar colour off the state left it
        blue while it was being recorded."""
        tile = self._tile({"Id": "p1", "Type": "Program", "TimerId": "t1",
                           "SeriesTimerId": "s1", "Status": "InProgress"})
        self.assertEqual(tile.record, "series")
        self.assertTrue(tile.recording)

    def test_a_series_rule_that_is_not_taping_yet_shows_the_symbol_only(self):
        tile = self._tile({"Id": "p1", "Type": "Program", "TimerId": "t1",
                           "SeriesTimerId": "s1", "Status": "New"})
        self.assertEqual(tile.record, "series")
        self.assertFalse(tile.recording)

    def test_a_recording_still_being_written_is_flagged(self):
        # Its DTO carries no timer state; the query it came from is what
        # knows, and stamps it.
        tile = self._tile({"Id": "r1", "Type": "Recording",
                           "_recording": True})
        self.assertTrue(tile.recording)
        self.assertEqual(tile.record, "recording")

    def test_a_finished_recording_is_not(self):
        tile = self._tile({"Id": "r1", "Type": "Recording"})
        self.assertFalse(tile.recording)
        self.assertEqual(tile.record, "")

    def test_the_repository_stamps_in_progress_results(self):
        api = RecordingApi.Api()
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        api.get_live_tv_recordings = lambda **kw: {"Items": [{"Id": "r1"}]}
        self.assertTrue(
            src.get_recordings("srv", is_in_progress=True)[0]["_recording"])
        self.assertNotIn(
            "_recording", src.get_recordings("srv")[0])

    def test_the_recordings_tab_marks_what_is_still_taping(self):
        source = self.b.source
        calls = []

        def recordings(srv, limit=60, is_in_progress=None,
                       series_timer_id=None):
            calls.append(is_in_progress)
            if is_in_progress:
                return [{"Id": "rec1", "_recording": True}]
            return [{"Id": "rec1", "Name": "Taping", "Type": "Recording"},
                    {"Id": "rec2", "Name": "Done", "Type": "Recording"}]
        source.get_recordings = recordings
        page = open_live_tv(self.b, "recordings")
        latest = page.route["_data"]["latest"]
        self.assertTrue(latest[0].get("_recording"))
        self.assertIsNone(latest[1].get("_recording"))

    def test_the_flag_is_part_of_the_strip_cache_key(self):
        """Otherwise a tile that starts (or stops) recording keeps whatever
        bitmap it had — the whole point of a content-keyed cache."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile

        store = StripStore.__new__(StripStore)
        plain = store._tile_key(Tile(key="a"))
        self.assertNotEqual(store._tile_key(Tile(key="a", recording=True)),
                            plain)
        # ...and so is which symbol, or a programme whose series rule was
        # cancelled keeps the series glyph.
        self.assertNotEqual(store._tile_key(Tile(key="a", record="series")),
                            store._tile_key(Tile(key="a", record="timer")))

    def _painted(self, **kw):
        """The composited tile bitmap for one Tile, as a PIL image.

        Read back off the file backend, which stores raw premultiplied BGRA
        — so the channels are swapped here to compare against a named
        colour. (There is no other way to see what was drawn: the whole
        point of a strip is that it is one baked bitmap.)
        """
        import shutil
        import tempfile

        from PIL import Image

        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            StripStore, Tile, TileGeom)

        tmp = tempfile.mkdtemp(prefix="livetv-strip-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = StripStore(cache_dir=tmp, geom=TileGeom())
        out = store.strip([Tile(key="a", title="A", progress=0.5, **kw)])
        with open(out["src"], "rb") as fh:
            raw = fh.read()
        image = Image.frombytes("RGBA", (out["iw"], out["ih"]), raw)
        b, g, r, alpha = image.split()
        return Image.merge("RGBA", (r, g, b, alpha))

    def test_the_bar_is_red_while_recording(self):
        """The bar means "how much of the broadcast has gone"; the accent
        colour is spoken for by "where you left off", so this one is red."""
        from jellyfin_mpv_shim.mpvtk_browser import theme
        from jellyfin_mpv_shim.mpvtk_browser.strips import TileGeom

        geom = TileGeom()
        # Left end of the progress bar, which the 0.5 fraction covers.
        at = (4, geom.tile_h - 3)
        self.assertEqual(self._painted(recording=True).getpixel(at)[:3],
                         theme.rgb(theme.FAV_RED))
        self.assertEqual(self._painted().getpixel(at)[:3],
                         theme.rgb(theme.ACCENT))

    @staticmethod
    def _reddish(pixel):
        r, g, b = pixel[:3]
        return r > g + 40 and r > b + 40

    def _badge_columns(self, painted):
        """Columns of red ink across the middle of the corner badge.

        The shape is asserted through this rather than through fixed
        coordinates: the glyphs are rasterized Material paths now, so their
        exact pixels are the icon set's business — what this file cares
        about is that the two symbols are *different in the way they are
        meant to be*.
        """
        from jellyfin_mpv_shim.mpvtk_browser.strips import TileGeom

        geom = TileGeom()
        cy = 17
        return [x for x in range(geom.tile_w - 34, geom.tile_w)
                if self._reddish(painted.getpixel((x, cy)))]

    def test_the_record_dot_is_red_and_unringed(self):
        """No white outline: the plain red dot is the record symbol
        everywhere else in the app, and ringing this one made the same thing
        look like a different badge."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import TileGeom

        geom = TileGeom()
        painted = self._painted(record="recording")
        self.assertTrue(self._badge_columns(painted))
        self.assertFalse(self._badge_columns(self._painted()))
        # Nothing white anywhere in the badge's box — that was the ring.
        for x in range(geom.tile_w - 30, geom.tile_w - 4):
            for y in range(4, 30):
                pixel = painted.getpixel((x, y))[:3]
                self.assertFalse(min(pixel) > 200,
                                 "white ink at %d,%d" % (x, y))

    def test_a_series_rule_draws_a_different_symbol(self):
        """jellyfin-web's two glyphs: a plain dot for a single recording
        (``fiber_manual_record``) and a dot with a crescent beside it for one
        covered by a series rule (``fiber_smart_record``). Drawing the dot
        for both is what the screenshot caught.

        Asserted structurally: the crescent is detached, so the series glyph
        has a *gap* in its ink and reaches further right. That is the whole
        difference between the two symbols.
        """
        single = self._badge_columns(self._painted(record="recording"))
        series = self._badge_columns(self._painted(record="series"))
        self.assertTrue(single and series)
        self.assertGreater(max(series), max(single),
                           "the series crescent should sit outside the dot")
        # A plain dot is one solid run; the series glyph is two.
        self.assertEqual(single, list(range(min(single), max(single) + 1)),
                         "the single-recording dot should be solid")
        self.assertNotEqual(series, list(range(min(series), max(series) + 1)),
                            "the series glyph should have a gap in it")

    def test_the_glyphs_are_the_ones_the_guide_draws(self):
        """The same two names, from the same path data — so a series-recorded
        programme reads identically on a tile and in the guide cell next to
        it. A hand-drawn approximation is what this replaced."""
        from jellyfin_mpv_shim.mpvtk_browser import live_tv

        self.assertEqual(live_tv.STATE_ICONS["recording"],
                         "fiber_manual_record")
        self.assertEqual(live_tv.STATE_ICONS["series"], "fiber_smart_record")

    def test_an_inactive_series_rule_is_muted(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        from jellyfin_mpv_shim.mpvtk_browser.strips import TileGeom

        geom = TileGeom()
        cx, cy = geom.tile_w - 17, 17
        painted = self._painted(record="series_inactive")
        self.assertNotEqual(painted.getpixel((cx - 2, cy))[:3],
                            theme.rgb(theme.FAV_RED))


class TileProgress(unittest.TestCase):
    """An On Now tile's bar is how far through the *broadcast* is — there is
    no resume point for something airing live."""

    def test_an_airing_program_shows_its_progress(self):
        b = browser()
        start = live_tv.now() - datetime.timedelta(minutes=15)
        item = {"Id": "p1", "Name": "Half Way", "Type": "Program",
                "StartDate": start.astimezone(UTC).isoformat(),
                "EndDate": (start + datetime.timedelta(minutes=30)).astimezone(
                    UTC).isoformat()}
        tile = b.tiles._tile(item, b.geom)
        self.assertAlmostEqual(tile.progress, 0.5, places=1)

    def test_an_ordinary_item_still_uses_its_resume_point(self):
        b = browser()
        tile = b.tiles._tile(
            {"Id": "m1", "Type": "Movie", "RunTimeTicks": 100,
             "UserData": {"PlaybackPositionTicks": 25}}, b.geom)
        self.assertEqual(tile.progress, 0.25)


class Capability(unittest.TestCase):
    """Recording hides entirely on an apiclient that cannot do it, rather
    than rendering buttons that fail."""

    def test_it_fails_open(self):
        b = browser()
        self.assertTrue(b._actions.can_record())

    def test_a_definite_no_hides_the_buttons(self):
        ctl = FakeController()
        ctl.live_tv_apis = lambda: False
        b = browser(controller=ctl)
        b.source.get_live_program = lambda srv, pid: {
            "Id": pid, "Name": "Thing", "Type": "Program", "ChannelId": "c1",
            "IsSeries": True}
        b.navigate({"kind": "program", "server": "srv1", "item_id": "pr1",
                    "title": "T"})
        nodes, _h = build_scene(b, (1280, 720))
        found = ids(nodes)
        self.assertIn("pg-watch", found, "browsing must still work")
        self.assertNotIn("pg-record", found)
        self.assertNotIn("pg-recseries", found)


class HomeSection(unittest.TestCase):
    """jellyfin-web pairs the On Now strip with buttons into the six Live TV
    screens. The strip alone left the guide reachable only by finding the
    library tile."""

    def setUp(self):
        self.b = browser()
        self.b.source.home_rows = [
            {"title": "On Now", "items": [{"Id": "p1", "Type": "Program",
                                           "Name": "The News"}],
             "collection_type": "livetv", "kind": "livetv", "slot": 0}]

    def test_the_buttons_are_drawn(self):
        self.b.navigate({"kind": "home", "server": "srv1"})
        nodes, _h = build_scene(self.b, (1280, 720))
        found = ids(nodes)
        for tab in ("programs", "guide", "channels", "recordings", "schedule",
                    "series"):
            with self.subTest(tab):
                self.assertIn("home-lt-" + tab, found)

    def test_a_button_opens_that_tab(self):
        self.b.navigate({"kind": "home", "server": "srv1"})
        page = self.b._page_for(self.b.route)
        row = page._live_tv_buttons()
        # The first two children are the indent spacer and the heading.
        row.children[2 + 1].on_click()          # "Guide"
        self.assertEqual(self.b.route["kind"], "livetv")
        self.assertEqual(self.b.route["_tab"], "guide")

    def test_no_buttons_without_a_live_tv_row(self):
        self.b.source.home_rows = [
            {"title": "Continue Watching", "items": [{"Id": "m1",
                                                      "Type": "Movie"}],
             "collection_type": None, "kind": "resume", "slot": 0}]
        self.b.navigate({"kind": "home", "server": "srv1"})
        nodes, _h = build_scene(self.b, (1280, 720))
        self.assertNotIn("home-lt-guide", ids(nodes))


class LiveTvStaysFresh(unittest.TestCase):
    """Live TV is the one part of the library a third party changes while
    you are looking at it: a recording starts, a programme ends, another
    client sets a timer. Everything else is loaded once per navigation."""

    def setUp(self):
        self.b = browser()

    def _count_calls(self, name):
        calls = []
        real = getattr(self.b.source, name)

        def counted(*a, **kw):
            calls.append(kw)
            return real(*a, **kw)

        setattr(self.b.source, name, counted)
        return calls

    def test_returning_to_a_cached_tab_re_reads_it(self):
        """The cache still paints instantly -- it is what stops a Guide flip
        paying for the guide fetch twice -- but it must not be the last word.
        Serving it and stopping is how the Schedule tab came back without an
        in-progress recording that had started while the screen was up."""
        page = open_live_tv(self.b, "schedule")
        calls = self._count_calls("get_recordings")
        page._set_tab("channels")
        page._set_tab("schedule")
        self.assertTrue(calls, "a cached tab was served without re-reading")

    def test_the_cached_data_is_shown_while_the_re_read_runs(self):
        """No spinner over what the user is already reading."""
        page = open_live_tv(self.b, "programs")
        loaded = page.route["_data"]
        page._set_tab("channels")
        from tests._shell_harness import _NeverPool

        self.b._pool = _NeverPool()      # the re-read never lands
        page._set_tab("programs")
        self.assertEqual(page.route["_data"], loaded)

    def test_a_timer_event_refreshes_the_screen(self):
        open_live_tv(self.b, "schedule")
        calls = self._count_calls("get_timers")
        self.b.refresh_live_tv()
        self.assertTrue(calls)

    def test_a_timer_event_ignores_a_screen_that_is_not_live_tv(self):
        """It arrives from the websocket thread whatever is showing, and
        re-loading an unrelated route would refetch it for no reason."""
        self.b.navigate({"kind": "home", "server": "srv1"})
        calls = self._count_calls("get_timers")
        self.b.refresh_live_tv()
        self.assertEqual(calls, [])

    def test_the_refresh_does_not_bump_the_epoch(self):
        """A bump cancels in-flight work the user DID ask for. Nobody asked
        for this refresh, so it must not cancel anything."""
        open_live_tv(self.b, "guide")
        before = self.b._epoch
        self.b.refresh_live_tv()
        self.assertEqual(self.b._epoch, before)

    def test_the_channel_page_refreshes_too(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import LIVE_KINDS

        self.assertIn("channel", LIVE_KINDS)
        self.assertIn("livetv", LIVE_KINDS)

    def test_every_timer_event_is_bound(self):
        """The four jellyfin-web subscribes to. There is deliberately no
        "recording started" among them -- the server has no such message --
        which is why the screen polls as well."""
        from jellyfin_mpv_shim.event_handler import EventHandler, bindings

        for name in EventHandler.LIVE_TV_EVENTS:
            with self.subTest(name):
                self.assertIn(name, bindings)

    def test_the_event_reaches_the_hook(self):
        from jellyfin_mpv_shim.event_handler import EventHandler

        handler = EventHandler()
        seen = []
        handler.live_tv_changed = lambda client: seen.append(client)
        handler.handle_event("client", "TimerCreated", {})
        self.assertEqual(seen, ["client"])

    def test_a_broken_hook_does_not_kill_the_websocket_thread(self):
        """This runs on the socket thread; an escaping exception there takes
        the connection down with it."""
        from jellyfin_mpv_shim.event_handler import EventHandler

        handler = EventHandler()

        def boom(_client):
            raise OSError("no")

        handler.live_tv_changed = boom
        handler.handle_event("client", "TimerCancelled", {})   # must not raise


class ChannelListSurvivesAConcurrentRefresh(unittest.TestCase):
    """A background refresh and a scroll page-in, both in flight.

    Every browser suite runs on _SyncPool, which executes a job at submit
    time, so no test in the tree had ever had two jobs in flight — and this
    screen is the only one with a second writer for a list the user is also
    paging. The properties are the list's, not the calls': every channel
    exactly once, in server order, and never shorter than it was.
    """

    TOTAL = 400

    def setUp(self):
        self.b = browser()
        self.asked = []
        self.b.source.get_channels = self._channels

    def _channels(self, server_uuid, start_index=0, limit=CHANNEL_PAGE, **kw):
        # Honours start_index and limit against a fixed line-up. A fake that
        # returns one canned page for every request cannot show a page merged
        # twice, which is how this went unseen.
        self.asked.append((start_index, limit))
        end = min(start_index + limit, self.TOTAL)
        return ([{"Id": "c%d" % i, "Name": "Ch %d" % i, "Type": "TvChannel"}
                 for i in range(start_index, end)], self.TOTAL)

    def _paged_in(self, page, count):
        page.route["_data"] = [{"Id": "c%d" % i, "Name": "Ch %d" % i,
                                "Type": "TvChannel"} for i in range(count)]
        page.route["_total"] = self.TOTAL

    def _assert_sane(self, route, why=""):
        """Every property the list owes, checked at one instant: no channel
        twice, no gap, and never shorter than the longest it has been. The
        high-water mark is tracked here because a shrink is only visible
        *between* two completions — comparing start to end hides it."""
        data = route.get("_data") or []
        ids = [i["Id"] for i in data]
        self.assertEqual(len(ids), len(set(ids)), "a page was merged twice" + why)
        self.assertEqual(ids, ["c%d" % i for i in range(len(ids))],
                         "the list is no longer a prefix of server order" + why)
        self.assertGreaterEqual(len(ids), self._longest,
                                "the list shrank under a scroll past it" + why)
        self._longest = max(self._longest, len(ids))

    def _open(self, paged=250):
        page = open_live_tv(self.b, "channels")
        self._paged_in(page, paged)
        pool = _DeferredPool()
        self.b._pool = pool
        self.asked.clear()
        self._longest = paged
        return page, pool

    #: A scroll event near the bottom of the list. on_scroll's `then` fires on
    #: every event of a drag, so several of these per gesture is the norm.
    SCROLL = (9000, 9200)

    def test_a_page_is_never_fetched_twice(self):
        """The scroll that follows a refresh's completion computes its start
        from a list that refresh just rewrote — while the page-in it already
        submitted is still in flight against the same index."""
        page, pool = self._open()
        self.b.refresh_live_tv()                    # job A: re-read the 250
        page._channels_scrolled(*self.SCROLL)       # job B: page in at 250
        pool.release(0)                             # A answers, clears a guard
        page._channels_scrolled(*self.SCROLL)       # same drag, next event
        pool.drain()

        # Page-ins only: a refresh always asks from 0, and legitimately so.
        # `more` refuses to page an empty list, so its start is never 0.
        starts = [s for s, _l in self.asked if s]
        self.assertEqual(len(starts), len(set(starts)),
                         "start_index %r — one page fetched twice, and the "
                         "one after it never" % (starts,))
        self._assert_sane(page.route)

    def test_a_refresh_cannot_shorten_a_list_a_page_in_just_grew(self):
        """The likelier order, since the refresh is the bigger query: the
        page-in answers first and the refresh lands flat on top of it."""
        page, pool = self._open()
        self.b.refresh_live_tv()
        page._channels_scrolled(*self.SCROLL)
        pool.release_last()                         # page-in answers first
        self._assert_sane(page.route, " (after the page-in)")
        pool.drain()                                # then the refresh
        self._assert_sane(page.route, " (after the refresh)")

    def test_many_interleavings_keep_the_list_whole(self):
        """The property, over an arbitrary sequence rather than one order —
        the shape tests/test_syncplay_e2e.py uses for the same reason."""
        page, pool = self._open(paged=CHANNEL_PAGE)
        for step in range(20):
            # Alternate which one is submitted first — whichever gets there
            # first is the one that must make the other stand down, and only
            # one of those two directions was guarded.
            if step % 2:
                self.b.refresh_live_tv()
                page._channels_scrolled(*self.SCROLL)
            else:
                page._channels_scrolled(*self.SCROLL)
                self.b.refresh_live_tv()
            # ...and which of them answers first.
            (pool.release_last if step % 3 == 0 else pool.release)()
            self._assert_sane(page.route, " at step %d" % step)
            # The drag continues while the other is still in flight — this is
            # what turns one start computed against a replaced list into a
            # page fetched twice.
            page._channels_scrolled(*self.SCROLL)
            pool.drain()
            self._assert_sane(page.route, " at step %d" % step)
            starts = [s for s, _l in self.asked if s]
            self.assertEqual(len(starts), len(set(starts)),
                             "start_index %r at step %d" % (starts, step))
        self.assertGreater(self._longest, CHANNEL_PAGE,
                           "nothing paged in at all — the test proves nothing")

    def test_a_refresh_still_runs_after_the_last_one_finished(self):
        """The marker is released however the load ends. If it ever leaked,
        the screen would simply stop refreshing — silently, and only after
        however long the user leaves it open."""
        page, _pool = self._open()
        self.b._pool = _SyncPool()
        for _tick in range(5):
            self.asked.clear()
            self.b.refresh_live_tv()
            self.assertTrue(self.asked, "the refresh marker leaked")

    def test_a_superseded_refresh_still_releases_the_marker(self):
        """The case that runs neither callback: the user navigates while the
        refresh is in flight, so the epoch moves and on_done is dropped."""
        page, pool = self._open()
        self.b.refresh_live_tv()
        self.b.navigate({"kind": "home", "server": "srv1"})
        pool.drain()
        self.assertNotIn("_refreshing", page.route)


class GuideFollowsTheClock(unittest.TestCase):
    """"On now" has to keep meaning now.

    The Guide is the screen this app expects to be left open, and the poll's
    own comment says the interval exists to keep "on now" meaning now — which
    was true of every Live TV tab except this one, because the window was
    seeded from the clock exactly once and every later refresh re-fetched the
    same hours. No test in the tree moved the clock, so none of them could
    tell.
    """

    def setUp(self):
        self.b = browser()
        self._real_now = live_tv.now
        self.clock = [self._real_now()]
        live_tv.now = lambda: self.clock[0]
        self.addCleanup(lambda: setattr(live_tv, "now", self._real_now))

    def _advance(self, **kw):
        self.clock[0] = self.clock[0] + datetime.timedelta(**kw)

    def _covers_now(self, page):
        data = page.route["_data"]
        return data["start"] <= live_tv.now() < data["end"]

    def test_a_refresh_moves_the_window_with_the_clock(self):
        page = open_live_tv(self.b, "guide")
        self.assertTrue(self._covers_now(page))
        for hours in (1, 3, 9, 30):
            self._advance(hours=hours)
            self.b.refresh_live_tv()
            self.assertTrue(self._covers_now(page),
                            "the guide is showing %d hours ago" % hours)
            self.assertEqual(page.route["_start"],
                             live_tv.floor_to_cell(live_tv.now()))

    def test_a_window_the_user_paged_to_is_never_dragged_back(self):
        """The half that makes the other half safe. Yanking someone out of
        tomorrow evening because a timer event arrived is worse than a stale
        grid, so this is a flag rather than a staleness test."""
        page = open_live_tv(self.b, "guide")
        page._move_window(datetime.timedelta(days=1), {})
        parked = page.route["_start"]
        for _refresh in range(5):
            self._advance(hours=2)
            self.b.refresh_live_tv()
            self.assertEqual(page.route["_start"], parked,
                             "a background refresh moved the user's window")

    def test_the_now_button_hands_the_window_back(self):
        page = open_live_tv(self.b, "guide")
        page._move_window(datetime.timedelta(days=1), {})
        self._advance(hours=1)
        page._jump_to_now()
        self.assertEqual(page.route["_start"],
                         live_tv.floor_to_cell(live_tv.now()))
        # ...and it follows the clock again afterwards.
        self._advance(hours=5)
        self.b.refresh_live_tv()
        self.assertTrue(self._covers_now(page))

    def test_a_cached_tab_return_re_seeds_too(self):
        """The third trigger: the tab cache paints instantly, and then the
        re-read lands. Both have to be about the right hours."""
        page = open_live_tv(self.b, "guide")
        page._set_tab("channels")
        self._advance(hours=6)
        page._set_tab("guide")
        self.assertTrue(self._covers_now(page))


class RefreshKeepsTheUsersPlace(unittest.TestCase):
    """An auto-refresh is the one screen update nobody asked for, so it has
    to be invisible: same scroll, same open menu, same dialog, same list."""

    def setUp(self):
        self.b = browser()

    def _scene(self):
        return build_scene(self.b, (1280, 720))

    def test_it_does_not_park_or_reset_the_scroll(self):
        """park/reset are the navigation pair. A refresh is not navigation;
        resetting would drop the renderer's offset for every container."""
        open_live_tv(self.b, "channels")
        calls = []
        self.b._scroll.reset = lambda: calls.append("reset")
        self.b._scroll.park = lambda *a, **kw: calls.append("park")
        self.b.refresh_live_tv()
        self.assertEqual(calls, [])

    def test_the_scroll_container_keeps_its_id(self):
        """The renderer applies a parked offset only to a container it has
        no offset for yet, so a stable id IS the preserved scroll."""
        open_live_tv(self.b, "channels")
        before = ids(self._scene()[0])
        self.assertIn("livetv-channels", before)
        self.b.refresh_live_tv()
        self.assertIn("livetv-channels", ids(self._scene()[0]))

    def test_it_does_not_run_while_a_context_menu_is_open(self):
        open_live_tv(self.b, "channels")
        self.b._open_tile_menu({"Id": "c1", "Type": "TvChannel",
                                "Name": "One"}, 100, 100)
        calls = []
        self.b.source.get_channels = lambda *a, **kw: (calls.append(1),
                                                       ([], 0))[1]
        self.b.refresh_live_tv()
        self.assertEqual(calls, [], "the ground moved under an open menu")

    def test_the_menu_is_still_up_afterwards(self):
        open_live_tv(self.b, "channels")
        self.b._open_tile_menu({"Id": "c1", "Type": "TvChannel",
                                "Name": "One"}, 100, 100)
        self.b.refresh_live_tv()
        self.assertIsNotNone(self.b._menu)
        self.assertIn("tilemenu", ids(self._scene()[0]))

    def test_it_does_not_run_while_a_dialog_is_open(self):
        page = open_live_tv(self.b, "guide")
        page._open_guide_settings()
        calls = []
        self.b.source.get_guide = lambda *a, **kw: (calls.append(1), [])[1]
        self.b.refresh_live_tv()
        self.assertEqual(calls, [])

    def test_the_dialog_survives_a_refresh(self):
        """Its state is the dialog's own, not the route's — but a repaint
        rebuilds it, so this pins that the rebuild still finds it."""
        page = open_live_tv(self.b, "guide")
        page._open_guide_settings()
        self.b._guide_set("color_coded", True)
        self.b.refresh_live_tv()
        self.assertIn("gs-cat-movies", ids(self._scene()[0]))
        self.assertTrue(self.b._guide_dlg["prefs"]["color_coded"])

    def test_it_does_not_run_while_a_page_in_is_in_flight(self):
        """Paginator.more computes its merge against the list length at
        submit time; replacing the list under it duplicates or drops a
        page."""
        open_live_tv(self.b, "channels")
        self.b.route["_loading"] = True
        calls = []
        self.b.source.get_channels = lambda *a, **kw: (calls.append(1),
                                                       ([], 0))[1]
        self.b.refresh_live_tv()
        self.assertEqual(calls, [])

    def test_a_refresh_re_reads_every_page_the_user_scrolled_in(self):
        """The failure this guards: the tab pages in on scroll, so asking
        for one page would shrink a 250-item list back to 100 and the
        renderer would clamp the scroll to the top of it."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import CHANNEL_PAGE

        page = open_live_tv(self.b, "channels")
        asked = []

        def channels(server_uuid, start_index=0, limit=CHANNEL_PAGE, **kw):
            asked.append(limit)
            return ([{"Id": "c%d" % i, "Name": "Ch %d" % i,
                      "Type": "TvChannel"}
                     for i in range(start_index, start_index + limit)], 400)

        self.b.source.get_channels = channels
        # Two pages already scrolled in.
        page.route["_data"] = [{"Id": "c%d" % i, "Type": "TvChannel"}
                               for i in range(250)]
        page.route["_total"] = 400
        asked.clear()
        self.b.refresh_live_tv()
        self.assertTrue(asked)
        self.assertGreaterEqual(asked[0], 250)
        self.assertGreaterEqual(len(page.route["_data"]), 250)

    def test_the_first_load_still_asks_for_one_page(self):
        """The preservation above must not turn every cold load into a
        request for the whole line-up."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import CHANNEL_PAGE

        asked = []
        real = self.b.source.get_channels
        self.b.source.get_channels = (
            lambda *a, **kw: (asked.append(kw.get("limit")), real(*a, **kw))[1])
        open_live_tv(self.b, "channels")
        self.assertEqual(asked, [CHANNEL_PAGE])


class ChannelFilter(unittest.TestCase):
    """The Channels tab's filter row. jellyfin-web has a filter button whose
    dialog, in livetvchannels mode, is exactly one checkbox: Favorites."""

    def setUp(self):
        self.b = browser()

    def _ids(self):
        nodes, _h = build_scene(self.b, (1280, 720))
        return ids(nodes)

    def _texts(self):
        nodes, _h = build_scene(self.b, (1280, 720))
        return [n.get("text") for n in nodes if n.get("text")]

    def test_the_controls_are_there(self):
        open_live_tv(self.b, "channels")
        found = self._ids()
        self.assertIn("lt-chanfav", found)
        self.assertIn("lt-chancfg", found)

    def test_the_favorites_toggle_filters_the_fetch(self):
        """get_channels has carried favorites_only since the tab was written
        and nothing ever passed it."""
        page = open_live_tv(self.b, "channels")
        seen = []
        real = self.b.source.get_channels
        self.b.source.get_channels = (
            lambda *a, **kw: (seen.append(kw.get("favorites_only")),
                              real(*a, **kw))[1])
        page._toggle_channel_favorites()
        self.assertTrue(page.route["_fav_only"])
        self.assertIn(True, seen)

    def test_toggling_it_back_clears_the_filter(self):
        page = open_live_tv(self.b, "channels")
        page._toggle_channel_favorites()
        page._toggle_channel_favorites()
        self.assertFalse(page.route["_fav_only"])

    def test_an_empty_favorites_list_says_why_it_is_empty(self):
        page = open_live_tv(self.b, "channels")
        self.b.source.get_channels = lambda *a, **kw: ([], 0)
        page._toggle_channel_favorites()
        self.assertTrue(any("favorite" in (t or "").lower()
                            for t in self._texts()))

    def test_guide_settings_opens_from_the_channels_tab(self):
        """The categories in it filter the channel LIST as well as the grid,
        and this tab's _data is a list -- indexing "prefs" out of it the way
        the guide tab does raises."""
        page = open_live_tv(self.b, "channels")
        page._open_guide_settings()
        self.assertIn("gs-cat-movies", self._ids())

    def test_the_channel_count_is_shown(self):
        open_live_tv(self.b, "channels")
        self.assertTrue(any("3" in (t or "") for t in self._texts()))


class ChannelScreen(unittest.TestCase):
    """The channel page: what a channel tile opens now instead of tuning in.

    jellyfin-web's item detail page for a TvChannel — a link, not a play
    button, whose whole content is that channel's upcoming programmes.
    """

    def setUp(self):
        self.b = browser()

    def _open(self, listing=None, seed=None):
        if listing is not None:
            self.b.source.get_channel_listing = (
                lambda srv, cid, limit=200: listing)
        route = {"kind": "channel", "server": "srv1", "item_id": "c1",
                 "title": "Channel 1"}
        if seed is not None:
            route["_seed"] = seed
        self.b.navigate(route)
        return self.b._page_for(self.b.route)

    def _nodes(self):
        nodes, _h = build_scene(self.b, (1280, 720))
        return nodes

    def _texts(self):
        return [n.get("text") for n in self._nodes() if n.get("text")]

    def test_it_renders_the_channel_and_a_watch_button(self):
        self._open()
        found = ids(self._nodes())
        self.assertIn("ch-watch", found)
        self.assertIn("ch-fav", found)
        self.assertIn("Channel 1", self._texts())

    def test_it_lists_the_upcoming_programmes(self):
        self._open()
        self.assertIn("Program 0", self._texts())
        self.assertIn("Program 2", self._texts())

    def test_watch_tunes_the_channel(self):
        """Playing directly is still one click — it just is not the ONLY
        thing the tile can do any more."""
        plays = []
        self.b.controller.play_list = lambda ids_, srv, i, **kw: plays.append(
            list(ids_))
        page = self._open()
        page._buttons().children[0].on_click()
        self.assertEqual(plays, [["c1"]])

    def test_favoriting_flips_the_button_on_the_page(self):
        """toggle_favorite writes into the item it is handed, so the page has
        to hand it the live dict — a copy left the old label up until
        something else reloaded the route."""
        page = self._open()
        page._buttons().children[1].on_click()
        self.assertIn("Remove from Favorites", self._texts())

    def test_the_rows_fill_the_content_width(self):
        """Without align="stretch" on the outer Column the rows took their
        natural width: a 747px content area drew a 479px row and ellipsized
        both the programme title and the episode title with 268px of empty
        space to the right of them."""
        self._open()
        nodes, _h = build_scene(self.b, (779, 707))
        rows = [n for n in nodes if (n.get("id") or "").startswith("ch-pg-")]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=row["id"]):
                # The content area is the window less the two content pads.
                self.assertGreater(row["w"], 779 - 2 * 24)

    def test_a_listing_row_opens_the_program_page(self):
        page = self._open()
        page._program_row(page.route["_data"][1]).on_click()
        self.assertEqual(self.b.route["kind"], "program")
        self.assertEqual(self.b.route["item_id"], "pr2")

    def test_a_listing_row_carries_the_channel_for_watch(self):
        """The program page's Watch tunes ChannelId; guide data that omits it
        would leave the button off entirely."""
        page = self._open()
        row = dict(page.route["_data"][1])
        row.pop("ChannelId")
        page._program_row(row).on_click()
        self.assertEqual(self.b.route["channel_id"], "c1")

    def test_the_programmes_are_grouped_by_day(self):
        page = self._open()
        groups = live_tv.group_by_day(page.route["_data"])
        self.assertTrue(groups)
        self.assertTrue(all(label for label, _items in groups))

    def test_the_seed_draws_before_the_fetch_lands(self):
        """Clicking a channel tile must not show a spinner for a channel
        whose DTO the caller already had."""
        from tests._shell_harness import _NeverPool

        self.b._pool = _NeverPool()
        self._open(seed={"Id": "c1", "Name": "Seeded", "Type": "TvChannel"})
        self.assertIn("Seeded", self._texts())

    def test_the_fetch_replaces_the_seed(self):
        """A route outlives the tile that seeded it — a favourite toggled on
        the page would otherwise redraw from a stale DTO forever."""
        page = self._open(seed={"Id": "c1", "Name": "Seeded",
                                "Type": "TvChannel"})
        self.assertEqual(page.route["_channel"]["Name"], "Channel 1")

    def test_an_empty_listing_says_so(self):
        self._open({"channel": {"Id": "c1", "Name": "Channel 1",
                                "Type": "TvChannel"},
                    "programs": [], "capped": False})
        self.assertIn("ch-watch", ids(self._nodes()))
        self.assertTrue(any("No guide data" in t for t in self._texts()))

    def test_a_capped_listing_admits_it(self):
        """A listing that just stops at a round number reads as the provider
        having run out of guide data."""
        self._open({"channel": {"Id": "c1", "Name": "Channel 1",
                                "Type": "TvChannel"},
                    "programs": [FakeSource._program(0, -5)], "capped": True})
        self.assertTrue(any("Showing the next" in t for t in self._texts()))

    def test_the_guide_channel_column_opens_it(self):
        """jellyfin-web's guide-channelHeaderCell is data-action="link" too."""
        page = open_live_tv(self.b, "guide")
        page._open_channel({"Id": "c7", "Name": "Seven"})
        self.assertEqual(self.b.route["kind"], "channel")
        self.assertEqual(self.b.route["item_id"], "c7")

    def test_the_guide_channel_cells_are_clickable(self):
        open_live_tv(self.b, "guide")
        self.assertIn("guide-ch-c1", ids(self._nodes()))


class ChannelListingIsWindowed(unittest.TestCase):
    """A fortnight of listings is ~670 rows and the fetch allows a thousand.
    Building them all put ~2400 nodes in one scene and cost 35ms a frame, so
    the page draws only the days near the viewport -- the same windowing the
    grid and the guide do."""

    N = 1000          # ~21 days at half-hour granularity
    PER_DAY = 48

    def setUp(self):
        self.b = browser()
        self.b.source.get_channel_listing = lambda srv, cid, limit=1000: {
            "channel": {"Id": cid, "Name": "Channel 1", "Type": "TvChannel"},
            "programs": self._programs(self.N), "capped": False}
        self.b.navigate({"kind": "channel", "server": "srv1",
                         "item_id": "c1", "title": "Channel 1"})

    @staticmethod
    def _programs(n):
        base = datetime.datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0)
        out = []
        for i in range(n):
            start = base + datetime.timedelta(minutes=30 * i)
            out.append({
                "Id": "pr%d" % i, "Name": "P%d" % i, "Type": "Program",
                "ChannelId": "c1",
                "StartDate": start.astimezone(datetime.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.0000000Z"),
                "EndDate": (start + datetime.timedelta(minutes=30)).astimezone(
                    datetime.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.0000000Z")})
        return out

    def _rows(self, offset=None):
        if offset is not None:
            self.b._scroll.on_scroll("channel", offset, 100000)
        nodes, _h = build_scene(self.b, (1280, 720))
        return [n for n in nodes
                if (n.get("id") or "").startswith("ch-pg-")]

    def test_only_a_few_days_are_built(self):
        rows = self._rows(0)
        self.assertTrue(rows)
        self.assertLess(len(rows), self.N // 4,
                        "the whole listing was built into one scene")

    def test_the_scene_stays_the_size_of_the_window_not_the_guide(self):
        """The property that lets the fetch be a thousand rather than two
        hundred: node count must not track the listing length."""
        short, _h = build_scene(self.b, (1280, 720))
        self.b.route["_data"] = self._programs(100)
        self.b.route.pop("_groups", None)
        few, _h = build_scene(self.b, (1280, 720))
        self.assertLess(abs(len(short) - len(few)), 60)

    def test_the_window_follows_the_scroll(self):
        first = {n["id"] for n in self._rows(0)}
        later = {n["id"] for n in self._rows(20000)}
        self.assertTrue(later)
        self.assertFalse(first & later, "the window did not move")

    def test_the_built_rows_cover_the_viewport_at_every_offset(self):
        """The failure this guards is a screenful of blanks: the placeholder
        heights have to reproduce the Column layout exactly, or the computed
        window drifts further from the real one the longer the listing gets.
        """
        every = {n["id"]: n["y"] for n in self._all_rows()}
        top, bottom = min(every.values()), max(every.values())
        for offset in (0, 5000, 12000, 20000, 30000):
            with self.subTest(offset=offset):
                ys = [n["y"] for n in self._rows(offset)]
                self.assertTrue(ys, "nothing drawn at %d" % offset)
                # Everything in the viewport that HAS content is built. The
                # clamps are the ends of the listing: at the top the header
                # occupies the first screen, at the bottom there is no more.
                self.assertLessEqual(min(ys), max(offset, top))
                self.assertGreaterEqual(max(ys), min(offset + 720, bottom))

    def test_every_day_heading_is_drawn(self):
        """Headings are one node each and there are a couple of dozen, so
        they are cheaper to draw than to place a heading-shaped hole for --
        and the listing keeps its shape while you scroll."""
        nodes, _h = build_scene(self.b, (1280, 720))
        texts = [n.get("text") or "" for n in nodes]
        days = [t for t in texts if t.count(",") == 1 and len(t) <= 13]
        self.assertGreaterEqual(len(days), self.N // self.PER_DAY)

    def _all_rows(self):
        """Every row's y, from a build with the window opened wide enough to
        hold the whole listing."""
        page = self.b._page_for(self.b.route)
        rows = {}
        for offset in range(0, 40000, 1000):
            for node in self._rows(offset):
                rows[node["id"]] = node
        return list(rows.values())

    def test_the_scroll_is_reported_back(self):
        """``watch`` is what makes the renderer send scroll events back for
        this container; without it the window would be computed once and
        never move again."""
        nodes, _h = build_scene(self.b, (1280, 720))
        scroller = next(n for n in nodes
                        if n.get("id") == "channel" and n.get("t") == "scroll")
        self.assertTrue(scroller.get("watch"),
                        "the channel scroller reports nothing: %r" % scroller)

    def test_the_day_grouping_is_computed_once_per_fetch(self):
        """parse_time is not cheap and render needs the grouping every
        frame; re-grouping a thousand programmes per repaint was the whole
        residual cost once the rows were windowed."""
        page = self.b._page_for(self.b.route)
        first = page._groups()
        self.assertIs(page._groups(), first)
        page.route["_data"] = self._programs(20)
        self.assertIsNot(page._groups(), first)


class ChannelListingFetch(unittest.TestCase):
    def test_the_cap_is_a_backstop_not_a_page(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import CHANNEL_LISTING

        # A fortnight of half-hour listings is ~670 rows; the ceiling is
        # about the response now, not the render.
        self.assertGreaterEqual(CHANNEL_LISTING, 672)

    @staticmethod
    def _source():
        api = type("Api", (), {})()
        api.get_item = lambda *a, **kw: {"Id": "c1", "Type": "TvChannel"}
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        return src, api

    def _captured(self):
        src, api = self._source()
        captured = {}
        api.get_programs = lambda **kw: captured.update(kw) or {"Items": []}
        src.get_channel_listing("srv", "c1")
        return captured

    def test_it_asks_for_no_image_fields(self):
        """These rows are text. ChannelImage in particular costs a channel
        lookup per programme -- across a thousand of them -- for a tag
        nothing on this screen draws. jellyfin-web passes EnableImages:false
        here for the same reason."""
        captured = self._captured()
        self.assertNotIn("ChannelImage", captured.get("fields") or "")
        self.assertIsNone(captured.get("enable_image_types"))
        self.assertIsNone(captured.get("image_type_limit"))
        self.assertIn("ChannelInfo", captured["fields"])

    def test_it_asks_for_what_has_not_finished_yet(self):
        """HasAired=False keeps whatever is mid-broadcast, which is what
        makes the first row "on now" rather than "on next"."""
        captured = self._captured()
        self.assertIs(captured["has_aired"], False)
        self.assertEqual(captured["sort_by"], "StartDate")

    def test_a_channel_that_cannot_be_read_still_returns_its_listing(self):
        """The tile that linked here seeded the header; the listing is what
        the page is for."""
        src, api = self._source()
        api.get_programs = lambda **kw: {"Items": [{"Id": "pr1"}]}
        api.get_item = lambda *a, **kw: (_ for _ in ()).throw(OSError("no"))
        out = src.get_channel_listing("srv", "c1")
        self.assertIsNone(out["channel"])
        self.assertEqual(len(out["programs"]), 1)


class EmptyStatesSitWhereContentWould(unittest.TestCase):
    """chrome.error is direction="row", which makes align the VERTICAL axis
    — it read align="center" and floated the line halfway down an otherwise
    empty screen, attached to nothing."""

    EMPTY = (
        ("recordings", "Nothing has been recorded yet."),
        ("schedule", "Nothing is scheduled to record."),
        ("series", "No series are set to record."),
        ("programs", "No programs are listed right now."),
    )

    def _empty(self, tab):
        b = browser()
        b.source.get_recordings = lambda *a, **kw: []
        b.source.get_recording_folders = lambda *a, **kw: []
        b.source.get_timers = lambda *a, **kw: []
        b.source.get_series_timers = lambda *a, **kw: []
        b.source.get_program_sections = lambda *a, **kw: []
        open_live_tv(b, tab)
        return build_scene(b, (1280, 720))[0]

    def test_the_message_sits_under_the_tab_bar(self):
        for tab, text in self.EMPTY:
            with self.subTest(tab=tab):
                nodes = self._empty(tab)
                node = next(n for n in nodes if n.get("text") == text)
                # The tab bar ends around y=110; centred it landed near 380.
                self.assertLess(node["y"], 200,
                                "%r floated at y=%s" % (text, node["y"]))


class GuideChannelColumn(unittest.TestCase):
    """It was ellipsizing names it had the room to draw."""

    def _label_w(self):
        # The logo, the Row gap and the pad come off before the label.
        return guide_view.CHANNEL_W - guide_view.LOGO - 8 - 12

    def test_a_long_channel_name_fits(self):
        from jellyfin_mpv_shim.mpvtk.layout import text_width

        for name in ("Sky Sports Main Event", "Discovery Turbo Xtra"):
            with self.subTest(name):
                self.assertLessEqual(text_width(name, 14), self._label_w())

    def test_the_wider_column_costs_the_grid_no_cells(self):
        """The window is a whole number of 30-minute cells, so the extra
        width comes out of each cell rather than out of the count -- but
        only while they stay above MIN_CELL_W."""
        for window in (800, 1024, 1280, 1366, 1600, 1920, 2560):
            with self.subTest(window=window):
                grid = guide_view.grid_width((window, 720))
                cells = live_tv.cells_for_width(grid)
                self.assertGreaterEqual(grid / cells, live_tv.MIN_CELL_W)


class ChannelNumber(unittest.TestCase):
    """Two endpoints, two spellings — the page reaches a channel by id and
    sees the one the tile that linked to it did not."""

    def test_the_channel_list_spelling(self):
        self.assertEqual(live_tv.channel_number({"Number": "101"}), "101")

    def test_the_item_endpoint_spelling(self):
        self.assertEqual(live_tv.channel_number({"ChannelNumber": "101"}),
                         "101")

    def test_no_number_is_empty_not_none(self):
        self.assertEqual(live_tv.channel_number({}), "")
        self.assertEqual(live_tv.channel_number({"Number": "  "}), "")


if __name__ == "__main__":
    unittest.main()
