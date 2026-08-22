# The app as a process

How the shim is put together as a running process: who elects the primary
instance, what a launch claims before anything else can, the tray child, how
quitting is made to finish, the input models, the local user model, and how the
module-level singletons find each other.

`CLAUDE.md` lists what the singletons *are*. This file is about the process
around them. Player-side seams (backends, mpv versions, SDL, input sections) are
in `docs/mpv-backends.md` and are cross-referenced rather than repeated.

## 1. Primary election and the instance channel

`jellyfin_mpv_shim/single_instance.py`.

Primary election is an **OS file lock** — `flock` on POSIX, `msvcrt.locking` on
Windows — held on a persistent fd for the process lifetime. Atomic, and released
automatically if the process crashes, so there is no stale-lock takeover logic to
race. The lock lives in the config directory, so two instances pointed at
different config dirs coexist by design.

The same channel carries the `stop` command, **which is why `stop` is a
subcommand rather than a signal**: it reaches exactly the instance owning *this*
configuration directory, without needing to find a pid or guess which of several
copies to kill.

**Two files, so Windows' mandatory region locks never interact with content
reads.** `instance.lock.guard` is only ever locked and never read;
`instance.lock` carries the primary's activation endpoint — a loopback port and a
random token. A second launch finds the guard held, connects, and (if the token
matches) asks the primary to raise its window, then exits. The token protects
against the port having been recycled by an unrelated process.

**Fail open, but say so.** If the guard file cannot even be opened the app runs
without the guard. If the lock is held but the primary does not answer, we still
refuse to run a second instance — a wedged listener must not lead to two catalog
writers. Because of the first case, `acquire()` returning True does not by itself
promise there is only one of us: **`holds_lock` is the stronger statement** (the
OS lock was really taken), and anything whose safety rests on that uniqueness
rather than merely preferring it must read `holds_lock`. The scratch-cache
namespace (§2) is exactly such a caller, because it reclaims every directory it
finds.

`release()` closes the fd and leaves the guard file in place. Unlinking it would
let a new primary lock a fresh inode while a concurrently-started process still
holds the old one — two primaries. An empty leftover file is harmless; the lock,
not the file's existence, decides who is primary.

`SHOW` goes on the wire in the original wordless form (token only, no command
word), so a newer client can still activate an older running copy across an
upgrade — that older instance compares the whole payload against its token and
would reject anything with a word appended.

## 2. What a launch claims before anything else

`jellyfin_mpv_shim/mpv_shim.py:main`. Three claims are made early, and in each
case the ordering is the whole point.

### 2.1 SIGTERM, claimed before SDL can take it

`_claim_sigterm` does two things at once.

**SIGTERM had no handler at all**, so `kill` skipped the whole shutdown sequence:
no final progress report (the server goes on showing the session as playing until
the websocket times out), no window geometry saved, no credentials flushed.
`jellyfin-mpv-shim stop` has always been the orderly path — it goes through the
instance lock, not a signal — but `kill` is what a person reaches for, and
systemd and a session logout both send exactly this.

**And claiming it is what keeps it.** SDL installs its own SIGINT/SIGTERM
handlers when mpv's gamepad support initialises, but `SDL_AddSignalHandler` only
replaces a handler still at `SIG_DFL` — which is why standalone mpv is unaffected
(it installs its own first) and we were not. CPython claims SIGINT and leaves
SIGTERM at the default, so SDL took it, turned it into an `SDL_QUIT`, and mpv's
gamepad loop — which handles controller events and nothing else — dropped it. The
app could not be stopped by anything short of SIGKILL. **Measured** with gamepad
on and `SDL_NO_SIGNAL_HANDLERS` deliberately *not* set, so it is the handler doing
the work and not the environment: no handler installed → SIGTERM never arrives;
this → handler runs, shutdown proceeds. See `docs/mpv-backends.md` §4 for the
environment variable that remains as the layer under it, and for the child mpv of
the external backend, which has no handler of its own.

Installed in `mpv_shim` rather than beside the mpv construction because it has to
be in place before SDL initialises, and `playerManager` is a module-level
singleton whose *import* creates mpv — every import that reaches it is below this
line. The main thread is also the only thread `signal.signal` may be called from,
and this is it.

The handler sets the `Event` the run loop waits on rather than exiting:
everything that makes a shutdown orderly happens in `main`'s `finally`, and the
exit watchdog (§4) is already armed there to force the exit if a step wedges. It
does **no logging** — a signal handler can land while the logging lock is held on
another thread, and a deadlock there is a process that cannot be stopped, which is
the bug being fixed.

### 2.2 `spawn`, forced, because a context resolves on first *read*

`_use_spawn_start_method` calls `set_start_method("spawn", force=True)`.

Why spawn at all: on macOS it avoids Objective-C fork crashes with GUI frameworks
(3.14's `forkserver` also crashes with Obj-C, issue #473); on Linux and Windows
the tray child is started *after* the timeline/action/sync worker threads, so a
plain fork can inherit a held lock (logging, say) and deadlock the child. Spawn
gives a clean interpreter, and the child relies only on its IPC-supplied options.

**`force=True`, because without it the call did nothing when launched from
`run.py`.** `set_start_method` raises rather than overriding a context that is
already resolved — and a context resolves on the first *read*, not only on a set.
`run.py` calls `multiprocessing.freeze_support()` before `main`, whose first line
is `self.get_start_method()`, which materialises the platform default. So on
Linux the context was pinned to **fork** before this ran, the `RuntimeError` was
swallowed as "already set" (which read as "somebody set it to what we wanted" and
was the opposite), and every tray child was forked. The installed console script
has no `freeze_support` call and did spawn, so this was a from-source and
frozen-Windows-build bug only.

The cost is not theoretical: a forked child inherits the parent's signal
handlers, so it took a copy of the SIGTERM handler above — which sets an `Event`
that, in the child, nothing waits on. Every `TrayManager.stop()` (a `terminate()`,
i.e. a SIGTERM) was therefore swallowed, and the tray process outlived the app and
had to be SIGKILLed. `tray._reset_inherited_signals` and the escalation in
`TrayManager.stop` are the layers under this (§3).

The resulting method is read back with `allow_none=False` and **reported when it
is not spawn**: a start method that is not spawn is a real hazard on every
platform, and the failure is silent.

### 2.3 The scratch-cache namespace, keyed on config dir *and* host

`scratch_namespace()` names the directory this instance's scratch caches live in.
Everything in it is reclaimable by whoever holds it, so what goes into the name is
exactly what bounds that claim.

- The **config directory**, because that is what the single-instance lock covers:
  two copies started with different `--config` directories are legal and share a
  machine's temp space.
- The **host**, because a home directory can be shared. `~/.cache` is one of the
  bases, and **`flock` is host-local on plenty of network filesystems** — so two
  machines mounting the same home can each hold what each believes is the only
  lock, and a name keyed on the config path alone would have them reclaiming each
  other's live caches. A per-host name costs nothing to a normal setup and makes
  that case structurally impossible rather than merely unlikely.

Hashed (sha1, first eight hex digits) rather than spelled out, because both parts
are long, one is a path, and neither is meant to be read: this is an identity, not
a label. The cost is that a machine which renames itself abandons its old
namespace — nothing sweeps a namespace but its owner — so one run's worth of
scratch stays behind on real disk until the OS reclaims it.

**Installed only against a lock that was really taken** (`single.holds_lock`).
`acquire()` fails open when the guard file cannot be opened at all, and a second
copy that merely *believes* it is alone would reclaim the first one's cache out
from under it. Without the namespace the pid rules still apply, which is what
every release before this one ran on.

## 3. The tray is a child process

`jellyfin_mpv_shim/tray.py`.

**It runs in a separate process, not a thread.** pystray needs its own process's
main thread for its GTK/AppIndicator loop, and pystray + libmpv in one process
segfaults with GNOME AppIndicator. What lives in *this* process is a small pump
thread reading the child's command queue and dispatching to callbacks. Per the
optional-dependency policy, a missing or broken pystray logs a warning and leaves
the app running headless-but-functional.

The child holds no references to the player or the browser — with spawn it is a
fresh interpreter anyway — so everything it can do is put a command name on a
queue. `TrayManager` ignores unknown commands, which is what lets an older
installed copy of the child keep working after a menu entry is removed.

**First thing the child does is reset its inherited signal dispositions**
(`_reset_inherited_signals`). SIGTERM is the one that matters and only a *forked*
child has it to undo (§2.2). SIGINT is reset in both kinds of child and is
cosmetic: every child arrives with CPython's `default_int_handler` (**measured**,
fork and spawn alike), so a Ctrl-C — which the terminal sends to the whole process
group — raises `KeyboardInterrupt` from wherever the GTK loop happens to be and
prints a traceback on the way out.

### 3.1 The icon, and the click action

The icon source is **128px**, not the 16px this shipped for years. Every pystray
backend takes the image and produces the size *it* wants, so a source larger than
the panel is the supported direction and a smaller one is what cannot be recovered
from — 16px on a HiDPI panel, or a KDE tray asking for 32/48, is upscaled 2-3x and
reads as mush.

- **win32** saves it as an ICO and calls `LoadImage(LR_DEFAULTSIZE)`, picking a
  frame for the system metric. Pillow writes every standard size up to the source,
  so a 16px source offered exactly one frame and 128 offers 16/24/32/48/64/128
  (256 is ICO's ceiling; 128 stays clear of it).
- **darwin** resizes to the status bar thickness with LANCZOS, **xorg** to the
  size the tray asks for.
- **gtk/appindicator** write the PIL image out as a PNG and hand the path over, so
  the panel scales it itself.

The artwork is `integration/jellyfin-128.png` — the same mark, on transparency.
`logo.png` is **not** interchangeable: it carries an opaque dark background, which
is a square tile in a light panel.

"Show Library Browser" is `default=True`, which makes it the **click action**, not
just a menu entry: clicking the icon is what people expect to reopen the window,
and having to right-click and pick from a list to get the app back is a poor
greeting. It stays in the menu as well. Honoured only where the backend can report
a primary click (`Icon.HAS_DEFAULT_ACTION`): **win32, gtk and xorg; appindicator
and darwin both set it False.** That is not a pystray shortcoming — the Indicator
GObject exposes no signals at all (only a *secondary* activate target, i.e. middle
click), so there is no primary click to hook. StatusNotifierItem does define
`Activate`, but libappindicator never surfaces it, which is why Qt apps on the same
desktop can tell the buttons apart and this cannot.

### 3.2 Will the icon actually render

pystray's `visible = True` says the icon object exists, not that anything drew it.
An AppIndicator is a D-Bus object plus a registration call; with no watcher on the
bus there is nobody to register with, so libappindicator quietly does nothing, no
error is raised anywhere, and the app goes on to hide itself behind an icon that
does not exist. That is GNOME's default state — the shell has drawn no tray since
3.26, and the AppIndicator extension is what puts one back. So the tray is probed
directly, and `tray_will_render(backend)` is the answer:

- `_NATIVE_BACKENDS` (win32, darwin) — always yes. pystray's own `dummy` backend —
  no.
- `_XEMBED_BACKENDS` (gtk, xorg) — ask `xembed_tray_present()`.
- `_SNI_BACKENDS` (appindicator, ayatana_appindicator) — ask both, in order.

**`None` means "could not tell", and every caller must read that as *yes*.** A
probe that cannot run has no business taking a working tray away from someone;
only a confident `False` changes behaviour. The probes are injectable so this
stays answerable without a desktop.

`sni_watcher_present()` asks GDBus (through PyGObject, which the backends that
matter already require) whether anything owns `org.kde.StatusNotifierWatcher` —
the bus name every SNI host registers, whoever wrote it; Ayatana kept KDE's
spelling and GNOME's extension takes the same name, which is precisely why its
absence is a usable answer. Three outcomes, and the third is a distinct value:

- **True** — a watcher with a host registered behind it.
- **False** — a watcher on the bus with **no host registered**. Worse than none:
  the item registers successfully, the GtkStatusIcon fallback never starts, and
  nothing draws it. This is the difference between the extension being *installed*
  and being *enabled*. An unreadable `IsStatusNotifierHostRegistered` fails open to
  True — the name *is* owned, so someone is answering.
- **`NO_WATCHER`** — nobody owns the name. A `_NoWatcher(int)` that is falsy, so
  every "is there a StatusNotifier tray" test reads as before, but distinguishable,
  because **this case and no other** starts libappindicator's GtkStatusIcon
  fallback.

**"No watcher" is not the end of the story, and reading it that way is what made
this wrong on X11 (#4).** libappindicator and its ayatana fork both keep a
GtkStatusIcon fallback (`start_fallback_timer` in libayatana-appindicator3) and use
it exactly when no `StatusNotifierWatcher` owns the name. So on a desktop with an
old-style XEmbed tray and no D-Bus host — i3 with i3bar's tray, xfce4-panel, tint2,
most of X11 that is not KDE — the icon appears perfectly well, and the app was
offering "Keep Running in Background" to people who had a working tray in front of
them. **Confirmed by watching the icon dock into i3bar while the watcher name was
unowned.** So `NO_WATCHER` falls through to the XEmbed probe, and a confident
`False` is returned only if both probes actually ran and both said no.

`xembed_tray_present()` asks whether anything owns `_NET_SYSTEM_TRAY_S<n>`, via
**ctypes against libX11** — not python-xlib (not a dependency of ours or of the
backends that need the answer) and **not GDK, which cannot give it**:
`gdk_selection_owner_get_for_display` resolves the owner through GDK's own table
and returns NULL for a window belonging to another client, which every tray is.
**Verified against a real i3bar: Xlib says `0x0010000d`, GDK says None.** The
library is loaded by SONAME first, because `ctypes.util.find_library` shells out to
`ldconfig -p` on every call — a fork out of the tray's GTK main loop, since the
watch callbacks reach here — and in a bundle with no ldconfig and no compiler it
returns None, which would answer "cannot ask" inside a process that already has
libX11 mapped. That is the i3 case this exists for, silently reverted. No
`DISPLAY`, or a display that will not open, is a confident **no**: GTK in this same
process opens the same display for the icon itself.

The answer is taken **inside `setup()`**, as late as it can be, because at login we
may well have started before the shell extension that owns the watcher. And it is
not taken once: `_watch_for_tray` registers a `Gio.bus_watch_name` (held on `self`,
or the watch is collected out from under the loop it was registered on) so a host
appearing or a shell restarting is reported rather than having been wrong about
once. Both callbacks **re-run the full probe** rather than trusting the name — the
watch reports ownership only, while the probe also asks whether a host is
registered, and losing the D-Bus host is not losing the tray on a desktop that also
has an XEmbed one. `vanished` is additionally guarded by `seen_host`, because GLib
calls it at registration when the name is already unowned, which on the desktops the
fallback exists for is every launch.

`tray_unavailable_advice()` names the way out rather than the diagnosis (GNOME gets
the extension name; everyone else gets "Allow Background"), because a tray that does
not appear is only a problem because of what the app does about it.

### 3.3 GNOME Wayland, where two bugs pull in opposite directions

`wants_x11_backend(env)` forces `GDK_BACKEND=x11` (and drops `WAYLAND_DISPLAY`)
for the tray child **on GNOME's Wayland session only**.

- pystray's GTK loop crashes at startup there, and forcing X11 (XWayland) dodges
  it (**#506**).
- Forcing it *everywhere* is **#646**: on every other Wayland compositor the
  indicator then reports `visible = True`, raises nothing, and simply never
  registers with the StatusNotifierWatcher, so the tray silently does not appear.
  Wayfire + wf-panel-pi was the report; the same code registers immediately with
  the backend left alone.

So both halves have to hold. GNOME on X11 needs nothing forced, and a non-GNOME
Wayland session must be left to use its own backend. `XDG_CURRENT_DESKTOP` is a
colon-separated list and names GNOME variously ("GNOME", "ubuntu:GNOME",
"GNOME-Classic:GNOME"), hence a substring test rather than an equality one. The
environment is an argument so the function is answerable without one.

### 3.4 Getting the child to actually stop

`TrayManager.stop()` **escalates**. `terminate()` alone is a *request* — a SIGTERM,
which a child can be holding a handler for, and a forked one was for exactly as
long as the start method was not spawn. The parent then `os._exit`s from
`exit_watchdog.finish` without reaping it, so what the user is left with is a tray
process with no app behind it, surviving until it is SIGKILLed by hand. So the
request is checked, escalated to `kill()`, and both waits are bounded by
`TERMINATE_GRACE` (2.0 s — short on purpose: this runs inside the shutdown
sequence, which has its own deadline, and the child holds no state of ours). A
child that outlives even this is worth a line in the log, not a shutdown that
stalls.

The pump thread also has to notice a child that **crashed and sent nothing**:
without that the manager would report a tray that is not there for the rest of the
session, and the window would go on hiding behind an icon nobody can click — the
app's only way back on screen. A GTK process has ways to die that do not run our
code: Xlib's default I/O error handler calls `exit()` on a lost display, and GDK's
calls `_exit()`. So an empty queue read plus a dead process is dispatched as
`tray_died`.

`_release_queue` closes the command queue after joining the pump (bounded by one
poll interval). Its POSIX semaphores are otherwise cleaned up by multiprocessing's
resource tracker, which says so on stderr — and since `exit_watchdog.finish` leaves
by `os._exit`, that notice would be the last thing printed on every quit.
`cancel_join_thread` because anything still unsent is a command for a child that is
already gone.

## 4. Making the exit finish

`jellyfin_mpv_shim/exit_watchdog.py`.

Every thread the shim owns is either a daemon or joined by `main`'s shutdown
sequence, so in principle the interpreter exits on its own. **Two things can still
leave the app running with no window and no way to quit it**, and CPython waits for
both — it joins every non-daemon thread before the process can die, and
`concurrent.futures` registers an atexit hook that joins every
`ThreadPoolExecutor` worker.

- **A shutdown step that never returns**, so the steps after it never run. This is
  what the deadline in `arm()` catches. It happened for real: closing the window
  parked the action thread for two minutes inside an mpv command whose reply never
  came, and the shutdown sequence joins that thread. That specific cause is fixed
  at the source (`player.bound_ipc_replies`, and `docs/mpv-backends.md` §1), and
  this remains as the backstop.
- **A thread that outlives its `stop()`** and keeps the interpreter alive after
  `main` returns. Pool workers are the usual shape: `shutdown(wait=False,
  cancel_futures=True)` drops the queue, but a worker already inside a socket read
  runs until the server answers or the timeout fires. `finish()` reports these.

**Nothing may follow `exit_watchdog.finish()` in `main`.** It ends in `os._exit`,
which is the point — that is what skips the atexit joins above — so it never
returns and anything written below it is dead code. The restart relaunch was put
there once and never ran: it armed, the app shut down cleanly, and nothing came
back. Whatever has to happen last goes through **`set_final_action`**, which runs
once, immediately before `os._exit` and before `logging.shutdown()`, on **both**
exits — the orderly `finish()` and the forced one in `arm()`.

That "both" is the reason the hook exists rather than a line in `main`. A wedged
shutdown is exactly when a user who pressed *Restart Now* most needs the app to
come back instead of vanishing, and `main`'s tail is unreachable in that case.
The deadline is deliberately not softened for it: the old process still has to
die for the new one to take the instance lock, so the guarantee stays "we end" —
what changed is that ending is no longer the last word. The action registered by
`main` releases the instance lock first, because the wedge can be anywhere and a
new copy that finds the lock held hands off to the dying process and exits.

**`arm()` is called at the *start* of the shutdown sequence, not the end** — the
failure it guards against is a step that never returns, and anything placed after
such a step is unreachable by definition. `SHUTDOWN_DEADLINE` is 20 s, generous
because a step legitimately waiting out a socket timeout must not be cut off and
blamed for it. `GRACE_SECONDS` is 3.0 and is a budget for **all** stragglers
together, not each: most are a socket read about to time out, and waiting lets them
end normally and keeps the log quiet.

**Both report stacks, not names.** A thread name on its own names nothing
actionable; the frame it is parked in is what identifies the call that needs
bounding. The wedged-shutdown path uses `faulthandler` (it runs while the rest of
the process is blocked, and it covers the **main thread**, which is the one parked
in the call that names the wedged step); the straggler path uses a briefer
`sys._current_frames` walk, a few frames each, because a leaked thread at the end
of an otherwise clean shutdown warrants a line in the log and not a full-process
dump. `enable_manual_dumps()` puts the same faulthandler dump on `SIGUSR1`, because
a hang is only diagnosable while it is hanging, at which point it is too late to add
instrumentation.

**`os._exit` is last and it is safe.** It runs only *after* the orderly shutdown —
playback stopped, the stop reported to the server, config and credentials written.
What it skips is the interpreter's own wait, which by then has nothing left to do
for us; specifically it skips the atexit hook that joins every pool worker. It also
skips buffer flushing, which is **not** skippable — the straggler warning is the
entire value of `finish()` — so `logging.shutdown()` and explicit flushes come
first.

**Everything here tolerates `sys.stdout is None`.** A Windows GUI build (pythonw,
the frozen installer build) has no console, so both streams are `None`, and code
that exists to make quitting reliable is the last code that should raise on the way
out. Flushing them unconditionally turned quitting into an `AttributeError`. Streams
go through `_write` / `_flush` / `_dump_to` and are never touched directly.
`_dump_to` additionally requires a working `fileno()`, since faulthandler writes
through a file descriptor — which rules out `None` and also the wrappers a frozen
GUI build can leave in place of a console. The log file is written to as well as
stderr, because it is what gets sent back in a bug report and it is the **only**
destination on a Windows GUI build; if nothing could take a dump at all, the compact
walk still goes out through logging so the reason for the exit is not lost entirely.

## 5. The gamepad is not a second input model

`jellyfin_mpv_shim/gamepad.py` is pure data plus one function: no mpv, no settings
import, no I/O.

**Every button resolves to something the shim already has** — a keyboard key it
already binds, or the seek the arrow keys already perform. That is the whole
design: a parallel set of gamepad handlers is a second implementation of the
navigation ladder, and the second implementation is where the drift happens. Three
kinds, and the split is not cosmetic:

- **`KEY` is a synthetic keypress.** The renderer issues the keyboard key and
  whatever is bound to it right now answers — the browser's spatial navigation while
  the library is up, the playback HUD's summon while a video is on, mpv's own default
  when neither claims it. So the d-pad follows the UI from screen to screen without
  knowing any of them exist, and a page that claims ENTER (the epub reader, a dialog)
  gets the controller's confirm button for free.
- **`SEEK` goes to `PlayerManager.kb_seek`**, which is the *keyboard's* seek and not
  a number of ours: it reads the distance out of the user's own `input.conf` binding,
  applies `use_web_seek`, and routes through a seek a SyncPlay group hears about. A
  raw `seek 5` from Lua would be none of those three, and the third is a desync the
  group then corrects.
- **`NAV` hands the argument to `PlayerManager.menu_action`** — the remote control's
  own ladder — for a button whose meaning *changes* between the library and a playing
  video and which has no single keyboard key covering both. START is the focused
  item's context menu while browsing and the player's settings menu over a video, and
  only that ladder knows which. A MENU *keypress* would not do: the MENU key is a
  browser nav binding, so over a playing video it is bound to nothing at all and the
  button would go dead exactly where the remote's hamburger works.

### 5.1 mpv names the face buttons by POSITION, not by label

`ACTION_DOWN` is the bottom button and `ACTION_RIGHT` the right one, whatever is
printed on them — **which is exactly the problem, because the two common layouts
disagree about where A is.** An Xbox-style pad puts A at the bottom, a
Nintendo-style one (Switch Pro, most 8BitDo) puts it on the right, and SDL reports
both by position. So "confirm is A" is not a statement this code can make.
`CONFIRM_BUTTON`/`BACK_BUTTON` are named in position order, bottom first, and
`gamepad_swap_confirm` is the user telling us which pad is in their hands; the swap
exchanges what those two carry and nothing else on the pad moves.

### 5.2 The sticks are asymmetric on purpose

The left stick and d-pad drive the UI; the right stick seeks. That is why the left
stick **wakes a hidden playback HUD** rather than falling through to mpv's arrows the
way the keyboard does under the default `hud_grab_keys`: a keyboard has one set of
arrows and has to share them, and a controller does not.

Confirm is ENTER (what the browser's nav activates on and what the hidden HUD wakes
on) and back is ESC (it already steps out exactly one layer — page, dialog, menu,
playback — and a second implementation of that ladder would drift from it; the
mouse's back button is routed the same way for the same reason). Play/pause is SPACE
rather than `cycle pause`, so it lands on the shim's claim of that key: in a SyncPlay
group a local pause is not a pause, it is a desync the group then corrects.

### 5.3 Repeat rates, per control

`DIRECTION_REPEAT` 0.15 s, `PAGE_REPEAT` 0.35 s, `SEEK_REPEAT` 0.4 s, and
`NO_REPEAT` **0 means it does not auto-repeat at all**.

mpv repeats a held key at `--input-ar-rate`, which defaults to 40 a second. On a
keyboard that is a cursor moving through text; on a stick it is forty rows of a
library per second, which is not something anybody can aim. An analog stick is worse
than the d-pad again, because **an axis resting near the threshold chatters across it
and each crossing is a fresh press rather than a repeat** — which is why the limit is
applied to every event and not only to the ones mpv marks as repeats.

Per control rather than one number, because holding these does not mean the same
thing. A direction is "keep going" and wants to feel like a scroll; a page is a jump
and wants to be slower; a **seek repeats over real time**, so at the direction's rate
a resting thumb would cross a film in a couple of seconds. Confirm, back, play/pause
and the menu do not repeat at all: holding a button is not a request to press it
again, and an auto-repeating Select activates whatever it landed on over and over.

### 5.4 What is deliberately unbound, and how it reaches the renderer

`GAMEPAD_ACTION_UP` (Y/X), `GAMEPAD_BACK` (Select/View) and both triggers are left
free for `input.conf`. Every binding is registered **non-forced**
(`add_key_binding`, not `add_forced_key_binding`), so a line in `input.conf` naming
the same key wins outright and nothing has to be disabled first. Inventing a meaning
for the spare buttons would take that away from the people who have a use for them.

`bindings()` returns lists rather than tuples because this is JSON on the way to the
renderer; the renderer reads all four fields positionally, so the arity is part of
the contract. `MpvtkApp.push_gamepad` sends it on ready and again whenever the layout
setting changes, which is what lets a swap apply without a restart — unlike
`input_gamepad` itself, which mpv reads once at startup. It is pushed
**unconditionally**, not gated on `input_gamepad`: the bindings are inert without it
(mpv delivers no `GAMEPAD_*` events at all), and gating would add a state to get
wrong for no gain.

In `renderer.lua` the controller bindings live **outside `NAV_KEYS`**, which is where
they started and where they were wrong: those bindings are torn down by `ui_suspend`
the moment a video starts, because playback wants the arrows back. The controller does
not share that problem — it has a whole second stick — so it must not share the
teardown, and the first version went completely dead the moment anything played. The
`GAMEPAD_*` names live in mpv's keycode table on any build, so binding them is safe on
one without SDL2 support; they simply never fire.

Whether events arrive at all is `--input-gamepad`, which is **build-gated** and must
be set at construction — see `docs/mpv-backends.md` §2 and §4 for the option's
discovery, the retry that drops it, and the SDL signal handling that comes with it.

## 6. Key claims and the `input.conf` migration

Two pure modules, summarised here; the derivation is `docs/mpv-backends.md` §5.

`jellyfin_mpv_shim/keysweep.py` answers **which keys currently mean pause, seek or
fullscreen**, by reading mpv's *resolved* `input-bindings` property rather than
parsing anyone's config: mpv has already applied sections, profiles, priorities and
`ignore`, so there is no precedence model of ours to drift from theirs, for the cost
of one property read. That is what lets a claim **follow a remapped key** and
**re-issue the command that was already bound** instead of substituting one of ours.
It also distinguishes mpv's default from the user's choice (`is_weak`), which is
exactly what the migration below needs. Pure logic: the caller reads the property and
installs the section.

`jellyfin_mpv_shim/input_conf.py` is the one-time migration of the old `seek_*` and
`kb_*` key settings out of `conf.json` and into the user's own `input.conf`. `plan()`
computes it and `migrate()` writes it. Two things are load-bearing: it writes
**before the first `[` section header**, because everything after one belongs to that
section until the next, so appending to the end of the file would put the bindings
somewhere they apply conditionally or never while the file looked right; and it
**declines to write a setting whose meaning mpv cannot express** (`use_web_seek`,
`skip_intro_on_seek`), because a migration that quietly dropped a feature would be
worse than none. `mpv_ext_no_ovr` skips the write entirely and logs the block
instead — that setting means "use my own mpv config, if something breaks it's my
problem".

## 7. Users and fast user switching

`jellyfin_mpv_shim/users.py`, with `userManager` as the module-level singleton.

Jellyfin (and jellyfin-web) has no concept of switching between local accounts on one
device. Because this client owns its own UI we can offer it: a **user** here is a
*local grouping of one or more server logins that are connected together*. Only one
user is active at a time; switching disconnects the active user's servers and connects
the target user's.

**Each user carries its own Jellyfin device id**, so that two users logged into the
same physical server do not collide on one server-side session — which would make them
fight over playback and remote-control state. The migrated "(default)" user keeps the
original `settings.client_uuid` so its existing sessions and saved tokens keep working
untouched; every other user gets a fresh device id. The device *name* follows the same
rule: the default user reports the plain `player_name`, every other user appends its
own name so the sessions are distinguishable in the Jellyfin dashboard.

A user may be **PIN-protected**, which is a parental-control affordance and *not a
security boundary* — the PIN is only salted-hashed (PBKDF2-HMAC-SHA256, 200k rounds,
kept modest deliberately because this gates a PIN and not a real secret). Switching
*into* a locked user requires the PIN, and optionally the PIN can also be demanded at
startup (`require_pin_startup` → `startup_needs_unlock()`). A startup PIN gates
*connection*: `mpvtk_browser/ui.py` shows the lock screen and lets the unlock drive the
connect, rather than connecting first.

Persistence is `users.json`, next to `cred.json` in the config directory. On first run
with this feature the existing `cred.json` is migrated into a "(default)" user — the
same flat-list normalisation `ClientManager.load_credentials` used to do. `_normalize`
fills in keys missing from an older `users.json`, and a dangling `active` pointer is
repaired to the first user. Saves are **write-temp-then-rename**: `users.json` holds
every user's server tokens and `save()` is reachable from several threads (the UI
action loop, the switch worker, a finishing login), so a truncate-in-place write
interrupted or interleaved would lose all of them. The `RLock` guards the user list and
the active id against concurrent switches, and is **never held across network I/O**.

### 7.1 What is per-user and what is per-server

Per **user**: the device id, the device name, the PIN, the credential list, and
`last_server` — the uuid of the server this user was last browsing, so the next launch
lands where they left off instead of on whichever server happened to connect first. It
is a *hint*: the server may since have been removed or have failed to connect, so it is
only usable if it is still in the live server list. `set_last_server` no-ops when
unchanged rather than rewriting a file full of tokens on every navigation.

Per **server**, inside a user: one credential entry each, which is what
`clientManager` connects.

Two projections exist so the rest of the app never handles hashes or tokens:
`public_users()` (id, name, locked, default, and `require_startup` — exposed because
without it the PIN dialog always showed "off" and saving a new PIN silently cleared the
startup requirement) and `known_servers()`, which is **addresses only** so a new or
other user can be provisioned without retyping the URL; a URL alone grants nothing
without credentials.

### 7.2 How it meets `clientManager`'s credential store

`userManager` owns persistence; `clients.py` owns connecting. The seam is four calls:

- `load_credentials()` → `userManager.load()` then `_adopt_active_user()`, which points
  the live credential list, `device_id` and `device_name` at the active user.
- `save_credentials()` → `set_active_credentials()`, with the volatile runtime keys
  stripped first (a stale `connected: true` is misleading on reload).
- `credentials_for_active()` hands back a **copy**, so `ClientManager`'s live mutations
  do not touch the store until it explicitly saves back.
- `switch_user(user_id)` serialises on `_switch_lock` and, with a `_switching` flag up
  so the health check and reconnect loops stand down: saves the active user's
  credentials, stops all its clients, `set_active` + `_adopt_active_user`, and clears
  the removal ledger (stale uuids from the previous user must not suppress the next
  user's possibly uuid-colliding reconnects). The heavy `connect_all()` runs **after**
  the swap with the flag cleared, so the health check can help it along and shutdown
  stays responsive. A target with no servers is still a successful switch — it lands on
  the login screen.

**A login can finish after the user who started it has been switched away.**
`_finalize_login` therefore carries the initiating `owner_id`, and if the active user
changed while the (slow) login ran it files the credential under that user via
`append_credentials_for` instead of leaking it into whoever is active now, and does not
register a live client under the wrong user's session. A switch that slips in between
the save and the connect is caught on the far side too: the credential is already
safely filed, so the live client that was just registered is torn down.

## 8. How the singletons find each other

There is no DI container and no event bus. Each singleton is a module-level instance;
they find each other by **direct import**, and they wake each other with
**`threading.Event` triggers** — `playerManager.timeline_trigger` and
`playerManager.action_trigger` are the two events the player sets to make the
background threads do a pass now rather than at the top of their next poll.

**Who owns a background thread:**

| Singleton | Thread |
|---|---|
| `clientManager` | `PeriodicHealthCheck` (daemon), plus per-client websocket/redial threads |
| `timelineManager` | is a `Thread`; 5 s poll, or immediately on `trigger` |
| `actionThread` | is a `Thread`; 1 s poll, or immediately on `trigger` |
| `syncManager` | its worker loop (5 s) plus a download pool |
| `user_interface` | the mpvtk render loop thread, a connect thread, and the tray pump thread (the tray itself is a child *process*, §3) |

`timelineManager` and `actionThread` both bound their `stop()` join at
`JOIN_TIMEOUT = 15 s` and log rather than hang: a queued task can legitimately block
for a long time — a playback start waits out `playback_timeout`, a `stop_cmd` hook is
an `os.system()` call with no bound at all — and an unbounded join means the
close-the-window path never reaches the rest of the shutdown, which is §4's first
failure shape. `actionThread` also does a **final drain** after `halt`: tasks queued
during shutdown (the mpv teardown that reports the stop to the server) must still run.

**Startup order** in `mpv_shim.main`, and each step depends on the one above it:

1. Config, i18n, CLI overrides, logging, FriBiDi preload (before anything can import
   `PIL.ImageFont`), manual dumps, spawn method (§2.2).
2. `stop` is handled here, before we try to become the primary ourselves and before any
   service starts — this launch is never going to play anything.
3. `SingleInstance.acquire()`; then `halt`, `single.on_stop`, `_claim_sigterm` — the
   halt event is created **before anything can request a stop**, so a `stop` arriving
   during startup is honoured rather than acknowledged and dropped.
4. The scratch namespace, only under `holds_lock` (§2.3).
5. Choosing `user_interface`: `enable_gui`, Pillow importable, **and** this mpv has lua.
   Both fallbacks land in the same place and both must also call
   `set_osc_style` — with the browser gone nothing sets `on_hud_menu`, and
   `toggle_settings_menu` refuses the OSD menu while the resolved style is `mpvtk`, so
   without it a machine that merely lacks Pillow has no menu at all. The lua probe needs
   a live mpv, so importing `player` here is deliberate. See `docs/mpv-backends.md` §2.
6. Wiring: `clientManager.callback = eventHandler.handle_event`;
   `timelineManager.start()` then `playerManager.timeline_trigger`; `actionThread.start()`
   then `playerManager.action_trigger`; `syncManager.start(...)`; `user_interface.start()`;
   `single.on_activate = user_interface.activate`; `user_interface.login_servers()`.

**`clientManager.on_server_connected` is a single slot, and `mpvtk_browser/ui.py`
assigns it — from `login_servers`, i.e. *after* `syncManager.start()` has already run.**
That is why `sync/manager.py` watches the client registry for a server appearing rather
than subscribing to that hook: taking the slot would clobber the browser's use of it or
require growing a fan-out for one more listener, and the hook is a *notification* fired
from five call sites, so a sixth reconnect path would leave a gap nobody sees until a
catalog is stale. The registry is the state itself, so a set comparison against it cannot
miss a transition however the server came back. See `docs/offline-sync.md` §3.

**Shutdown order** is fixed and each step is logged *before* it runs, so the last line in
the log names the step that hung even when the dump is unavailable:

```
exit_watchdog.arm()  →  bound_ipc_replies()  →  player, timeline, action thread,
sync manager, clients, user interface, instance lock  →  exit_watchdog.finish()
```

`bound_ipc_replies()` covers the quit paths that do not start at a window close (tray
Quit, Ctrl-C): mpv is about to go away either way and no reply is worth minutes now
(`docs/mpv-backends.md` §1). Every step is wrapped individually — **one component failing
to stop must not strand the rest**, because a half-shut-down app is exactly what leaves
the stray threads this sequence exists to clean up.
