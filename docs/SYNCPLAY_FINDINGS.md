# SyncPlay: findings from tracing the client against the server

Date: 2026-07-25. Client tree at `05b0098a`; server read from a local checkout
of `jellyfin/jellyfin`.

## Why this exists

`syncplay.py` was hand-ported from jellyfin-web's client logic around 2019,
not written against the server's own state machine — the server source was too
large to hold in one head at the time. So the client implements *another
client's* model of the protocol, and it has drifted. The symptom has been
years of intermittent, never-diagnosed misbehaviour: pauses that do not
propagate, groups that sit waiting, and at least one hard client hang
requiring a restart.

This document records what the trace found so the work can be scheduled. It is
**not** a task list to be worked top to bottom — several items want their own
investigation first.

Two pieces of context that shape how to read all of it:

* **jellyfin-web is the source, not the specification.** The client's logic was
  hand-transpiled from web's JS. Where the two agree, that means a bug was
  inherited rather than introduced; it is evidence about provenance, not about
  correctness. The server is the only authority.
* **SyncPlay is effectively unmaintained upstream.** It arrived as a drive-by
  PR that added a great deal of complexity to the server, and the author then
  disappeared; the Jellyfin team is not fond of it. Practically: the protocol
  is unlikely to change under us, so conforming to the server as written is
  safe — and equally, upstream is unlikely to meet us halfway, so anything
  fixed here has to be fixed on the client side.

## Verification status

Marked per item:

* **verified** — checked directly against current source in this tree.
* **relayed** — from the trace, plausible and cited, but not independently
  re-checked. Re-verify before acting.
* **demonstrated** — there is a test that fails on it.

`tests/_syncplay_server.py` is a port of the server's group state machine,
read from `MediaBrowser.Controller/SyncPlay/GroupStates/` with the enums from
`MediaBrowser.Model/SyncPlay/`. `tests/test_syncplay_protocol.py` drives a
real `SyncPlayManager` against it. Each conformance test was written as an
`@unittest.expectedFailure` against a defect first and the decorator came off
with the fix, so every one of them is known to fail on the code it describes.

Five further tests in that file assert on the mock itself, so a mock that
drifts from the server cannot quietly make the rest pass.

Server-side claims are now **verified directly**, not relayed: `GroupStateType`
has exactly the four states, `SendCommandType` exactly the four commands
(`Unpause`, `Pause`, `Stop`, `Seek`), and `WaitingGroupState.HandleRequest`
for a `SeekGroupRequest` broadcasts `Seek` and then calls
`SetAllBuffering(true)` — the group cannot leave `Waiting` until every session
answers. Read from the server source while building the mock.

The client-side line numbers below are current for `syncplay.py` and
`player_reporting.py`.

## Fixed

Five, all with tests that fail on the previous commit.

### pause_ignore was written by the timeline thread

`get_timeline_options` set it to mpv's live pause state, giving a flag that
means "the pause value we just commanded" a second, contradictory meaning --
and sampling it twenty-five lines before writing it. A stale sample landing on
a fresh guard swallowed the next local pause or unpause: the player changed
state and the group was never told.
`tests/test_syncplay_pause_ignore.py`.

### No `Ready` was sent in response to a `Seek` command

The server sets every member buffering on `Seek` and stays in `Waiting` until
all of them report `Ready`; the client's `Seek` path never answered, so a group
seek left everyone stopped until somebody pressed play (which force-overrides
the wait). It looked intermittent because a seek slower than
`min_buffer_thresh_ms` went out as `Buffering` and came back as `Ready` by that
route -- so it hung on fast local files and worked on slow streams. Known to
the maintainer as "idk, just pause and unpause it".

Fixed by `schedule_seek` passing `report_ready=True`. On the seek path only,
and deliberately: **only `WaitingGroupState` emits a `Seek` command** -- every
other state routes a seek through it first -- so a `Seek` always means the
group is waiting on us. A `Pause` carries no such promise, and answering one in
the `Paused` state gets "client got lost, sending current state" back, which
would answer itself forever.

### `SendCommandType.Stop` was not handled

`process_command` fell through to `log.error("Command Stop is unknown")`, so
another member stopping the group left this client playing and still reporting
progress.

Fixed with `schedule_stop`. Two things it does not do, both on purpose. It does
**not** leave the group -- the server moves the group to Idle and keeps every
session in it, so `PlayerManager.stop` grew a `leave_group=False` argument
rather than tearing our own membership down. And it does not call `stop()`
inline: this runs on the websocket reader thread, and `stop()` takes the player
lock and then the timeline lock, which would stall that server's whole message
stream and add a fresh path into the lock inversion in item 1 below. It is
queued onto the action thread instead.

### Real buffering was never reported

The client only raised `on_buffer` from mpv's `seeking` property, so a cache
underrun -- the case the feature exists for -- was never reported to the group,
and this client simply fell behind until SkipToSync yanked it back.

Fixed with an observer on `paused-for-cache` (`PlayerManager._on_cache_pause`).
`on_buffer` already debounces by `min_buffer_thresh_ms`, and loads are excluded
via `do_not_handle_pause`, or every file would announce itself as buffering
while filling its cache.

### The duplicate-command filter ate the server's resync

The server corrects a client it thinks has drifted by re-sending the current
state to that session alone (`PlayingGroupState.cs`, `PausedGroupState.cs`,
"Client got lost, sending current state"). `Group.NewSyncPlayCommand`
(`Group.cs:418-427`) stamps every command with `EmittedAt = DateTime.UtcNow`,
so the resync differs from the broadcast it repeats in exactly that field --
and the client compared `When`, `PositionTicks` and `Command` while excluding
it.

Fixed by comparing `EmittedAt` too. That was the choice flagged as deliberate:
it keeps the filter meaningful for a *literal* re-delivery of one message
(pinned by `test_a_literal_redelivery_is_still_filtered`) while letting a
genuine resync through. Deleting the filter outright would have worked equally
well against this server but dropped that guard.

## Open, ranked

### 1. ABBA lock inversion between `_lock` and `_tl_lock` (verified)

Deadlock, reachable **only with SyncPlay enabled**, which matches the reported
hard hangs.

* Holds `_tl_lock`, wants `_lock`: `send_timeline`
  (`player_reporting.py:332`, `@synchronous("_tl_lock")`) →
  `syncplay.sync_playback_time()` (`player_reporting.py:353`) → SkipToSync →
  `local_seek` (`syncplay.py:644`) → `playerManager.seek`, which is
  `@synchronous("_lock")` (`player.py`).
* Holds `_lock`, wants `_tl_lock`: `_play_media` → `send_timeline`; and
  `stop`, `finished_callback`, `play_next`, `skip_to`, `play_prev` →
  `send_timeline_stopped`.

Different locks, so `RLock` re-entrancy does not help. `set_speed`/`show_text`
are not `@synchronous`, so the SpeedToSync path is safe; SkipToSync is not.

**Design note from the maintainer, and the reason not to just reorder the
locks:** `_tl_lock` was introduced to stop out-of-order timeline reports
without making the main `_lock` hang the player. But *a lock is the wrong tool
for protecting timeline validity.* Ordering/staleness is a sequencing problem
— a generation counter or a single-writer queue expresses it directly, and
neither can deadlock against `_lock`. `SessionReporter` already exists and
already serialises the actual sends. Treat this as a redesign of how timeline
validity is enforced, not as a lock-ordering bug to patch.

### 2. `Media.replace_queue` does not update `has_next` / `has_prev` (relayed)

`media.py:716-723` assigns `self.queue, self.seq` and returns; the flags are
only set elsewhere. Every `PlayQueue` group update goes through here
(`syncplay.py:578`), so after the group queues or removes items,
`finished_callback` reads a stale `has_next` — and either silently leaves the
group at the end of an episode, or calls `get_next()` on a shortened queue.

### 3. `PlayingItemIndex` can be `-1` and is used as a Python index (relayed)

The server uses `-1` for "no playing item" (e.g. after the playing item is
removed from the playlist). The client passes it straight into `Media(...)`
(`syncplay.py:568`) and `replace_queue` (`:578`), where `sp_items[-1]` selects
the **last** item in the queue.

### 4. `Ping` is sent as a float against a `long` DTO, and is nearly never sent (relayed — wants context)

`syncplay.py:211` posts `ping.total_seconds() * 1000`, a fractional number,
where the server DTO declares `long` — which would explain the existing
`# Server responds with 400 bad request` comment at `syncplay.py:208`. It is
also gated on `self.sync_enabled`, which is the drift-correction flag and is
false most of the time. If both hold, the server always uses its default ping
for this session and the unpause latency compensation is wrong for the whole
group.

Context needed: confirm the 400 against a real server and check whether the
apiclient or the server has since changed.

### 5. Blocking I/O on threads that must not block (verified in shape)

* `_on_pause_change` and `_on_seeking` issue **synchronous HTTP** from mpv's
  single event thread (`pause_request`, `play_request`, `seek_request`,
  `_buffer_req`). `put_task` exists to keep work off that thread — its comment
  says issuing commands from inside an event handler "causes a crash" — and
  these paths bypass it.
* `upd_queue` → `_play_video` → `playerManager.play` runs a **full playback
  start on the websocket reader thread**, and `schedule_pause`'s callback can
  run inline there too. A playback start holds `_lock` for up to
  `playback_timeout` (30s default), during which no websocket traffic for that
  server is processed at all, including KeepAlive.

### 6. Smaller items (relayed)

* `play_done` (`syncplay.py:317`) sends `Ready` without clearing
  `is_buffering` / `last_playback_waiting` the way `on_buffer_done` does, so a
  stale `is_buffering` can survive a track change and disable drift correction
  for the rest of the session.
* `upd_queue` sends no `Ready` when `_play_video` fails (`syncplay.py:547`) —
  it logs and the group waits forever.
* `enable_sync_play` replays the queued command *before* converting
  `enabled_at` to server time (`syncplay.py:222` vs `:224`), so the first
  command after joining is compared against a local-clock timestamp and can be
  discarded as "old". Swapping the two lines fixes it.
* `disable_sync_play` calls `leave_sync_play()` without a null check on
  `self.client` (`syncplay.py:277`); it is inside a bare `except`, so the
  group is simply never left server-side.
* `timesync.remove_subscriber` uses `set.remove`, which raises if
  `disable_sync_play` runs twice.
* The client never sends `IgnoreWait`, so it can never excuse itself from a
  wait it is itself blocking — the escape hatch for items 2 and 4.
* Dead handlers remain for messages the server no longer sends
  (`PrepareSession`, `GroupWait`, `CreateGroupDenied`, `JoinGroupDenied`). The
  modern equivalent of the "someone is buffering" notice is
  `StateUpdate{State: Waiting}`, which the client only logs
  (`syncplay.py:344-349`).

## Suggested order

The protocol conformance items are done. Item 1 is now the most serious thing
left, and it wants the redesign in its note rather than a lock-ordering patch
-- and the Stop fix above is a small worked example of the same instinct,
queueing onto the action thread rather than reaching for a lock.

Items 2 and 3 are queue-handling bugs that want a media mock to test properly.
Build that shaped by those two rather than speculatively, the way the SyncPlay
mock was shaped by the protocol findings.

Item 4 wants confirming against a live server before anything changes.
