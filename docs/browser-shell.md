# The browser shell

`mpvtk_browser/` renders the library **inside the player's mpv window**, in the
main process, attached to `playerManager`'s mpv. There is no second window, no
browser subprocess, and nothing in the package imports tkinter
(`tests/test_no_tkinter.py` enforces that).

This file is the reference for how the shell works. `app.py` states each rule at
the line that depends on it; the reasoning, the alternatives that were tried, and
the bugs that established the rules are here.

## 1. Shape

`MpvtkBrowser` owns the route stack, async data loading, and the `build(size)`
that turns the current route into an mpvtk widget tree. It attaches its UI via
`mpvtk.MpvtkApp.attach` — see `jellyfin_mpv_shim/mpvtk/GUIDE.md` for the toolkit itself.

Around the core sit two things, and the split is a **migration in progress**
(`docs/archive/ARCHITECTURE_TARGET.md` §3.2), not a design.

**Pages** (`pages/`) own a route each — a class with `load` and `build` and its
own state, registered in `pages/PAGES`. This is where a route should go. Home,
grid, detail, series, season, search, playlists, the queue editor, music browsing
and Live TV are all Pages.

**Mixins** are what has not been converted, plus the app-wide surfaces that are
not routes at all:

```
MpvtkBrowser(DialogsMixin, LiveTvDialogsMixin, AuthMixin, SettingsMixin,
             MusicMixin, ViewsMixin, TilesMixin, CastMixin)

dialogs.py         modal shell, add-to picker, download + SyncPlay dialogs
livetv_dialogs.py  the guide/timer dialogs
auth.py            login / Quick Connect, lock screen, user switching
settings/          the Settings routes and the downloads panel (a package)
music.py           the now-playing bar and music playback glue
tiles.py           tile art, rows and grids, the tile context menu
cast.py            the cast screen route
views.py           forwarders left behind by the Page conversion; it shrinks
                   to nothing as its callers move
```

The mixins are a **partition, not a layering**: they all operate on the same
`self`, so the split makes the shared state visible rather than reducing it. No
name may be defined by two of them — MRO would silently pick a winner — and
`tests/test_mpvtk_browser_mixins.py` enforces that.

**Adding a view means adding a Page**: subclass `pages.base.Page`, give it a
`kind`, register it in `pages/PAGES`. A kind absent from that registry falls back
to the mixins' merged `ROUTES` tables (`kind: (loader, renderer)`), which is what
lets the conversion proceed one route at a time. `tests/test_page_contract.py`
fails a kind claimed by both, because it would resolve by whichever the shell
consulted first.

Kinds are declared in each mixin's `ROUTES` table alongside their renderer, so
adding a view is one edit in one place. This used to be a 215-line `elif` chain
in the dispatcher and a dict a thousand lines away.

### Pages and their context

A `Page` (`jellyfin_mpv_shim/mpvtk_browser/pages/base.py`) owns one screen.
`load(epoch)` runs on navigation, on the loop thread, and does its actual work
through `ctx.run`; `render(size)` runs on the loop thread and returns a widget
tree for the **content area only** — the shell owns chrome, dialogs and the
now-playing bar. The `epoch` is read by the shell on the loop thread and handed
down, because a page that read it later would be racing the navigation it
guards.

`close()` is called once, when the shell renders a *different* page — not on
navigation, because navigation happens on threads the render loop does not own
(a websocket, mpv's event thread) and this may touch the player. Almost no page
needs it. It exists for the ones that take something the window can only hold
one of: the comic reader hands mpv a picture, which nothing else would take
down, and extracts pages to files, which nothing else would delete. It may be
followed by another `load()` — going back returns to a route whose dict is still
here — so it must leave the page usable rather than spent.

`PageContext` is everything a page is allowed to depend on: `source`, `server`,
`nav`, `run`, `art`, `player`, `actions`, `dialogs`, `status`, `invalidate` —
ten names against the 413 distinct `self.*` the mixins can reach. Small enough
to fake, which is the point: a page test constructs the page and nothing else.
It is **frozen**, because a page that rewired its own dependencies would be
invisible to everything that reasons about navigation.

Three of those names are not the obvious ones:

- `source` is a `LibrarySource` **or** an `OfflineLibrarySource`, and a page
  must not care which. That is what makes offline browsing work at all.
- `actions` is separate from `player` because these are orchestrated actions
  (optimistic write, rollback, dialog, toast), not the raw capability the
  gateway exposes.
- `dialogs` is a layer rather than a page convenience: a dialog renders in the
  shell, *above* whatever page is showing, and outlives navigation. A page asks
  for one; it never draws one.

**`ctx.shell` is an escape hatch, and the difference between that and a loophole
is that it is counted.** It exposes the browser for helpers that are still
methods on the shell — the tile/row/grid builders, the chrome's busy and error
nodes. Those are component-shaped but still close over `self.strips` /
`self._posters`, so extracting them is its own job.
`tests/test_page_contract.py` pins the number of `ctx.shell` references and
fails if it grows; it can only go down.

**The page budget is zero.** Every converted page takes what it needs from its
context. One use remains, in `pages/base.py` itself — `route_async` — and that
one is not transitional: recording a load failure has to decide whether this
route is *still the screen* before dropping the user to the offline home, and
only the shell knows that. It is pinned by `BASE_SHELL_USES` rather than
budgeted, so the framework's own hatch cannot quietly become the place new
coupling goes.

Converting a route is mechanical:

1. subclass `Page`, set `kind`;
2. move the loader body into `load()` and the renderer body into `render(size)`,
   replacing `self.X` with `self.ctx.X` or `self.ctx.shell.X`;
3. register it in `pages/__init__.py`;
4. delete the two methods and the `ROUTES` entry.

Unconverted kinds keep working — the shell falls back to its `ROUTES` table for
anything the registry does not claim — so this proceeds one page at a time with
the app shippable throughout.

### The gateway

`PlayerGateway` (`jellyfin_mpv_shim/mpvtk_browser/gateway/`) is the browser's
one way to reach the rest of the app: playback and transport, the window handoff
between browse and video, servers and users, the offline catalog, SyncPlay.
**Nothing else under `mpvtk_browser` imports `playerManager`, `clientManager`,
`userManager` or `syncManager`** — `tests/test_source_invariants.py` enforces
that. It was already the boundary in practice (61 of the browser's 68
cross-package imports lived in one private class); making it a fact about the
module graph rather than a convention is what lets the page objects be
constructed, and tested, without dragging `player.py` in.

**It is a package because the one class had grown to 102 methods and 1,154
lines**, and it was never one responsibility — its own section banners named ten
domains, and one of them ("tile actions") had silently accumulated the whole
server-management surface underneath it. The facade composes one mixin per
domain, so every `gateway.X()` call is unchanged and each domain is a file you
can read in one sitting.

**Composition by inheritance, deliberately.** A nested-namespace gateway
(`gw.users.add`) would have been a wider change at every call site for no gain
here — these are a flat vocabulary of operations, not a tree. The cost of
flattening is that two mixins could define the same name and one would silently
win; `tests/test_gateway_mixins.py` refuses that, the same way
`tests/test_mpvtk_browser_mixins.py` does for the browser. The gateway holds no
state of its own — every method reaches a singleton and returns — which is what
makes the flat composition safe to read: there is no initialisation order
between the mixins because there is nothing to initialise.

**Imports stay lazy.** Every method imports its collaborator inside the call
rather than at module scope. That keeps `mpvtk_browser` importable without
`player.py`, which selects an mpv backend at import time and wires
interdependent singletons, and it is also the seam that lets
`tests/test_player_controller.py` substitute a broken collaborator and sweep the
whole class. `gateway/deps.py` holds the single exception and says why.

**The failure contract: a gateway method does not raise, and the three that do
are named.** Almost every one catches `Exception` and returns a documented
fallback, because its callers are the render loop — where an escape kills the UI
— or a pool worker, where it kills the worker with nobody watching. The
exceptions: `add_user` and `rename_user` let the failure through, because
catching made the field clear and nothing happen and the caller is what shows
the message; and `_sync` catches only to log, then re-raises, because the
SyncPlay actions built on it need the caller to see the failure.
`tests/test_player_controller.py` pins all three categories. This is the
reference version of the rule, and it is also stated in
`gateway/__init__.py` on purpose: a caller reading one method needs it there.

**`gateway/base.py` holds `_act`,** the guarded "do something to the player"
primitive. Transport, HUD and queue all reach for it, so it is not a transport
concern — it is the gateway's own vocabulary. It goes through
`playerManager.run_action` rather than calling through, because these run on the
browser's loop thread and the player's lock is held for the whole of a playback
start; calling through would freeze the window until the load finished or timed
out.

**And it holds the cross-domain declarations, because the coupling has a
length.** Splitting the gateway made visible what one 1,154-line class had
hidden: three calls cross a domain boundary. They are legitimate — a queue "add
these and play" genuinely needs playback, and a user switch genuinely needs the
source rebuilt — so the answer is to *declare* them under `if TYPE_CHECKING:`
rather than baseline the finding or pretend the domains are independent:

```
QueueMixin   -> play_list       (PlaybackMixin)
UsersMixin   -> rebuild_source  (ServersMixin)
UsersMixin   -> offline_source  (ServersMixin)
ServersMixin -> offline_source  (its own; listed for symmetry)
```

Keeping them in one place means the number is visible, and it is four. If that
list grows, the split is drifting back toward one class and the next person can
see it happening — **which nothing currently checks, and which is the obvious
test to write.**

### Two app-wide surfaces

**Cast screen** (`cast.py`) — the Chromecast-like preview (idle "Ready to cast"
backdrop plus `DisplayContent` item preview) is a browser **route**, not a
separate UI. Backdrop, gradient and text are baked into one full-window bitmap
because mpv composites overlay bitmaps *above* all script ASS (`jellyfin_mpv_shim/mpvtk/GUIDE.md`
§6), so text drawn as a node would be hidden. It was `display_mirror.py`, which
attached its own `MpvtkApp` and ran its own loop — two owners of one window,
which is why `display_mirroring` used to fall back to the Tk browser.

**`headless`** (`conf.py`) — cast-target mode: the cast screen is the only page
and the library is unreachable from the machine. Enforced at the single choke
point `MpvtkBrowser.navigate()`, plus `enter_browse`, `on_nav_command`,
`display_item` and the now-playing bar's Queue button.
`tests/test_mpvtk_headless.py` enumerates every door and has a catch-all, so a
newly added route is refused by default. Not a security boundary — the tray still
reaches Settings, deliberately.

## 2. The three invariants

### The thread contract

Renderer event handlers and `build()` run on the **loop thread**. `on_playstate`,
`notify_update`, `set_download_status`, `display_item` and `on_downloads_changed`
are called from foreign threads, as are the pool workers behind `run_async`.
Everything they touch must be write-then-`invalidate()`, never a direct scene
change.

### Epoch discipline

`_epoch` and `_lock` live **only** in `app.py`. Dispatchers read `ep =
self._epoch` on the loop thread and hand it to `run_async`, which drops the
result if navigation has moved on since. Caching an `ep` and passing it across a
module boundary reads fine and is subtly wrong.

**The epoch is re-read in `_load_route` rather than threaded down** from the
`_bump_epoch()` its caller performed immediately above. The two statements are
not atomic, and that is deliberate — it is not the race it looks like.

Re-reading yields the *newest* epoch, so a loader can never capture one that is
already superseded. Threading the navigation's value down is what breaks it: an
interloping bump makes the captured epoch stale, `run_async` drops the `on_done`,
and because no `_error` is set the view spins forever with no retry. That was
tried; `TestNavigationSurvivesAConcurrentBump` in `tests/test_shell_*.py` is what
caught it.

The residue is benign: if a foreign thread bumps *and* navigates in between, the
load applies into a route dict that is no longer on screen. A wasted write, not a
wrong one. The `epoch` parameter therefore exists only for callers that have
their own — none today. Leave it None.

### `_lock` protects writers from each other, not from the reader

`build()` reads route data **unlocked**. That is deliberate and safe only because
every writer ends with `invalidate()`, so a torn read is a one-frame glitch that
the next build heals.

Do not "fix" it by locking `build()`.

### The `AsyncRunner` callback contract

`AsyncRunner` (`jellyfin_mpv_shim/mpvtk_browser/async_runner.py`) owns the
epoch, its lock and the worker pool. They are one mechanism and have one owner;
they used to be three attributes among seventy-three. Reading the epoch from
anywhere is fine and expected — *advancing* it is that module's job alone, which
`tests/test_source_invariants.py` enforces, because this was asserted in prose
for a long time with nothing checking.

`run(work, on_done, epoch, on_error=None, always=None)` has three arms. All
three have bitten, and `tests/test_async_runner.py` pins each.

**`on_done(result)`** — applied only if the epoch still matches. Runs under the
lock.

**`on_error(exc)`** — runs when `work()` raises, and is **deliberately not
epoch-gated**. A rollback undoes an optimistic edit in the route dict it
captured, or clears a paging guard; neither is a claim about what is currently
on screen. Gating it meant that navigating away before the failure landed
dropped the rollback, so the route kept a change the server had refused and
showed it again on the way back. That puts the burden on the handler: anything
in an `on_error` that touches the live screen must check for itself — §12 is the
rollback path that does.

**`always()`** — runs after every outcome: success, failure, *and a result
dropped because the epoch moved*. It runs even when the pool is already shut
down and the work is discarded outright, because that is exactly when a guard
would otherwise be left set on a route dict that outlives the pool.

**A guard that must not outlive the call goes in `always`, never in `on_done` or
`on_error`.** Past the epoch **both** of the other two are dropped, so a flag
cleared only in `on_done` stays set forever, and one cleared in both still leaks
on the stale-success path. This is the rule behind `_loading`, `_win_load` and
`_page_loading` (§13), and it is re-derived at every site that holds a guard, so
take it from here: a `_loading` left set means that route never pages again —
scroll to the bottom, click a tile, come back, and the list is silently capped
for the rest of the session.

Every callback is individually guarded, because they run on a pool worker where
an escaping exception kills the thread with no caller to see it. The pool is
four workers wide: enough to overlap a route load with the client mutations a
screen issues, small enough that a slow server cannot fan out into dozens of
sockets. Jobs that can take minutes must not run here at all — see
`MpvtkBrowser._run_long`.

## 3. The standing footgun: state that changes between draws

A screen is rebuilt from scratch on every repaint, so a widget tree is a
**snapshot**. Everything a handler closes over is the state as of the draw that
built it; everything drawn is the state as of the draw that drew it. Nothing
reconciles either afterwards.

Two failures follow, and **both have shipped**.

**A handler that captures.** `lambda: play(item, aid=aid)` where `aid` was
resolved in `render()` fires with whatever was selected when the page last drew.
*Read mutable state inside the handler, not in the builder* — that is what makes
a button correct however long it has sat there. This bit the detail page's
Play/Resume/chapter buttons after a track pick.

**A handler that writes without asking for a repaint.** The renderer flips a
Dropdown's own selection and a TextBox's own text optimistically, so those look
self-updating. **A `Checkbox` does not exist on that side** — it is Box-plus-tick,
coloured here from `checked`, and only a redraw can move it. Anything else drawn
from a value (a row that appears once a name is typed) is the same. This bit the
Add to Playlist dialog's Private box, which flipped invisibly.

### A scene assertion is not a repaint assertion

`build_scene` renders when asked, so it draws a correct tree whether or not the
app would ever have redrawn. **Whether a repaint happened is the one thing it
cannot answer.** A handler that changes state owes a test that `invalidate` (or
`_show_dialog` / `_reload_tab`) was called, and a stray `build_scene(b)` between a
click and its assertion silently refreshes every closure on screen.

Both bugs above passed their scene-based tests throughout — one of them
*because* the test rebuilt in between.
`tests/test_shell_playlists.py:test_toggling_private_asks_for_a_repaint` is the
shape to copy.

The *capturing* half is checkable from the source, so it is:
`tools/audit_stale_captures.py` reports a local read from mutable state (one call
deep, which is where `_effective_tracks` hid) that a handler then closes over, and
`tests/test_no_stale_captures.py` runs it. A finding is not automatically a bug —
read the state inside the handler, or add it to that file's `ACCEPTED` with the
reason it cannot go stale, which is what the six route-identity captures there
are. It is a lead generator: it follows one frame, and says nothing about the
writing half.

## 4. Refreshing a screen under the user

Live TV and Home are the only screens a **third party** changes while you are
looking at them: a programme ends, a recording starts, another client sets a
timer, someone finishes an episode on a phone or drops a film from Continue
Watching in a browser (#560). A stale Continue Watching row is not cosmetic — it
offers to resume something already watched, and pressing it starts it over.

Both use the same pattern, and every part of it is load-bearing.

**A load, not a reload** (`nav.load` / `_load_route`, never `_bump_epoch`).
Nothing in flight is cancelled, and because every such loader writes its result
*in place* rather than clearing first, the screen keeps the data it has until the
new data lands. A refresh nobody asked for must not blink a spinner over what
they are reading.

**Scroll survives** for two reasons together: the container id does not change,
and the renderer applies a parked offset only to a container it has no offset for
yet (`off0` in renderer.lua, pinned by `tests/lua/test_renderer.lua` — "off0
yanked the user back on a later frame").

**Deferred, never forced, while the user is mid-interaction.** An open context
menu or dialog means they are acting on what is on screen, and `route["_loading"]`
means a page-in is in flight whose merge was computed against the list length at
submit time — replacing the list under it would duplicate or drop a page.
Skipping costs at most one poll interval.

**A refresh must say so while it runs.** Without a guard of its own, a scroll
landing *after* the refresh was submitted paged in against a list the refresh was
about to replace wholesale. Whichever order the two answers arrived in the list
was wrong: a page fetched twice and another never, or — likelier, since the
refresh is the larger query — 100 rows dropping out from under a scroll already
past them. `_route_async` clears the flag however the load ends. `_load_channels`
re-reads `max(CHANNEL_PAGE, len(_data))` to prevent the same thing.

**Debounce, except for the caller that is not a burst.** `_start_daemon` keeps one
thread per slot, so a burst of websocket events schedules exactly one re-read: the
first arrival starts the wait and the rest land while the slot is taken. Coming
back from playback (`enter_browse`) skips the debounce — that is the *local* half
of the same bug, where watching something in the shim itself left the rows stale
because Home only re-read on a Back press.

Live TV additionally polls, because **the server has no "recording started"
event** — which is why polling is not redundant with the websocket.

## 5. The route dict is the page cache

Loaded rows hang off the route as `_data`, with the Page object. Pushing a fresh
dict therefore means `chrome.busy()` until a whole fetch lands.

**Home is reused rather than re-pushed** for exactly that reason: it is the one
screen the user almost always already has, reached by the one button that should
feel free. Reuse is safe rather than stale because Home re-reads itself in place —
`HomePage.load` publishes over what is there (its partial first batch is withheld
once `_data` exists, precisely so a refresh does not take the Latest rows away and
put them back), and `_publish` drops the write entirely when nothing changed. The
scroll position comes back with it, parked on the route being restored.

**Home-on-Home still goes the whole way round.** The epoch and `_screen_seq` both
move, so the caches shed and the rows re-read. That is deliberate: it is the one
gesture in the app that means "reload this", and `_shed_caches_on_screen_change`
is what a reload should do. Do not "fix" it by skipping the bump when the route
is unchanged.

The server is part of the match, not an afterthought: switching servers pushes its
own home (`_switch_server`) and must not land on the previous one's rows. A stack
with no usable home falls through to a fresh route, which is also what keeps
headless refusing — the Navigator sees the same "home" it always did.

## 6. Shedding decoded artwork

Decoded images are the most expensive thing the app holds per picture — a 4K
backdrop is **33 MB decoded against ~400 KB on the wire** — and they exist for one
job: compositing tile strips. Once a row has been composited they are not needed
again, and once the screen is behind you neither is the row. So the moment the
screen changes is the moment nearly all of that memory has nothing left to do;
holding it to the 96 MiB ceiling means holding it until something else needs the
room.

**Observed on the loop thread, not done in the navigation itself.** `navigate()`
is reachable from mpv's event thread and from the websocket (a remote's GoHome, a
phone's DisplayContent), and this cache has no lock — every other access is on the
loop thread, where `build()` runs. A counter bumped in navigation and read during
build turns a cross-thread call into a loop-thread observation, and costs nothing
on the frames where nothing happened, which is nearly all of them.

**Keyed on `_screen_seq`, not on the async epoch.** That was the first thing tried
and it is a different question. `_bump_epoch` means "cancel what is in flight", and
four things do it without leaving the screen at all: a sort or filter change, a
collections toggle, a retry after a failure, and a server switch that keeps its
place. Shedding on those cut the cache for the page the user is still looking at,
so toggling a sort re-decoded the visible screenful for nothing.

The **composited rows** are a different trade and normally not worth shedding:
they are what makes going Back instant, and Back is the most common move there is.

## 7. Scroll state

`jellyfin_mpv_shim/mpvtk_browser/scroll_state.py` owns where each scroll
container is, and when that warrants a repaint. The **renderer** is the
authority on where a container is scrolled, and the shell reads its live
snapshot once per frame; a page holding its own copy would drift from the thing
actually drawing, which is why no page owns this privately.

### Five pieces of state, one per failure

**`_live`** — the renderer's own offsets, read synchronously at the top of every
`build()`. The only value that cannot be stale, because the renderer clamps it
to the *current* content. A failed read degrades to `_recorded` rather than
silently reusing last frame's numbers against this frame's content.

**`_recorded`** — the throttled `on_scroll` copy. A fallback for mpv < 0.36,
which has no `user-data` and so cannot answer the live query at all — and *only*
for that. It is a whole-snapshot substitute, never a per-id one: consulting it
for ids missing from a live snapshot resurrects offsets the renderer has
deliberately dropped.

**`_rendered`** — the offset each container was last *re-rendered* at, which is
the baseline the re-render threshold measures against. It must not be the
previous *event*: continuous sub-row scrolling arrives in many small steps, so
comparing adjacent events lets a slow scroll drift a whole window without ever
crossing the gap — and the virtualized rows fall out of the built window as
blank spacers until some larger coalesced jump finally trips it.

**`_pending`** — the offsets **this frame's scene is about to command**: the
route's parked offsets, which the pages pass as `Scroll(offset=…)`. For a
container the renderer has not heard of yet, this outranks its silence — the
scene is telling it where to go, so that is where it will be by the time
anything is drawn. Without it, a restore is indistinguishable from a container
that really is at the top.

**`_seeded`** — the ids the renderer has answered for **since the parked
snapshot was taken**. A restore is a one-shot, not a standing order, and this is
what makes it one: without it a parked offset was re-applied every time its
container left the scene and came back — a Live TV tab flip through the busy
screen, a reconnect — undoing whatever the user had done since.

"Since the parked snapshot was taken" is the whole of it, and the two ways a
container's offset can vanish are what force that wording. A yield to playback
empties the scene, but `park` runs on the way out, so the parked values *are*
where the user is and have to be re-armed. A tab flip through the busy screen
empties it too and nothing parks, so the parked values are from the last
navigation and re-applying them would undo the visit. Hence `park` clears
`_seeded` and a repaint does not.

**`_seeded`, and not a pop from `_pending`.** `_pending` is re-read from the
route on every frame, so a pop would last one build — and the frame that matters
is a later one. It is cleared by `reset()` (a route change, where a new screen
may legitimately restore the same container ids) and by `park()`.

### `offset()`: two containers the renderer has never met

Where the renderer has an answer it is the only authority; its copy is the one
clamped to the current content. What it does *not* answer for is a container
that has only just entered the scene, and there are two of those, which want
**opposite** things:

- **The container really is at the top.** Tick Paginated on a scrolled grid and
  untick it, or change a sort (which drops to the busy screen and takes the
  scroll container with it): the renderer built a fresh container at 0. Letting
  `_recorded` fill that gap re-armed an offset the container no longer has and
  windowed the returning grid around it — a screenful of blank spacers with no
  tiles in it.
- **The container is being *restored*.** Press Back onto a library scrolled to
  the end: the scene being built carries `off0` for exactly that, and the
  renderer applies it before it draws anything. Windowing that frame from its
  silence built the top of the list and then jumped to the bottom, so the screen
  came back empty and stayed empty until a scroll rebuilt it.

`_pending` is what tells them apart, and it is not a memory of where the
container *was*: it is where this frame's scene is about to *put* it. A
container the user has since scrolled has a live entry, which still wins —
including when that entry is 0.

### `pending()`: a parked offset nothing restores is a lie

`pending(scroll_id)` is what to pass as `Scroll(offset=…)`, and it exists for
the containers a page does not build by hand. The home screen's carousels are
one `tile_row` call each with ids generated per row, so the only code that can
restore them is the shared component that builds them.

That is not a convenience. `offset` answers for a container the renderer has not
met with the parked value *because the scene is about to command it* — so a
parked offset that nothing restores makes that answer a lie for exactly one
frame, and it is the frame a screen comes back on. The carousel drew at 0 with
its page buttons derived from wherever it had been left, and nothing invalidated
afterwards to correct them.

### `on_scroll`: distance, end stops, `edges_only`

`then(offset, maximum)` runs first and unconditionally — it is how infinite
scroll asks for the next page, and that must not be gated on the repaint
threshold.

A repaint otherwise follows when the container has moved `STEP` (120 px) from
the offset it was last rebuilt at: small enough that the refresh lands well
before the user reaches the edge of the built window.

**Crossing into, out of, or straight across an end stop always repaints**,
whatever the distance. The carousel page buttons derive their disabled state
from the offset, and the last few px of a drag to the end are usually well under
`STEP`, so the button that just became useless would otherwise stay lit until
something else happened to invalidate. The end stop is three states — start,
neither, end — rather than a boolean, because a carousel one page longer than
its viewport goes from one stop to the other in a single click, and a boolean
"is at an end" cannot tell those apart: the move that reverses *both* buttons
was the one move that repainted neither.

`edges_only` drops the distance rule and keeps just that one, for a container
whose *only* offset-dependent content is at the ends. The home carousels are the
case: nothing about them is virtualized, so a mid-row repaint would recomposite
a screenful of poster strips to change nothing.

### The live read is split out of `refresh()`

`refresh()` is the per-frame call: it takes `_live`, marks everything the
renderer answered for as seeded, and recomputes `_pending` from the route about
to be built. `park` needs the same renderer read *without* any of that, so the
read itself is a separate method.

`park` runs on whatever thread called the navigation — the websocket thread
delivering a DisplayContent, a remote sending GoHome — and calling `refresh`
there clears `_pending` out from under a `build()` in progress on the loop
thread. **A torn read of `_live` is the one-frame glitch the browser tolerates
by design (§2); a `_pending` emptied mid-build is not**, because `off0` is
applied to a container exactly once and the renderer has already seeded it at 0
by the time the next frame could correct it.

### Parking, and re-arming the restore

`park` stashes the current screen's offsets on its route dict so coming back
lands where it was left. It reads the renderer live rather than trusting the
last frame's snapshot: a scroll shorter than `STEP` never triggered a rebuild,
so `_live` can be that far behind at the moment of a click. Restoring is the
*page's* half — it passes `offset=` on the container it rebuilds — because only
the page knows which of its scrollers it wants restored. `reset()` immediately
after is what stops one view's offsets bleeding into the next under the same id;
the parked values travel with the route, which is what makes them survive it.

`park` clears `_seeded`, and that is what re-arms every restore: those offsets
are current as of now, so every container is owed one from them again, including
the ones already on screen whose ids `refresh` has been marking as seeded all
along. **This is what makes coming back from playback work.** That is not a
navigation — `enter_browse` rebuilds the same route, so `reset()` never runs —
and without the clear the returning grid was *windowed* at the top while `off0`
put the container at the bottom, a screenful of holes.
`tests/test_shell_paging.py:TestAReturningScrollContainerStartsAtTheTop` exists
to catch exactly that.

**It refuses to park while the browser is not on screen, and that is not an
optimisation.** A yielded scene holds no containers, so `scroll_offsets()` answers
`None` — indistinguishable from mpv being too old to ask — and `park` falls
through to its `_recorded` fallback, which only ever holds containers that
installed a watch. A page's own vertical scroll installs none.

So parking from a yielded state does not merely fail to record anything: it writes
that *partial* snapshot over the complete one `_yield` saved on the way into
playback, silently dropping the page position and keeping the rows. Reachable
because a remote's GoHome navigates before it stops the video (`on_nav_command`),
and on every mpv < 0.36, where the live read is never available at all.

`_park_on_leaving_browse` is the one caller that runs at the boundary, which is why
it is invoked *before* `_yield` clears the flag rather than after.

## 8. Pollers and one-at-a-time daemon threads

`_start_daemon` runs a body on a daemon thread, at most one per slot attribute.

**The check and the assignment have to be atomic.** Every caller used to write `if
self._x_thread is not None: return` and then assign, but they are reachable from
the loop thread *and* from foreign ones (`on_playstate`, `on_downloads_changed`),
so two callers could both see None and both start a thread. Doubling a poller is
only a wasted refresh today, which is exactly why it would have gone unnoticed.

The slot is cleared when the thread exits. The return value says whether *this*
call started it, so callers driven by a **user action** can say something rather
than appear to do nothing.

**`restartable=True`** closes a gap that bit the logs tail. A poller decides to
exit by noticing the route it was started for is no longer current, but it only
notices on its next tick — up to a full poll interval later. Leave the tab and come
straight back inside that window and the sequence is: the view starts a poller for
the new route, `_start_daemon` returns False because the old thread is still
registered, then the old thread wakes, sees a stale route, exits and clears the
slot. Nobody is left polling, and since only the render path starts one, the panel
is frozen until something else rebuilds it.

`restartable` makes the departing thread `invalidate()` once it has released the
slot. That re-runs the view, which starts a poller iff it still wants one — no
queued body to re-arm, so a request that has itself gone stale simply is not
honoured. Opt-in, because `_arm_toast_clear` releases its slot early by design and
would invalidate on a timer that is still live.

## 9. What a setting change re-derives live, and what it does not

**Theme** (`set_theme`) needs three things beyond re-applying the palette: the
renderer needs the new tokens pushed (it draws text fields, dropdowns, scrollbars
and tooltips itself); mpv's own `background-color` is what shows behind the
browser and is a property, not something the scene paints; and every composited
strip has the old theme's colours baked into its bitmap, so the strip store is
**retagged, not cleared** (`StripStore.tag`) and rows recomposite as they are next
drawn.

Tile **geometry** is deliberately not re-derived on a theme change.
`poster_scale` and `tile_landscape` feed sizes a live rebuild would have to
rediscover through every cached row, and the payoff is a cover size changing under
the pointer. Those stay restart-only; the colours, which are what a theme is mostly
made of, do not.

**Cover Size** (`apply_cover_size`) is the opposite case, and the distinction is
the point: a control *labelled* Cover Size exists to be seen happening, which is
why it sat behind a restart and nobody could tell what the values meant (#616).

Two things have to be cleared for the whole stack, not just the current route:

- parked scroll offsets — pixel positions into a list whose row pitch just
  changed, so keeping them lands the user somewhere they never scrolled to;
- every route's parked **grid shape**. `GridPage._grid_shape` computes the
  median-artwork geometry once per route and keeps the resolved `TileGeom` on the
  route dict — deliberately, so a grid does not change shape as you page through
  it — which means the library on screen went on drawing at the old cover size
  until it was reloaded. That is why this looked like it needed you to leave the
  page and come back.

**Logo legibility** bakes into the composited strip the same way a theme colour
does, so it retags rather than clears. See `docs/artwork-pipeline.md`.

## 10. Four deliberate divergences from jellyfin-web

The artwork audit closed the rest; these were argued and kept, so they read as
parity bugs and are not.

1. **Continue Listening stays square.** Web makes every media type 16:9 except
   Book; landscape does not make sense for album art. Marked as such in `home.py`
   so it does not look like a side effect of the `collection_type: "music"` tag.
2. **`auto_geom`'s `≥3` bucket folds into landscape.** No web row or grid defaults
   to banner, and everything landscape in web is `overflowBackdrop`, which *is*
   our landscape tile under another name. This covers only the *inferred* case; an
   explicit `imageType=Banner` from the view settings still gets a real ~5.4:1
   tile.
3. **No blurhash.** Thumbnails are cached aggressively and decoded on a worker
   pool; a blurhash canvas per tile is render cycles spent against mpv overlay
   churn for a placeholder on screen for a few frames.
4. **Track lists are tables, not cards**, where web uses list views in the same
   places.

## 11. Re-querying a grid: stale-while-revalidate

`GridPage._reload` re-runs the route's query **keeping what is on screen until the new
results land**.

It used to pop `_items`, and `render` answers a missing `_items` with `chrome.busy()`
for the whole page — title, filter bar, A–Z rail and all. So every filter tick, every
sort change and every letter press blanked the library and drew a spinner over it.
Behind the filter panel, which covers the middle of the window, all that was visible
was the page going dead.

Same rule as `refresh_live_tv` (§4), with two differences: this **does** bump the epoch
(the query has changed, so anything in flight is answering the wrong question), and the
old data is genuinely *wrong* rather than merely stale. It stays up anyway, because an
empty page for the length of a query says nothing true either, and says it much louder.

`_total` is deliberately **not** dropped. It is the header's item count — shell rather
than content — and zeroing it puts "0 items" over the spinner, which is a worse thing
to say than a count one second out of date. `_install` overwrites it.

## 12. View settings

### Which artwork is part of the *query*, not the paint

The server only sends the image tags a request names, so a grid fetched as Auto has no
Banner in its `ImageTags`, and switching to Banner can only fall back to the poster it
already has. Redrawing was the whole of the old implementation, which is why the
setting looked like it did nothing.

**The test is what the query would be, not which setting was picked.** Auto and Poster
ask for exactly the same artwork and differ only in the shape it is drawn at, so
switching between them is a repaint.

The re-ask happens **in place** rather than through `_reload`: the items are not stale,
only the tags on them.

### `nav.load`, not `nav.reload`, on the rollback path

The rollback reaches `_redraw_or_refetch` from `on_error`, which is deliberately **not**
epoch-gated (see `AsyncRunner`, §2), so a save that fails after the user has walked away
lands on the route they left. `reload` bumps the epoch, which would cancel the
in-flight load of whatever *is* on screen and strand it on a spinner with nothing left
to re-issue it. `load` re-runs this route's own loader without touching the epoch.

### The legacy `viewType` key is migrated one-way

`view_prefs.is_list` still honours it — a library put in list view before the setting
moved onto web's shared `imageType` has to keep coming up as a list — which means
nothing that writes only `imageType` can take such a library back *out* of list view.
The checkbox saved the grid value, the legacy key went on outvoting it, and it came
back ticked on the next frame: a control that read as dead.

`_clear_legacy_view` is called from the top of `_set_view`, ahead of **both** the no-op
check and the snapshot it takes of `_view`. Ahead of the check because the box unticks
by writing the imageType the library already had, so the write it rides on is very
often no write at all; ahead of the snapshot because it sets `_view` itself, and a
caller holding an older copy puts the legacy value straight back.

One-way deliberately: nothing writes `viewType` any more, so this is a migration rather
than a setting.

### Grid shape is computed once per route and parked

jellyfin-web's library grid asks for `CardShape.Auto` and lets the median
`PrimaryImageAspectRatio` decide (`ItemsView.tsx:87`). Movies come out as posters
because movie posters *are* 2:3, not because anything says "movies are posters". Ours
said it, for every collection type at once, which is why a Home Videos library — 16:9
camcorder clips with no poster art to crop — came out portrait.

A grid is paged, and a median taken per page would change the grid's shape as you
scroll through one library. A route is one folder, so the first page's median is the
folder's. Cleared wherever `_items` is: a filter or a sort is a different set of items
and deserves a fresh look.

An explicit choice beats the median outright, exactly as in web
(`ItemsView.tsx:75-88`) — the whole point of the setting is that the artwork got it
wrong for this library. The fallback when *nothing* carries a ratio is square, matching
`cardBuilder.js:102-104`: that case is precisely a grid of art-less items, and square
placeholders tile better than tall ones.

### Collections is a door, not a filter

Genres, Networks and **Collections** leave the library rather than filtering it.
Collections sets `route["_collections"]`, tears down `_items`, `_total`, the grid shape
and the paginator, and comes back with a different item type wearing a different tile
shape. The test is whether it composes: everything in `_filters` intersects, and
Collections cannot intersect with anything.

They lead the filter row rather than sitting among the filters, because a filter changes
what this grid shows and these navigate somewhere else. jellyfin-web reaches both
through library tabs, which this client does not have — so leading the bar is the
nearest thing to a tab strip.

## 13. Paging a result set

`Paginator` (`jellyfin_mpv_shim/mpvtk_browser/pagination.py`) pages a result set
too large to hold. Four routes use it — grid, person, music, music_genre — in
three modes that share the machinery.

**Windowed** (`window`) is what a library grid does: the list is `_total`
entries long from the first frame, mostly holes, and the pages covering what is
on screen are fetched as it comes into view. That is what makes the scrollbar
full-length and the drag stable — the bug in #617 was the scroller growing under
the thumb as pages arrived. `spread()` is the splice that keeps it: an item's
index **is** its position in the library, and everything downstream reads a hole
as "not here yet" and draws a blank tile of the right size. (A falsy total —
the source did not say, or said Random, where what is loaded is all there is —
makes it a plain splice that keeps whatever length the list had.)

**Infinite scroll** (`more`) appends the next chunk as the user nears the bottom
(`PAGE_SLOP`, 800 px). It is what the routes that are not windowed still use.
Three views used to carry a copy of it and each learned its invariants
separately, which is why they are spelled out on the method.

**Paginated** (`ensure` / `go` / `jump`) fills one screenful at a time with a
bar at the bottom instead of a scrollbar. It keeps the current page and its two
neighbours warm so Next/Previous land instantly, and prunes to that window so a
deep library does not accumulate every page it visited. The inline checkbox
flips a **global** setting, not a per-route one, and resets the page state so
turning it on lands on page 1.

`WINDOW_PAGE` is 100 and that is not arbitrary: it is also the repository's
default page limit, so a route's initial load fetches `[0, limit)`, page 0 is
complete, and the first render asks for nothing. A smaller limit would leave
page 0 holed and re-fetched immediately. `PAGE_MAX` (60) caps a fixed page for
the overlay budget, not for the layout, and `page_size` rounds its row count
**down** so a page never overflows its slot.

**The paging state lives on the route dict, not on the paginator, and
deliberately.** The route is what navigation keeps and throws away, so going back
to a library returns to the page you left, and leaving it frees the cache with no
bookkeeping at all. The class is the logic over that state. `content_h` is a
callback for the mirror-image reason: sizing a page means measuring the shell's
own chrome — the update banner, the download bar, the now-playing bar — and only
the shell knows which are up. It is the one thing here that is not
self-contained, and an explicit argument is the honest way to say so.

### `more()`'s invariants

- **Only page the route that is on screen.** A scroll event can arrive for a
  view being left.
- **`_loading` guards re-entry and must not survive a failure**, or the list
  never requests anything again for the rest of the session. It is cleared in
  `always`, not in `on_done`/`on_error` (§2).
- **An in-range page that comes back empty ends the list.** A random sort that
  reshuffles per request, or a filter the server applies differently than we do,
  otherwise gets re-asked on every scroll event forever.
- **Never page from an empty list** — that is `start_index=0`, i.e. the initial
  load, and the loader owns it.
- **Never page against a list that is being replaced.** Live TV re-reads itself
  behind the user's back (§4) and that refresh rewrites `_data` from index 0. A
  page-in submitted alongside it computes `start` from a length that is about to
  change, so the merge either duplicates a page or skips one — permanently,
  since `len >= total` then ends the list early. `_refreshing` is that refresh's
  own guard; the deferral is symmetric, and either direction alone leaves the
  race.

A failure toast is raised only if the route is still current, and the other two
modes narrow it further: nobody *asked* for a page a scroll triggered, and a
prefetch nobody can see stays silent. An edit the user pressed a button for is
the opposite case.

### `window()`: one attempt per page, per scroll

`window` is called from **render**, like `ensure`: which items are visible is a
question about geometry, and only the view can answer it. The range passed must
be the range the view actually composites, or the grid fetches one window and
draws another.

Render driving it is also why a failed page must not retry itself. A
re-request on failure would issue one per frame for as long as the server stayed
down — and the toast it raises invalidates, which is a frame. So an attempt is
remembered in `_win_tried` and not repeated; `rewindow()` clears that, and the
view calls it on a scroll. A window that failed is therefore retried when the
user **moves**, which is the cadence `more` had, and never when they hold still.
`rewindow` also runs wherever the result set is replaced, because page 3's items
are not page 3's items any more.

**`_win_load` is a separate set and is the in-flight one.** Clearing `_win_tried`
mid-scroll must not re-issue a request that has not come back yet. It is cleared
in `always`, so a page dropped for being stale releases it too.

### A page number means nothing without its page size

"Page 2" is items 24–35 at `ps=12` and 60–89 at `ps=30`. A job submitted before
a resize — or before the now-playing bar appeared, which is the same thing to
`page_size` — answers the question that *was* asked and lands on a cache the
answer no longer fits: the stale page draws 30 tiles into a 12-slot page and
hides the ones that belong there. So the fetch's `on_done` re-checks
`route["_page_size"]` and drops the result if it has moved.

**The epoch cannot cover this, because nothing navigated.** Epoch discipline
(§2) answers "has the user gone somewhere else"; this is the same route, the
same query, and a different window.

For a related reason the page cache is written `setdefault`-shaped: `reset` may
have replaced `_pages` and `_page_loading` outright between submit and land, and
a `KeyError` raised in a pool callback is one nobody can see. `reset` runs
wherever the underlying result set changes — sort, filter, collections toggle,
music tab — because page 3 of one ordering is nothing like page 3 of another.

## 14. The playback HUD

The HUD is the jellyfin-styled player UI (`osc_style: mpvtk`, the default),
drawn by the browser while it is yielded to video. `hud.py` builds the widget
tree; `hud_control.HudController` owns what that tree reads. The two halves are
split across two files for no reason other than history.

**Nothing here is browser chrome.** The HUD belongs to *video playback*: the
renderer owns its summon and auto-hide lifecycle and reports it through
`on_hud`, and the browser's only interest is that it must not push a HUD scene
at a renderer that is not showing one.

### State, and why each piece exists

**`shown`** — the renderer's summon flag, not ours. Echoed here so `build()`
knows whether to produce a HUD scene at all.
**`state`** — the latest video playstate snapshot (position, duration,
chapters …), pushed from the player's thread via `on_playstate`. `None` means no
video.
**`scrub` / `scrub_paused`** — a seek gesture in flight: the pending target in
seconds, and whether *we* paused playback to make the position inspectable. The
second flag is what stops a commit from resuming playback the user had paused
themselves.
**`menu` / `menu_anchor`** — the open settings-menu level ("root", "speed", …)
and the node it hangs off. One level at a time.
**`info`** — the playback-info panel is up.
**`tc_remaining`** — the clock shows remaining rather than total; a click
toggles it.

`reset()` drops all of that when a *fresh* renderer is attached: it has no HUD
state, so keeping ours would have `build()` pushing a HUD scene at an idle
renderer. `state` deliberately survives — it comes from the player, not the
renderer.

The scrub preview bubble is deliberately absent: the renderer draws it from the
trickplay tiles and mpv's chapter list, so no hover state reaches here at all
(#618).

### Lifecycle

Yielding to video keeps the renderer **attached but idle** rather than getting
fully out of the way, which is what the lua OSCs need instead; `available()` is
that test. `engage()` is `set_hud(True)` (the `mpvtk-hud` script message)
carrying everything the renderer owns: the keyboard policy — grab the arrows, or
take only the wake key so mpv's own seek keys keep working — the auto-hide delay
and mode, whether the glyphs carry their own shadow, whether the hidden HUD takes
the left button at all, and whether a pause the *renderer* performs has to go
through Python (locally cycling pause is what makes click-to-pause feel
immediate, but in a SyncPlay group a local pause is not a pause, it is a desync
the group then has to correct).

**`engage()` is idempotent, and that matters**: re-engaging is the only thing
that carries a changed setting to the renderer, which is what makes all of those
apply without a restart.

Playback then runs clean until an arrow key, ENTER or mouse motion summons the
HUD, and `hud_hide_secs` without input hides it again. On the summon, `on_hud`
asks the player for a fresh position snapshot before the bar first paints and
starts the shared **1 s ticker** that keeps its clock moving — the same ticker
the music now-playing bar uses. On the hide it drops the menu and the info
panel: the panel is anchored to nothing once the bar is gone, and leaving it set
would bring it back with the next summon, which nobody asked for.

### Scrubbing: a change previews, a commit seeks

`scrub_change` starts the gesture — it pauses playback if the user had not, so
the position is inspectable, and records the target. **It does not seek.** Only
`scrub_commit` calls `seek`. `scrub_done` clears the gesture and resumes
playback *only if we were the ones who paused it*, which is also what runs when
the renderer hides the HUD mid-drag.

### Constants mirrored into `renderer.lua`

The Skip Intro/Credits button exists twice: `jellyfin_mpv_shim/mpvtk/renderer.lua`
draws it while the HUD is idle and hands over to `hud.py`'s copy mid-segment. Two
implementations of one widget, so every number that places or styles it has to
agree — and each way of getting it wrong has its own symptom on screen:

- **the bottom inset**, measured to the button's *bottom* edge so the two line up
  whatever the label's measured height turns out to be. A mismatch is the button
  **hopping** on summon or hide.
- **the horizontal inset.** This one was a bare literal on both sides, so when
  the UI scale landed only the Python copy scaled — layout folds `dx` into `x`,
  which `scale_scene` converts — and the two buttons **drifted apart** by
  `24 × (scale − 1)` px, including the renderer-drawn hit rect, which is what you
  actually click.
- **the type size and padding**, or the two copies differ in size and weight even
  when they share a corner.
- **the colours**, where a mismatch is a **flash** of a differently-coloured
  button on summon.

`tests/test_python_lua_constants.py` enforces the set (along with the heuristic
char-width table, for the same reason: Python reserves one width and Lua draws
another).

### The bars are drawn at alpha 0, not omitted

Under the "panel" scrim the HUD's two bars paint a flat translucent band; under
any other scrim setting they are still emitted, invisible. **Invisible rather
than absent, because the renderer needs them to exist as scene nodes**: it holds
the auto-hide off while the pointer is over them (`phud_busy`), and layout only
emits a node for a container that has a fill, a border or a click. Alpha 0 costs
one ASS event that draws nothing, and the node is not a hit target — `node_at`
ignores a rect with no click, tip or hover of its own.

An open modal counts as a busy HUD through the same mechanism, which is why the
playback-info panel is a `Dialog`: it gets ESC and click-outside dismissal for
free, and it cannot be read for four seconds and then yanked away with the bar
it is attached to.
