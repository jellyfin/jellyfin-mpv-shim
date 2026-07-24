"""Page registry — the replacement for the mixins' ``ROUTES`` tables.

A kind listed here is served by a :class:`~.base.Page`; anything absent falls
back to the shell's ``ROUTES`` lookup, so the conversion proceeds one route at
a time with the app shippable throughout. That fallback is the whole reason
this can be done incrementally rather than as a 19-route big bang.

``tests/test_page_contract.py`` checks that no kind is claimed by both a page
and a ``ROUTES`` table — a duplicate would resolve by whichever the shell
consulted first, which is exactly the silent-winner hazard
``test_mpvtk_browser_mixins.py`` was written for.
"""

from .base import Page, PageContext
from .home import HomePage
from .search import SearchPage

#: kind -> Page subclass.
PAGES = {
    HomePage.kind: HomePage,
    SearchPage.kind: SearchPage,
}

__all__ = ["PAGES", "Page", "PageContext"]
