"""The sort is written where the user's jellyfin-web will read it.

This is a **cross-client write**, which is the whole reason it needs a server.
`view_prefs.SORT_SETTINGS` and `keys_for` encode a claim about somebody else's
product -- that web stores a library's sort in the DisplayPreferences
`CustomPrefs` document under `items-<parentId>[-<routeType>]-sortby` and
`-sortorder` -- and the unit suite asks that claim of `FakeSource`, which
stores whatever key it is handed. A wrong key there is not a crash and not a
visible bug: the shim reads back what it wrote, web reads back what *it* wrote,
and the two quietly diverge. Nobody finds that from this side.

So the value here is not "does it round trip" -- the fake proves that. It is:

* **the server really stores it**, under the key `keys_for` chose, and a
  reader coming the other way finds it there. The read below goes through the
  session's own api rather than the source that wrote it, so a cache standing
  in for the server cannot satisfy it;
* **the value is a string.** `save_view_setting`'s docstring says web compares
  these as strings, so a JSON boolean reads there as false -- and a sort name
  is a string already, which means nothing here would notice if the document
  started carrying something else;
* **the round trip preserves the key.** `resolve_sort` hands the key back so a
  later save lands where the reader looked; if the server normalised or
  renamed it, that contract would be broken in a way only a server shows.

**It puts the document back.** The QA server is shared with every other module
in this suite and a stray `items-...-sortby` changes what the grid asks for
everywhere, so the original value -- including "there wasn't one" -- is
restored in a cleanup.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

#: Deliberately not the default. A sort equal to the screen's own default
#: would be indistinguishable from nothing having been stored.
SORT_BY, SORT_ORDER = "DateCreated", "Descending"


@_e2e.require_server
class TheSortLandsWhereWebLooksTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        library = cls.session.view("Movies")
        cls.parent_id = library["Id"]
        cls.collection_type = library.get("CollectionType")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser import home_sections

        self.prefs_client = home_sections.DISPLAY_PREFS_CLIENT
        # Every key this library's sort could occupy, so the restore puts the
        # document back whichever one the writer picks.
        from jellyfin_mpv_shim.mpvtk_browser import view_prefs

        self.candidates = []
        for setting in view_prefs.SORT_SETTINGS:
            self.candidates += view_prefs.keys_for(
                self.parent_id, self.collection_type, setting)
        self.before = {k: v for k, v in self._custom_prefs().items()
                       if k in self.candidates}
        self.addCleanup(self._restore)

    def _custom_prefs(self):
        """The document as a reader coming the other way sees it.

        Through the session's own api, not through the source that wrote it:
        the source caches `CustomPrefs` per server, so asking it would answer
        from memory and pass with nothing on the server at all.
        """
        dto = self.session.api.get_user_settings(client=self.prefs_client) or {}
        return dto.get("CustomPrefs") or {}

    def _restore(self):
        dto = self.session.api.get_user_settings(client=self.prefs_client) or {}
        custom = dict(dto.get("CustomPrefs") or {})
        for key in self.candidates:
            custom.pop(key, None)
        custom.update(self.before)
        dto["CustomPrefs"] = custom
        self.session.api.update_user_settings(dto, client=self.prefs_client)

    def _save(self):
        for setting, value in (("sortby", SORT_BY), ("sortorder", SORT_ORDER)):
            self.source.save_view_setting(
                _e2e.SOURCE_UUID, self.parent_id, self.collection_type,
                setting, value)

    def test_the_server_holds_it_under_a_key_web_reads(self):
        """The claim `keys_for` encodes, asked of the server.

        Not "some key was written": the name is the contract, because it is
        what the user's web client looks under. `keys_for` is the authority
        for which names are acceptable, and the document must use one of them.
        """
        from jellyfin_mpv_shim.mpvtk_browser import view_prefs

        self._save()

        custom = self._custom_prefs()
        for setting, expected in (("sortby", SORT_BY),
                                  ("sortorder", SORT_ORDER)):
            keys = view_prefs.keys_for(self.parent_id, self.collection_type,
                                       setting)
            stored = [k for k in keys if k in custom]
            self.assertTrue(
                stored,
                "nothing under any key web would read for %s; the document "
                "has %r" % (setting, sorted(custom)))
            self.assertEqual(expected, custom[stored[0]])

    def test_what_the_server_stores_is_a_string(self):
        """Web compares these as strings -- the reason every boolean in this
        document goes out as "true"/"false". A sort name is already a string,
        so nothing here would notice if that stopped being true."""
        self._save()

        custom = self._custom_prefs()
        values = [v for k, v in custom.items() if k in self.candidates]

        self.assertTrue(values, "nothing was stored at all")
        for value in values:
            self.assertIsInstance(value, str)

    def test_a_fresh_read_resolves_it_and_names_the_key_it_came_from(self):
        """The read half, off the real document.

        `resolve_sort` returns the key beside the value so a later save lands
        where the reader looked. A server that renamed or normalised the key
        would break that silently, and only a server can say.
        """
        from jellyfin_mpv_shim.mpvtk_browser import view_prefs

        self._save()

        resolved = view_prefs.resolve_sort(
            self._custom_prefs(), self.parent_id, self.collection_type)

        self.assertEqual(SORT_BY, resolved["sortby"][0])
        self.assertEqual(SORT_ORDER, resolved["sortorder"][0])
        for setting in view_prefs.SORT_SETTINGS:
            key = resolved[setting][1]
            self.assertIn(
                key, view_prefs.keys_for(self.parent_id,
                                         self.collection_type, setting),
                "the key read back is not one a save would write to, so the "
                "next change would land somewhere else")

    def _ids(self, sort_by, sort_order):
        items, _total = self.source.get_library_items(
            _e2e.SOURCE_UUID, self.parent_id,
            sort_by=sort_by, sort_order=sort_order,
            filters={}, image_type="Primary",
            collection_type=self.collection_type)
        return [i.get("Id") for i in items]

    def test_the_grid_then_asks_the_server_for_that_sort(self):
        """The property the dropdown cannot show: a label is not a query.

        Asserted as *a different order*, not as a field the items carry. The
        first draft of this compared `DateCreated` values and passed on an
        empty list, because that field is not in the grid's DTO at all -- 40
        items, none of them carrying it, `sorted([]) == []`. A test that
        cannot fail, in the module added to stop exactly that.
        """
        from jellyfin_mpv_shim.mpvtk_browser import view_prefs

        self._save()
        resolved = view_prefs.resolve_sort(
            self._custom_prefs(), self.parent_id, self.collection_type)

        stored = self._ids(resolved["sortby"][0], resolved["sortorder"][0])
        default = self._ids("SortName", "Ascending")

        self.assertTrue(stored, "the library came back empty")
        self.assertEqual(sorted(stored), sorted(default),
                         "the two queries returned different ITEMS, so this "
                         "is not comparing orders of the same set")
        self.assertNotEqual(
            default, stored,
            "the stored sort produced the same order as the screen's "
            "default, so nothing here can tell whether it reached the query")


if __name__ == "__main__":
    unittest.main()
