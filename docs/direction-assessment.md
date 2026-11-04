# Direction assessment: fork, new app, or other clients

Written 2026-11-03 on branch `jellyhome-privacy-fork`; sections 11 and 12 were added on 2026-11-04. Sections 1-10 are
kept as written at the time, with an in-place note wherever later work overturned a statement. Evidence labels:
**Verified** (run here), **Documented** (source read, not exercised), **Unverified**
(recollection or thin search results; do not rely on it), **Planned**, **Unsupported**.

## 1. Recommendation

**Option A: keep improving the fork, as a desktop-only client.** Cover phones and
televisions with the existing maintained Jellyfin clients (option D) instead of porting this
one. Do not start a rewrite (B/C) on current evidence.

Why, from what was inspected:

* The playback engine is already libmpv/ffmpeg. That is the maintained decoder a new
  desktop app would choose, so a rewrite adds no codec coverage. Container, codec,
  subtitle and deinterlace capability belongs to mpv/ffmpeg and the GPU driver, not to
  the shell around them.
* The fork already does what the brief asks for in application code (Documented,
  `utils.get_profile`, `media.py`): its device profile declares direct play with no
  container, codec, profile, level, bit-depth or resolution limits (see section 4 for what
  that does and does not mean), so the server transcodes only when the bitrate cap, a
  forced setting or a refused source forces it; embedded bitmap subtitles (DVD VobSub/PGS) are
  rendered by mpv on the client; the HUD distinguishes DirectPlay / Remux / DirectStream /
  Transcode and shows the server's reasons (`media.transcode_play_method`,
  `mpvtk_browser/hud.py`); failed starts are reported and a transcode retry is a single
  **user-initiated** step offered only when the first attempt was not already a transcode
  (`player._notify_load_error`, `retry_failed_playback`), so there is no retry loop.
* The one honest cost of "advertise everything" is device performance, not
  compatibility: `hwdec` defaults to `no` (software decode) and `deinterlace_auto` to
  off. SD MPEG-2 decodes trivially in software; 1080p H.264/HEVC on an old HDMI laptop
  may not. That is a preset problem (section 5), not an architecture problem.
* The codebase is large and heavily tested (about 280 unit modules), with documented
  traps (`docs/do-not-fix.md`, `docs/mpv-backends.md`). Re-creating that is the real
  price of B/C.
* Mobile and TV: libmpv does not give an Android/iOS/TV *app*; each needs a new UI and
  packaging. The upstream projects already ship these (Unverified details; see 3).

What would change this recommendation: measured evidence that libmpv cannot play the
target media acceptably on the weak HDMI laptops (then the fix is hardware decode choices,
still not a new shell), or a requirement for phone/TV that Jellyfin's own apps do not meet.

## 2. Diagnosis of the current problems

| Symptom | Cause | Class | Evidence |
|---|---|---|---|
| Many brief player windows, RAM at 96% | `player.py` builds the `playerManager` singleton and **opens a real mpv window at import**; ~61 test modules import it. `tools/run_tests_parallel.py` ran one process per module with `cpu_count // 2` workers (6 on this laptop), no memory ceiling, and no cgroup | Test harness | `STARTHERE.md`, `run_tests_parallel.py:_default_jobs`; each import under Xvfb measured at 70-160 MiB, so 6 concurrent real-mpv workers plus Xvfb is well under 3 GiB. The 96% therefore needs the larger integration/e2e matrix or a real desktop session on top; not reproduced. **Later finding (section 11):** those windows were also visible on the real desktop, because `xvfb-run` left `WAYLAND_DISPLAY` set and mpv prefers Wayland |
| Windows closing quickly | Intentional per-module test instances exiting normally at end of module. No crash seen in logs here | Test harness | exit codes 0 in runs below |
| Leftover processes | **Verified cause:** workers end in `os._exit`, skipping teardown, so with the external-mpv backend each worker's `mpv` child was orphaned (25 in one serial run, ~117 MiB each, all inside the scope, removed when it was stopped). Fixed by `_teardown_players()` | Test harness | before: 25 orphans; after: `PLAYER-TEARDOWN` closes the handle itself, 0 orphans over 5 modules (external) and 4 modules (libmpv). **A second source was found later (section 12):** the integration entry point never ended the shared player singleton's external mpv, which only the runner's group kill hid |
| `test_audio_settings`: 5 failures, 1 error (external backend only) | A test stand-in gap: the fake player did not answer `command("get_property", ...)`, which the external backend uses to read properties; the application's read path was confirmed correct against a real external mpv. Fixed in the test, with a both-backend regression class | Test defect | passes 79/79 on libmpv and on the external backend; same failures on pristine `HEAD` before the fix |
| Transcoding on a weak server | Only when the profile cap, a forced setting or an unplayable stream demands it. Default `remote_kbps` is 10000 kb/s, so a server classed *Remote* transcodes any source above ~10 Mb/s (typical Blu-ray rip), including on a 100 Mbit link | App config (code can warn) | `conf.py:remote_kbps`, `get_profile`; set Connection Type to Local or raise Maximum Bitrate per server |
| Interlaced DVD combing | `deinterlace_auto` is off (mpv default); mpv's `auto` is unreliable by the project's own note | Backend + preset | `conf.py`; needs a per-title choice, left to the HUD toggle |

What application code can do: capability advertisement, track and subtitle selection,
explaining the path, error classification, presets. What the backend/device decides:
codec/profile/level/bit-depth support, hardware decode, deinterlace and scaling cost.
What needs the server: remux/audio-only transcode, subtitle burn-in on devices that cannot
draw bitmaps, and any video transcode (Pentium E5300: effectively none; treat Direct Play
as a requirement, not an optimisation).

## 3. Options compared

Effort is relative to continuing the fork (=1), assuming one maintainer; maintenance is
the ongoing cost. These are estimates, not measurements.

| | A. Fork (recommended) | B. New UI on libmpv, reuse API code | C. New app, shared logic, per-platform backends | D. Existing clients |
|---|---|---|---|---|
| Container/codec/subtitle coverage | Everything libmpv/ffmpeg opens; bitmap subs rendered locally | Same engine, same coverage | Per backend: mpv on desktop, platform players on mobile (coverage differs per OS) | Per client; Jellyfin Desktop is Qt WebEngine plus libmpv (Documented via Flathub listing); mobile clients vary (Unverified) |
| Windows / Linux | Yes (Linux verified here; Windows build not run) | Yes | Yes | Yes (Jellyfin Desktop) |
| Android / iOS | No; not feasible without a new app | Not by itself | Yes, but a separate playback stack each | Swiftfin (iOS) and Findroid (Android) exist; engines Unverified |
| Browser | Not applicable; browser is limited by the browser's codecs (forum reports AC-3 and some codecs transcode or remux there; Unverified) | Same | Same | Jellyfin web: needs server help for DVD MPEG-2/AC-3 (Unverified, not tested) |
| TV | No | No | Possible per platform, large | Jellyfin has TV clients (Unverified detail) |
| HW accel / fallback | `hwdec` setting; software default | Same | Per platform | Per client |
| Weak-device CPU/RAM | mpv-based, UI drawn in the mpv window via Pillow; not profiled on weak hardware | Likely lighter UI possible; unmeasured | Unmeasured | Qt WebEngine is heavier than mpv alone (Unverified) |
| Server transcode load | Low in practice only because the profile declares no limits; unverified beyond one file | Same if profile reused | Depends on each backend's honest profile | Depends |
| Deinterlace/scaling | mpv's, user-selectable | Same | Per backend; weaker on mobile (Unverified) | Per client |
| Packaging/signing | Existing scripts; Windows/Flatpak not built here; unsigned | New | Several pipelines | Upstream's |
| Features lost | none | Browser shell, offline sync, readers, syncplay, live TV need rebuilding | Same, per platform | Differ |
| Effort / maintenance | 1 / ongoing upstream merges | 5-10x / lower coupling, new bugs | 15x+ / highest | ~0 / follows upstream |

Shared code is not shared capability: a shared Jellyfin API layer does not give a phone
the same codec support as desktop mpv.

## 4. Playback strategy (explicit)

1. Original media first. **What the profile actually says:** `DirectPlayProfiles` is
   `[{Type: Video}, {Type: Audio}, {Type: Photo}]` with no `Container`/`AudioCodec`/`VideoCodec`
   (an empty value means "any" to the server), `CodecProfiles` is empty unless the user turns
   on a `transcode_*` setting, and `MaxStreamingBitrate` comes from the connection type
   (Local: effectively unlimited; Remote: `remote_kbps`, default 10000 kb/s). That is a
   declaration that **the client will attempt anything**, which is a design choice
   (`docs/jellyfin-api-notes.md` section 11), **not a compatibility guarantee**: the server
   never learns what this mpv build, this CPU or this GPU can decode in real time.
   **What was demonstrated:** one synthetic MKV (MPEG-2 Main 720x576 interlaced, AC-3 stereo
   x2, one generated VobSub track, one external SRT) direct-played on mpv 0.40.0 / FFmpeg
   7.1 with software decode on a Ryzen 5 5500U (Debian, GNOME/Wayland, Vulkan GPU output),
   from Jellyfin 10.11.8 over loopback. **Not demonstrated, so unverified or unsupported
   as claims:** any other codec, profile, level, bit depth, frame rate or resolution
   (H.264/HEVC at HD or 4K, 10-bit, HDR, Dolby Vision, VC-1, AV1); hardware decode
   (`hwdec` defaults to `no`, so every pass above is software decode); real-time
   performance on a weak laptop or a CPU without SIMD extensions; mpv/FFmpeg builds
   other than Debian's (Windows, macOS, Flatpak builds may lack decoders); bitmap
   subtitles other than the one generated VobSub (real DVD SPU streams, PGS); what the
   server does when the client *cannot* cope (there is no client-side capability probe, so
   a stutter is never reported to the server, and no automatic transcode fallback happens
   for performance, only after a load failure and only on user request); network paths
   (remote, 100 Mbit, TLS); and any server other than 10.11.8. Dropped frames are the
   symptom of a device limit, and the profile cannot see them (**gap**).
2. Audio and subtitles decoded/rendered locally (Documented).
3. Remux or audio-only transcode when the server says so (Documented: `PLAY_REMUX`,
   `PLAY_DIRECT_STREAM`).
4. Video transcode only if the source is refused or the bitrate cap forces it.

Quality: avoiding a lossy transcode preserves the source; it does not add detail. A DVD is
about 720x480 (NTSC) or 720x576 (PAL); upscaling can smooth it but not recover HD detail.

## 5. Compatibility matrix (this machine: Debian, Ryzen 5 5500U, mpv 0.40.0, libmpv/Xvfb)

| Media / platform | Linux desktop | Windows | Weak HDMI laptop | Android/iOS | Browser |
|---|---|---|---|---|---|
| DVD MPEG-2 + AC-3 + VobSub in MKV | **Verified for one synthetic file** (section 9): DirectPlay from a real 10.11.8 server, software decode, on Xvfb, and in section 10 on the real desktop with GPU output; no real DVD rip | Documented, untested | Documented; SD software decode expected cheap, untested | Unverified (use client D) | Unverified; server would remux/transcode |
| H.264 | Documented, untested | Documented, untested | Needs `hwdec`, untested | Unverified | Unverified |
| HEVC | Documented, untested | Documented, untested | Needs `hwdec`, untested | Unverified | Unverified |
| External text subtitles | Documented (profile + mpv) | Documented | Documented | Unverified | Unverified |
| Embedded bitmap subs | Documented (client render; burn-in fallback) | Documented | Documented | Unverified | Needs server burn-in (Unverified) |
| Deinterlacing | mpv option; `auto` flag-based, user toggle | same | CPU cost untested | n/a | n/a |

Only the one synthetic file of sections 9 and 10 was played; everything else above is not **Verified** playback.
One sample proves one file, not a codec: profile, level, bit depth, interlacing and
bitrate vary within each codec.

## 6. Privacy and security checks (code inspected, not trusted from the summary)

Verified by search of `jellyfin_mpv_shim/`: no `verify=False`/`CERT_NONE`; no public-IP or
telemetry services; update checks default off (`conf.check_updates=False`); Discord off;
`ignore_ssl_cert` is an explicit user opt-in. Documented in `docs/privacy.md`. Remaining
risks: plaintext tokens in `users.json`; server-named artwork URLs; `http://` LAN logins;
unsigned installers; the loopback socket and external-mpv IPC permissions were not
audited. This does not claim zero third-party traffic.

## 7. What this pass did and did not run

This section describes the **first pass** (2026-11-03). See `docs/testing.md` ("Bounded runs on a workstation") for
commands. Not done in that pass: physical-device, Windows, Flatpak, hardware-decode, browser and Android/iOS testing;
a disposable Jellyfin 10.11.8 server (no Docker; also the 3 GiB budget); playback of any media; the full-application
GUI smoke test and repeated launch/close cycles. **Later passes did** the disposable server and playback (sections 9
and 10), GUI launches (sections 11 and 12) and the integration matrix (section 11); the others remain not done.

## 8. Roadmap and decisions before any rewrite

1. **Now:** containment tooling (done); resolve `test_audio_settings` on mpv 0.40 (done: a test-fake gap, fixed).
2. **Next:** a "light" mpv preset for old HDMI laptops (`hwdec=auto-safe`, no shaders),
   chosen explicitly and measured on the real hardware before it is a default; a
   "playback path and why" line in the failure dialog; a warning when a per-server bitrate
   cap will force a video transcode of a source.
3. **Then:** a synthetic media set (ffmpeg `testsrc` MPEG-2/AC-3, H.264, HEVC with ASS/SRT
   and a generated VobSub `.idx/.sub`) played against a disposable 10.11.8 server (done for the one MPEG-2/VobSub
   file; H.264, HEVC and the ASS/SRT variants are not).
4. **Mobile/TV:** use Jellyfin's clients; reassess only if they miss a requirement.

Decisions needed from the owner: acceptable target hardware for the weak laptops; whether
phones/TV matter enough to justify a second codebase; whether to publish builds (signing).

## 9. Disposable-server playback test (2026-11-03)

Server: Jellyfin 10.11.8 (official archive tarball, sha256 `d7fba5f3...3ca1`; the publisher serves no
checksum, so provenance beyond HTTPS from repo.jellyfin.org is unverified), loopback only
(`127.0.0.1:18097`, verified with `ss`), system ffmpeg 7.1.5 instead of jellyfin-ffmpeg (a deviation).
Client: JellyHome Player 3.3.0, in-process libmpv, mpv 0.40.0, FFmpeg 7.1.x, **Xvfb with `vo=x11`**
(`vo=x11` is a test workaround, not an app setting; it is also what actually confined that run to the virtual display, see section 11).
Fixture (synthetic): 20 s MKV, MPEG-2 Main 720x576 576i top-field-first 25 fps, SAR 64:45 / DAR 16:9,
two AC-3 stereo tracks (eng, spa), one VobSub track (generated bitmap SPU stream, ffmpeg-decodable),
plus an external SRT. Not a real DVD rip.

Result: login through the real GUI form; library browsing showed "Movies" and the title; **PlayMethod
DirectPlay, TranscodingInfo none, no FFmpeg transcode log, no ffmpeg process** (full-speed software
decode of SD MPEG-2). Playback sustained; pause/resume, seek, audio-track switch (to index 3), embedded
VobSub selection and external SRT selection all took effect as reported by the server; both subtitle
kinds rendered; stop reached the server ("Stopped at 14000 ms"); the client exited with rc 0 and nothing
remained. Server 344 MiB peak, client 207 MiB peak, scope peak 419 MiB; lowest system MemAvailable 9632 MiB.

Findings (not fixed): (1) combing is visible on moving content with defaults (`deinterlace_auto` off);
(2) the client auto-selected the external SRT (index 0) over the default-flagged VobSub; (3) shutdown
logged an exit-watchdog warning for the third-party websocket thread and exited ~3 s late, rc 0;
(4) the server's own startup "Update Plugins" task contacts the plugin repository (server traffic, not
the client's). The 20 s item is below Jellyfin's minimum resume duration, so resume position is not
testable with it. **Not verified:** real desktop/GPU, visual quality, hardware decode, real DVD or
Blu-ray files, H.264/HEVC, forced transcoding and its reasons, remote (non-loopback) paths, phones,
browser, Windows/macOS/Flatpak. The scripts are in `tools/jf_disposable/`; the repo copy was
path-adjusted after the run and was executed afterwards (sections 10 to 12).

## 10. Real-desktop GPU test and follow-up investigations (2026-11-03)

**Setup.** Same disposable 10.11.8 server and synthetic fixture (section 9), run from the repo copy
of `tools/jf_disposable/` (executed end to end first, in Xvfb, before use: it caught a login-timing
flake in the driver, now fixed). One client window on the real GNOME/Wayland desktop (AMD Lucienne
iGPU), no `vo` or GPU option overridden; the temporary config's `mpv.conf` held only
`input-ipc-server`. The login was seeded through the real GUI form in a private Xvfb, then the
single desktop client reused it. Properties and window screenshots came from mpv's IPC socket
(`xdotool` cannot drive or capture Wayland windows, and a desktop capture would include other windows).

**Actual pipeline (read from mpv, not assumed):** `current-vo=gpu` on `current-gpu-context=waylandvk`
(Wayland + Vulkan); `hwdec=no`, `hwdec-current=no` (software decode of MPEG-2; GPU output is not
hardware decoding); 720x576 yuv420p, bt.601, PAR 1.4222 shown as 1024x576 (DAR 16:9) in a 1280x720
window; container 25 fps, display 60 Hz; server view DirectPlay, no TranscodingInfo.

**Interlacing.** Metadata agrees at every layer: Jellyfin `IsInterlaced=true`, ffprobe
`field_order=tb`, and mpv's per-frame `video-frame-info` `interlaced=true tff=true`. Conditions on
the same frame (counter ~276-278): **A** app default (`deinterlace=no`): combing visible on the moving
diagonal and the grey bar; **B** `deinterlace=auto` and **C** `deinterlace=yes`: clean, output rate
doubled (`estimated-vf-fps` 25 -> 50), the frame's interlaced flag cleared after the filter. So
automatic detection activated correctly **for this file, whose flags are correct**. VobSub and SRT
both rendered on the GPU path. The deinterlace value was written to mpv's `deinterlace` property
(what the app writes); the HUD gear toggle and the `deinterlace_auto` setting path were **not**
exercised on the desktop (the setting is covered by unit tests only).
Not established: perceived smoothness (screenshots cannot show it; 50 fps output on a 60 Hz display
was not examined for judder); VO frame drops were A 0, B 28, C 0 in single runs, the 28 unexplained
and unrepeated, so no conclusion; D/E counters were read after end of file and are void.
Client CPU (whole app, UI included) was 11-26% of one core in every 15 s window, with no clear
difference between conditions; GPU load and GPU memory were not measured; client RSS peaked at
305 MiB, server 360 MiB; IPC round trip 0.1-0.5 ms (max 3.9 ms during the switch to auto).

**Deinterlacing policy (recommendation only; no default changed).** Keep every saved preference as
is. Do not turn `deinterlace=yes` on globally: it would re-filter progressive content. `auto` is the
right *mechanism* (it follows each frame's flag), but it is unproven on the cases that matter for
DVD rips: progressive film in an interlaced-flagged stream, telecined material, files with wrong or
missing flags (those stay combed under `auto`), and real CPU cost of software deinterlacing on weak
laptops. A safe staged change: (1) a HUD hint when the source's metadata says interlaced and
deinterlacing is off; (2) test `auto` on progressive, telecined and mis-flagged fixtures and on
the target low-power machine; (3) only then consider `auto` as the default for fresh installs,
never overriding a saved value.

**Subtitle choice.** Not a client policy and not an account-language rule. The viewer account has
`SubtitleMode=Default`, no language preference; Jellyfin returned `DefaultSubtitleStreamIndex=0`
(the external SRT) in the media source; the client has no `language_config` (unset), no remembered
track on a first item and no explicit pick, so `media.map_streams` adopts the server's value
(`user_sid`). Why Jellyfin prefers the external SRT over the default-flagged embedded VobSub in
Default mode was **not** verified (would need the server source or an A/B with the sidecar
removed); it is the server's decision and not a client bug. Smart Subtitles is not involved.

**Shutdown.** Desktop run: client exited rc 0, 0.1 s after `stop`, no warning, no leftovers (server
stopped by its API; the earlier SIGTERM to the server was ignored). The earlier Xvfb run's
"1 thread did not stop" was the third-party websocket thread parked in `select()`:
`ws_client.run_forever(ping_interval=10)` has no `ping_timeout`, and `stop_client()` closes the
socket from another thread. In isolation, with no JellyHome code, websocket-client 1.9.0 left that
thread stuck in 2 of 10 earlier trials (with and without a prior `shutdown()`), then 0 of 20 more;
a `ping_timeout` was not shown to help. So it is an intermittent upstream race; the watchdog
reported it correctly and its checks were not changed. No targeted fix is justified yet.

**Still unverified:** the HUD deinterlace toggle on a real desktop (it was checked on a virtual display, section 11); progressive, telecined and mis-flagged content;
hardware decode; real DVD/Blu-ray files; weak hardware; perceived motion and visual quality;
H.264/HEVC; forced-transcode reasons; non-loopback networks; Windows/macOS/Flatpak; phones;
browser. Loading `jf_run.py`/`jf_client.py` from the repo is now verified for Xvfb and desktop modes.

## 11. Integration matrix, HUD/settings check and corrections (2026-11-04)

**Correction to earlier reports.** Virtual-display runs without `vo=x11` were not hidden: `xvfb-run` leaves
`WAYLAND_DISPLAY` set, mpv prefers the Wayland compositor, and the windows opened on the real desktop (this is
also why the original "17 open players" were visible). Section 9's explanation of the black Xvfb screenshot was
wrong for the same reason. Unit-test results are unaffected; the real-player integration results were, and
were redone in true isolation. Tooling now unsets `WAYLAND_DISPLAY` (`docs/testing.md`).

**Integration matrix:** all eight legs pass in isolation (467 tests per whole-suite leg, 4 skipped by design,
both backends); resource peak 1601 MiB, lowest MemAvailable 9305 MiB, no leftovers. Details, skips and the
software-GL shim the real-player legs need on a virtual display: `docs/testing.md`. Not covered: `tests/e2e/`
(needs a real server) and any real-GPU run of the matrix.

**Fix made:** `get_local_ips()` blocked the connect path for 5 s on a machine whose hostname does not resolve
(measured on the development laptop; two authentication integration tests failed on it, also on pristine
`HEAD`). Bounded to 0.25 s; regression tests added. Product-wise this is a startup/user-switch latency of 5 s
on such machines, previously unnoticed. (The first version of this fix started one thread per call; the committed
version is single-flight, see section 12.)

**Deinterlacing controls (real UI, virtual display):** the HUD toggle and the `deinterlace_auto` setting both
reach mpv (`yes` for the toggle, `auto` for the setting), the toggle is per session and clears on a new
playback, the setting saves and survives a restart. Defaults unchanged. HUD-toggle behaviour on a real
desktop, with progressive/telecined/mis-flagged content, and on weak hardware remains untested; the
frame-drop observation from section 10 stays unresolved.

**Privacy note (documented, not packet-captured):** `get_local_ips()` does a UDP `connect()` to `8.8.8.8:80` to
read which local address would be used; connecting a UDP socket sends no packet, so this is not a connection to
that address.

**Remaining blockers to a local desktop test build:** none found in the unit suite or the matrix. Unbuilt and
untested: the Windows installer, Flatpak, signing and macOS; real DVD/Blu-ray media; H.264/HEVC at HD;
hardware decode; forced-transcode reasons; remote networks; any server other than the loopback 10.11.8.

## 12. Later work, 2026-11-04: local test build, scrolling, harness leak, commit series

Written after the commit series for this branch; `docs/verification-record.md` has the dependency versions and the
corrections table, `docs/testing.md` the measurements.

* **Local test build.** A Python wheel (3.3.0) was built with the project's prep and `python3 -m build` in a throwaway
  copy, installed into a private virtualenv (Pillow pinned to the tested 12.3.0) and launched on the real desktop with a
  temporary configuration and the host's `libmpv2` 0.40.0: video output `gpu` on `waylandvk` on the integrated AMD
  GPU, the sign-in screen shown about a second after launch, memory flat at ~210 MB, `stop` answered in 0.2 s and the
  process gone in 0.7 s. **Not verified by anything but the person using it:** that the window appeared, responded
  to input, resized and closed as expected. The Flatpak, Windows and macOS builds remain unbuilt.
* **Scrolling.** The user reported choppy scrolling on image screens and a touchpad that was far too sensitive. Two
  separate causes were measured and fixed: every wheel notch moved the drawn content one whole step in one frame (now
  eased), and the renderer never received mpv's wheel `scale` (a touchpad frame counted as a whole 80 px notch, about 31x
  the finger; now ~3.3x). Both devices were judged "much better" afterwards. Open: other touchpads and hi-res wheels,
  dropdown and popup lists, and any real, large library.
* **A harness leak the earlier explanation missed.** The "leaked at least one process" line the integration runner printed
  was attributed to `xvfb-run`'s own Xvfb exiting, which was true for trivial commands but not for the real-player
  legs: the real-player modules share one player singleton that is deliberately never terminated, and on the external
  backend its mpv child was orphaned and swept up by the runner's group kill. Fixed in the entry point and in the
  runner (a bounded grace, and a leaking leg now fails with rc 79).
* **The 5 s connect delay** is bounded by a single-flight lookup (no thread per call); the privacy document now says
  the machine's own hostname is resolved through the system resolver.
* **Corrections to earlier reports** are listed in `docs/verification-record.md` (virtual-display runs were not hidden;
  the black-screenshot explanation; which libmpv the scroll comparison loaded; and others).
* **Still the recommendation.** Nothing here changes section 1: keep improving the fork as a desktop client. Open items
  that matter for the weak target machine are unchanged: no hardware-decode choice has been tested, no H.264/HEVC HD
  file has been played, and the unexplained VO frame-drop observation of section 10 is still unexplained.
