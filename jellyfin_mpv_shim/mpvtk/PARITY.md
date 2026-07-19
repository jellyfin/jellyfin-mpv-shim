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
| Entry (9) | TextBox | ✅/🔨 | basic editing + paste done; **password mask** (login/PIN) S; **selection+copy** M (~150 lines); IME ⛔ on X11 |
| Checkbutton (9) | rect + check glyph + click | 🧩 S | settings toggles |
| Notebook (Settings tabs) | button row + view switch in build() | 🧩 S | |
| Treeview (PlaylistEdit, Queue track tables) | table composite: header buttons + row Texts + selection highlight state | 🧩 M | single-select + button reorder — no DnD needed |
| Listbox (AddTo) | VScroll of Buttons | 🧩 S | |
| Progressbar determinate (Downloads, move) | two rects | 🧩 S | |
| Progressbar indeterminate (Connecting) | ASS `\t` animation node | 🔨 S | runs in libass, no per-frame pushes |
| Scale ×2 (seek/volume scrub-drag) | Slider: track+thumb, drag like scrollbar thumb | 🔨 S–M | reuse scrollbar drag machinery |
| Text (Logs, readonly) | VScroll of Text lines | 🧩 S | no editing needed |
| tk.Menu (tile context menu) | Menu | ✅ | |
| Toplevel + grab_set (6 dialogs) | **modal dialog layer**: floating Box + hit-test grab + ESC/Enter | 🔨 M | generalize the Menu floating-layer; backdrop dim is z-order-limited (see paradigms) |
| messagebox (3) | modal dialog composite | 🧩 | after dialogs exist |
| filedialog (1, download dir) | path TextBox | ⛔ | accepted |
| PhotoImage (logo, album art) | Image | ✅ | |
| Update banner / toasts | floating Box layer + Python timer | 🧩 S | needs floating-layer generalization |

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

## Suggested build order

1. **Floating-layer generalization + modal dialogs** (unlocks 6 dialogs,
   messageboxes, toasts/banner) — extends proven Menu machinery.
2. **Slider + checkbox + tabs + progress + table composites** — all
   mechanical, parallelizable.
3. **TextBox password mask, then selection/copy.**
4. **Real-data integration**: repository.py/thumbnails.py feeding strips
   (poster decode on the existing worker pool; strips recomposite as
   thumbnails arrive).
5. **Keyboard/remote spatial navigation** (net-new capability; the
   reason to render in mpv at all for the 10-foot case).

Rough total for parity (1–4): on the order of the spike's size again —
each item is bounded and none requires a new rendering primitive; the
hard unknowns (z-order, overlay budget, crop math, input, scroll,
windowing, metrics) were all retired during the spike rounds.

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
