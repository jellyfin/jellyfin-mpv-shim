# Target architecture

Where the code should end up, and why. This is a destination, not a schedule —
`REFACTORING_METHOD.md` is how to get there without breaking things.

> **Baseline branch:** `local-ui-mpvtk`, not `master`. The mpvtk UI has not
> been merged to `master` yet — `master` is what live packaging installs, and
> the merge is gated on this refactor plus a hand code-review. Everything
> below is written against `local-ui-mpvtk` as the stable branch. See
> `REFACTORING_METHOD.md` §4.

Nothing here is urgent. The current design works, is unusually well
documented, and has 1785 unit tests plus a two-backend integration matrix
behind it. The case for change is that two structures have grown past the
point where they can be reasoned about locally, and both are now the main
cost of adding anything:

| | size | shape |
|---|---|---|
| `MpvtkBrowser` | 360 methods, 73 `__init__` attributes, 418 distinct `self.*` names across 9 files | one class, 8 mixins |
| `PlayerManager` | 4030 lines, 4 locks, 26 `_handle_mpv_disconnect` call sites | one class |

Both are god objects. The mixin split was the right first move — it made the
shared state *visible* — but `app.py`'s own docstring is candid that it is "a
partition, not a layering". This document is about the layering.

---

## 1. The browser: pages as separate concerns

### 1.1 What is actually coupled today

A route's loader/renderer pair reaches for eleven things on `self`:

```python
def _load_grid(self, route, ep):
    srv = route.get("server") or self.server      # session
    items, total = self.source.get_library_items(...)   # data
    self._route_async(route, work, done, ep)      # async + epoch
def _render_grid(self, route, size):
    ...self._grid(...)                            # tile components
    ...self.navigate({...})                       # navigation
    ...self.strips / self.thumbs                  # render resources
    ...self.controller.play(...)                  # player actions
    ...self.set_status(...)                       # chrome
```

Only the first four lines are *this page's* business. The rest are services
that every page needs. Because they arrive via `self`, a page cannot be
constructed, tested, or reasoned about without the whole browser.

The result shows up in the coverage numbers. Pages are well covered
(`views.py` 88%, `dialogs.py` 95%, `queue_edit.py` 92%) because tests drive
them through the composed object — but `ui.py`, the boundary between the
browser and everything else, is at **41.6%**, and its `_PlayerController` is
almost entirely untested. The seam nobody can construct is the seam nobody
tests.

### 1.2 The target shape

Three layers, with dependencies pointing one way only:

```
   shell        BrowserShell        nav stack, chrome, dialog layer,
     |                              now-playing bar, HUD switch, build()
     v
   pages        GridPage            one class per route kind.
                DetailPage          owns its own state; no self.<browser>
                SearchPage
                SettingsPage ...
     |
     v
 components     tiles.poster_row()  pure functions: data in, widget tree out
                strips.StripStore   no browser reference anywhere
                dialogs.confirm()
     |
     v
 services       LibrarySource       already independent
                PlayerGateway       (the ui.py boundary, made explicit)
                AsyncRunner         epoch + pool
                Navigator           route stack + headless policy
```

**A page becomes a class, not two methods on a shared object:**

```python
class GridPage(Page):
    kind = "grid"

    def __init__(self, ctx, route):
        self.ctx = ctx            # the services bundle, below
        self.route = route        # this page's own state, not a shared dict
        self.items = None
        self.total = 0
        self.error = None

    def load(self):
        """Called once on navigation. Runs off the loop thread via ctx.run."""

    def render(self, size):
        """Loop thread. Returns a widget tree for the CONTENT AREA only —
        the shell owns chrome, dialogs and the now-playing bar."""
```

`ctx` is a small frozen bundle, passed in rather than inherited:

```python
@dataclass(frozen=True)
class PageContext:
    source:     LibrarySource      # data
    nav:        Navigator          # navigate / go_back / headless policy
    run:        AsyncRunner        # run_async, epoch-guarded
    art:        ArtResources       # strips + thumbs
    player:     PlayerGateway      # play / queue / transport
    status:     StatusSink         # set_status, the toast
```

Six names instead of 418. The whole point is that `PageContext` is small
enough to fake, so a page test constructs the page and nothing else.

### 1.3 What this buys, concretely

- **A page is testable in isolation.** Today `tests/test_shell_*.py`
  is 8438 lines and 658 tests against the composed object, because that is the
  only thing that can be constructed. It becomes one test file per page.
- **The route dict stops being a shared mutable bag.** `route["_items"]`,
  `route["_loading"]`, `route["_error"]`, `route["_filtervals"]` are page
  state stored in a dict that navigation, `go_back`, `after_playlist_deleted`
  and every mixin can reach into. As page fields they have one owner.
- **`_routes()` MRO merging goes away.** A registry (`PAGES = {kind: cls}`)
  replaces the class-dict walk, and the "no name may be defined by two mixins"
  rule that `test_mpvtk_browser_mixins.py` enforces stops being necessary,
  because there is no shared namespace to collide in.
- **Headless enforcement gets a real boundary.** Today `navigate()` is the
  choke point and `_default_route()`'s docstring records that assigning
  `nav_stack` directly already bypassed it once. `Navigator` owns the stack
  privately; there is no attribute to assign.

### 1.4 Components become imports

`tiles.py` is 578 lines of tile/row/grid construction currently expressed as
37 methods on the browser. Almost none of it needs `self`:

```python
# now
def _tile_row(self, items, geom=None, async_=True): ...

# target — jellyfin_mpv_shim/mpvtk_browser/components/tiles.py
def poster_row(items, art, on_open, geom=POSTER_GEOM, async_=True):
    """Data in, widget tree out. `art` supplies strips/thumbs; `on_open` is
    what a click calls. No browser, no route, no self."""
```

The tell for what is genuinely a component: it needs `art` and callbacks, but
never `nav`, `source`, or `route`. By that test, most of `tiles.py`, all of
`strips.py`, the dialog builders in `dialogs.py` and the whole of `hud.py`'s
node construction are components today in everything but their signature.

### 1.5 What deliberately stays shared

Not everything wants decomposing, and pretending otherwise is how a refactor
turns into a rewrite:

- **`build()` stays one function on the shell.** The scene is a single tree
  pushed to one renderer; the shell composing chrome + page + dialogs + bar is
  the correct amount of centralisation.
- **The epoch stays global.** It means "navigation has moved on", which is a
  property of the app, not of a page. `AsyncRunner` owns it; pages never see
  the integer. (This also fixes the hazard `app.py` warns about — "caching an
  `ep` and passing it across a module boundary reads fine and is subtly
  wrong" — by removing the opportunity.)
- **One thread contract, unchanged.** `load()` off-thread, `render()` on the
  loop thread, writers end with `invalidate()`. Decomposition must not become
  an excuse to introduce a second concurrency model.
- **The mpvtk toolkit is already right.** `mpvtk/` has no dependency on the
  browser, is 90-99% covered on its core (`layout.py` 96%, `widgets.py` 99%),
  and should be left alone.

---

## 2. `player.py`: separating concerns

4030 lines, but the concerns inside are cleanly separable — they are already
grouped in the file, just not in the type system.

| concern | roughly | what it owns |
|---|---|---|
| **mpv session** | init, teardown, terminate, re-create, disconnect, `_mpv_errors` | the handle and its lifecycle |
| **start pipeline** | PlaybackInfo → load → gate → failure/retry | `_loading`, `_load_*`, `_start_in_progress` |
| **transport** | play/pause/seek/streams/speed/aspect | mostly stateless commands |
| **queue** | advance, repeat, playlist ops | `_play_epoch`, the queue |
| **window state** | force-window, browse background, geometry, OSC, fullscreen | `_geometry_armed`, `_showing_browse_bg` |
| **reporting** | timeline, session open/close, offline progress | `_reporter`, `_session_ready` |

The single most valuable extraction is the **mpv session**, because it is the
one whose absence causes the rest of the sprawl. Today, twenty-six call sites
independently do:

```python
try:
    self._player.<something>
except _mpv_errors:
    self._handle_mpv_disconnect()
```

That is one policy written twenty-six times, and it is exactly where the
`_join_render_loop` self-join bug lived: `_handle_mpv_disconnect` fires
teardown callbacks synchronously from whichever thread happened to touch a
dead handle. An `MpvSession` that owns the handle, applies the
try/except/disconnect policy in one place, and dispatches its lifecycle
callbacks on a *known* thread would have made that bug unrepresentable.

```python
class MpvSession:
    """Owns one mpv handle and its lifecycle. Every touch of the handle goes
    through here, so 'the handle died' is handled once."""
    def command(self, *args): ...      # raises SessionGone, never _mpv_errors
    def get(self, prop, default=None): ...
    def set(self, prop, value): ...
    def on_gone / on_terminated / on_recreated   # fired on the action thread
```

`PlayerManager` keeps `_lock`, the state machine and the public API. It gets
smaller by delegation, not by being split into pieces that all still need each
other.

### 2.1 The `run_action` ambiguity

`run_action` takes `_lock` non-blockingly and runs the action *inline on the
caller's thread* when it is free, deferring to the action thread when it is
not. Every `@synchronous("_lock")` method therefore has two possible execution
contexts and the method cannot tell which it got.

That is the mechanism behind the self-join bug, and it will keep producing
bugs of that shape. The target is one context: **UI actions always go to the
action thread**, and the UI shows optimistic state rather than waiting. That
costs a frame of latency on transport controls and removes a whole class of
reentrancy hazard. Worth doing, but *after* the session extraction, because
the session's callback-dispatch guarantee is what makes it safe.

---

## 3. Sequencing

Strictly ordered by risk, lowest first. Each step is independently shippable
and independently revertible.

| # | step | risk | prerequisite |
|---|---|---|---|
| 1 | Extract `components/` (pure functions out of `tiles.py`, dialog builders) | very low | none — they barely use `self` |
| 2 | Extract `AsyncRunner` (epoch + lock + pool + `run_async`) | low | none |
| 3 | Extract `Navigator` (route stack + headless policy) | low | `tests/test_mpvtk_headless.py` already pins the policy |
| 4 | ~~**Cover `ui.py`'s `PlayerController`**~~ **done** | — | was *blocking 5*; 41.6% → 67.2% |
| 5 | Formalise `PlayerGateway` from `_PlayerController` | medium | 4 |
| 6a | `Page` + `PageContext` + registry, first route converted | medium | 1–3, 5 |
| 6b | **Extract `TileRenderer` + `ScrollState`** (see §3.1) | medium | 6a |
| 6c | Convert the remaining route kinds, one at a time | medium | 6b |
| 6c-prep | `ItemActions`, `Paginator`, `components/{controls,detail}` | medium | 6b |
| 6d | The five loaderless kinds — see §3.2 | **open question** | 6c |
| 7 | Extract `MpvSession` from `player.py` | medium-high | integration matrix green on both backends |
| 8 | Make `run_action` single-context | high | 7 |

Steps 1–3 are mechanical and could be done in an afternoon each. Step 4 is
test-writing, not refactoring, and is the one that must not be skipped: it is
the least-covered code in the branch and everything after it depends on that
boundary holding still.

> **Revised while doing step 2.** This table originally put `_route_async`
> and `_page_more` in `AsyncRunner`. They do not belong there: both read
> `self.route`, write `route["_error"]` / `route["_loading"]`, and
> `_route_async` calls `_offline_fallback`. They are *route* helpers that
> happen to use the runner, so they stay on the shell now and move to the
> `Page` base class in step 6. `AsyncRunner` is exactly epoch + lock + pool +
> `run_async`.
>
> The tell is the same one §1.4 uses for components: a thing that needs
> `route` is not part of the async mechanism, however much it looks like it
> from the call site.

> **Revised again while doing step 6, and this time the test said so.**
> Step 1 extracted the *pure* helpers — the seven functions that used `self`
> zero times. What it left behind is the much larger set that needs `art`
> (strips, thumbs, the poster cache, tile geometry) but not `route` or `nav`:
> `_tile_row`, `_grid_of`, `_track_list`, `_image_map`, `_art_cell`. Those
> are components by §1.4's test; they just aren't *pure*.
>
> That matters because pages cannot be converted without them. Measuring the
> five remaining `ViewsMixin` renderers against `PageContext` gives **50 new
> `ctx.shell` uses against a budget of 9** — the escape hatch would become
> the primary interface and `Page` would be the mixins with an extra hop.
> `tests/test_page_contract.py`'s budget is what surfaced this, before any
> of it was written.
>
> So 6b is inserted: turn `TilesMixin` into a `TileRenderer` that holds `art`
> and a scroll-offset callback, and make it `PageContext.art`. Pages then say
> `ctx.art.tile_row(...)` instead of `ctx.shell._tile_row(...)`, and the
> per-page shell cost collapses. Its own dependencies are almost all
> *sibling helpers inside `tiles.py`* rather than shell state, which is what
> makes the extraction tractable.

### 3.1 What else is missing, measured

"Is `TileRenderer` the only thing in the way?" is answerable rather than a
judgement call. Taking every unconverted route's loader and renderer, and
counting the **direct** `self.X` each touches (not the transitive closure —
a page calls `ctx.nav.navigate(...)` and stops, it does not inherit
`navigate`'s own needs), the demand splits cleanly:

* **29 names are needed by more than one route.** Those are shared
  infrastructure and want a home.
* **63 are needed by exactly one route.** Those are that page's own state —
  `_login_error`, `_do_login`, `_pe_sel`, `_music_header_text` — and become
  page fields when it converts. They are not missing modules, and treating
  them as such would invent seven single-caller services.

The 29 group into six homes, ordered by how much they unblock:

| home | names | route-uses | status |
|---|---|---|---|
| `components/chrome.py` | `_busy`, `CONTENT_PAD`, `_paragraph`, `_body_w` | 28 | **done** |
| `TileRenderer` | `_grid_of`, `_track_list`, `_tile_row`, `GRID_GAP`, `_paged_grid`, `_banner_box`, `_backdrop_node`, `_art_cell`, `_square_geom` | 32 | 6b part 2 |
| **`ScrollState`** | `_on_scroll`, `_on_grid_scroll`, `_header_offset`, `_paginated` | 17 | **missing** |
| `components/controls.py` | `_sort_bar`, `_grid_filter_bar`, `_toggle_collections`, `_action_btn` | 8 | small |
| `components/detail.py` | `_people_row`, `_meta_line` | 4 | small |
| per-family page bases | `_music_action_bar`, `_music_header_text`; `_pe_sel`, `_pe_set_sel`, `_select_click` | 11 | shared by *one family* of routes each — a `MusicPage` / `SelectionPage` base, not a global service |

`_play_list` (3 routes) needs no new home: it wraps `controller.play_list`,
so a page calls `ctx.player.play_list(...)` through the gateway that already
exists.

**`ScrollState` is the one genuinely missing module.** It owns
`_scroll_off`, `_scroll_rendered` and `_live_offsets` — the virtualization
bookkeeping every list-shaped page needs and no page should own privately,
because the renderer is the authority and the shell reads it once per frame
(`build()` → `scroll_offsets()`). Extract it alongside `TileRenderer`; the
tile grid is its largest consumer, so doing them together avoids threading a
callback between two half-built objects.

---

### 3.2 Where 6c stopped, and why the line falls there

**14 of 19 route kinds are `Page`s. The 5 that are not are exactly the 5 that
never had a loader.**

That is not a coincidence and it is not where effort ran out. `ROUTES` maps a
kind to `(loader, renderer)`, and every converted kind had both:

| converted (14) | `(loader, renderer)` |
|---|---|
| home, search, detail, series, season, grid, person, music, album, artist, music_genre, playlist, queue, playlist_edit | both |

| not converted (5) | `(loader, renderer)` |
|---|---|
| settings, login, locked, connecting, cast | `(None, renderer)` |

A `Page` is *fetch, then draw*: `load()` gets what the screen needs, `render()`
turns it into a widget tree, and `PageContext` is the set of things a screen
needs to do that. The five holdouts do not fetch. They draw **application
state the shell already owns**, which is why they never needed a loader:

- **`cast`** renders one baked bitmap produced by a compositor that runs on
  the pool and is driven by websocket events (`display_cast_item`). The
  renderer is ~15 lines reading `_cast_entry` / `_cast_size`. The state, the
  compositor and the entry points are all shell.
- **`login` / `locked` / `connecting`** are the session state machine. Their
  handlers do not load data, they *replace the data source and reset the
  navigation stack* — `set_source`, `set_offline`, `show_login`,
  `show_connecting`, `connect_failed`. That is the shell's identity
  management; a page that could swap the source is not a page.
- **`settings`** is the closest call. Its five tab renderers are genuine page
  work, but they sit directly on config writes, source swaps (work-offline,
  remove-server), user management, download management and two pollers.

Converting them is possible, but only by extracting three more services of
roughly `ItemActions`' size — a session/auth service, the cast compositor, and
a settings-operations service — because the budget (rightly) refuses to let a
page reach the shell for any of it.

**The open question is whether that is worth doing, or whether these five are
correctly shell-owned.** There is a real argument for the latter: they are app
*states*, not library screens, and the `ROUTES` fallback that serves them is
three lines in `_load_route` / `_render_route`. Forcing them through an
abstraction built for "fetch, then draw" may cost more clarity than it buys.

Either way the escape hatch stayed at **zero** throughout 6c, so nothing here
is a debt that is quietly growing; it is a boundary with a reason.

## 4. Prose that belongs here, not at the call site

Several comments in the code have grown into post-mortems. They are valuable
and should be kept — just not inline, where they bury the code they explain.
Move the body here (or to `docs/development.md`) and leave a one-line pointer:

| location | what it is | ~lines |
|---|---|---|
| `app.py` `run_async` | why `on_error` is not epoch-gated, why `always` exists | 30 |
| `app.py` `_load_route` | rebuttal of a review's race claim, and the test that settled it | 18 |
| `strips.py` `_store` | libmpv address recycling and why `v` exists | 14 |
| `app.py` `_start_daemon` | the `restartable` gap that froze the log tail | 20 |
| `player.py` `_teardown_player` | stale-task drain across mpv re-create | 16 |
| `thumbnails.py` `__init__` | the urllib3 pool-block TLS diagnosis | 12 |

Suggested home: `docs/development.md` gains a "Design decisions and their
history" section; each entry keeps its full text under a heading the code can
cite as `# see docs/development.md#strip-address-recycling`.
