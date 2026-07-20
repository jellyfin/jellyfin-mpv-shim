# app.py split — plan and session handoff

**Temporary.** Delete this file once the split is done and its follow-ups
are either finished or moved into `MIGRATION.md`.

Written at the end of a long field-testing + parity session (23 commits,
`0e43810..b25811e`). `mpvtk_browser/app.py` is **5,471 lines** and has
become unsafe to edit in place. This is the plan for splitting it, plus
the context a fresh session needs.

State at handoff: **796 unit tests green**; targeted integration suites
green (see *Verifying* below).

---

## 1. The split

**Do it as a mechanical partition into mixin modules. Not a redesign.**

The file is not a tangle — it is ~250 small, cohesive methods already
grouped under honest section comments. The risk is almost entirely in
*rewriting while moving*.

The decisive constraint is **test coupling**: `tests/test_mpvtk_browser_shell.py`
(5,238 lines) calls private methods on the browser instance constantly —
`b._open_add_to`, `b._track_list`, `b._pe_set_sel`, `b._effective_tracks`,
`b._render_*` — and pokes `b._pool`, `b._menu`, `b._dialog`, `b._hud_state`.
Mixins keep every `b._method` working, so **the existing suite is the
regression harness for the move itself**. A function-style split (the
`hud.py` shape) would break hundreds of assertions for no behavioural
gain — don't, at least not in the same pass.

### Partition — extract in this order (least entangled first)

1. `dialogs.py` — `_show_dialog` / `_close_dialog` / `_dialog_shell` /
   `_message` / `_confirm`, add-to dialogs, download dialog, SyncPlay dialog.
2. `auth.py` — login / Quick Connect, locked + PIN screens, user switching,
   PIN setup.
3. `settings.py` — settings tabs, generated form, servers & users, downloads
   panel + pollers, logs. **Biggest single win (~760 lines).**
4. `queue_edit.py` — queue view, playlist editor, `_block_move`, `_pe_*`.
5. `music.py` — music tabs / album / artist / genre / playlist views,
   now-playing bar.
6. `views.py` — home / grid / detail / series / season / search renderers,
   track pickers.
7. `tiles.py` — `_request_image`, `_image_done`, `_poster_for`, `_tile`,
   `_image_map`, `_backdrop_node`, `_compose_banner`, `_tile_row`,
   `_hscroll_row`, `_grid_of`, `_track_list`, `_art_cell`. **Extract last** —
   most shared.

**Core `app.py` keeps** (~1,200 lines): `__init__`, routing / epoch /
`run_async` / `_route_async`, `_load_route`, `build` / `_render_route`,
chrome, playback lifecycle (`on_playstate`, `_yield`, `enter_browse`,
`minimize`, HUD glue), `set_app` / `reassert_window_state`, `shutdown`.
**The epoch and `_lock` must live only here.**

### Mechanics

1. **One mixin per commit. Pure cut-paste.** No renames, no signature
   changes, no "while I'm here" cleanups. The file's comments memorialise a
   dozen bugs that came from exactly the closure/staleness subtleties you
   will be tempted to tidy (e.g. *"Reading self.route here raced
   navigation"*). Cleanups are separate commits, after.
2. `class MpvtkBrowser(DialogsMixin, AuthMixin, …)`. No method may exist in
   two mixins. **Add a meta-test asserting the mixins' method-name sets are
   pairwise disjoint** — that turns the one real mixin hazard (silent
   override) into a test failure.
3. Full unit suite after every commit; targeted integration suites too.
4. Head each mixin with a docstring listing the `self` attributes it
   reads/writes. The split does **not** reduce state coupling (~45 mutable
   attributes on `self` remain shared) — it makes it visible. That is the
   actual editing-safety win.

### Hazards, ranked

- **Closures over `route` dicts.** Correctness depends on capturing route
  *state* on the loop thread before dispatch (`parent = self.route.get(...)`).
  A mechanical move preserves this; anything that changes *when* a closure
  is created can silently reintroduce a race.
- **The thread contract is implicit.** Renderer handlers + `build()` = loop
  thread. `on_playstate`, `notify_update`, `set_download_status`,
  `display_item`, `on_downloads_changed` = foreign threads; everything they
  touch must be write-then-`invalidate()`. Write this into core's header.
- **Epoch discipline.** Keep `_epoch` / `_lock` / `run_async` in core. A
  cached `ep` passed across a module boundary reads fine and is subtly wrong.
- **`_lock` protects writers from each other, not from the reader.**
  `build()` reads route data unlocked. Today every writer ends with
  `invalidate()`, so a torn read is a one-frame glitch that self-heals.
  That invariant is undocumented — document it, don't "fix" it by locking
  `build()`.

---

## 2. Follow-ups (queued, not done)

Roughly in value order:

- **Deduplicate the three infinite-scroll pagers.** `_on_grid_scroll`,
  `_on_music_scroll`, `_on_genre_scroll` each reimplement the same
  invariants (route-identity guard, `_loading` flag, near-bottom threshold,
  "empty in-range page ends the list", "`_loading` must not survive
  failure"). Each copy learned those lessons *separately* — the comments
  prove it. One `_page_more(route, fetch, get_items, set_items)` helper.
- **Rename `_np_stop` → `_shutdown_evt`.** Three pollers share it
  (`_start_np_ticker`, `_poll_downloads`, `_poll_download_status`). It works
  only because it is set at shutdown; the name invites someone to clear it
  and silently kill the download pollers.
- **Epoch drops rollbacks.** `run_async` discards `on_error` as well as
  `on_done` when the epoch moved. Optimistic edits (`_pe_remove`,
  `_pe_move`, `_queue_move`, `_pe_toggle_public`) rely on `on_error` to
  restore state. Navigate away before the failure lands and the rollback is
  dropped — the route dict keeps the rejected state, and returning to it can
  show an edit the server refused. Fix: run `on_error` regardless of epoch
  (it targets a dict, not the screen), or clear `_items` on the departed
  route.
- **One dispatch table for views.** `_load_route` is a 215-line elif chain;
  `_render_route` is a dict 1,500 lines away. Adding a view means editing
  two distant points. `VIEWS = {"grid": (loader, renderer), …}`, populated
  per mixin. Do this *after* the split settles.
- **Pool starvation.** One 4-worker pool serves route loads, client
  mutations *and* `_move_downloads`' multi-GB file copy. Give long jobs
  their own thread.
- **Check-then-act in the poller starters.** `_start_np_ticker` (and the
  `_dl_thread` / `_dlbar_thread` guards) are reachable from two threads;
  two tickers can start. Harmless today (doubled refresh), but fix with a
  small lock or by starting them only from the loop thread.
- **`_compose_banner` imports privates from `display_mirror`**
  (`_apply_dark_gradient`, `_pil_font`, `_scale_to_cover`) — an optional
  Pillow-gated module. When Tk dies and someone cleans up `display_mirror`,
  the detail banner breaks. Move them to a shared `imageutil`.
- **`self.thumbs._notify = self.invalidate`** pokes a private. Make it a
  constructor arg or `set_notify()`.
- **Keep-in-sync constants across Python/Lua** with no cross-check test:
  the heuristic char-width table (`layout.py` ↔ `renderer.lua`) and
  `hud.py`'s `_SLIDER_PAD` ↔ renderer's `SLIDER_PAD`. A test that greps both
  and compares would make the contract enforced rather than commented.
- **`list_downloads()` / `download_status()` are view logic in `ui.py`**
  (~110 lines of display-tree grouping in the player bridge). Move with
  `settings.py`, or into the repository layer.

---

## 3. What this session was, and what it taught

23 commits of field-testing fixes and Tk-parity work, driven by four
code:code audits. The parity list is closed; `MIGRATION.md` ends with the
cutover checklist for deleting the Tk browser.

### The recurring failure mode — read this before writing tests

**Code that exists but never reaches the screen.** It happened five times,
and the test suite stayed green through all of them:

- `_scenes_row` — written, committed, described as "ported", never called
  from `_render_detail`.
- `collection_remove` / `collection_new` — controller methods with zero call
  sites.
- Create-collection — reachable only via a button gated on already *having*
  collections.
- Playlist "Remove Download" — wired to `_is_downloaded`, which could never
  return True for a playlist.
- `self.status` — written from 14 places, rendered in 1 (the Settings tab),
  so `_edit_call`'s failure message was invisible on every screen where
  edits actually happen.

The common shape: **assert on the helper, stub the layer beneath, never
assert on what a user would see.** The discipline that catches it:

> A test for a helper is only worth having alongside a build-to-scene test
> proving the helper is wired.

Related: a fix's error path can be **inert** because a lower layer
swallows. `_edit()` logged-and-returned, which silently defeated every
caller's `on_error` — including a delete-rollback whose test passed because
it stubbed the controller and bypassed `_edit` entirely. `_edit`,
`queue_reorder` and `playlist_move_many` now raise deliberately; the
comments say why.

### The verification that did work

**Revert-check every fix.** After making a change and adding tests, revert
the change and confirm the new tests fail. This caught several tests that
passed against broken code — including two integration tests that only
proved internal consistency, not that anything was on screen. It found more
real problems than the suite did.

### Editing hazard — this matters for the split

Much of this session was edited with Python string-replace scripts because
the file is unwieldy. **That caused three defects:**

- a replacement that silently didn't match, leaving `_addcol_name`
  uninitialised (crashed on first use);
- a regex that ate a `@staticmethod` decorator;
- a regex that dropped the closing paren on eleven call sites at once
  (caught only because the module stopped parsing).

Use the `Edit` tool. It fails loudly on a non-unique or non-matching
target; string-replace fails silently. This is a large part of *why* the
split is worth doing.

---

## 4. Verifying

```sh
python3 -m unittest discover tests                      # 796, fast
xvfb-run -a python3 -m unittest \
  tests.integration.test_mpvtk_browser \
  tests.integration.test_mpvtk_hud                      # real mpv
./regen_pot.sh                                          # after new _() strings
```

**Known issue — the integration suite is not trustworthy as a whole run.**
`python3 -m unittest discover tests/integration` reports ~12–17 failures
("renderer never became ready") that pass when their modules run in
isolation. Reproduced against a stashed tree with none of this session's
work applied, so it predates it — resource contention from many mpv
instances in one process. **Run integration modules in groups.** Worth
fixing before the Tk deletion, so that commit can be validated by a green
full run.

Note `tests/integration/_harness.py` neutralises `sys.argv` — the app parses
it on first config-dir resolution, and unittest's tokens make argparse exit.
Unit tests that import `event_handler` (or anything pulling in `conf`) need
the same guard.

---

## 5. Do not disturb

Called out by review as well-built; changing these buys nothing and risks a
lot:

- **`layout.py`** — the best file in the project. Every subtle constant
  carries its lesson (`WRAP_SLOP`, the ellipsize half-pixel slop, flex-shrink
  floors). Don't "simplify" it.
- **`widgets.py`** — genuinely declarative, zero mpv knowledge; `Table`'s
  virtualization and its `min_w` rationale encode a hard-won fix.
- **`mpvtk/app.py`** — the loop, event coalescing, the two backends, the
  metrics flow.
- **`ui.py`'s controller seam** — thin, lazily imported, with documented
  return-value contracts (`switch_user`'s False/None distinction,
  `set_watched`'s recorded-somewhere contract).
- **The renderer protocol** — semantic events only, renderer-local state
  keyed by id, sticky overlay slots with paint-order renumbering. Load-bearing
  and documented in `GUIDE.md`.

The toolkit (`mpvtk/`) is genuinely separable from the app
(`mpvtk_browser/`) — `mpvtk/` never imports the browser, and `demo.py`
proves it stands alone. The one deliberate bulge is the playback-HUD
lifecycle living in the toolkit (including the Jellyfin-specific skip
button). Accept it; don't add a second app-specific renderer widget.
