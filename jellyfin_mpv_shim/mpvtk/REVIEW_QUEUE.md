# Review queue — findings from the 2026-07-20 multi-angle audit

**Temporary.** Delete once the queue is empty; fold anything still open into
`MIGRATION.md`.

Five parallel review agents went over the mixin split and its follow-ups
(parity vs the Tk browser, concurrency, dead/unreachable UI, test quality,
general correctness). This is everything they found, plus what has been done
about it.

Status key: `[x]` done · `[ ]` open · `[~]` deliberately declined

---

## 0. Already fixed (commit 200bf47c)

- [x] **Stale home failure yanked the user out of what they were doing.**
  `on_error` was un-gated from the epoch check; `_route_async`'s handler ends
  in `_offline_fallback` → `set_source()`, which discards the nav stack, swaps
  the source and clears `_locked`. Now guarded by `route is self.route`.
- [x] **A dropped page permanently killed infinite scroll for a route.**
  `_loading` was cleared only in `on_done`, which `run_async` skips when the
  epoch moved. `run_async` gained `always`. `_load_downloads` had the same
  shape and no `on_error` at all.
- [x] `_ROUTES_CACHE` resolved through the MRO — a subclass would silently
  drop its own `ROUTES`.
- [x] A MusicVideo would have been captioned "S1E3".
- [x] Two weak tests: the `_start_daemon` lock test did not actually detect a
  missing lock (the revert-check had widened the window with a sleep), and
  the shutdown test was tautological.
- [x] `base.pot` was missing `"A move is already in progress."`

---

## 1. P1 — broken and user-visible  ✅ all cleared

- [x] **Search "Songs" renders blank rows past the first screenful.**
  `views.py:920` passes `scroll_id="search"` so the table virtualizes, but the
  `VScroll` at `views.py:928` has no `on_scroll`, so nothing ever calls
  `invalidate()` and the window computed at offset 0 is the only one
  materialized. Sole outlier among 11 virtualized lists. `head_h=120` is also
  wrong by ~10x — the table sits below the People row and up to six carousels.
- [x] **Random sort corrupts the grid.** The server reshuffles per request, so
  paging yields duplicates and skips. Tk capped a Random grid to its first page
  for exactly this reason (`library_browser/views.py:619`). `_page_more` only
  stops on an *empty* page, which a reshuffle never returns.
  **Decision: cap at one page, as Tk did.**
- [x] **65 settings rows discard the edit unless you press Enter.**
  `settings.py:213` wires `on_submit` only; `renderer.lua`'s `blur()` emits
  nothing. Type a value, click the next row, it is gone — no toast, no dirty
  marker. The `sync_path` row one branch above already learned this and its
  comment says so; it was never generalized.
  **Decision: add a semantic `blur`/commit event to the renderer protocol** —
  additive, and the alternative (a Tk-style batched Save button) is a much
  bigger UX change.
- [x] **Season "Remove Download" is structurally impossible.**
  `tiles.py:161` `_is_downloaded` has branches for plain items, `Series` and
  `Playlist` — no `Season`. `sync/db.py` has no `downloaded_season_ids`, and
  `sync/manager.py:477` expands a Season into episodes, so only episode ids are
  ever written. Consequences: `se-undownload` is unrenderable (`views.py:559`);
  the `Season` branch of `_remove_download` (`views.py:586`) is dead; a fully
  downloaded season tile never shows the badge. This is the documented playlist
  `_is_downloaded` bug, one item type over.
- [x] **`_move_downloads` with an empty field resets the store to the default
  location.** `settings.py:225`'s on-screen advice tells you to press Enter,
  which passes `""` → `relocate(None)` → `config.py:208` resets. Destructive
  and mislabelled.
- [x] **Downloads manager never shows completion.** `settings.py:645` — the
  poller breaks when `pending` hits 0 without a final reload, and the sync push
  hook only refreshes tile badges. The finished item reads `downloading` until
  a manual Refresh.
- [x] **Removing a server leaves inconsistent state.** `settings.py:379` calls
  the controller then only `invalidate()`; `LibrarySource` keeps its tokens, so
  the removed server stays in the dropdown and browsable but playback refuses
  (`ui.py:226`). Tk rebuilt the source (`gui_mgr.py:783`).

## 2. P2 — inert error paths  ✅ all cleared

The project has been bitten by these repeatedly: a handler exists, is wired,
and can never fire because a lower layer swallows.

- [x] **`views.py:597`'s "The download could not be removed." is
  unreachable** — `ui.py:818` `delete_download` catches and logs, returning
  `None`, so `on_error` never fires.
- [x] **`settings.py:685` double-swallows the same call** (inner `try/except`
  plus `ui.py`'s) with no `on_error`. A failed delete says nothing.
- [x] **PIN set/remove failure reports success.** `auth.py:171` — `ui.py:529`
  returns True/False, `_safe` discards both return and exception, and
  `_close_dialog()` + `_after_users_changed()` run unconditionally. The user
  believes their account is locked when it is not.
- [x] **`add_user`/`rename_user` swallow twice** (`settings.py:389,423`). A
  duplicate name clears the field and changes nothing.
- [x] **Download enqueue and SyncPlay join/new/leave route through
  `_client_call`→`_safe`** (`dialogs.py:305,413`). Deliberate per `_edit`'s
  docstring, but these are button presses whose failure the user should see.

## 3. New feature requests  ✅ done

- [x] **Copy logs to the clipboard from the UI.** Users will expect it.
  **Decision: no new dependency.** Layered: mpv's `clipboard/text` property
  where available, else `wl-copy` / `xclip` / `pbcopy` / `clip`, else write a
  file and report the path. Also worth a "copy the log file path" affordance.

## 4. P2 — Tk features with no mpvtk equivalent

Not in the accepted-losses list. Roughly by value.

- [x] **Context menu on track-list rows** (`Table` never gets `on_context`,
  `tiles.py:734`). Loses Play/Queue/Favorite/Download and per-track "Remove
  from Playlist" on every music playlist — only the bulk editor remains.
- [x] **Play Next Up on the season page** — `_play_next_up` exists but is only
  called from the *series* page (`views.py:636`).
- [x] **Album/artist detail header** — no backdrop, cover, metadata line,
  Overview or "Albums" heading (`music.py:191,212`).
- [x] **Per-item watched marker in the downloads panel** (`downloads.py:48`),
  and "Remove Watched" renders unconditionally (`settings.py:556`) so it looks
  destructive but often deletes nothing silently.
- [x] **Live log tailing** — one-shot snapshot only (`settings.py:709`), and
  500 of 2000 lines (`settings.py:728`). Now a 1s tail poller that only
  redraws when the ring changed, over a virtualized table of all 2000 lines.
  Needed a `follow` scroll container (renderer sticks to the end while you
  are at the end, unpins the moment you scroll up).
- [x] **Series name on episode tiles** — bare `S1E1` (`tiles.py:47`), so
  Continue Watching / Next Up no longer say which show.
- [x] **Crew job labels** — `Role or ""` (`views.py:743`) vs Tk's
  `Role or Type`, so every Director/Writer tile is captioned blank.
- [x] **Genres in the metadata line** — dropped (`views.py:284`) though
  `Genres` is still fetched.
- [ ] **SyncPlay across servers, and joined-state** — single server only
  (`dialogs.py:372`), never marks which group you are in, Leave always shown.
- [x] **Sort control on a person's filmography** — the filter bar is gated on
  `kind == "grid"` (`views.py:138`) and person routes are `"person"`. Added a
  sort-only bar; the repository has always taken sort_by/sort_order and
  `_load_person` was the one caller that never passed them.
- [x] **Zero-item guard on the Download dialog** (`dialogs.py:283`) — dead click.
- [x] **Tooltips** in browser chrome and the now-playing bar. Chrome buttons
  are tipped exactly when compact drops their label; the search button and
  the whole (icon-only) now-playing bar are tipped always.
- [ ] **Per-known-server Quick Connect** — fills the URL only (`auth.py:229`).
- [x] **"Work offline" on the connecting screen** — the `connecting` route
  now exists (chrome-free), with Work Offline gated on having downloads, and
  Retry / Sign In once the connect has actually given up. Startup opens on
  it instead of an empty home route. A failed connect with saved servers
  stays here rather than dropping to the login form (which lost the offline
  library).

## 5. P3 — degraded behaviour

- [x] Non-contiguous multi-select collapses on Up/Down (`queue_edit.py:117`);
  also no-ops for the whole selection when the first row is already at the top.
- [x] "Play All" on a playlist loses resume (`music.py:285` omits `items=`).
- [x] Download button offered while offline on the playlist page (`music.py:292`).
- [ ] "Add to Favorites" offered on MusicGenre tiles (`tiles.py:364`); Tk
  excluded it — will hit the server with a non-favoritable id.
- [x] Music tabs refetch on every switch (`music.py:91`); Tk cached per tab.
- [x] Queue removal failures swallowed — `_safe` (`queue_edit.py:111`) where
  every other edit uses `_edit_call`. `_pe_remove` restores `_items` but not
  `_sel`.
- [x] Media-info loses codec+resolution when `DisplayTitle` is absent
  (`views.py:706`): `HEVC 1920x1080` → `1080p`.
- [x] Version picker no longer dedups same-named sources (`views.py:402`).
- [x] User switcher offered while offline (`app.py:1270`); Tk gated it because
  a switch reconnects.
- [~] Offline banner is one fixed string (`app.py:1404`) — cannot distinguish
  an outage from the `work_offline` setting. **Retry failure now reports**;
  the outage-vs-setting distinction is still open.
- [x] Download status text raw and untranslated (`settings.py:594`):
  `pending`/`downloading` verbatim vs Tk's "Queued"/"Downloading 42%".
- [x] Dead buttons: playlist header renders Play All/Shuffle before the empty
  check (`music.py:282`); artist action bar renders with `ids=[]` if the song
  fetch failed (`music.py:430`).
- [x] Runtime now reads `1:52:00` rather than `112 min`.
- [x] Cast tiles square not portrait (`views.py:748`) — now the same poster
  shape as everything else, on the search People row too.
- [x] Songs tab loses per-row art (`music.py:160`) — it is the mixed-album
  case the art column exists for, and it is virtualized so the overlay
  budget holds.
- [x] Volume slider live rather than commit-on-release — live for audible
  feedback, but only the release notifies: `set_volume` woke the timeline
  thread, which posts to the *server*, so one drag was a burst of round
  trips.
- [x] Add-to name boxes and the login form lack Enter-to-submit.
- [ ] Seek time frozen during scrub (`music.py:374`).

## 6. P4 — dead / half-finished

- [x] **`set_offline` (`app.py:1383`) has zero production call sites** — only
  three in tests. `_offline` is really driven by `set_source`.
- [x] `"connecting"` in `CHROME_FREE` (`app.py:96`) — no route, nothing
  navigates to it.
- [x] Trailer fetch for `Series` (`views.py:989`) can never surface — series
  route to `_render_series`, which has no Trailer button. One wasted API call
  per series load.
- [ ] `_grid_of(heading=…)` (`tiles.py:699`) — never passed by any caller.
- [x] `MENU_FAVORITE` (`tiles.py:370`) reads as widening `MENU_PLAYABLE` but is
  a no-op; both names are already in it.
- [x] `config.py:17` `_HIDDEN` doesn't cover `client_uuid` — an editable
  free-text row that rewrites the device identity the server tracks sessions by.
- [ ] `_render_album` has no `on_scroll` (`music.py:202`); its virtualized table
  relies on live offsets, so it windows wrong on the mpv < 0.36 fallback path.
- [ ] `Table`'s `on_dbl` (`mpvtk/widgets.py:590`) is unused browser-wide — the
  natural home for the missing queue double-click-to-jump.

---

## Incidental fixes found while working the queue

Not from the audit — turned up while doing the above.

- [x] **The renderer had no tests at all.** Two protocol additions (the
  textbox `commit` event, `follow` containers) were written against nothing
  but hand testing. `tests/lua/` now loads the real `renderer.lua` against a
  faked mpv and drives it through the real script-message boundary;
  `tests/test_renderer_lua.py` runs it in the normal suite (skipped when no
  Lua interpreter is installed — mpv embeds one, so CI and any dev machine
  with mpv has it).
- [x] **An unknown icon name took down the entire scene.** `icon_ass` raised
  `KeyError` out of the middle of layout, so one button naming an icon
  that isn't in the generated set blanked the whole browser. Now renders
  nothing and logs once. (Hit this for real: `cloud_off` isn't in the set.)
- [x] **`test_pin_is_hashed_not_plaintext` flaked ~1 run in 70.** It searched
  the whole serialized user entry for the PIN `"1234"`, which turns up
  inside a random hex salt or hash about that often. Uses a PIN that cannot
  appear in hex.

## Calibration on these findings

The parity agent flagged that its findings come in two tiers: items it traced
end to end itself (the search-scroll gap, Random-sort paging, `set_offline`
being test-only, settings-key parity, track-list `on_context`, block-move,
album/artist headers, music tab refetch, route-table coverage), and items from
its own sub-agents that it spot-checked but did not exhaustively re-verify
(the downloads poller gap, `_remove_server`, crew labels, SyncPlay, log line
counts, per-server Quick Connect, tooltips, most of P3). Every one of the ~8 it
spot-checked held up. Treat tier two as strong leads rather than verified.

One agent also reported the `Read` tool twice returning content that differed
from disk — including a line shown *without* the very code under investigation.
It discarded both and re-verified with grep. Findings here rest on grep/sed.
