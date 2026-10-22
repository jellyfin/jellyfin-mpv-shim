"""An invalid ``aprefer``/``sprefer`` regex must not stop playback.

``amatch``/``smatch`` already guard ``re.compile``; the two bias fields did
not, so a typo like ``"(commentary"`` raised ``re.error`` out of
``resolve_tracks_for_negotiation`` and every item reaching that rule failed
to start. A bias that cannot compile biases nothing.
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

from jellyfin_mpv_shim import language_config

SOURCE = {
    "MediaStreams": [
        {"Type": "Audio", "Index": 1, "Language": "eng", "Title": "Main"},
        {"Type": "Audio", "Index": 2, "Language": "eng", "Title": "Commentary"},
        {"Type": "Subtitle", "Index": 3, "Language": "eng", "Title": "Full"},
        {"Type": "Subtitle", "Index": 4, "Language": "eng", "Title": "Signs"},
    ]
}


class InvalidPreferRegexTest(unittest.TestCase):
    def _apply(self, spec):
        rules = language_config.parse_language_config(spec)
        with self.assertLogs("language_config", level="WARNING"):
            return language_config.apply(rules, SOURCE, {})

    def test_invalid_aprefer_with_alang_falls_back_to_first_track(self):
        self.assertEqual(
            self._apply([{"alang": "eng", "aprefer": "(commentary"}]), (1, None)
        )

    def test_invalid_aprefer_alone_falls_back_to_first_track(self):
        self.assertEqual(self._apply([{"aprefer": "(commentary"}]), (1, None))

    def test_invalid_sprefer_with_slang_still_picks_a_subtitle(self):
        aid, sid = self._apply([{"slang": "eng", "sprefer": "[signs"}])
        self.assertIsNone(aid)
        self.assertIn(sid, (3, 4))

    def test_invalid_sprefer_alone_still_picks_a_subtitle(self):
        aid, sid = self._apply([{"sprefer": "[signs"}])
        self.assertIn(sid, (3, 4))

    def test_valid_aprefer_still_biases(self):
        rules = language_config.parse_language_config(
            [{"alang": "eng", "aprefer": "commentary"}]
        )
        self.assertEqual(language_config.apply(rules, SOURCE, {}), (2, None))


if __name__ == "__main__":
    unittest.main()
