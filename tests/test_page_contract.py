"""The Page contract, and the budget on its escape hatch.

Step 6 of ``docs/ARCHITECTURE_TARGET.md`` §3 converts route kinds from
``(loader, renderer)`` method-name pairs on a 358-method object into classes
that own their state and receive their dependencies. It proceeds one route at
a time, which means two things have to be true throughout:

* the two dispatch mechanisms must not disagree about who owns a kind, and
* the transitional coupling must be visible and shrinking, not quietly
  permanent.

The second is what ``SHELL_USE_BUDGET`` is for. ``PageContext.shell`` exists
because a handful of helpers — the tile/row/grid builders, the busy and error
nodes — are component-shaped but still close over ``self.strips`` /
``self._posters``, so extracting them is its own step. Pretending they were
already extracted would be a lie; leaving the hatch uncounted would let it
become the new normal. So it is counted, and the number may only go down.

It has done its job once already. Measuring the five ``ViewsMixin``
renderers against the context gave 50 shell uses against a budget of 9,
which is what stopped the conversion and produced steps 6b (TileRenderer,
ScrollState) and 6c's prep (chrome, controls, detail). Afterwards the worst
route needs 11 names, nearly all of them its own private helpers.
"""

import ast
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.pages import PAGES  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.pages.base import (  # noqa: E402
    Page, PageContext)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES_DIR = os.path.join(REPO, "jellyfin_mpv_shim", "mpvtk_browser", "pages")

#: Current number of ``ctx.shell`` references across all pages.
#:
#: **This may only decrease.** Raising it means a new page leaned on the shell
#: instead of on its context, which is the thing this whole step is undoing.
#: Lower it whenever an extraction removes uses — the test tells you to.
#:
#: 9 -> 1: step 6c's prep put the tile builders and chrome behind ``ctx.art``
#: and ``components/``, which was all SearchPage had been reaching for.
#: 1 -> 0: ItemActions gave the last one (starting a track list) a home.
#:
#: Zero here does NOT mean the hatch is gone. ``base.py`` is excluded from
#: the count below because it is the framework rather than a page, and it
#: still has one use — see BASE_SHELL_USES, which pins it so the exclusion
#: cannot quietly become a hiding place.
SHELL_USE_BUDGET = 0

#: ``Page.route_async`` calls ``ctx.shell._route_async``. That one is not
#: transitional: recording a load failure has to decide whether this route is
#: *still the screen* before dropping the user to the offline home, and only
#: the shell knows. It is pinned rather than budgeted — it may not grow.
BASE_SHELL_USES = 1


def _page_sources():
    for name in sorted(os.listdir(PAGES_DIR)):
        if name.endswith(".py") and name not in ("__init__.py", "base.py"):
            path = os.path.join(PAGES_DIR, name)
            with open(path, encoding="utf-8") as fh:
                yield name, ast.parse(fh.read(), filename=path)


class TestTheRegistryAndTheTablesAgree(unittest.TestCase):
    def test_no_kind_is_claimed_twice(self):
        """A kind served by both a page and a ROUTES entry resolves by
        whichever the shell consults first — the same silent-winner hazard
        test_mpvtk_browser_mixins.py exists for, one layer up."""
        claimed = set()
        for base in type.mro(MpvtkBrowser):
            claimed |= set(base.__dict__.get("ROUTES") or {})
        overlap = sorted(set(PAGES) & claimed)
        self.assertEqual(
            overlap, [],
            "These kinds are served by BOTH a Page and a ROUTES table; "
            "delete the mixin's loader/renderer and its ROUTES entry: %s"
            % overlap)

    def test_every_registered_page_declares_its_kind(self):
        for kind, cls in PAGES.items():
            with self.subTest(kind):
                self.assertTrue(issubclass(cls, Page))
                self.assertEqual(
                    cls.kind, kind,
                    "%s.kind is %r but it is registered under %r"
                    % (cls.__name__, cls.kind, kind))

    def test_every_page_implements_render(self):
        for kind, cls in PAGES.items():
            with self.subTest(kind):
                self.assertIsNot(
                    cls.render, Page.render,
                    "%s does not implement render()" % cls.__name__)

    def test_the_shell_still_serves_unconverted_kinds(self):
        """The fallback is what makes this incremental. If it broke, every
        route not yet converted would render as a spinner."""
        claimed = set()
        for base in type.mro(MpvtkBrowser):
            claimed |= set(base.__dict__.get("ROUTES") or {})
        # Shrinks as 6c converts kinds. When it reaches zero the conversion
        # is done: retire this test, the ROUTES fallback in _load_route /
        # _render_route, and the now-empty mixin tables together.
        self.assertGreater(
            len(claimed), 0,
            "the ROUTES fallback is empty — either the conversion is "
            "complete (retire this test and the fallback) or it broke")


class TestPagesDependOnTheirContext(unittest.TestCase):
    """A page may use its context freely; the shell only through the hatch."""

    #: Attributes a page must reach through ``ctx``, never by importing.
    FORBIDDEN_IMPORTS = {"app", "ui"}

    def test_pages_do_not_import_the_shell(self):
        offenders = []
        for name, tree in _page_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    if node.level >= 1 and root in self.FORBIDDEN_IMPORTS:
                        offenders.append("%s:%d imports %s"
                                         % (name, node.lineno, root))
        self.assertEqual(
            offenders, [],
            "A page receives the shell through PageContext; importing it "
            "creates a cycle and makes the page unconstructible in a "
            "test:\n  " + "\n  ".join(offenders))

    def test_pages_do_not_reach_for_live_singletons(self):
        """Same rule as the rest of the view layer: a page must be
        constructible without player.py."""
        banned = {"playerManager", "clientManager", "userManager",
                  "syncManager", "eventHandler"}
        offenders = []
        for name, tree in _page_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in banned:
                            offenders.append("%s:%d imports %s"
                                             % (name, node.lineno, alias.name))
        self.assertEqual(offenders, [], "\n  ".join(offenders))


class TestTheEscapeHatchIsShrinking(unittest.TestCase):
    """``ctx.shell`` is transitional. Count it, and only ever downward."""

    @staticmethod
    def _shell_uses():
        uses = []
        for name, tree in _page_sources():
            for node in ast.walk(tree):
                # ctx.shell.X  /  self.ctx.shell.X
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Attribute)
                        and node.value.attr == "shell"):
                    uses.append("%s:%d shell.%s"
                                % (name, node.lineno, node.attr))
                # `shell = self.ctx.shell` then `shell.X` — the common idiom,
                # counted through the binding so the number is honest.
                elif (isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Attribute)
                        and node.value.attr == "shell"):
                    pass          # the binding itself is not a use
        # Uses via a local alias, resolved by name.
        for name, tree in _page_sources():
            aliases = {t.id for node in ast.walk(tree)
                       if isinstance(node, ast.Assign)
                       and isinstance(node.value, ast.Attribute)
                       and node.value.attr == "shell"
                       for t in node.targets if isinstance(t, ast.Name)}
            if not aliases:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in aliases):
                    uses.append("%s:%d shell.%s"
                                % (name, node.lineno, node.attr))
        return sorted(uses)

    def test_the_hatch_is_within_budget(self):
        uses = self._shell_uses()
        self.assertLessEqual(
            len(uses), SHELL_USE_BUDGET,
            "Pages lean on the shell %d times, over the budget of %d. A new "
            "page should take what it needs from PageContext; if a helper it "
            "needs is genuinely missing, extract the helper rather than "
            "raising this number.\n  %s"
            % (len(uses), SHELL_USE_BUDGET, "\n  ".join(uses)))

    def test_the_framework_hatch_does_not_grow(self):
        """base.py is excluded from _page_sources, so its shell uses would
        otherwise be invisible. Pin them."""
        path = os.path.join(PAGES_DIR, "base.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        uses = [n for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Attribute)
                and n.value.attr == "shell"]
        self.assertEqual(
            len(uses), BASE_SHELL_USES,
            "pages/base.py touches the shell %d times, pinned at %d. The "
            "framework's own hatch is excluded from SHELL_USE_BUDGET; that "
            "exclusion is not a place to put new coupling."
            % (len(uses), BASE_SHELL_USES))

    def test_the_budget_is_not_slack(self):
        """A budget nobody tightens is a budget nobody respects.

        Written as `uses >= budget - 2`, which stopped meaning anything the
        moment the budget reached 0: the assertion became `>= -2`, which no
        count can fail. Stated as an exact match instead — at zero there is
        no slack to allow, and any use at all must move the number.
        """
        uses = self._shell_uses()
        if SHELL_USE_BUDGET == 0:
            self.assertEqual(
                len(uses), 0,
                "a page reached for the shell; the budget is zero:\n  "
                + "\n  ".join(uses))
            return
        self.assertGreaterEqual(
            len(uses), SHELL_USE_BUDGET - 2,
            "Pages now use the shell only %d times (budget %d). Lower "
            "SHELL_USE_BUDGET to lock the improvement in."
            % (len(uses), SHELL_USE_BUDGET))


class TestChromeConstantsDoNotDrift(unittest.TestCase):
    """``CONTENT_PAD`` exists in two places during the transition.

    The shell still owns one for its unconverted mixins;
    ``components/chrome.py`` owns the one pages use. They must agree, or a
    converted page lays out at a different padding from an unconverted one
    and the seam becomes visible on screen.

    Caught during the extraction: the first draft of chrome.py used 24, and
    the real value is 16.
    """

    def test_the_two_content_pads_agree(self):
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome

        self.assertEqual(
            chrome.CONTENT_PAD, MpvtkBrowser.CONTENT_PAD,
            "components.chrome.CONTENT_PAD (%r) and "
            "MpvtkBrowser.CONTENT_PAD (%r) have drifted; converted and "
            "unconverted screens would pad differently"
            % (chrome.CONTENT_PAD, MpvtkBrowser.CONTENT_PAD))


class TestPageContextIsSmall(unittest.TestCase):
    """The point of the context is that it is small enough to fake.

    If it grows a field per page, it has become the god object again with an
    extra hop.
    """

    def test_it_has_few_fields(self):
        fields = set(PageContext.__dataclass_fields__)
        # 10 -> 11 for `dialogs` (step 6c, the queue/playlist-edit batch).
        # Raising this is allowed only for a genuine architectural layer, not
        # for one page's convenience: dialogs render in the shell ABOVE the
        # page and outlive navigation, so no page can own one. If a future
        # field is really "screen X needs Y", extract Y instead.
        self.assertLessEqual(
            len(fields), 11,
            "PageContext has grown to %d fields: %s. Adding a field per page "
            "recreates the god object behind an indirection."
            % (len(fields), sorted(fields)))

    def test_it_is_frozen(self):
        """A page must not rewire its own dependencies — that is the shell's
        job, and a page that swapped its own source would browse a server the
        user has left."""
        ctx = PageContext(source=None, server=None, nav=None, run=None,
                          art=None, player=None, actions=None, dialogs=None,
                          status=lambda _s: None, invalidate=lambda: None)
        with self.assertRaises(Exception):
            ctx.source = "something else"


if __name__ == "__main__":
    unittest.main()


class TestThePageCacheDoesNotCollideWithPagination(unittest.TestCase):
    """``route`` is a shared dict, and two features wanted the same key.

    ``_page`` has meant "which page NUMBER of a paginated grid" since long
    before the Page framework existed. Step 6a cached the Page *object* under
    the same name, so ticking Paginated on a library made ``Paginator.ensure``
    compare an int against a GridPage:

        TypeError: '<' not supported between instances of 'int' and 'GridPage'

    The renderer keeps the previous frame when ``build()`` raises, so the
    symptom was the entire browser freezing with nothing logged. Every
    pagination test called ``_ensure_page`` on a bare dict, never through
    ``_page_for``, so 1886 tests passed.
    """

    def _browser(self, route):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser as B
        from tests._shell_harness import FakeSource, _SyncPool

        b = B(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.server = "srv1"
        b._pages.enabled = lambda: True      # the Paginated checkbox
        b.navigate(route)
        return b

    def test_the_keys_are_distinct(self):
        self.assertNotEqual(
            MpvtkBrowser.PAGE_OBJ_KEY, "_page",
            "the Page cache is back on the pagination key")

    def test_a_paginated_grid_renders(self):
        b = self._browser({"kind": "grid", "server": "srv1",
                           "parent_id": "lib1", "title": "Lib"})
        b._render_route(b.route, (1280, 640))      # raised before the fix
        self.assertIsInstance(b.route.get("_page"), int,
                              "_page must stay the page NUMBER")

    def test_a_paginated_music_library_renders(self):
        b = self._browser({"kind": "music", "server": "srv1",
                           "parent_id": "lib1", "_tab": "albums",
                           "title": "Music"})
        b._render_route(b.route, (1280, 640))
        self.assertIsInstance(b.route.get("_page"), int)

    def test_paging_does_not_evict_the_page_object(self):
        """The reverse clobber: Paginator.go writing an int over the cached
        object rebuilt a fresh Page every frame, defeating the
        one-instance-per-route contract in _page_for's own docstring."""
        b = self._browser({"kind": "grid", "server": "srv1",
                           "parent_id": "lib1", "title": "Lib"})
        first = b._page_for(b.route)
        b._pages.go(b.route, 3)
        b._pages.reset(b.route)
        self.assertIs(b._page_for(b.route), first,
                      "paging rebuilt the Page and lost its state")


class TestEveryRouteKindActuallyRenders(unittest.TestCase):
    """Render every route kind, in both pagination modes, and assert nothing
    raises.

    This exists because the ``_page`` collision was not caught by anything.
    ``strict_builds`` (mpvtk/app.py) makes a swallowed build failure loud,
    but it only fires if some test drives the broken screen — and no test
    ticked Paginated on a grid, so there was nothing to be loud about. The
    gap was coverage, not reporting.

    Deliberately a smoke test: it asserts only "did not raise". Anything
    finer belongs in the per-screen tests. What it buys is that a new route
    kind, or a new shared-state collision like this one, cannot reach a user
    without something going red first.
    """

    #: Enough keys to satisfy every loader's route lookups.
    ROUTE = {"server": "srv1", "parent_id": "lib1", "item_id": "m1",
             "person_id": "p1", "series_id": "sh1", "title": "T",
             "term": "q", "_tab": "albums"}

    #: Kinds whose screen is app state rather than library content, and which
    #: need a live controller/compositor to render at all. Covered by
    #: tests/integration/test_mpvtk_auth.py and the cast tests instead.
    SKIP = {"cast", "connecting", "locked", "login"}

    def _kinds(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser as B

        return sorted((set(PAGES) | set(B._routes())) - self.SKIP)

    def test_there_are_kinds_to_render(self):
        self.assertGreater(len(self._kinds()), 12)

    def test_every_kind_renders_in_both_pagination_modes(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser as B
        from tests._shell_harness import FakeSource, _SyncPool

        for kind in self._kinds():
            for paginated in (False, True):
                with self.subTest(kind=kind, paginated=paginated):
                    b = B(app=None, source=FakeSource())
                    b._pool = _SyncPool()
                    b.server = "srv1"
                    b._pages.enabled = lambda p=paginated: p
                    b.navigate(dict(self.ROUTE, kind=kind))
                    b._render_route(b.route, (1280, 640))
