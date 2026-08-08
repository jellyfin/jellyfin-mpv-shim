# UI fixes, batch 4 — work list (branch `ui-fixes-4`)

Ten items from the post-3.0.0pre10 wave, investigated against the tree at
`b6cff304`. This file is the plan of record: the diagnosis, the decision
taken on each, and the shape of the fix. Decisions marked **[iw]** came from
Izzie on review of the investigation; everything else is the investigation's
own conclusion.

Two of the eleven originally listed were **fact checks** rather than work, and
both are answered here with the measurement (#8, #9). One was investigated and
**dropped** (#3) — its section is kept, because the reason it was dropped is a
fact about the input layer that the next person to want it will need.

Commit policy for the branch: one commit per item, with the shared media-info
formatter landing as its own commit before the two items that need it.

| # | Title | State |
|---|---|---|
| — | [Groundwork: a shared media-info formatter](#groundwork-a-shared-media-info-formatter) | done `ac2cea8e` |
| [1](#1--mpv-default-mouse-modality-669) | mpv default mouse modality (#669) | done, needs hand-testing |
| [2](#2--the-reader-should-dismiss-the-downloading-toast) | Reader dismisses the downloading toast | todo |
| [12](#12--a-hardware-decoding-setting) | A hardware-decoding setting | done |
| [13](#13--shader-packs-must-not-reach-stills) | Shader packs must not reach stills | done |
| [15](#15--per-library--per-series-shader-profiles) | Per-library / per-series shader profiles | todo |
| [14](#14--search-asks-for-no-fields) | Search asks for no fields | todo |
| [3](#3--dropped-zoom-and-drag-for-photos) | ~~Zoom/drag for photos~~ | **dropped [iw]** |
| [4](#4--delete-from-disk) | Delete from Disk | done `798edee0` |
| [5](#5--lookahead-hysteresis-661) | Lookahead hysteresis (#661) | todo |
| [6](#6--previous-item-from-next-up-650) | Previous item from Next Up (#650) | done |
| [7](#7--posters-and-thumbnails-on-video-pages) | Posters/thumbnails on video pages | todo |
| [8](#8--fact-check-exif-orientation) | Fact check: EXIF orientation | **answered — no work** |
| [9](#9--fact-check-does-playstate-reach-the-local-catalog) | Fact check: playstate → local catalog | **answered — work needed** |
| [10](#10--playback-info-that-matches-jellyfin-webs) | Playback info matching jellyfin-web | done `faf129fd` |
| [11](#11--media-info-in-the-context-menu) | Media info in the context menu | done `a6d5c7b4` |

---

## Groundwork: a shared media-info formatter

#10 and #11 are the same knowledge shown twice — one over a playing video, one
over an item you have not started — and `pages/detail.py:_media_info_line`
(line 368) is a third copy of a slice of it that already exists. Three
independent renderings of "what is this file" will drift, and the way they
drift is invisible: each one is right on the item you tested it against.

So: a `mpvtk_browser/components/media_info.py` holding the pure formatting —
stream → labelled attribute rows, container/size/bitrate, the play-method
name — with no widgets in it and no server access, the same split as
`home_sections.py` (pure logic) versus `LibrarySource` (I/O). `_media_info_line`
becomes a caller.

Pure because that is what makes it testable: the interesting cases here are
DTO shapes (a stream with no `Codec`, a Dolby Vision profile, an external
subtitle, a `MediaSource` with no `Size`), and every one of them is a
dictionary, not a screen.

---

## 1 — mpv default mouse modality (#669)

> Can we get an option to reverse the play and pause mouse clicks to mimic the
> default mpv behavior?

**[iw]** "Make the current behaviour the default, also add fullscreen
doubleclick as it doesn't conflict to the default, add a config switch for
swapping click to play/pause (default) to the regular MPV modality."

### Diagnosis

`renderer.lua:5303`, in `phud_bind_summon` — the state where the playback HUD
is *hidden*:

```lua
mp.add_forced_key_binding('mbtn_left', 'mpvtk_phud_click', function()
    ... else mp.commandv('cycle', 'pause') end
end)
```

That one forced binding is the whole cause. A forced `mbtn_left` binding
consumes the click, and mpv will not drag its window with a consumed click.

The renderer already knows how to do this correctly and does it everywhere
else: `state.vodrag` is `'allow-vo-dragging'` when the mpv build supports it
(4636), `ui_resume` turns `input-builtin-dragging` off and enables our sections
*with* that flag (5065-5069), and `ui_suspend` hands built-in dragging back
(5116) — "nothing of ours is on screen now, and dragging the video to move the
window is what mpv does everywhere else", as the comment there says. The
hidden-HUD click is the one place that skipped it.

The other two defaults are not overridden at all while the HUD is hidden:
`mbtn_left_dbl` and `mbtn_right` are bound only inside the `mpvtk_mouse`
section (4565, 4568), which `ui_suspend` disables. The config dir's
`input.conf` is empty and `input_default_bindings=True` (`player.py:711`), so
mpv's own `MBTN_LEFT_DBL cycle fullscreen` and `MBTN_RIGHT cycle pause` are
already reaching us there.

### Shape

- New setting, `mpv_mouse_bindings: bool = False` (name to settle) — off is
  today's behaviour, on is mpv's modality. Advanced or Playback tab; it is a
  playback-input preference, so Playback.
- Pushed to the renderer the way `hud_grab_keys` is: this is a renderer-side
  decision (see the disabled-state precedent in `UI_UX_FIXES_2026-08.md`), and
  a round trip per click is not a thing to add to a click handler.
- Off: bind `mpvtk_phud_click` as now, but **with** `allow-vo-dragging` so the
  drag survives. That is the whole of "dragging moves the window" if the flag
  behaves the way `ui_resume` believes it does.
- On: do not bind `mbtn_left` at all while the HUD is hidden. Right-click
  pausing then comes free from mpv.
- Double-click fullscreen in both modes.

### What measurement settled

Driven against a real mpv under Xvfb with `xdotool`, which answered both open
questions and removed half the work.

**A double click delivers `mbtn_left`, `mbtn_left_dbl`, `mbtn_left`.** So with
our click-to-pause bound, the two pause toggles *cancel* and mpv's own
`MBTN_LEFT_DBL cycle fullscreen` still fires — observed as
`pause true -> pause false -> fullscreen true`. **Double-click fullscreen
therefore already worked and needed no code at all**, in either mode. The
plan had it down as something to add.

**And the whole `allow-vo-dragging` question was the wrong one.** A forced
binding on `mbtn_left` is simply what stops the VO dragging with that button,
so the two are mutually exclusive and the setting is "which of them do you
want" rather than "can we have both". Confirmed the other way too: with
nothing bound, left click does nothing, right click pauses, double click
fullscreens — mpv's modality entire, for free.

(`begin-vo-dragging` *is* a command, and `--input-builtin-dragging=no` "does
not disable window dragging initialized with the command" — so a hybrid that
starts a drag on motion-while-pressed and pauses on release-without-motion is
buildable. It is more than #669 asked for and is not in this batch.)

### The half that hand-testing found

**[iw]**: "this mostly works, but critically the HUD breaks the setting. When
the HUD is visible, dragging doesn't work, double click for fullscreen doesn't
work, and it always plays/pauses on single click."

All three, one cause. The setting was honoured in `phud_bind_summon`, which
governs the HUD while it is *hidden*. Once summoned, `ui_resume` enables the
`mpvtk_mouse` section, which owns `mbtn_left`, `mbtn_left_dbl` and
`mbtn_right` — so every one of them reaches the scene handlers instead, and
those handlers paused on any bare-video click, returned early from
`on_dbl`, and ignored a right click with no node under it.

Fixed in all three, at the "no node under the pointer" branch each of them
already had:

* left click → pause, **or** `begin-vo-dragging` in mpv's modality (the same
  command, and the same press-is-the-only-moment reasoning, as the
  client-side title bar directly below it);
* double click → `cycle fullscreen`, in **both** modes, because this is
  mpv's own default and the only reason it stopped was our section taking
  the key. Guarded on the HUD being shown — `mpvtk_mouse` is enabled in
  *browse* mode too, and an unguarded version makes a double-click on empty
  library background toggle full screen;
* right click → `cycle pause`, in mpv's modality only.

Pressing the bar's own *background* drags rather than pausing in that mode,
which is deliberate: it is chrome, it mirrors that press pausing under the
other setting, and the controls themselves are higher nodes that `node_at`
already prefers.

Nothing in the lua suite had ever clicked the **picture** with the controls
up, which is why this reached hand-testing. Six assertions now do.

### Still to hand-test

Dragging itself, which Xvfb cannot answer: it has no window manager, and
window dragging is the VO asking the WM to take over. **[iw]** will test X11;
the Debian VM covers Wayland (see the `gnome-test-vm` note). The two do not
have to agree — this is VO-side.

---

## 2 — The reader should dismiss the downloading toast

> Book viewer on display should dismiss the downloading toast — i.e. comics and
> epubs, once displayed, are done downloading; the toast that said it was
> downloading doesn't need to be displayed.

### Diagnosis

`item_actions.py:678` raises `set_status(_("Downloading %s…") % name)`, and
`app.py:TOAST_SECS = 6.0` is the only thing that ever takes it down. Both
in-window readers know the exact moment the content is on screen —
`pages/reader.py:_open_book`'s `done(doc)` and the comic page's equivalent —
and neither says anything.

On a book already on disk, or on a fast local server, the download finishes
inside those six seconds, so the toast outlives the thing it is reporting and
sits over the first page.

### Shape

Clear the status when the document lands. The one subtlety is not clearing
someone *else's* toast: six seconds is long enough for an unrelated message to
have replaced ours. So `set_status` gains a companion that clears only if the
message currently showing is still the one the caller put up — comparing the
text is enough, and it is what `_arm_toast_clear`'s slot discipline
(`app.py:2578`) is already shaped like.

---

## 3 — Dropped: zoom and drag for photos

**[iw]** "Let's drop zoom in photo mode, it's not worth it for now."

Recorded because the investigation turned up the reason it is not cheap, and
that reason will not be obvious to the next person who looks.

The arithmetic is genuinely reusable: `gateway/picture.py`'s `fit_scale`,
`fit_zoom` and `pan_bounds` take a picture size and a window and know nothing
about comics. `reset_picture_view` already exists and already runs on every
browse→video handoff, which is the per-photo reset.

**What is not reusable is the input path.** `state.vpan_grab` is reached from
the mouse-down handler inside the `mpvtk_mouse` key section, and `ui_suspend`
disables that section for playback (`renderer.lua:5110`). A comic is the
"third window state" — the browser keeps the chrome and the pointer — but a
photo is *ordinary playback*: `media.py:136` makes it a real `Media`, the
window is yielded, and the browser has given the mouse back. So the gestures
would have to be made reachable from the playback-HUD input state, which is new
wiring in the part of the renderer that is hardest to get right, not a reuse of
the comic's.

One fact from that investigation is load-bearing for anyone who does pick this
up, and is filed under #8 below: mpv's `video-params/dw` is the **pre-rotation**
size and `video-out-params/dw` is the post-rotation one. Photo pan maths must
use the second.

---

## 4 — Delete from Disk

> We want to add basic media management support (gated by permissions) such as
> the ability to delete media.

**[iw]** "Gate access the same way Jellyfin web does and use a confirmation
dialog that makes it painfully clear this deletes the actual media files and
not merely removing from Jellyfin. The name could be *Delete from Disk*."

### State

Nothing exists. What does:

- `jellyfin_apiclient_python.api.delete_item` (api.py:1229) — no new
  dependency, no apiclient PR.
- `CanDelete` is a real `ItemFields` value and is already requested in one
  place, `repository.py:1358` (Live TV recordings).
- `gateway/editing.py:85` is the existing shape for a destructive server call.
- `dialogs.py:626` `_confirm` is the existing confirmation dialog.

### The gate

jellyfin-web's rule is per-item and nothing else — `itemContextMenu.js:210`:

```js
if (item.CanDelete && options.deleteItem !== false) {
```

`CanDelete` is computed by the server against the user's policy *and* the
library's delete-from-folder list, which is why it is the right question and
`EnableContentDeletion` alone is not: a user may delete from one library and
not another, and only the server knows which. So: no new `user_policy.py`
helper, ask the DTO. `CanDelete` has to be added to `DETAIL_FIELDS` and to
whichever list queries offer the action, or it arrives absent and every item
looks undeletable.

Absent is the correct fallback here, and it is the opposite of `may_download`'s
fail-open: hiding a delete the user could have made is an inconvenience, and
offering one that 403s at the point of no return is not.

### The confirmation

jellyfin-web's own string is the right content and not quite enough emphasis:

> "Deleting \"{0}\" will delete it from both the file system and your media
> library. Are you sure you wish to continue?"

Ours says the same thing under a heading that has already said it — the menu
entry is **Delete from Disk**, the dialog title is the same, the confirm button
says *Delete from Disk* rather than *OK*. The item's name in the body, because
a context menu can be opened on the wrong tile and the name is the only thing
that catches that.

### Shape

- `repository.py`: `CanDelete` into `DETAIL_FIELDS`, and into the list fields
  used where the menu appears.
- `gateway/editing.py`: `delete_item(server_uuid, item_id)`, raising like its
  playlist sibling — "a failed delete that looks like a success is the bug
  `delete_download` was fixed for".
- `tiles.py` context menu + `detail_components.common_actions`: one entry,
  guarded on `item.get("CanDelete")`.
- After a successful delete: drop any downloaded copy
  (`gateway/downloads.delete_download`) — the local file is now the only copy
  of something the user asked to destroy, and leaving it behind is a surprise
  in the other direction — then leave the page if the deleted item *is* the
  page, and refresh the list if it is not.
- `docs/PERMISSION_GAPS.md` gets §6.

### Not in scope

Metadata editing, identify/refresh, and anything under server management. #11
is the other half of the jellyfin-web gap and is deliberately read-only.

---

## 5 — Lookahead hysteresis (#661)

> Lookahead downloads can cause frequent small transfers, repeatedly spinning
> up HDDs. The current 20-item per-pass queue limit also appears to be
> hardcoded.

**[iw]** "Keep the current setting and make the advanced settings null and
disabled by default, left in advanced so people who want to tune the behaviour
more can."

### State

All of it is in `sync/auto.py`:

- `conf.py:277` `auto_download_lookahead: int = 2` — a flat window.
- `_lookahead()` (auto.py:403) asks the server for `count` episodes from the
  Next Up anchor, per series we hold something for.
- `_MAX_PER_PASS = 20` (auto.py:60) — the hardcoded limit the issue names.

The hard part is already solved and must not be undone. The window is anchored
on **watch progress, never on what is on disk**, and the comment at auto.py:403
explains why at length: anchoring on the furthest episode held is a ratchet
that walks the whole series whether or not anyone watches it. Hysteresis is a
*threshold* change; it must not become an anchor change.

### Shape

Three new keys, all nullable, all null by default:

```python
auto_download_lookahead_min: Optional[int] = None
auto_download_lookahead_max: Optional[int] = None
auto_download_max_per_pass: Optional[int] = None
```

`Optional[int]` is in `settings_base.object_types` (line 41), so this needs no
work in the config layer.

Null means today's behaviour exactly: flat window of `auto_download_lookahead`,
per-pass limit of 20. No migration, no config version bump, and an existing
install that never opens the new settings behaves identically — which is what
"disabled by default" has to mean for a feature whose failure mode is unwanted
disk activity.

With min and max set, per series: count the upcoming episodes from the anchor
that are already **complete, pending or downloading** (`manager.db` has all
three states; `_followed_series` already does one variant of this query); if
that count is at or above min, queue nothing; below min, extend to max.
`_MAX_PER_PASS` becomes `auto_download_max_per_pass or 20` read at the call
site in `fill()` (auto.py:316 and the log line at 331).

Setting one of min/max and not the other is a config a user can type. Treat a
half-configured pair as off and log it, the same way `allowed_servers` handles
"enabled but no servers" (auto.py:363) — "enabled but silently doing nothing is
otherwise indistinguishable from a bug".

### Placement

The auto-download group is curated on the **Downloads** tab
(`config.py:130-135`) while `ADVANCED_TAB = "general"`, so "put it in advanced"
cannot mean the literal Advanced group without moving these settings away from
the ones they qualify. It means: below the existing `auto_download_lookahead`
row, under the tab's own show-advanced disclosure. `settings/general.py:52`
currently keys that disclosure off the group being *titled* "Advanced", so this
needs the disclosure to become a property of a group rather than its name — a
small change in the settings renderer, and the second time it has been wanted.

`test_docs_coverage.py` fails until `docs/configuration.md` has all three.

### Note text

The three rows need one note between them saying what null means, because a
blank numeric field that means "use the simple setting above" is not guessable
from the label. `config.NOTES` (line 375) is where it goes.

---

## 6 — Previous item from Next Up (#650)

> It seems playlist always starts at current item and no backwards movement
> possible.

**[iw]** "jf-web doesn't support this either, but it's easy enough to add: if
the user explicitly clicks prev then do a lookup for the previous item even if
it isn't in the queue."

### Diagnosis

Confirmed exactly. `item_actions.py:147` special-cases episodes:

```python
q = source.get_series_queue(srv, series, start_item_id=iid)
```

`StartItemId` is inclusive, so the queue is *this episode onward*. Next works
because the rest of the series is in it; `Media.has_prev` is `seq > 0`
(media.py:811) and `seq` is 0, so `play_prev` (player.py:2475) returns False
and nothing happens. Nothing auto-extends the queue backwards — the only
`insert_items` callers are SyncPlay, the websocket handler and the browser's
own queue edits.

### Shape

Do not widen the initial queue. Fetching the whole series and setting
`start_index` would change queue length, what is reported as the PlayQueue, and
what SyncPlay sees — for a case that is one keypress on one screen.

Instead `play_prev` gains a fallback when `has_prev` is False: ask the server
for the episode preceding the one playing and splice it in with
`Media.insert_items`, which already exists and already documents the lock-free
publish ordering it needs (media.py:863 — "publish the fully-built queue first,
then flip the flag").

Three declines, all silent no-ops rather than errors:

- offline (no client to ask);
- SyncPlay enabled — `play_prev` already routes to `request_prev` there, and
  inventing a queue entry the group does not have is not ours to do;
- not an Episode, or the server has nothing before it (episode one).

The lookup is one blocking HTTP call on a keypress, so it belongs on the action
thread like the rest of `play_prev`'s work, not on the render loop.

### What it came to

`AdjacentTo` looked like the server-side answer and is not: measured, it
returns the **entire series**, not the neighbours. So the lookup is the
episode list, once, and the queue is **prepended** rather than rebuilt —
the entries ahead already exist, already carry their PlaylistItemIds, and
may have been edited from the queue screen or by a websocket Play command,
all of which reconstructing from the server's listing would discard.

`Media.replace_queue` turned out to be exactly the right tool despite being
written for SyncPlay: it is the only publisher of a whole new queue that
already has the lock-free ordering discipline this needs. Its docstring now
says it is not SyncPlay-specific.

Lazy, on the press: nothing is fetched until someone actually uses the
button, and then once for the rest of the session. Doing it at playback
start would put a round trip on every episode for a button most people
never touch.

### What the server does with our queue — checked, because a remote can drive this

**[iw]**: "worth a quick look at the server code to check for footguns related
to remote control, this issue was raised against our project but is also a
defect in jf-web which we generally have parity with."

`NowPlayingQueue` is what we publish (`player_reporting.py:345`), and across
the whole server it has exactly **two** consumers:

* `SessionManager.cs:1116` stores it on the session (and hands it back on the
  session DTO, which is what a remote's queue view draws);
* `Group.cs:262` seeds a **SyncPlay** group's playlist from it on
  `CreateGroup` — `session.NowPlayingQueue.Select(item => item.Id)`.

Two things follow, one reassuring and one worth reporting upstream.

**Widening is safe.** The group takes only the *ids*; the synthetic
`PlaylistItemId`s we invent never reach the server's queue at all, so there is
nothing there for them to collide with or to go stale against. And the widen
is refused outright while SyncPlay is enabled, so a group's queue is never
rewritten under it.

**#650 propagates into SyncPlay, and jellyfin-web has it identically.**
`PlayQueueManager.Previous()` is pure index arithmetic on the playlist it was
handed (`PlayingItemIndex--`, floor at 0) — it cannot look up an episode
outside the list. So a group created from a Next Up start inherits the
truncated queue and **no member can step back before that episode**, which is
the same bug one layer up. jellyfin-web reports the same truncated
`NowPlayingQueue`, so it seeds groups the same way. Our fix improves this for
free without touching SyncPlay: once someone presses previous, the widened
queue rides the next timeline report, and a group created after that gets the
whole series.

**One thread note.** A remote `PreviousTrack` runs `play_prev` on the
**websocket callback thread** (`event_handler.py:239`), where the local key
routes through `put_task` and the action thread. That asymmetry predates this
and the handler already does blocking player work there — `stop`, `seek`, and
`play_prev`'s own `get_playback_url` PlaybackInfo POST — so the widen's single
extra GET is incremental rather than novel. Measured against the QA server:
120 episodes, 12 ms, 158 KB. Worth tidying one day; not worth widening this
change to do it.

### Test

Per the standing rule about multi-step properties: press prev **three times**
from a Next Up start and assert you walk back three episodes and the queue
grows monotonically. A one-step test passes on an implementation that can only
ever go back one.

---

## 7 — Posters and thumbnails on video pages

> We should show series/movie posters and episode/video thumbnails on the video
> pages too, not just the backdrops.

### State

`pages/detail.py:70` and `pages/series.py:54` draw exactly one piece of
artwork, `tiles.backdrop_node(...)`, and the heading is **baked into the banner
bitmap** — deliberately: overlay bitmaps composite above all script ASS
(mpvtk GUIDE §6), so a heading drawn as a text node would sit *under* the
image.

That constraint decides the fix. The poster wants to be baked into the same
bitmap by `components/banner.py:compose_banner` (line 49), not added as a
second Image node beside it.

### The trap

`backdrop_node` has two states that must lay out identically — the loaded
banner and the `"pending|"` placeholder that carries the same baked heading
over a flat panel (tile_renderer.py:838). Its docstring spells out why: a
header that drew the heading elsewhere while waiting "moved everything under
it (play buttons included) the moment the image arrived". `eca8e3d6` is that
bug, fixed a week ago.

So the poster goes into **both** paths or the header jumps. And the poster is a
*second* fetch with its own arrival time, which the placeholder mechanism was
not built for — one bitmap, two independent images. The cheap answer is to
reserve the poster's box in the composition from the first paint and paint it
when it lands; the expensive answer is to wait for both. Reserve.

### Shape

- `compose_banner` grows an optional poster image and insets the heading
  stack past it. Text sizing is already derived from the banner height, so the
  measure changes but the arithmetic does not.
- Which image: `Primary` for Movie and Series, `Primary` on the *episode* for
  an Episode (that is the still, and it is what jellyfin-web shows there).
  `repository.backdrop_spec`'s reasoning is the guide for what to do when it is
  missing — and note its rule that "a poster is not a fallback" is about
  cropping a poster *into* the banner, which is the opposite of this and not in
  conflict.
- `has_backdrop` stays the answer to "will there be a banner"; a poster with no
  backdrop is a case that needs deciding (poster on a flat panel, or fall
  through to today's text header).

---

## 8 — Fact check: EXIF orientation

> I don't think mpv applies the exif directions actually.

**It does.** Measured here on mpv 0.41.0, with a 200x100 image tagged
`Orientation=6`:

```
norot.jpg   VO: [null] 200x100    video-params/rotate = 0
rot.jpg     VO: [null] 100x200    video-params/rotate = 90    <- rotated
rot.png     VO: [null] 100x200    video-params/rotate = 90
rot.webp    VO: [null] 100x200    video-params/rotate = 90
```

mpv autorotates from EXIF for jpg, png and webp (`--video-rotate` defaults to
`yes`; ffmpeg exports the EXIF orientation as a display matrix for these
decoders).

**And we already handle it.** `pages/comic.py:_picture_size` (line 574) swaps
width and height for orientations 5-8, with a comment asserting exactly this —
which the measurement confirms is correct. Photos need nothing either: they go
through ordinary playback and mpv rotates them.

### The one thing that came out of it

`video-params/dw` reports the **pre-rotation** size and `video-out-params/dw`
the post-rotation one:

```
rot.jpg   video-params/dw=200 dh=100      video-out-params/dw=100 dh=200
```

Nothing today asks mpv for a displayed picture size — the comic reader measures
the file with Pillow — so nothing is wrong. But any future code that does
(#3, if it is ever picked up) must use `video-out-params`, and the difference
is exactly a wrong answer that looks plausible.

### Left open

A photo fetched through the **image endpoint** rather than `/Download` — HEIC,
or a user without `EnableContentDownloading` (`media.py:634`) — is re-encoded
by the server. If Jellyfin rotates the pixels and leaves the EXIF tag in place,
mpv would rotate a second time. Not answerable from here; needs one rotated
HEIC against a real server. Low risk, worth five minutes during hand-testing.

---

## 9 — Fact check: does playstate reach the local catalog?

> Does playstate *always* sync to the local database, even when the user is
> online? If it doesn't, then stale data might get shown in offline mode.

**It does not.** `sync/offline_media.py:190`:

```python
def record_offline_progress(self, position_ticks, finished=False):
    if self.client is not None:
        return  # online: the timeline already reports progress
```

`set_played` (line 170) does the same — it reports to the server and returns
before the local write. And `userdata_json` is written into the catalog at
**download time only** (`manager.py:720`); the only other writers are those two
offline paths. There is no refresh pass anywhere.

So: watch a downloaded episode while online, go offline, and the catalog still
shows it unwatched at position 0.

### Where it bites, and where it does not

The auto-download reaper is **not** affected, and its code says why. `_is_watched`
(auto.py:248) asks the server first and falls back to `userdata_json` only when
offline, with a docstring naming this exact problem: "The catalog's userdata is
a download-time snapshot, so it says 'unwatched' forever if we trust it alone."
So the storage cap is being spent correctly.

That is the strongest argument for fixing this at the source rather than an
argument that it does not matter. One consumer has already had to work around
the stale snapshot with a per-item HTTP call per pass; the ones that cannot
make an HTTP call are the ones that break. Those are:

- **Offline browsing** — the reported symptom. Everything the catalog draws
  offline reads `userdata_json`.
- **`delete --watched-only`** (`manager.py:589` and `_delete_playlist` at 610),
  which reads the snapshot with no server fallback at all. "Delete watched
  downloads" therefore skips everything watched online — silently, since not
  deleting looks the same as having nothing to delete.

### Shape

Two halves, and the cheap one covers most of it:

1. **Anything played through the shim writes locally too.** Make those two
   methods write the local catalog *in addition to* reporting online rather
   than instead of. One SQLite write per progress report, and it covers every
   item the user watches here, downloaded or not. This is most of the value for
   very little of the work.
2. **What was watched elsewhere** genuinely needs the server asked. Izzie's
   proposal — on home-page load, look for state changes on series we hold
   downloads for — is the right scope: it is bounded by what is in the catalog,
   it runs when the user is already waiting for a screen, and it does not probe
   the whole offline catalog.

`db.update_userdata` is **advance-only** by design (db.py:390, and the contrast
with `set_reading_position` at 452 spells out why). So an online *rewind* will
not propagate to the local copy. That is almost certainly right — the local
copy is a floor, not a mirror — but it should be a stated decision rather than
an accident of which method was reused.

### Test

The standing multi-step rule applies squarely: this is state feeding back into
the input that produced it. Drive **three** progress reports and assert the
stored position tracks; a one-step test passes on an implementation that writes
once and then latches.

---

## 10 — Playback info that matches jellyfin-web's

> Improve the media info screen to match jellyfin-web's, namely showing media
> type, transcode/remux state, etc. (Still bind `i` to MPV's info screen.)

### State

There is no such screen. The HUD's gear menu row labelled *Playback Data*
(`hud.py:373`) toggles **mpv's own stats.lua overlay** via
`controller.toggle_stats()` — the same thing `i` does (`player.py:1116`). So
today the shim shows mpv's view of the decode and nothing at all of Jellyfin's
view of the stream.

`i` staying bound to stats.lua is explicit **[iw]**, so this is a new screen
beside it, not a replacement. The gear row is the natural home; whether it
displaces *Playback Data* or sits next to it is a look-at-the-menu decision.

### What jellyfin-web shows

`src/components/playerstats/playerstats.js:380-445` — categories: Playback
Info (player name + localized play method), then the player's own stats, then
Transcoding/Remuxing/Direct Streaming info if any, then Original Media Info,
then SyncPlay info.

The play method is four-valued, not two (`playback/playmethodhelper.js`):

| Displayed | Condition |
|---|---|
| Remux | video direct **and** audio direct |
| DirectStream | video direct, audio not |
| Transcode | server `PlayMethod === 'Transcode'` |
| DirectPlay | server `PlayMethod` is DirectStream or DirectPlay |

### What the server actually returns — measured, and not what was planned

Both readings this section originally proposed were wrong, and a live probe
against 10.11 is what caught them. Two device profiles per outcome, one item,
PlaybackInfo only (no stream fetched, so no ffmpeg started):

* **``TranscodeReasons`` is not a MediaSource field.** It is a query
  parameter *inside* ``TranscodingUrl``, comma-joined. Reading it off the
  DTO — which is what the schema suggests — yields None every time, and the
  panel then says the file is being transcoded for no reason at all.
* **A remuxing URL does not say ``VideoCodec=copy``.** It names the *target*
  codec, which for a remux is simply the codec the file already has. The
  test is a comparison against the source stream, not a keyword — and the
  parameter can name several codecs, so it is membership, not equality.

Source ``mkv / hevc / aac``:

===============================  ============  ============  ==============
profile                          VideoCodec    AudioCodec    method
===============================  ============  ============  ==============
container refused, codecs kept   hevc          aac           Remux
container refused, audio changed hevc          opus          DirectStream
video codec refused              h264          aac           Transcode
===============================  ============  ============  ==============

`tests/test_play_method.py` carries these as fixtures, with the URLs in the
shape the server really sends.

### The design decision

jellyfin-web derives all of that from `session.TranscodingInfo` — the
**server's** view of its own session, fetched from `/Sessions`. We could do the
same, but it is an HTTP call that has to be polled for as long as the screen is
up, for a screen that is up for seconds.

We do not need it. We *are* the client that made the decision, and
`media.py:_get_url_from_source` (line 415) knows exactly which of three
branches it took: direct path (local or remote, line 457/469), static stream
(`SupportsDirectStream`, line 475), or `TranscodingUrl` (line 501). Remux
versus transcode is readable from the transcoding URL's own parameters
(`VideoCodec=copy`), and the *reasons* are on the media source
(`TranscodeReasons`) without asking anyone.

So: derive locally. What that gives up is the live transcode progress —
completion percentage and encoder fps — which only the server knows. Leave
those out rather than poll for them; they are the least useful rows on
jellyfin-web's version and the only ones that cost a request per second.

### Deferred: what DirectPlay should mean

`player_reporting.py:339` and `:557` both report:

```python
"PlayMethod": "Transcode" if video.is_transcode else "DirectPlay"
```

Jellyfin's enum has three values and we only ever send two, so a `static=true`
HTTP stream is being reported as `DirectPlay`.

**[iw]** "DirectStream vs DirectPlay is a fun one because everyone basically
means the second when they say the previous. DirectPlay might mean direct file
paths (which we could genuinely report for offline playback). That is worth a
fact check but can be deferred for later."

That reading is coherent and is the one this client is unusually well placed to
use, because unlike a browser we have all three cases for real:

| Ours | What happened |
|---|---|
| DirectPlay | we opened the file ourselves — a direct path, or a **downloaded copy** played back through `offline_media` while still online and still reporting |
| DirectStream | `static=true` over HTTP, the server serving the file unmodified |
| Transcode | the server re-encoding, remux or not |

The downloaded-copy case is the one that makes it worth doing: it is genuine
direct play, it is reported to the server today as the same string as a remote
HTTP stream, and no other client has it.

**Deferred out of this batch [iw].** Changing it changes what the server
records and shows for our sessions, and the mapping wants confirming against a
live dashboard with all three branches forced. #10 derives its *displayed* play
method locally and does not touch what is reported, so the two are independent.

### Also: the useful half of mpv's own overlay

**[iw]** "Worth adding things from MPV's display to ours that are actually
useful, MPV's screen was never ideal because it showed behind our OSC."

That is not a preference, it is the z-order: stats.lua draws ASS and the HUD
is overlay bitmaps, which composite **above** all script ASS (mpvtk GUIDE
§6). mpv's numbers have therefore always been drawn *behind* the controls
you would be reading them from. Moving the viewer-facing ones into a panel
we draw is the only way they are legible while the OSC is up.

The set answers a viewer's questions, not a developer's: hardware
acceleration, video output, framerate, dropped frames, A/V sync, buffered
seconds, download speed. Three rules, each with a test:

* **Dropped frames is two numbers, labelled.** A decoder drop is a machine
  that cannot keep up; a VO drop is usually display sync. One combined
  figure sends people to the wrong fix.
* **Software decoding reads "No"**, not mpv's own ``"no"``, which looks like
  a broken value rather than an answer.
* **A counter mpv has nothing to say about is omitted, never shown as 0.**
  There is no ``estimated-vf-fps`` before the first frame and no video
  counters during audio; a zero would read as a measurement. The whole
  block goes when mpv says nothing, so there is never a heading over
  nothing.

A misspelled property name fails *silently* — both backends turn an unknown
attribute into a property read, so it raises, is dropped with the
legitimately-absent ones, and the row simply never appears.
`tests/test_mpv_stat_properties.py` checks every name against
``mpv --list-properties``, offline, skipping when mpv is not on PATH.

### Shape — what it came to

- `media.py` owns the derivation and the four constants, set at each of the
  four exits of `_get_url_from_source` (plus the photo branch, which returns
  before it, and `OfflineVideo`, which sets it in `__init__` — a local file
  has no decision pending, so the panel must not need playback started).
- `gateway/hud.py:playback_info()` and `player_stats()`, both read per build
  rather than pushed on the playstate snapshot: the panel is open for
  seconds and the blob carries a whole MediaSource.
- The panel is a **`Dialog`**, which buys two things for one: ESC and
  click-outside dismissal, and `state.modal` — which `phud_busy` treats as a
  busy HUD, so the panel cannot be read for four seconds and then yanked
  away with the bar it hangs off.
- Sized against the window. The HUD is drawn down to phone-shaped, and a
  fixed 520-wide panel in a 480-wide window has its edges off both sides.

### One string is not ours to choose

`seed_from_jellyfin_web.py` matches our msgid against jellyfin-web's
**value**. So every label here is their English character for character —
trailing full stops and all, including `DV bl preset flag`, typo and all.
77 of the formatter's 82 strings seed on that basis; the five that do not
are unit formats web builds with template literals. Tidying any of them
costs the translation in 86 locales and gains nothing.

---

## 11 — Media info in the context menu

> Add a metadata info screen to context menu, similar to jf-web, to allow
> seeing media info details for files. This is the other main media management
> gap that jf-web has with us. I don't intend to add metadata editing or server
> management.

### What jellyfin-web shows

`src/components/itemMediaInfo/itemMediaInfo.js`. Per **media source** (a
version picker when there are several): Container, Path, Size — then one block
per stream, typed Video / Audio / Subtitle, each a list of labelled attributes:
Title, Language, Codec, Codec tag, AVC, Profile, Level, Resolution, Aspect
ratio, Anamorphic, Interlaced, Framerate, Layout, Channels, Bitrate, Sample
rate, Bit depth, Video range, Video range type, the Dolby Vision block, Color
space/transfer/primaries, Pixel format, Ref frames, Rotation, NAL, and
Default/Forced/External on audio and subtitle streams.

The context menu entry is `MoreMediaInfo` (itemContextMenu.js:279).

### The one gate — and we are not taking it

`itemMediaInfo.js:72`:

```js
if (version.Path && user?.Policy.IsAdministrator) {
```

jellyfin-web shows the **file path to administrators only**. Everything else on
that dialog is shown to everyone.

**[iw]** "I don't see a reason to make copying stream path admin only. If
someone wants to rip a Jellyfin server they can copy the path out of the logs.
(Or devtools on web.) Download permission is more of a courtesy thing, not a
security feature, as stream static has no auth whatsoever beyond the media guid
as 'password'."

So: **show the path to everyone**, and this is a deliberate divergence from
jellyfin-web rather than an oversight. The reasoning is that the gate is not a
boundary — the same string is already in this app's own logs, in a web client's
devtools, and a `static=true` stream is reachable by anyone holding the item
guid regardless of any of it. A control that stops nobody and inconveniences
the owner of the machine is not worth the row it hides.

Recorded here because it will read as a parity bug to the next person who
diffs the two dialogs, exactly like the four deliberate divergences listed in
`CLAUDE.md`. `user_policy.py` therefore needs no `is_administrator` helper for
this item.

### State on our side

The data is already fetched — `DETAIL_FIELDS` has `MediaSources,MediaStreams`
— so a detail page needs no extra request. From a **tile** context menu it
does: a grid DTO carries neither. That is one `get_item` on the worker, which
is what `dialogs.py:_open_add_to` (line 35) already does for the same reason.

`dialogs.py` is the home; `_dialog_shell` (line 567) and the scroll it needs
already exist. This is a lot of rows on a small window, so it is a scrolling
dialog rather than a fixed one.

### Shape

- `dialogs.media_info(item, server)` — fetch if the DTO is thin, render
  through the groundwork formatter, version picker when
  `len(MediaSources) > 1`.
- Menu entry in `tiles.py` and on the detail page's action row.
- Read-only. No identify, no refresh, no metadata editing.

---

## Order of work

**[iw]** "At least one commit per major change, group work in similar areas
logically in the code, and do harder tasks first to reduce issues with commit
cleanup (merging post-fix and post-test bug fixes into previous commits where
possible) and making stacked PRs easier later."

So: hardest first, grouped by the code they touch, trivia last — the opposite
of the smallest-first order this file was first drafted with.

**Media info and management** (the biggest block, and it shares groundwork):

1. Groundwork — the shared media-info formatter.
2. **#10** — playback info. Hardest of the pair: live state, a new HUD page,
   and the playstate snapshot.
3. **#11** — the media info dialog. Large but mechanical once the formatter
   exists.
4. **#4** — Delete from Disk. Same context menu and the same dialogs module as
   #11, and wants the same DTO-field plumbing.

**Player and input** (highest uncertainty, so early):

5. **#1** — mouse modality. The open question below can reshape it, and the
   hand-testing needs both VM sessions.
6. **#6** — previous item from Next Up.

**Browser rendering:**

7. **#7** — posters and thumbnails. Independent of everything, and its
   placeholder trap makes it a bad one to rush at the end.

**Offline sync:**

8. **#9** — playstate into the local catalog.
9. **#5** — lookahead hysteresis, plus the settings-disclosure change.

**Last:**

10. **#2** — the reader's toast. Trivial, zero fixup risk, a good thing to have
    left when the branch is otherwise done.

The `PlayMethod` reporting change is deferred out of the batch entirely (see
#10).

## Open questions

- **#1** — dragging itself, which needs a window manager: **[iw]** on X11, the
  VM on Wayland. Everything else about it was settled by measurement (above).
- **#7** — a poster with no backdrop at all: poster on a flat panel, or fall
  through to today's text header? Taking the first unless it looks wrong.
- **#8** — does the image endpoint leave the EXIF tag on a rotated HEIC?
  Five minutes during hand-testing; low risk either way.
- **#10** — the gear menu's *Playback Data* row opens mpv's stats.lua today,
  and `i`/`I` do the same thing. Assumption taken: **our new screen takes the
  gear row**, and mpv's stays on the keys, per "still bind `i` to MPV's info
  screen". That leaves mpv's stats without a pointer-reachable entry, which is
  the one part worth a second look on screen.
- **#5** — exact labels for three settings whose meaning is "leave blank unless
  you know why".

## Hand-testing checklist

Beyond the unit suite (`xvfb-run -a python3 -m unittest discover tests`) and
the mpv integration matrix on both backends:

1. **#1** — drag, double-click and right-click, in both modes, on X11 and on
   Wayland, with the HUD hidden and with it up.
2. **#2** — a book already downloaded, and one that downloads slowly enough to
   see the toast; confirm an unrelated toast raised in between survives.
3. **#4** — delete as a non-admin with delete rights on one library and not
   another; confirm the entry is absent on the second. Delete an item that has
   a downloaded copy.
4. **#5** — set min/max, watch an episode, confirm one batch refill rather than
   a trickle; leave the new settings blank and confirm nothing changed.
5. **#6** — three presses back from a Next Up start; then the same in a
   SyncPlay group (expect the group's behaviour, not ours) and offline.
6. **#9** — watch a downloaded episode online, pull the network, confirm the
   catalog agrees. Then, still online, "delete watched downloads" and confirm
   it now removes it (today it does not).
7. **#10/#11** — against a file that direct-plays, one that remuxes and one
   that transcodes; a multi-version item; a downloaded copy played while
   online. The path row shows for everyone, so check it as a non-admin too —
   deliberately, see #11.

---

## 12 — A hardware-decoding setting

**[iw]** "We should also add a setting to the player for hwdec. Am debating if
it should be enabled by default. Jellyfin Media Player enabled it by default
and it worked fine for *most* users but caused issues for a long tail of
users, probably sadly the same users that probably needed it the most.
Anything 1080p or lower though probably doesn't need hardware decoding on most
hardware from the past decade."

### What the research changed

The obvious design — default to `auto-safe`, mpv's "safer on" — **does not
exist**. In current mpv `auto-safe` is documented as "exactly the same as
auto", and `auto` is already the whitelisted mode; `auto-unsafe` is the
anything-goes one. mpv#12948 is the proposal to default it on, and it is open
and argued against by a maintainer:

* particular vendor/GPU combinations are badly broken — AMD vaapi on Linux
  causing GPU resets — and mpv cannot afford Chromium's allow/blocklists;
* IINA, which does default it on, had to strip vp9 from `hwdec-codecs` after
  Intel Macs froze;
* one reporter has mpv **hanging with the window never opening** on
  vp9/videotoolbox;
* the power-saving argument is questionable below ~720p on desktop, which is
  the same observation as **[iw]**'s about 1080p.

mpv's manual, on turning it on: *"acknowledge that this may cause problems"*.

### Does `auto-copy` buy us anything? — no

**[iw]** asked, and the answer is no, for a reason specific to this app. mpv's
case for copy-back is that it *"will allow CPU processing with video filters.
This mode works with all video filters and VOs."* We use **no video filters**
— the shader pack is `glsl-shaders`, which runs inside the GPU renderer on
frames already on the GPU — and we set no `vo`, so we get mpv's default, which
is **gpu-next**: a VO that does direct hwdec interop natively. JMP needed
copy-back because `vo_libmpv` renders into Qt, which is the hard interop case.
So it is offered as a fallback for a misbehaving direct path, not as a default.

### Shape

Four values: `no` (default), `over-1080p`, `auto`, `auto-copy`.

`over-1080p` is the one **only this client can offer**: the source resolution
is in the DTO before playback starts, so decoding can be software where
software is fine and hardware only where it is not. It is the thing mpv's own
maintainer says would be needed first ("some basic qualifiers for it like a
minimum video resolution").

Three things are load-bearing:

* **It is applied per file, before `play()`**, beside the volume and
  still-duration writes and for the same reason: `hwdec` is read at *decoder
  init*, which is where the failure modes happen. Setting it after would apply
  to the next file. A side benefit is that changing the setting takes effect
  on the next item rather than the next launch.
* **The height comes off the MediaSource, not the item.** A multi-version item
  has one height per version and the one playing is the one at stake.
* **An unknown height means software.** Audio, a photo, an unprobed file —
  starting hardware and turning it off is the wrong way round.

`--disable-hwdec` is the recovery path, per-run like `--ui-scale` rather than a
config write like `--reset-shaders`: it exists for hardware decoding stopping
the window opening at all, and once it has opened the setting is reachable in
the ordinary way.

### The default

**Off. [iw]'s choice**, with the same argument the investigation reached:
shipping a new default in the same release as the feature gives no signal
about which of the two broke someone.

(Recorded because it briefly looked otherwise: the question tool returned only
the free-text notes attached to the answer and not the option chosen with them,
so this file said for one commit that nobody had picked. Worth knowing for the
next time an option is picked *and* annotated.)

### Naming

**[iw]** "Probably worth renaming 'On (copy back)' into 'Copy (advanced)' with
a note that SVP/VapourSynth might need it."

Which is the one real use we have for it: the direct modes do not work with
video filters and the copy modes do, and a user running SVP has a VapourSynth
`vf` in their own `mpv.conf`. (The shim only drives SVP's HTTP API to pick
profiles — the filter itself is the user's.) The shader pack is *not* a video
filter and is unaffected either way, which the note in `config.NOTES` says so
that the two are not confused.

## 13 — Shader packs must not reach stills

**[iw]** "We need to make sure we're not applying shader packs to photos and
comic books!"

Confirmed, and it is worse than per-file: `VideoProfileManager.load_profile`
is applied once — from the menu, or restored at startup from
`shader_pack_profile` — and left on the mpv instance. Nothing on the play path
touches it. So an anime-upscaling chain runs over a photograph, and over a
comic page at 1600x2400 or larger, where it is both wrong and expensive.

**The fix cannot be `unload_profile`.** That clears `current_profile`, which
the menu's selection and the remembered setting both read, so a still would
silently reset the user's chosen profile. It needs a suspend/resume pair that
keeps the remembered name — suspend on a photo (`_play_media`, `is_photo`) and
on a comic page (`show_picture`), resume for video and on `clear_picture`.

## 13b — …and the pack must not decide hardware decoding either

Found by **[iw]** grepping the pack, one commit after #12 shipped:

```
pack.json:      "hwdec-default": { ... ["hwdec", "auto-copy"] },
pack-next.json: ["hwdec", "auto-copy"], ["hwdec", "d3d11va"],
                "default-setting-groups": [ ..., "hwdec-default" ]
```

Every profile pulls that group in, so picking any shader profile silently
turned hardware decoding on — one commit after it became a user-facing
setting that defaults **off**, and defaults off because a long tail of
drivers handle it badly. The breakage would have been attributed to the
shader profile, which is the last place anyone would look.

**[iw]** "I don't want to override the user's setting. We should set
auto-copy transparently based on shader pack settings, SVP integration
enabled, or mpv config when a vf is detected. Default shader pack should
otherwise NOT touch hwdec. If the user has it set to off, nothing happens.
If the user has it set to auto, we force it to auto-copy when it's actually
needed."

So the pack's value is split, and the on/off half is simply dropped: the
pack does not get to turn hardware decoding on.

**The blanket `auto-copy` is not evidence of anything.** It is in *every*
profile — **[iw]**: "yeah, this was just me being risk-averse in the past" —
and a glsl shader runs inside the GPU renderer, on frames that are already
there. So a shader profile on gpu-next needs nothing copied back, and the
`auto-copy` in `hwdec-default` is ignored outright.

**What does need system RAM is a real `vf`.** Grepping the shipped pack, there
is exactly one, and it is instructive:

```json
"hw-d3d11va-rtxvsr": { "settings": [
    ["hwdec", "d3d11va"], ["gpu_api", "d3d11"],
    ["vf", "format=nv12,d3d11vpp=scale=2:scaling-mode=nvidia"] ] }
```

`d3d11vpp` is a Direct3D **video-processor** filter operating on d3d11
surfaces, and the profile names a *direct* hwdec mode beside it for exactly
that reason. So a filter is not automatically a reason to copy back — a
profile naming a direct mode is saying its filter wants GPU frames, and
copying back would break the only profile in the pack that has a filter at
all.

The rule is therefore `sets a vf AND does not name a direct hwdec mode`,
which for the shipped pack means **no profile asks for copy-back** — which is
the right answer, and the one that lets the user opt in from the menu instead
**[iw]**.

`hwdec_for(height, needs_copy)` then **upgrades, never enables**: `no` stays
`no`, `auto` becomes `auto-copy`, an explicit `auto-copy` is unchanged, and
the threshold mode upgrades only where it was already on.

The other two sources of `needs_copy` are unchanged and are the ones that
still fire in practice: `svp_enable` (a VapourSynth filter in the user's own
mpv.conf), and **mpv's own `vf` property being non-empty** — the general
case, and the only one that sees a filter the app knows nothing about.
Measured: `vf` reads `[]` on a fresh handle before any file is loaded, so it
is answerable at exactly the moment hwdec has to be decided.

### Two escape hatches, both **[iw]**

**A pack may state a requirement.** The first cut dropped every `hwdec` a
profile set, which left `rtx-vsr` unable to work at all — it needs `d3d11va`
for its Direct3D filter. **[iw]**: "maybe we should whitelist non-naive hwdec
settings in packs, like d3d11va". So the line is *policy vs requirement*:

* `auto`, `auto-copy`, `auto-safe`, `yes`, … are opinions about the machine
  ("use hardware decoding if you can, whatever that is here") and stay
  dropped;
* a **named decoder** is a requirement of the profile — applied, and
  remembered as `forced_hwdec` so the per-item write does not undo it on the
  next file. Choosing that profile is opting in;
* **and so is `no`** **[iw]** — "nothing sets it currently", but a profile
  setting it would be saying its shaders need software frames, which is a
  statement about the profile and not about the machine. Listed as a
  requirement for what it *would* mean rather than for what any pack does
  today.

**And the user's own mpv.conf pins it.** **[iw]**: "if the user's mpv config
sets the value, we pin it and never touch it, and show 'Pinned by config' on
the settings page". `hwdec_for` returns **None** there and both callers treat
that as *do not write the option at all* — which keeps mpv's own config
precedence intact rather than modelling it here. The Settings page says so,
because a control that is silently inert is the failure this whole feature is
downstream of.

The scan is deliberately of the **top level only**: an `hwdec` inside a
profile section (`[name]`) is conditional, and reading it as a pin would
disable the setting on a value that may never apply. Not pinning is the safe
direction — the setting keeps working and mpv still applies the profile where
it fires.

### Precedence, in order

1. `--disable-hwdec` — per-run recovery, wins everything.
2. **The user's mpv.conf** — we write nothing.
3. **A profile stating a requirement** — a named decoder, or `no`.

   Enforced in `process_setting_group`, not only in `_play_media`: a profile
   applies its settings *directly*, so the pin has to be checked where the
   pack is read or it slips past between one file and the next. The per-item
   write is not the only writer.
4. The Hardware Decoding setting, plus the copy upgrade where a real filter
   needs system RAM.

## 14 — Search asks for no fields

Noticed while adding `CanDelete`. `LibrarySource.search` passes **no `fields`
at all**, which costs three things at once, measured against a real 800-item
search:

* `PrimaryImageAspectRatio` is absent on **all 800 items**, so search tiles
  have never been shaped by their own artwork (`auto_geom` falls back for
  every one of them);
* `MediaSourceCount` is absent, so a multi-version item shows no version chip
  — this is the one **[iw]** remembers being asked for;
* `CanDelete` is absent, so #4's Delete from Disk cannot appear on a search
  result, unlike everywhere else.

Adding `GRID_FIELDS` costs +47 KB on a 1.28 MB response and was not slower in
the measurement. Note that `MediaSourceCount` is **absent rather than 1** for
a single-version item — the server omits the property at 1, which
`tile_renderer` already documents — so the chip correctly stays off for most
of a library either way.

---

## 15 — Per-library / per-series shader profiles

**[iw]** "It's probably honestly worth adding options to make the user's
shader selection library or series specific too. Anime4K shaders are usually
specific to the media type and specific quality defects — e.g. a poorly
compressed anime gets a different setting than a crisp video, which gets a
different setting from a live action movie."

Which is the real shape of the problem: there is no one right Anime4K
profile, there is a right one *per kind of source*, and the current single
global choice makes the user re-pick it by hand or accept the wrong one.

### Storage — its own file **[iw]**

`settings_base.object_types` has no `dict`, so a mapping cannot be a config
key without being encoded as a list of pairs. **[iw]** chose a separate JSON
file, which is also the right answer for a second reason: these overrides are
**device-local**, not account-level. Which profile runs well depends on this
machine's GPU, so unlike `home_sections` (DisplayPreferences, server-side)
this must not follow the user to another device.

Beside `cred.json` in the config directory, keyed by item id, with the server
uuid in the key — ids are only unique per server, and a multi-server setup is
the norm here.

### UI **[iw]**

> "Maybe a Default, Library Specific, Series Specific options from the menu
> before we show the options, and also show which of those is currently in
> effect in the menu?"

So the profile menu grows a scope step in front of it, and the scope step
reports the answer as well as asking the question — the second half is the
part that makes it usable, because otherwise "why is this film sharpened
differently" has no visible cause. Something like:

```
Video Playback Profile
  Scope:  This Series  (Anime4K: Mode B)     >
  Default (all media)  ·  Anime4K: Mode A
  This Library         ·  not set
  This Series          ·  Anime4K: Mode B    <- in effect
```

### Decided

* Resolution order: **series → library → default** **[iw]**.

### Open questions for the design pass

* **What "this library" means for an item reached by search or by a
  by-name screen**, where there is no library in the route.
* The menu is `menu.py` (OSD, for the lua OSCs) *and* the mpvtk HUD's gear.
  Both need it, or the setting is unreachable in one of the two UIs — the
  same split #12's setting avoided by living in Settings.
* Whether an override should also pin `shader_pack_gpu_api`, which is
  currently global and is the other half of "will this profile run here".

Not started. Listed here so it is covered by the QA and code-review pass at
the end of the batch **[iw]**.
