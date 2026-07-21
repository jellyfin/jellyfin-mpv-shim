"""Home-screen section layout: the jellyfin-web interop rules.

The layout is shared with jellyfin-web through DisplayPreferences, so the
encoding is not ours to choose. These pin the three rules that silently break
interop when they drift: empty means the slot's default (not "none"), only a
literal "none" blanks a slot, and a default-valued slot is written back as "".
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import home_sections as hs  # noqa: E402


class TestResolveLayout(unittest.TestCase):

    def test_no_prefs_is_the_default_layout(self):
        self.assertEqual(hs.resolve_layout({}), list(hs.DEFAULT_LAYOUT))
        self.assertEqual(hs.resolve_layout(None), list(hs.DEFAULT_LAYOUT))

    def test_empty_slot_falls_back_to_that_slots_default(self):
        """Not to "none", and not to slot 0's default."""
        layout = hs.resolve_layout({"homesection0": "", "homesection1": ""})
        self.assertEqual(layout[0], hs.LIBRARIES)
        self.assertEqual(layout[1], hs.RESUME)

    def test_only_literal_none_blanks_a_slot(self):
        layout = hs.resolve_layout({"homesection1": "none"})
        self.assertEqual(layout[1], hs.NONE)

    def test_folders_is_remapped_to_slot_zeros_default(self):
        """"folders" is a pre-10.x alias. jellyfin-web maps it to slot 0's
        default, not the containing slot's — so in slot 5 it becomes
        smalllibrarytiles, NOT nextup."""
        layout = hs.resolve_layout({"homesection5": "folders"})
        self.assertEqual(layout[5], hs.LIBRARIES)
        self.assertEqual(hs.DEFAULT_LAYOUT[5], hs.NEXT_UP)   # guards the point

    def test_unsupported_values_survive_resolution(self):
        """We cannot draw Live TV, but we must not lose it: the same layout is
        read by jellyfin-web."""
        layout = hs.resolve_layout({"homesection0": hs.LIVE_TV})
        self.assertEqual(layout[0], hs.LIVE_TV)
        self.assertNotIn(hs.LIVE_TV, hs.SUPPORTED)

    def test_values_are_stringified_and_stripped(self):
        layout = hs.resolve_layout({"homesection0": "  resume  "})
        self.assertEqual(layout[0], hs.RESUME)

    def test_length_is_always_slot_count(self):
        self.assertEqual(len(hs.resolve_layout({})), hs.SLOT_COUNT)


class TestLayoutToPrefs(unittest.TestCase):

    def test_default_slots_are_written_as_empty(self):
        prefs = hs.layout_to_prefs(list(hs.DEFAULT_LAYOUT))
        self.assertEqual(set(prefs.values()), {""})

    def test_non_default_slots_are_written_literally(self):
        layout = list(hs.DEFAULT_LAYOUT)
        layout[1] = hs.LATEST
        prefs = hs.layout_to_prefs(layout)
        self.assertEqual(prefs["homesection1"], hs.LATEST)
        self.assertEqual(prefs["homesection0"], "")

    def test_none_is_written_literally_not_elided(self):
        """Slot 1 defaults to resume, so blanking it must persist "none" —
        writing "" would resurrect Continue Watching on the next read."""
        layout = list(hs.DEFAULT_LAYOUT)
        layout[1] = hs.NONE
        self.assertEqual(hs.layout_to_prefs(layout)["homesection1"], hs.NONE)

    def test_round_trip_is_stable(self):
        layout = [hs.LATEST, hs.NONE, hs.LIBRARIES, hs.RESUME_AUDIO,
                  hs.LIVE_TV, hs.NEXT_UP, hs.RESUME, hs.NONE, hs.NONE,
                  hs.NONE]
        self.assertEqual(hs.resolve_layout(hs.layout_to_prefs(layout)), layout)

    def test_short_layout_is_padded_with_slot_defaults(self):
        prefs = hs.layout_to_prefs([hs.NONE])
        self.assertEqual(prefs["homesection0"], hs.NONE)
        self.assertEqual(len(prefs), hs.SLOT_COUNT)


class TestStages(unittest.TestCase):

    def test_libraries_needs_no_fetch(self):
        self.assertEqual(hs.stages_for([hs.LIBRARIES]), {"local"})

    def test_latest_is_its_own_stage(self):
        """The per-library fan-out is the slow half and sits below the fold."""
        self.assertEqual(hs.stages_for([hs.LATEST]), {"latest"})

    def test_unsupported_sections_contribute_no_work(self):
        self.assertEqual(hs.stages_for([hs.LIVE_TV, hs.RESUME_BOOK]), set())

    def test_default_layout_needs_both_fetch_stages(self):
        stages = hs.stages_for(hs.DEFAULT_LAYOUT)
        self.assertIn("primary", stages)
        self.assertIn("latest", stages)


class TestSectionLabels(unittest.TestCase):

    def test_every_offered_value_is_supported(self):
        """The dropdown must not offer something the renderer drops."""
        for value, _label in hs.section_labels():
            self.assertIn(value, hs.SUPPORTED)

    def test_every_supported_value_is_offered(self):
        offered = {v for v, _l in hs.section_labels()}
        self.assertEqual(offered, set(hs.SUPPORTED))

    def test_every_stage_key_is_supported(self):
        for value in hs.STAGE:
            self.assertIn(value, hs.SUPPORTED)


if __name__ == "__main__":
    unittest.main()
