"""The route stack, and the headless lockdown that guards it.

Extracted from ``MpvtkBrowser`` (step 3 of ``docs/archive/ARCHITECTURE_TARGET.md``
§3). The browser kept ``nav_stack`` as a plain list attribute, which meant
the lockdown could be — and was — bypassed by assigning it.

**Headless is enforced here and nowhere else.** In headless (cast-target)
mode the cast screen is the only page and the library is unreachable from
the machine. Every way into the library funnels through :meth:`push` — a
tile click, the tray's "Show Library Browser", a remote's GoHome, the
now-playing bar's Queue button, a ``DisplayContent`` from a phone — so
refusing here is what makes the mode mean something rather than hiding one
entry point and leaving five others open. :meth:`unpop` is the one other
door — the forward stack can outlive the setting being switched on — and it
asks :meth:`allows` again rather than trusting that its route was legal when
it was pushed.

It is deliberately **not a security boundary**: the tray still reaches
Settings. ``tests/test_mpvtk_headless.py`` enumerates every door and has a
catch-all so a newly added route is refused by default.

**Why this is a class and not a list.** ``_default_route``'s docstring used
to carry the warning that "every direct ``nav_stack`` assignment must come
through here", because a successful connect once put a headless box on the
library: ``set_source`` reset the stack itself and the refusal never ran.
A warning in prose does not survive a decomposition. Owning the list
privately means there is no attribute to assign, and
``tests/test_source_invariants.py`` checks that no other module mutates it.
"""

import logging

log = logging.getLogger("mpvtk_browser.navigator")

#: Routes headless mode still allows. Everything else is the library.
HEADLESS_ROUTES = {"cast", "connecting", "locked"}


class Navigator:
    """Owns the route stack. Knows nothing about loading or rendering — the
    shell does that after asking for a stack change."""

    def __init__(self, default_route, is_headless=None):
        """``default_route()`` supplies a fresh landing route (headless-aware,
        so it is the shell's to decide). ``is_headless()`` is read live rather
        than captured: the flag can change when settings are saved."""
        self._default_route = default_route
        self._is_headless = is_headless or (lambda: False)
        self._stack = [default_route()]
        #: Routes popped by :meth:`pop`, oldest first, so the top is the page
        #: an immediate forward returns to. Browser semantics: it survives
        #: going back and forth, and any *new* navigation discards it.
        self._forward = []

    # -- reading -----------------------------------------------------------

    @property
    def route(self):
        """The route currently on screen."""
        return self._stack[-1]

    @property
    def stack(self):
        """The live stack.

        Returned as the real list, not a copy: ``build()`` reads it every
        frame and two mixins check its depth to decide whether to draw a back
        button. Mutating it from outside is what the invariant test forbids.
        """
        return self._stack

    @property
    def depth(self):
        return len(self._stack)

    @property
    def can_go_back(self):
        return len(self._stack) > 1

    @property
    def forward(self):
        """The forward stack, oldest first. Read for the history menu."""
        return self._forward

    @property
    def can_go_forward(self):
        return bool(self._forward) and self.allows(self._forward[-1])

    # -- the lockdown ------------------------------------------------------

    def allows(self, route, force=False):
        """Whether ``route`` may be shown. ``force`` is for the screens
        headless itself needs to reach."""
        if force or not self._is_headless():
            return True
        return route.get("kind") in HEADLESS_ROUTES

    # -- writing -----------------------------------------------------------

    def push(self, route, reset=False, force=False):
        """Put ``route`` on top. Returns True if the stack changed.

        A False return means the lockdown refused it, and the caller must not
        run the load/render side effects that normally follow.
        """
        if not self.allows(route, force):
            log.debug("headless: refusing navigation to %r", route.get("kind"))
            return False
        if reset:
            self._stack = []
        self._stack.append(route)
        # Branching away drops the forward history, as a browser does: the
        # pages it held are no longer "where you were going".
        self._forward = []
        return True

    def pop(self):
        """Leave the current route. Returns the route left, or None at the
        root (where there is nothing to go back to)."""
        if not self.can_go_back:
            return None
        left = self._stack.pop()
        self._forward.append(left)
        return left

    def unpop(self):
        """Go forward again: return to the last route :meth:`pop` left.
        Returns that route, or None when there is nothing ahead.

        The lockdown is re-checked rather than assumed. Headless can be
        switched on from Settings while library pages sit in the forward
        stack, and a route that was legal when it was pushed is not
        evidence about now — this is the one entry to the library that does
        not come through :meth:`push`.
        """
        if not self._forward:
            return None
        route = self._forward[-1]
        if not self.allows(route):
            log.debug("headless: refusing forward to %r", route.get("kind"))
            return None
        self._forward.pop()
        self._stack.append(route)
        return route

    def fast_forward(self, depth):
        """Go forward until the stack is ``depth`` deep. Returns the route
        landed on, or None if nothing moved.

        The history menu's other half. Stops early at the first route the
        lockdown refuses, which is the same answer repeated forward presses
        would give.
        """
        landed = None
        while len(self._stack) < depth:
            route = self.unpop()
            if route is None:
                break
            landed = route
        return landed

    def rewind_to(self, depth):
        """Go back to ``depth`` pages deep (1 is the root), pushing every
        route left onto the forward stack. Returns the routes left, nearest
        first — empty if nothing moved.

        The history menu's jump. Repeated `pop` rather than a slice so the
        forward stack ends up exactly as it would after that many single
        back presses. It returns *what* it left for the same reason: the
        shell's half of a back press is decided by the page being left (a
        playlist editor makes what is underneath stale), so a caller that
        only knew the destination could not reproduce it.
        """
        if not 1 <= depth < len(self._stack):
            return []
        left = []
        while len(self._stack) > depth:
            left.append(self.pop())
        return left

    def replace(self, routes):
        """Set the whole stack, falling back to the default route if what is
        left is empty.

        Deliberately unfiltered: callers have already decided (a playlist was
        deleted, the data source changed underneath). The headless guarantee
        rests on ``default_route()`` being headless-aware, which is why that
        is a callback rather than a constant.

        The forward stack goes with it. It was collected against the stack
        being replaced, so keeping it would offer a way forward into pages
        from a history that just stopped applying.
        """
        self._stack = list(routes) or [self._default_route()]
        self._forward = []

    def reset_to_default(self):
        self._stack = [self._default_route()]
        self._forward = []

    def prune(self, keep):
        """Drop every route for which ``keep(route)`` is false.

        Used when the thing a route points at stops existing — a deleted
        playlist. Never empties the stack; the default route backfills.

        Filters the forward stack and keeps what is left, rather than
        letting `replace` clear it wholesale: a deleted playlist is no
        reason to forget a history that mostly does not mention it. Without
        the filter the dead page this method exists to remove would still
        be one forward press away.
        """
        ahead = [r for r in self._forward if keep(r)]
        self.replace([r for r in self._stack if keep(r)])
        self._forward = ahead
