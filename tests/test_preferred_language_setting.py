"""`preferred_language` is offered only while a Language Preference preset
reads it.

It feeds `language_config.preset_rules` and nothing else, so under Unset and
Custom -- Custom being the default -- it is a box that changes nothing.
Hidden rather than disabled, per docs/settings-curation.md §2.
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
from unittest import mock

sys.argv = [sys.argv[0]]      # conffile reaches args.get_args() on import

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.settings.base import (  # noqa: E402
    SettingsBase,
)

KEY = "preferred_language"
PRESETS = ("dubbed_shows", "subbed_shows", "dubbed_all", "subbed_all")


def _groups(preference):
    with mock.patch.object(settings, "language_preference", preference):
        return dict(cfg.sections())


def _drawn(preference):
    return {k for keys in _groups(preference).values() for k in keys}


class VisibilityTest(unittest.TestCase):
    def test_every_preset_that_builds_rules_offers_it(self):
        for preference in PRESETS:
            with self.subTest(preference=preference):
                keys = _groups(preference)["Subtitles & Languages"]
                self.assertEqual(
                    keys[keys.index("language_preference"):][:2],
                    ["language_preference", KEY])

    def test_unset_and_custom_hide_it_from_every_group(self):
        """Every group, Advanced included: a key that is hidden but was
        never seeded into `curated` goes on being drawn, and asserting only
        that it left its own group cannot see that."""
        for preference in ("unset", "custom"):
            with self.subTest(preference=preference):
                self.assertNotIn(KEY, _drawn(preference))

    def test_the_dropdown_offers_exactly_these_values(self):
        """Pins PRESETS to the dropdown, so a new preset makes this file
        decide where it belongs rather than going untested."""
        offered = {v for _l, v in cfg.LABELED_ENUMS["language_preference"]}
        self.assertEqual(offered, set(PRESETS) | {"unset", "custom"})

    def test_it_has_a_note(self):
        self.assertTrue(cfg.NOTES.get(KEY))


class _Form(SettingsBase):
    """The real write path against the real config module, minus the
    browser. `_set_setting` reaches for these and nothing else here."""

    app = None
    _config_obj = None

    def __init__(self):
        self.repaints = 0
        self.status = None

    def set_status(self, text):
        self.status = text

    def invalidate(self, *a, **k):
        self.repaints += 1


class PickingAPresetTest(unittest.TestCase):
    def test_the_row_follows_the_dropdown_across_several_picks(self):
        """Through the real setter, several times in both directions, with a
        repaint each time: the row appears and disappears only because the
        form is rebuilt, so a pick that saved without redrawing would leave
        the screen showing the previous answer."""
        form = _Form()
        with mock.patch.object(settings, "language_preference", "custom"), \
                mock.patch.object(settings, "language_config", None), \
                mock.patch.object(settings, "save"):
            for preference in ("dubbed_all", "custom", "subbed_shows",
                               "unset", "subbed_all"):
                before = form.repaints
                form._set_setting("language_preference", preference)
                self.assertEqual(settings.language_preference, preference)
                self.assertGreater(form.repaints, before, preference)
                drawn = {k for _t, keys in cfg.sections() for k in keys}
                self.assertEqual(KEY in drawn, preference in PRESETS,
                                 preference)


if __name__ == "__main__":
    unittest.main()
