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

| # | Title | Size |
|---|---|---|
| [612](#612--hover-bubble-position-depends-on-chapter-title-length) | Hover bubble position depends on chapter-title length | S (or folded into 618) |
| [613](#613--hiding-titles-also-hides-the-year) | Hiding titles also hides the year | S + groundwork |
| [614](#614--mouse-backforward-buttons-seek-chapters) | Mouse back/forward → chapter seek | S |
| [615](#615--enable-osc-does-nothing-under-the-jellyfin-ui) | "Enable OSC" does nothing under the Jellyfin UI | S |
| [616](#616--cover-size-in-the-library-view-dialog) | Cover Size in the library View dialog | M |
| [617](#617--scrollbar-loses-the-drag-while-items-page-in) | Scrollbar loses the drag while items page in | L |
| [618](#618--controls-slow-to-update-visually) | Controls slow to update visually | S + L |
| [620](#620--playback-hud-design-feedback) | Playback HUD design feedback | M |
| [575](#575--commercial--preview--recap-segments) | Commercial / Preview / Recap segments | M |
| [560](#560--continue-watching-doesnt-update) | Continue Watching doesn't update | M |

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

## Order of work

Groundwork first, then the small fixes, then the two structural changes. One
commit each.

1. This document.
2. Groundwork — mpvtk disabled state.
3. #618(a) — observe `mute` / `volume`.
4. #613 — year-only captions.
5. #614 — mouse chapter navigation.
6. #615 — `osc_style: none`.
7. #616 — live cover size.
8. #560 — `UserDataChanged` + refresh on return.
9. #620 — HUD scrim / auto-hide / subtitle-margin settings (+ renderer text
   shadow).
10. #575 — media segment types.
11. #618(b) + #612 — renderer-side scrub preview.
12. #617 — true virtual scroll.

## Decisions taken without asking

Small enough to reverse, recorded so they are not mistaken for oversights.

- **#575 defaults:** Commercial / Preview / Recap default to `off`. Intro and
  Credits default to `ask` [iw]. jellyfin-web ships the new three off too.
- **#614 with no chapters:** the binding does nothing. Falling back to
  ±10s/+30s would be a different feature wearing the same setting's name.
- **#613 greying:** none, for the reason recorded under that issue — the
  premise for it turned out not to hold.
