"""The main content routes.

Home, grid (a library), detail, series, season and search, plus the
detail-page pieces: track pickers, action buttons and the media-info line.

State on ``self``: none of its own — every view keeps its data in the route
dict and every mutation ends with ``invalidate()``. Handlers here run on
the loop thread and must capture route state *before* dispatching async
work; reading ``self.route`` inside the callback races navigation.
"""

import logging

from ..i18n import _
from ..mpvtk.scaling import px
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dropdown,
    Icon,
    Row,
    Spacer,
    Text,
    VScroll,
)
from . import components, home_sections, theme
from .components import chrome, controls, detail

log = logging.getLogger("mpvtk_browser.views")

# Grid sort modes now live with the page that owns them; re-exported
# because app.py and the tests have always imported them from here.
from .pages.grid import SORTS  # noqa: E402,F401

_LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"


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

    def _reload_grid(self, route):
        for k in ("_items", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        self._reset_pagination(route)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _set_grid(self, key, route, value):
        route[key] = value
        self._reload_grid(route)

    def _set_grid_filter(self, route, key, value):
        route.setdefault("_filters", {})[key] = value
        self._reload_grid(route)

    def _toggle_grid_filter(self, route, key):
        f = route.setdefault("_filters", {})
        f[key] = not f.get(key)
        self._reload_grid(route)

    def _toggle_paginated(self):
        self._pages.toggle(self.route)

    _meta_line = staticmethod(detail.meta_line)



    _fmt_ticks = staticmethod(detail.fmt_ticks)

    _action_btn = staticmethod(controls.action_btn)

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
