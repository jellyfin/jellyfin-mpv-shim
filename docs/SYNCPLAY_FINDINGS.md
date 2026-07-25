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

## Verification status

Marked per item:

* **verified** — checked directly against current source in this tree.
* **relayed** — from the trace, plausible and cited, but not independently
  re-checked. Re-verify before acting.

Server-side claims (four group states; `Waiting` is left only when every
non-ignored member sends `Ready`; exactly four `SendCommandType` values —
`Unpause`, `Pause`, `Stop`, `Seek`) are **relayed**. The client-side line
numbers below are current for `syncplay.py` and `player_reporting.py`.

## Fixed

### pause_ignore was written by the timeline thread — **FIXED**

See `player_reporting.py` history and `tests/test_syncplay_pause_ignore.py`.
Kept here because it is the one item with a confirmed mechanism, and because
the remaining items are the reason to keep looking.

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

### 2. No `Ready` is sent in response to a `Seek` command (verified, and observed in practice)

The server sets every member buffering on `Seek` and stays in `Waiting` until
all of them report `Ready`. The client's `Seek` path is `process_command` →
`schedule_seek` (`syncplay.py:589`) → `schedule_pause` (`:518`) →
`local_pause()` + `local_seek()`, and **never** calls `_buffer_req(False)`.
The only `Ready` senders are `on_buffer_done` (`:310`), `play_done` (`:317`)
and `upd_queue`'s no-change branch (`:587`).

Result: after any group seek the group hangs until someone presses play, which
force-overrides the wait. It recovers *by accident* when the seek takes longer
than the 1000 ms buffering timer, because that path does send `Buffering` and
then `Ready` — so it hangs on fast local files and works on slow streams.

Known to the maintainer as "idk, just pause and unpause it".

### 3. `SendCommandType.Stop` is not handled (verified)

`process_command` (`syncplay.py:400-407`) handles `Unpause`, `Pause`, `Seek`
and falls through to `log.error("Command {0} is unknown.")`. Another member
stopping the group leaves this client playing, still in the group, still
reporting progress. Joining an idle group has the same effect.

### 4. Real buffering is never reported (verified)

The server has a `Buffer` request precisely so the group pauses for a stalled
member. The client only ever fires `on_buffer` from mpv's **`seeking`**
property; the observer set is `eof-reached`, `playback-abort`, `seeking`,
`pause`, `current-tracks/audio/codec`. Nothing observes `paused-for-cache` or
`core-idle`, so a cache underrun desyncs this client silently and it then gets
yanked by SkipToSync. Likely the most common everyday desync.

### 5. `Media.replace_queue` does not update `has_next` / `has_prev` (relayed)

`media.py:716-723` assigns `self.queue, self.seq` and returns; the flags are
only set elsewhere. Every `PlayQueue` group update goes through here
(`syncplay.py:578`), so after the group queues or removes items,
`finished_callback` reads a stale `has_next` — and either silently leaves the
group at the end of an episode, or calls `get_next()` on a shortened queue.

### 6. `PlayingItemIndex` can be `-1` and is used as a Python index (relayed)

The server uses `-1` for "no playing item" (e.g. after the playing item is
removed from the playlist). The client passes it straight into `Media(...)`
(`syncplay.py:568`) and `replace_queue` (`:578`), where `sp_items[-1]` selects
the **last** item in the queue.

### 7. The duplicate-command filter defeats the server's resync (relayed — wants context)

The server deliberately re-sends a byte-identical command to a single session
to correct a drifted client. The client drops it as a duplicate
(`syncplay.py:381-388`), so the server's only correction channel is unusable.

Context needed before acting: jellyfin-web has the same filter, so either the
server's resync is also useless there, or web distinguishes them some way the
port missed. Check jellyfin-web before changing this.

### 8. `Ping` is sent as a float against a `long` DTO, and is nearly never sent (relayed — wants context)

`syncplay.py:211` posts `ping.total_seconds() * 1000`, a fractional number,
where the server DTO declares `long` — which would explain the existing
`# Server responds with 400 bad request` comment at `syncplay.py:208`. It is
also gated on `self.sync_enabled`, which is the drift-correction flag and is
false most of the time. If both hold, the server always uses its default ping
for this session and the unpause latency compensation is wrong for the whole
group.

Context needed: confirm the 400 against a real server and check whether the
apiclient or the server has since changed.

### 9. Blocking I/O on threads that must not block (verified in shape)

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

### 10. Smaller items (relayed)

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

Items 2 and 4 are the ones users feel daily and are small, self-contained
protocol fixes. Item 1 is the most serious but wants the redesign described in
its note rather than a patch. Items 7 and 8 want checking against jellyfin-web
and a live server before anything changes.
