"""The 12/24-hour clock setting, and every screen that prints a time of day.

Two halves, and the second is the one worth having. `timefmt.clock` is four
lines and easy to get right; what was actually wrong before it existed is that
the *three* places showing a wall clock each called `strftime` themselves, so
a setting wired into one or two of them would look implemented and disagree
with itself on screen. So each call site is exercised through the code that
draws it, with the setting flipped, rather than by asserting that it calls the
helper.
"""

import datetime
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import settings                        # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import live_tv, timefmt       # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser       # noqa: E402

from tests._scene_snapshot import FROZEN, frozen_clock             # noqa: E402
from tests._shell_harness import (                                 # noqa: E402
    FakeSource, HudController, _SyncPool, build_scene, detail_page)


class ClockSettingMixin:
    """Restores the setting, whatever the test did to it.

    The single global `conf.settings` is shared by every module in the
    process, so a test that leaves it flipped fails a later one in the full
    run and passes when its own module is selected alone.
    """

    def setUp(self):
        super().setUp()
        self._saved = settings.clock_12h
        self.addCleanup(self._restore)

    def _restore(self):
        settings.clock_12h = self._saved

    def set_12h(self, on):
        settings.clock_12h = on


def _at(hour, minute):
    return datetime.datetime(2026, 9, 2, hour, minute)


class TestClock(ClockSettingMixin, unittest.TestCase):
    #: (hour, minute) -> (24-hour, 12-hour). Written out rather than
    #: computed, so the expectations cannot agree with the code by
    #: construction.
    CASES = {
        (0, 0): ("00:00", "12:00 AM"),      # midnight is 12 AM, not 0 AM
        (0, 7): ("00:07", "12:07 AM"),
        (9, 5): ("09:05", "9:05 AM"),       # no leading zero on the hour...
        (11, 59): ("11:59", "11:59 AM"),
        (12, 0): ("12:00", "12:00 PM"),     # noon is 12 PM, not 0 PM
        (12, 1): ("12:01", "12:01 PM"),
        (13, 0): ("13:00", "1:00 PM"),
        (20, 30): ("20:30", "8:30 PM"),
        (23, 59): ("23:59", "11:59 PM"),
    }

    def test_the_two_formats(self):
        for (hour, minute), (h24, h12) in self.CASES.items():
            with self.subTest(at=(hour, minute)):
                self.set_12h(False)
                self.assertEqual(timefmt.clock(_at(hour, minute)), h24)
                self.set_12h(True)
                self.assertEqual(timefmt.clock(_at(hour, minute)), h12)

    def test_the_minute_keeps_its_leading_zero_when_the_hour_loses_one(self):
        """The whole point of not using "%I:%M": dropping the pad has to be
        the hour's alone. "9:5 PM" is not a time."""
        self.set_12h(True)
        self.assertEqual(timefmt.clock(_at(9, 5)), "9:05 AM")

    def test_no_time_is_no_string(self):
        for on in (False, True):
            self.set_12h(on)
            self.assertEqual(timefmt.clock(None), "")

    def test_the_setting_is_read_per_call(self):
        """It applies live, so a value bound once — at import, or into a
        cached formatter — is a control that does nothing until a restart.
        Three flips, not one: a value latched on first use passes a single
        flip in whichever direction it was latched in."""
        when = _at(20, 30)
        seen = []
        for on in (False, True, False, True):
            self.set_12h(on)
            seen.append(timefmt.clock(when))
        self.assertEqual(seen, ["20:30", "8:30 PM", "20:30", "8:30 PM"])

    def test_a_translation_can_put_the_marker_first(self):
        """zh/ja/ko write the day period BEFORE the time (下午8:30), and a
        format string baked into the code cannot express that -- translating
        only the marker gives them "8:30 下午". So the whole pattern is a
        message, and this asserts a translator can actually reorder it."""
        renderings = {}
        real = timefmt._p
        try:
            for lang, am, pm in (("zh", "上午", "下午"),
                                 ("ja", "午前", "午後")):
                timefmt._p = (
                    lambda _ctx, msg, am=am, pm=pm:
                    "{period}{time}" if "{" in msg
                    else (am if msg == "AM" else pm))
                self.set_12h(True)
                renderings[lang] = timefmt.clock(_at(20, 30))
        finally:
            timefmt._p = real
        self.assertEqual(renderings, {"zh": "下午8:30", "ja": "午後8:30"})

    def test_clock_epoch_agrees_with_clock(self):
        when = _at(20, 30)
        for on in (False, True):
            self.set_12h(on)
            self.assertEqual(timefmt.clock_epoch(when.timestamp()),
                             timefmt.clock(when))


class TestLiveTvFollowsTheSetting(ClockSettingMixin, unittest.TestCase):
    """The guide headings and air-time labels — the biggest consumer."""

    START = "2026-09-02T20:00:00.0000000Z"
    END = "2026-09-02T20:30:00.0000000Z"

    def _program(self):
        # Parsed and rendered in local time, so the assertions below are
        # written against what parse_time answers rather than against the
        # UTC string above.
        return {"StartDate": self.START, "EndDate": self.END}

    def _expected(self, key):
        start = live_tv.parse_time(self.START)
        end = live_tv.parse_time(self.END)
        self.set_12h(key)
        return "%s - %s" % (timefmt.clock(start), timefmt.clock(end))

    def test_an_air_time_range_follows_the_setting(self):
        for on in (False, True):
            with self.subTest(clock_12h=on):
                want = self._expected(on)
                self.set_12h(on)
                self.assertEqual(
                    live_tv.air_time_label(self._program()), want)
                self.assertEqual(("AM" in want or "PM" in want), on,
                                 "the fixture did not change format at all")

    def test_a_guide_column_heading_follows_the_setting(self):
        slot = _at(20, 0)
        self.set_12h(False)
        self.assertEqual(live_tv.fmt_time(slot), "20:00")
        self.set_12h(True)
        self.assertEqual(live_tv.fmt_time(slot), "8:00 PM")

    def test_the_day_label_is_not_a_clock(self):
        """`fmt_day` shares the module and must not follow this: a date has
        no 12-hour form, and a switch that quietly reworded it would be a
        second thing happening under one control."""
        for on in (False, True):
            self.set_12h(on)
            self.assertEqual(live_tv.fmt_day(_at(20, 0)),
                             _at(20, 0).strftime("%a, %b %d"))


class TestEndsAtFollowsTheSetting(ClockSettingMixin, unittest.TestCase):
    """The two "Ends at" labels: the detail page's, and the HUD's.

    They are computed differently — one from a resume position and
    `datetime.now()`, the other from the remaining runtime over the playback
    speed and `time.time()` — which is exactly why both are here.
    """

    def _item(self):
        return {"Id": "m1", "Type": "Movie",
                "RunTimeTicks": 60 * 60 * 10000000,     # one hour
                "MediaSources": [{"Id": "ms", "Container": "mkv"}]}

    def _detail_line(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        return detail_page(b, {})._media_info_line(self._item())

    def _hud_texts(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        b._browsing = False
        b.hud.shown = True
        b.hud.state = {"stopped": False, "is_audio": False, "title": "Movie",
                       "position": 50.0, "duration": 100.0, "paused": False}
        nodes, _handlers = build_scene(b, (1280, 720))
        return [n.get("text") or "" for n in nodes]

    def _ends_at(self, texts):
        found = [t for t in texts if "Ends at" in t]
        self.assertEqual(len(found), 1, "expected one Ends-at label: %r"
                         % (found,))
        return found[0]

    def test_the_detail_page_label(self):
        with frozen_clock():
            self.set_12h(False)
            self.assertIn("Ends at %s" % timefmt.clock(FROZEN + _HOUR),
                          self._detail_line())
            self.set_12h(True)
            line = self._detail_line()
        self.assertIn("Ends at %s" % timefmt.clock(FROZEN + _HOUR), line)
        self.assertRegex(line, r"Ends at \d{1,2}:\d\d [AP]M")

    def test_the_player_controls_label(self):
        with frozen_clock():
            self.set_12h(False)
            plain = self._ends_at(self._hud_texts())
            self.set_12h(True)
            twelve = self._ends_at(self._hud_texts())
        self.assertRegex(plain, r"Ends at \d\d:\d\d$")
        self.assertRegex(twelve, r"Ends at \d{1,2}:\d\d [AP]M$")

    def test_both_labels_name_the_same_clock(self):
        """The failure this file exists for: one call site converted and the
        other left on `strftime`, which reads as "the setting half works".

        Asserted on the marker rather than on a literal hour -- the frozen
        clock is 03:04, so "PM" would be the wrong expectation and "AM"
        would pass against a label that never changed.
        """
        with frozen_clock():
            self.set_12h(True)
            twelve = (self._detail_line(), self._ends_at(self._hud_texts()))
            self.set_12h(False)
            plain = (self._detail_line(), self._ends_at(self._hud_texts()))
        for text in twelve:
            self.assertRegex(text, r"Ends at \d{1,2}:\d\d [AP]M")
        for text in plain:
            self.assertRegex(text, r"Ends at \d\d:\d\d")
            self.assertNotRegex(text, r"\d\s*[AP]M")


class TestTheTilesFollowTheSetting(ClockSettingMixin, unittest.TestCase):
    """A Live TV air time is part of a tile's caption, and a caption is
    baked into the composited strip. So the question this class answers is
    what makes a cached row unreachable when the format changes.

    The answer is: nothing extra has to. The caption text is *in* the
    strip's cache key, so a flipped setting produces a different key on the
    next render and the row recomposites on its own. The first draft of
    this feature added an `apply_clock_format` that retagged the whole
    store, on the reasoning that a baked caption cannot be repainted --
    true of `logo_legibility`, which changes how a tile is DRAWN, and false
    here, where it changes what the tile SAYS. It cost every cached row in
    the app -- movie posters included -- a recomposite on the way back from
    Settings, and it was invisible to a test that stubbed the method out
    and asserted the stub had been called.
    """

    PROGRAM = {"Type": "Program", "Name": "News", "ChannelName": "Fake One",
               "StartDate": "2026-09-02T20:00:00Z",
               "EndDate": "2026-09-02T20:30:00Z"}

    def _tile(self, on):
        from jellyfin_mpv_shim.mpvtk_browser import components

        self.set_12h(on)
        return components.tile_lines(dict(self.PROGRAM))

    def test_the_caption_says_the_time_in_the_chosen_format(self):
        self.assertIn(" - ", self._tile(False)[1])
        self.assertNotIn("PM", self._tile(False)[1])
        self.assertIn("PM", self._tile(True)[1])

    def test_the_strip_cache_key_changes_on_its_own(self):
        """The property the retag was added for, held by the key instead.
        Asserted against `StripStore._tile_key` itself rather than against
        a belief about it."""
        from types import SimpleNamespace

        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        def key(on):
            _title, subtitle = self._tile(on)
            return StripStore._tile_key(None, SimpleNamespace(
                key="p1", poster_tag="", poster=None, title="News",
                subtitle=subtitle, subtitle2="", watched=False, badge=0,
                progress=0, downloaded=False, kind="", recording=False,
                record="", glyph="", contain=False, live=True, sources=0))

        self.assertNotEqual(key(False), key(True))

    def test_saving_it_does_not_retag_the_whole_store(self):
        """A retag makes every cached row in the app unreachable, including
        the ones with no clock anywhere in them. Pinned because adding one
        back is the obvious-looking repair for a caption that looks stale.
        """
        from tests._shell_harness import FakeConfig

        cfg = FakeConfig()
        cfg.schema["clock_12h"] = "bool"
        cfg.values["clock_12h"] = False
        b = MpvtkBrowser(app=None, source=FakeSource(), config=cfg)
        b._pool = _SyncPool()
        before = b.strips.tag
        b._set_setting("clock_12h", True)
        self.assertEqual(b.strips.tag, before)


_HOUR = datetime.timedelta(hours=1)


if __name__ == "__main__":
    unittest.main()
