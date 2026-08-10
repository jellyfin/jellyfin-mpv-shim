"""Every filter the panel can offer, against a real server.

**Why this exists.** The filter panel grew eight categories and about
twenty controls, and the only thing checking any of it was unit tests
against a fake source that accepts whatever it is given. That fake cannot
answer the two questions that actually matter about a filter:

1. **Does the server accept it?** `Filters=IsUnplayed,IsPlayed` is
   answered with **HTTP 400** — so ticking Played and Unplayed together
   did not show an empty grid, it failed the load and put "Failed to
   load. Check the connection." over a library that was working. That
   shipped, and nothing in 4400 unit tests could have seen it: the fake
   records the filters and returns its items.

2. **Does the server UNDERSTAND it?** An unparseable `VideoTypes` value
   is silently ignored — `VideoTypes=Nonsense` answers with the whole
   library, exactly as sending nothing does. So a wrong enum spelling
   does not break the filter, it turns it off, and the screen looks
   perfectly normal. The same is true of any parameter name we get
   wrong: the server drops what it does not recognise.

Both are invisible to a fake by construction, and both are cheap to
check against a real one. **[iw]**: "this means we have a strong argument
for adding exhaustive e2e tests that exercise the different options and
listen for errors."

**Exhaustive by derivation, not by hand.** The filter keys come from
``dialogs.FILTER_SECTIONS`` and the picker values from the server's own
``get_filter_values``, so a category added to the panel is covered here
without anyone remembering to add it — and a category the server does not
offer (the language pickers before Jellyfin 12) is skipped rather than
failed, which is the panel's own gate expressed as a test.

**Run it against both major versions.** ``JMS_E2E_SERVER`` picks one;
``JMS_E2E_SERVER_ALT`` adds a second and the whole matrix runs twice.
That is the point of the file rather than a nicety: the language pickers
exist on 12.0 and not on 10.11, `Filters` rejects its contradictory pair
on one and may not on the other, and every one of those differences is
the kind that ships.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from tests.e2e import _e2e  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser import dialogs  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    FEATURE_FILTERS, VIDEO_FLAG_FILTERS, VIDEO_TYPE_FILTERS,
)


#: Collection type the sweep runs against. Movies, because it is the one
#: library every filter category is offered on -- the gate is derived
#: from `FILTER_SECTIONS`, so a narrower library would silently shrink
#: the matrix and the test would still pass.
CTYPE = "movies"


def checkbox_keys(collection_type=CTYPE):
    """Every checkbox the panel can draw on this library type.

    Read out of FILTER_SECTIONS rather than listed, so a box added to the
    panel is swept here without anyone remembering.
    """
    out = []
    for _label, kind, spec, libs in dialogs.FILTER_SECTIONS:
        if kind != "checks" or not dialogs._applies(libs, collection_type):
            continue
        out += [key for key, _text, box_libs in spec
                if dialogs._applies(box_libs, collection_type)]
    return out


def picker_keys(collection_type=CTYPE):
    """``[(filter key, values key)]`` for the drop-down categories."""
    return [spec for _label, kind, spec, libs in dialogs.FILTER_SECTIONS
            if kind == "pick" and dialogs._applies(libs, collection_type)]


class _FilterMatrix:
    """The body of the sweep. Subclassed once per server below.

    A mixin rather than a parameterised TestCase so each server gets its
    own class name in the output: "which version failed" is the first
    thing to know, and a subTest label buried in a shared class is not
    where anyone looks.
    """

    SERVER_ENV = "JMS_E2E_SERVER"

    @classmethod
    def setUpClass(cls):
        cls.address = (os.environ.get(cls.SERVER_ENV) or "").rstrip("/")
        if not cls.address:
            raise unittest.SkipTest("%s is not set" % cls.SERVER_ENV)
        cls.session = _e2e.Session(address=cls.address)
        cls.source = cls.session.library_source()
        libs = cls.source.get_libraries(_e2e.SOURCE_UUID)
        movies = [lib for lib in libs
                  if lib.get("CollectionType") == CTYPE]
        if not movies:
            raise unittest.SkipTest("no movies library on %s" % cls.address)
        # The BIGGEST one, not the first. The curated "Movies" fixture has
        # four films in it, and four is enough for every assertion here to
        # be true by accident: a filter that matches all four is
        # indistinguishable from one the server ignored.
        sized = []
        for lib in movies:
            _items, total = cls.source.get_library_items(
                _e2e.SOURCE_UUID, lib["Id"], limit=1, collection_type=CTYPE)
            sized.append((total or 0, lib["Id"]))
        cls.total, cls.parent = max(sized)
        if cls.total < 20:
            raise unittest.SkipTest(
                "largest movies library has %d items, too few to tell a "
                "filter that matched everything from one that was "
                "ignored" % cls.total)
        cls.values = cls.source.get_filter_values(
            _e2e.SOURCE_UUID, cls.parent, collection_type=CTYPE)
        cls.version = cls.session.server_version()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    # -- the one call every test makes -----------------------------------

    def query(self, filters):
        """``(total, error)`` for a filter dict, through the real source.

        Errors are RETURNED, not raised: the whole point is to sweep and
        report which combinations the server refuses, and a raise would
        stop at the first one.
        """
        try:
            _items, total = self.source.get_library_items(
                _e2e.SOURCE_UUID, self.parent, limit=1,
                filters=filters, collection_type=CTYPE)
            return total, None
        except Exception as exc:               # noqa: BLE001 - reported
            return None, "%s: %s" % (type(exc).__name__, exc)

    def raw(self, params):
        """``TotalRecordCount`` for a hand-built query on this library.

        Deliberately below `_filter_kwargs`: the acceptance sweep above
        goes through our translation because that is what ships, and this
        goes to the wire because the question is what the SERVER
        understands.
        """
        import json
        import urllib.parse
        import urllib.request
        query = {"ParentId": self.parent, "IncludeItemTypes": "Movie",
                 "Recursive": "true", "Limit": "1"}
        query.update(params)
        req = urllib.request.Request(
            self.address + "/Items?" + urllib.parse.urlencode(query),
            headers={"Authorization":
                     'MediaBrowser Token="%s"' % self.session.token})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response).get("TotalRecordCount")

    # -- accepted --------------------------------------------------------

    def test_every_checkbox_alone_is_accepted(self):
        bad = []
        for key in checkbox_keys():
            _total, err = self.query({key: True})
            if err:
                bad.append((key, err))
        self.assertEqual(bad, [], "filters the server refused: %r" % bad)

    def test_every_picker_value_is_accepted(self):
        bad = []
        for key, vals_key in picker_keys():
            options = self.values.get(vals_key) or []
            if not options:
                continue      # the panel would not draw it either
            for option in options[:8]:
                value = (option[1] if isinstance(option, (tuple, list))
                         else option)
                _total, err = self.query({key: value})
                if err:
                    bad.append((key, value, err))
        self.assertEqual(bad, [], "picker values refused: %r" % bad)

    def test_every_reachable_pair_of_checkboxes_is_accepted(self):
        """Pairs, because that is the size at which the 400 lived.

        A single filter is never contradictory; two can be, and the
        panel lets you tick any two that are not in MUTUALLY_EXCLUSIVE.
        Reachability is asked of the same table the UI enforces, so a
        pair this skips is a pair the panel cannot produce.
        """
        keys = checkbox_keys()
        bad = []
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if dialogs.MUTUALLY_EXCLUSIVE.get(a) == b:
                    continue
                _total, err = self.query({a: True, b: True})
                if err:
                    bad.append((a, b, err))
        self.assertEqual(bad, [], "pairs the server refused: %r" % bad)

    def test_the_whole_panel_at_once_is_accepted(self):
        """Everything tickable, ticked. Nonsensical as a query and
        entirely reachable by clicking."""
        filters = {}
        for key in checkbox_keys():
            if dialogs.MUTUALLY_EXCLUSIVE.get(key) in filters:
                continue
            filters[key] = True
        total, err = self.query(filters)
        self.assertIsNone(err, "the full panel was refused: %s" % err)
        self.assertIsNotNone(total)

    # -- understood ------------------------------------------------------

    def test_the_video_type_spellings_are_not_silently_ignored(self):
        """An unparseable `VideoTypes` value is DROPPED, not rejected --
        it answers with the whole library, exactly as sending nothing
        does. So a wrong spelling turns the filter off and looks normal.

        The contrast is the only available evidence: a value that parses
        must answer differently from one that cannot.
        """
        base, err = self.query({})
        self.assertIsNone(err)
        bogus, err = self.query({"vt_bogus": True, "vt_dvd": False})
        # `vt_bogus` is not a key the translation knows, so this is the
        # unfiltered query -- the baseline the ignored case looks like.
        self.assertIsNone(err)
        self.assertEqual(bogus, base)
        for key, param in VIDEO_TYPE_FILTERS:
            with self.subTest(video_type=param):
                total, err = self.query({key: True})
                self.assertIsNone(err)
                self.assertNotEqual(
                    total, base,
                    "%s answered with the whole library (%d), which is "
                    "what an IGNORED value answers -- the spelling is "
                    "probably wrong" % (param, base))

    def test_every_boolean_parameter_is_understood(self):
        """The other way a filter fails silently: a parameter name the
        server does not recognise is **dropped**, so the grid shows the
        whole library and looks perfectly normal.

        Asked at the wire rather than through `_filter_kwargs`, because
        the question is about the parameter and not about our dict --
        there is no way to spell "Is4K=false" in a filters dict, and the
        negative direction is half the evidence.

        The discriminator is "``true`` and ``false`` do not BOTH answer
        the unfiltered total". An ignored parameter answers it in both
        directions; a real one cannot, whatever the library holds. This
        is deliberately weaker than "they partition", which several of
        these do not: `Is4K=true` is 5 and `Is4K=false` is 0 against a
        library of 1131, so the two do not sum to the whole and a
        partition test would fail on a parameter that works.
        """
        params = [param for _key, param in FEATURE_FILTERS]
        params += [param for _key, param in VIDEO_FLAG_FILTERS]
        params.append("IsHd")
        ignored = []
        for param in params:
            with self.subTest(param=param):
                yes = self.raw({param: "true"})
                no = self.raw({param: "false"})
                if yes == self.total and no == self.total:
                    ignored.append(param)
                self.assertNotEqual(
                    (yes, no), (self.total, self.total),
                    "%s answered the whole library (%d) both ways, which "
                    "is what a parameter the server does not recognise "
                    "does" % (param, self.total))
        self.assertEqual(ignored, [])

    def test_a_parameter_name_we_got_wrong_would_look_like_this(self):
        """The control for the test above -- proof that its discriminator
        can actually see the failure it is named after.

        Without this the assertion is a guess about how the server treats
        an unknown parameter. It drops it: both directions answer the
        unfiltered total.
        """
        self.assertEqual(self.raw({"IsDefinitelyNotAParameter": "true"}),
                         self.total)
        self.assertEqual(self.raw({"IsDefinitelyNotAParameter": "false"}),
                         self.total)

    def test_the_status_filters_partition_the_library(self):
        """Played and Unplayed must add up to everything -- these two DO
        partition (unlike the video flags), because every item is one or
        the other."""
        base, err = self.query({})
        self.assertIsNone(err)
        played, err = self.query({"played": True})
        self.assertIsNone(err)
        unplayed, err = self.query({"unplayed": True})
        self.assertIsNone(err)
        self.assertEqual(played + unplayed, base,
                         "played (%s) + unplayed (%s) is not the library "
                         "(%s)" % (played, unplayed, base))
        self.assertNotEqual(played, base, "Filters was not applied")

    # -- the pair the exclusion exists for --------------------------------

    def test_the_contradictory_pair_is_never_useful(self):
        """Sent anyway -- bypassing the panel -- to pin WHY the exclusion
        exists, and to notice if a server version stops needing it.

        Asserted as "refused, or empty", because the two versions need not
        agree on which: 12.0 answers HTTP 400. What must never happen is
        a non-empty answer, which would mean the server ORed them and the
        exclusion is throwing away a usable query.
        """
        total, err = self.query({"played": True, "unplayed": True})
        if err is None:
            self.assertEqual(
                total, 0,
                "the server answered %d items for Played AND Unplayed -- "
                "it is ORing them, so MUTUALLY_EXCLUSIVE is discarding a "
                "query that works" % (total or 0))

    def test_and_the_panel_cannot_produce_it(self):
        """The other half, stated against the same server: whatever the
        server does with it, we never send it."""
        for a, b in (("played", "unplayed"), ("hd", "sd")):
            self.assertEqual(dialogs.MUTUALLY_EXCLUSIVE.get(a), b)
            self.assertEqual(dialogs.MUTUALLY_EXCLUSIVE.get(b), a)


class FilterMatrixPrimaryTest(_FilterMatrix, unittest.TestCase):
    """The server JMS_E2E_SERVER names."""
    SERVER_ENV = "JMS_E2E_SERVER"


class FilterMatrixAltTest(_FilterMatrix, unittest.TestCase):
    """A second server, for the version differences.

    Skipped when JMS_E2E_SERVER_ALT is unset, so the ordinary run is
    unchanged -- but the differences this catches (the language pickers
    exist on 12.0 and not on 10.11) are exactly the ones that reach
    users, so set it.
    """
    SERVER_ENV = "JMS_E2E_SERVER_ALT"


if __name__ == "__main__":
    unittest.main()
