# mpvtk — declarative UI rendered inside the mpv window

Spike exploring rendering the library-browser-class UI directly in the
mpv window, inverting the usual "embed mpv in a toolkit" architecture:
mpv owns the window and swapchain; the UI is OSD data composited in
mpv's own render pass. The video path is untouched (native VO, direct
scanout, HDR passthrough all intact).

## Architecture

```
Python                                 mpv process (either backend)
──────                                 ────────────────────────────
widgets.py   declarative element tree
layout.py    → flat scene (abs coords)
app.py       → JSON over script-message ──►  renderer.lua
                                             draws ASS (osd-overlay)
             semantic events            ◄──  + BGRA files (overlay-add)
             (click/change/select)           owns hover, scroll offsets,
             → rebuild tree, push scene      text editing, popups locally
```

- **No per-frame IPC.** The Lua renderer handles hover, wheel/drag
  scrolling, cursor blink, and dropdown popups locally. Python is only
  involved when a semantic event fires, and responds by pushing a whole
  new scene (~tens of KB of JSON; full replace, no diffing).
- **Renderer-local state survives scene pushes**, keyed by node id:
  scroll offsets, textbox text/cursor/focus, dropdown selection. A node
  with `force=true` resets its state from the scene. Stateful widgets
  need explicit, unique ids (layout warns on duplicates).
- **Images** are pre-rasterized once to raw premultiplied-BGRA files
  (`rawimage.write_bgra`) at display size, in a plain temp dir — no
  tmpfs assumption; mpv re-reads them through the OS page cache when
  the renderer re-issues `overlay-add` during scrolling. Partial
  visibility at viewport edges is handled by cropping the source via
  offset/stride math, not by moving pixels.
- **Both backends**: the renderer runs inside mpv, so python-mpv-jsonipc
  and libmpv are identical (`--backend` in the demo; 13/13 selftest
  checks pass on both).

## Spike findings

1. **overlay-add bitmaps composite ABOVE all script ASS** (verified on
   mpv 0.41; the thumbfast "hole punch" comment suggests the opposite —
   its hole serves translucency, not visibility). Consequences:
   - ASS cannot draw on top of an image. Captions/labels must be laid
     out beside images, not over them.
   - Chrome that must cover images (dropdown popups; later: dialogs)
     is an *occluder*: the renderer subtracts its rect from every
     image's visible region, emitting up to 4 sub-overlays per image.
   - Hover rings draw just outside image bounds.
2. **Text metrics are approximated** (per-char width table, duplicated
   in layout.py and renderer.lua). Fine for sizing boxes and ellipsis;
   cursor positioning drifts slightly on unusual glyphs. A follow-up
   could ship a real width table for the chosen font (uosc does this).
3. **Text input** enumerates printable ASCII as forced key bindings
   while a textbox is focused; Ctrl+V pastes via the `clipboard/text`
   property (mpv ≥ 0.40). IME (CJK) input is not available — the
   long-term path is mpv's `mp.input` / Wayland text-input integration.
4. **Overlay budget** is 63 ids. Off-viewport images cost nothing;
   ~50 simultaneously visible posters is comfortable. The renderer
   warns and drops beyond the budget.
5. **Screenshots need a video frame**: `screenshot-to-file` fails on a
   pure OSD window (selftest falls back to X11 capture).
6. Wheel events walk up the scroll chain by axis: vertical wheel over a
   tile row scrolls the page; horizontal (or shift+) wheel scrolls the
   row.

## Demo / selftest

```sh
python3 -m jellyfin_mpv_shim.mpvtk                 # interactive (jsonipc)
python3 -m jellyfin_mpv_shim.mpvtk --backend libmpv
xvfb-run -a python3 -m jellyfin_mpv_shim.mpvtk --selftest /tmp/shots
```

The selftest drives the renderer's `mpvtk-debug` hooks (hover/click by
node id, wheel, typing, popup selection), screenshots each step, and
asserts on renderer state and Python-side model updates.

Unit tests for the layout engine: `python3 -m unittest tests.test_mpvtk_layout`.

## Not yet built (deliberately out of scope for the spike)

- Modal dialogs (the popup-occluder mechanism generalizes to them).
- Focus traversal (Tab), keyboard/remote navigation of tiles.
- Momentum/animated scrolling (ASS `\t` or renderer timers).
- Scene diffing (full-replace is fine at this scale).
- Image scaling in the renderer (pre-scale with Pillow instead).
- IME text input; native file chooser (keep Tk for that one settings
  page or use a path textbox).
