"""The 12/24-hour clock setting, and every screen that prints a time of day.

Two halves, and the second is the one worth having. `timefmt.clock` is four
lines and easy to get right; what was actually wrong before it existed is that
the *three* places showing a wall clock each called `strftime` themselves, so
a setting wired into one or two of them would look implemented and disagree
with itself on screen. So each call site is exercised through the code that
draws it, with the setting flipped, rather than by asserting that it calls the
helper.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

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


def _iso_at(hour, minute):
    """A Jellyfin UTC timestamp for ``hour:minute`` **local time**.

    Built from local rather than written as a UTC literal, because
    `live_tv.parse_time` answers in local time and every assertion below is
    a literal clock reading. A hard-coded "20:00:00Z" reads as 20:00 only
    at UTC, and this suite runs wherever it runs -- measured failing under
    Asia/Tokyo, Pacific/Kiritimati, Australia/Adelaide and Asia/Kathmandu.
    Same shape as `tests/test_live_tv.py`'s own helper.
    """
    local = _at(hour, minute).astimezone()
    return local.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.0000000Z")


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

    def _program(self):
        return {"StartDate": _iso_at(20, 0), "EndDate": _iso_at(20, 30)}

    def test_an_air_time_range_follows_the_setting(self):
        """Literal readings, not `timefmt.clock` compared against itself:
        an expectation computed by the code under test agrees with whatever
        answer that code gives."""
        self.set_12h(False)
        self.assertEqual(live_tv.air_time_label(self._program()),
                         "20:00 - 20:30")
        self.set_12h(True)
        self.assertEqual(live_tv.air_time_label(self._program()),
                         "8:00 PM - 8:30 PM")

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

    def _program(self):
        return {"Type": "Program", "Name": "News", "ChannelName": "Fake One",
                "StartDate": _iso_at(20, 0), "EndDate": _iso_at(20, 30)}

    def _tile(self, on):
        from jellyfin_mpv_shim.mpvtk_browser import components

        self.set_12h(on)
        return components.tile_lines(self._program())

    def test_the_caption_says_the_time_in_the_chosen_format(self):
        self.assertEqual(self._tile(False)[1],
                         "Fake One   ·   20:00 - 20:30")
        self.assertEqual(self._tile(True)[1],
                         "Fake One   ·   8:00 PM - 8:30 PM")

    def test_the_strip_cache_key_changes_on_its_own(self):
        """The property the retag was added for, held by the key instead.

        Asserted against the real `Tile` and the real `_tile_key`, not a
        stand-in of either: a hand-built namespace of the fields I believe
        the key reads is a test of my belief, and it would keep passing on
        the day the key stopped reading `subtitle`.
        """
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile

        def key(on):
            title, subtitle = self._tile(on)
            return StripStore._tile_key(
                None, Tile(key="p1", title=title, subtitle=subtitle))

        self.assertNotEqual(key(False), key(True))

    def test_and_it_is_the_subtitle_that_carries_it(self):
        """Guards the test above: if the air time stopped being part of the
        caption the assertion would still pass on some other field, and
        this class would be testing nothing it is named after."""
        self.assertIn("20:00", self._tile(False)[1])
        self.assertIn("8:00 PM", self._tile(True)[1])

    def test_it_is_not_a_restart_setting(self):
        """Went with the class that tested `apply_clock_format` and is not
        about it: "Requires restart" means literally nothing has happened,
        and something has. Adding `clock_12h` to `RESTART_REQUIRED` would
        put a banner over a control that applies live, and nothing else in
        the suite would notice."""
        from jellyfin_mpv_shim.mpvtk_browser import config

        self.assertNotIn("clock_12h", config.RESTART_REQUIRED)

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
