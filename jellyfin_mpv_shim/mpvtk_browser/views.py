"""What is left of the main content routes.

Every kind this mixin owned is a Page now (``pages/``). What remains is a
handful of forwarders that unconverted callers and the tests still reach as
methods on the browser, plus the SORTS re-export. It shrinks to nothing as
those callers move; see ``docs/ARCHITECTURE_TARGET.md`` §3.2.
"""

from ..i18n import _
from .components import chrome, detail
# Grid sort modes live with the page that owns them; re-exported because
# app.py and the tests have always imported them from here.
from .pages.grid import SORTS  # noqa: F401


class ViewsMixin:

    # Content-area primitives now live in components/chrome.py; these stay as
    # thin aliases until every caller is a Page taking them from its context.
    _error = staticmethod(chrome.error)
    _paragraph = staticmethod(chrome.paragraph)

    def _body_w(self, w):
        return chrome.body_width(w, self.CONTENT_PAD)


    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    #: Every kind this mixin owned is now a Page (pages/). It keeps only
    #: the forwarders unconverted routes still call as methods.
    ROUTES: dict = {}

    def _square_geom(self, items):
        return self.tiles.square_geom(items)

    def _toggle_paginated(self):
        self._pages.toggle(self.route)

    _meta_line = staticmethod(detail.meta_line)



    def _common_actions(self, item, server, prefix):
        return detail.common_actions(self._actions, self.tiles, item, server,
                                     prefix)

    def _download_btn(self, item, server, prefix):
        return detail.download_button(self._actions, self.tiles, item, server,
                                      prefix)

    def _remove_download(self, item):
        self._actions.remove_download(item)

    def _play_next_up(self, series_id, server):
        self._actions.play_next_up(series_id, server)

    def _shuffle_series(self, series_id, server):
        self._actions.shuffle_series(series_id, server)

    def _act_watched(self, item, server):
        self._actions.toggle_watched(item, server)

    def _act_favorite(self, item, server):
        self._actions.toggle_favorite(item, server)

    def _people_row(self, people, server):
        return detail.people_row(self.tiles, people)


    def _search(self, term):
        term = (term or "").strip()
        if not term:
            return
        self.navigate({"kind": "search", "server": self.server,
                       "term": term, "title": _("Search")})


    # ---------------------------------------- route loaders
