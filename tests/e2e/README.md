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
| `test_source_conformance` | E1 | the fake `LibrarySource` still describes the real one |
| `test_playback_advance` | E2 | an episode finishes and the next starts; the server agrees; resume position |
| `test_playback_eof` | E2 | last-in-queue watched-marking, seek-to-end (#541), replaying a finished episode (#157/#323) |
| `test_playback_failure` | E2 | truncated, zero-byte and single-frame media fail rather than hang |
| `test_mpv_reopen` | E2 | closing mpv mid-playback then playing again (#458) — runs out of process |

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

**A scenario that can crash belongs in a child process.** `_close_child.py`
exists because a use-after-free on the mpv handle is a SIGSEGV, and a segfault
in-process loses the whole run instead of failing one test. Same reasoning as
`tests/integration/_idle_reopen_child.py`.

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
