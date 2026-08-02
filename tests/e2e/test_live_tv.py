"""Live TV against a real tuner and a real guide.

`tests/test_live_tv.py` is 110k of tests against fabricated data, and it was
green while `4bc952c1` shipped: category flags derived as `"is_" + name`, so
`"movies"` became `is_movies` and every filtered fetch raised `TypeError`
before reaching the server — **the unit test asserted the wrong spelling,
which is why it passed**. That is the case for doing this against a server,
and it is not a hypothetical: Live TV is where the shim makes the most claims
about server behaviour, and `CLAUDE.md` records a dozen of them as prose that
nothing executes.

The claims pinned here, each of which failed silently when it was wrong:

* Category flags reach the server without raising, and go to the **channel**
  query only.
* Guide bounds are UTC with the `Z`; the server accepts an offset-less bound
  *without shifting it* and then answers for the wrong window, so getting this
  wrong returns plausible data for a time you did not ask about.
* Times come back with seven fractional digits, which most parsers accept and
  silently mis-zone.
* Guide preferences live in jellyfin-web's DisplayPreferences document, under
  its client id, written as the **strings** `"true"`/`"false"` — and saving
  them must not take the home layout with them.

The tuner is faketvsource (`stdjflib serve --live-tv`): six channels, ~970
programmes, generated against the clock. So every assertion here is relative
— "something is on now", "this window excludes that one" — and never against
an absolute time.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class _LiveTvCase(unittest.TestCase):
    """A session with Live TV, skipping if the server has no tuner."""

    ACCOUNT = "qa-user"

    @classmethod
    def setUpClass(cls):
        from jellyfin_mpv_shim.mpvtk_browser import live_tv
        cls.live_tv = live_tv
        cls.session = _e2e.Session(cls.ACCOUNT)
        cls.source = cls.session.library_source()
        cls.source.get_libraries(_e2e.SOURCE_UUID)   # populates has_live_tv
        if not cls.source.has_live_tv(_e2e.SOURCE_UUID):
            cls.source.stop()
            cls.session.stop()
            raise unittest.SkipTest(
                "no Live TV on this server — start it with "
                "`stdjflib serve <library> --live-tv`")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    @property
    def uuid(self):
        return _e2e.SOURCE_UUID


@_e2e.require_server
class ChannelsTest(_LiveTvCase):

    def test_the_line_up_comes_back_with_what_a_tile_needs(self):
        channels, total = self.source.get_channels(self.uuid)
        self.assertTrue(channels, "no channels from a server with a tuner")
        self.assertGreaterEqual(total, len(channels))
        for channel in channels:
            self.assertEqual(channel.get("Type"), "TvChannel")
            self.assertTrue(channel.get("Name"))
            # The tile draws the number, and the channel logo is the whole
            # artwork fallback for guide data that carries none of its own.
            self.assertTrue(channel.get("ChannelNumber"))


@_e2e.require_server
class GuideWindowTest(_LiveTvCase):
    """The window bounds, which are the easiest thing here to get wrong in a
    way that still returns data."""

    def _guide(self, start, end):
        channels = self.source.get_channels(self.uuid)[0]
        ids = [c["Id"] for c in channels]
        return self.source.get_guide(self.uuid, ids, start, end)

    def test_every_entry_overlaps_the_window_asked_for(self):
        start = utcnow()
        end = start + datetime.timedelta(hours=2)
        entries = self._guide(start, end)
        self.assertTrue(entries, "an empty guide for the next two hours")
        for entry in entries:
            entry_start = self.live_tv.parse_time(entry["StartDate"])
            entry_end = self.live_tv.parse_time(entry["EndDate"])
            self.assertTrue(
                entry_start < end and entry_end > start,
                "%r runs %s..%s, outside the window %s..%s — the bounds were "
                "sent in a form the server read as a different time"
                % (entry.get("Name"), entry_start, entry_end, start, end))

    def test_a_later_window_answers_about_later(self):
        """The sharp one.

        An offset-less bound is accepted and *not* shifted, so a client in a
        non-UTC zone gets a plausible guide for a window hours from the one it
        asked about. Comparing two windows catches that where checking one
        cannot: this box is UTC-4, so a dropped offset moves the answer by
        four hours and these two sets stop disagreeing the way they should.
        """
        now = utcnow()
        soon = self._guide(now, now + datetime.timedelta(minutes=30))
        later_start = now + datetime.timedelta(hours=6)
        later = self._guide(later_start,
                            later_start + datetime.timedelta(minutes=30))
        self.assertTrue(soon, "nothing on in the next half hour")
        self.assertTrue(later, "nothing on in half an hour, six hours out")

        for entry in later:
            self.assertGreater(
                self.live_tv.parse_time(entry["EndDate"]), later_start,
                "a window six hours out returned something that had already "
                "finished — the bound was not read as the time we sent")

    def test_times_carry_seven_fractional_digits_and_parse_to_utc(self):
        """`parse_time` exists because every shorter way of reading these
        yields a plausible datetime that is out by the UTC offset."""
        entries = self._guide(utcnow(), utcnow() + datetime.timedelta(hours=2))
        raw = entries[0]["StartDate"]
        self.assertTrue(
            raw.endswith("Z"),
            "the server stopped sending UTC-marked times: %r" % raw)
        parsed = self.live_tv.parse_time(raw)
        self.assertIsNotNone(parsed.tzinfo,
                             "parse_time returned a naive datetime, which is "
                             "how an offset silently goes missing")
        # Same instant, whatever local zone the box is in.
        self.assertEqual(
            parsed.astimezone(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S"),
            raw.split(".")[0])


@_e2e.require_server
class CategoryFilterTest(_LiveTvCase):
    """`4bc952c1` — the flags must reach the server, and only on channels.

    What this can assert is narrow on purpose. The *results* are a server
    wart: `IsMovie` is a column predicate while `IsSports`/`IsNews`/`IsKids`
    become a tag filter, so several categories legitimately return nothing
    here and two together return nothing at all. jellyfin-web behaves
    identically. Asserting on the counts would be asserting the wart, so what
    is asserted is that the call is well-formed and completes — which is
    exactly what was broken.
    """

    def test_every_category_reaches_the_server_without_raising(self):
        for category in ("movies", "sports", "kids", "news"):
            with self.subTest(category=category):
                kwargs = self.live_tv.category_kwargs([category])
                self.assertTrue(kwargs, "no flag produced for %r" % category)
                # The regression: "movies" -> is_movie, not is_movies.
                channels, _total = self.source.get_channels(
                    self.uuid, categories=(category,))
                self.assertIsInstance(channels, list)

    def test_two_categories_at_once_still_completes(self):
        channels, _total = self.source.get_channels(
            self.uuid, categories=("movies", "sports"))
        self.assertIsInstance(channels, list)

    def test_a_column_predicate_category_actually_filters(self):
        """`movies` is the one of the four the server answers properly, so it
        is the one that can show the filter is applied rather than ignored."""
        everything = self.source.get_channels(self.uuid)[0]
        movies = self.source.get_channels(
            self.uuid, categories=("movies",))[0]
        self.assertTrue(everything, "no channels at all")
        self.assertLess(
            len(movies), len(everything),
            "the movies filter returned the whole line-up, so it was not "
            "applied")

    def test_no_categories_means_no_filter(self):
        self.assertEqual(self.live_tv.category_kwargs([]), {})
        self.assertEqual(
            self.live_tv.category_kwargs(["movies", "sports", "kids", "news"]),
            {},
            "all four must mean 'no filter', not four ANDed flags that match "
            "nothing")


@_e2e.require_server
class GuidePrefsTest(_LiveTvCase):
    """Guide preferences are jellyfin-web's, in jellyfin-web's document.

    Two things are load-bearing and neither is visible without a server: the
    booleans are stored as the *strings* `"true"`/`"false"` (jellyfin-web
    compares with `=== 'true'`, so a JSON boolean reads as false there and the
    setting appears to revert whenever the web client is opened), and saving
    is a read-modify-write of the whole DTO — there is no partial update, so
    writing only our keys drops the home layout that shares the document.
    """

    def setUp(self):
        self.original = dict(self.source.get_live_tv_prefs(
            self.uuid, refresh=True))
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            self.source.save_live_tv_prefs(self.uuid, self.original)
        except Exception:
            pass

    def test_preferences_round_trip(self):
        changed = dict(self.original)
        changed["color_coded"] = not self.original.get("color_coded")
        changed["favorites_first"] = not self.original.get("favorites_first")
        indicators = dict(self.original.get("indicators") or {})
        indicators["hd"] = not indicators.get("hd")
        changed["indicators"] = indicators

        self.source.save_live_tv_prefs(self.uuid, changed)
        read_back = self.source.get_live_tv_prefs(self.uuid, refresh=True)

        self.assertEqual(read_back.get("color_coded"),
                         changed["color_coded"])
        self.assertEqual(read_back.get("favorites_first"),
                         changed["favorites_first"])
        self.assertEqual((read_back.get("indicators") or {}).get("hd"),
                         indicators["hd"])

    def _stored_document(self):
        """The DisplayPreferences document as the *server* holds it.

        Read back raw rather than through the source, so this asserts what is
        on the wire instead of what our own parser made of it — and so it also
        pins the client id. `usersettings` under client `emby` is
        jellyfin-web's legacy namespace, and any other client string reads a
        different, empty preference set: get that wrong and everything still
        "works" while sharing nothing with the web client, which is the entire
        purpose of storing it here.
        """
        from jellyfin_mpv_shim.mpvtk_browser import home_sections
        return self.session._request(
            "/DisplayPreferences/usersettings?userId=%s&client=%s"
            % (self.session.user_id, home_sections.DISPLAY_PREFS_CLIENT))

    def test_booleans_are_stored_as_strings(self):
        changed = dict(self.original)
        changed["color_coded"] = True
        self.source.save_live_tv_prefs(self.uuid, changed)

        custom = (self._stored_document() or {}).get("CustomPrefs") or {}
        self.assertTrue(custom, "no CustomPrefs on the server after a save")
        stored = custom.get("guide-colorcodedbackgrounds")
        self.assertEqual(
            stored, "true",
            "guide booleans must be the strings jellyfin-web compares with "
            "=== 'true'; a JSON boolean reads as false there and the setting "
            "appears to revert whenever the web client is opened. Got %r"
            % (stored,))

    def test_the_document_is_the_one_jellyfin_web_reads(self):
        """`usersettings` / client `emby`, which is what makes these settings
        shared rather than private to this app."""
        from jellyfin_mpv_shim.mpvtk_browser import home_sections
        self.assertEqual(home_sections.DISPLAY_PREFS_CLIENT, "emby")
        document = self._stored_document()
        self.assertTrue(
            document, "no DisplayPreferences document under client 'emby'")
        self.assertIn("CustomPrefs", document)

    def test_saving_guide_prefs_keeps_the_home_layout(self):
        """The same DisplayPreferences document holds both. A partial write
        here is a home screen quietly reset to defaults."""
        layout_before, _excludes = self.source.get_home_prefs(
            self.uuid, refresh=True)
        self.assertTrue(layout_before, "no home layout to preserve")

        changed = dict(self.original)
        changed["color_coded"] = not self.original.get("color_coded")
        self.source.save_live_tv_prefs(self.uuid, changed)

        layout_after, _after = self.source.get_home_prefs(
            self.uuid, refresh=True)
        self.assertEqual(
            layout_after, layout_before,
            "saving guide preferences rewrote the home screen layout")


@_e2e.require_server
class ProgramSectionsTest(_LiveTvCase):
    """The Programs screen's rows, which are six independent guide queries."""

    def test_every_row_comes_back(self):
        sections = self.source.get_program_sections(self.uuid, limit=6)
        self.assertTrue(sections, "the Programs screen has no rows at all")
        titles = [s.get("title") for s in sections]
        self.assertEqual(len(titles), len(set(titles)),
                         "duplicate rows: %s" % titles)
        # One failed row costs only that row, by design — so an all-empty
        # result is the failure mode worth catching.
        self.assertTrue(
            any(s.get("items") for s in sections),
            "every Programs row came back empty")

    def test_on_now_is_actually_on_now(self):
        sections = self.source.get_program_sections(self.uuid, limit=6)
        on_now = [s for s in sections if s.get("key") == "onnow"]
        if not on_now:
            self.skipTest("no On Now row on this server")
        items = on_now[0].get("items") or []
        self.assertTrue(items, "On Now is empty while the tuner has channels")
        now = utcnow()
        for item in items:
            start = self.live_tv.parse_time(item["StartDate"])
            end = self.live_tv.parse_time(item["EndDate"])
            self.assertTrue(
                start <= now <= end,
                "%r is in On Now but runs %s..%s" % (item.get("Name"),
                                                     start, end))


@_e2e.require_server
class TimerTest(_LiveTvCase):
    """Scheduling a recording, and the two questions about it.

    `timer_state` answers *which icon* and `single_timer_state` answers *what
    the Record button should do*; `4bc952c1` drove the button off the former,
    so every episode of a series being recorded offered "Record" again — a
    second timer for a programme that already had one, with no way to skip a
    single showing. The two only diverge once a real series rule exists on the
    server, which is why this is worth doing here.

    Timers are the one thing in this file that writes to the server, so every
    one created is registered for cancellation the moment it exists.

    **This class grants itself a permission and gives it back.** Scheduling a
    recording needs `EnableLiveTvManagement`, which is a *third* Live TV
    permission distinct from `EnableLiveTvAccess` — and stdjflib grants it to
    nobody, not even `qa-admin`, so without this every DVR path is
    unreachable and `POST /LiveTv/Timers` answers 403 for every account on the
    server. Rather than leave the whole surface untested, the class runs as
    `qa-admin`, turns the flag on in `setUpClass` and restores whatever it
    found in `tearDownClass`. The server is disposable by design; that is the
    only reason this is acceptable, and it is why the restore is not
    conditional on the tests passing.
    """

    ACCOUNT = "qa-admin"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._original_policy = None
        policy = cls.session.policy()
        if not policy.get("EnableLiveTvManagement"):
            cls._original_policy = dict(policy)
            granted = dict(policy)
            granted["EnableLiveTvManagement"] = True
            try:
                cls.session.set_policy(granted)
            except Exception as exc:
                cls._original_policy = None
                raise unittest.SkipTest(
                    "cannot grant EnableLiveTvManagement, so no DVR path is "
                    "reachable: %s" % exc)
            # The token's cached policy is stale now; a fresh session sees it.
            cls.session.stop()
            cls.session = _e2e.Session(cls.ACCOUNT)
            cls.source = cls.session.library_source()
            cls.source.get_libraries(_e2e.SOURCE_UUID)

    @classmethod
    def tearDownClass(cls):
        try:
            if cls._original_policy is not None:
                cls.session.set_policy(cls._original_policy)
        except Exception:
            pass
        super().tearDownClass()

    def setUp(self):
        self.api = self.session.api
        self._cancelled = set()
        channels = self.source.get_channels(self.uuid)[0]
        ids = [c["Id"] for c in channels]
        start = utcnow() + datetime.timedelta(minutes=20)
        entries = self.source.get_guide(
            self.uuid, ids, start, start + datetime.timedelta(hours=3))
        future = [e for e in entries
                  if self.live_tv.parse_time(e["StartDate"]) > utcnow()]
        if not future:
            self.skipTest("no future programme to record")
        self.program = future[0]

    def _create_timer(self):
        defaults = self.api.get_new_timer_defaults(
            program_id=self.program["Id"])
        self.api.create_live_tv_timer(defaults)
        timer = self._find_timer()
        self.assertIsNotNone(timer, "the timer was created but is not listed")
        self.addCleanup(self._cancel, timer["Id"])
        return timer

    def _find_timer(self):
        for timer in self.source.get_timers(self.uuid):
            if timer.get("ProgramId") == self.program["Id"]:
                return timer
        return None

    def _cancel(self, timer_id):
        # Idempotent: the happy path cancels explicitly and the cleanup fires
        # too, and a second DELETE logs a 404 that reads like a failure.
        if timer_id in self._cancelled:
            return
        self._cancelled.add(timer_id)
        try:
            self.api.cancel_live_tv_timer(timer_id)
        except Exception:
            pass

    def _reload_program(self):
        return self.source.get_live_program(self.uuid, self.program["Id"])

    def test_a_timer_can_be_scheduled_and_cancelled(self):
        before = self._reload_program()
        self.assertIsNone(
            self.live_tv.single_timer_state(before),
            "this programme already had a timer, so the test starts dirty")

        timer = self._create_timer()

        after = self._reload_program()
        self.assertTrue(
            after.get("TimerId"),
            "the programme carries no TimerId after scheduling a recording")
        self.assertEqual(
            self.live_tv.single_timer_state(after), "timer",
            "the Record button would still offer to record a programme that "
            "already has a timer")
        self.assertEqual(self.live_tv.timer_state(after), "timer")

        self._cancel(timer["Id"])
        self.assertIsNone(self._find_timer(),
                          "the timer is still listed after cancelling")
        self.assertIsNone(
            self.live_tv.single_timer_state(self._reload_program()),
            "the programme still reports a timer after cancelling")

    def test_a_series_rule_and_an_own_timer_are_different_questions(self):
        """The `4bc952c1` distinction, against a real series rule.

        Under a series rule `timer_state` says "series" — the icon — while
        `single_timer_state` reports only whether *this showing* has a timer
        of its own. Driving the button off the first is what broke.
        """
        defaults = self.api.get_new_timer_defaults(
            program_id=self.program["Id"])
        try:
            self.api.create_live_tv_series_timer(defaults)
        except Exception as exc:
            self.skipTest("this programme takes no series rule: %s" % exc)

        series = self.source.get_series_timers(self.uuid)
        if not series:
            self.skipTest("the series rule was not created")
        self.addCleanup(self._cancel_series, series[0]["Id"])

        covered = self._reload_program()
        if not covered.get("SeriesTimerId"):
            self.skipTest("the server did not attach the rule to this showing")

        self.assertEqual(
            self.live_tv.timer_state(covered), "series",
            "a showing covered by a series rule must draw the series icon")
        # The button's question is answered independently: whatever the icon
        # says, this is about whether the showing has a timer of its own.
        self.assertEqual(
            self.live_tv.single_timer_state(covered),
            "timer" if covered.get("TimerId") else None,
            "single_timer_state must read TimerId alone and never consult "
            "SeriesTimerId")

    def _cancel_series(self, timer_id):
        try:
            self.api.cancel_live_tv_series_timer(timer_id)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
