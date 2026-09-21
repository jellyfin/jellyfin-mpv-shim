"""Per-library view settings, shared with jellyfin-web.

The setting behind the Home Videos shape mismatch: a library remembers
which image type to draw its items with, and web skips its median-aspect
rule entirely when one is set.
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

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser import view_prefs  # noqa: E402


class KeyTest(unittest.TestCase):
    """The key is not fully knowable from web's source -- getSettingsKey
    appends a route type only when the route carried one, so the same
    library reached two ways has two keys. Hence candidates, not a guess."""

    def test_the_observed_key_is_a_candidate(self):
        """Seen in the wild on a Home Videos library:
        items-<parentId>-Folder-imageType."""
        keys = view_prefs.keys_for("PID", "homevideos")
        self.assertIn("items-PID-Folder-imageType", keys)

    def test_the_bare_key_is_last_not_first(self):
        """A typed key is more specific, and web writes one whenever the
        route had a type. Reading the bare key first would shadow it."""
        keys = view_prefs.keys_for("PID", "homevideos")
        self.assertEqual(keys[-1], "items-PID-imageType")
        self.assertGreater(len(keys), 1)

    def test_an_unknown_collection_type_still_gets_the_bare_key(self):
        self.assertEqual(view_prefs.keys_for("PID", "channels"),
                         ["items-PID-imageType"])

    def test_no_parent_means_no_keys(self):
        self.assertEqual(view_prefs.keys_for(None, "movies"), [])


class ResolveSortTest(unittest.TestCase):
    """#758: the sort lived on the route and died with the navigation.

    Web's key names, because the point of persisting it here is that the two
    clients agree: `userSettings.js` writes `<key>-sortby` and
    `<key>-sortorder`, and a save has to land on the key the read came from.
    """

    def test_an_untouched_library_stores_nothing(self):
        got = view_prefs.resolve_sort({}, "lib1", "movies")

        self.assertEqual({"sortby": (None, None),
                          "sortorder": (None, None)}, got)

    def test_webs_own_keys_are_read(self):
        prefs = {"items-lib1-sortby": "DateCreated",
                 "items-lib1-sortorder": "Descending"}

        got = view_prefs.resolve_sort(prefs, "lib1", "movies")

        self.assertEqual(("DateCreated", "items-lib1-sortby"), got["sortby"])
        self.assertEqual(("Descending", "items-lib1-sortorder"),
                         got["sortorder"])

    def test_a_typed_key_wins_over_the_bare_one(self):
        """The same precedence every other setting here has, and the reason
        the resolver returns candidates rather than one key."""
        prefs = {"items-lib1-sortby": "SortName",
                 "items-lib1-Movie-sortby": "CommunityRating"}

        got = view_prefs.resolve_sort(prefs, "lib1", "movies")

        self.assertEqual(("CommunityRating", "items-lib1-Movie-sortby"),
                         got["sortby"])

    def test_the_two_halves_can_come_from_different_keys(self):
        """Web writes them separately, so they can be stored separately --
        and each is written back where it was found, not both to one key."""
        prefs = {"items-lib1-Movie-sortby": "PlayCount",
                 "items-lib1-sortorder": "Ascending"}

        got = view_prefs.resolve_sort(prefs, "lib1", "movies")

        self.assertEqual("items-lib1-Movie-sortby", got["sortby"][1])
        self.assertEqual("items-lib1-sortorder", got["sortorder"][1])

    def test_no_parent_means_nothing_stored(self):
        """A person page or a search result has no key family, so it keeps
        today's behaviour -- which is web's too."""
        got = view_prefs.resolve_sort({"items-lib1-sortby": "PlayCount"},
                                      None, "movies")

        self.assertEqual({"sortby": (None, None),
                          "sortorder": (None, None)}, got)


class ResolveTest(unittest.TestCase):
    def test_an_untouched_library_is_auto(self):
        value, key = view_prefs.resolve_image_type({}, "PID", "movies")
        self.assertEqual(value, "primary")
        self.assertIsNone(key, "nothing stored, so nothing to write back to")

    def test_the_observed_setting_is_read(self):
        value, key = view_prefs.resolve_image_type(
            {"items-PID-Folder-imageType": "thumb"}, "PID", "homevideos")
        self.assertEqual(value, "thumb")
        self.assertEqual(key, "items-PID-Folder-imageType")

    def test_the_key_it_came_from_is_returned(self):
        """So a save lands where the reader looked -- writing elsewhere
        leaves the user's web client reading the old value."""
        _v, key = view_prefs.resolve_image_type(
            {"items-PID-imageType": "banner"}, "PID", "movies")
        self.assertEqual(key, "items-PID-imageType")

    def test_a_typed_key_wins_over_the_bare_one(self):
        value, _k = view_prefs.resolve_image_type(
            {"items-PID-Movie-imageType": "banner",
             "items-PID-imageType": "thumb"}, "PID", "movies")
        self.assertEqual(value, "banner")

    def test_junk_is_ignored_rather_than_applied(self):
        value, key = view_prefs.resolve_image_type(
            {"items-PID-Movie-imageType": "nonsense"}, "PID", "movies")
        self.assertEqual(value, "primary")
        self.assertIsNone(key)

    def test_case_and_space_do_not_matter(self):
        value, _k = view_prefs.resolve_image_type(
            {"items-PID-Movie-imageType": " Thumb "}, "PID", "movies")
        self.assertEqual(value, "thumb")


class ShapeTest(unittest.TestCase):
    def test_primary_means_no_override(self):
        """It is "auto": shape the grid by its artwork, which is what it
        does with no setting at all."""
        self.assertIsNone(view_prefs.shape_for("primary"))

    def test_thumb_is_landscape(self):
        self.assertEqual(view_prefs.shape_for("thumb"), ("geom_wide", "Thumb"))

    def test_banner_gets_a_banner(self):
        """Decided explicitly: if they ask for banner, give them banner --
        auto_geom folds its own >=3 bucket into landscape, but that one is
        inferred and this one is asked for."""
        self.assertEqual(view_prefs.shape_for("banner"),
                         ("geom_banner", "Banner"))

    def test_disc_is_square_and_logo_is_wide(self):
        self.assertEqual(view_prefs.shape_for("disc"), ("geom_square", "Disc"))
        self.assertEqual(view_prefs.shape_for("logo"), ("geom_wide", "Logo"))

    def test_poster_forces_what_auto_usually_infers(self):
        """Ours, not web's. Auto usually comes out as posters, but a Home
        Videos library with a few portrait clips among landscape ones has a
        median that says otherwise and no way to argue."""
        self.assertEqual(view_prefs.shape_for("poster"), ("geom", "Primary"))
        self.assertIsNone(view_prefs.shape_for("primary"),
                          "Auto must stay 'shape it from the artwork'")

    def test_poster_asks_the_server_for_what_auto_asks_for(self):
        """Only the shape is forced. If it asked for something else the two
        could disagree about which artwork exists."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            browse_image_types)
        self.assertEqual(browse_image_types(view_prefs.shape_for("poster")[1]),
                         browse_image_types(None))

    def test_a_stored_poster_is_read_back(self):
        value, _key = view_prefs.resolve_image_type(
            {"items-PID-imageType": "poster"}, "PID", "homevideos")
        self.assertEqual(value, "poster")

    def test_an_unknown_value_is_no_override(self):
        self.assertIsNone(view_prefs.shape_for("wat"))
        self.assertIsNone(view_prefs.shape_for(None))


if __name__ == "__main__":
    unittest.main()
