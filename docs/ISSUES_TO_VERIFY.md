# Open issues to verify against the local-ui fixes

Candidates this session's `local-ui` work may resolve, from mining the open
issue tracker (`jellyfin/jellyfin-mpv-shim`) against `master..HEAD`. **Do not
close from this doc alone** — each needs a reproduction against the actual fix,
because several original reporters identified different root causes (mpv's own
`resume-playback`, `enable_osc`, server-side bugs). Confidence is how closely
the report matches a fix, not proof.

Nothing here has been posted to the tracker.

## High confidence — flagship lifecycle cluster (verify, likely closeable)

### #295 — Losing connection to the server requires restart
- **Symptom:** after a sleep / network drop the device disappears from the cast
  menu and never reconnects until the app is restarted.
- **Fix:** `077a42d` — `validate_client`'s force-reconnect path called
  `client.callback(...)` two lines after nulling it to a no-op, so a failed
  health check stopped the websocket and never reconnected. Now it drops the
  client and the credential-retry pass reconnects it in the same tick.
- **Repro:** connect, block the server (firewall/stop Jellyfin), wait past a
  health-check interval, restore — device should reappear in the cast list with
  no restart.

Result from testing: Confirmed fixed.

### #344 — Have to reconfigure the server on every startup
- **Symptom:** reboot-while-open → "Client is not actually connected" warning →
  must remove and re-add the server.
- **Fix:** same `077a42d`, plus `d78d0f2` (restore last-used server after
  reconnect) and `1dc73ab` (stop persisting the runtime `connected` flag).
- **Repro:** connect, restart the machine with the app open, relaunch — server
  should reconnect without re-adding.

Result from testing: Not tested yet with reboot, but dirty quits don't cause the issue.

### #331 — Restarting never works because of cred.json
- **Symptom (relevant half):** a stale `connected: true` in `cred.json` breaks
  the next startup; multiple `run.exe` pile up.
- **Fix:** `1dc73ab` strips volatile keys before saving; the atomic
  single-instance lock (`4f14f2b`) addresses the duplicate-process half.
- **Repro:** save creds, inspect `cred.json` (no `connected` key), relaunch
  cleanly; launch twice → single instance.
- **Note:** the OSC-config half of this report is unrelated and not addressed.

Result from testing: Never been able to reproduce.

### #458 — Closing mpv crashes the player and hangs the shim (18 comments)
- **Symptom:** closing the mpv window throws in `action_thread.run` /
  `timeline.run` (dies to `ShutdownError`/`BrokenPipeError`), then the shim
  hangs and needs SIGKILL.
- **Fix:** worker-thread guards (`0320640`) — the action/timeline loops survive
  exceptions and the teardown drains; mpv shutdown teardown moved onto the
  action thread.
- **CAVEAT — do not fully close yet:** a later comment describes a residual
  X-button freeze that also *marks the item watched* via
  `get_timeline_options`. Verify that specific path before closing; it overlaps
  the mpv-process-lifecycle refactor (see `MPV_LIFECYCLE_REFACTOR.md`).
- **Repro:** play, close the mpv window (OSC 'x') mid-playback and while paused,
  once with the server unreachable — shim should report stop, not hang, and not
  mark a mid-file item watched.

Result from testing: Fixed!

## Medium confidence — reproduce first

### #503 — Broken pipe crashes external mpv
- **Fix (partial):** `_mpv_errors` now includes the external backend's error
  types and the worker loops survive them. The **start-timeout** half (#454) is
  upstream `python-mpv-jsonipc`, not addressed here.
- **Repro:** external backend (`mpv_ext`), induce a broken pipe on a property
  read during `send_timeline` — playback state should survive.

### #157 / (closed #323) — Replaying a finished episode starts at EOF, marks watched, skips
- **Fix (maybe):** genuine-EOF watched-marking + playback epoch
  (`400f04c`/`9b1cbd2`) — an item at exact-end shouldn't be marked watched or
  auto-advanced.
- **CAVEAT:** at least one reporter's root cause was mpv's `resume-playback`
  saving the position at the end; confirm against the fix. #323 was reported
  **external-only** — worth a both-backend parity check.

### #541 — Seek to the end pauses instead of playing the next episode
- **Fix (maybe):** genuine-EOF detection. **Open question the maintainer
  flagged:** historically the bug is *events not firing when you skip at the
  very end of a file to the end* — a seek-induced end may not deliver
  `eof-reached` the way a played-out end does. This is the exact edge the EOF
  fix may or may not cover; needs a real repro (seek to last second → end).

Result from testing: This appears to be fixed.

### #421 — SyncPlay: pause/resume in mpv doesn't affect the group
- **Fix (maybe):** the missing `_rearm_sync` method (`c0dedcc`) — every SyncPlay
  Unpause / skip / join-a-playing-group raised `AttributeError`.
- **CAVEAT:** the report also reads as by-design local-pause behavior. Reproduce
  specifically by *joining a group that is already playing* (the "Playing Now"
  branch that hits `_rearm_sync`) to confirm this is the cause.

Result from testing: This appears to be fixed.

### #505 — Forkserver child orphaned on exit
- **Fix (maybe):** single-instance / process-lifecycle work (`da57bbf`,
  `4f14f2b`).
- **Repro:** launch, quit, assert no child/forkserver process survives.

Result from testing: This appears to be fixed.

## Low confidence — mention only
- **#461** silently fails to register (reporter links to #344) — possible.
- **#408** connection interrupted on page refresh — partly server-side (10.9.9).
- **#113** local-network address change — possible.
- **#410** "reconnect to server" option — auto health-check partially satisfies;
  the tray-state UI is not built.
- **#324** tray extra copy after screen lock — reporter says flatpak-only.

## Fragile areas the tracker confirms (for test prioritization)
Client lifecycle/reconnect and the **external-mpv backend** are the two largest
open-bug clusters, followed by shutdown/process lifecycle and playback
auto-advance/watched-marking. The external backend is the least-tested path —
the new both-backend test matrix targets it directly.
