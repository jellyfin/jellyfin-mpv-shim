# End-to-end suite — a real server, real media, real mpv

Everything else under `tests/` runs against a fake. That is why those suites
are fast and deterministic, and it is also why a wrong request parameter, a
DTO field we invented, or a reporting round trip that never closes are all
invisible to them. `docs/E2E_PLAN.md` has the evidence and the roadmap; this
file is how to run what exists.

## Running it

```sh
# One shell: the server. Takes a few minutes the first time (it builds
# Jellyfin from source and scans ~4,800 items).
cd ~/Desktop/stdjflib && ./stdjflib.py serve ~/Desktop/std-jf-lib --live-tv

# Another: the suite. Both mpv backends, under xvfb.
JMS_E2E_SERVER=http://127.0.0.1:8096 python3 tests/e2e/run_e2e.py

JMS_E2E_SERVER=... python3 tests/e2e/run_e2e.py --backend libmpv
JMS_E2E_SERVER=... python3 -m unittest tests.e2e.test_playback_advance -v
```

`tests/e2e/` has **no `__init__.py`**, so `python3 -m unittest discover tests`
never recurses into it and the fast suite still needs no server and no mpv.

**With `JMS_E2E_SERVER` unset — or set and unreachable — every test skips and
the runner exits 0.** A machine without a server is not a failure. The probe
requires a parseable `/System/Info/Public`, not merely a socket that accepts:
a server still shutting down on the same port accepts and closes, and calling
that "up" moves the error somewhere that explains nothing.

## What runs where

| Tier | Needs | Covers |
| --- | --- | --- |
| E1 contract | server | the request the shim actually sends; DTO shape; per-account policy |
| E2 playback | server + mpv + xvfb | the playback loop with the server *in* it |
| E3 app | server + mpv + browser | route walk, scroll under real latency |

E1 and E2 exist so far:

| Module | Tier | Covers |
| --- | --- | --- |
| `test_account_policy` | E1 | restricted libraries, Live TV access, no-password / disabled / hidden / one-session logins |
| `test_syncplay_group` | E1 | **two real clients on one real group** — pause/seek propagation, the Waiting handshake, halt/resume; the check that `tests/_syncplay_server.py`'s port of the group state machine is telling the truth |
| `test_source_conformance` | E1 | the fake `LibrarySource` still describes the real one |
| `test_auto_download` | E1 | the two endpoints the lookahead window is built from — NextUp names the first episode of an *unstarted* series (or the window silently never extends), advances with what was watched, is empty for a finished one; `StartItemId` is inclusive and carries `MediaSources` |
| `test_strm_source` | E1 | `.strm` shortcuts: where the source is, where the runtime is, what a refused one resolves to |
| `test_live_tv` | E1 | channel line-up, guide window bounds, category flags, guide prefs, timers |
| `test_route_walk` | E1 | every screen loads and renders against the real library |
| `test_paging` | E1 | virtual scrolling over ~1000 items at real totals (#617) |
| `test_keyboard_nav` | E1 | reaching and activating the library by keyboard, online |
| `test_large_queue` | E1 | a queue too big for one request line (the 414) |
| `test_connection_loss` | E1 | the server stops answering: gone, token revoked, or a page-in that fails after a screenful drew |
| `test_books` | E1 | books: no media source / container / size on the DTO, `Path` reaching a non-admin, the three `RunTimeTicks` progress encodings round-tripped, the two audiobook shapes, and a real book downloaded end to end |
| `test_collections` | E1 | box sets: the toggle's unscoped query, a `collection.xml`'s members against its `ChildCount` of 0, a Series member surviving an untyped listing, and create/add/remove through the gateway |
| `test_syncplay_playback` | E2 | **the real player in a real group** — stop halts rather than leaves (and leaves when the UI says the SyncPlay menu is unreachable), a halted player is not driven, resume replays the group's content |
| `test_playback_advance` | E2 | an episode finishes and the next starts; the server agrees; resume position |
| `test_playback_eof` | E2 | last-in-queue watched-marking, seek-to-end (#541), replaying a finished episode (#157/#323) |
| `test_playback_failure` | E2 | truncated, zero-byte and single-frame media fail rather than hang |
| `test_strm_playback` | E2 | resuming a `.strm`, and not finishing a version-set alternate at its sibling's length |
| `test_mpv_reopen` | E2 | closing mpv mid-playback then playing again (#458) — runs out of process |
| `test_input_routing` | E2 | real keys through mpv's input layer across every UI transition |
| `test_scroll_recovery` | E2 | wheel-scrolling 1000 items hard in a real window; tiles come back |
| `test_track_selection` | E2 | Jellyfin's stream index vs mpv's track id, in both numbering schemes, and reported back |
| `test_photos` | E2 | one still is held, an album is a slideshow, and neither inherits the browser's endless display duration |
| `test_window_resize` | E2 | the window changes size under the UI, down to a size nobody could use |

**E1 runs once, without a display**, because nothing in it imports
`player.py`; the runner keeps it in its own tier and the whole of it is under
two seconds. Only E2 pays for the backend matrix. Put a new test in E1 if it
can answer its question without a player — it is thirty times cheaper.

The plan doc has the ordered list of what comes next and which past bug each
line would have caught, plus the two defects this suite has already found.

**Test classes own disjoint fixtures.** One series each (`The Standard Show`,
`Absolute Numbering Show`, `Flat Show No Season Folders`, `Show With Missing
Episodes`, `Date Based Show`, `Double Episode Show`), so nothing here depends
on execution order — and each resets its own playstate in `setUp` as well as
on cleanup, so a run that died halfway cannot change what the next one
measures. Keep that up: pick an unused series rather than sharing one.

**A route walk needs three assertions, not one.** `test_route_walk` checks
that the build does not raise, that `route["_error"]` is unset, and that
nothing logged a failure. A negative control that reintroduced `b97dd523`
passed against the build check alone: `_route_async` catches a failing loader,
records the error on the route and leaves it empty, and an empty route builds
perfectly. Write the negative control before trusting a walk.

**The interaction sweep is verified on one path only.** `test_route_walk`
right-clicks, scrolls and hovers each screen, but a negative control that made
`_open_tile_menu` raise is caught on the home rows and *not* on the grid,
detail or music screens — they wire `on_context` elsewhere. Green there does
not mean right-click is covered everywhere; see `_interact`.

**Log traps go on the root logger.** The two that matter are named `mpvtk` and
`mpvtk_browser.async_runner` — outside the package hierarchy, so a handler on
`jellyfin_mpv_shim` sees neither.

## Things that are easy to get wrong

**Never bake an item id into a test.** Ids are assigned on scan and change on
every reprovision. `Session.find` / `.episodes` look up by name.

**`NameStartsWith` matches SortName, not Name.** SortName strips the leading
article, so `NameStartsWith="The Standard Show"` returns *nothing* while
`"Standard"` returns it. `Session.find` filters client-side for this reason —
the harness is not allowed to contain the bug shape it is meant to catch.

**A short item can never hold a resume position.** The server discards one
below `MinResumeDurationSeconds` (300 by default) and clamps to
`MinResumePct` 5% / `MaxResumePct` 90%. A resume test written against a
10-second episode fails in a way that reads as a shim bug. Use `x-long`
("Three hours").

**A `.strm` has no runtime until something asks for `PlaybackInfo`.** A library
scan never probes a shortcut, so the item carries no `RunTimeTicks`; the server
learns it from the probe it runs during the playback request and puts it on the
**MediaSource**. That is not cosmetic — `UserDataManager.UpdatePlayState` marks
an item whose runtime it does not know *fully played* on the first progress
report and stores no position, which is the whole of "resume does not work on
my .strm". Two consequences for tests here: never assert "the item DTO has no
runtime" (true only until the first play, then false for ever), and expect a
stream file to be marked watched if anything reports progress before the URL
was resolved.

**Stream fixtures come with two kinds of origin.** stdjflib's **local origin**
(`127.0.0.1:8410`, started by `stdjflib serve`) is h264+aac that Jellyfin
treats as genuinely remote — `Protocol=Http`, `IsRemote=true`, probed over
HTTP, direct play — so only the route is local: a loose 30s movie, a 400s one
(`Long Origin Stream Movie`) and **two version sets that differ only in which
file is primary**. Its **catalogue** fixtures are archive.org over TLS.

**Prefer the local ones.** With the internet blocked, 21 of the 22 strm tests
still run — the whole of `test_strm_playback` included. The one that skips is
the commented fixture, which is the file built out of everything
`FetchShortcutInfo` tolerates and has no local equivalent yet. Networked
classes carry `_strm.require_origin(...)` and **skip** rather than fail;
somebody else's host being slow is not a defect in this client. `_strm.py`
holds the fixture names and the probe.

**A resume position needs the 400s clip.** The server discards one below
`MinResumeDurationSeconds` (300) and clamps to `MinResumePct` 5% /
`MaxResumePct` 90%, so `Long Origin Stream Movie` is the only item in the
library that can hold one at all; the usable window is 20s–360s.

**A `.strm` inside a version set is never probed, and the gate is the item's
path.** `item.Path.EndsWith(".strm")` decides it, and a version set's
`item.Path` is its *primary's* — so the shortcut in the set never qualifies and
naming it with `MediaSourceId` does not change the answer. It arrives with no
runtime *and* no `MediaStreams`, which also means it can never match a
direct-play profile: every play of one is a transcode of a source whose length
the server does not know, so mpv gets a growing HLS estimate rather than a
length. Do not assert on `_player.duration` for one. The sibling's runtime is
right there on the Item and is the wrong answer; see `Video.get_duration`.

**The two version sets are opposites and you need both.** `Local Origin
Versions` has an `.mkv` primary, so its `.strm` alternate goes unprobed — the
shape that broke, where every assertion is about an absence. `Origin Primary
Versions` names the `.strm` exactly like its folder, which puts it in the
primary slot and ends `item.Path` in `.strm`, so the gate fires and **both**
sources come back measured (30s remote primary, 20s local alternate, Item
reporting the primary's 30s). Only the second can assert that the shim uses an
alternate's *own* runtime rather than the Item's; only the first reproduces the
bug. Do not "simplify" them into one.

The refused fixtures (a dead loopback `rtsp://`, and a local path) reach nobody
at all and need no gate.

**The player is a process-wide singleton.** `playerManager` is shared by every
test class in the interpreter, so it is built once and terminated at exit
(`ensure_real_player`) — never in `tearDownClass`. Getting this wrong fails on
one backend only: in-process libmpv quietly re-creates itself, while an
external mpv is a separate process that is simply gone, and the next `play`
raises `BrokenPipeError: socket is closed`.

**Playstate is the state these tests share.** Watched flags and resume
positions persist on the server, so anything that dirties them registers
`Session.reset_played` with `addCleanup`, both before and after.

**Sessions and devices leak unless you log out.** `client.stop()` only closes
the socket; the session stays registered and the server keeps a Device record
per device id forever. So `Session` uses one deterministic device id per
account (not a fresh uuid — that left 119 device records behind) and `stop()`
POSTs `/Sessions/Logout`. It matters beyond tidiness: accumulated sessions
exhaust `qa-onesession`'s cap, and its test then fails on the *first* login,
which looks exactly like the cap working. That test purges the account's
devices as admin in `setUp` for the same reason.

**Live TV timers need a third permission.** `EnableLiveTvManagement` is
separate from `EnableLiveTvAccess`, a newly created Jellyfin user does not get
it, and there is no administrator bypass — so `POST /LiveTv/Timers` is 403
until someone grants it. Current stdjflib grants it to `qa-admin` and
`qa-user`, so `TimerTest` needs nothing; against a server provisioned before
that fix it grants the permission as admin and restores the original policy
afterwards. That fallback is the only place the suite writes server
*configuration*. See `docs/PERMISSION_GAPS.md` for why the permission is off
on a fresh server and on for anyone who upgraded into it.

**Audio goes to a null sink, not your speakers.** The playback legs decode
real media, so mpv opens a real output — audible, contending with whatever
else is playing, and able to fail on a device another process holds (a run
against the real device produced "Audio device underrun detected"). The runner
loads one PipeWire/PulseAudio null sink for the whole matrix, exports it as
`JMS_E2E_AUDIO_DEVICE`, and unloads it at the end; `quiet_settings` puts it in
`settings.audio_device`. **The default sink is never changed** — nothing about
your audio moves.

A null sink rather than `ao=null` on purpose: mpv still opens an output and
the whole path runs (device selection, format negotiation, the AudioMixin
settings), it just ends nowhere, so this suite can grow audio tests. With no
`pactl` it falls back to mpv's own `null` device — quiet and contention-free,
just less of the path exercised. Both paths are verified.

**A wheel event goes to whatever is under the pointer, and under xvfb
nothing has ever moved a mouse.** `mouse-pos` reports no hover and the
coordinates sit at (-1, -1), so wheel events are delivered and discarded.
Send `mouse <x> <y>` first and wait for `hover`. Without it a scroll test
scrolls nothing — and reads as "the app stops scrolling two thirds of the way
down" when it never started.

**Input tests must press real keys.** `test_input_routing` exists because
declaring a key binding and *enabling its section* are different calls, and
only mpv holds the second — the fake's `enable_key_bindings` was a no-op, so
the tests covering those commits could only assert which section a binding was
DECLARED in. Three regressions in 48 hours got through that way. Anything
asserting on input goes through `handle.command("keypress", ...)`, never a
synthesised event, which reaches the handler whether or not its section is on.

**mbtn_back does not page back in browse.** It fires ESC, and in plain browse
ESC has no binding — the forced ones belong to an open menu and to the
playback HUD. So it dismisses an overlay; open the tile menu first if you want
something to observe.

**Both backends, always.** External mpv is the least-tested path in the app
and one of the two largest open-bug clusters in the tracker. The runner makes
it a separate leg so a jsonipc-only regression is unmissable — and the first
defect this suite found ran the other way, crashing only on libmpv.

**Closing mpv is a race; do not hang an assertion on it.** Whether
`finished_callback` or the shutdown teardown wins decides which report path
runs, so a test built on a window close passes about a third of the time.
Exercise the close for outcomes that do not depend on who won (does it
re-open, does auto-advance survive); for the abort-report path, drive
`send_timeline_stopped(finished=True)` directly.

**Stop playback before the process ends.** Leaving a file decoding means the
atexit teardown destroys the libmpv handle while it is still running, which
races exactly as `finished_callback` did — a SIGSEGV *after* all the
assertions passed, roughly one run in four. `_close_child.verdict` stops first
for this reason. The real app stops before it terminates; so should a test.

**Tear the libmpv handle down explicitly at exit.**
`PlayerManager.terminate` destroys the handle only for *external* mpv; on
in-process libmpv it is left to CPython's finalization. Fine for the app — a
desktop process exiting lets the OS reap it — and racy under a test runner,
where it surfaced as a rare SIGSEGV *after* "OK" was printed and a leg the
runner then called failed. `_terminate_player` does it explicitly.

**A scenario that can crash belongs in a child process.** `_close_child.py`
exists because a use-after-free on the mpv handle is a SIGSEGV, and a segfault
in-process loses the whole run instead of failing one test. Same reasoning as
`tests/integration/_idle_reopen_child.py`.

**A collection's `ChildCount` is 0 and its members are still there.**
A `collection.xml` is parsed into the in-memory item and its linked children
are never written to the table the count is read from, so every collection
stdjflib builds from a file reports 0 for ever while `GET /Items?parentId=`
returns all of them. Measuring a collection by `ChildCount` is what makes a
working collection look broken; stdjflib's `docs/COLLECTION_XML_BUGS.md` has
the reproduction. The API-made ones (`Api Made Collection` and friends) count
correctly, which is what makes the pair a control.

**A collection holds any item type, from any library, and must be listed
untyped.** `Two Libraries, One Collection` is a Series and a Movie. The
typed+recursive query a library grid makes returns neither — it recurses past
the collection — so `_open_item` passes the BoxSet's own collection type,
which is none. Test both halves or the untyped query reads as an oversight.

**Editing a collection needs `EnableCollectionManagement`**, which is off for
a newly created account and has no administrator bypass — the whole of
`CollectionController` is behind it, so create, add and remove are one
permission and one 403. stdjflib grants it to `qa-user` (and `qa-admin`), so
the edit tests run as the **ordinary** account; the refusal is asserted
against `qa-restricted`, one of the ten that still lack it. Deleting the
fixture afterwards is a *different* permission (`EnableContentDeletion`),
which is the only thing the admin session in that class is for.

**Bulk items all share a creation time.** They are built by one scan, so
`DateCreated` ties and the server falls back to name order — "Date Added" and
"Name" return the same first item, which makes a resort look like a refetch
that never happened. Use "Release Date" to prove a reorder.

**The grid's query is typed and recursive** from the collection type
(`LIBRARY_ITEM_TYPES`). A hand-rolled comparison query that omits that
describes a different set and lands on different indices — index 500 came back
"Midnight Yard" untyped against the grid's "Midnight Zenith", which reads as
an off-by-something in the shim and is not one. Compare through
`source.get_library_items`.

## Fixtures worth knowing about

From `~/Desktop/std-jf-lib`, built by stdjflib and deterministic by design
(every NFO carries `<lockdata>true</lockdata>`, dates derive from a fixed
epoch, nothing depends on wall-clock time or `hash()`).

| Item | Library | Why it exists |
| --- | --- | --- |
| `The Standard Show` | Shows | six 10s episodes per season — a queue that finishes in seconds |
| `Three hours` | Test Media | resume points, seeking far from the start |
| `Twelve chapters` | Test Media | chapter nav and boundaries |
| `Six audio tracks` | Test Media | track selection, language preference, the default flag |
| `Truncated file` | Test Media | must fail cleanly rather than hang |
| `Zero-byte file` | Test Media | must be refused cleanly |
| `One frame` | Test Media | duration rounds to zero; progress bars divide by it |
| `Bulk *` | — | ~1000 items each: paging, virtual scroll, thumbnail pressure |

Twelve test accounts (password `stdjflib`, except `qa-nopassword`) cover the
policy paths — `qa-restricted`, `qa-nodownload`, `qa-noplayback`, `qa-kid`,
`qa-onesession` and the rest. `./stdjflib.py accounts` explains each one.
