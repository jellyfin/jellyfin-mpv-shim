Release review: v2.10.0 → f0c9de69

Reviewed the clean `master` checkout against tag `v2.10.0` (`bb120471`): 1,282 commits, 598 changed files. Translation changes account for approximately 69% of changed lines. Three review agents and the lead reviewer concentrated on playback, browser/rendering, client lifecycle, and offline sync. No repository files were modified.

Ten reproduced findings follow, ordered by priority. P1 findings should be addressed before release; P2 findings affect narrower workflows but are actionable regressions. This is a targeted review of a very large changeset, not an exhaustive certification.

1. **[P1] Check the media origin before installing the global authorization header.**

   [player.py:2365](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/player.py:2365)

   `_apply_auth_headers()` checks for foreign subtitle hosts but not the actual media host. With `direct_paths=True`, an HTTP `.strm` source hosted elsewhere is returned unchanged by `Video._get_url_from_source()`, while mpv retains the Jellyfin server's global Authorization header. Playing the item sends the access token to the third-party stream host. HTTP path substitutions have the same exposure. Authentication must be scoped to the resolved stream origin as well as subtitle origins.

   Evidence: [authentication probe](/tmp/review_playback_auth_probe.py), using only synthetic credentials and no network. The selected URL belongs to `cdn.example.invalid`, the foreign-subtitle set is empty, and mpv's header contains the synthetic Jellyfin token.

2. **[P1] Preserve the original download store until relocation succeeds.**

   [manager.py:339](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/sync/manager.py:339)

   `_move_tree()` deletes each original after copying it. If a cross-device move copies `catalog.db` successfully and then runs out of space copying media, the exception handler reopens the old directory. That creates an empty catalog; `_reconcile_disk()` then deletes the original media as orphaned directories. The returned error incorrectly says the downloads were left in place. Movement needs a recoverable commit boundary or rollback before reconciliation can run.

   Evidence: [sync probe](/tmp/jms_release_sync_probe.py), which injects EXDEV and then ENOSPC while exercising real recovery. The catalog survives at the destination, but the media exists at neither location. Independently reviewed by a second reviewer.

3. **[P1] Keep health-check reconnects behind the startup PIN gate.**

   [clients.py:1088](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/clients.py:1088)

   Startup loads credentials but intentionally waits for the PIN before connecting. The periodic health check ignores that gate and connects the protected account after the default 300 seconds. Its notification reaches the browser's `set_source()`, which clears `_locked` and opens Home without a PIN. Background reconnects and source updates must respect whether the current session has been unlocked. This defeats the documented parental-control affordance; the PIN is not otherwise presented as a hardened security boundary.

   Evidence: [lifecycle probe](/tmp/jms_release_lifecycle_probe.py). Three executions of the real health-check → UI callback → gateway → browser path leave `startup_needs_unlock=True`, `_locked=False`, and Home visible.

4. **[P1] Reject connection results belonging to the previous local user.**

   [clients.py:933](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/clients.py:933)

   `switch_user()` drains registered clients but does not invalidate authentication already in flight. A slow startup or reconnect can complete after the switch and register the old user's authenticated client: publication checks shutdown/removal, not the initiating user or a connection generation. The server list includes registered clients, so a background connection notification can expose the old account under the new user. Pin ownership when starting a connection and validate it again when publishing its result.

   Evidence: [lifecycle probe](/tmp/jms_release_lifecycle_probe.py). In three controlled interleavings, switching to a user with no credentials and then releasing old authentication leaves `credentials=[]`, `clients=['old-credential']`, and the old client running.

5. **[P2] Preserve explicit watched-state ordering across asynchronous stop reports.**

   [player_reporting.py:542](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/player_reporting.py:542)

   Moving `session_stop()` to `SessionReporter` breaks the order relied on by `unwatched_quit()` and `watched_skip()`. `stop_and_close()` returns before the queued stop reaches the server; the following `set_played()` is synchronous and can arrive first. The later stop can restore resume progress or mark an item watched again near its end, undoing Quit and Mark Unwatched. Explicit marks need ordering with the queued reports, not merely ordering of Python calls.

   Evidence: [report-order probe](/tmp/review_playback_order_probe.py), using production methods, a real `SessionReporter`, and the harness Journal. With an earlier report delayed, the observed order is `mark(False)` followed by a 95-second stop for a 100-second item. The existing ordering test replaces the asynchronous sender itself and misses this boundary.

6. **[P2] Apply remembered track choices before negotiating a transcode.**

   [player.py:2704](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/player.py:2704)

   Episode track memory is applied after negotiating and loading the playback URL. For transcoded audio, changing `video.aid` cannot change the audio already encoded into that stream, and `configure_streams()` skips transcode audio changes. The next episode plays the server's default language while the UI/reporting claims the remembered language. Remembered burn-in subtitles have the same ordering problem. Resolve memory before PlaybackInfo/URL negotiation.

   Evidence: [track-memory probe](/tmp/review_playback_tracks_probe.py). Production playback requests `aid=None`, loads a URL with `AudioStreamIndex=1`, then ends with `video.aid=2`; no renegotiation occurs.

7. **[P2] Acknowledge only the offline playstate version actually uploaded.**

   [manager.py:1021](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/sync/manager.py:1021)

   Replay snapshots pending rows, performs network I/O, then deletes completed IDs. Concurrent `upsert_playstate()` updates the existing row without changing its ID, so that deletion also removes newer, unsent progress or a final watched mark. This can happen when playback starts offline and the server reconnects: `OfflineVideo.client` remains the captured `None`, while the sync worker can use the newly connected client. Use a version-aware acknowledgement or another atomic claim mechanism.

   Evidence: [sync probe](/tmp/jms_release_sync_probe.py). Across three interleavings, replay uploads position 10, concurrent playback writes position 100 plus `played=True`, and replay leaves the pending queue empty. The server never receives the final values. Independently reviewed by a second reviewer.

8. **[P2] Reload unfinished routes when returning with Back.**

   [app.py:727](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/mpvtk_browser/app.py:727)

   Open a library and submit the always-visible Search field before its fetch finishes. Search changes the epoch, dropping the library result. Back restores the route but `_land_back()` does not restart this unfinished load. The grid has no items, error, or outstanding request and displays a spinner indefinitely. Forward already has missing-data recovery; Back needs the equivalent.

   Evidence: [browser probe](/tmp/jms_release_browser_probe.py), using the real browser, rendered search handler, and existing deferred-pool harness. Three subsequent render/drain cycles retain `_items=None`, no pending jobs, and a busy indicator.

9. **[P2] Stop failed page fetches from retrying on every repaint.**

   [pagination.py:342](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/mpvtk_browser/pagination.py:342)

   In Paginated mode, a failed current-page request or neighbor prefetch clears `_page_loading`. Async completion invalidates the scene, and the next `ensure()` immediately retries the same failed request. A fast 500/503 response creates a continuous request/repaint loop without user input and repeatedly occupies the shared API pool. Windowed mode tracks attempted fetches; fixed pages need a retry boundary or backoff too.

   Evidence: [browser probe](/tmp/jms_release_browser_probe.py), with real `AsyncRunner`/`Paginator` and a failing fetch. Three render/drain cycles increase requests and invalidations from 3 to 6 to 9.

10. **[P2] Serialize DisplayPreferences read-modify-write transactions.**

    [repository.py:825](/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/mpvtk_browser/repository.py:825)

    Quickly changing Show titles and Show years submits independent jobs to the four-worker pool. Both can read the same full DTO, change separate keys, and post their copies; the later POST restores the earlier setting despite both calls succeeding. Leaving legacy List view also generates two writes. Serialize the complete GET/change/POST operation per server/user across the shared document's writers.

    Evidence: [browser probe](/tmp/jms_release_browser_probe.py), calling the real persistence methods against a full-DTO API stand-in with a barrier. Requesting both preferences false leaves one false and one true.

Validation and limits:

- The existing project virtualenv passed all 5,125 unit tests in 182 modules: [unit log](/tmp/jms-release-unit-venv.log).
- Initial test attempts encountered the local Xvfb wrapper's sandbox restriction and missing dependencies in system Python. The virtualenv rerun is the meaningful baseline; no packages were installed.
- All eight integration matrix legs passed on the isolated rerun, covering libmpv, jsonipc, and each backend's combined suite: [isolated integration log](/tmp/jms-release-integration-isolated.log). There were 1,142 executed test invocations and eight expected skips across the matrix (some tests run in multiple legs).
- Validation caveats: the first integration run lost its shared virtual X server during the external-mpv leg and was stopped; subsequent display failures are not treated as regressions. The isolated rerun's final test process exited 0, but a leftover test mpv held its stdout pipe open. After that specific process was terminated, the runner printed its all-pass summary and exited 0. This cleanup issue was observed but not traced to a production cause.
- Findings use controlled local probes rather than a live Jellyfin server. Probe credentials are synthetic.
- All six reproduction scripts were rerun together on a private Xvfb display; every script exited 0: [probe log](/tmp/jms-release-probes.log).
- Coverage emphasized playback/queue/auth/reporting, browser navigation/paging/persistence/artwork lifetimes, client reconnect/user/PIN boundaries, and sync replay/relocation. Packaging received static inspection.
- Windows/macOS runtime behavior, real installers/Flatpak builds, desktop-specific tray behavior, and exhaustive reader/Live TV behavior remain unverified. The renderer received a bounded additional review rather than exhaustive analysis.
