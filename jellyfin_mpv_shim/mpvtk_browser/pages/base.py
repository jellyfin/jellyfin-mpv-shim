"""``Page`` and ``PageContext`` — one class per screen instead of two methods
on a 358-method object.

Step 6 of ``docs/archive/ARCHITECTURE_TARGET.md`` §3, and the one the earlier steps
existed to make safe. A route kind is currently a ``(loader, renderer)`` pair
of method *names* on ``MpvtkBrowser``, resolved with ``getattr``, operating on
a shared mutable ``route`` dict that navigation and five mixins can all reach
into. A page becomes a class that owns its own state and receives what it
needs.

**The escape hatch is deliberate, documented and measured.**

A page needs six services (below) — and, today, a handful of helpers that are
still methods on the shell: the tile/row/grid builders, the chrome's busy and
error nodes. Those are genuinely component-shaped (``docs/ARCHITECTURE_TARGET``
§1.4) but they still close over ``self.strips`` / ``self._posters``, so
extracting them is its own step. Rather than pretend otherwise, ``ctx.shell``
exposes the browser and pages may use it *for those helpers only*.

That is a strangler-fig seam, not a loophole, and the difference is that it is
counted: ``tests/test_page_contract.py`` pins the number of ``ctx.shell``
references and fails if it grows. It can only go down.

As of step 6c the page budget is **zero** — every converted page takes what
it needs from its context. One use remains, in this file: ``route_async``
below. That one is not transitional. Recording a load failure has to decide
whether this route is *still the screen* before dropping the user to the
offline home, and only the shell knows that. It is pinned by
``BASE_SHELL_USES`` rather than budgeted, so the framework's hatch cannot
quietly become the place new coupling goes.

Converting a route is mechanical:

1. subclass :class:`Page`, set ``kind``;
2. move the loader body into ``load()`` and the renderer body into
   ``render(size)``, replacing ``self.X`` with ``self.ctx.X`` or
   ``self.ctx.shell.X``;
3. register it in ``pages/__init__.py``;
4. delete the two methods and the ``ROUTES`` entry.

Unconverted kinds keep working: the shell falls back to its ``ROUTES`` table
for anything the registry does not claim, so this proceeds one page at a time
with the app shippable throughout.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("mpvtk_browser.pages.base")


@dataclass(frozen=True)
class PageContext:
    """Everything a page is allowed to depend on.

    Six names instead of the 413 distinct ``self.*`` the mixins can reach.
    Small enough to fake, which is the point: a page test constructs the page
    and nothing else.

    Frozen because a page must not rewire its own dependencies — that is the
    shell's job, and a page that swapped its own source would be invisible to
    everything that reasons about navigation.
    """

    #: Data. A ``LibrarySource`` or an ``OfflineLibrarySource``; a page must
    #: not care which, which is what makes offline browsing work at all.
    source: Any
    #: uuid of the server being browsed. Read-only for pages.
    server: Optional[str]
    #: Navigation. ``navigate`` / ``go_back`` — never the raw stack.
    nav: Any
    #: Off-thread work, epoch-guarded. See ``async_runner.AsyncRunner``.
    run: Any
    #: Render resources: the strip store, the thumbnail store, tile geometry,
    #: and ``node_rect(id)`` — a node's laid-out rect from the **last pushed
    #: scene**, which is the toolkit's answer (GUIDE §2) for content that has
    #: to be *rasterized* at the size layout gives it. An image cannot flex,
    #: so a page drawing one full-bleed measures the hole on one frame and
    #: fills it on the next. A page that only needs to *place* things should
    #: flex instead.
    art: Any
    #: The player and everything outside the package. A ``PlayerGateway``.
    player: Any
    #: What the user does *to* an item: play, mark watched, download. An
    #: ``item_actions.ItemActions``. Separate from ``player`` because these
    #: are orchestrated actions (optimistic write, rollback, dialog, toast),
    #: not the raw capability the gateway exposes.
    actions: Any
    #: The shell's dialog layer: ``confirm``, ``message``, ``add_to``,
    #: ``open_download``. A distinct layer rather than a page convenience —
    #: a dialog renders in the shell, ABOVE whatever page is showing, and
    #: outlives navigation. A page asks for one; it never draws one.
    dialogs: Any
    #: Transient user-facing status (the toast). ``status(str)``.
    status: Callable[[str], None]
    #: Wake the render loop after writing state from any thread.
    invalidate: Callable[[], None]

    #: **Escape hatch, shrinking.** The browser shell, for helpers that have
    #: not been extracted into ``components/`` yet — tile rows, track lists,
    #: the busy and error nodes. Counted by ``tests/test_page_contract.py``;
    #: every use is a TODO with a number attached.
    shell: Any = None


class Page:
    """One screen.

    ``load()`` runs on navigation and may go off-thread via ``ctx.run``;
    ``render(size)`` runs on the loop thread and returns a widget tree for the
    **content area only** — the shell owns chrome, dialogs and the
    now-playing bar.
    """

    #: Route kind this page claims. Must be unique across the registry.
    kind: str = ""

    def __init__(self, ctx: PageContext, route: dict):
        self.ctx = ctx
        #: This page's own state. Still the shared route dict during the
        #: transition, so ``go_back`` and ``after_playlist_deleted`` keep
        #: working unchanged; it becomes private fields once every kind is a
        #: page and nothing else indexes into it.
        self.route = route

    # -- lifecycle ---------------------------------------------------------

    def load(self, epoch: int) -> None:
        """Fetch what the screen needs. Called once per navigation, on the
        loop thread; do the actual work through ``self.ctx.run``.

        ``epoch`` is read by the shell on the loop thread and handed down —
        a page that read it later would be racing the navigation it guards.
        """

    def render(self, size) -> Any:
        """Build the content-area widget tree. Loop thread."""
        raise NotImplementedError

    def close(self) -> None:
        """This page has stopped being the screen. Loop thread.

        Called once, when the shell renders a *different* page — not on
        navigation, because navigation happens on threads the render loop
        does not own (a websocket, mpv's event thread) and this may touch
        the player.

        Almost no page needs it. It exists for the two that take something
        the window can only hold one of: the comic reader hands mpv a
        picture, which nothing else would take down, and it extracts pages
        to files, which nothing else would delete. A page that only holds
        memory should let the caches do their job instead.

        It may be followed by another ``load()`` — going back returns to a
        route whose dict is still here — so it must leave the page usable
        rather than spent.
        """

    # -- convenience -------------------------------------------------------

    def parked_scroll(self, scroll_id):
        """Where ``scroll_id`` was when this route was last navigated away
        from, for passing to ``VScroll(offset=...)``. None on a first visit.

        The shell parks the offsets on the route dict, which is what lets
        them survive the ``ScrollState.reset()`` that stops one view's
        offsets bleeding into the next under the same container id. The
        renderer applies it once, clamped to the content it actually has —
        see ``Scroll``.
        """
        from ..scroll_state import ScrollState

        return ScrollState.parked(self.route, scroll_id)

    def open_link(self, url):
        """Hand an external link to the desktop's browser.

        On ``Page`` rather than on the one screen that first wanted it,
        because three screens do (detail, series, season) and a copy per
        screen is how the error handling ends up different on each.

        Reports a failure rather than swallowing it: pressing a button and
        having nothing at all happen is indistinguishable from a dead UI,
        and that is the outcome on a box with no desktop opener -- or for a
        scheme ``system_open`` refuses, which these links can carry because
        the *server* composed them. The gateway answers ``(ok, method)`` for
        exactly this.
        """
        from ...i18n import _

        try:
            ok, _method = self.ctx.player.open_url(url)
        except Exception:
            log.warning("could not open an external link", exc_info=True)
            ok = False
        if not ok:
            self.ctx.status(_("Could not open that link."))

    def route_async(self, work, on_done, epoch) -> None:
        """``ctx.run`` for this page's data, recording a failure on the route
        so the view can offer a retry instead of spinning forever."""
        self.ctx.shell._route_async(self.route, work, on_done, epoch)
