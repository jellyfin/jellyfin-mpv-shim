"""Every screen, loaded and rendered against a real library.

This replaces a manual step. `docs/REGRESSION_CHECKLIST_2026-07-25.md` §1 is:

    Walk each route once: home, library grid, person, detail, series, season,
    search, playlists, playlist editor, queue editor, music library (all five
    tabs), album, artist, genre, downloads, settings, cast screen.
    **Then grep `log.txt` for `scene build failed`.** This is the important
    half.

It is the important half because `strict_builds` is off in production: a build
exception keeps the last good frame, so a broken route does not look like a
crash — it looks like a UI that ignored the click. `b97dd523` is what that
costs. `get_channel_listing` passed `enable_images=False` to a client method
with no such argument, so opening a Live TV channel raised `TypeError` before
the fetch, and nothing found it until a human opened that screen.

Every route here is driven the way production drives it — `navigate()` through
the real `Navigator`, the real page `load()` against real DTOs, then a real
`build()` and `layout()`. What is *not* here is mpv: the failures this catches
live in load and build, and dropping the window makes it a contract-tier test
that runs once in about a second instead of a windowed one per backend. The
real-window rendering path already has coverage in
`tests/integration/test_mpvtk_browser.py`.

Three things are checked per screen, and the order they were added is the
lesson. **(1)** the build does not raise — `app=None` means `build()` is
called directly rather than through the app's guarded wrapper, so nothing
swallows it. **(2)** `route["_error"]` is unset. **(3)** nothing logged a
failure. A negative control that reintroduced `b97dd523` passed against (1)
alone, because `_route_async` catches a failing loader, records the error on
the route and leaves it empty — and an empty route builds perfectly. (2) is
what caught it. (3) is the belt, and it hangs off the **root** logger: the two
loggers that matter are named `mpvtk` and `mpvtk_browser.async_runner`,
outside the package hierarchy, so a handler on `jellyfin_mpv_shim` sees
neither — which is how the first version of the trap collected nothing while
the failure printed to the console beside it.
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SIZE = (1280, 720)

#: Window chrome, excluded from the interaction sweep — see `_interact`.
CHROME_PREFIXES = ("nav-", "chrome-", "topbar-", "bar-")


#: A route whose loader raised carries this, set by `_route_async`. The
#: primary signal, because it depends on no logging configuration and names
#: the route that broke.
ROUTE_ERROR_KEY = "_error"


#: The log lines that mean a screen broke. This is the automated form of the
#: checklist's "then grep `log.txt` for `scene build failed`", widened by what
#: a negative control showed: reintroducing `b97dd523` (a TypeError in the
#: channel page's fetch) did NOT fail a walk that only watched `build()`,
#: because `AsyncRunner` catches a failing loader, logs it, and leaves the
#: route empty — and an empty route builds perfectly. The load is where these
#: bugs actually land, and the log is the only place it is recorded.
FAILURE_LOGS = (
    "async work failed",
    "async on_error failed",
    "scene build failed",
)


class _LogTrap(logging.Handler):
    """Collect the failure lines above while a route is walked."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        if any(message.startswith(sig) for sig in FAILURE_LOGS):
            self.records.append(record)

    def drain(self):
        found, self.records = self.records, []
        return found

    def describe(self):
        out = []
        for record in self.records:
            text = record.getMessage()
            if record.exc_info:
                text += ": %s" % (record.exc_info[1],)
            out.append(text)
        return out


class _SyncPool:
    """Run route loaders inline, so a fetch completes before the render.

    The real pool is threaded and the shell tests substitute this to make
    ordering deterministic. Here the work it runs inline is a *real* request,
    which is the point: `navigate()` fetches, then `build()` draws what came
    back.
    """

    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


@_e2e.require_server
class RouteWalkTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser, PAGES
        cls.PAGES = PAGES
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        cls.libraries = cls.source.get_libraries(_e2e.SOURCE_UUID)
        cls.by_name = {lib["Name"]: lib for lib in cls.libraries}
        cls._browser_cls = MpvtkBrowser

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def setUp(self):
        # ROOT, not the `jellyfin_mpv_shim` logger. The two that matter are
        # named `mpvtk` and `mpvtk_browser.async_runner` — outside the package
        # hierarchy, so a handler on the package name sees neither. That is
        # exactly how the first version of this trap caught nothing while the
        # failure printed to the console beside it.
        self.trap = _LogTrap()
        root = logging.getLogger()
        root.addHandler(self.trap)
        self.addCleanup(root.removeHandler, self.trap)
        self.browser = self._browser_cls(
            app=None, source=self.source, server_uuid=_e2e.SOURCE_UUID)
        # `__init__` already kicked the home load onto the runner's real
        # threaded pool (app.py:408), so swapping the pool afterwards does not
        # catch it. Drain that one first — otherwise it lands *after*
        # tearDownClass has logged the session out, which shows up as a
        # cascade of 401s and a render that asserted against a spinner.
        self.browser._async._pool.shutdown(wait=True, cancel_futures=True)
        self.browser._pool = _SyncPool()
        # Now redo it inline, so the route holds real data before anything
        # renders.
        self.browser._load_route(self.browser.route)
        self.assertEqual(
            self.trap.describe(), [],
            "the home screen failed to load before the walk even started")
        self.trap.drain()
        self.addCleanup(self._shutdown_browser)

    def _shutdown_browser(self):
        try:
            self.browser.shutdown()
        except Exception:
            pass

    # -- the walk ----------------------------------------------------------

    def _render(self, label):
        """Build and lay the current route out, as a frame would.

        `app=None` means there is no `MpvtkApp` to carry `strict_builds`, so
        the exception surfaces here directly — `build()` is called rather than
        the app's guarded wrapper, and anything the page raises comes straight
        out with the route named.
        """
        from jellyfin_mpv_shim.mpvtk.layout import layout
        try:
            tree = self.browser.build(SIZE)
            nodes, handlers = layout(tree, *SIZE)
            self._handlers = handlers
        except Exception as exc:
            raise AssertionError(
                "%s raised while building its scene against real data: "
                "%s: %s" % (label, type(exc).__name__, exc)) from exc
        self.assertTrue(
            nodes,
            "%s built an empty scene — it drew nothing at all" % label)
        # The half that actually catches things. A loader that raised was
        # caught, recorded on the route, and left it empty; the scene above
        # then built fine and proved nothing. A negative control that
        # reintroduced b97dd523 passed until this was added.
        self.assertIsNone(
            self.browser.route.get(ROUTE_ERROR_KEY),
            "%s failed to load against real data: %s"
            % (label, self.browser.route.get(ROUTE_ERROR_KEY)))
        broken = self.trap.describe()
        self.trap.drain()
        self.assertEqual(
            broken, [],
            "%s logged a failure while loading or building against real "
            "data: %s" % (label, broken))
        return nodes

    def _interact(self, label):
        """Right-click, scroll and hover every content node, then repaint.

        The route walk above only *loads and renders* each screen, and two
        real crashes fired on interaction instead: an `AttributeError` deep
        in `layout.py` from right-clicking, and a raster `ValueError` while
        scrolling the library that logged "scene build failed; keeping the
        previous frame" every frame. Both look like a UI that ignored the
        gesture, because that is exactly what a kept frame is.

        Best-effort by design — a screen with no tiles has nothing to
        right-click and that is not a failure. What is asserted is that
        nothing raised and nothing logged.

        **Verified on the home rows only.** A negative control that made
        `TilesMixin._open_tile_menu` raise is caught on `test_home` and NOT on
        the grid, detail or music screens: they build the same
        `TileRenderer.image_map` lambda but wire `on_context` to something
        else, so that control never reaches them. So this sweep is known to
        fire and known to propagate on one path, and unproven on the others —
        do not read a green run here as "right-click is covered everywhere".
        Closing that means finding each page's `on_context` and controlling
        against it, or making the failure observable (the two real crashes
        both surfaced as `scene build failed`, which the trap does catch).
        """
        for event in ("hover", "context", "scroll", "hover_end"):
            # CONTENT nodes only. The chrome carries these events too, and
            # firing e.g. nav-back's context first can navigate away — taking
            # the tiles this is actually about out of the scene, so the sweep
            # silently stopped testing anything. (It did: a negative control
            # that made right-click raise was caught on the home screen and
            # not on a grid.)
            targets = [i for i, _h in self.handlers_for(event)
                       if not any(i.startswith(p) for p in CHROME_PREFIXES)]
            for node_id in targets[:3]:
                if node_id not in self._handlers:
                    continue        # the scene moved on; nothing to fire
                handler = self._handlers[node_id].get(event)
                if handler is None:
                    continue
                try:
                    if event == "scroll":
                        handler(240.0, 4000.0)
                    elif event == "context":
                        handler(400.0, 300.0)
                    else:
                        handler()
                except TypeError:
                    # Signatures differ per event; a mismatch here is the
                    # test's problem, not the app's.
                    continue
                except Exception as exc:
                    raise AssertionError(
                        "%s raised on %s of %s against real data: %s: %s"
                        % (label, event, node_id, type(exc).__name__, exc)
                    ) from exc
                # Repaint with the gesture's state applied — an open menu is
                # where the layout crash lived, so the build MUST happen
                # while it is still up.
                self._render("%s after %s" % (label, event))
            if event == "context":
                self.browser._close_menu()
                self._render("%s after dismissing the menu" % label)

    def handlers_for(self, event):
        return [(i, h) for i, h in self._handlers.items() if event in h]

    def _walk(self, label, route):
        self.browser.navigate(dict(route))
        self.assertEqual(self.browser.route.get("kind"), route["kind"],
                         "%s did not become the current route" % label)
        # The loader runs inline, so anything still marked loading means the
        # fetch never happened — and a screen rendered mid-load draws a
        # spinner, which builds perfectly and proves nothing. Without this
        # the whole walk passes against a server it never reached.
        self.assertFalse(
            self.browser.route.get("_loading"),
            "%s is still loading after an inline fetch, so its scene would "
            "be a spinner rather than the screen" % label)
        return self._render(label)

    def _item(self, library, item_type, fields=None):
        items = self.session.find_all(library=library, item_type=item_type,
                                      fields=fields, Limit=1)
        if not items:
            self.skipTest("no %s in %r" % (item_type, library))
        return items[0]

    def _base(self, item):
        return {"server": _e2e.SOURCE_UUID, "item_id": item.get("Id"),
                "title": item.get("Name", "")}

    # -- the screens -------------------------------------------------------

    def test_home(self):
        nodes = self._render("home")
        self.assertTrue(nodes)
        self._interact("home")

    def test_library_grids(self):
        """Every library, not one: the grid classifies itself by collection
        type, so a books or photos library takes a different path from movies.
        """
        for lib in self.libraries:
            if lib.get("CollectionType") == "livetv":
                continue                      # its own page kind, below
            with self.subTest(library=lib["Name"]):
                kind = "music" if lib.get("CollectionType") == "music" else "grid"
                self._walk("grid:%s" % lib["Name"],
                           {"kind": kind, "server": _e2e.SOURCE_UUID,
                            "parent_id": lib["Id"], "title": lib["Name"],
                            "collection_type": lib.get("CollectionType")})

    def test_detail_series_and_season(self):
        series = self._item("Shows", "Series")
        self._walk("series", dict(self._base(series), kind="series"))

        seasons = self.source.get_seasons(_e2e.SOURCE_UUID, series["Id"])
        self.assertTrue(seasons, "the series has no seasons")
        self._walk("season", dict(self._base(seasons[0]), kind="season",
                                  series_id=series["Id"]))

        episodes = self.source.get_episodes(
            _e2e.SOURCE_UUID, series["Id"], seasons[0]["Id"])
        self.assertTrue(episodes, "the season has no episodes")
        self._walk("detail:episode",
                   dict(self._base(episodes[0]), kind="detail"))

        movie = self._item("Movies", "Movie")
        self._walk("detail:movie", dict(self._base(movie), kind="detail"))

    def test_music_screens(self):
        music = self.by_name.get("Music")
        if music is None:
            self.skipTest("no Music library")
        albums = self.source.get_music_albums(
            _e2e.SOURCE_UUID, music["Id"], start_index=0, limit=1)[0]
        self.assertTrue(albums, "no albums")
        self._walk("album", dict(self._base(albums[0]), kind="album"))

        artists = self.source.get_album_artists(
            _e2e.SOURCE_UUID, music["Id"], start_index=0, limit=1)[0]
        if artists:
            self._walk("artist", dict(self._base(artists[0]), kind="artist"))

        genres = self.source.get_music_genres(_e2e.SOURCE_UUID, music["Id"])
        if genres:
            self._walk("music_genre",
                       dict(self._base(genres[0]), kind="music_genre",
                            parent_id=music["Id"]))

    def test_person(self):
        movie = self._item("Movies", "Movie", fields="People")
        people = (self.source.get_item(_e2e.SOURCE_UUID, movie["Id"])
                  or {}).get("People") or []
        if not people:
            self.skipTest("no People on this item — nothing to open")
        self._walk("person", dict(self._base(people[0]), kind="person",
                                  person_id=people[0].get("Id")))

    def test_search(self):
        route = {"kind": "search", "server": _e2e.SOURCE_UUID, "query": "the"}
        self._walk("search", route)

    def test_favorites_and_genres(self):
        self._walk("favorites",
                   {"kind": "favorites", "server": _e2e.SOURCE_UUID})
        movies = self.by_name.get("Movies")
        if movies is not None:
            self._walk("genres", {"kind": "genres",
                                  "server": _e2e.SOURCE_UUID,
                                  "parent_id": movies["Id"],
                                  "title": "Genres"})

    def test_playlists_and_queue(self):
        playlists = self.source.get_playlists(_e2e.SOURCE_UUID)
        if playlists:
            self._walk("playlist",
                       dict(self._base(playlists[0]), kind="playlist"))
            self._walk("playlist_edit",
                       dict(self._base(playlists[0]), kind="playlist_edit"))
        self._walk("queue", {"kind": "queue", "server": _e2e.SOURCE_UUID})

    def test_live_tv_screens(self):
        """The screens `b97dd523` took down. The channel page is the one that
        raised before it fetched anything."""
        if not self.source.has_live_tv(_e2e.SOURCE_UUID):
            self.skipTest("no Live TV on this server")
        live = [l for l in self.libraries
                if l.get("CollectionType") == "livetv"]
        self.assertTrue(live, "has_live_tv but no Live TV view")
        self._walk("livetv", {"kind": "livetv", "server": _e2e.SOURCE_UUID,
                              "parent_id": live[0]["Id"],
                              "title": live[0]["Name"]})

        channels = self.source.get_channels(_e2e.SOURCE_UUID)[0]
        self.assertTrue(channels, "no channels")
        self._walk("channel", dict(self._base(channels[0]), kind="channel",
                                   _seed=channels[0]))

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        guide = self.source.get_guide(
            _e2e.SOURCE_UUID, [c["Id"] for c in channels], now,
            now + datetime.timedelta(hours=2))
        self.assertTrue(guide, "no programmes in the next two hours")
        self._walk("program", dict(self._base(guide[0]), kind="program",
                                   channel_id=guide[0].get("ChannelId"),
                                   _seed=guide[0]))

    def test_every_page_kind_was_reached_or_is_excused(self):
        """The catch-all, so a route added later is not silently unwalked.

        `tests/test_mpvtk_headless.py` uses the same shape for the headless
        door list, and for the same reason: an enumeration that has to be
        updated by hand rots, so make forgetting it a failure.
        """
        walked = {
            "home", "grid", "music", "series", "season", "detail", "album",
            "artist", "music_genre", "person", "search", "favorites",
            "genres", "playlist", "playlist_edit", "queue", "livetv",
            "channel", "program",
        }
        # Reached by a Studio or Genre tile rather than by a library, and
        # covered by `list` below; kept out of the walk because building one
        # needs an ItemsByName id that this library may not have.
        excused = {"byname", "list"}
        unwalked = sorted(set(self.PAGES) - walked - excused)
        self.assertEqual(
            unwalked, [],
            "these page kinds are never opened by the route walk, so a "
            "failure on real data would go unnoticed: %s" % unwalked)


if __name__ == "__main__":
    unittest.main()
