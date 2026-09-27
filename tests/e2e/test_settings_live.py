"""Slice S6, row 54: the Settings screen, at the keyboard of the shipped app.

- a change on each form tab (General, Browse, Playback) is written to
  conf.json, drawn as a tick, and both survive a relaunch;
- search finds a setting by its label;
- the Logs tab tails without wedging: its repaints stay bounded (it polls
  and re-renders on new lines, and a log line per render would feed back).

The restart banner and the relaunch are app_lifecycle's AnotherLanguageTest.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _flows  # noqa: E402
from test_playback_lifecycle import _PlaybackCase  # noqa: E402

#: One harmless checkbox per form tab: nothing here changes what a relaunch
#: can reach (enable_gui, say, would leave the next launch without a UI).
FORM_TAB_KEYS = (("general", "raise_mpv"), ("browse", "clock_12h"),
                 ("playback", "skip_intro_on_seek"))


def ticked(frame, node_id):
    """Whether the checkbox ``node_id`` is drawn checked: a Checkbox is a
    Row whose box holds a "✓" text only when checked (widgets.Checkbox),
    so the tick is a text node inside the row's rect."""
    box = _app.node(frame, node_id)
    if not box or box.get("w") is None:
        return None
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["w"], y0 + box["h"]
    return any(n.get("text") == "✓" and x0 <= n.get("x", -1) <= x1
               and y0 <= n.get("y", -1) <= y1
               for n in frame.get("nodes", []))


class _SettingsCase(_PlaybackCase):

    def conf(self):
        with open(os.path.join(self.app.config_dir, "conf.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def to_setting(self, tab, key):
        """The tab, then keyboard focus on its ``set-<key>`` control. DOWN,
        not TAB: TAB walks only what is on screen (the register, "TAB
        stops at the fold")."""
        _flows.open_settings_tab(self.app, tab)
        self.app.wait_for(lambda f: _app.node(f, "set-" + key), timeout=15,
                          what="set-%s on the %s tab" % (key, tab))
        return self.app.move_to("set-" + key, key="DOWN")


class AChangeOnEachTabSurvivesARelaunchTest(_SettingsCase):

    def test_toggled_by_keys_then_relaunched(self):
        want = {}
        for tab, key in FORM_TAB_KEYS:
            f = self.to_setting(tab, key)
            before = bool(self.conf().get(key))
            self.assertEqual(before, ticked(f, "set-" + key),
                             "%s: the tick disagrees with conf.json" % key)
            self.app.key("ENTER")
            want[key] = not before
            self.app.wait_for(
                lambda f: ticked(f, "set-" + key) is want[key], timeout=10,
                what="%s's tick to move (a Checkbox needs a repaint)" % key)
            self.assertEqual(want[key], bool(self.conf().get(key)),
                             "%s: not written to conf.json" % key)

        self.app = _flows.relaunch(self.app, self.relay)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the relaunch")
        for tab, key in FORM_TAB_KEYS:
            f = self.to_setting(tab, key)
            self.assertEqual(want[key], bool(self.conf().get(key)),
                             "%s: conf.json lost the change" % key)
            self.assertIs(want[key], ticked(f, "set-" + key),
                          "%s: drawn as it was before the change" % key)
        self.assertEqual(0, self.app.quit(timeout=30))


class SearchFindsASettingTest(_SettingsCase):

    def test_by_its_label(self):
        _flows.open_settings_tab(self.app, "general")
        self.assertFalse(_app.shown(self.app.frame(), "set-clock_12h"),
                         "the premise: the setting is on another tab")
        self.app.type_into("set-search-box", "12-Hour")
        self.app.wait_for(lambda f: _app.shown(f, "set-clock_12h"),
                          timeout=15, what="the search result")
        self.assertEqual(0, self.app.quit(timeout=30))


class TheLogsTabTailsWithoutWedgingTest(_SettingsCase):

    #: Repaints in 10 s on an idle Logs tab: measured 0 on both backends
    #: (2026-09-27). The poller re-renders only when lines changed, so a
    #: render that logged would feed it and repaint every poll.
    MAX_REPAINTS = 3

    def test_repaints_stay_bounded(self):
        _flows.open_settings_tab(self.app, "logs")
        self.app.wait_for(lambda f: _app.shown(f, "log-refresh"), timeout=15,
                          what="the Logs tab")
        time.sleep(2)                         # let the first load settle
        start = self.app.frame()["rev"]
        time.sleep(10)
        repaints = self.app.frame()["rev"] - start
        self.assertLessEqual(repaints, self.MAX_REPAINTS,
                             "an idle Logs tab repainted %d times in 10 s"
                             % repaints)
        # And it still answers the keyboard.
        self.app.move_to("stab-general")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "set-raise_mpv"),
                          timeout=15, what="General, from the Logs tab")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
