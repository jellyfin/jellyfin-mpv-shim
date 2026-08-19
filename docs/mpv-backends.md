# Talking to mpv

Reference for the seams between the shim and mpv: the two backends and where
they diverge, the mpv versions and build variants that behave differently, and
the mpv state that outlives a single queue item.

This is the derivation behind the one-line warnings in `player.py`,
`mpv_events.py`, `mpv_options.py`, `player_window.py` and `video_profile.py`.
Each of those states its conclusion at the line; this file records how it was
established, so the code does not have to.

## 1. Two backends

`player.py` picks one at import time and aliases both as `mpv`:

- default — `import mpv`, the `python-mpv` libmpv binding;
- `settings.mpv_ext`, or libmpv failing to load (`OSError`) — `python_mpv_jsonipc`,
  with `is_using_ext_mpv = True`. macOS forces this; libmpv is not reliable there.

### What differs

| | libmpv (`python-mpv`) | external (`python-mpv-jsonipc`) |
|---|---|---|
| shutdown errors | `BrokenPipeError`, `mpv.ShutdownError` | `BrokenPipeError` |
| observer registration | `observe_property` | `bind_property_observer` |
| unknown attribute | `__getattr__` issues a **property read** | plain `AttributeError` |
| a core in this process | yes | no — mpv is a child process |
| property read cost | in-process call | synchronous IPC command |

`_mpv_errors` is the tuple to catch. `mpv_events.wait_property` and
`PlayerManager._observe` both discriminate on the **class**, not on a module
flag, so they carry no global state and stay testable against a fake.

### Which option an exception blames

Both backends can name the offending option and neither does it the same way:

- **libmpv** raises `AttributeError('mpv option does not exist', -5, (handle,
  b'input-gamepad', b'yes'))` — the name is in the third arg.
- **python-mpv-jsonipc >= 1.3.0** raises `MPVProcessError` with `bad_option`
  set, having asked mpv why it refused to start. Older versions flattened every
  start failure into `"MPV process retry limit reached."` *after spending the
  whole retry budget*, which is why the shim's floor is 1.3.0.

The answer is returned in the **underscored** form, because that is how the
option appears in the dict handed to `mpv.MPV`.

### Bounding IPC replies during teardown

Only the external backend needs this. Every command there is a request/response
over a socket, and the reply is waited for with
`python_mpv_jsonipc.TIMEOUT`, which is **120 s**.

A closing window puts that squarely in the failure path: mpv can accept a
command, run it, and exit before its reply is written back. Observed on the
close path, where trickplay's overlay-clear reached mpv (it logged "Clearing
trickplay") but the reply never came, parking the action thread for two minutes
with the whole shutdown queued behind it. libmpv has no equivalent — a dead
handle raises immediately, which is why the same close is instant there.

Bounding the wait is the fix rather than hunting individual calls: any command
issued while the window is disappearing can lose its reply, and during teardown
there is no command whose answer is worth minutes. `TIMEOUT` is a module global
read at each wait, so lowering it takes effect immediately.

### Bound methods cannot be property observers on libmpv

libmpv's `property_observer` decorator writes an `unobserve_mpv_properties`
attribute onto the callback it is handed. A bound method has no `__dict__` to
take it, so this raises `AttributeError` — on **exactly one backend**. jsonipc
and the `FakeMPV` harness both accept it, so nothing catches this until a real
libmpv runs. Register through `observe_property` / `bind_property_observer`
instead, which is what `PlayerManager._observe` exists for.

### Read raw state out of `__dict__`, not by attribute

Because libmpv turns an unknown attribute into a property read, `getattr` on a
half-constructed or dying object asks a core that is in no state to answer.
Anything inspecting an mpv object's internals — notably the orphan reaper below —
must go through `__dict__` directly.

### Extracted modules must import backend globals per call

`is_using_ext_mpv`, `_mpv_errors`, `discord_presence` and `win_utils` are
imported **inside the method**, never at module scope. Module-scope binding
captures the backend, and the integration harness swaps a fake mpv in and out by
evicting modules from `sys.modules`; a second module holding a bound copy would
have to be evicted in lockstep, and it fails only on the whole-suite leg. Each
extracted module carries a subprocess guard test pinning this.

## 2. Construction failures

### A failed `mpv.MPV(...)` leaves a *running* mpv behind (libmpv)

python-mpv sets every option inside a `try` whose `finally` is `mpv_initialize`
(mpv.py 1.0.8, `MPV.__init__`), so an option this build does not have raises
*after* the core has come up. What is left is not a stale handle — it is a
running mpv with its window, its scripts and its threads, owned by a half-built
object nobody holds a name for. Nothing stops it until the garbage collector
reaches the reference cycle the traceback made, and by then the retry's mpv is
up: two cores, two VOs on one display, and the process dies in a thread that is
not Python's.

Measured with `--input-gamepad` on a libmpv without it, three runs in three. The
crash lands *after* the retry has already logged success, which is what made it
read as "the retry failed".

So the orphan is reaped where it is made. The instance is recovered from the
traceback because that is the only place it exists — the constructor never
returned it — and identified by holding a live handle rather than by its class,
since the external backend has no core in this process to reap.

Both option retries below depend on this; the lua fallback creates an orphan too,
and has since it shipped.

### A build without lua refuses to start, it does not ignore `--osc`

Measured against a `-Dlua=disabled` mpv 0.41, on both backends:

- libmpv raises `AttributeError` from the constructor;
- the external binary exits with `Error parsing option osc (option not found)`,
  arriving as `MPVError("MPV process retry limit reached.")`.

The two reports are entirely different and **none of it has to be parsed**,
because `--osc` is the single lua-gated option the shim sets. Failing to
construct *with* it and succeeding *without* it is not evidence needing
interpretation — it is the answer. Dropping the option is the right answer
rather than a workaround: the OSC being turned off is itself lua, so a build
without lua has none to turn off.

This made the whole lua fallback unreachable before it was handled: `lua_works`
needs a live mpv to probe, and the app died constructing one.

The answer is recorded rather than rediscovered, and **only in this direction**.
mpv having `--osc` says lua was *compiled in*, not that it runs — lua that loads
and then errors is exactly what the probe is for — so the ordinary path is left
to ask.

Re-learning is expensive: on the external backend a failed construction used to
consume the whole start-retry budget, measured at ~31 s with the shipped
defaults, paid on every re-open (idle-quit then a cast, `set_browse_window`,
`force_window`).

### Probing for lua

`lua_works()` loads `lua_probe.lua` and waits for it to report back. A probe
rather than a capability string, because `mpv-configuration` does not mention lua
on every build (measured on this one) and `load-script` on a script that cannot
run **raises nothing on either backend**. Only a script reporting back is proof,
which also catches lua that loads and then errors.

Costs ~10 ms when lua works (measured), and the full timeout once when it does not.

Everything the shim draws depends on the answer: the library browser and the
playback HUD are `renderer.lua`, the stock OSC is lua, `mouse.lua` is lua. An mpv
without it leaves the app running and drawing nothing but video — and, before
this existed, with no menu either, because `toggle_settings_menu` refuses the OSD
menu whenever the *configured* style is mpvtk, live renderer or not.

## 3. mpv versions

### `force-window` is only live from 0.41

Every mpv *stores* the property; only 0.41 and newer create or destroy the video
output in response to a change made while idle. Older builds decide at startup
and never revisit it — so the window has to be asked for on the command line, and
releasing it later does nothing.

An unreadable version is treated as **old**, because the two ways of being wrong
are not symmetric: assuming old costs a fallback that works everywhere, assuming
new costs a window that will not go away.

This only started mattering when the browser stopped loading a background file.
Historically the window was summoned by loading a file and released by unloading
one, so `force_window` was a flag alongside real media; `PlayerManager.force_window`
still works that way. The browser loads nothing deliberately — reloading a
background file tears the video output down and reads as the window closing and
reopening — and so inherited the newer behaviour.

### A negative absolute seek is read as end-of-file

Measured on mpv v0.41.0: `seek -0.005 absolute+exact` on a 30 s file lands at
29.96 with `eof-reached` true. mpv does not clamp it.

This is #614. A matroska chapter can start at a slightly negative timestamp —
container start-time offsets put the first one at -0.005 on an ordinary episode —
so "previous chapter" hit EOF, and the shim's own EOF observer advanced the
queue. That is the reported "prev chapter plays the next episode". It predates
the current branch: master's `hud._chapter_jump` passes `ch["time"]` on just as
unclamped.

Both the chapter arithmetic and `seek()` itself refuse a negative absolute seek,
because the chapter *picker* hands over the very same value.

### Chapter-jump semantics

The asymmetry is mpv's `add chapter -1`, and every other player's: going back
restarts the chapter you are in unless you are within its first couple of
seconds, in which case you meant the one before. Going forward has no grace at
all — it is the next boundary strictly ahead, not "ahead by half a second". A
position is a float from mpv and is never exactly a boundary, while the half
second before one is half a second of real playback in which the button would do
nothing.

Both directions can answer "nowhere to go", and the caller must then not seek.
Before the first boundary there is nowhere to go, and a button that quietly
restarts the file is worse than one that declines. That covers the first seconds
of any file, and a file with no chapters at all, where every press used to jump
to 0.0.

## 4. SDL, gamepads and signals

mpv's gamepad support is SDL2, and `SDL_Init` installs its own SIGINT/SIGTERM
handlers unless `SDL_NO_SIGNAL_HANDLERS` says not to. It only replaces a handler
still at `SIG_DFL`, which is why standalone mpv is unaffected — it installs its
own first.

**This process is no longer the case that needs the variable.**
`mpv_shim._claim_sigterm` installs a real handler before anything can import the
player, so SDL finds SIGTERM already taken and leaves it — measured with the
variable deliberately unset. That fix is preferable because it does not depend on
a `putenv` here being visible to a `getenv` inside SDL2, which is certain on
glibc and unverified on Windows. The variable stays as the layer under it.

**The child mpv of the external backend has no handler of its own.** jsonipc
spawns it with `terminal=no`, and mpv installs its SIGTERM handler from
`terminal_setup_getch`, which that path skips. So the child's SIGTERM is at
`SIG_DFL`, SDL takes it, and `MPVProcess.stop()` — a `terminate()`, i.e. a
SIGTERM — is swallowed, leaving an orphaned mpv window behind. The variable is
inherited by the child, which is the only lever this side has. (Sending mpv
`quit` over the IPC socket instead of signalling it is the better answer and
belongs in python-mpv-jsonipc; until then, this.)

**Set unconditionally, not only when the shim passes the option.**
`input-gamepad` is an ordinary mpv option with no `M_OPT_NOCFG`, so a line in the
user's own `mpv.conf` starts the SDL thread with the option absent from anything
the shim built — and that config is exactly what `mpv_ext_no_ovr` users are told
to use. There is no third place to ask and no way to ask mpv in time, since the
thread starts inside `mpv_initialize`. The only correct gate is no gate. The
variable does nothing at all in a process that loads no SDL.

The cost is one real side effect, accepted deliberately: `os.environ` is
process-wide and nothing spawns children with a scrubbed environment
(`system_open`, the clipboard helpers, the shell-command hooks), so an SDL
application launched from the shim inherits it and loses SDL's own Ctrl-C
handling. Narrower gating buys a silently orphaned mpv back.

A blank value counts as unset, which is SDL's own reading: `SDL_GetHintBoolean`
returns the *default* for `""`, so preserving one would leave the bug in place
for anybody whose launcher exports an empty variable. `"0"` is honoured — somebody
who wrote that wants SDL's handlers — and logged, because on the external backend
it is the one value that can still strand an mpv window.

`GAMEPAD_OPTION` is likewise gated on a build-time capability: the SDL thread is
never started on a build without it, and the setting would look applied and do
nothing.

## 5. Input sections and key claims

The shim installs its keys as mpv input **sections** (jsonipc cannot unbind), and
claims a key only where its own verb differs from mpv's. See `keysweep.py` for
how the resolved bindings are read, and `input_conf.py` for the one-time
migration of the old `seek_*` settings into the user's `input.conf`.

### `SECTION_FLAGS = "allow-hide-cursor+allow-vo-dragging"`

The same string mpv's own `defaults.lua` gives a script's bindings, and the same
one python-mpv gives every key bound through it.

A section's mouse area defaults to the **whole screen** (`input.c`:
`get_bind_section` starts it at `INT_MIN..INT_MAX`), and an enabled section
covering the pointer *without* `allow-hide-cursor` is how mpv is told "a script's
UI is under the mouse": every call to `mp_input_get_mouse_event_counter` bumps
the counter, which re-arms `handle_cursor_autohide`'s timer, so **the cursor
never hides again**. The shim's sections hold keyboard keys and have no UI at
all, so the fullscreen standing claim — installed at mpv creation and never
released — left a pointer sitting over every film for the whole session.

`allow-vo-dragging` is the same mistake in the other direction: without it
`mp_input_test_dragging` refuses to move the window from a drag on the video,
which is mpv's own behaviour everywhere else.

`renderer.lua` withholds `allow-hide-cursor` from its **own** mouse sections on
purpose — while the library or the HUD is up the pointer really is over a UI —
and enables them only for as long as it is. The player's sections are enabled for
the life of the process.

### Which keys the shim binds, and why so few (#16)

The governing rule is *stop intercepting keys whose meaning we did not change*.

- `kb_menu_*` are **menu** keys and are not bound at the player at all. The OSD
  menu installs them itself for exactly as long as it is on screen
  (`claim_menu_keys`); the rest of the time the key is mpv's. One setting used to
  mean two things — which key drives the menu, and which key seeks — and almost
  everybody who touched it was reaching for the second in order to get rid of it.
- `f` is mpv's own key with mpv's own meaning, so fullscreen is *claimed* rather
  than bound; the claim follows a remapped key where a fixed binding never did.
- Seeking is mpv's too, unless the shim's own seek does something mpv's cannot —
  then it is claimed. `_MPV_EQUIVALENT_SEEK` is what decides, and only
  `use_web_seek` is left in it: the six seek settings that used to be there are
  gone from the config entirely, because a distance now lives in the user's
  `input.conf`. While they existed they were actively misleading — a changed
  distance made the check return True, and the resulting claim then seeked by the
  amount in *mpv's* binding rather than by the setting.
- `skip_intro_on_seek` deliberately has no key. `_on_seeking` observes the
  `seeking` property and applies it to *any* forward seek, mpv's own bindings
  included. Claiming a key would double-handle, since the claim's own `seek()`
  raises that same observer.
- ENTER confirms the OSD menu and does nothing else. It is swallowed rather than
  left to mpv, which binds it to `playlist-next`; the shim's mpv playlist holds
  one file, so what that does depends on `keep-open` and is not a behaviour to
  inherit by accident. It must not *open* the menu either — `menu_action("ok")`
  on a hidden menu is `show_menu()`, and under mpvtk the OSD menu draws as mpv
  OSD text, landing under the overlay bitmaps and taking the arrow keys with it.

## 6. mpv state that outlives a queue item

mpv is **not** re-created between queue items, so any global, persistent option
written for one item is still set for the next — including a next item the shim
deliberately *refuses* to set it for. Three cases, all of which have bitten:

### `http-header-fields`

Everything mpv fetches for a file goes through it — the stream, any external
subtitle sidecar — so one option covers them all and none of those URLs needs a
token in its query string.

`Authorization: MediaBrowser Token="…"` is the one header scheme the server does
not gate behind `EnableLegacyAuthorization` (`AuthorizationContext`);
`X-Emby-Token` and friends are all legacy. The apiclient already builds exactly
this line for its own requests, so it is borrowed rather than re-spelled.

**Clearing it up front is load-bearing.** Without the clear the refusal defeated
itself: auto-advance from a normal item to one whose subtitle lives on a
third-party host, and mpv sent the previous item's `Authorization` to that host
while the log said it had not. Clearing once at the top rather than on each
`return False` is the point — every exit path past that line leaves mpv holding
nothing, including the ones nobody has written yet.

Failure returns False rather than raising, and the caller falls back to putting
the token in the URL. mpv has had `http-header-fields` for over a decade so this
should not happen, but the cost of being wrong is that nothing plays at all.

### Motion interpolation

**"Off" writes nothing until we have written something.** Every one of these is a
property somebody may have set in their own `mpv.conf`, and the default of the
setting is off — so an off that wrote its idea of "not interpolating" would reach
out on the very first item and turn off frame blending the user configured
themselves, for everyone, with no setting here that puts it back. That is the
mistake `hwdec_pinned_by_config` exists to avoid.

It is not enough to avoid it for `video-sync` alone: writing `interpolation=no`
while carefully preserving `video-sync=display-resample` leaves the **worst
pair**, paying that mode's cost with the feature it exists for switched off.
(mpv's manual: `--interpolation` "requires setting the --video-sync option to one
of the display- modes, or it will be silently disabled".)

So the undo is symmetric with the do. The first time a preset is applied, the
previous value of every property any preset touches is kept, and off restores
exactly those. `_interp_saved` is None while nothing has been written, which is
also the "leave it alone" signal.

### `keepaspect`

Owned by `set_browse_window` (off, so the library window resizes freely instead
of snapping to the last video's shape) and `browse_yield` (on). The comic reader
only *borrows* it. See `docs/artwork-pipeline.md` and `player_window.py` for the
handoff ordering, which is the expensive half.

## 7. Frames in system RAM

The direct hardware-decoding modes hand mpv frames that live on the GPU, which a
video filter cannot read — so where there is a filter, hardware decoding has to
be the copy-back kind or it silently does not apply. Three sources, none a guess:

- the active shader profile said so (`wants_copy_hwdec` — the pack names a
  `-copy` mode because it knows what it will do with the frames);
- SVP is enabled, which means a VapourSynth filter in the user's own `mpv.conf`;
- mpv reports a filter chain. This is the general case and catches the other two
  once playback is running, but it is asked separately because it is the only one
  that sees a filter the app knows nothing about.

Never raises: an unanswerable question means "no filter", and the cost of being
wrong is a filter that does not apply, not a player that fails to start.

## 8. mpv going away and coming back

mpv is torn down and re-created across idle-quit and crash recovery. Anything
holding the raw handle has to follow it: the OSD menu does this via
`menu.update_player()`; the in-window UI attaches a whole renderer, so it gets
explicit hooks.

**Two phases, and the distinction is load-bearing:**

- `on_mpv_gone` — the handle is no longer ours. Stop pushing to it. mpv itself
  may still be running: terminate happens on its own thread, so this fires while
  the process is on its way out.
- `on_mpv_terminated` — mpv is actually dead. Only now is it safe to free
  anything mpv reads **by address**, i.e. the in-process BGRA tile buffers.
  Freeing them at `on_mpv_gone` time released memory a live mpv was still
  compositing from every frame, which is a segfault on quit.

`on_mpv_recreated` fires once a fresh handle is ready.

### Discovered capabilities must outlive the mpv they were discovered on

`_init_mpv` runs again on every re-creation — an idle-quit then a cast,
`set_browse_window`, `force_window`. Re-resolving the OSC style from settings
there put it back to `mpvtk` with no renderer behind it and `on_hud_menu` still
None, so `toggle_settings_menu` went back to refusing: no HUD, no OSD menu, no
way to reach either. One idle timeout undid the whole lua fallback. Hence
`_osc_style_override`, which is applied on top of `resolve_osc_style()` and kept.

Its answer feeds `mpv_scripts` and `build_mpv_options` as well, because there is
no point handing lua scripts to an mpv already known not to run them.

## 9. Waiting for a property

`mpv_events.wait_property` blocks until a property satisfies a condition, on
either backend. Four things about it are not obvious:

**`skip_initial` guards against a value from the *previous* file.** Both backends
deliver one initial property-change notification carrying the property's current
value the instant the observer registers. When a prior file is still loaded
(cast-while-playing, or auto-advance with `keep_open` holding the finished file),
that value belongs to the old file. So the property is sampled at registration:
if it already satisfies the condition it is a stale ready value and the first
notification is dropped (mpv re-delivers the same value); if it does not, there
is nothing stale to skip and the first qualifying notification is accepted, which
keeps the normal first-play path working even if the file loads before the
observer is processed.

*Residual race:* if the new file finishes loading before the sample is taken, the
sample is already fresh. The first notification is only dropped when it
re-delivers the exact sampled value, so a fresh value that *differs* is accepted;
only a new value equal to the stale one (same-duration reload) is
indistinguishable, and the caller's `timeout` bounds that case.

**The wait is poll-assisted, on its own daemon thread.** Besides the observer the
property is re-read every `POLL_INTERVAL_SECS`. Observer events are the fast path;
the poll rescues the wait when property-change delivery is lost — the external
backend's IPC pipeline (socket reader → event queue → handler) has been seen in
the field to drop notifications, which turned an otherwise fine playback start
into a hard "no duration" timeout that killed the session. **Do not simplify this
away.**

It runs on its own thread because on the external backend a property read is a
synchronous IPC command with a long internal timeout (120 s in
python-mpv-jsonipc), so polling on the waiting thread would let a wedged mpv
stretch the caller's deadline by minutes. This way `timeout` stays a hard bound;
a poller blocked on a wedged read just exits late, alone.

**`abort`** lets the caller give up early — used when mpv reports the file failed
to load, where waiting out the full timeout for a duration that can never arrive
just freezes the UI. Observed by the poll thread, so it lands within one poll
interval rather than instantly; that turns a 30 s hang into a sub-second one. An
aborted wait returns False, exactly like a timed-out one.

**`satisfied_by`** is the mirror: an Event that ends the wait *successfully*, for
when something other than the property proves what the caller was really waiting
for. The playback start uses it to accept mpv's file-loaded event, because
`duration` never arrives for a live or otherwise unbounded stream and waiting it
out would kill a stream that is in fact playing.

### When mpv says nothing at all

A remote origin that stops delivering **without closing the connection** produces
no statement of any kind: the demuxer blocks in read, so there is no end-file
event, `eof-reached` stays False and `playback-abort` stays False. With
`keep_open` holding the last frame mid-queue that is indistinguishable from a
normal hold — the queue just stops forever. Reported against `.strm` items, whose
origins are arbitrary third-party servers, but nothing about it is `.strm`-specific.

The rescue deliberately requires the position to be at the **end** of the media,
not merely frozen. A bare stall is far more likely to be rebuffering on a slow
origin, and advancing through that would silently skip the rest of an episode — a
worse outcome than the freeze it fixes. Items with no known duration therefore
get no rescue, and live streams none either: a stall there is an outage, and
"finishing" one would advance past a channel the user is still watching.

## 10. Why the video settings default the way they do

`conf.py` states each default at its declaration. This is the evidence behind the
three that are off, all of which look conservative and are not.

### `hwdec` — off, following mpv rather than the other clients

mpv's manual on turning it on: "acknowledge that this may cause problems", and its
maintainers decline to default it on (**mpv#12948**) because particular vendor/GPU
combinations are badly broken — AMD vaapi on Linux causing GPU resets, vp9 on Intel
Macs hanging mpv before the window even opens.

Jellyfin Media Player did default it on and it worked for most people. This is
about the tail it did not work for, who are disproportionately the people on
hardware that needed it.

`over-1080p` is the option that exists because **we can do what mpv cannot**: the
source resolution is in the DTO before playback starts, so decoding can be software
where software is fine and hardware only where it is not. Most hardware of the last
decade decodes 1080p without help, and often looks better doing it.

### `deinterlace_auto` — off, which is also mpv's default

The interlaced flag is not reliable in either direction. Plenty of interlaced DVD
and broadcast rips are not marked, and plenty of progressive files carry the flag
from whatever produced them. So `auto` is right much more often than it is wrong,
but the case where it is wrong — deinterlacing progressive video — softens a picture
that was fine.

The per-session toggle in the playback HUD's gear menu is the answer for the other
half: the file that *is* interlaced and does not say so. It lasts until the library
comes back or the window is closed.

`auto` is **mpv 0.38+**. An older build rejects the value and the setting behaves as
off. `PlayerManager._apply_deinterlace` will **not** substitute `yes`: forcing
deinterlacing on everything is not a degraded version of this, it is a different and
worse setting.

### `motion_interpolation` — off, and not because it is expensive

This is frame **blending**, not motion compensation: it resamples along the time
axis so a 24fps film on a 60Hz screen stops repeating frames unevenly. It does not
synthesise motion the way SVP does. SVP was looked at and rejected — it is paid on
Linux now, and its dependencies are heavy enough that shipping them is not on the
table.

The reason for the default is a **clock-reading problem, not a cost**. Measured on a
9950X3D + RTX 4090, it dropped frames badly — on a *multi-monitor X11 desktop whose
screens run at different refresh rates* (144Hz beside two at 60). That is not a
hardware limit. From mpv's manual on `--display-fps-override`: "on multi-monitor
systems, there is a chance that the detected value is from the wrong monitor", and
"setting an incorrect value (even if slightly incorrect) can ruin video playback".
All three modes follow that clock, so all three inherit the problem, and a
mixed-refresh desktop is common enough that the default has to assume it.

The first thing to try is mpv's own
`display-fps-override=<the refresh of the screen you watch on>` in the user's
`mpv.conf` — but that did **not** fix the machine above, so the mismatched-desktop
case is not fully understood and this is not a workaround to promise anyone.
Deliberately not a setting of ours either way: we would be guessing at which
monitor, which is the thing that is already wrong.

It also costs GPU on every frame, mpv reverts to audio timing on its own for
low-framerate or VFR content, and `display-resample` adjusts audio speed to track
the display — which is exactly what somebody bitstreaming to a receiver does not
want.

## 11. Shader packs, the GPU API and colorspace

A shader pack is not only shaders — a profile carries a list of mpv *settings*,
and those are written straight into the live player. Three of them ask for things
the pack is not entitled to decide, and `video_profile.py` refuses each for a
different reason.

### `gpu_api: opengl` and `fbo_format: rgba16f` are a 2020 pair

default-shader-pack **84fc5df** ("Fix Windows and external MPV compatibility
issues") added both in one commit, and the pairing is the whole explanation:
`rgba16f` is an *OpenGL-backend* format name. The Direct3D 11 backend spells the
same format `rgba16hf` (mpv `video/out/d3d11/ra_d3d11.c`), so on d3d11 the pack's
value fails to initialise, mpv falls back to **dumb mode**, and dumb mode disables
every user shader — silently, with a working picture. Pinning `opengl` made the
format name true again, which is a workaround for the format, not a requirement of
the shaders.

Both are dropped now:

- **`fbo_format`** — mpv's `auto` already tries 16-bit float first (`rgba16f`,
  `rgba16hf`) on whichever backend is live. That is exactly what the pack was
  asking for, spelled portably.
- **`gpu_api: opengl`** — the shaders do not need OpenGL. mpv cross-compiles user
  GLSL to SPIR-V, and the pack's profiles run unmodified on Vulkan and compile
  clean through the d3d11 chain. Forcing OpenGL **costs HDR on Windows**, because
  the autoprobe order there is d3d11, then Vulkan, then OpenGL last
  (mpv `video/out/gpu/context.c`) — so the pin does not merely fail to help, it
  demotes the backend past the two that can do HDR.

Only that one legacy value is refused. A profile naming some *other* API means it
— a Direct3D 11 video filter such as RTX Video Super Resolution genuinely cannot
run on another backend — so those pass through, and the user's own
`shader_pack_gpu_api` outranks everything.

A pack may still name an API this build has no context for (a d3d11 profile read
on Linux). mpv rejects the value outright; `load_profile` swallows that rather
than losing the rest of the profile, because running the shaders on the API we
already have beats raising into the menu.

### `hwdec` — a naive value is a policy, a named decoder is a requirement

Every profile pulls in a `hwdec-default` group setting `hwdec` to `auto-copy`.
That is a policy — "use hardware decoding if you can" — and it is not the pack's
to have. Author's own account: *"this was just me being risk-averse in the past."*

`hwdec` is a user-facing setting that defaults **off** for the reasons in §10, up
to and including mpv hanging before the window opens (**mpv#12948**). A shader
profile switching it back on gets the resulting breakage attributed to the
profile, which is the last place anyone would look. So a naive value is dropped.

A *specific* decoder is different in kind. The shipped `rtx-vsr` names `d3d11va`
because its `d3d11vpp` filter operates on Direct3D surfaces: the profile does not
work without it, and choosing that profile **is** the opt-in. Applied, and
remembered on the manager, so the per-item write in `_play_media` does not undo it
on the next file.

Two things outrank a pack as well as the setting, and both are checked here rather
than left to `_play_media` — a profile writes its settings directly and would
otherwise slip past the pin between one file and the next:

- the user's own `mpv.conf`, where a pin means nothing writes the option at all;
- `--disable-hwdec`, the recovery path for hardware decoding stopping the window
  from opening.

### The idle window keeps a stale colorspace hint (#605)

**mpv only revisits the swapchain's colorspace while a video frame exists.**
`vo_gpu_next.c`:

```c
if (target_hint && frame->current) {
    ... set_colorspace_hint(p, &hint);
} else if (!target_hint) {
    ... set_colorspace_hint(p, NULL);
}
```

`target_hint` is `--target-colorspace-hint`, and its default of `auto` resolves to
**true** on d3d11 (that context implements `target_csp()`). So on Windows the
first branch is the live one, and with no file loaded `frame->current` is NULL and
*neither* branch runs. The last hint set during playback is never withdrawn — and
it is real swapchain state, not a note: libplacebo's d3d11 backend acts on it with
`SetColorSpace1` plus a backbuffer format change.

The reproduction: play something, turn Windows HDR **on** mid-playback (mpv
re-hints to PQ, correctly), stop, then turn HDR back **off**. Nothing re-hints, so
mpv keeps encoding the library UI as PQ while the display reads it as sRGB —
raised blacks and clipped highlights. Cycling HDR without ever opening the player
is fine, which is what the report says, because the hint is never set at all.

This bites us specifically because **we are one of the few things that keeps an
mpv window on screen with no file loaded**: the library UI *is* the idle window.

`player_window.suspend_colorspace_hint` parks the option at `no`, which takes the
second branch instead, and `pl_swapchain_colorspace_hint`'s NULL maps to sRGB. The
swapchain returns to 8-bit sRGB and stays there, letting Windows do the
SDR-in-HDR conversion it does for every other desktop app. That is the honest
answer regardless: the library UI is sRGB content, and hinting a swapchain toward
a video's colorspace while no video exists is meaningless.

Two scope limits, both load-bearing:

- **Only while nothing is playing.** The browser stays up over music, and the
  library can be opened over a playing video; parking the hint there would cost
  that video its HDR passthrough, which is worse than the bug being worked around.
- **Only the browse window.** The OSD menu's `force_window` window is torn down
  when the menu closes, and that destroys the swapchain along with the stale hint.

The suspend does nothing at all on an mpv without the option (built without
gpu-next, or too old) — reading it is how we find that out.
