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
`mpvtk.MpvtkApp.attach` — see `mpvtk/GUIDE.md` for the toolkit itself.

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

### Two app-wide surfaces

**Cast screen** (`cast.py`) — the Chromecast-like preview (idle "Ready to cast"
backdrop plus `DisplayContent` item preview) is a browser **route**, not a
separate UI. Backdrop, gradient and text are baked into one full-window bitmap
because mpv composites overlay bitmaps *above* all script ASS (`mpvtk/GUIDE.md`
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

## 7. Parking scroll offsets

`park` stashes the current screen's offsets on its route dict so coming back lands
where it was left.

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
epoch-gated (see `AsyncRunner`), so a save that fails after the user has walked away
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
