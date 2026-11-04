# Implementation summary (JellyHome Player fork)

Branch `jellyhome-privacy-fork`. Package name `jellyfin_mpv_shim` is intentionally unchanged to keep upstream
merges mechanical; user-visible identity, packaging and config paths are the fork's own.

This is a status record of what the code and tests in this repository show. Where a claim rests on a run whose logs are not
in the repository it is dated and says so. It says nothing about a published release (none had been published when this
was written).

## Requirements and status

| Requirement | Status | Notes |
|---|---|---|
| Remove public-IP lookup | Implemented | The request to a public-IP service and the `utils` function that made it were removed; locality is decided in `network_locality.py` from the server's own address and the Jellyfin server. `LocalityTest.test_no_third_party_is_ever_contacted` makes `requests.get`, `requests.Session.request` and `urllib.request.urlopen` fail, and a test in `test_jellyhome_identity.py` fails if any first-party source mentions the removed service. |
| Per-server Connection Type Auto/Local/Remote | Implemented | Stored on the server's credential entry (`connection_type`, keyed by the uuid that survives re-auth); Settings → Servers dropdown with bandwidth explanation; default Auto; absent key = Auto. Persistence and the gateway calls are tested (`test_jellyhome_privacy.py`); no test renders the Servers-tab dropdown and it has not been driven in a live window. |
| Conservative fallback / no trust bypass | Implemented | Failures → Remote + untrusted (previously a failure fell back to `is_local = True`). `Media.direct_path_trusted` is separate from `Media.is_local`; Local override and server `IsInNetwork` never grant it. |
| Auto-update checks off by default | Implemented | Default `False`; `CONFIG_VERSION 7` resets an inherited `true` once; an opt-in saved at v7 persists (tested through `Settings.load/save`). |
| Check for Updates Now | Implemented (button untested in a live UI) | `UpdateChecker.check_now`; uses `constants.RELEASE_REPO = braisravina/jellyhome-player` taken from this checkout's `origin`. Empty → no request and an explanatory message. Redirects into upstream are refused. |
| Discord off + disclosure | Implemented | Was already off; setting note now states what is shared. |
| No telemetry / remote assets | Implemented | None added; appdata/README third-party screenshots removed. |
| Network/privacy table | Implemented | `docs/privacy.md`. |
| Separate identity | Implemented | `APP_NAME=jellyhome-player`, `DESKTOP_ID=io.github.braisravina.JellyHomePlayer`, new Inno `AppId` GUID, Flatpak id/command, MPRIS bus name, console script `jellyhome-player`, exe `JellyHomePlayer.exe`. `io.github.braisravina…` assumes the GitHub user from `origin`; change it if the publisher identity differs. |
| Explicit upstream import | Implemented | `jellyhome-player import-upstream`; preferences only, never logins/device id. |
| Windows installer | Implemented, **not built** | Per-user, optional desktop icon, launch after install, ask-before-close of own files, autostart value removed on uninstall. No ISCC/Windows here. |
| Flatpak | Implemented, **not built** | Renamed manifest/desktop/appdata. `finish-args`, runtime and SDK are identical to upstream's manifest (`--device=all` kept for controllers; no home or host-filesystem access). Not on Flathub. |
| Start with computer | Implemented (Windows registry path tested against a fake `winreg`; Linux XDG tested for real) | Off by default; unsupported on Flatpak/macOS with a message. |
| Keep running for casting | Already existed | `close_to_tray` / `allow_background`; documented. Label not renamed (would drop existing translations). |
| First run | Partially | Existing flow (address → Quick Connect/login) reused. Added optional HTTPS prefill (`DEFAULT_SERVER_URL`, empty), and https-by-default for non-LAN bare addresses. Error screens for malformed/offline/cert not changed or re-verified. |
| i18n | Partially | New strings use `_()`; `base.pot` **not regenerated** (no `xgettext`); es/gl fall back to English for new strings; two existing strings changed (`jellyhome-player stop`) lose their translation until retranslated. No translations were invented. |
| Optional-improvement report | See below | Nothing implemented. |
| Jellyfin 10.11.8 | Partly tested (2026-11) | One synthetic DVD-style file direct-played from a disposable loopback 10.11.8 server (login, browse, DirectPlay, pause/seek/track switches, stop); `/System/Endpoint` and the Connection Type paths have still not been exercised against a server. (The test scripts were not in the repository when this was written.) |

## Verification record

Two kinds of evidence are mixed here and labelled: what the tests in this repository show, and dated runs whose logs are
not in the repository.

* **First session** (a host with no `mpv`/`libmpv`, `xvfb`, `xgettext`, `msgfmt`, `ISCC`, Docker or sudo). Baseline before the
  edits, `tools/run_tests_parallel.py`: 6814 tests in 280 modules, 56 modules unable to start because the real player could
  not (`FileNotFoundError: 'mpv'`). After the edits: 6871 tests in 282 modules with the identical set of failing modules
  (the 56 that need a real mpv), once four doc/fake/snapshot guards were fixed.
* **Later, with a private non-root mpv/libmpv 0.40.0 and Xvfb** (2026-11-03/04; one module at a time in a 3 GiB, no-swap
  memory scope): all 283 test modules passed (7,264 tests, 0 failures, no leftover process, largest scope peak 2.1 GiB).
  That run predates later fixes; the whole set was not repeated after each of them, and each carries its own focused
  tests. The per-module logs were kept outside the repository.
* **Tests added for this work:** `tests/test_jellyhome_privacy.py` (33: update checks, migration, locality, persistence, log
  redaction) and `tests/test_jellyhome_identity.py` (24: packaging parse/cross-check, autostart, import, first-run).
  Existing tests were updated for the new owner / `direct_path_trusted` field, and `tests/snapshots/settings.jsonl` was
  regenerated for the new settings controls.
* mypy gate skipped (mypy not installed).
* **Not run:** `tests/e2e/` (needs a real server); any Windows, Flatpak or macOS build; the Connection Type dropdown and the
  "Check for Updates Now" button in a live window against a server.

## Manual checklist (needs a real machine)

1. Windows: build with `build-win.bat` after `gen_pkg.sh --skip-build`; install as a normal user (no UAC), run, check Start menu and optional desktop icon; install over itself while running (prompt appears; other apps untouched); install upstream too and confirm separate settings; enable *Start With Computer*, reboot, confirm launch minimized; uninstall and confirm the Run value is gone.
2. Flatpak: build per `docs/installation.md`; launcher appears; playback, controller, audio; `flatpak run --command=sh` shows no home access.
3. With a Jellyfin 10.11.8 server: sign in by Quick Connect and by password; wrong password, server off, HTTPS with bad cert; play over LAN IP, over a hostname that resolves publicly (hairpin), and over VPN; set Connection Type Remote and confirm the bitrate cap applies; capture traffic and confirm no request other than your server (GitHub only after pressing the update button, and the local-network discovery broadcast on the sign-in screen).
4. Update button: with no releases the result should be "Could not check" or "up to date"; confirm nothing else leaves the machine while the automatic switch is off.

## Follow-up backlog (reported, not implemented)

* MPV presets: a "light" profile for DVD/older PCs (`profile=fast`, `vo=gpu` + `hwdec=auto-safe`, no shaders/interpolation) versus the existing higher-quality `gpu-next` path. Expected benefit (not measured): fewer dropped frames on weak GPUs; cost: no HDR tone-mapping or shader upscaling; needs per-hardware testing.
* Subtitles: image-based subtitles (PGS/VobSub) are not something this fork changes; a way to size them for SD sources is not provided (an idea, not investigated).
* Encrypt stored tokens with the OS keychain (Windows DPAPI / libsecret) instead of plaintext JSON.
* Sign Windows builds; publish screenshots owned by the fork; regenerate `base.pot` and translate es/gl.
* Verify `GET /System/Endpoint` authorisation on 10.11.8 and consider caching `IsInNetwork` per network change.
* `pip-audit`/Dependabot in CI.
