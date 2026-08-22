# Settings: curation and how a change takes effect

`docs/configuration.md` is the **user-facing reference** for every setting, and
`tests/test_docs_coverage.py` fails a key that has no entry there. This file is
the other half: the maintainer's view — why a setting sits on the tab it sits on,
and, more usefully, **whether changing it does anything before a restart**.

Sources: `jellyfin_mpv_shim/conf.py` (the declarations),
`jellyfin_mpv_shim/mpvtk_browser/config.py` (the tab layout and the help text),
`jellyfin_mpv_shim/mpvtk_browser/settings/base.py` (the live-apply cascade).

## 1. Adding a key

`conf.py:Settings` declares every key as a typed class attribute; defaults live
there. **The type annotation must be one of the entries in
`settings_base.object_types`** (`bool`, `int`, `str`, `float`, `list`, or the
`Optional[...]` forms of the first four) — anything else `KeyError`s at load time.

Then: an entry in `docs/configuration.md` (enforced), and a row in
`config.py`'s tab table if it should be reachable from the UI. A key that is
runtime state the app rewrites, or migration bookkeeping, goes in
`test_docs_coverage.py`'s `INTERNAL` list **with a reason**, rather than getting
a hollow doc entry.

## 2. Which tab, and why it matters

The tabs are curation, not taxonomy. The Settings screen was one General page
that had grown to eleven groups and about a hundred controls; the split exists so
a control can be found, not because the groups are conceptually clean.

The consequence worth knowing: **a setting's tab is part of how discoverable it
is**, so moving one is a user-visible change even though nothing about its
behaviour altered.

Two conventions:

- **Dependent settings are hidden, not disabled**, when their parent is off —
  and turning the parent off *says so* if it also had to clear a dependant
  (`TRAY_DEPENDENT` → `start_minimized`). Leaving a hidden setting acting at
  every startup with no way to see or undo it is the failure being avoided.
- **A control that is not offered beats a control that is disabled** where the
  reason is a permission or a missing capability, because a disabled control
  invites the user to go looking for the switch that enables it.

## 2.5 Search, and what it means for how you write a note

The form is about a hundred controls over three tabs, so there is a search box
on each of them (`config.search`, drawn by `settings/__init__._search_box_row`).
Results are drawn **across all three tabs** and are editable in place; picking a
tab clears the query, which is the way out.

Two consequences for anyone adding a setting:

- **The note is part of the search corpus, and usually the useful part.** A
  label is two or three words chosen before anyone knew what people would call
  the thing. "banding", "buffering", "judder", "stutter", "controller" and
  "tray" are all words users type and none of them was ever a label.
  `tests/test_settings_search.py` holds those cases as cases, because a query
  that finds nothing is invisible from the code.
- **Matching is substring and therefore directional.** A query word must appear
  *in* the haystack: a note saying "buffering" is found by `buffer`, but a note
  saying only "buffer" is **not** found by `buffering`. So write the longer,
  more colloquial form into the note — `network_buffer`'s says "stopping for
  buffering" for exactly this reason.

Search is built on `sections()` rather than on the schema, so a control the form
is currently hiding — the passthrough toggles the audio mode cannot carry,
whichever of `close_to_tray`/`allow_background` does not apply — cannot be found
either. A result that leads to a control the form then refuses to draw would be
worse than no result. Advanced groups *are* searched, and returned without the
disclosure: somebody who typed a query has already narrowed it, and hiding half
the answers behind a checkbox that is not on screen would make the search
quietly incomplete.

## 3. Does it take effect now, or at the next start?

This is the table a future "restart to apply?" prompt would be built from. The
governing rule is: **a control whose whole purpose is "try this and see" must
apply live, or it reads as broken** — the user cannot tell whether the value they
picked was the wrong one or whether the control does nothing.

### Applies live

| setting(s) | how |
|---|---|
| `work_offline` | `_apply_work_offline` |
| `auto_download_enable` | seeds the server list on first enable |
| `audio_*` | `_apply_audio_settings` |
| `scroll_wheel_pixels`, `scroll_mode` | `app.push_scroll_config()` — the renderer re-derives |
| `gamepad_swap_confirm` | `app.push_gamepad()` — the renderer rebinds |
| `poster_scale` (Cover Size) | `apply_cover_size()` |
| `ui_text_scale`, `ui_text_min` | toolkit type scale **and** `apply_cover_size()` |
| `logo_legibility_*` | `apply_logo_legibility()` → `StripStore.retag()` |
| `theme` | colours only — see below |
| `reader_font_size`, `reader_theme`, `reader_justify` | re-read every frame by the reader |
| `comic_fit` | read per call |

### Applies to the next thing you play

Neither live nor restart, and worth its own row because "restart to apply" would
be wrong in the direction that makes users restart for nothing.

| setting(s) | how |
|---|---|
| `hwdec` | `_play_media` writes it per item |
| `deinterlace_auto` | `_apply_deinterlace`, per item |
| `motion_interpolation`, `deband`, `tone_mapping`, `render_quality` | `_apply_render_presets`, per item |
| `network_buffer` | per item, and it could not be sooner — the demuxer reads those options when it opens a file |

### Needs a restart

`config.RESTART_REQUIRED` is this list as data, and `settings/base.py`'s
`_set_setting` raises the restart banner when a key in it is written to a value
that differs from the one it had (compared *after* coercion, so re-submitting an
unchanged text field is not a change). The banner names the settings, offers
**Restart Now** where `restart.supported()` says the launch can be
reconstructed, and **Later** always.

Three rules for editing that set:

- **Under-listing is the safe direction.** A missing key costs a banner nobody
  sees; a key wrongly in the set asks somebody to restart for a change that had
  already taken effect, which teaches them to ignore the banner.
- **It is not the complement of the live-apply list.** The "next thing you
  play" group above is neither, and a restart banner for `deband` or `hwdec`
  would be asking for a restart that is not needed.
- **A setting whose whole subject is startup is not pending.** `start_minimized`
  and the tray pair have already taken full effect the moment they are saved;
  there is nothing waiting on a restart.

The restart itself is `restart.py`, and the one thing to know about it is
*where* it happens: at the very end of `mpv_shim.main`'s `finally`, after
`single.release()`. Started any earlier, the new copy finds the instance lock
still held, hands off to the process that is exiting, and quits — so the restart
looks like a plain quit. `tests/test_restart.py` pins that ordering, and pins
the argv rebuild, which is an allowlist so that `--password` from a one-off
`--server` login can never reappear on a launch the user did not type.

| setting(s) | why |
|---|---|
| `ui_scale` | the whole interface geometry is derived once |
| `osc_style` | picks which OSC mpv is *constructed* with |
| `input_gamepad` | mpv reads it once, at startup |
| `theme` — the **size** half | `poster_scale`/`tile_landscape` feed sizes a live rebuild would have to rediscover through every cached row; see `docs/browser-shell.md` §9 |
| shader pack directory | read once at startup |
| several mpv options | mpv reads them at construction — see `docs/mpv-backends.md` |

### Neither, and it is not symmetric: `trickplay_fast_mode`

Read by the TrickPlay worker each time it builds a window, so the two
directions land differently and the honest phrasing is per-direction:

- **On** applies to the **current** video, at the next scrub the loaded window
  does not cover — `_covers` says no, `_window_for` returns the whole video,
  and the rest of the film is fetched then. Not at the moment you flip it, but
  not next video either.
- **Off** applies to the **next** video. The whole-video window already covers
  every position, so `_covers` never says no again and no further fetch is
  ever issued for the current item.

Worth stating because the obvious summary — "takes effect on the next video" —
is wrong in the direction a user is more likely to flip it, and wrong towards
*more* work than they expect rather than less. See `docs/artwork-pipeline.md`
§11.

### The two that look inconsistent and are not

**`theme` splits.** Colours apply immediately; sizes do not. A theme change is
not a request to resize, so re-deriving tile geometry would change the cover size
under the pointer as a side effect of picking a palette.

**`poster_scale` does not split**, and that is the same argument run the other
way: the control is *labelled* Cover Size, so watching it happen is the entire
point. It sat behind a restart once and nobody could tell what the values meant
(#616).

### The trap when adding a live-apply

`force_scroll_snapping` — one of the two settings `scroll_mode` replaced — was
never listed in the cascade, so it took a restart **silently**. For a setting
whose whole purpose is "try this if scrolling feels wrong", that meant trying it
and seeing nothing happen. If a new key belongs in the live-apply list, adding it
there is not an optimisation; it is the difference between a working control and
one that reads as dead.

Anything baked into a **composited bitmap** (tile captions, logo plates, theme
colours) needs `StripStore.retag()` rather than a repaint, or the rows on screen
do not change until they age out of the LRU. `retag`, never `clear` — see
`docs/artwork-pipeline.md` §1.

## 4. Help text is translated, and two things about it bite

- **`xgettext: no-python-format`** is required on any help string containing a
  literal percent followed by a letter. `"100% on"` parses as a `% o` conversion
  and `msgfmt --check` then *rejects the whole catalogue* for the languages that
  translated it; zh_Hans has already tripped over this.
- **A `_p(context, …)` discards every existing translation of the string it is
  added to**, so a context goes on the label, not on a button verb shared with
  the rest of the app. See `docs/i18n.md` §6.
