"""``Page`` and ``PageContext`` — one class per screen.

A page owns its own state and is handed what it needs: ``load(epoch)`` on
navigation, ``render(size)`` on the loop thread, both against a
``PageContext`` of ten names rather than the 413 distinct ``self.*`` the
mixins can reach.

**``ctx.shell`` is an escape hatch, and what makes it one rather than a
loophole is that it is counted** — ``tests/test_page_contract.py`` pins the
number of references and it can only go down. The page budget is zero; the
one use left is ``route_async`` below, which is pinned by ``BASE_SHELL_USES``
rather than budgeted because it is not transitional.

Adding a view means adding a page, and converting an unconverted route is a
four-step recipe with the app shippable throughout. Both, and why the hatch
and the budget are shaped this way: see docs/browser-shell.md section 1.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("mpvtk_browser.pages.base")


@dataclass(frozen=True)
class PageContext:
    """Everything a page is allowed to depend on.

    Ten names instead of the 413 distinct ``self.*`` the mixins can reach.
    Small enough to fake, which is the point: a page test constructs the page
    and nothing else. Frozen because a page that rewired its own dependencies
    would be invisible to everything that reasons about navigation.
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

        Called once, when the shell renders a *different* page -- not on
        navigation, which happens on threads the render loop does not own and
        this may touch the player. Almost no page needs it, and it may be
        followed by another ``load()``, so it must leave the page usable
        rather than spent. Which pages need it: see docs/browser-shell.md
        section 1.
        """

    # -- convenience -------------------------------------------------------

    def parked_scroll(self, scroll_id):
        """Where ``scroll_id`` was when this route was last navigated away
        from, for passing to ``VScroll(offset=...)``. None on a first visit.

        The shell parks the offsets on the route dict, which is what makes
        them survive the ``ScrollState.reset()`` that stops one view's
        offsets bleeding into the next; see docs/browser-shell.md section 7.

        **A restore is a one-shot**, which is why this goes through
        ``ScrollState.pending`` rather than reading the route directly: the
        parked value is not a standing order. ``offset=`` only bites the
        first frame an id is seen (widgets.Scroll), and an in-place reload
        makes an id un-seen — a sort or filter change drops ``_items``, the
        page draws a spinner where the scroller was, and the container that
        comes back with the results is new to the renderer. Reading the
        route re-commanded it to wherever the user had been on the *previous*
        visit, so every filter tick jumped the library back down.
        """
        return self.ctx.art.scroll.pending(scroll_id)

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

    def web_url(self, item):
        """This item's page in jellyfin-web, or None (#714).

        Beside :meth:`open_link` and for the same reason: the three screens
        that draw the provider row all want it, and the composition —
        which server, and what web calls the route — is one answer, not
        three. ``None`` offline, where there is no server to send anyone to.
        """
        from ..components import detail

        source = self.ctx.source
        server = self.route.get("server") or self.ctx.server
        return detail.jellyfin_web_url(source.server_address(server), item)

    def route_async(self, work, on_done, epoch) -> None:
        """``ctx.run`` for this page's data, recording a failure on the route
        so the view can offer a retry instead of spinning forever."""
        self.ctx.shell._route_async(self.route, work, on_done, epoch)
