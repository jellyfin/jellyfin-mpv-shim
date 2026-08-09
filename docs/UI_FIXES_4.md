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
| [2](#2--the-reader-should-dismiss-the-downloading-toast) | Reader dismisses the downloading toast | done |
| [12](#12--a-hardware-decoding-setting) | A hardware-decoding setting | done |
| [13](#13--shader-packs-must-not-reach-stills) | Shader packs must not reach stills | done |
| [15](#15--per-library--per-series-shader-profiles) | Per-library / per-series shader profiles | todo |
| [16](#16--how-many-key-bindings-do-we-still-need) | How many key bindings do we still need? | investigation, not started |
| [14](#14--search-asks-for-no-fields) | Search asks for no fields | done |
| [3](#3--dropped-zoom-and-drag-for-photos) | ~~Zoom/drag for photos~~ | **dropped [iw]** |
| [4](#4--delete-from-disk) | Delete from Disk | done `798edee0` |
| [5](#5--lookahead-hysteresis-661) | Lookahead hysteresis (#661) | done |
| [6](#6--previous-item-from-next-up-650) | Previous item from Next Up (#650) | done |
| [7](#7--posters-and-thumbnails-on-video-pages) | Posters/thumbnails on video pages | done |
| [8](#8--fact-check-exif-orientation) | Fact check: EXIF orientation | **answered — no work** |
| [9](#9--fact-check-does-playstate-reach-the-local-catalog) | Fact check: playstate → local catalog | done |
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

### What it came to

`clear_status_if(text)` on the shell, called from both readers the moment a
document or a page is on screen. Conditional on the text still being ours,
because six seconds is long enough for something else to have replaced it and
clearing an *error* would be worse than the stale toast.

The message has one spelling (`ItemActions.downloading_message`) because the
raiser and the clearer compare against each other — two format strings would
silently never match, and the symptom would be "the fix does nothing" rather
than an error.

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

### Left for the QA pass: an e2e test that actually deletes

The most destructive thing in the batch is unit-tested only. The QA server is
regenerable, but **[iw]**: "deletes have a real cost to regenerate from" — so
the test has to create what it destroys, and **[iw]** named the way to do it:
**record something off Live TV, then delete the recording**. It is ours, it
costs nothing to remake, and it goes through the same `/Items/{id}` DELETE as
any other item. See the `stdjflib-qa-server` note.

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

### What it came to

`hysteresis()` and `_per_pass()` resolve the three settings; `_lookahead`
consults the first and `fill` the second. `_upcoming_held` does the counting,
against the **catalog** rather than against the window — the window is what is
being sized.

Four decisions worth keeping:

* **Queued and downloading count as held.** The issue asks for it, and
  without it every pass re-queues the same episodes for as long as the first
  batch takes, which is precisely the stampede this replaces.
* **Errored rows do not.** Those are episodes we tried and failed to get, and
  treating a failure as stock is how a series quietly stops being topped up.
* **A catalog read failure looks *stocked*, not empty.** Answering "none"
  would top up on every pass — the behaviour being removed.
* **A sub-1 per-pass cap falls back to 20 rather than clamping to 1.** A
  hand-typed `-3` clamped to 1 is a one-item trickle that looks like the
  feature working, and this setting exists to make passes bigger.

### The settings screen needed a small change first

`ADVANCED_GROUPS` replaces `title == _("Advanced")`. The disclosure used to be
a property of a group's *name*, which capped a tab at one hidden group and
forced it to be called that — so these three fields could not sit behind it
next to the settings they qualify without moving to another tab. Now it is
membership in a set, with one checkbox per tab however many groups are in it.

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

### What it came to

Baked into the banner bitmap, as the trap above requires. Three things the
build settled:

**The cache key is on the poster's *presence*, not its identity.** It is a
second fetch with its own arrival time, and `_banner_poster` can answer a key
as soon as it knows the spec — before the bytes land. Keying on that gives the
waiting composition and the finished one the same key, so the cache serves the
poster-less banner for ever and the poster never appears. Its absence is keyed
too (`nopo`), or the two states collide the other way.

**`inherit=False`.** An episode's banner is already the *series* backdrop, so
an inheriting poster would draw the same series twice and the episode not at
all. Off is what makes the slot the episode still — the thing actually asked
for.

**No plate, no rounded corners** **[iw]**: "thumbnails are drawing inside a
poster with rounded corners and black letterboxing… just compose what we have
over the backdrop, and use a drop shadow". The first cut made the slot a fixed
2:3 box, so a 16:9 still arrived letterboxed — which reads as a poster *of* a
photograph rather than as the frame it is. Now the slot is a **bounding** box
and each shape is drawn at its own aspect inside it, bottom-aligned so its
baseline agrees with the heading, separated by a drop shadow painted onto the
canvas. Not `imageutil.with_shadow`, which keeps the shadow inside the image's
own bounds — right for a logo with margins, invisible on a full-bleed
rectangle where every edge is ink.

### And a setting **[iw]**

"Might also be worth adding a browser setting for 'show posters/thumbnails on
detail pages' because some may want the old behaviour back or don't like
spoilers in thumbnails."

`detail_artwork`, default on, gated at `_banner_poster` — the single choke
point, so it cannot be honoured in one path and not another. Nothing has to be
invalidated when it changes: the key gains `nopo` and the banner recomposes on
the next paint.

Worth recording that the two reasons are unrelated, since only one is about
taste: an episode still is a frame of an episode you have **not watched**,
sitting on the page you opened to decide whether to watch it. jellyfin-web has
no equivalent — its `UseEpisodeImagesInNextUp` is about the Next Up rows,
which we already honour through `image_spec`'s `inherit`.

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

### What it came to

Both halves, as planned.

**Anything played through the shim writes locally too.** `_mirror_locally`
runs before the online/offline branch in both `record_offline_progress` and
`set_played`, so the catalog is written whichever way the network is going.
The **replay queue** stays offline-only, because that is what it is for: a
list of changes the server has not been told about, and queueing while online
would queue a write that already happened.

**And a pull for what was watched elsewhere.** `SyncManager._refresh_userdata`,
beside `_sync_playstate` in the worker loop and its mirror image — batched
(`USERDATA_BATCH = 60`, because ids travel in the query string and proxies cap
it), every five minutes rather than thirty seconds (someone finishing an
episode on a phone does not need noticing within the minute), and it only
notifies the browser when something actually moved. `db.update_userdata` now
returns whether it changed anything, which is what makes that last part
possible.

**Advance-only, still.** The local copy is a floor, not a mirror: an item
un-watched on another device stays watched here. That is the existing rule
rather than a decision taken here, and it is the one thing about this worth
revisiting — flagged in the method's docstring.

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

`GRID_FIELDS`, not `LIST_FIELDS`: search asks for **800** items, five times a
grid page, so the one field the grid already drops for being a third of the
body is the one this must not add back. It costs +47 KB on a 1.28 MB response
and was not slower in the measurement. Note that `MediaSourceCount` is **absent rather than 1** for
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

---

## 16 — How many key bindings do we still need?

An investigation, not a change, and not started. Listed here rather than
raised separately because this batch already moves input handling (#1's mouse
modality), so the same ground gets reviewed either way **[iw]**. Related:
PR #547, "Get original keybindings for arrow keys from conf", which is long
since outdated.

### The observation

**[iw]**: "Makes me wonder how many of our keybinds could be turned into ones
which don't even need to be overridden on the python side at all anymore,
except when lua scripts are detected as not being available (which at this
point we need to treat no lua as a full commandline mode fallback because it
means there is no UI!)"

#1 is a worked example of the pattern paying off. `mbtn_left_dbl` and
`mbtn_right` needed **no** Python-side binding: mpv's own defaults did the
right thing, and the only reason they misbehaved was our `mpvtk_mouse` section
swallowing them once the HUD was up. The fix was to fall *through* to mpv
rather than to reimplement — and that is available for keys too, because
`MpvtkApp.claim_keys` already exists to take a key only while something on
screen wants it.

### Why the arrow keys and space were taken in the first place **[iw]**

Two reasons, and both look weaker than they did:

1. **The OSD menu.** Arrows and ENTER drive `menu.py`, so they had to be
   Python-side. Better served by an mpv **input section** enabled while the
   menu is up and disabled when it is not — which is exactly what the
   renderer already does for `mpvtk_mouse` / `mpvtk_thumb`. Python bindings
   torn down afterwards remain the fallback for the no-lua case.
2. **SyncPlay needs to schedule player events.** There are already *two*
   paths, and only one of them is the binding:

   * the **direct** one — `_on_pause_key` -> `toggle_pause` -> `set_paused`,
     which is SyncPlay-aware at the top (`if self.syncplay.is_enabled() and
     not force: pause_request()/play_request()`) and never touches mpv's
     `pause` at all in a group;
   * the **defensive** one — `_observe("pause", self._on_pause_change)`,
     which exists precisely for the pauses we did not initiate: a user's own
     mpv binding, the classic OSD, an external mpv, a script.
     `pause_ignore` is what keeps the two from double-reporting.

   So dropping the binding would not lose SyncPlay support — the observer
   catches it. **But the two are not equivalent, and the difference is
   visible.** In a group, the direct path never lets mpv unpause; the
   observer path lets it unpause and then calls `set_paused(True, True)` to
   force it back while the group is asked. That is a brief local
   unpause-then-repause flicker on every play in a SyncPlay session.

   **[iw]**: "it's not ideal, worth capturing when using syncplay." So that
   is the answer rather than a question: `space` stays claimed **while a
   group is active** and is mpv's the rest of the time. Which is the same
   shape as everything else here — claim a key while something needs it,
   release it otherwise — and makes SyncPlay one more claimant rather than
   a reason to hold the key permanently.

### The three options — and a fourth

**[iw]** framed it as:

1. keep our bindings and let the user change them (today);
2. drop them and behave differently when SyncPlay is on — *"confusing for
   user"*, and rightly: a key that means one thing alone and another in a
   group is worse than either;
3. read the user's config and make decisions about it — which is where
   PR #547 was heading, and **[iw]** pushed back at the time because it was
   "a lot of surface to cover for what was then a codebase that didn't have
   any help from AI to maintain".

The objection to (3) is still the right one, and AI help does not answer it:
the cost was never *writing* an input.conf parser, it is **being right about
someone else's runtime semantics forever** — sections, profiles, modifiers,
`ignore`, script bindings, and mpv's own precedence between them, which mpv
may change. That is the same argument as the notes on `tools/msgfmt.py`
matching gettext and on the SyncPlay port ("a port is a belief about someone
else's code that has already been wrong twice here").

**But there is a fourth option that gets (3)'s answer without its surface:
don't read the config — ask mpv.** The `input-bindings` property is the
*resolved* set, and it is already exactly what is needed. Measured, with a
custom `input.conf` containing `SPACE cycle mute`, `p cycle pause`,
`LEFT ignore`:

```
SPACE  [('cycle pause', 'default', 0), ('cycle mute', 'default', 7)]
p      [('cycle pause', 'default', 0), ('cycle pause', 'default', 7)]
LEFT   [('seek -5',     'default', 0), ('ignore',     'default', 7)]
f      [('cycle fullscreen', 'default', 0)]
```

192 entries, each with `key`, `cmd`, `section` and `priority` — the default
*and* the user's override, with the priority that decides between them. So
"has the user rebound space?" and "does anything still reach `cycle pause`?"
are lookups, not a parser. No precedence model of our own, nothing to drift
from mpv, and it costs one property read at startup.

That is what makes the audit worth doing now rather than in 2021.

### The design that falls out **[iw]**

With `input-bindings` available, the three-way choice resolves into one
shape:

**1. Intercept semantically, and only where SyncPlay needs it.** "We could
semantically intercept stuff *only when we need it for syncplay*" — so the
claim is scoped to the state that needs it rather than held for the life of
the process, and `input-bindings` is how we know what we would be taking
over.

**2. Drop our default bindings; migrate the ones the user changed.** "We
could just drop default bindings unless the user touched those config
options, at which point it probably makes sense to do a one-time migration
of those configs to input.conf." `SettingsBase.__fields_set__` already
records which keys came from the file rather than the class default, so
"did the user touch this?" is answerable exactly, and `CONFIG_VERSION`
(currently 2) is the existing one-time-migration mechanism.

**Only mpv's own keys are in scope** **[iw]**: "keys that name a shim action
can just stay as-is, only interested in dropping the needless interception
of MPV's default bindings." So `kb_watched`, `kb_next`, `kb_stop` and the
rest of the shim's own verbs keep their Python bindings and are not part of
this at all — which removes the `script-message` migration the previous
draft proposed, and most of the surface with it. What migrates is the small
set that only ever duplicated something mpv already does: `kb_pause`,
`kb_fullscreen`, and the seek/menu arrows.

**3. The legacy menu keeps its bindings, and only while it exists.** "The
old menu can just become temp bindings only used when the legacy osd menu
is enabled and ripped out when not." That is the cleanest part: the arrows,
ENTER and ESC stop being global and become the menu's own, installed on
show and removed on hide — which is what makes them available to mpv the
rest of the time without any conditional behaviour for the user to notice.

### The shape of the audit

**The test is not "does mpv bind this key" but "does our binding MEAN the
same thing as mpv's"** — which is the distinction that puts `q` on the
opposite side to `space` despite both being mpv defaults. `kb_stop` is
bound to `q`, and mpv's `q` quits; ours **returns to the browser**. That is
a different verb wearing the same key, so it stays intercepted **[iw]**,
and dropping it because "mpv already binds q" would be exactly the wrong
reading of this exercise.

And the answer to the complaint behind PR #547 is a config decision, not a
mechanism **[iw]**: "if someone doesn't like *our* custom bindings they can
disable them or remap them to something else, that's a legitimate mpv shim
config decision." What is *not* legitimate is silently swallowing a key
whose meaning we did not change — which is the whole of what this removes.
The gain is that a user's own `input.conf` is intercepted only where we
genuinely need to intercept it.

For each of the seventeen `kb_*` settings, decide which of three it is:

* **mpv's already** — drop the binding and let the default fire (`f`, and
  arguably `space` once the point above is confirmed);
* **claimed while a UI owns the screen** — an input section or
  `claim_keys`, released otherwise (the menu's arrows/ENTER/ESC);
* **genuinely ours** — either a shim concept mpv has no opinion about
  (`w`/`u` watched, `<`/`>` queue) or, like `q`, a key mpv *does* bind
  where we deliberately mean something else.

The regression surface is real and spread across three input owners (mpv
defaults, `menu.py`, the renderer), which is why the first deliverable is
that list rather than a diff.

### The footgun to design around **[iw]**

"We need to make sure we don't break an already existing input.conf that
contains sections."

Which is a real hazard and not an obvious one. mpv's `input.conf` sections
are `[name]` headers, and **everything after one belongs to it until the
next** — so appending migrated bindings to the end of a file that has any
section puts them *inside* that section, where they apply conditionally or
never. The bindings would be written, the file would look right, and the
keys would silently not work.

So a migration writes **before the first `[`**, never at the end, and says
so where it writes. Note this is the shim's *own* config directory —
`mpv_options` sets `config_dir` to `conffile.confdir(APP_NAME)` and
`_init_mpv` seeds `input.conf` there — so it is a file the app already
owns, which makes writing to it reasonable and makes not corrupting
somebody's hand-edited sections the whole of the obligation.

The same trap is already handled once in this batch, in
`mpv_options.hwdec_pinned_by_config` (#12): it stops scanning at the first
section header, because a `hwdec` inside a conditional profile is not a
pin. Worth reusing that reading rather than writing a second, differently
wrong parser. Two things to be careful of: `kb_*` are
user-editable settings, so "drop the binding" has to keep honouring a value
somebody has already customised; and the no-lua path is not hypothetical —
it is what CLI mode *is*, so every key that moves to a section needs its
Python fallback kept and tested, not assumed dead.

### The audit — the list, per key

Measured, not read off the source: `input-bindings` from a real mpv with no
config (`vo=null`, `config=False`), against the seventeen bindings
`_bind_mpv_handlers` installs. Three verdicts, as above — **mpv's**,
**claimed**, **ours**.

| setting | key | mpv's default | what we do with it | verdict |
|---|---|---|---|---|
| `kb_stop` | `q` | `quit` | return to the browser | **ours** — a different verb wearing mpv's key **[iw]** |
| `kb_prev` | `<` | `playlist-prev` | previous item in the *Jellyfin* queue | **ours** — mpv's playlist is not our queue (we load one file at a time), so its default does nothing useful |
| `kb_next` | `>` | `playlist-next` | next item in the queue | **ours**, same reason |
| `kb_watched` | `w` | `add panscan -0.1` | mark watched | **ours** — a shim verb on a key mpv binds, like `q` |
| `kb_unwatched` | `u` | `cycle-values sub-ass-override` | mark unwatched | **ours**, same |
| `kb_menu` | `c` | *unbound* | open the OSD menu | **ours**, and free |
| `kb_kill_shader` | `k` | *unbound* | drop the shader profile | **ours**, and free |
| `kb_debug` | `~` | *unbound* (mpv binds `` ` ``, a different key) | **`pdb.set_trace()`** | **ours**, free — but see below |
| `kb_menu_esc` | `esc` | `set fullscreen no` | menu back → in-window UI back → `set fullscreen no` **and** `fullscreen_disable = True` | **claimed** — the fall-through already *is* mpv's default plus one shim side effect |
| `kb_menu_ok` | `enter` | `playlist-next` | `menu_action("ok")` — **unconditionally** | **claimed**, and the worst offender: see below |
| `kb_menu_left` | `left` | `seek -5` | menu nav, else `kb_seek` | **claimed** — and the default `seek_left` is `-5` |
| `kb_menu_right` | `right` | `seek 5` | menu nav, else skip-intro, else `kb_seek` | **claimed** — `seek_right` default `5` |
| `kb_menu_up` | `up` | `seek 60` | menu nav, else skip-intro, else `kb_seek` | **claimed** — `seek_up` default `60` |
| `kb_menu_down` | `down` | `seek -60` | menu nav, else `kb_seek` | **claimed** — `seek_down` default `-60` |
| `kb_pause` | `space` | `cycle pause` | menu OK, else `toggle_pause` | **mpv's**, claimed while a SyncPlay group is active **[iw]** |
| `kb_fullscreen` | `f` | `cycle fullscreen` | `toggle_fullscreen` → `set_fullscreen(persist=True)` | **mpv's**, *conditional* — see below |
| — | `i` / `I` | `script-binding stats/…` | forwards to mpv's stats, or our overlay, or swallows it | **claimed** — already conditional on what is on screen |

### Four things the table turned up

**1. The four arrow keys have mpv's own numbers in them.** `seek_up`,
`seek_down`, `seek_right`, `seek_left` default to `60`, `-60`, `5`, `-5` —
which is `seek 60`, `seek -60`, `seek 5`, `seek -5`, character for
character what mpv binds. So with default settings, no menu on screen, no
intro segment and no SyncPlay group, these four bindings exist to
reimplement mpv's arrows exactly. That is the largest single piece of
"needless interception of MPV's default bindings" in the set, and it is
four of the seventeen.

They are still **claimed** rather than **mpv's**, because three things
genuinely need them — the OSD menu, `skip_intro_on_seek`, and SyncPlay
(a seek in a group has to be broadcast, exactly as a pause does) — and
because a user who has customised `seek_left` has to keep getting their
value. `__fields_set__` answers that last one exactly.

**2. `enter` is taken unconditionally, and its fallback is the wrong menu.**
`_on_menu_ok` is the one handler here with no `is_menu_shown` guard: it
always calls `menu.menu_action("ok")`, which for a hidden menu means
`show_menu()`. So ENTER **opens the legacy OSD menu** — under the mpvtk UI,
a menu that is not the one the user is looking at — and mpv's
`playlist-next` never fires. Every sibling handler (`left`, `right`, `up`,
`down`, `esc`, and `space`) checks first. This one is a plain inconsistency
rather than a design decision, and it is the single clearest fix in #16.

**3. `f` is a real drop candidate, conditional on one observer.** It looks
like a pure duplicate of `cycle fullscreen` and is not: `toggle_fullscreen`
goes through `set_fullscreen(persist=True)`, which is what remembers the
choice into `fullscreen` / `browser_fullscreen`. Dropping the binding would
lose that — *unless* the intent is recorded by observing mpv's `fullscreen`
property instead, which is the same defensive-observer shape SyncPlay
already uses for `pause`. And unlike `pause` there is no cost to it: the
observer path's flicker exists because a group has to be *asked* before a
play is allowed, and nothing has to be asked about fullscreen. Do the
observer, drop the binding, and a user's own `f` mapping is theirs again.

**4. `~` runs `pdb.set_trace()` in a shipped application.** Out of scope for
the keybinding question and worth its own line anyway: the default binding
for `kb_debug` drops a released build into an interactive Python debugger,
which on a GUI launch has no console attached to prompt at — so the key
freezes the player rather than doing anything visible. It does not conflict
with mpv (mpv binds `` ` ``, not `~`), so this is not a *reason* to change
it, only the audit noticing it.

### What is left to decide

Nothing blocking, but the order matters: **(2) first**, then **(3)**, which
is self-contained, and only then the claimed-section work for the arrows and
ESC, which is the part with the real regression surface and needs the no-lua
fallback kept and tested rather than assumed dead.

**(2) is done.** It was not quite the one-line guard it looked like: the
guard is easy, but *what ENTER should do instead* had design content. **[iw]**
settled it — "ENTER doesn't need to open the menu, `c` is fine for that" — so
it opens nothing under either OSC and only confirms a menu that is already
up. Swallowed rather than left to mpv, whose `playlist-next` on our
single-file playlist does something that depends on `keep-open` and is not a
behaviour to inherit by accident. `tests/test_remote_menu_commands.py:EnterKeyTest`.

**`kb_debug` is gone**, setting and handler. **[iw]**: "isn't needed anymore,
I haven't used it in literal years." It dropped a released build into `pdb`
with no console attached to prompt at, so the key froze the player; it was
also the one binding the integration sweep had to skip, for exactly that
reason. An unknown key left behind in someone's `config.json` is ignored by
`SettingsBase.from_dict` (it iterates the declared fields) and disappears on
the next save, so no migration is needed.

**The arrows and `f` are agreed and unstarted**, and **[iw]** has settled
the shape. "Agree arrow keys, ENTER, and fullscreen removal from default
bindings are a huge improvement for end users, a lot of people get annoyed by
those bindings."

They are not deletions. Three mechanisms, and the first is the one the other
two lean on.

#### A. The semantic key sweep — one mechanism, not three

**[iw]**: "that becomes a generic case of *the app needs to sweep the user's
keybinds and temp-intercept them when syncplay is enabled*."

So: ask `input-bindings` which keys are currently bound to the commands we
care about (`cycle pause`, `seek …`, `cycle fullscreen`), install forced
bindings on **those keys, whatever they are**, and have each one do our thing
*and then run the command the user had bound*. Claimed as a group, released
as a group.

That is strictly better than hard-coding `space`/`f`/arrows, and it is why it
generalises:

* it follows a **remapped** key. Somebody who moved pause to `p` gets
  SyncPlay-aware pause on `p`, which our fixed `kb_pause` never gave them;
* it **preserves meaning**, because we re-issue the command they bound rather
  than substituting ours. That is the whole complaint behind PR #547 answered
  — we intercept only where we genuinely need to, and even there the key
  still does what their config says;
* it needs **no precedence model of our own**. `input-bindings` is the
  *resolved* set, so mpv has already decided.

**SyncPlay claims seek and pause while a group is active** and releases when
it is not, which is what makes dropping the permanent arrow bindings safe: a
seek in a group has to be broadcast exactly as a pause does, and unlike
`pause` there is no defensive observer to catch one we did not initiate.

**Fullscreen uses the same shape rather than an observer.** **[iw]**:
"`set_fullscreen(persist=False)` exists precisely so the app's own changes
don't count — yeah that makes sense… Might be best to just intercept it in
the same shape as the keyboard nav in syncplay, `pause_ignore`-shaped things
are bug factories." Which is right: an ignore flag is a second piece of state
that has to be set and cleared around every self-initiated change, and every
path that forgets is a silent mis-record. Interception has no such state —
**a fullscreen change that came through our binding is user-initiated by
construction**, and one that did not is ours. Nothing to reset, nothing to
race.

#### B. The OSD menu binds its own keys, while it is open

**[iw]**: "ideally we use listeners bound only while the menu is up that uses
the arrow keys, and it tears down the intercepts when the menu is closed."

Installed on show, removed on hide. That is what lets the arrows, ENTER and
ESC belong to mpv for the whole of the time the menu is not up, with no
conditional behaviour for the user to notice — and it is the same lifecycle
the renderer already runs for `mpvtk_mouse` / `mpvtk_thumb`.

#### C. A one-time migration to input.conf, minus the ones they parked

**[iw]**: "we wanted to do a one-time migration of the user's old mpv shim
settings to input.conf before we drop the config options, unless of course
they set the config options to null, that means they were probably parking
our nav interception away and eating the penalty."

So the rule is `__fields_set__` **and** a non-empty value: touched *and* not
cleared. Someone who set `kb_menu_left` to null was giving the key back to
mpv the only way we offered, and re-binding it during a migration would undo
the exact thing this change is for.

That rule needed #22 first — until the settings became `Optional[str]`, a
`null` in the config arrived as the string `"None"` and the value could not
say what the user meant.

Two hazards already established: the migration writes **before the first
`[`**, never at the end, because everything after a section header belongs to
it (`mpv_options.hwdec_pinned_by_config` reads the same rule for `hwdec`);
and `CONFIG_VERSION` is the existing one-time mechanism.

---

## Hand-testing round 1 — what [iw] found

Confirmed working by hand: the header artwork (#7), the media info and
playback info screens (#10/#11), **Delete from Disk actually deleting** —
Live TV recordings on the QA server, from both the context menu and the
detail page — window dragging in mpv's modality (#1, the one thing Xvfb could
not answer), hwdec "Only above 1080p" reading `no` on a 1080p Hi10 file and
`vulkan` on a 4K HDR one (#12), previous-from-Next-Up walking ep4 back to ep1
and correctly declining under SyncPlay (#6), the shader suspension applying to
video, not to comics or photos, and coming back afterwards (#13), and the
lookahead fetching 8 in one batch (#5).

### One real bug, and the test that should have caught it

**The list underneath a deleted detail page did not refresh.** `go_back()`
lands on the grid, and `_land_back` re-reads only Home and two special cases
— everything else keeps the items it was loaded with. So the deleted tile was
still on screen, and pressing it 404s.

Fixed by flagging the route (`_deleted`) before going back, which `_land_back`
now treats like the playlist-editor case: pop the cached items and reload.
Flagged rather than detected there, because only the page that deleted knows —
a detail screen is left for a dozen reasons and re-reading on all of them
would refetch a grid every time somebody looked at a film and came back.

The existing test asserted **only that we left the page**, which is precisely
why it passed against the bug. That is the "assert the property, not the
mechanics" rule failing in its usual direction: the easy half of the
behaviour was checked and the half that matters was not.

### ...and a second bug, found by pulling on a log line

**[iw]** verified #5 by watching the log, saw the per-pass cap fire at 8, and
then noted that disabling and re-enabling downloaded **two more**. Chasing why
turned up a real defect in the hysteresis, unrelated to the two extra items
(which were legitimately Next Up entries — that source has no hysteresis).

`_upcoming_held` counted **every held episode of the series**, not the ones in
the window. The issue says "at least the minimum number of *upcoming*
episodes", and *upcoming* is the word that does the work: someone holding
twenty old episodes of a series was above any minimum for ever, so that series
was **never topped up again** — with no downloads and no error to show for it.
[iw]'s own test did not hit it because the eight they held were exactly the
upcoming ones.

Fixed by asking the server for the window first and intersecting: `_held_ids`
returns the ids we hold, and the caller counts how many of *those* are in the
window. `fill` already skips items in the catalog, so the whole window is
handed over and the ones we have cost nothing.

**This costs nothing extra against master, which is worth stating plainly
because the first draft of this note claimed otherwise.** Master already
called `get_episodes` once per followed series per pass, unconditionally; so
does this, with `limit` set to the maximum instead of the flat window. The
buggy intermediate version *skipped* that call when it believed a series was
stocked — so the "saving" I described existed only relative to my own broken
code, and it was bought with the wrong answer.

The one genuinely new piece of work is the `db.list(series_id=…)` inside
`_held_ids`, and it is gated on the window being configured: with the advanced
settings unset it never runs. It is also a local SQLite read, not a server
request.

The per-pass log line was corrected at the same time: it fired on
`queued >= cap`, which is also true when a pass fills exactly to the limit
with nothing left over, and then promised a "rest" that does not exist. It now
fires only where the loop actually broke with a candidate in hand — which
matters because that line is how the feature is verified by hand.

### #9 did not work at all — and the reason is instructive

**[iw]**: "downloaded an episode, watched 30% in, went offline using firejail,
its progress was at 3s — note that I played and then immediately backed out."

The method was right and **never called**. `record_offline_progress` was made
to write the catalog whether or not there is a server; all *three* of its call
sites were gated on being offline:

* the periodic timeline tick was an `elif` on "no client";
* `send_timeline_stopped` required `client is None and video.client is None`;
* `_report_stopped_offline` required `video.client is None`.

So online, the method that had been fixed was never reached. Ungated all
three. The stop paths are the ones that mattered for the reported case: "played
and immediately backed out" never reaches the 30-second periodic tick, so the
stop is the only chance to record anything.

**The tests were at the wrong layer**, which is why they passed. They exercised
`record_offline_progress` directly and proved it writes locally when online —
true, and useless, because nothing called it. The new ones are at the
**caller**: they assert `send_timeline_stopped` and `_report_stopped_offline`
reach it with a server present.

Two details worth keeping. `_report_stopped_offline` runs on a daemon thread
during teardown and is *handed* its video because `self._video` may already be
None — so its own record is sometimes the only one, and its test sets
`_video = None` to isolate that rather than watching the delegate do the work.
And both it and `send_timeline_stopped` can record for one stop; that is
harmless, because `db.update_userdata` is advance-only and idempotent for the
same position.

The name `record_offline_progress` is now a misnomer — it records progress, and
only the *replay queue* half of it is offline-only. Left alone because
`hasattr(video, "record_offline_progress")` is the duck-type check that tells
an `OfflineVideo` from a `Video` in five places; renaming it is a tidy-up for
its own commit, not a rider on a bug fix.

### One thing to attribute rather than fix

Shutdown reported a leaked thread parked in the apiclient's websocket
(`websocket/_app.py:run_forever` -> `dispatcher.read` -> `sel.select`). Nothing
in this batch touches the websocket: `SyncManager.get_client` is an injected
pure lookup (`clients.py` hands it in), so `_refresh_userdata` cannot start
one, and `stop_all_clients` already calls `client.stop()` on every client.
Almost certainly a pre-existing apiclient teardown issue — the watchdog exits
anyway, which is what it is for — but it wants its own reproduction rather
than a guess, and it is not this branch's to fix.

---

## Adversarial review round — what 42 agents found

Five reviewers by angle (concurrency, pure-function correctness, browser
footguns, renderer/Lua, server contract), then **two independent refutation
passes per finding**, then a synthesis. **18 candidates, 14 survived** —
three of which were the same `_play_media` finding from three reviewers, so
twelve distinct. The ones acted on:

### The serious one: `_play_media` ran unlocked for eleven commits

`@synchronous("_lock")` was on `_play_media` on master. `faf129fd` inserted
`_forced_hwdec` **between the decorator and the def**, so the decorator moved
to the helper and the whole of a playback start — `loadfile`, the duration
wait bounded only by `playback_timeout`, and every assignment after it — ran
with no lock on every external entry path.

That invariant is load-bearing and is stated in at least five places:
`run_action`'s non-blocking fast path is built on it, and so are
`cancel_load`, `retry_failed_playback`, `gateway/base.py` and
`player_window.reset_picture_view`'s guard. Concretely: a browser Stop would
take the free lock and run *inline* mid-start, clearing `_video` and
returning to the library, and the start would then finish and resurrect
itself.

**Nothing failed.** The suite passed, the app worked, and the damage was a
race under timing nobody reproduces on purpose. `tests/test_player_locking.py`
now asserts both the source *and* `__wrapped__` for eight methods, and
reproducing the drift fails with "_forced_hwdec is holding the lock that
belongs to _play_media".

The lesson is narrow and worth keeping: **inserting a method directly above
an existing one silently steals its decorator.** Nothing in Python or in a
behavioural test notices.

### Also fixed

* **`--disable-hwdec` was defeated by a shader profile naming a decoder** —
  the recovery path for "hardware decoding stops the window opening" did not
  work in the one case it exists for. It now outranks a profile, and the pack
  is refused its `hwdec` under the flag as well as under an mpv.conf pin.
* **Deleting a Series or Season left every episode on disk** —
  `delete_download(item_id=<series id>)` matches no row. Shares
  `remove_download`'s type dispatch now.
* **The Skip Intro button was unclickable in mpv modality** (three reviewers,
  independently) — its *mouse* half lives in `mpvtk_phud_click`, which that
  mode does not install. Bound for the seconds the button is up, dragging by
  `begin-vo-dragging` meanwhile.
* **`transcode_reasons` / `direct_path` were never reset**, so a quality
  change or a forced-transcode retry reported the previous negotiation.
* **The inset poster was fetched at scale²** on HiDPI — `raster()` applied to
  an already-physical box.
* **Detail-page Delete was not gated on offline**, unlike the tile menu.
* **The banner recomposed on every repaint** — `bitmap()` takes a callable
  and calls it only on a miss; the loaded path passed an eager image, which
  re-cropped the backdrop, re-drew the heading and (since #7) re-blurred a
  full-canvas drop shadow every frame.
* **A same-codec re-encode was reported as a Remux.** Measured: a 100 kbps
  ceiling on an h264 file gives target `h264` with reasons
  `ContainerBitrateExceedsLimit`, so the codec comparison alone calls it a
  copy. The reasons now outrank the comparison.

### Refuted

A claim that a 500 from the delete endpoint is reported as success: the
apiclient calls `raise_for_status()`, so it propagates and `edit()`'s failure
path fires.

### Not acted on

The userdata pull asking for the apiclient's default field set. Real but
small, and `UserData` rides `EnableUserData` rather than `Fields`, so the
tightening is a `fields=""` experiment rather than a fix. Left for the next
pass.

### One that was missed, and how

**[iw]** asked whether every review finding had actually been fixed. Checking
the workflow's own output rather than this section — `result.confirmed`, 14
entries — turned up **one that survived verification and was never written
down here, so it was never fixed**: `on_dbl` and `on_rclick`'s bare-video
fall-throughs fire while a modal or a dropdown popup is open.

`node_at()` answers with clickable *scene* nodes. A modal's body, a
dropdown's popup rows, a context menu and the textbox copy menu are none of
those, so it returns nil **on** them — and "no node" is what both handlers
read as "bare video". `on_mouse_down` checks all four before its own
bare-video branch; these two checked none. Concretely, with the HUD up: a
double click inside the Playback Info panel (which this batch introduced —
the HUD's first modal) full-screened the window under it, and in mpv
modality a right click on it toggled pause. Fixed in both, inlined rather
than given a helper because renderer.lua's main chunk is at Lua's 200-local
ceiling, with five renderer tests that all fail without the guards.

Worth recording *why* it slipped: the synthesis prose was written from the
findings I had in front of me, and this section was then written from the
prose. Neither step counted. The list of what survived was a machine-readable
field in the task output the whole time — **check the output, not the
summary of it.**

---

## Round 3 — three more from **[iw]**

## 17 — The unplayed-count chip could not hold three digits

**Fixed.** `strips._paint_decorations` drew the chip as a fixed `_px(26)`
rounded rect centred on the badge column. Measured at the default
`badge_size` of 14: `"9"` is 10px wide, `"12"` is 19, `"123"` is **29**. So a
three-digit count was wider than the chip carrying it and hung out of both
ends, and two digits already sat on 3px of padding where one digit had 8.

Sized from the text now, jellyfin-web's way — its `.countIndicator` is
`padding: 0 .5em` over a min-width — and pinned by its **right** edge, since
growing from the centre walks a wide chip off the corner of the card.

`tests/test_strip_count_badge.py` measures the composited pixels rather than
the arithmetic, because the arithmetic is what was wrong: the old code
computed a width too, it just did not compute it from the text. It asserts
**padding**, not containment: at exactly three digits the ink's antialiased
edge is dim enough that a strict inside/outside test reads the overflow as
contained, which is the case the bug report is about.

## 18 — "Follow display" and the Genres screen

**[iw]** reported the UI under Genres flashing oddly with `ui_scale` on
"Follow display", not reproducible after setting a fixed scale and putting it
back, and guessed caching or scroll jank. Traced, not reproduced. Three
things came out of it.

### It is not layout, and now something checks that

Genres, Favorites and By-Name were excluded from the DPI matrix as screens
the fake source had no data for. That stopped being true when the harness
grew `genre_rows` / `favorite_rows` / `byname_rows` — so the screen named in
the report had never been laid out at a scale by anything in the suite. They
are in now, over several rows with names of real length (the tile captions
are baked into the strip bitmap, so what these screens put on the page is
their row *headings*, and one heading cannot overflow anything). All seven
scales, every window: clean.

### It cannot be a live rescale

`scaling._scale` is resolved once, on the renderer's `ready` event, and never
moves — the setting says "takes effect after a restart" and means it. So the
whole family of "half the frame is at the old scale" is out by construction.

### What the measurement did find: Genres has no windowing

`GenresPage.render` builds a `tile_row` for **every** genre, visible or not,
and each row composites a full-width strip bitmap. Twenty-five genres in a
1920x1080 window, measured through the real `StripStore`:

| scale | strip bytes held |
|-------|------------------|
| 1.0   | 42.0 MB          |
| 1.5   | 94.7 MB          |
| 2.0   | **168.1 MB**     |

`StripStore.MAX_BYTES` is 128 MB, and `TIGHT_MAX_BYTES` — what a machine
short of RAM gets — is 32 MB. At the `max_genres` cap of 40 rows, 2x comes to
roughly 270 MB.

The bound cannot defend itself here, and that is the part worth writing down.
Every row is touched by every build, so every entry is `_protected`, so
`_trim_to` stops dead at the LRU head and frees nothing. The budget is not
enforced and then exceeded; it is *unreachable*. It is also exactly a
scale-squared effect, which is the correlation in the report.

That is consistent with the symptom without being a reproduction of it: on
the jsonipc backend these bytes are files in a scratch directory that is
RAM-backed wherever one exists, and a write that fails makes `_compose`
raise. `strips.strip` logs `strip composite failed` and the row stays a
placeholder — a row of blank cards — until the next frame retries. That is a
flash, on the screen named, four times more likely per unit of scale².

### What a width cap would buy — measured, 2026-08-09

**[iw]**: "It might be worth making these start off as width-constrained
bitmaps until they're actually interacted with. Lazy loading them would be
really annoying."

Measured before implementing, and the answer is that it does not reach the
screen that was reported. A carousel strip is `n * tile_w + (n-1) * gap`
logical px wide; capping it at the viewport saves the part hanging off the
right edge:

| screen | tiles/row | rows | 1280px | 1920px | 2560px | held at 1x / 2x |
|---|---|---|---|---|---|---|
| Genres | 10 | 25 | 23% | **0%** | **0%** | 42 MB / 168 MB |
| Favorites | 24 | 6 | 68% | 52% | 36% | 24 MB / 97 MB |
| By name | 24 | 26 | 68% | 52% | 36% | **105 MB / 422 MB** |

**A genre row is ten tiles — 1626 logical px — which already fits a 1920
window.** So on the machine the report came from, a width cap frees
nothing at all; Genres' 168 MB is entirely row *count*, which is a vertical
problem. It saves 23% at 1280, which does not bring 2x under the 128 MB
budget either.

Two things did come out of measuring it, and both are worth more than the
original item:

**By name is worse than Genres and nobody had looked at it.** 26 rows of 24
tiles is 105 MB at 1x — inside the budget by a hair — and **422 MB at 2x**,
which is over three times it. Width-capping halves that to ~202 MB, still
over. It is the worst screen in the app for this and it is not the one that
got reported.

**A width cap is still the right thing for the wide rows**, where it is
worth 36–68%, and it is the only measure that touches them without
windowing. It just is not the fix for #18.

**[iw] on the numbers**, and this is the design the measurement points at:
"our test library is synthetic, genres very likely *does* have a lot of
content outside of the visible rows on real servers. That being said, the
test library still being over makes a very strong argument for lazy loading
and garbage collection on the same page, but **only when ram is
constrained**."

Worth being precise about which axis a real server moves, because it is not
the one a width cap addresses: `get_genre_sections` fetches `limit=10` per
genre regardless of library size, so a real genre *row* is still ten tiles.
What a real library has more of is **genres** — up to the `max_genres=40`
cap, against the 25 measured here, which is 40/25 more of exactly the term
that is already the problem. Real servers are worse vertically and
identical horizontally.

Gating on memory pressure also has its hook already: `set_memory_pressure`
is asked per screen change, and `TIGHT_MAX_BYTES` (32 MB) is the budget a
constrained machine gets. So "paint everything until RAM says otherwise"
is a state this cache already tracks — what is missing is the page
consenting to drop rows, which is what makes `_protected` able to free
anything (today every row is touched by every build, so nothing is
evictable and the bound is unreachable, not merely exceeded).

Neither the cap nor windowing is landed here. The cap does not move the
reported screen, and windowing is the approach [iw] steered away from, so
which of the two to build (or both) is [iw]'s call with the numbers above
rather than the audit's.

### The fix, deliberately not taken in this batch

The primitives are already here — `row_window`, `virtual_window`,
`list_virtual` — but they serve *grids* and *tables*. A Column of
`tile_row`s is the one shape with no windowing, and there are four:
`home` (bounded at ten sections, so it is fine), `favorites`, `byname`, and
`genres` (bounded at forty, so it is not). Row heights are known before the
rows are built, so the window is computable the same way `grid_of`'s is.

It is a change to the scroll/build path, which is not something to land
beside a chip-width fix. Sequenced against #15 and #16 by **[iw]**.

## 19 — Filters: no Language, and not only Language — **surveyed**

**[iw]**, reported by a user: jellyfin-web can filter by language and we
cannot. Surveyed rather than patched, as **[iw]** framed it — "I wonder what
other filters we haven't implemented? We might end up wanting to add an
advanced filters modal or something eventually." The answer is *most of
them*, and the reported one has a problem underneath it.

### The finding that changes the job

**Corrected on 2026-08-09.** The first pass measured this wrong and
concluded the server ignores `AudioLanguages`. It does not. Two separate
facts were being seen as one, and both are now measured against two live
servers -- the v12 source build on :8096 and a Jellyfin **10.11.11**
container on :8097 (`stdjflib container --image jellyfin/jellyfin:10.11.11`),
same library, same query:

| server | endpoint | all | `AudioLanguages=eng` | `=zzz` | `Filters2` offers |
|---|---|---|---|---|---|
| 10.11.11 | `/Items` | 1131 | 1131 | 1131 | Genres, Tags |
| 10.11.11 | `/Users/{id}/Items` | 1131 | 1131 | 1131 | " |
| 12.0.0 | `/Items` | 1131 | **1108** | **0** | + AudioLanguages, SubtitleLanguages |
| 12.0.0 | `/Users/{id}/Items` | 1131 | 1131 | 1131 | " |

**1. The parameter is v12-only.** `audioLanguages` / `subtitleLanguages`
landed on Jellyfin master in `2b7f641163 "feat: language filters for
subtitles and audio"` (2026-05-10) and appear in no 10.10 or 10.11 tag.
`MediaBrowser.Model/Querying/QueryFilters.cs` at `v10.11.11` has only
`Genres` and `Tags`. So on every *stable* server there is no server-side
language filter at all, and jellyfin-web's own control cannot work there
either.

**2. Our endpoint could never see it anyway.** A follow-up commit,
`068b3fd58d "remove language filters from old Items endpoint"`, makes the
legacy `Users/{userId}/Items` action pass `[]` for both arrays --
hard-coded, in the delegation to `GetItems`. Diffing the two signatures,
those two plus `indexNumber` are the **only three** of 88 parameters the
legacy route drops. The shim reaches it through
`get_user_items` -> `user_items`, which is every library grid in the app.

So the earlier "all three spellings inert" reading was one true observation
(the legacy endpoint drops it) generalized to a claim about the server that
is false.

**What this settles about the design.** jellyfin-web does not version-gate
its language filter; it renders the accordion only when
`/Items/Filters2` returns a non-empty `AudioLanguages`
(`FilterButton.tsx`, `!!filters?.AudioLanguages?.length`). That gate is
exactly right and needs no version knowledge, because the same server that
lacks the query parameter also returns no options: measured above, 10.11
answers `Filters2` with `{Genres, Tags}` and v12 adds both language lists
(`English (eng)`, `Undetermined (und)` for our Movies view; 24 subtitle
languages). Copy that: **offer the control iff the server offered options.**

**No apiclient change is needed**, which is worth knowing before anyone
opens a PR against it. Both halves are reachable through the generic
method that is already there, and both were checked against the live
server through an unmodified client:

    api.items(params={..., "AudioLanguages": "eng"})   # -> GET /Items
    api.items("/Filters2", params={...})               # -> GET /Items/Filters2

`API.items(handler)` is `self._get("Items%s" % handler, params)`, so the
handler argument covers `Filters2` for free. The trap is the *named*
helper: `get_filters()` calls `Items/Filters` and its own docstring cites
`GetQueryFiltersLegacy` — there is a convenience method for the wrong
endpoint and none for the right one. Adding `get_filters2()` upstream
would be a nicety, not a blocker.

The remaining work is therefore known and unblocked: move the filtered
query off `Users/{id}/Items` onto `/Items` (which takes `userId` as a query
parameter), read the option lists from `Filters2` rather than the older
`Filters`, and gate the UI on the lists being non-empty.

### Parity, in full

Web's modern filter panel is thirteen categories
(`src/apps/modern/features/libraries/components/filter/`), against our five.

| web filter | API parameter | us |
|---|---|---|
| Status | `filters=IsPlayed/IsUnplayed/IsFavorite/IsResumable/Likes` | **partial** — Unplayed and Favorites only |
| Genres | `genres` | **yes** |
| Years | `years` | **yes** |
| (alpha picker) | `nameStartsWith` / `nameLessThan` | **yes** |
| Features | `hasSubtitles`, `hasTrailer`, `hasSpecialFeature`, `hasThemeSong`, `hasThemeVideo` | no |
| Video quality | `isHd`, `is4K`, `is3D` | no |
| Video types | `videoTypes=BluRay/Dvd/Iso/VideoFile` | no |
| Official ratings | `officialRatings` | no |
| Tags | `tags` | no |
| Studios | `studioIds` | no |
| Series status | `seriesStatus=Continuing/Ended` | no |
| Episode | `isMissing`, `isUnaired`, `parentIndexNumber=0` | no |
| **Audio languages** | `audioLanguages` | no — **and see above** |
| **Subtitle languages** | `subtitleLanguages` | no |

**Sorts are nearly at parity** and are not the problem: web offers SortName,
DateCreated, PremiereDate, CommunityRating, CriticRating, OfficialRating,
DatePlayed, PlayCount, Runtime, Random and DateLastContentAdded — every one
of which we have — plus `ProductionYear` (we have `PremiereDate` and not
this) and the music-specific Album / AlbumArtist / Artist, which our music
library answers with its own screens. **No language sort exists in web
either**, so the report is about filtering.

### Shape

**[iw]**'s instinct was right: nine missing categories will not fit the
filter row, which already carries a sort, three filters and a shuffle. A
dialog is the shape — and the natural model is the one this codebase already
has for view settings, since these are per-library and jellyfin-web persists
them (`LibraryViewSettings`) exactly as it persists the view type the shim
already reads and writes through `view_prefs`.

Not started beyond this. The order that seems right:

1. **Establish whether `audioLanguages` works on stable 10.x.** It is the
   reported bug, and if the answer is no, the honest fix is to say so rather
   than to ship an inert control.
2. The cheap, certainly-working ones first — Features, video quality, Tags,
   Official ratings — which are plain booleans and lists on a request we
   already build.
3. The dialog, once there are enough of them to need one.

## 20 — People carousels are not keyed properly

**[iw]**, from hand-testing: the people rows spam "duplicate node id" errors.
Not started. A row id is built from the item id, and a person can legitimately
appear twice in one credit list (two roles, or the same name resolved to two
entries), so the collision is in how the row keys its tiles rather than in the
data. Captured here rather than fixed because it is a different screen from
anything else in this batch.

## 21 — A header with no backdrop showed a grey box

**[iw]**: "when no backdrop is available still show thumbnails/posters —
right now the backdrop short circuits the UI and we don't draw anything
except a grey box."

Fixed. `backdrop_node` had three outcomes and the third was a bare
`Box(bg=PLACEHOLDER_BG)`: artwork present → composed banner; artwork coming →
flat panel with the heading baked in **and the poster inset**; no backdrop →
grey. The middle one already did exactly what was wanted, and the no-backdrop
case simply never reached it — the whole thing sat under `if spec:`.

**The poster is still not stretched across the banner.** `backdrop_spec`
rejects that deliberately (a 2:3 poster cropped into a 2.67:1 strip is a
horizontal band through the middle of the key art, which reads as a rendering
fault rather than as missing artwork), and that judgement stands. What the
waiting composition does instead is draw the poster *inset at its own aspect*
over a flat panel, which shows the artwork without distorting it — so the
no-backdrop case now composes the same thing. Same call, same geometry, so a
header cannot move depending on which of the two it got.

`has_backdrop` became **`header_bakes_heading`**, because that stopped being
the question: the caller uses it to decide whether to draw the heading as
text underneath, and a poster-only header bakes its own. Leaving the name
would have meant a header with a poster drawing its title twice. The
source-scanning guard in `tests/test_banner_placeholder.py` follows the new
name, so a page that draws a backdrop header without asking is still caught.

## 22 — `kb_*` could not actually be set to null

Found while working out #16's migration rule, which turns on telling "the
user cleared this binding" from "the user never touched it".

`docs/configuration.md` has always said *"You can also set them to `null` to
disable the shortcut"*, and the settings were typed `str` — so a JSON null
coerced to the **string `"None"`**, and `_bind_key`'s guard (`if key is not
None`, documented as "an unset keybind is a supported configuration") could
never match. It looked like it worked only because no keyboard produces a key
named `None`: mpv bound something unreachable instead of binding nothing.

All fifteen are `Optional[str]` now, and `_bind_key` refuses `None`, `""` and
the literal `"None"` — the last because a config written under the old typing
holds it. `__fields_set__` plus the value is now a real answer to "did they
park this on purpose?", which is what the migration needs.

---

## Review round 2 — three agents over the 1,940 lines since the last one

Three reviewers by angle (the shader-override feature, input/renderer, browser
drawing), each asked to refute its own findings before reporting. **Seven
survived and all seven were real**; every one was verified here before being
acted on. Notably, three of them are defects *this batch introduced while
fixing something else*, which is the pattern worth naming.

### The one that invalidated my own test

`on_dbl`'s new guards were **dead code, and the file said so 140 lines
above them.** mpv delivers a double click as `mbtn_left`, `mbtn_left_dbl`,
`mbtn_left` — measured against a real mpv and written down in
`tests/lua/test_renderer.lua` — and that leading `mbtn_left` runs
`on_mouse_down`, which dismisses the textbox menu, the context menu and the
dropdown popup before returning. So by the time `on_dbl` ran, asking "is one
open?" always answered no. Only `modal_active()` survived, because clicking
inside a modal body dismisses nothing.

My tests fired `mbtn_left_dbl` **alone** — one event where the real device
sends three — so they passed against a guard that did nothing for two of the
three cases the commit message named. The fix is `state.pop_press`, recorded
on the press and read by the double click, because the question is "did a
popup consume this press?", not "is one open now?". The tests drive the real
three-event sequence, and the context-menu case (which had no test at all)
is covered.

### The shader feature, three ways

* **"Remember Last Used Profile" off made a pick last for zero items.**
  `menu_handle` loaded the profile, skipped the settings write because
  `remember` was off, then re-resolved the default scope from
  `shader_pack_profile` — the value it had deliberately not written. It was
  None, so the re-resolve unloaded what had just been loaded. Fixed with
  `session_default`: the default scope's value for this session, seeded from
  the setting and persisted only when asked. There is now one writer per
  scope (`set_scope_profile`), which is what the two menus had been
  duplicating badly.
* **`k` stopped working across an item boundary.** The escape hatch unloaded
  the profile; the next episode's resolve found the override and put it
  straight back. On the one key whose entire purpose is recovering from a
  profile that breaks playback. Now `suppressed` until the user picks again.
* **The scope menu was stale after a pick** — `back` popped the snapshot
  built before the change, so the per-scope values and the "in effect"
  marker were the old ones on the one screen where they had just moved.
  Both levels are rebuilt.

### The two costs that were not bounded

* **An offline play path could aim the Ancestors lookup at a dead server.**
  `OfflineVideo.client` is documented as possibly None, and `_client` could
  not tell "the caller passed nothing" from "the caller knows there is no
  client" — so it fell back to the *previous* item's client, which is
  precisely the wrong-server lookup the explicit hand-in exists to prevent,
  from inside `_play_media`, holding the player lock, with the apiclient's
  30s x 5 retry behind it. `ASK_PLAYER` is now the default and `None` is an
  answer.
* **A transient failure was cached as a permanent one.** One timeout meant
  the "This Library" row never appeared again for that series, and `force=True`
  could not retry it — so the user's natural retry (reopening the menu) could
  not work. A positive answer is still permanent; a negative one is retried
  when the menu asks.

### The browser, two ways

* **The no-backdrop fallback had no pending state.** `header_bakes_heading`
  answers from the *spec*, known on the first paint; the banner was gated on
  the decoded *image*. In between, the caller suppressed its heading and the
  banner had none — so the page drew no title at all, and if the fetch never
  succeeded, no title ever. Exactly what `backdrop_node`'s docstring says the
  pending composition exists to prevent, reintroduced one branch over. The
  two questions are answered from the same thing now.
* **Transparent channel logos were inset unplated.** The fallback made the
  header reachable for a `Program`, whose `image_spec` resolves to the
  channel logo — dark ink on transparency by broadcaster convention, invisible
  on `PLACEHOLDER_BG`. The guide's channel column plates the same file, so
  the two screens disagreed about it. `_banner_poster` goes through
  `logo_plate` now, which keeps #637's settings answered in one place.

### And one the reviewers found in my tests, not my code

Three poster stubs returned a bare `object()` as a "decoded image". That is
the fake-omits-a-field trap in CLAUDE.md almost verbatim: the moment
`_banner_poster` had to *look at* the artwork, those tests raised rather than
covering the new path. Real images now.

### Refuted, and worth recording

`_library_ids`' unsynchronised cross-thread mutation — the concern I flagged
going in — is genuinely harmless: reads and `d[k] = v` only, no deletes, so
the worst case is two threads making the same request once. `_profile_scopes`
cannot issue a request from the render path (`_build_state` returns early for
anything without a media source, and the play path has already warmed
everything else). `apply_for_item`'s early return is correct in every
combination. The file format round-trips and the two scope tables cannot be
confused. And the ENTER change lost no reachable path to the OSD menu.

## 23 — Migrate the string `"None"` in old configs

Found by the input reviewer, and it is the half of #22 that actually reaches
the affected users. Retyping `kb_*` to `Optional[str]` fixes *new* nulls; a
user who cleared a binding years ago has the string `"None"` on disk right
now, because that is what the old coercion produced and `save()` wrote it
back verbatim. That loads as a non-empty string, so the distinction #16's
input.conf migration turns on would still not exist for exactly the people it
is about. `CONFIG_VERSION` 3 normalises it.

## 24 — The library lookup belongs in the catalog, not on the play path

**[iw]**, on the offline finding above: "we should probably add the metadata
locally so the lookup can be done for the shader packs for downloaded media,
also we should avoid doing sync tasks while holding the player lock when
possible."

Both done, and together they mean **the play path now makes no request at
all**.

* **The catalog records it.** `downloads.library_id` (an additive column, the
  same mechanism `origin` and `completed_at` used) is written by
  `SyncManager._add_row` at download time — a path already doing network I/O,
  and keyed on the series so a season costs one request rather than one per
  file. `SyncDB.library_id` answers for an item id *or* a series id.
  Consulted **first, online too**: a downloaded item's library is
  authoritative, free, and answerable with the server away, which is exactly
  the case that was reaching for the previous item's client to ask a server
  it already knew was unreachable.
* **The play path reads caches only.** `apply_for_item` runs inside
  `_play_media`, which holds the player `_lock` for the whole of a playback
  start — and `run_action`'s non-blocking fast path is built on that lock
  being held. Anything still unknown is resolved by `_warm_library_later` on
  the action thread, which re-applies once it lands. The profile arrives a
  beat into playback rather than before it, which is fine for the one thing
  this is: shader profiles are applied to a *running* mpv, and switching one
  mid-playback is what the menu does. It only happens when a library override
  exists and the item is neither downloaded nor already cached.

The menu still asks the server synchronously (`force=True`), which is right:
it is a user action, off the player lock, and it is where the first override
gets created.

### ...and an e2e test, because the contract was hand-probed

**[iw]**: "this is also one of the reasons I try to have e2e tests for major
pathways, it avoids assumptions and mocks that often don't catch the things
most likely to break."

Which is the correct reading of this round: the whole feature rests on
`/Items/{id}/Ancestors` being the only way to name an item's library, and
that was established by one manual probe. `tests/e2e/test_batch4_contracts.py:
LibraryScopeLookupTest` pins four things against a real server — an episode
and a film both resolve to a `CollectionFolder`, every episode of one series
resolves to the *same* library (so keying the cache on `SeriesId` is correct
rather than merely cheap), and **no item DTO names its library** under any
field set the shim asks for, which is the premise of needing the call at all.
That last one is written to fail loudly if the server ever grows such a
field, because then this whole cost model can be deleted.

## 25 — The renderer's own mouse paths bypass SyncPlay — **done**

Both halves are handled: the pause hands over to Python while a group is on,
the seek is suppressed for the duration. What follows is why each got the
answer it did, since they are deliberately different.

Noticed while writing #16's sweep: `_is_pointer` excludes `MBTN_*` and
`WHEEL_*` from every claim, because the pointer belongs to the renderer —
`mpvtk_mouse` owns the buttons while the HUD or the library is up, and #1's
whole subject was getting that ownership right. A second claimant there
would be fighting it, and mpv's own `MBTN_LEFT_DBL cycle fullscreen` is
precisely the binding #1 arranged to fall *through* to.

That left a real gap, pre-existing rather than new — though only half of it
was the renderer's, which is worth stating precisely because the two halves
got different answers.

The **pause** half was the renderer's: four sites issued `cycle pause`
straight to mpv. The **seek** half never was — there is no `seek` anywhere
in `renderer.lua`; the wheel that seeks is mpv's own `WHEEL_LEFT` /
`WHEEL_RIGHT` (`seek ∓10`), which the renderer does not claim (its
`wheel_names` are `WHEEL_UP`/`WHEEL_DOWN`, for scrolling). Either way,
neither reached a SyncPlay group by the direct path: they landed on the
defensive `pause` observer, with the play-then-force-repause flicker that
comes with it, or on nothing at all.

**The pause half is fixed.** **[iw]**, hand-testing the key claims: "our own
HUD play/pause click handler does NOT get redirected into syncplay" — the
keys had been done and the *click* had not. There were four renderer sites
issuing `cycle pause` (click-to-pause on bare video, right-click in mpv
modality, the summon key, and the hidden-HUD click binding), and all four
went straight to mpv.

They now go through `state.pause_now`, which hands over to Python **only
while a group is on** — the same rule the key claims follow, and for the
same reason on both sides: `cycle pause` with no round trip is what makes
click-to-pause feel immediate, and paying for a round trip on every click
to fix a case that is off almost always would be the wrong trade. Python's
`toggle_pause` → `set_paused` is SyncPlay-aware at the top, so handing over
is the whole fix. The flag rides the HUD token beside `click_pauses`, and
`on_syncplay_change` re-sends it on join and leave so a group entered
mid-playback takes effect at once.

Two things it cost, both worth recording. `state.pause_now` is a **field on
`state`, not a file-scope local** — adding one put the chunk over LuaJIT's
200-local ceiling, which is a load error rather than a warning, exactly as
CLAUDE.md says. And the first version of its test read a stale `send` from
the block above, because `last_event` scans the whole log: `fake.reset_events()`
between blocks, as the textbox tests already do.

**The seek half is suppressed rather than routed.** **[iw]**: "should just
disable wheel seek during syncplay." Which is the better trade twice over:
routing it would mean a message per notch for a gesture that delivers
dozens, all to reach an operation the group is going to refuse anyway — and
it is not the renderer's key to route in the first place.
`keysweep.pointer_keys` finds whatever the pointer currently means for
seeking and the SyncPlay claim emits `ignore` lines for it: suppressed at
the mpv level, for the duration, with no round trip and nothing added to the
renderer at all. `WHEEL_UP`/`WHEEL_DOWN` are volume and are not touched.

Both halves are done, so #25 is closed.

## 26 — Two drop-down items from **[iw]** — **done**

Both halves turn out to be the same problem seen twice: **the text in these
lists is not ours.** A subtitle's title is whatever the person who made the
file called it and a version name is the user's own directory label — so
composing the label ourselves makes two different tracks identical, and
drawing it at the control's width makes every row ellipsize to the same
prefix. Either way the picker offers a choice it cannot express.

1. **`get_sub_display_title` now prefers the server's own `DisplayTitle`**,
   which is the string jellyfin-web shows and the only one carrying "Signs &
   Songs" versus "Full". The composed Language/Forced/Codec form stays as the
   fallback for a stream that has none — an offline item rebuilt from the
   local catalog, or an older server — and using DisplayTitle also stops
   "Forced" being appended to a string the server already put it in.

   It fixed a latent crash on the way past: the old code read
   `stream.get("Language", _("Unkn")).capitalize()`, which returns **None**
   for a stream that has the key set to null rather than missing, and
   `.capitalize()` then raised. Any untagged subtitle track.

   **Verified against real data**, after **[iw]** pointed out that the QA
   server's *Test Media* library has subtitles (my first search covered the
   bulk library and found none). What it sends:

   ```
   DisplayTitle='Styled - English - Default - ASS'   Title='Styled'
   DisplayTitle='English - SUBRIP'                   Title=None
   DisplayTitle='English - SUBRIP - External'        Title=None
   DisplayTitle='English - DVBSUB'                   Title=None
   ```

   The first is exactly the case this exists for: composed from
   Language/Codec it is `Eng (ass)`, which is what *every* ASS track in that
   file would be called. `tests/test_track_picker_labels.py` pins those
   strings as fixtures rather than invented ones.

   Two things the real data showed that are worth knowing and were not
   worth changing. The server puts **`External`** in DisplayTitle itself, so
   a track delivered as a separate file now reads `English - SUBRIP -
   External` beside our own `External` aside — mildly redundant, and the
   aside is still the more useful of the two because it is a *shim* fact
   (this one will be fetched separately) rather than a container fact.
   Suppressing it would mean matching a translated word against the server's
   English, which fails in every other locale. And one track reports
   `Language='Greek, Modern (1453-)'` — a name, not a code — which only
   reaches the composed fallback, but is pinned so it cannot start raising
   there.

2. **The three detail-page pickers get `popup_w`**, exactly as the Settings
   page's audio-device list uses it. The OPEN list widens, up to 640; the
   control stays 300, because a wider control would put those rows out of
   line with the rest of the page and it is closed almost all of the time.
   The popup takes only as much of the allowance as its widest item needs —
   visible in the regenerated `detail` snapshot, where the fixture's short
   labels leave it at exactly 300.

## #16 parts B and C

**B. The OSD menu binds its own arrows, while it is open.** `claim_menu_keys`
installs a section on `show_menu` and tears it down on `hide_menu` — the same
lifecycle the renderer runs for its own mouse sections, and what lets the
arrows belong to mpv the rest of the time with no conditional behaviour for
the user to notice. ENTER and ESC are deliberately *not* in it: they keep
their Python bindings, which are already guarded on `is_menu_shown` and have
duties outside the menu.

**...and the arrows are only dropped where they meant what mpv means.**
`_arrows_differ_from_mpv` is the gate: `seek_up`/`seek_down`/`seek_right`/
`seek_left` default to 60/-60/5/-5, which is `seek 60`/`seek -60`/`seek 5`/
`seek -5` character for character, so in a default configuration those four
bindings existed to reimplement mpv's arrows exactly. Any of the four
distances changed, either exact-seek flag, `use_web_seek`, `skip_intro_on_seek`,
or a `kb_menu_*` the user set — and the binding is earning its keep and is
kept. The menu section is a no-op in that case, or the menu would be driven
twice per press.

**C. The one-time migration** (`input_conf.py`, `CONFIG_VERSION` 4). The
settings the user *changed* become real mpv bindings in the shim's own
`input.conf`, and are then cleared so nothing binds them twice — the choice
moves out of our config and into theirs, where they can edit it like any
other mpv binding. Three rules:

* **Cleared stays cleared.** `None`/`""`/`"None"` means they parked our
  interception on purpose, and re-binding the key would undo the exact thing
  #16 is for.
* **It writes above the first `[`.** Everything after a section header
  belongs to that section until the next, so appending to a file with any
  section puts the bindings inside it — written, looking right, and never
  firing. A marker line makes it idempotent even if the config version is
  lost.
* **It declines what mpv cannot express.** `use_web_seek` and
  `skip_intro_on_seek` have no equivalent, so where either is on the arrows
  are not migrated and keep their binding. A migration that quietly dropped
  a feature would be worse than none. The `kb_*` that name *shim* actions
  (watched, next, our menu) are not in scope at all.

### `__fields_set__` is the wrong question anyway

Hand-testing produced a real migrated `input.conf`:

```
space cycle pause
f cycle fullscreen
up seek 60
down seek -60
right seek 5
left seek -5
```

**[iw]**: "these are default bindings, we don't need to set them." Every one
of those is mpv's own default, written back as an explicit binding for
nothing.

The cause is that `__fields_set__` answers "was this key in the file", and
`save()` writes **all 186 settings** — so after a single save the entire
config reads as deliberately chosen, and the predicate is true of everything.
The honest question is whether the value differs from the class **default**,
which is what both the migration and the arrow gate ask now. It cannot tell
a user who deliberately typed the default from one who never touched it, and
that is fine here: our defaults *are* mpv's for every key in scope, so the
two want the same thing.

(A config already migrated by the first version keeps its block — the marker
makes this idempotent. Those lines are harmless duplicates of mpv's defaults
and can be deleted by hand.)

### And the bug underneath it

**`__fields_set__` never reached the global settings object.** `parse_obj`
builds it on a throwaway and `load` copied only the *values*, so
`settings.__fields_set__` was empty however much the user had configured.
Nothing had ever consulted it — until #16, which asks exactly that question
to tell "our default, take it back" from "their choice, honour it". So both
the arrow gate and the migration would have answered "untouched" for
everyone, silently dropping customised arrow keys and migrating nothing.
Found by writing the migration rather than by any test.

### Confirmed by hand

The arrows really are mpv's now, and **[iw]** noted the proof by structure:
mpv's own seek OSD appears, which our binding never showed — and it appears
*only* outside a SyncPlay group, which is the claim being taken and given
back. Seeing that OSD is fine (**[iw]**); it is mpv's key doing mpv's thing,
which is the whole point.

---

## Review round 3 — two agents over #16

Both asked to refute their own findings first. **Seven survived, all seven
real, all verified here.** One was found independently by both. Three of
them are a *design* error rather than a slip, which is the part worth
recording.

### The design error: migrating the arrows was never coherent

An arrow is not one binding. It seeks during playback **and** drives the OSD
menu, and `input.conf` can express the first and not the second. So
migrating one has no good outcome: either the shim goes on binding a key mpv
now also binds (two seeks per press), or the setting is cleared and the
menu's navigation goes with it.

The first version cleared it — and the gate then made it permanent.
`_arrows_differ_from_mpv` still answered "ours" (the changed `seek_right`
that made it migratable is still changed, and a cleared `kb_menu_right`
differs from its default too), so `_bind_key(None, …)` bound nothing *and*
`claim_menu_keys` refused to install the menu section. **The arrow reached
neither the menu nor the shim.** With `seek_v_exact` and `seek_h_exact` both
on, all four went, and the OSD menu had no navigation at all — for anyone on
the classic OSC or in CLI mode, permanently.

The fix is not a better clearing rule, it is not migrating them: `SEEKS` is
empty. The arrows keep their Python binding exactly when they differ from
mpv and are given back untouched when they do not, which is the whole of
#16's benefit for them and needs no migration.

### The rest

* **`resend_hud_config` engaged the HUD unconditionally.** `engage()` is not
  a re-send — it is `set_hud(True)`, which the renderer treats as a *mode
  change* when the HUD is not already up and answers with `ui_suspend()`:
  nav keys, mouse and wheel unbound. Every other call site is guarded; this
  one was not. Pressing stop in a SyncPlay group leaves the group, which
  fired this from the library — and **froze the library with no way back**.
  For a lua-OSC user it switched the mpvtk summon layer on over their OSC
  and took `mbtn_left` with it.
* **Combined seek flags were mistranslated.** mpv joins flags with `+`, so
  `absolute+exact` is one argument meaning two things; matching the token
  whole read it as *relative*. `KP3 seek 30 absolute+exact` became a
  30-second jump from wherever the file was — and in a group, `seek_request`
  takes everybody with it. Read component by component now, which also stops
  `relative+exact` losing its exactness.
* **`kb_fullscreen`'s gate asked `__fields_set__`** — the exact trap the two
  neighbouring functions document at length and refuse. `save()` writes all
  186 settings, so it is true for every install that has ever run, `f`
  stayed intercepted for everyone, and the standing fullscreen claim swept
  up nothing because `f` was already a non-weak Python binding at sweep
  time. Found by **both** reviewers.
* **`_refresh_key_section` was called without the lock its contract
  requires.** `_bind_mpv_handlers` is reachable from `set_browse_window` /
  `force_window`, neither `@synchronous`, on the browser loop thread — so a
  `GroupJoined` on the websocket thread could mutate `_key_claims` mid
  iteration (out of `_init_mpv`, aborting a window rebuild) or simply win
  the race and leave the group's claim out of the installed section.
* **Clearing was by value, not by name.** `kb_pause = "right"` cleared
  `kb_menu_right`, whose binding the plan had deliberately declined — the
  "quietly dropped a feature" the module exists to avoid, arriving by the
  back door.
* **The write truncated before it wrote.** `open(path, "w")` on the user's
  own `input.conf`, where `Settings.save` next door goes to lengths to avoid
  exactly that. Temp-file-then-`os.replace` now.

### And a note on my own mutation testing

The first mutation I wrote for the clear-by-value fix **survived**, and I
nearly recorded that as the test being weak. It was the mutation that was
wrong: with `SEEKS` empty there is no second setting in scope to collide
with, so the mutant was not the bug. Reproducing the bug meant putting an
arrow back in `SEEKS` as well. A surviving mutant is a claim about the
mutant as much as about the test.

---

## #16, resolved properly: one setting was two settings

The review's worst finding was that migrating an arrow took the OSD menu's
navigation with it. I fixed that by not migrating the arrows — which worked,
and was treating the symptom.

**[iw]** named the actual cause: *"people probably configured arrow keys to
something else **so that our seek bindings weren't messing with the mpv
defaults**; the menu logically uses arrow keys and the only other thing I
could see someone binding those to are wasd."*

`kb_menu_*` meant two things at once — *which key drives the menu* and
*which key seeks* — and almost everybody who ever touched one was reaching
for the second, to get rid of it. `input.conf` can carry the second and not
the first, which is why migrating it was incoherent. Split, everything falls
out:

* **`kb_menu_*` is the menu's key, and only the menu's.** Never bound in
  Python at all; the OSD menu installs its own section for exactly as long
  as it is on screen. The setting keeps its value and is simply read as what
  its name always said — **[iw]**: "those configs keep their values, and we
  let the config version bump determine the migration."
* **The seek distances migrate onto mpv's own arrows** (`up seek 30`), which
  is coherent now that nothing about the menu rides on them, and reset to
  their defaults afterwards so nothing claims a key whose distance has
  moved.
* **What is left is claimed, not bound.** `use_web_seek` is the one seek
  feature mpv cannot express, so those users keep a live claim on whatever
  currently seeks — which follows a remapped key, where four fixed bindings
  never did. Routed by sign, because that is all a binding can tell us, and
  all web seek needs.

### ...and one claimant that was never needed

**[iw]**: "doesn't seek to skip intro listen for seeks too?" It does.
`_on_seeking` observes the `seeking` property and applies `skip_intro_on_seek`
to any forward seek — *"including custom key bindings"*, as its own comment
has said all along. So it needs no key claim, it is not a reason to decline
a migration, and the branch I had added to `_on_claimed_key` was
double-handling: the claim's own `self.seek()` raises that same observer.

With that, `_on_menu_left` / `_right` / `_up` / `_down` had no callers left
and are gone. Nothing binds them, and the menu's section talks to
`menu_action` directly.

## 27 — No lua is no UI, and the shim did not notice

**[iw]**, following #16's menu section: "the menu is all we have for the CLI
user who doesn't have lua compiled in — we should probably detect when lua is
broken and fallback add the osd menu via python."

The premise turned out to be inverted, and the real gap was worse.

**The menu keys are not lua.** `define-section` is core mpv input handling
and `script-message` is core client messaging, so the OSD menu's arrows reach
Python with no script host in the path at all. Measured, with no script
loaded: the message arrives, and `disable-section` releases the key again —
which also answers, at last, the question that unit tests could not ("the
keys still work, don't know if they're bound though").

**What was actually broken is everything else.** Every surface the shim
draws is lua: the browser and the playback HUD are `renderer.lua`, the stock
OSC is lua, `mouse.lua` is lua. `resolve_osc_style` falls back on
`enable_gui` and `thumbnail_osc_builtin` — both *settings* — and never on
whether mpv can run a script. So a default install on a no-lua mpv got no
browser, no HUD, no OSC **and no menu**: `toggle_settings_menu` refuses the
OSD menu whenever the *configured* style is mpvtk, live renderer or not. The
app ran and drew nothing but video, with no way to reach anything.

**[iw]**: "the lua probe failure should be a full and hard fallback to cli
mode with osd menu enabled (and of course the osc setting doesn't matter
because MPV's default OSC *needs lua*)."

So `PlayerManager.lua_works()` loads a one-line script and waits for it to
report. A probe rather than a capability check, for two measured reasons:
`mpv-configuration` does not mention lua on every build (it does not on
this one), and **`load-script` on a script that cannot run raises nothing on
either backend** — so an exception-based check would answer "fine" for
exactly the user it exists for. A probe also catches lua that loads and then
errors, which no capability string would. It costs ~1 ms on a healthy mpv
and the timeout once on a broken one.

`main` then drops the GUI and calls `set_osc_style("none")` — honest, because
there is no OSC to fall back *to*, and it is what makes the OSD menu
reachable again.

### A new lua file is five edits, and I made one of them

**[iw]** suggested folding the probe into `mouse.lua` "to avoid config
churn". The instinct was right and the specific target was not: `mouse.lua`
loads only when `settings.menu_mouse` is on, so the probe would have been
gated on a user-facing setting — turning off menu mouse support would have
disabled the entire GUI. A packaging dependency traded for a worse one.

But the churn is real, and I had already fallen into it. A lua file has to
be listed in **`MANIFEST.in`** (the sdist; `pyproject.toml`'s `*.lua` glob
covers only the wheel) **and in all four `build-win*.bat` PyInstaller
lines**. `lua_probe.lua` was in none of them — so an sdist install and
*every Windows build* would have shipped without the file, the probe would
have timed out, and every one of those users would have been dropped to the
CLI with no GUI. The batch scripts were **[iw]**'s catch, from memory.

`tests/test_lua_resources_are_packaged.py` walks the package for `*.lua` and
asserts each one appears in the manifest and in every build script, so the
next file cannot be forgotten the same way; it also checks the reverse, that
every `get_resource("*.lua")` names a file that exists, since `load-script`
fails silently either way. Verified beyond the guard by building the sdist
and the wheel and listing their contents — all five scripts in both.

---

## 21 — An mpv built without lua could not start at all

**[iw]**: "we should build a matrix of different mpv configs to test
against." `~/Desktop/mpv-matrix/build-one.sh <name> <ref> [meson args]`
does that — a git worktree per variant off `~/Desktop/mpv-build/mpv`, so
that checkout is never disturbed, installing both `bin/mpv` and
`lib/.../libmpv.so.2` because the two backends need different ones. It
reads `mpv-build`'s own `mpv_options`, which is not optional: this box's
pipewire headers are broken and `-Dpipewire=disabled` lives there.

The variant that matters is `v0.41-nolua` (`-Dlua=disabled`), and pointing
the shim at it found the bug immediately.

### The fallback was unreachable

`--osc` is only registered when mpv was built with lua, and a build without
it **does not ignore the option — it refuses to start**. So the app died
constructing mpv, before `lua_works()` (which needs a live mpv to probe)
could answer. Measured on both backends, and they report nothing alike:

| backend | what arrives |
|---|---|
| libmpv | `AttributeError('mpv option does not exist', -5, (h, b'osc', b'no'))` from the constructor |
| jsonipc | the binary prints `Error parsing option osc (option not found)` and exits; reaches us as `MPVError("MPV process retry limit reached.")` after every start retry is spent |

The first fix parsed both — an error-shape reader for libmpv and a
subprocess probe of the binary for jsonipc. **[iw]** cut it: "we don't even
need the detector, osc not being available means lua wasn't compiled in."
`--osc` is the single lua-gated option the shim sets, so failing to
construct *with* it and succeeding *without* it is not evidence to
interpret, it is the answer. One uniform retry replaces both mechanisms.

Dropping the option is also correct rather than a workaround: the OSC being
turned off is itself lua, so such a build has none to turn off.

The answer is then recorded rather than rediscovered — `lua_works` skips
its script load and its 2s timeout. **Only in that direction.** mpv having
`--osc` proves lua was *compiled in*, not that it runs, and lua that loads
and then errors is exactly what the probe exists for, so the ordinary path
still asks. `test_a_clean_start_leaves_the_lua_question_open` is what holds
that; getting it backwards would skip the probe on every normal machine.

### How long this has been broken: since 2023-02-16

**[iw]**: "the lua gap means non-osc builds of MPV have been broken for
years now." Traced, and it is more specific than that — the shim used to
handle this case correctly, by accident:

| when | how `osc` was set | a no-lua mpv |
|---|---|---|
| 2020-02 `4aff3bf2` | `if hasattr(self._player, "osc"): self._player.osc = ...` — **after** construction | fine. libmpv turns an unknown attribute into a property read, so `hasattr` is False and the line is skipped |
| 2023-02 `b6eb3b0b` | `mpv_options["osc"] = False`, inside `if settings.thumbnail_enable:` | **dies at construction** — and `thumbnail_enable` defaults True |
| 2026-07 `bc1caf0f` | gated on `osc_style in _REPLACES_OSC`, default `mpvtk` | dies unconditionally |

So the regression is `b6eb3b0b "Fix regression where trickplay broke
external MPV."`, which moved the option from a post-construction attribute
(where `hasattr` guarded it) into the constructor (where nothing could).
Broken on default settings since then; the mpvtk work only removed the last
way to avoid it (turning thumbnails off).

That it went unreported for three years is itself a data point about how
many people build mpv without luajit.

### Where the tests live, and why not together

The pure half is `tests/test_lua_only_options.py`. The live half —
the shim started against real builds, one subprocess each because `player`
picks its backend at import — is `tests/e2e/test_mpv_matrix.py`, run with
`python3 -m unittest tests.e2e.test_mpv_matrix` (~33s).

It is outside the discovered suite for a reason beyond needing a built mpv:
discovery imports the modules that import `player`, which puts a **live
libmpv in the test process**, and spawning an mpv binary out of that
process segfaults at teardown. Reproducible, unrelated to what is being
asserted, and it survived being moved into a clean child interpreter — the
fork is the problem, not the child. A flaky segfault is not worth adding to
a 4300-test suite.

### The version spread is mostly free, and mostly not buildable

Debian 13 ships `libmpv2` **0.40.0** in `/usr/lib/x86_64-linux-gnu` beside
whatever `mpv-build` installed into `/usr/local`, so a second real version
costs an `LD_LIBRARY_PATH` — and 0.40 is the useful one, sitting below the
0.41 line `runtime_force_window_works` draws.

Older than that will not build from the matrix: `FF_PROFILE_*` became
`AV_PROFILE_*` in FFmpeg 7, so mpv 0.40 and below fail to compile
`demux_mkv.c` against mpv-build's ffmpeg and would each need an ffmpeg of
their own era built first — the expensive half of a full `mpv-build` run.

---

## 22 — Carousel headings jogged sideways row to row

**[iw]**: "the titles with a link in them have different margins than the
non-link items." Measured, at 1280x720:

| row | title x | title y | first tile x | strip y |
|---|---|---|---|---|
| plain | 0.0 | 0.0 | 5.0 | 45.0 |
| linked | 6.0 | 2.0 | 5.0 | 49.0 |

A heading with a "see all" chevron was a `Box(pad=(6, 2))`; one without was
a bare `Text`. So the title moved 6px right and 2px down, and the row
measured **4px taller**. The home screen mixes them (Next Up links,
Continue Watching does not), so adjacent titles jogged and the rows under
them did not line up.

Both are now the same Box; only the contents and the handlers differ. The
pad is `RING_PAD`, which is what `hscroll_row` insets the strip by, so the
title now starts directly above the first tile's artwork — **both** old
positions were wrong about that, which the detail page's snapshot shows
plainly: "Cast & Crew" sat at x=16 over tiles at x=21.

Pinned as equality between the two spellings rather than against the
numbers (the pad is a design value and may move; the two must move
together), plus the one absolute claim about the artwork.
`test_only_the_linked_heading_is_clickable` is the guard that making them
identical did not make the plain one a nav target.

---

## 23 — HiDPI and type sizing audit

**[iw]**, three questions. Measured on the real scene, not read off the
source.

### 1. What sizes does the app use?

Every `size=` on a widget call, across `mpvtk_browser/` and `mpvtk/`:

| widget | default | calls | what is passed |
|---|---|---|---|
| `Text` | 22 | 234 | 15×54, 17×23, 16×22, 14×22, 18×21, 22×19, 20×15, 26×10, computed×17 |
| `Button` | 20 | 122 | **default ×111**, 15×6, 18×1, 19×1 |
| `Dropdown` | 20 | 23 | **default ×19**, 16×2, 14×2 |
| `Checkbox` | 20 | 23 | **default ×23** |
| `Icon` | 20 | 20 | **default ×20** |
| `TextBox` | 20 | 19 | **default ×17**, 16×1, 14×1 |

The shape of the problem is in that table. **Body text nearly always passes
an explicit size and clusters at 14–18; controls nearly always take the
default 20.** `Text`'s own default of 22 is used by nobody — 217 of 234
calls override it.

### 2. Do they respond to HiDPI? Yes, and carefully.

`scaling._EXACT_KEYS = ("size",)` — a font size is scaled but deliberately
**not** rounded, with the reason written down: at 0.75x, `px(18)` is 14,
which is 18.67 logical, so every line renders 3.7% wider than the width
layout fitted it to. Verified on the settings scene at 1.25/1.5/2.0: 35
text nodes, **0 off-scale** at every factor, and no image's physical bitmap
dimension moved (that boundary is `raster()`'s, not `scale_scene`'s).

So there is no HiDPI bug here. What the report was about is (3).

### 3. The drop-downs really are bigger than the app

Counting the text actually drawn at 1280x720:

| screen | sizes drawn |
|---|---|
| settings | 14×6, 15×4, 17×2, **20×22**, 22×1 |
| grid | 14×1, 15×27, 16×1, **20×7**, 22×1, 26×1 |
| detail | 15×1, 16×6, 18×4, **20×3**, 22×1, 24×2, 26×1 |

On Settings, **22 nodes at 20px against 12 at 14–17px** — the control text
is *larger than the labels it belongs to*, which is backwards. The 20px
nodes are the top-bar nav, the tab buttons, and every Dropdown, TextBox and
Checkbox: precisely the widgets that never pass a size.

So this is not a design decision anywhere. It is the gap between
`widgets.py`'s defaults and what call sites ask for, and "some but not all
buttons" is literally the 6 of 122 that pass `size=15`.

### What it would take

Prototyped: dropping the `Button`/`TextBox`/`Checkbox`/`Dropdown`/`Menu`
defaults from 20 to 17 moves Settings to `14×6, 15×4, 17×22, 20×2` — the
two survivors being explicit `Text(size=20)` section headings — and the
whole DPI matrix (7 scales × every window) stays green. Nothing overflows,
because the change only ever makes text smaller.

That is the cheap version and it is a real improvement, but it swaps one
set of unchosen numbers for another. The durable version is a named type
scale, which the theme layer already has the shape for: `heading_size` is
a theme key (24), and `tile_title_size` / `tile_sub_size` are two more. A
`body_size` / `control_size` beside them would put every one of these in a
themeable table, make the 14–18 spread visible as the decision it is, and
let a large-text theme move all of it together.

**Not landed.** It is a global visual change and the choice of numbers is
[iw]'s, not the audit's. The measurement above is what the decision needs.

---

## 24 — Off `GET /Users/{userId}/Items` entirely

**[iw]**: "we should migrate off of that wholesale then, Jellyfin upstream
plans to break those old legacy endpoints eventually and they're already
causing issues for us." Both halves of that are true and they are separate
arguments: the route is `[Obsolete("Kept for backwards compatibility")]`,
*and* it is already lossy (§19 — three of 88 parameters dropped, silently).

`jellyfin_mpv_shim/items_api.py` is the replacement; 8 call sites across
`repository.py` and `sync/manager.py` moved. No apiclient change: the user
rides as `UserId: "{UserId}"`, a template the http layer substitutes into
**params** as well as into the URL.

Signature-compatible with `get_user_items` on purpose — six of the eight
sites spread a kwargs dict built somewhere else, so a rename that also
reshuffled arguments would have been a migration nobody could review.

**The helper's own failure mode is the bug it fixes.** A keyword we fail to
map is not an error, it is a filter that stops being applied — and a
library where everything matches looks exactly like a library with no
filter. So the mapping is checked against the apiclient's *own signature
and source* rather than a copy of itself, and an unmapped keyword raises.

Verified against the live server before the tests were touched — six query
shapes, identical `TotalRecordCount`, identical item ids, identical DTO
field sets. And the thing it buys, on the same server:

| | legacy | modern |
|---|---|---|
| unfiltered | 1131 | 1131 |
| `AudioLanguages=eng` | 1131 | **1108** |
| `AudioLanguages=zzz` | 1131 | **0** |

`tests/e2e/test_items_endpoint.py` pins it, and is the only test in the
project that can tell the two endpoints apart — from the client's side a
dropped filter and a library that matches everything are the same thing. It
skips by **asking the server** (`/Items/Filters2` returning options), which
is the gate jellyfin-web uses and the one the filter UI will use, so it
needs no version knowledge.

### What the batch rename cost, which is the part worth writing down

The call boundary moved from `get_user_items(**kwargs)` to
`items(params={...})`, so every stand-in had to grow `items()` and every
assertion had to move from the apiclient's keyword names to the server's
query keys. That was done with a regex over `["snake_case"]`, and **it was
wrong five times** — every one of them a recorded call that was never the
items query:

| what | endpoint | vocabulary |
|---|---|---|
| `vals["genres"]` | `get_filter_values` output | not a query at all |
| `kw["limit"]` (nextup) | `get_next` | keywords |
| `kw["include_item_types"]`, `kw["parent_id"]` | `get_studios` | keywords |
| `g.calls[0][1][...]` | `get_genres` | keywords |
| `call["media_types"]` (shuffle) | `get_random_items` | keywords |

Only `/Items` became a query dict. The two spellings now live side by side
— `test_list_page.py:504-505` asserts both, two lines apart — and which is
right is decided by *which endpoint the recorded call went to*, not by what
the key is called.

All five failed loudly with `KeyError`, but that was luck rather than
coverage, and [iw] named the reason: "this is the type of thing I'd run,
and then very carefully review the diff of. The tests help a ton but
probably aren't sufficient to catch every type of bug able to be caused by
such a batch rename."

The class they cannot catch is the **negative** assertion.
`assertNotIn("recursive", call)` passes trivially once the dict holds only
PascalCase keys — a live test silently becomes a tautology, and nothing
goes red. There were six of those in the rename. Two checks, because a
passing suite says nothing about either:

1. Every negative assertion was mutation-tested by making `build_query`
   emit `Recursive` and `IncludeItemTypes` unconditionally. All 16 affected
   tests went red; a tautology would have stayed green.
2. Both directions of the diff were audited by *recorder*, not by name —
   every remaining snake_case subscript confirmed to be a non-items call,
   and every PascalCase one confirmed to be on an items record.

`test_shuffle_filters_on_the_same_axis` is the one to keep in mind: its
`assertIsNone(call.get("IncludeItemTypes"))` had been renamed wrongly and
was, at that moment, vacuous. What caught it was the *positive* assertion
on the line above raising `KeyError`. A test with only the negative half
would have shipped silently broken.

---

## 25 — Review round 3, and what a passing suite could not see

**[iw]** asked for an adversarial review of everything since `771c976d`
(18 commits, ~480 production lines): seven dimensions in parallel, each
finding verified by an independent skeptic prompted to refute. 27 agents,
20 findings judged, **12 confirmed, 8 refuted**. All 12 are fixed.

Deduplicated they are five pieces of work, and the three worth reading are
the ones the suite was *structurally* unable to catch.

### A broken paste hid a real regression

`tests/test_input_conf_migration.py` had a second copy of its module header
pasted into the middle of the file, and the three tests below it became
nested functions inside a duplicated `_settings()`. Never collected, never
run — and nothing goes red for a test that was not collected, so the suite
stayed green throughout.

Two of them were `test_web_seek_stops_the_distances_moving` and
`test_skip_intro_on_seek_does_not_stop_them`: the tests watching the seek
path, absent while exactly that path regressed. They pass unchanged now,
so they were right all along; they were simply not there.

### The seek settings had stopped meaning anything

#16 gave the arrows back to mpv, and what replaced the old bindings is a
*claim*: `_seek_is_ours` decides only **whether** to claim, and
`_on_claimed_key` then seeks by the amount parsed out of mpv's own binding
— never by the setting. So a changed distance bought a claim that ignored
it. Nothing else carried the number either: `input_conf.migrate` runs once
under `config_version < 4`, and `Settings.load` stamps `CONFIG_VERSION` on
a freshly created config, so a **new install never migrates at all**.

**[iw]**: "should drop the dead settings post-migration." They are gone
from the schema. Two things fell out of that:

- The migration now reads the **raw config dict**, because by the time it
  runs `parse_obj` has discarded every key the schema no longer declares.
  That is what a migration should do anyway — it is the only code that has
  to know what the config used to look like.
- `kb_seek`, the phone and web-remote path, was the last reader, which is
  its own bug: after a migration wrote `up seek 30` and cleared the setting
  to 60, the arrow key seeked 30 and the remote's Up seeked 60 **on the
  same machine**. It asks mpv what the keyboard would do now, so the two
  agree by construction rather than by being kept in step.

And the one case that cannot be carried: `mpv_ext` + `mpv_ext_no_ovr` makes
external mpv read the user's own config directory, so the file the
migration writes is never loaded — it wrote there anyway and cleared the
settings, losing the choice outright. **[iw]**: "that config basically says
'use my own mpv config, if something breaks it's my problem'." So it now
does nothing there and logs the block it would have written.

### The no-lua fallback lasted exactly one mpv

`main` asks `lua_works()` once and calls `set_osc_style("none")`, but
`_init_mpv` re-resolved the style from settings on every mpv re-creation —
an idle-quit then a cast, `set_browse_window`, `force_window`. So the style
went back to `mpvtk` with no renderer behind it and `on_hud_menu` still
None, and `toggle_settings_menu` went back to refusing. **One idle timeout
put the user back in the state this branch exists to remove.** The Pillow
fallback had the same shape.

### Two lessons about the tests, not the code

**A stand-in that omits a field makes a path uncoverable, not uncovered.**
Every Season stand-in omitted `SeriesName`, so `_open_item` — the only
production path that sets `bar_title` — had nothing to read. A test written
against those fakes would have passed against code that never set the field
at all. Fifth instance of the pattern in CLAUDE.md's list.

**My first test for the OSC fix was worthless, and looked fine.** It
re-implemented the decision inside its own stand-in and asserted a string
appeared in the source, so `if False and ...` passed both halves. Extracting
`_effective_osc_style()` and driving the real method kills that mutation and
the wrong-order one. Every fix in this round is mutation-tested for that
reason.

### Two corrections to my own claims

Both caught by the suite rather than by me, and both stated confidently in
a commit message first:

- "The seek settings were never in the settings UI or the README." The
  README half was true; they are documented in `docs/configuration.md`,
  which is where this project's configuration reference actually lives. I
  grepped one file and generalised. `test_no_documented_setting_has_been_removed`
  exists for exactly this.
- `migrate` cleared every setting it wrote by reading the class default,
  which for a seek name is now an `AttributeError` — a production
  regression in the commit that had already declared the work done.
