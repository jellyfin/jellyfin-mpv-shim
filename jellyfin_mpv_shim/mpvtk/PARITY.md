# mpvtk parity inventory — what the full Tk browser needs

Final spike deliverable: a grounded gap analysis between what
`library_browser/` actually uses and what mpvtk provides. Compiled from
the real widget/paradigm usage in `app.py` / `views.py` / `widgets.py`,
not from tkinter's API surface.

## What the Tk browser is made of

- **18 views**: Home, Grid, Series, Season, Playlist, PlaylistEdit,
  Search, Detail, Connecting, Login, Locked, Settings, MusicLibrary,
  AlbumDetail, ArtistDetail, MusicGenre, Queue + panels (Settings /
  Downloads / Servers / Logs as Notebook tabs).
- **6 modal dialogs**: Pin, PinSetup, ClosePreference, AddTo, SyncPlay,
  Download.
- **Chrome**: nav bar with back stack (`navigate`/`go_back`), now-playing
  bar (album art, seek + volume Scales with scrub-drag, transport
  buttons), update banner, context menus on tiles (`tk.Menu`).
- **Interaction**: mouse-first; wheel scrolling (`bind_all`), Return to
  submit forms, modal `grab_set`, `after()` timers for periodic refresh
  (downloads, logs, now-playing). No drag-and-drop anywhere — playlist
  reorder is Top/Up/Down/Bottom buttons on selected table rows.

## Status legend

✅ proven in the spike ・ 🧩 composite of existing nodes (mechanical) ・
🔨 new renderer/framework capability ・ ⛔ accepted loss

## Components

| Tk usage | mpvtk answer | Status | Notes |
|---|---|---|---|
| Frame/pack layout (79/334 uses) | Box/Row/Column/Spacer, flex, stretch | ✅ | |
| Label (106) | Text (measured metrics, ellipsis) | ✅ | |
| Button (66), NavButton | Button | ✅ | |
| Poster tiles / MediaTile grid+rows | ImageMap strips (baked badges, progress, captions) | ✅ | z-order + overlay budget solved |
| ScrollableGrid / VScrollFrame / HScrollRow | V/HScroll + scrollbar + windowed infinite scroll | ✅ | throttled scroll events |
| Combobox (12, all readonly pickers) | Dropdown | ✅ | |
| Entry (9) | TextBox | ✅ | editing, paste, password `mask`, selection (shift+arrows, ctrl+a/c, replace-on-type); IME ⛔ on X11; click-drag select not built |
| Checkbutton (9) | Checkbox sugar | ✅ | demo Widgets page |
| Notebook (Settings tabs) | button row + view switch in build() | ✅ | demo tab bar |
| Treeview (PlaylistEdit, Queue track tables) | table composite: header + row Texts + selection + reorder buttons | ✅ | demo track table |
| Listbox (AddTo) | VScroll of Buttons | ✅ | same pattern as table rows |
| Progressbar determinate (Downloads, move) | nested-Box bar | ✅ | demo, driven by background thread |
| Progressbar indeterminate (Connecting) | Busy spinner node | ✅ | renderer-side animation timer |
| Scale ×2 (seek/volume scrub-drag) | Slider (drag + throttled change) | ✅ | |
| Text (Logs, readonly) | VScroll of Text lines | ✅ | demo Logs page (300 lines) |
| tk.Menu (tile context menu) | Menu | ✅ | |
| Toplevel + grab_set (6 dialogs) | Dialog: centered top layer, input grab, ESC/click-away dismiss | ✅ | no backdrop dim (z-order) |
| messagebox (3) | Dialog composite | ✅ | |
| filedialog (1, download dir) | path TextBox | ⛔ | accepted |
| PhotoImage (logo, album art) | Image | ✅ | |
| Update banner / toasts | Float + Python timer | ✅ | demo auto-dismissing toast |

## Paradigms

| Paradigm | mpvtk answer | Status |
|---|---|---|
| View routing + back stack | Python state; build() switches on route (demo proves the shape) | ✅ |
| Fingerprint-diffed refresh (Tk perf hack) | unnecessary: full scene re-push + renderer-local state | ✅ |
| `after()` timers (downloads/logs/now-playing refresh) | Python timers + thread-safe `invalidate()` (wakes the loop) | ✅ |
| Background workers (API pool, thumbnails) → UI updates | worker mutates state → `invalidate()`; posters recomposite strips under new content keys | ✅ pattern, needs `thumbnails.py` integration |
| Now-playing bar | composite (Image + Slider + Buttons) + ~1Hz position pushes (ASS-only deltas — cheap with sticky slots) | 🧩 |
| Modal input grab | dialog layer swallows hit-tests below; ESC bound while open (Menu already does both in miniature) | 🔨 with dialogs |
| Dimmed modal backdrop | **z-order constraint**: translucent ASS cannot dim bitmaps (overlays render above ASS). Options: no dim (fine), hide covered images via occluder, or recomposite dimmed strips. Recommend: no dim | ⚠ design choice |
| Focus / Tab traversal | not in Tk UI either (mouse-first); **spatial keyboard/remote nav** is new scope beyond parity — focusable nodes + arrow-key navigation from node geometry + focus ring | 🔨 M–L, optional but the media-center payoff |
| i18n | strings flow through scene JSON; libass renders unicode w/ font fallback. CJK display ✅, CJK input ⛔ (X11) | ✅/⛔ |
| Multi-window (Tk Toplevels) | single surface + floating layers; paradigm shift, no gap | ✅ |
| Window/session lifecycle | quit events wired both backends | ✅ |

## Build order — status

1. ~~Floating-layer generalization + modal dialogs~~ **done** (Dialog,
   Float, top-layer render pass, input grab, ESC/click-away).
2. ~~Slider + checkbox + tabs + progress + table composites~~ **done**
   (demo Widgets page proves each).
3. ~~TextBox password mask + selection/copy~~ **done** (click-drag
   selection excluded).
4. **Real-data integration**: repository.py/thumbnails.py feeding
   strips (poster decode on the existing worker pool; strips
   recomposite as thumbnails arrive). ← next
5. **Keyboard/remote spatial navigation** (net-new capability; the
   reason to render in mpv at all for the 10-foot case).

Every framework-level PARITY item is now built and exercised by the
demo's test pages (35 selftest checks, both backends); what remains is
app integration (4) and the optional 10-foot input model (5).

## Open issues

- Intermittent ~1s scroll stall at very fast wheel rates. Diagnosed via
  HUD to a failing hit-test (`tgt:- off:-1`) while renders continue —
  since the Lua side is single-threaded and geometry was fresh, the bad
  input is the mouse coordinates (mouse-pos likely goes unreliable
  during trackball button-scrolling). Mitigated by wheel gesture
  stickiness: a gesture keeps scrolling its last target for up to 2s
  when the raw hit-test fails (HUD shows `tgt:<id>*` when engaged).
  Root-cause confirmation: read the `mouse:x,y` HUD field during a
  stall.
- Non-ASCII glyph metrics (measured table covers printable ASCII).
