# Verification record for the local test build (2026-11-04)

What was checked, what was corrected along the way, the one expected failure the real-player legs carry, the
exact dependency versions behind every result, and what is still unchecked. It consolidates `docs/testing.md`
and `docs/direction-assessment.md`; where they disagree with this file, this file is later. Nothing here says the
application is universally compatible, completely secure, fully tested or ready for distribution. The changes it
describes are committed on the local branch `jellyhome-privacy-fork` as a series (section 8); nothing has been pushed
or published.

## 1. Corrected findings

Several things reported earlier turned out to be wrong or too broad. These are the corrected versions.

| Earlier statement | Correct statement | Evidence |
|---|---|---|
| Runs under `xvfb-run` were hidden from the desktop | They were not: `xvfb-run` leaves `WAYLAND_DISPLAY` set and mpv prefers Wayland, so those windows opened on the real desktop (this was also the original "17 open players"). Runners now unset it. | `current-gpu-context=waylandvk` on runs meant to be virtual; `tests/integration/run_integration.py`, `tools/run_modules_serial.sh` |
| Black screenshot = Xvfb cannot init a GPU | Same Wayland contamination. On a truly isolated Xvfb mpv refuses software GL unless told `gpu-sw=yes`. | `tools/xvfb_gl_shim/` (opt-in only, `JH_XVFB_GL_SHIM=1`) |
| "leaked at least one process" on every `xvfb-run` leg is `xvfb-run`'s own Xvfb still exiting | True only for trivial commands (Xvfb gone ~10 ms after `xvfb-run` returns). On the real-player legs it was **also a genuine leak**: `test_settings_screen` shares the player singleton and never ends it; on the external backend that is a real `mpv` child, orphaned when the test process exited and quietly swept by the runner. | module run under the scope monitor: `cleanup=FAILED`, 1 mpv; same with the original renderer, so unrelated to the wheel change |
| Settings scrolling was a separate sensitivity problem | It was the touchpad's unscaled pointer frames, i.e. the same root cause as everywhere else. | see section 2 |
| The image-screen scroll comparison ran on the host libmpv | It loaded libmpv from the private toolchain directory (`LD_LIBRARY_PATH` inherited from my shell). The file is byte-identical to the host package's; both runs used the same one. | SHA-256 `a6209320c6f0d216...` for both |
| I inferred which segments were touchpad and which mouse from event rates | Wrong. The devices were identified from the Wayland protocol (`axis_source`). | `WAYLAND_DEBUG=client` capture, filtered to `wl_pointer` scroll messages |
| (process) | I set `JH_MAX_MPV=3` once by mistake (stopped at once) and once on a mutation run where the maximum seen was 1; neither run needed it. The standing threshold is 2. | |
| The commits were each validated and stand alone | Not quite: `README.md` has linked `docs/implementation-summary.md` since the first commit, but the file only arrived in the fifth, so `tests/test_doc_pointers` fails on commits 1-4 (it passes on `origin/master` and on commits 5-11). Commit 1 has a second dangling link, `README.md -> docs/privacy.md`, which arrived in commit 2. I had left that guard out of the first validation lists. Fixed forward, not amended. | `test_doc_pointers` re-run on an isolated export of each of the 11 commits and of `origin/master` (section 9) |
| The glide/gain "neither applies in `aligned`/`row` mode" | Wrong for the gain: it applies in `continuous` and `aligned` mode; in `row` mode a fractional delta is that fraction of a detent without it. Corrected in `docs/configuration.md` and backed by two Lua assertions. | `tests/lua/test_renderer.lua` |
| The bounded hostname lookup is just a timeout on a thread | The first version started a thread per call, which would pile up behind a slow resolver. The committed one is single-flight (one thread at most). | `tests/test_local_ips_bounded.py` |
| (process) | One startup check matched the word "login" inside a traceback and reported success for a launch that had failed before opening a window. | fixed to require the "showing login" log line |

## 2. Scrolling: what was found and changed

- **Touchpad too fast, root cause.** mpv 0.40's `mp.set_key_bindings` calls its handlers with no arguments and
  drops the scroll `scale`; only `complex` bindings get an event table (`player/lua/defaults.lua`). The wheel
  bindings used it, so `on_wheel`'s `e.scale` was always nil and every pointer frame was a whole 80 px notch.
  Before: 1,398 px of finger travel moved the content 42,966 px (~31x). The Lua tests could not see it because their
  fake handed a table to any callback.
- **Fix.** Wheel bindings are `complex`, scalable bindings; the `mpvtk_wheel` section is defined by hand with the same
  name and flags. Fractional deltas get `state.fine_gain` (0.25). An exact 1.0 (a mouse wheel) is a whole notch.
  After: 4,009 px of finger travel moved the content 13,247 px (~3.3x; ~2x for ordinary frames, more for the ~7%
  that mpv clamps to 1.0). No duplicated or dropped events (1,023 app events for 1,023 protocol frames). The gain applies
  in `continuous` and `aligned` mode; in `row` mode a fractional delta is that fraction of a detent, without it.
- **Mouse choppiness.** Every notch moved the drawn content one whole step (~94 px) in one frame. A wheel notch
  now eases the *drawn* offset (`state.glide`, `glide_tau` 0.08 s; it was 0.06 s when first compared, and raised because the
  mouse was wanted a little smoother); stored offsets, Python notifications, the
  scrollbar destination, keyboard and paging, and the boundaries are unchanged. Frame cadence (`TICK`) is
  unchanged.
- **Judged by the user, per device, after the final change:** touchpad and mouse both "much better". This is the
  only evidence for the *feel*; the numbers above describe what the renderer submitted, not what reached the
  screen (mpv exposes no per-frame presentation data for an idle UI).
- **mpv behaviour worth knowing.** mpv puts a 0.125 dead zone in front of every wheel gesture
  (`input.c: process_wheel`): a single tiny delta that starts a gesture is swallowed; a stream is not. On Wayland
  mpv reports `value/10` clamped to +-1 per pointer frame, so fast touchpad frames saturate at a whole notch.

## 3. The remaining expected failure, and the skips

Every real-player leg reports **1 expected failure**. It is the same test on every leg and it is not new:

- `tests/integration/test_mpvtk_hud.py` -- `test_the_console_gets_esc_as_well`, marked `@unittest.expectedFailure`.
- **The defect it pins (known, unfixed):** `phud_summon` binds ESC (and F12) with a bare
  `mp.add_forced_key_binding` instead of the loan-aware helper, so a HUD summoned while mpv's console already has the
  keyboard keeps ESC. Pressing ESC in a half-typed console command then dismisses the bar instead of reaching the
  console.
- **Why it is marked this way:** it is the repo's idiom for a measured-but-unfixed defect. The day someone routes
  those two bindings through the helper the test reports an *unexpected success*, which fails the run until the
  marker is removed. It is not a flaky test and nothing in this work touches it.
- `tests/e2e/test_mpv_reopen.py` carries one more `expectedFailure`; `tests/e2e/` was **not run** (it needs a real
  server).

**How it shows up, and a reporting slip.** unittest prints it as `OK (skipped=1, expected failures=1)` on each real leg
and on both whole-suite legs (and as `OK (expected failures=1)` when the test is run alone, below). The final
verification's own summary lines only extracted the `skipped=` count, so they did not mention it, and I wrongly told
the user afterwards that the failure "did not show up". It did, in all four logs that contain the real-player module (both real legs and both whole-suite legs);
the fake-mpv, agnostic and display-once legs do not contain it. Run alone from the committed HEAD
(`2ceae5c5`, an isolated export, under `tools/limited_run.sh`): the test is collected (1 test), the
`@unittest.expectedFailure` decorator is present (`__unittest_expecting_failure__` is true), and the result on each
backend is an **expected failure** (libmpv 7.7 s, external mpv 8.0 s; `Ran 1 test ... OK (expected failures=1)`; no
leftover process). Not an unexpected success, a skip or an ordinary failure, so the defect it pins is still present.
Neither the decorator nor the assertion was changed.

The **1 skip per real leg** is the other backend's variant of one test, by design: libmpv skips
`test_idle_quit_fires_and_reopens_managed_external`; the external backend skips
`test_idle_quit_fires_and_reopens_on_libmpv`. The two cross-mount-move tests skip when no second volume exists
(`JMS_ALT_VOLUME`).

## 4. Exact dependencies behind the results

**Host (Debian 13 "trixie")**: kernel 6.12.113+deb13-amd64; GNOME Shell 48.7 / mutter 48.7-0+deb13u1 (Wayland
session); display 1.0 scale, 60 Hz; GPU AMD Radeon (RADV RENOIR, integrated) via Mesa `mesa-vulkan-drivers`
25.0.7-2+deb13u1; `libmpv2` 0.40.0-3+deb13u1 (`libmpv.so.2.5.0`, installed on request); `libplacebo349`
7.349.0-3; `libwayland-client0` 1.23.1-3; `ffmpeg` 7:7.1.5-0+deb13u1; Python 3.13.5-1; systemd 257.13-1~deb13u1.
Input devices: built-in touchpad `MSFT0001:00 04F3:31BE Touchpad`; a USB wireless mouse (Trust, the only external
pointer listed; its model was not independently confirmed beyond that).

**Local test build** (`dist/local-test/venv`, the artifact the user ran): `jellyhome-player` 3.3.0 (wheel built with
`gen_pkg.sh --skip-build` + `python3 -m build` in a throwaway copy), `python-mpv` 1.0.8, `python-mpv-jsonipc` 1.5.0,
`jellyfin-apiclient-python` 1.21.0, **Pillow 12.3.0 (pinned, `dist/local-test/PIN.txt`)**, `requests` 2.34.2,
`urllib3` 2.8.0, `certifi` 2026.10.23, `idna` 3.20, `websocket-client` 1.9.2. No tray, MPRIS or Discord extras.

**Test-suite venv** (where the unit, Lua-wrapper and integration suites ran): same `python-mpv`, `python-mpv-jsonipc`,
`jellyfin-apiclient-python`, Pillow 12.3.0 and `certifi`, but **`requests` 2.32.3 and `websocket-client` 1.9.0** (older
than the test build's), plus `pystray` 0.19.5 and `PyGObject` 3.50.0 (which the test build does not have). So the suite
results do not strictly cover `requests` 2.34.2 or `websocket-client` 1.9.2; the test build was launched with them but
not put through the suites.

**Private toolchain (virtual-display runs only, unpacked with `dpkg -x`, nothing installed)**: mpv/libmpv 0.40.0
(the same `libmpv.so.2.5.0` bytes as the host package), `luajit` 2.1.0+openresty20250117-2 (LuaJIT 2.1.1737090214) for
the Lua harness, `openbox` 3.6.1-12+b2. `xvfb` and `xdotool` came from Debian trixie (apt candidates
2:21.1.16-1.3+deb13u4 and 1:3.20160805.1-5.1; the exact unpacked versions were not recorded).

**Disposable server**: Jellyfin 10.11.8 (`jellyfin_10.11.8-amd64.tar.gz`, SHA-256
`d7fba5f34da1e12c5095b17a9cd053d603aaf514e293973a31851f0885633ca1`), loopback only, `--nowebclient`, ffmpeg 7.1.5.

**Repository**: base commit `d7b54f8b` (`origin/master`); the work is the series in section 8 on top of it, none of it
pushed. Resource protection on every run: `tools/limited_run.sh` (3 GiB, no swap, timeout, 4 GiB headroom
watchdog, stop above 2 mpv, leftover check).

## 5. Check counts behind the claims (not a broad rerun)

Lua renderer harness: 537 assertions on the final tree. Real mpv, libmpv and external backends: `test_mpvtk_browser`, `test_mpvtk_hud`,
`test_settings_screen` (58 tests, both backends). The runner's real external-backend leg: 107 run and 1 skipped with the
wheel tests (104 run at the harness commit), plus the suite's 1 expected failure, no leak. Each commit's own focused
results are in its message. New or changed tests: `TestFractionalWheelReachesTheRenderer` (real mpv, both
backends), the glide/boundary/reversal/touchpad blocks in `tests/lua/test_renderer.lua`,
`IntegrationEntryPointTeardownTest`, `test_xvfb_gl_shim`, `test_local_ips_bounded`, the leak-probe tests in
`test_integration_runner`. One existing Lua assertion was **changed on purpose** (a fling "paints where the wheel
put it" became "trails the wheel, never ahead, arrives exactly"); mutation runs confirm the new tests fail on a wrong
implementation.

## 6. Not verified (carry these forward)

- A real, large library: longer lists, poster loading, repeated scrolling.
- **Dropdowns and popup lists:** they take a fixed three rows per wheel event regardless of scale, so they may
  reproduce the touchpad problem. Untested on a touchpad.
- The `aligned` and `row` scroll modes by hand (their glide and gain behaviour is Lua-tested only), and the paginated mode.
- Hi-res wheels and other touchpads. A *fractional* hi-res wheel is scaled like a touchpad and would feel about 4x
  slower per notch than a whole-notch mouse. The gain is validated on one touchpad and one notch mouse only.
- Pillow 13, `requests` 2.34.2 and `websocket-client` 1.9.2 under the suites.
- Windows, macOS, Flatpak builds; `tests/e2e/`; real DVD/Blu-ray files; HD H.264/HEVC; hardware decode; non-loopback
  networks; packet-level confirmation of the LAN discovery broadcast and the UDP `connect()`.
- The unexplained VO frame-drop observation from the desktop playback test remains unresolved.

## 7. Deferred, by decision

- **No sensitivity setting yet.** Wait for the real-library trial. If another device needs adjusting, add a
  **"Fine-scroll sensitivity"** control that explicitly covers touchpads *and* high-resolution wheels. It must not be
  called "touchpad sensitivity": the renderer does not identify the device, it scales every fractional delta the same
  way, so that name would misdescribe what it does. `state.fine_gain` is a code constant today.
- No further renderer tuning, and no broad test rerun, unless the manual checks expose a problem.

## 8. The commit series

On top of `origin/master` (`d7b54f8b`), oldest first. This record, the security review, the direction assessment and the
result logs in `docs/testing.md` are the last commit.

```
862e5ba1  fork: give JellyHome Player its own identity, packaging and first-run
9e491e86  privacy: opt-in update checks, no third-party lookups, per-server connection type
f699edd1  feat: opt-in Start With Computer and an explicit import-upstream command
7ab2a413  tests: bounded, isolated test runs with guaranteed player teardown
8f5086b0  docs: add the reviewed implementation summary the README points to
75c182fc  tests: run the audio-settings apply path on the external-mpv read path too
eb6837c1  fix: bound the machine-hostname lookup on the connect path to one thread
5e605561  renderer: ease the drawn offset for discrete wheel notches
6cd850e9  renderer: honour mpv's wheel scale, so a touchpad moves content with the finger
590af06b  tools: disposable Jellyfin 10.11.8 server and client drivers for playback and scroll tests
```

Commits 1-4 each carry a dangling README pointer (`docs/implementation-summary.md`; commit 1 also `docs/privacy.md`, see
section 1 and section 9); the fifth fixes the last one. The eleventh commit is the one that adds this record, so it is
not in the list above.

## 9. Final verification of the committed series (HEAD `2ceae5c5`)

Run from an isolated export of HEAD (874 tracked files), serially, each module or leg in its own
`tools/limited_run.sh` scope (3 GiB, no swap, `JH_MAX_MPV=2`), Wayland unset, no leftover process in any scope.
**This is validation of the final tree only.** It says nothing about the intermediate commits except where the next
table says so.

| Part | Result |
|---|---|
| Unit modules | 285 of 285 passed (286 `Ran` lines, about 7,290 tests, no `FAIL`/`ERROR`) |
| Lua renderer harness | 537 assertions, 0 failing |
| Integration: agnostic / display once | 84 run, 2 skipped / 15 run |
| Integration: fake mpv, both backends | 264 run, 1 skipped each |
| Integration: real mpv, both backends | 107 run, 1 skipped, 1 expected failure each |
| Integration: whole suite, both backends | 470 run, 4 skipped, 1 expected failure each |

A first attempt at this run tested nothing (the export step failed silently and the scripts ran against an empty
directory); it was stopped, nothing was left running, and the run was redone.

**Intermediate commits: only `tests/test_doc_pointers` was checked, on an isolated export of each commit.** No other
guard or test was re-run on commits 1-10.

| Commit | `test_doc_pointers` |
|---|---|
| `origin/master` `d7b54f8b` | passes |
| 1 `862e5ba1` | fails: `README.md -> docs/implementation-summary.md` and `README.md -> docs/privacy.md` |
| 2 `9e491e86`, 3 `f699edd1`, 4 `7ab2a413` | fail: `README.md -> docs/implementation-summary.md` |
| 5 `8f5086b0` to 11 `2ceae5c5` | pass |

History was not rewritten.

### The local Debian test wheel, rebuilt from the committed source

Built in a throwaway copy of `git archive HEAD` for source SHA
`2ceae5c5a61901343d4d285f5ad32f07fd4e534f`, with `gen_pkg.sh --skip-build` and `python3 -m build --no-isolation`
(Python 3.13.5, setuptools 78.1.1, wheel 0.46.1, build 1.6.1 from the suite venv). The shader pack was not
downloaded again: the copy used the cached `v3.0.0`, which is also the latest upstream release when checked.
`gen_pkg.sh` rewrites the tracked `locale_index.py` in the copy (1146 msgids), as documented; the checkout is untouched.

| Artifact | SHA-256 |
|---|---|
| `jellyhome_player-3.3.0-py3-none-any.whl` (360 files) | `08e1199237714646602e606c163d0e0d00181a97f78ff4bf0ca70d8f27f4350c` |
| `jellyhome_player-3.3.0.tar.gz` | `bf652a7e1fac440384120d736a0e4322f91ee0b3e87dd58ae44a85e288cc1c4d` |

The wheel's own requirements are unpinned (`python-mpv>=1.0.8`, `jellyfin-apiclient-python>=1.20.2`,
`python-mpv-jsonipc>=1.5.0`, `requests`, `pillow`, `certifi>=2026.10.19`); the tested versions are the ones in
section 4.

**Does the installed build match it? Not byte for byte.** The installed test build (`dist/local-test/venv`) is
identical, file for file, to the earlier wheel `dist/jellyhome_player-3.3.0-py3-none-any.whl` (SHA-256
`094fb35ef233b5cf318daac3828dad57e1c78fe5ac3746ce30beed841b48a887`, built 16:41): the installed package's files have
the same contents as that wheel's. The new wheel differs from it in exactly one file, `mpvtk/renderer.lua`, and only in the
comment above `fine_gain` (the committed text mentions hi-res wheels and the future "Fine-scroll sensitivity"
setting). The code is the same, so what the user judged "much better" is what is committed. The installed build was
not replaced.
