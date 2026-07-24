"""The route stack, and the headless lockdown that guards it.

Extracted from ``MpvtkBrowser`` (step 3 of ``docs/ARCHITECTURE_TARGET.md``
§3). The browser kept ``nav_stack`` as a plain list attribute, which meant
the lockdown could be — and was — bypassed by assigning it.

**Headless is enforced here and nowhere else.** In headless (cast-target)
mode the cast screen is the only page and the library is unreachable from
the machine. Every way into the library funnels through :meth:`push` — a
tile click, the tray's "Show Library Browser", a remote's GoHome, the
now-playing bar's Queue button, a ``DisplayContent`` from a phone — so
refusing here is what makes the mode mean something rather than hiding one
entry point and leaving five others open.

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
        return True

    def pop(self):
        """Leave the current route. Returns the route left, or None at the
        root (where there is nothing to go back to)."""
        if not self.can_go_back:
            return None
        return self._stack.pop()

    def replace(self, routes):
        """Set the whole stack, falling back to the default route if what is
        left is empty.

        Deliberately unfiltered: callers have already decided (a playlist was
        deleted, the data source changed underneath). The headless guarantee
        rests on ``default_route()`` being headless-aware, which is why that
        is a callback rather than a constant.
        """
        self._stack = list(routes) or [self._default_route()]

    def reset_to_default(self):
        self._stack = [self._default_route()]

    def prune(self, keep):
        """Drop every route for which ``keep(route)`` is false.

        Used when the thing a route points at stops existing — a deleted
        playlist. Never empties the stack; the default route backfills.
        """
        self.replace([r for r in self._stack if keep(r)])
