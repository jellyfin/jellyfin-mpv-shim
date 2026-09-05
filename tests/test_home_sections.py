"""Home-screen section layout: the jellyfin-web interop rules.

The layout is shared with jellyfin-web through DisplayPreferences, so the
encoding is not ours to choose. These pin the three rules that silently break
interop when they drift: empty means the slot's default (not "none"), only a
literal "none" blanks a slot, and a default-valued slot is written back as "".
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
        """A section we cannot draw must not be lost: the same layout is
        read by jellyfin-web, and rewriting one to "none" would degrade the
        home screen of a user who only ever opened the shim.

        The example has had to move twice, which is the point of writing it
        down: activerecordings became drawable with the Live TV screens, and
        resumebook with book support. LIBRARY_BUTTONS is what is left -- and
        unlike those two it is not a gap, it is a second styling of the
        Libraries row we deliberately do not offer."""
        layout = hs.resolve_layout({"homesection0": hs.LIBRARY_BUTTONS})
        self.assertEqual(layout[0], hs.LIBRARY_BUTTONS)
        self.assertNotIn(hs.LIBRARY_BUTTONS, hs.SUPPORTED)

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
        self.assertEqual(hs.stages_for([hs.LIBRARY_BUTTONS, "notasection"]),
                         set())

    def test_continue_reading_is_an_above_the_fold_fetch(self):
        """One request, like the other two resume rows, and in the stock
        layout -- so it has to be in the first batch or the home screen
        would draw with a gap where it goes and fill it a beat later."""
        self.assertEqual(hs.stages_for([hs.RESUME_BOOK]), {"primary"})

    def test_active_recordings_is_an_above_the_fold_fetch(self):
        # It became drawable with the Live TV screens; it is one request and
        # belongs with the other primary rows, gated on the tuner check.
        self.assertEqual(hs.stages_for([hs.ACTIVE_RECORDINGS]), {"primary"})

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
