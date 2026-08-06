"""Collections (box sets) against a real server.

A collection is the one container in this library whose **shape the server
gets wrong in a way no fake would ever reproduce**, and stdjflib now ships
fixtures for every variant of it. Three claims are pinned here, each of which
is invisible to `tests/_shell_harness.FakeSource`:

**`ChildCount` is a lie for a collection read off disk.** A `collection.xml`
is parsed into the in-memory item and its members are never written to the
`LinkedChildren` table, so `ChildCount` reports **0** for the whole life of
the server while `GET /Items?parentId=` on the same item returns every
member. (Measured on 12.0; stdjflib's `docs/COLLECTION_XML_BUGS.md` has the
reproduction and the source reading.) The browser happens not to read
`ChildCount` anywhere — this suite is what keeps it that way, because
"collections show as empty" is the exact bug that follows from the obvious
optimisation, and it would show up on real libraries and on no fake.

**A collection holds items of any type, from any library.** A linked child is
an arbitrary item: `Two Libraries, One Collection` is a series from Shows and
a film from Movies. So the listing must go out **untyped and non-recursive**
— which is what `_open_item` arranges by passing the BoxSet's own
`CollectionType` (there is none) to the grid. Send `LIBRARY_ITEM_TYPES`'
`"movies"` instead, the way a library grid does, and the series silently
vanishes; the test below asserts both halves so the untyped query is not
"simplified" into the typed one.

**Editing a collection needs a permission a new account does not have.**
The whole of `CollectionController` is behind
`EnableCollectionManagement` — the `[Authorize]` is on the controller, not
its routes — and it is off for a newly created account with no administrator
bypass, the same shape as `EnableLiveTvManagement` (see
`docs/PERMISSION_GAPS.md` §5). So the shim hides the affordances for an
account that lacks it, and this is where the *field name* is checked: a unit
test builds the policy dict it then reads and would pass against a misspelt
key. The refusal itself is asserted too, because hiding a button is only
right for as long as pressing it would have failed.

And the three edit endpoints — `new_collection`, `add_collection_items`,
`remove_collection_items` — are exercised end to end through the real
`PlayerGateway`, because nothing else in the project ever calls them against
a server. That is `b97dd523`'s shape: an apiclient argument the fake accepts
and the server does not.

**Fixtures.** From stdjflib's `Box Sets` / `Auto Collections` libraries. Every
lookup skips rather than fails when a fixture is absent, so a library built
before collections existed does not report a defect it cannot have.
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

#: Collections built by stdjflib, and the one thing each is here to prove.
LINKED = "The Linked Collection"           # 3 films, from a collection.xml
API_MADE = "Api Made Collection"           # the same 3, via POST /Collections
CROSS_LIBRARY = "Two Libraries, One Collection"   # a Series and a Movie
SHORT_ONE = "One Member Is Missing"        # a member that names no file

SIZE = (1280, 720)


class _SyncPool:
    """Run route loaders inline, so a fetch completes before the assert.

    Same seam `test_route_walk` uses, and for the same reason: the work it
    runs is a *real* request, so `navigate()` fetches and what follows reads
    what came back rather than a spinner.
    """

    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


class _CollectionCase(unittest.TestCase):
    """A session and a real `LibrarySource`, plus fixture lookup by name."""

    ACCOUNT = "qa-user"

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session(cls.ACCOUNT)
        cls.source = cls.session.library_source()
        cls.source.get_libraries(_e2e.SOURCE_UUID)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    @property
    def uuid(self):
        return _e2e.SOURCE_UUID

    # -- fixtures ----------------------------------------------------------

    def box_sets(self):
        """Every collection on the server, by name. Never by id."""
        items = self.session.find_all(item_type="BoxSet",
                                      fields="ChildCount")
        return {i["Name"]: i for i in items}

    def collection(self, name):
        found = self.box_sets().get(name)
        if found is None:
            self.skipTest(
                "no collection named %r — this library predates stdjflib's "
                "collection fixtures; rebuild it with `stdjflib build`" % name)
        return found

    def members(self, box_set, collection_type=None):
        """The listing the grid makes when a collection is opened.

        `collection_type` is what `_open_item` passes: the BoxSet's own, and a
        BoxSet has none. Passing one is the mistake this exists to catch.
        """
        items, _total = self.source.get_library_items(
            self.uuid, box_set["Id"], limit=100,
            collection_type=collection_type)
        return items


@_e2e.require_server
class CollectionBrowseTest(_CollectionCase):
    """Reading collections: the toggle's query, and what a collection lists."""

    def test_the_collections_toggle_asks_the_whole_server(self):
        """`get_movie_collections` carries no ParentId, deliberately.

        It is reached from a movies library, but a collection is not parented
        to one — `The Linked Collection` lives in `Box Sets`, the automatic
        one lives under the server's data directory, and a cross-library one
        belongs to neither. Scope the query to the library the toggle was
        pressed in and most of them disappear.
        """
        items, total = self.source.get_movie_collections(self.uuid, limit=100)
        self.assertTrue(items, "the Collections toggle returned nothing")
        self.assertEqual(
            total, len(items),
            "the collections grid paged when it said it had not")
        self.assertEqual(
            {i.get("Type") for i in items}, {"BoxSet"},
            "the Collections toggle returned something that is not a BoxSet")

        by_name = {i["Name"]: i for i in items}
        every = self.box_sets()
        self.assertEqual(
            sorted(by_name), sorted(every),
            "the toggle's own query does not see every collection the server "
            "has — it is scoped to something")

    def test_a_collection_lists_members_the_server_counts_as_none(self):
        """The wart, pinned: browse by listing, never by `ChildCount`.

        A collection read from a `collection.xml` reports `ChildCount` 0 for
        ever — the members are applied to the in-memory item and never
        written to the table the count is read from. Nothing in the browser
        reads `ChildCount`; this is what keeps it that way, because the
        failure it buys ("all my collections are empty") reproduces on real
        libraries and on no fake.
        """
        linked = self.collection(LINKED)
        members = self.members(linked)
        self.assertEqual(
            len(members), 3,
            "%s should list its three films; the listing is the only place "
            "its membership is visible" % LINKED)
        self.assertEqual(
            linked.get("ChildCount") or 0, 0,
            "this server now persists a collection.xml's members — the "
            "12.0 defect this test documents has been fixed, and the note "
            "above should say so rather than being deleted")

    def test_a_disk_made_and_an_api_made_collection_browse_identically(self):
        """stdjflib's controlled pair: same three films, two mechanisms.

        `Api Made Collection` holds exactly what `The Linked Collection`
        holds and was created through `POST /Collections` instead of from a
        file. If the shim can tell them apart, it is reading something it
        should not be — which is how "empty on disk, fine over the API" gets
        misread as a client bug.
        """
        linked = {i["Name"] for i in self.members(self.collection(LINKED))}
        api_made = {i["Name"] for i in self.members(self.collection(API_MADE))}
        self.assertEqual(
            linked, api_made,
            "the two collections built from the same three films do not "
            "browse the same")

    def test_a_collection_is_listed_untyped_so_a_series_member_survives(self):
        """Both halves: what the shim sends, and what the other query costs.

        A linked child is any item at all. `Two Libraries, One Collection` is
        a Series and a Movie, and the typed+recursive query a *library* grid
        makes (`LIBRARY_ITEM_TYPES["movies"]`) answers with the film alone —
        measured, not assumed. So the series is not mis-shaped or mis-sorted
        by the wrong query, it is **absent**, which on screen is a collection
        that is simply short an item. The negative half is the point: without
        it, "just pass the collection type through like every other grid"
        reads as a tidy-up rather than a bug.
        """
        cross = self.collection(CROSS_LIBRARY)
        untyped = self.members(cross)
        self.assertEqual(
            {i.get("Type") for i in untyped}, {"Movie", "Series"},
            "%s should list one film and one series; a collection holds "
            "items of any type" % CROSS_LIBRARY)

        typed = self.members(cross, collection_type="movies")
        self.assertNotIn(
            "Series", {i.get("Type") for i in typed},
            "the typed movies query keeps the series member here, so this "
            "test can no longer tell which query the shim sent")

    def test_a_collection_short_a_member_still_opens(self):
        """A member naming a file that does not exist is dropped silently.

        On 12.0 it is dropped for good (a linked child lives in a table whose
        child column is a non-nullable id). A client cannot distinguish that
        from a collection that was built with one item, and must not try —
        what it must do is open.
        """
        members = self.members(self.collection(SHORT_ONE))
        self.assertEqual(
            len(members), 1,
            "%s should list the one member that resolves" % SHORT_ONE)

    def test_the_add_to_collection_picker_lists_collections(self):
        """`get_collections` is its own endpoint and its own request.

        The picker is its only caller, so a wrong argument here fails in one
        dialog and nowhere else — and the dialog renders an empty list, which
        looks like "you have no collections yet".
        """
        collections = self.source.get_collections(self.uuid)
        self.assertTrue(collections, "the picker would show an empty list")
        self.assertEqual(
            {c.get("Type") for c in collections}, {"BoxSet"},
            "the picker offered something that is not a collection")
        self.assertIn(
            LINKED, {c["Name"] for c in collections},
            "a collection made from a file is missing from the picker")


@_e2e.require_server
class CollectionRouteTest(_CollectionCase):
    """Opening a collection through the browser, against real DTOs.

    `test_route_walk` walks every *page kind*, and a collection is not one —
    it is a `grid` with a `parent_type`. So the wiring that makes it work is
    unwalked: `_open_item` has to send the BoxSet down the folder branch with
    no collection type, and the tile menu inside one has to offer the removal
    that only exists there.
    """

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        self.browser = MpvtkBrowser(app=None, source=self.source,
                                    server_uuid=_e2e.SOURCE_UUID)
        # __init__ already kicked the home load onto the real threaded pool;
        # drain it before swapping, or it lands after this class has logged
        # out. Same dance as test_route_walk.setUp.
        self.browser._async._pool.shutdown(wait=True, cancel_futures=True)
        self.browser._pool = _SyncPool()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        try:
            self.browser.shutdown()
        except Exception:
            pass

    def _open(self, name):
        """Click the collection's tile, the way the grid does."""
        box_set = self.collection(name)
        self.browser._open_item(box_set)
        route = self.browser.route
        self.assertEqual(route.get("kind"), "grid",
                         "a collection did not open as a grid")
        self.assertFalse(
            route.get("_loading"),
            "the collection is still loading after an inline fetch, so what "
            "follows would be asserting against a spinner")
        self.assertIsNone(
            route.get("_error"),
            "the collection failed to load: %s" % route.get("_error"))
        return route

    def test_opening_a_collection_asks_for_it_untyped(self):
        route = self._open(CROSS_LIBRARY)
        self.assertEqual(
            route.get("parent_type"), "BoxSet",
            "without parent_type the tile menu cannot offer Remove from "
            "Collection")
        self.assertIsNone(
            route.get("collection_type"),
            "a collection type on this route makes the grid's query typed "
            "and recursive, which drops every member that is not a movie")
        names = {i.get("Type") for i in (route.get("_items") or [])}
        self.assertEqual(
            names, {"Movie", "Series"},
            "the opened collection lists %r; a series member was lost on the "
            "way through the browser" % (names,))

    def test_the_tile_menu_inside_a_collection_offers_removal(self):
        route = self._open(LINKED)
        items = route.get("_items") or []
        self.assertTrue(items, "nothing to right-click")
        entries = self.browser._tile_menu_entries(items[0])
        labels = [label for label, _icon, _action in entries]
        self.assertIn(
            "uncollect", [action for _label, _icon, action in entries],
            "Remove from Collection is missing inside a collection "
            "(offered: %s)" % labels)

    def test_a_member_of_a_collection_opens_as_itself(self):
        """A series inside a collection opens the series screen.

        The collection is browsed untyped, so its members arrive as whatever
        they are — and a client that assumed a box set holds movies routes a
        Series to the detail page, which draws a play button for something
        with no media.
        """
        route = self._open(CROSS_LIBRARY)
        series = next((i for i in (route.get("_items") or [])
                       if i.get("Type") == "Series"), None)
        self.assertIsNotNone(series, "no series member to open")
        self.browser._open_item(series)
        self.assertEqual(
            self.browser.route.get("kind"), "series",
            "a series inside a collection did not open the series screen")


@_e2e.require_server
class CollectionEditTest(unittest.TestCase):
    """Create, add, remove — through the gateway, against the real server.

    Runs as **qa-user**, an ordinary non-admin, because that is the account
    the affordance is for and because `EnableCollectionManagement` has no
    administrator bypass: asking qa-admin would prove the endpoints work for
    somebody nobody is worried about. A second, admin session exists only to
    **delete** the fixture — `EnableContentDeletion` is a different
    permission that qa-user does not have and the shim never asks for, since
    deleting a collection is not something the browser offers.

    This class owns its fixture: it makes its own collection and deletes it,
    in `setUp` as well as on cleanup, so a run that died halfway cannot leave
    one behind for the next one to trip over. It touches no stdjflib
    collection — adding a member to one would be a change every other test in
    this file can see.
    """

    NAME = "JMS E2E Collection Fixture"

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session("qa-user")
        if not (cls.session.policy() or {}).get(
                "EnableCollectionManagement"):
            cls.session.stop()
            raise unittest.SkipTest(
                "qa-user cannot manage collections on this server — this "
                "library predates stdjflib granting it; reprovision")
        cls.admin = _e2e.Session("qa-admin")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.admin.stop()
        finally:
            cls.session.stop()

    def setUp(self):
        from types import SimpleNamespace
        from jellyfin_mpv_shim.mpvtk_browser.gateway import deps, PlayerGateway

        # The one foreign singleton the gateway binds at import; `deps.py`
        # exists so it can be rebound in exactly this way.
        # tests/test_player_controller.py does the same with a broken one.
        self._real_manager = deps.clientManager
        deps.clientManager = SimpleNamespace(
            clients={_e2e.SOURCE_UUID: SimpleNamespace(
                jellyfin=self.session.api)})
        self.addCleanup(self._restore, deps)
        # No __init__: the editing mixin reaches nothing but deps, and
        # building a real gateway would drag the player in and cost this
        # module its place in the contract tier.
        self.gateway = PlayerGateway.__new__(PlayerGateway)
        self.assertTrue(
            self.gateway.edit_apis(),
            "the installed apiclient cannot edit collections, so the shim "
            "would hide these affordances entirely")
        self._delete_fixture()
        self.addCleanup(self._delete_fixture)

    def _restore(self, deps):
        deps.clientManager = self._real_manager

    def _delete_fixture(self):
        # As admin: DELETE /Items is EnableContentDeletion, a different
        # permission from the one under test, and qa-user has neither it nor
        # any need for it.
        for found in self._find_all():
            try:
                self.admin.api.delete_item(found["Id"])
            except Exception:
                pass

    def _find_all(self):
        items = (self.session.api.get_collections(limit=300) or {}
                 ).get("Items", [])
        return [i for i in items if i["Name"] == self.NAME]

    def _members(self, collection_id):
        return self.session.api.user_items(
            params={"ParentId": collection_id})["Items"]

    def test_create_add_and_remove(self):
        """One test, because it is one sequence.

        Split into three it would need three fixtures or a shared one, and a
        shared one makes the second assertion depend on the first having run
        — which is the ordering dependency this suite refuses everywhere
        else. The property is about the sequence anyway: what a collection
        holds after three edits, not what one call returned.
        """
        film = self.session.find_all(
            library="Movies", item_type="Movie", Limit=1)[0]
        series = self.session.find_all(
            library="Shows", item_type="Series", Limit=1)[0]

        self.gateway.collection_new(_e2e.SOURCE_UUID, self.NAME, [film["Id"]])
        made = self._find_all()
        self.assertEqual(
            len(made), 1,
            "collection_new did not produce exactly one %r" % self.NAME)
        collection_id = made[0]["Id"]
        self.assertEqual(
            [i["Name"] for i in self._members(collection_id)], [film["Name"]],
            "a new collection did not hold the item it was created from")

        # A Series, not a second film: a collection takes any item, and the
        # add path must not quietly be movies-only.
        self.gateway.collection_add(_e2e.SOURCE_UUID, collection_id,
                                    [series["Id"]])
        self.assertEqual(
            {i["Name"] for i in self._members(collection_id)},
            {film["Name"], series["Name"]},
            "adding a series to a collection did not stick")

        self.gateway.collection_remove(_e2e.SOURCE_UUID, collection_id,
                                       [film["Id"]])
        self.assertEqual(
            [i["Name"] for i in self._members(collection_id)],
            [series["Name"]],
            "removing the film left the collection unchanged")

    def test_adding_a_member_twice_does_not_duplicate_it(self):
        """The server deduplicates by item id, so the second add is a no-op.

        Worth a test because the browser has no guard of its own: the picker
        offers every collection whether or not the item is already in one,
        and the tile menu is drawn from the item, not from the membership.
        """
        film = self.session.find_all(
            library="Movies", item_type="Movie", Limit=1)[0]
        self.gateway.collection_new(_e2e.SOURCE_UUID, self.NAME, [film["Id"]])
        collection_id = self._find_all()[0]["Id"]
        self.gateway.collection_add(_e2e.SOURCE_UUID, collection_id,
                                    [film["Id"]])
        self.assertEqual(
            [i["Name"] for i in self._members(collection_id)], [film["Name"]],
            "adding the same item twice put it in the collection twice")


@_e2e.require_server
class CollectionPermissionTest(unittest.TestCase):
    """An account that may not manage collections is not offered the controls.

    `EnableCollectionManagement` is off for a newly created Jellyfin user and
    there is no administrator bypass — `UserPermissionHandler` asks
    `HasPermission` and stops — so the whole of `CollectionController` is 403
    for most accounts on a modern server. The `[Authorize]` is on the
    controller rather than its routes, so create, add and remove are one
    permission and one refusal.

    Two halves, and the second is why this is an e2e test rather than a unit
    one: **the shim hides the affordance**, and **the server would really
    have refused it**. `tests/test_user_policy.py` pins the branch against a
    hand-written policy dict, which proves the logic and not the spelling of
    the field — that answer belongs to a server.

    `qa-restricted` rather than `qa-user`: stdjflib grants collection
    management to qa-user (it is the account whose description claims
    everything a non-admin can have), so the refusal has to be observed on
    one of the ten that still lack it. qa-restricted can see Movies, which is
    all this needs.
    """

    ACCOUNT = "qa-restricted"

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session(cls.ACCOUNT)
        if (cls.session.policy() or {}).get("EnableCollectionManagement"):
            cls.session.stop()
            raise unittest.SkipTest(
                "%s has been granted EnableCollectionManagement, so there is "
                "no refusal to observe" % cls.ACCOUNT)
        cls.source = cls.session.library_source()
        cls.source.get_libraries(_e2e.SOURCE_UUID)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def test_the_source_reports_the_refusal(self):
        """The field name itself, read off a real policy.

        A unit test builds the dict it then reads, so it cannot catch a
        misspelt key or a field the server renamed — it would pass against
        `EnableCollectionMangement` all day.
        """
        self.assertFalse(
            self.source.can_manage_collections(_e2e.SOURCE_UUID),
            "%s may not manage collections, and the source says it may"
            % self.ACCOUNT)

    def test_the_affordances_are_not_offered(self):
        """Both of them, from the real source: the tile entry inside a
        collection and the door to the collections picker."""
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        browser = MpvtkBrowser(app=None, source=self.source,
                               server_uuid=_e2e.SOURCE_UUID)
        browser._async._pool.shutdown(wait=True, cancel_futures=True)
        browser._pool = _SyncPool()
        self.addCleanup(browser.shutdown)

        self.assertFalse(
            browser._actions.can_manage_collections(_e2e.SOURCE_UUID),
            "the edit affordances would be drawn for an account the server "
            "refuses")

        browser.nav_stack = [{"kind": "grid", "server": _e2e.SOURCE_UUID,
                              "parent_id": "whatever",
                              "parent_type": "BoxSet"}]
        film = self.session.find_all(item_type="Movie", Limit=1)
        if not film:
            self.skipTest("%s can see no films" % self.ACCOUNT)
        actions = [a for _label, _icon, a
                   in browser._tile_menu_entries(film[0])]
        self.assertNotIn(
            "uncollect", actions,
            "Remove from Collection is offered to an account that cannot "
            "remove anything (offered: %s)" % actions)
        self.assertIn(
            "addto", actions,
            "Add to Playlist went with it — PlaylistController has no such "
            "policy, so that is a second bug wearing the first one's fix")

    def test_the_server_really_would_have_refused(self):
        """The premise. Without it, hiding the button is a guess.

        Also what keeps the gate honest if Jellyfin ever relaxes this: the
        day `POST /Collections` starts answering for an ordinary account,
        this fails and the hiding becomes the thing to reconsider.
        """
        from types import SimpleNamespace
        from jellyfin_mpv_shim.mpvtk_browser.gateway import deps, PlayerGateway

        real = deps.clientManager
        deps.clientManager = SimpleNamespace(
            clients={_e2e.SOURCE_UUID: SimpleNamespace(
                jellyfin=self.session.api)})
        self.addCleanup(lambda: setattr(deps, "clientManager", real))
        gateway = PlayerGateway.__new__(PlayerGateway)

        film = self.session.find_all(item_type="Movie", Limit=1)
        if not film:
            self.skipTest("%s can see no films" % self.ACCOUNT)
        # RAISES, deliberately: `_edit` used to log and return, which made a
        # refused edit look exactly like an applied one. The status line the
        # user sees hangs off this exception.
        with self.assertRaises(Exception):
            gateway.collection_new(_e2e.SOURCE_UUID,
                                   "JMS E2E Refused Collection",
                                   [film[0]["Id"]])

        self.assertEqual(
            [i for i in (self.session.api.get_collections(limit=300) or {}
                         ).get("Items", [])
             if i["Name"] == "JMS E2E Refused Collection"], [],
            "the refused collection exists, so the 403 this test is built on "
            "is no longer what the server does")


if __name__ == "__main__":
    unittest.main()
