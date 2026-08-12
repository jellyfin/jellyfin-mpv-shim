"""Every mpv property the playback-info panel asks for is one mpv has.

This exists because the failure mode is *silent*. Both backends turn an
unknown attribute into a property read, so a misspelled name raises at the
read, `player_stats` drops it with the rest of the tolerated failures, and
the row just never appears — on a panel where a missing row is
indistinguishable from a counter mpv had nothing to say about, which is a
real and common state (no `estimated-vf-fps` before the first frame, none of
the video properties during audio).

`mpv --list-properties` answers it offline, in milliseconds, with no
playback and no window. Skipped when mpv is not on PATH, since the unit
suite must not require it.
"""

import re
import shutil
import subprocess
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.gateway.hud import HudMixin  # noqa: E402


def _mpv_properties():
    exe = shutil.which("mpv")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--list-properties"], check=True,
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    names = set()
    for line in out.splitlines():
        m = re.match(r"\s+([a-z0-9][a-z0-9/-]*)", line)
        if m:
            names.add(m.group(1))
    return names or None


class MpvStatPropertiesTest(unittest.TestCase):

    def test_the_names_are_real_mpv_properties(self):
        known = _mpv_properties()
        if known is None:
            self.skipTest("mpv is not on PATH")
        missing = [prop for prop, _key in HudMixin._MPV_STATS
                   if prop not in known]
        self.assertEqual(missing, [],
                         "not mpv properties (a typo here shows as a row "
                         "that never appears): %r" % (missing,))

    def test_the_keys_are_unique(self):
        # Two properties writing one key is the same silent loss by another
        # route: the panel draws one row and the other measurement is gone.
        keys = [key for _prop, key in HudMixin._MPV_STATS]
        self.assertEqual(sorted(keys), sorted(set(keys)))

    def test_the_underscore_spelling_round_trips(self):
        # The read converts "frame-drop-count" to "frame_drop_count"; a
        # property whose real name contains a slash (e.g. "video-params/w")
        # would not survive that and needs its own read.
        for prop, _key in HudMixin._MPV_STATS:
            self.assertNotIn("/", prop,
                             "%r cannot be read by attribute" % prop)


if __name__ == "__main__":
    unittest.main()
