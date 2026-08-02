# UI/UX fixes — work list (branch `ui-ux-fixes`)

Ten open issues from the 3.0.0pre10 feedback wave, investigated against the
tree at `acb68211`. This file is the plan of record: the diagnosis, the
decision taken on each, and the shape of the fix. Decisions marked **[iw]**
came from Izzie on review of the investigation; everything else is the
investigation's own conclusion.

Commit policy for the branch: one commit per issue, with the two structural
changes (renderer-side scrub preview, true virtual scroll) landing as their
own commits separate from the small fixes. Shared groundwork (the mpvtk
disabled state) lands before the issue that needs it.

| # | Title | State |
|---|---|---|
| — | Groundwork: mpvtk disabled state | done `c968d588` |
| [618(a)](#618--controls-slow-to-update-visually) | Mute/volume lag behind the ticker | done `29921e31` |
| [613](#613--hiding-titles-also-hides-the-year) | Hiding titles also hides the year | done `53e14fc6` |
| [614](#614--mouse-backforward-buttons-seek-chapters) | Mouse back/forward → chapter seek | done `d35e5e38` |
| [615](#615--enable-osc-does-nothing-under-the-jellyfin-ui) | "Enable OSC" does nothing under the Jellyfin UI | done `f6a2210d` |
| [616](#616--cover-size-in-the-library-view-dialog) | Cover Size in the library View dialog | done `ee8415dc` |
| [560](#560--continue-watching-doesnt-update) | Continue Watching doesn't update | done `fbd119c9` |
| [620](#620--playback-hud-design-feedback) | Playback HUD design feedback | done `456682c5` |
| [575](#575--commercial--preview--recap-segments) | Commercial / Preview / Recap segments | done `88da297a` |
| [618(b)](#618b--renderer-side-scrub-preview) + [612](#612--hover-bubble-position-depends-on-chapter-title-length) | Renderer-side scrub preview | done `cb24f92d` |
| [617](#617--scrollbar-loses-the-drag-while-items-page-in) | True virtual scroll | done `9bdedd12` |

All ten issues are on `ui-ux-fixes`, one commit each, with the full unit
suite green after every one and the mpv integration suite green on both
backends. What each of the two structural changes actually came to is under
[Notes on the two structural changes](#notes-on-the-two-structural-changes).

---

## Groundwork: a disabled state for mpvtk widgets

**[iw]** "Adding a disabled state is an mpvtk change; we have been working
around the lack of one long enough that I think we should do it. Make sure
the disabled colour is themable if not using an already provided semantic
value."

Needed by #613 (grey out Show Year where it means nothing) and #620 (grey
out the auto-hide delay in the mode that ignores it), and it retires the
"omit the control entirely / draw it live and no-op the handler" workarounds
elsewhere.

Shape:

- `disabled=True` on `Checkbox`, `Button`, `Dropdown`, `Slider` (the four
  that have interaction to suppress). Serialised into the scene node.
- Renderer: a disabled node is skipped by `nav_candidates` (no focus ring, no
  D-pad stop), takes no hover style, and its click/keyboard handlers do not
  fire. It still draws, in muted colours.
- Colours: **no new token.** `tok.on_surface_faint` (`777777`) for label and
  glyph, `tok.control_sunken` (`2a2a2a`) for the control's own fill. Both are
  already themable and already pushed over `mpvtk-theme`, which is the
  "already provided semantic value" case.
- Tooltips still show on a disabled control — that is where the reason for it
  being disabled belongs.

Tests: `tests/lua/test_renderer.lua` for the no-focus/no-click contract,
plus a widget serialisation test.

---

## #612 — hover bubble position depends on chapter-title length

**Cause (confirmed).** `hud._preview_float` (`hud.py:753-795`) centres the
bubble on a width it *assumes*:

```python
bw = max(entry["lw"] if entry is not None else 0, 120) + 16   # :784
px = track_x + frac * track_w - bw / 2
return Box([...], anchor="sw", dx=px, ...)     # no w= on the Box
```

The `Box` has no `w=`, so its drawn width is its content's. With trickplay
the frame is the widest child and the assumption holds; **without trickplay
`bw` is a flat 136 while the real width is the chapter Text's**, so the drawn
centre lands at `true_centre + (real_w - 136)/2`. A ~25-char title measures
about 136 (correct), a short one measures less (sits left). Exactly the
reported pattern, and why it was invisible to a trickplay user. The reporter's
log confirms `trickplay: No trickplay data available` for that file. Their
1.75x UI scale is not the cause; it is why the error lands so wide on screen.

**Decision [iw].** **Fixed by #618's renderer-side bubble, not separately.**
The renderer measures its own text, which makes this class of bug unreachable
rather than fixed; a standalone `w=bw` patch would only be deleted again a few
commits later. #612 therefore closes when #618(b) lands, and not before.

---

## #613 — hiding titles also hides the year

**Cause (confirmed).** `tile_renderer._tile` (`:735-737`) does
`if not show_title: title = subtitle = ""`, and `grid.py:249-252` then
collapses `caption_h` to 0.

**Decision [iw].** Year-only is allowed where the second line really is a
year; where it is not, grey the checkbox out rather than silently doing
something else.

**Fix.**

1. `_tile`: when `show_title` is off, keep the subtitle instead of blanking
   it.
2. `strips._paint_caption` (`:763`): draw the subtitle at the title's y
   (`tile_h + 6`) when there is no title, so a one-line caption sits where a
   caption sits.
3. `grid.py`: `caption_h` becomes one line (`6 + title_size + 6`) for
   titles-off-year-on, staying 0 for both-off.
4. View dialog: **nothing to grey out, after checking.** The rule [iw] was
   "if the line means something semantic, relabel the checkbox; otherwise grey
   out based on route" — but the dialog is reachable from exactly two routes,
   `GridPage` and `PersonPage` (`grid.py:297`), and both always produce a
   year: a person's filmography is `Movie,Series` only
   (`get_items_by_person`, `api.py:785`). Live TV, season and series pages
   have no View button. The one non-year line reachable here is an Episode
   inside a collection or playlist grid, and `labels.py:52-56` deliberately
   keeps `show_year` off that line ("switching it off was not a request to
   blank the line").

   So: the checkbox keeps its label, stays enabled, and no route is greyed.
   The disabled state still lands as groundwork — #620 uses it. **Say if this
   read is wrong and a specific view was in mind.**

---

## #614 — mouse back/forward buttons seek chapters

**Decision [iw].** A settings option, default disabled — the buttons are easy
to hit by accident on some mice, and skipping a chapter is not as forgivable
as a Back press in the library.

**Fix.** New `mouse_chapter_nav: bool = False`, in the Interface section
(`config.py:85-91`). When set, the player binds `MBTN_BACK` / `MBTN_FORWARD`
via `_bind_key` (`player.py:733`) to the same prev/next-chapter behaviour the
HUD's chapter buttons use (`hud._chapter_jump`: prev re-seeks the current
chapter's start unless within its first 2s, like mpv's `add chapter -1`).

Scope note: this is **playback-only**. While the HUD is summoned the renderer
owns the thumb buttons (back = ESC, forward = history), and while browsing
they navigate the library. Only the attached-but-idle playback state leaves
them free (`renderer.lua:4438` binds `mbtn_left` alone).

Not exposed as a raw keybind, because no `kb_*` setting is reachable from the
settings UI.

---

## #615 — "Enable OSC" does nothing under the Jellyfin UI

**Cause (confirmed, and by design).** `enable_osc` only ever reached mpv's own
OSC. Under `osc_style == "mpvtk"` the player forces `_player.osc = False`
unconditionally (`player.py:3018-3025`), and the HUD is gated purely on
`use_hud()` (`gateway/hud.py:16`), which never consults the setting.

**Decision [iw].** Add a **"No controls"** option to Player Controls Style and
**orphan `enable_osc`** — "no OSC" violates expectations for the new app, so
anyone who wants it opts in again through the new option.

**Fix.**

1. `osc_style` gains `"none"`: `LABELED_ENUMS["osc_style"]` (`config.py:139`)
   plus `resolve_osc_style` (`mpv_options.py:29-57`).
2. `"none"` means: no mpv OSC, no mpvtk HUD (`use_hud()` returns False), no
   scripts loaded.
3. **Skip Intro must not vanish with the HUD.** `player.update`'s
   `hud_skip_button` (`player.py:1352-1356`) has to fall back to the OSD
   seek-to-skip prompt when the HUD is off, or "ask" mode has no surface.
4. `enable_osc` is dropped from the settings UI (`config.py:88`) and from
   `player.enable_osc`'s decision; the key stays in `conf.py` so existing
   `conf.json` files still load, marked orphaned. **No migration** — a user
   who had it off gets controls back and re-opts-out through the new option.

The way back to the library with no controls is unchanged: `kb_stop` (`q`)
stops to the browser, `kb_menu` (`c`) opens the OSD menu.

---

## #616 — Cover Size in the library View dialog

**Decision [iw].** Stays **global**, not per-library — jellyfin-web has no
equivalent to store a per-library value in, so a per-library one would be
ours alone. Live adjustment is nicer UX and worth doing. The current range is
the weak part: it only goes *up* from the default, which is not much use;
**it should also go smaller**.

**Fix.**

1. Re-derive the four tile geometries live. `poster_scale` is baked into
   `self.geom / geom_wide / geom_square / geom_banner` once in
   `MpvtkBrowser.__init__` (`app.py:296-310`); everything downstream reads
   `art.geom*` at render time, so extracting an `_apply_cover_size()` and
   calling it on change is enough. Strip bitmaps are keyed on the geometry
   dims (`strips.py:253`), so no stale art survives.
2. Parked scroll offsets must be forgotten on change — the row pitch moves,
   so a remembered offset points somewhere else.
3. Extend the enum (`config.py:161-167`) **downward, without touching the
   existing labels** [iw] — every one of them keeps pointing at the value it
   points at now, so its 86 translations survive:

   | Label | Value |
   |---|---|
   | Theme default | `None` |
   | Extra Compact | 0.75 |
   | Compact | 0.85 |
   | Small | 1.00 (the base) |
   | Medium | 1.20 |
   | Large | 1.40 |
   | Extra Large | 1.70 |
4. Surface it in the View dialog (`dialogs.view_settings`, opened from
   `grid.py:575-583`) alongside the existing view settings, and drop
   "Takes effect after a restart" from its description (`config.py:305`).

Note this deliberately reverses, for this one setting, the reasoning in
`set_theme`'s docstring (`app.py:915-921`), which declined live geometry
re-derivation because "a cover size changing under the pointer" was a
downside. For a control labelled Cover Size that is the point. Theme-driven
geometry stays restart-only.

**Open:** labels for the smaller steps — see [Ambiguities](#ambiguities).

---

## #617 — scrollbar loses the drag while items page in

**Cause (confirmed), two layers.** The grid's height comes from *loaded*
items: `nrows = (len(items) + cols - 1) // cols` (`tile_renderer.py:853`), so
every appended page grows `scroll_max`. The drag maths
(`renderer.lua:2761-2772`) re-reads `maxs` live against a fixed `start_off`:

```lua
local delta = (y - state.drag.start_m) / range * maxs
set_scroll(node, state.drag.start_off + delta)
```

so a grown `maxs` maps the same pointer delta onto a bigger jump, while the
thumb simultaneously gets shorter — and slides out from under the cursor.

**Decision [iw].** "We know how many items there are, we should do true
virtual scroll." So: fix the cause, not the drag.

**Fix.**

1. **Full-height grid.** Rows come from `route["_total"]`, not `len(items)`.
   Unloaded rows are the same fixed-height `Spacer`s virtualization already
   draws, so the scrollbar is full-length from the first frame and
   `scroll_max` never moves while paging — which is also what the reporter
   means by "same as jellyfin webui".
2. **Fetch by visible range, not by proximity to the bottom.** `Paginator.more`
   (`pagination.py:108-160`) currently only appends from `len(items)` when
   within `PAGE_SLOP` of the end. It grows a windowed mode: the rows in view
   ask for the pages that cover them. The existing fixed-page machinery
   (`_pages` / `_fetch`, `:213-238`) already fetches arbitrary offsets and
   keeps a bounded window, so this is closer to unifying the two modes than to
   writing a third.
3. **`_items` becomes sparse** (index-keyed, or a list padded with `None`).
   Blast radius is contained: `"_items"` appears in 5 files, 28 sites
   (`app.py`, `pages/grid.py`, `pages/queue_edit.py`, `tiles.py`,
   `settings/home.py`). Every consumer that means "everything on screen" —
   Play All, shuffle, the count line — has to be audited for the holes.
4. **Drag anchoring rides along**: with `maxs` stable the drag no longer breaks
   here, but anchoring it to the pointer the way the dropdown scrollbar
   already does (`state.dd_bar_drag = { grab = y - t.y }`, `renderer.lua:2906`)
   makes it robust wherever content still grows mid-drag.

Applies to every infinite-scroll route: grid, person, music, music_genre.

---

## #618 — controls slow to update visually
<a id="618b--renderer-side-scrub-preview"></a>

Two separate problems behind one report.

### (a) Mute button lags 0.5–1.5s — confirmed

The icon reads `st["muted"]` from the playstate snapshot (`hud.py:648`).
`toggle_mute` sets mpv's property directly (`gateway/hud.py:97`) and *nothing
pushes a new snapshot*; the only thing that does is the browser's 1s ticker
(`app.py:1825`) — hence the delay, jittering with where in the tick the click
landed. Pause feels instant because `pause` **is** observed
(`player.py:795` → `_on_pause_change` → `timeline_handle`). `mute` and
`volume` are not observed at all.

**Fix.** `self._observe("mute", ...)` / `self._observe("volume", ...)` in
`_bind_mpv_handlers` (`player.py:792-801`), calling `push_playstate()`
directly — **not** `timeline_handle`, which also POSTs to the server. This
covers mpv's own `m` and wheel bindings too, not just our buttons. Keep the
HUD's optimistic local flip as well (the favourite button's pattern,
`hud.py:481`) so the icon changes on the click rather than on the round trip.

### (b) Hover preview lags the cursor — structural

Passive hover reports are throttled to 0.15s (`renderer.lua:1658-1677`), then
each one round-trips to Python, rebuilds the **entire** HUD tree
(`hud_menu_state()` = a full `osc_bridge.build_state()`, `chapters()`,
`get_speed()`), re-lays it out and pushes a new scene. With trickplay each
new tile index also costs a file read plus a PIL decode plus a bitmap
publish, **on the loop thread**. The settings dropdown feels instant by
contrast because the renderer draws its hover state locally with no Python
involved.

The reporter's log also has `mpv: main: Too many events queued.` twice, once
mid-playback at 15:15:59 — mpv core dropping client events because the client
is not draining fast enough. Not attributed, but consistent with the loop
thread being busy.

**Decision [iw].** "If hovers on trickplay are causing repaints, I could see
that alone being enough to cause event issues. Trickplay seems like something
where we should feed the info into the HUD and let it deal with it."

**Fix — renderer-side scrub preview.** Push the data once per video, let the
renderer draw the bubble with no Python in the loop:

- The trickplay tile file is **already raw BGRA** (`trickplay.py:203-232`),
  which is exactly what `overlay-add` consumes, and frame *n* is at
  `n * w * h * 4`. The renderer's image path already does crop-offset maths
  (`renderer.lua:770-808`); it needs a base offset added to it.
- The metadata message already exists and is still sent:
  `shim-trickplay-bif` (count, multiplier, width, height, path) — this is how
  the retired lua OSC drew its preview, so the design is known-good rather
  than speculative.
- Chapters (times + titles) get pushed the same way, once per load.
- The renderer then owns hover entirely: no 0.15s throttle, no scene rebuild,
  no PIL on the loop thread, and it measures its own text — which makes #612
  unreachable rather than fixed.
- `hud._preview_float` / `_trickplay_frame` and the `hover`/`hover_end`
  plumbing come out. `HudController.hover` state goes with them.

Constants shared with `hud.py` are pinned by
`tests/test_python_lua_constants.py`, as with the standalone Skip button.

---

## #620 — playback HUD design feedback

Four complaints, four constants or deliberate rules. All become settings.

### Scrim

Today: `SCRIM_FRAC 0.55` / `SCRIM_MAX 380` with the gradient's dense end at
`bottom=215` (`hud.py:42-48`, `:719-727`). Sized so the title and slider sit
on the solid half of the ramp — so shrinking it means moving the fade
midpoint too, not just the height.

**Decision [iw].** Lower it, and offer four variants to UX-test later; which
becomes the default is decided after that test.

| Value | Meaning |
|---|---|
| `default` | Today's gradient, lowered |
| `half` | Half-height gradient |
| `panel` | Full-width flat translucent band the height of the bar — a hard edge, no ramp [iw] |
| `none` | No scrim; HUD text and icons get a drop shadow for legibility |

`none` needs a text shadow in the renderer, and that work is **in scope for
this branch** [iw]. It is in-idiom: text is drawn with hardcoded
`\bord0\shad0` (`renderer.lua:691`) and the themed heading glow already takes
the `\bord`/`\blur` path (`:682-687`), so this is a per-node flag, not new
machinery.

### Auto-hide

Today: `PHUD_HIDE_S = 4` fixed, and `phud_busy()` returns true while paused,
so the HUD never hides on a paused video (`renderer.lua:4334`, `:4468-4478`).
There is no "pointer left the controls" hide at all — mouse motion only
re-arms the timer.

**Decision [iw].** Three modes; **default is hide-if-not-hovered, at all
times** (i.e. pausing no longer holds the controls up):

| Value | Meaning |
|---|---|
| `hover` (default) | Hide as soon as the pointer is not over the controls, paused or not |
| `always` | Timer auto-hide, including while paused |
| `paused` | Timer auto-hide, but never while paused (today's behaviour) |

Plus `hud_hide_secs` (default 4). **`0` forces `hover` mode** whatever the
mode says [iw].

Keyboard/remote caveat: `hover` cannot mean "hide instantly" when there is no
pointer driving. A HUD summoned by key/remote (`state.phud.kbd`) keeps the
timer; `hover` applies to the pointer-driven case.

**Follow-up (found by the integration suite, fixed in `793d8793`).** "The
pointer is on the controls" was asked of `state.mouse`, which turned out to
answer for a pointer that was not there:

1. A mouse that has not been touched still reports a position. Left anywhere
   in the top or bottom band it read as a hover, and a HUD summoned with the
   keyboard then never hid. `phud_busy` now requires the pointer to have
   *moved* since this summon (`state.phud.moved`), which is what "reaching
   for a button" always involves.
2. The idle-HUD branch of the `mouse-pos` observer returns before
   `on_mouse_move`, so a **mouse** summon left `state.mouse` holding the
   position from before HUD mode. The observer records it for both branches
   now.
3. mpv keeps reporting the last coordinates with `hover=false` once the
   pointer leaves the window; those are forgotten now.

Two `test_mpvtk_hud` integration tests were failing on both backends because
of (1) — under a bare X server the pointer parks at 0,0 and the top bar is
drawn under it, so the HUD was held up for ever. `test_paused_video_keeps_hud_up`
was also stale: it asserted the *old* rule, and now drives `mode: "paused"`
through `hud_key_opts` with a companion test for the new default. The fake
controller sends conf.py's `hide`/`mode` defaults rather than omitting them,
because an absent `hide` means zero, and a HUD that hides half a second after
each keypress cannot be walked with the arrow keys at all.

### Subtitle margin

`gateway/hud.py:111` raises `sub_margin_y` to 130 while the HUD is up (already
skipping top/middle-positioned subtitles). **Decision [iw]: keep enabled by
default, make it disableable.**

### Settings added

`hud_scrim: str = "default"`, `hud_autohide: str = "hover"`,
`hud_hide_secs: float = 4.0`, `hud_sub_margin: bool = True`. Labelled enums in
`config.py`, Interface section beside the existing `hud_grab_keys` /
`hud_wake_key`. The renderer-side ones ride the same push path as
`mpvtk-scale` / `mpvtk-theme`.

---

## #575 — Commercial / Preview / Recap segments

**Cause.** The plumbing is already generic and just under-asks:
`Media.get_intro` requests `include_segment_types=["Outro", "Intro"]`
(`media.py:499-501`) though `Intro.type` is a free string, and only the button
label (`player_reporting.py:157-159`) and the intro-vs-outro prompt rule
(`player.py:1358+`) are type-aware. The server enum is
Unknown / Commercial / Preview / Recap / Outro / Intro.

**Decision [iw].** One dropdown per segment type — **no / ask / always** —
which is what jellyfin-web does. Seek-to-skip stays a separate checkbox,
default off, as today. **Intro and Credits default to ask.**

**Fix.**

1. Five string settings: `segment_intro`, `segment_outro`,
   `segment_commercial`, `segment_preview`, `segment_recap`, values
   `off` / `ask` / `always`. `str` is one of the types `settings_base`'s
   `object_types` accepts.
2. Defaults: intro `ask`, outro `ask`, the other three `off`
   (see [Ambiguities](#ambiguities)).
3. Migrate the four orphaned booleans on load: `*_always` → `always`, else
   `*_enable` → `ask`, else `off`. The old keys stay in `conf.py` as orphans
   so existing files load.
4. Widen `include_segment_types` to every type whose setting is not `off`, and
   drop the "any of four booleans" gate at `media.py:633-639`.
5. Per-type button/prompt labels ("Skip Recap", "Skip Preview", …). Check for
   an English collision needing `_p()` context before adding one.
6. `skip_intro_on_seek` unchanged.

The Skip Intro / Credits settings section (`config.py:116-118`) becomes five
dropdowns plus the checkbox.

---

## #560 — Continue Watching doesn't update

**Cause.** The server pushes `UserDataChanged` to the user's sessions;
`clients.callback` forwards any `MessageType` and `EventHandler.handle_event`
logs unknown ones (`event_handler.py:118-122`) — so it arrives and is dropped
today. Separately, Home only refreshes via `go_back` (`app.py:607-610`), so
**the shim's own playback leaves it stale too**: finishing a film returns
through `enter_browse`, which does not reload.

**Decision [iw].** Both halves, with a debounce.

**Fix.**

1. `@bind("UserDataChanged")` on `EventHandler`, forwarding to a browser hook,
   modelled on the Live TV one (`event_handler.py:230-249` → `ui.py:289` →
   `app.py:533`), including its rules: **a load, not a reload** (no epoch
   bump, nothing in flight cancelled, the screen keeps its data until the new
   rows land) and **deferred while a menu or dialog is up**.
2. **Debounce.** The event fires on every progress report, including our own
   playback, so a naive hook would refetch Home every few seconds mid-film.
   Coalesce on a short timer and skip entirely while not browsing.
3. Refresh Home when playback stops and Home is the live route, covering the
   local case.

---

## Notes on the two structural changes

Written while planning them and rewritten to what they came to. None of it is
discoverable from the issue text, and some of it is a trap.

### 618(b) — renderer-side scrub preview — DONE

Shipped as described below; what follows is what it actually came to, kept
because the shape of the boundary is the useful part.

**The bubble is drawn in `render()`, from data that was already local.** The
tile file is raw BGRA frames back to back and `overlay-add` consumes exactly
that, so frame *n* is a byte offset — `draw_image` grew a `node.base` that is
added to the crop offset it already computed, and nothing decodes anything.
The chapter caption comes from mpv's own `chapter-list`, observed. Text is
measured by the renderer, which is what closes **#612**: there is no longer
an assumed width and a drawn width to disagree.

**The three `shim-trickplay-*` messages are handled directly.** They are the
TrickPlay worker's own, already sent for thumbfast.lua, so nothing extra
crosses the boundary and the two consumers cannot disagree about which
generation of the file is live. `player.trickplay_meta` and the gateway's
`trickplay()` are gone with the Python decode path.

**The seek slider's opt-in is `preview=True` → `node.pv`.** `hoverev` and the
whole throttled value-reporting path (`notify_hover` / `fire_hover` /
`hover_watch`) are deleted; `hev` — plain enter/leave for the tile play-chip
— is untouched, and still shares `update_slider_hover` for the reason the
comment there gives. Deleting those four file-scope locals is also what
bought the headroom this needed: `renderer.lua` was **at** the 200-local
ceiling and is no longer.

**Three positions feed it, in order:** an in-flight mouse drag, an
arrow-adjust that has actually moved (`nav_scrubbed`, so merely focusing the
bar does not raise a bubble), then the pointer. `state.pv_rect` is the drawn
result and is reported in the debug state — the bubble is not a scene node,
so that is the only way a test can see it.

`_SLIDER_PAD` left `hud.py` with `_preview_float`, so
`tests/test_python_lua_constants.py` lost that pair: the inset now exists in
one place. Coverage is `tests/lua/test_renderer.lua` (16 cases: placement,
centring, frame indexing for both tile layouts, clamping past the last tile,
and the clear) plus the two integration tests, which now read the renderer's
state instead of looking for a `hud-preview` node.

(`test_full_lifecycle` and `test_paused_video_keeps_hud_up` were failing on
both backends when this landed. Not this change — #620 fallout, fixed in
`793d8793`; see the follow-up under that issue.)

### 617 — true virtual scroll — DONE

Shipped for the **grid and person routes** (`route["_items"]`), which is
what the issue is about. What it came to:

- `pagination.spread` pads a list to `_total` and places a fetched page at
  its own offset. `Paginator.window` asks for the pages covering a range and
  is called from **render**, like `ensure` and for the same reason: which
  items are visible is a question about geometry.
- The window is `TileRenderer.row_window` / `list_window` — the *same*
  function the renderer composites from, because fetching one window and
  drawing another is the failure mode worth designing out.
- A hole draws as an empty tile (`Tile(key="_pending%d")`, keyed by column so
  every unfilled slot in a library shares one cached blank) and carries no
  click region.
- **Random stays exempt and unchanged.** `_install` still reports what it
  loaded as the whole list, so there are no holes and nothing to window.
- **Render must not retry.** A failing server asked once per frame is a loop,
  and the toast it raises is itself a frame. So an attempt is recorded in
  `_win_tried` and the view clears that on a *scroll* (`Paginator.rewindow`)
  — the cadence the old pager had. `_win_load` is separate and is the
  in-flight set, so clearing one mid-scroll cannot re-issue the other.
- `WINDOW_PAGE = 100` equals the repository's default page limit on purpose:
  the initial load fills page 0 exactly, so the first render asks for nothing.
- The scrollbar drag is anchored inside the thumb (the dropdown scrollbar's
  pattern) instead of a delta scaled by a live `scroll_max`.
- Two knock-ons worth knowing: the count line is `"%d items"` rather than
  `"%(shown)d of %(total)d"` (how much is loaded stopped being visible), and
  `AsyncRunner.run` tolerates a shut-down pool, because render can now submit
  work and a closing app must not raise out of a repaint. `_play_photo` reads
  the loaded window rather than everything walked past.

**Music, music genre and Live TV keep append-on-approach**, deliberately.
Behind music's one `_data` are three different lists — a tile grid, a track
*table* and the unpaged genres — so windowing it means teaching `track_list`
about holes as well, which is a second piece of work rather than the same one
applied twice. Live TV's lists re-read and merge themselves on a timer and a
websocket, and a windowed fetch would fight that discipline. The drag
anchoring above is renderer-side and covers all of them anyway: the thumb no
longer slides out from under the pointer when a list grows, which is the
symptom that was reported.

`TestPagersShareTheirInvariants` lost its `grid` view for this reason and
`TestGridWindowing` is the grid's contract now.

**Play All and Shuffle are not affected** — checked against jellyfin-web at
Izzie's suggestion, and the answer is that neither client plays "what is
loaded".

jellyfin-web hands the *query* to the playback manager and lets the server
select: `getItemsForPlayback` (`playbackmanager.js:132`) caps every playback
query at **`Limit: 300`**, and the only caller that opts out is PhotoAlbum,
via the explicit `UNLIMITED_ITEMS = -1` sentinel. Its legacy library Shuffle
runs its own `SortBy: Random, StartIndex: 0, Limit: 300`; the modern one
passes `queryOptions` with `SortBy: Random` and takes the default cap. Play
All fetches the *library folder* and lets the folder expand server-side —
still through the same 300 cap.

The shim already works this way: `_play_all` and `_shuffle`
(`pages/grid.py:853-877`) call `get_play_all_ids` / `get_shuffle_ids`, which
ask the server with the grid's own sort, filters and collection type, capped
at 200. Neither reads `route["_items"]`, so making that sparse cannot reach
them, and the "Play All plays the first hundred" hazard was designed out
before this branch existed.

One alignment taken while here: **our cap was 200 and web's is 300**, for no
reason beyond when each was written. It is now `repository.QUEUE_LIMIT`, so
the number has somewhere to explain itself.

**Paginated mode stays as it is.** It has no scrollbar and no growing
content, so none of this bug — unifying the two modes is a separate question
from fixing the one that is broken.

## Working notes

Conventions this branch had to follow, collected so the next session does not
rediscover them:

- **Run the suite as `xvfb-run -a python3 -m unittest discover tests` from the
  repo root.** Two pre-existing warts: several modules (`test_shell_playback`,
  …) cannot be run *by name* because they rely on discover to set `sys.argv`
  before the app parses it; and `tests.test_mpv_options` plus
  `tests.test_playstate_payload` **segfault at interpreter exit when run
  together** — on the untouched tree too. `discover` hits neither.
- **Any new setting** needs: an entry in `docs/configuration.md` (enforced by
  `tests/test_docs_coverage.py`), its *values* added to that test's
  `vocabulary` set if it is an enum, a row in `mpvtk_browser/config.py`
  (SECTIONS + LABELS + NOTES), a regenerated
  `tests/snapshots/settings.jsonl` (`python3 tests/test_scene_snapshots.py
  --update`), and `./regen_pot.sh`.
- **`./regen_pot.sh` only.** Never `--merge`, never touch the per-locale `.po`
  files; that is Weblate's job and merging locally collides with master in
  files nobody on the branch edited.

## Order of work

Groundwork first, then the small fixes, then the two structural changes. One
commit each.

All twelve are done (see the table at the top).

The one thing deliberately left: music, music-genre and Live TV still page
on approach rather than windowing. The reason, and why the drag is fixed for
them anyway, is under #617.

## Decisions taken without asking

Small enough to reverse, recorded so they are not mistaken for oversights.

- **#575 defaults:** Commercial / Preview / Recap default to `off`. Intro and
  Credits default to `ask` [iw]. jellyfin-web ships the new three off too.
- **#614 with no chapters:** the binding does nothing. Falling back to
  ±10s/+30s would be a different feature wearing the same setting's name.
- **#613 greying:** none, for the reason recorded under that issue — the
  premise for it turned out not to hold.
- **#620's zero delay is floored at 0.5s.** `hud_hide_secs: 0` forces hover
  mode as intended, but a literal zero would also blink the controls out in
  the same frame as the mouse motion that summoned them. If 0 should instead
  mean "no timer at all, visibility is purely the hover test", that is a
  different mechanism — the controls would only ever appear with the pointer
  in the bottom band — and wants building separately.
- **#620's `panel` scrim is a full-width band** the height of each bar, and it
  is painted as the bars' own background rather than as a separate node. The
  bars therefore carry a fill in *every* mode (transparent when there is no
  panel), because `layout` only emits a container node that has something to
  draw and the renderer needs those two rects to exist — it tests the pointer
  against them for the hover-hold. Their ids (`hud-bar`, `hud-topbar`) are a
  contract with `renderer.lua`'s `phud_busy`.

## Hand-testing checklist

The suites are green (2893 unit, plus the mpv integration matrix on both
backends), so this is not "check the code works" — it is the list of things
those suites are structurally unable to see. Ordered by how likely a problem
is and how badly it is covered.

### 0. What the round found, and what was done

Nine issues, one commit each. Everything below is Izzie's, verbatim, with the
outcome under it.

| Reported | Fix |
|---|---|
| `AttributeError: settings.enable_osc` on minimize | `3e4c32af` — #615 retired the key and two call sites in `gateway/playback.py` kept reading it. `tests/test_settings_references.py` now walks the package for `settings.<name>` and fails on any the schema does not declare. |
| (the same crash, downstream) | `5893d5bd` — the three browse/playback transition callbacks were unguarded, so one raising line abandoned every browse→video transition half-applied. They go through `_tell_controller` now. |
| Trickplay bubble snaps back on mouse release | `971126c3` — a drag returns out of `on_mouse_move` before the hover tracking, so `pv_secs` still held where the pointer was when the drag *started*. `slider_set_from_x` carries it along now. |
| Cover Size needs leaving the page | `0ab0204b` — `_grid_shape` parks the resolved `TileGeom` on the route. Cleared for the whole nav stack. |
| "half" scrim unreadable on the top bar | `9b750110` — the top band is already the small one; halving it too put the title in the fading half of a 65px ramp. Bottom ramp only. |
| Continue Watching redraws when nothing changed | `a9883418` — two things: `load()` published a primary-only batch mid-refresh (taking the Latest rows away and putting them back), and the final publish replaced `_data` unconditionally. Both fixed. |
| Chapter nav only while the controls are hidden | `8a5133e4` — [iw] "leave those keys unbound in the HUD to prevent accidental closing of media player and allow users to bind it to whatever they want in the config". `mbtn_back`/`mbtn_forward` are their own key-binding section now, which a summoned HUD leaves disabled. |
| Downloaded shows render as squares, not posters | `8a946e39` — the synthesized offline Series DTO carried no `PrimaryImageAspectRatio`, and `auto_geom`'s no-ratio fallback is square. |
| Skip Intro does nothing on downloaded files | `3983a296` — `OfflineVideo.get_intro` was a stub. Segments are cached beside the media at download time now, like the trickplay tiles. **Existing downloads need re-downloading to pick them up.** |

Not changed, decided:

- **Scrim default** — [iw] "keeping the default the same as it was makes
  sense" means the `default` *option* stays the default. `SCRIM_FRAC` keeps
  the 0.42/300 this branch lowered it to.
- **Volume wheel** does not change volume while the HUD is up: the renderer
  owns the wheel there. Not a regression, and not what the mute/volume
  observers were about — those are about the display tracking mpv, which it
  does.
- **No toast on a network drop** — [iw] "could have missed it or not let the
  requests actually time out (it was an ifdown type drop)". An `ifdown` drop
  hangs the request rather than failing it, so there is nothing to report
  until the socket times out. Left as is.

### 0b. Comments as reported

- The half height option works fine on the lower half, on the upper player half it needs to be taller because the text becomes unreadable. I think that keeping the default the same as it was makes sense.

- Skip intro seems to not be showing skip intro buttons in some cases

- Got an error:
  File "/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/mpvtk_browser/gateway/playback.py", line 40, in on_minimize
    playerManager.enable_osc(settings.enable_osc)
                             ^^^^^^^^^^^^^^^^^^^

### 1. The scrub preview against a real server (`renderer.lua`)

All of it is new, and the tests drive a *fake* mpv with a synthetic tile file.

- [*] **With trickplay**: hover along the bar, drag the thumb, and arrow-scrub
      with the keyboard. All three raise the bubble and it tracks without lag.
      Check the frame is the right one near both ends of the bar.
      --> Works great except in one edge case: when I release the mouse the trickplay dialog snaps back to where it was before I started dragging.
- [X] **Without trickplay** — this is #612's case, and the reporter had no
      tiles. The bubble stays centred on the pointer whether the chapter name
      under it is two words or thirty characters. That is the whole bug.
      - [ ] On Windows specifically (need to test)
- [X] **Chapter-image fallback**: a server with no BIF data but chapter
      images. This path never worked in the mpvtk HUD at all before, so it is
      new behaviour rather than preserved behaviour.
- [X] **Video change**: scrub, stop, play something else, scrub again. The old
      tile file is unlinked immediately after `shim-trickplay-clear`; a
      renderer still pointing at it shows as mpv errors in the log or a stale
      frame.
- [X] **UI scale != 1** (1.75 is the reporter's, and Izzie's): the frame draws
      at native pixel size while the text scales, so the box proportions move.
      It should still look deliberate.
- [X] Both backends (`mpv_ext` on and off).

### 2. Library scrolling at real size and real latency (#617)

The tests use a synchronous pool, so pages arrive instantly and in order.
Neither is true against a real server.

- [X] **Drag the scrollbar hard** down a large library. The thumb stays under
      the cursor; the scroller does not resize. Release and check the right
      items are on screen.
- [X] **Jump to the middle** of a big library — you get *that* neighbourhood,
      not a walk from the top.
- [X] **Fast repeated drags.** The one to watch. Every new window asks the
      thumbnail workers for two or three screens of artwork, and parts of the
      library are now reachable without loading everything in between. Browser
      stutter or a complaining server is this.
- [X] **A window that fails**: kill the network mid-scroll. One toast and
      blank tiles — *not* a request per frame. Scrolling again retries.
      - Works fine, it just stops rendering items until the network comes back and I scroll again. Did not see a toast but could have missed it or not let the requests actually time out (it was an ifdown type drop).
- [X] **Random sort** does not window at all, and still shows only what it
      loaded.
- [X] **List view** on a big library, same drags.
- [X] **Paginated mode** on and off over the same library. Untouched by this,
      but the toggle shares route state with it.
- [X] **Back-nav**: scroll deep, open something, come back — the parked offset
      lands where you left.
- [*] **Offline / downloaded library.** The offline source takes the same
      windowed fetches and has not been exercised by hand.
      - Only strange thing is downloaded TV shows render as discs with posters in them, and not posters. Works fine to the extent that I can test it, downloading an entire window of files is impractical

One shared line to know about: `draw_image` gained `+ (node.base or 0)` to its
overlay offset. It defaults to zero so it is inert, but *every* tile strip in
the browser goes through it. Artwork offset or torn anywhere is that line.

### 3. HUD auto-hide (#620 follow-up)

Behavioural, and nearly untestable without a real pointer.

- [X] Summon with the **keyboard** while the mouse sits in the top or bottom
      band. The controls hide. (Before the fix they never did.)
- [X] Then **move the mouse onto the bar** — they stay up while it rests.
- [X] Move the pointer **off the mpv window** while the bar is up. It hides.
- [X] All three `hud_autohide` modes, plus `hud_hide_secs: 0`.

### 4. Config migration (`CONFIG_VERSION = 2`)

Back up `conf.json` first.

- [X] The four old `skip_intro_*` / `skip_credits_*` booleans land on the five
      `segment_*` tri-states as expected (`always` wins over `enable`).
- [X] A *fresh* config still gets `ask` / `ask` / `off` / `off` / `off`.
- [X] `enable_osc` is orphaned: someone who had it off gets controls back and
      must pick "No controls" again. That was the decision (#615), and it is
      the one thing here that will surprise an upgrader.

### 5. The smaller ones

- [*] **Cover Size** live from the View dialog, in both directions. Parked
      scroll offsets are dropped on change — check nothing lands somewhere you
      never scrolled to.
      - It works, but I have to leave the page and come back. It doesn't require a client restart.
- [X] **Mute and volume** are instant in the HUD (#618a), including via mpv's
      own `m` and the wheel.
      - Caveat: wheel doesn't work, but I didn't expect that. Mute updates immediately.
- [*] **Mouse back/forward chapter nav**: setting on, off, and on a video with
      no chapters (does nothing, by design). SyncPlay follows the seek.
      - Only works while player controls are hidden.
- [*] **Skip Intro/Credits** in each of the five segment settings, and
      specifically under `osc_style: none`, where the OSD prompt is the only
      surface.
      - Doesn't seem to do anything in mosts cases. I don't see a skip intro button.
- [X] **Continue Watching** updates after finishing something on another
      client, and the 3s debounce does not cause a refresh storm.
      - One thing with this: If nothing actually updated we shouldn't redraw the row.
- [X] **Play All / Shuffle** queue 300 rather than 200.
- [X] **Show Year with titles off** — the one-line caption sits where a
      caption sits.

### Least confident

The thumbnail-request pressure from free scrollbar dragging (2) and the
chapter-image trickplay fallback (1). Neither has a real-data test, and both
only misbehave at scale. If there is time for two things, those.
--> It works wonderfully. Am very pleased.