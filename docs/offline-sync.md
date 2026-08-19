# Offline sync

`sync/` downloads items for offline playback and keeps a local catalog of what is
held. This file covers the part that is not obvious from the code: **who is allowed
to write watched state, in which direction, and what schedule the catalog is kept
fresh on**.

## 1. Four writers of watched state, and their directions differ on purpose

The local catalog's `userdata_json` is a **floor, not a mirror** — except where a
person said otherwise. Getting a writer's direction wrong is invisible until someone
browses offline.

| writer | trigger | direction |
|---|---|---|
| `mirror_playstate` | our own playback | **advance-only** |
| `apply_userdata_event` | a `UserDataChanged` push | applies the payload |
| `_refresh_userdata` | the periodic sweep | **advance-only** |
| `record_watched` | the user picks Mark Watched/Unwatched | **verbatim, both ways** |

**Playback is advance-only** because reports arrive out of order and a position that
went backwards is a stale one.

**A deliberate mark is not.** Mark Watched / Mark Unwatched is the only signal in the
app that is authoritative in *both* directions, so `record_watched` writes verbatim
through `db.set_watched`.

Un-watching is the half that did not work before that existed. Every writer of the
column was advance-only, so a downloaded item un-watched from this app's own menu
stayed watched on the copy on disk **forever** — the sweep is advance-only too, so
nothing would ever have corrected it. Offline browsing showed a tick the user had just
removed, and "delete watched downloads" was still willing to throw the item away.

`record_watched` is called **unconditionally** at its call sites, with no check of
whether the item is downloaded, because `db.watched_targets` answers with nothing for
an item we hold no copy of. That is what keeps the check from being forgotten at a
call site again.

**`_refresh_userdata` staying advance-only is the one thing here worth revisiting.**
An item un-watched on *another* device stays watched locally. That is the existing
rule inherited from `db.update_userdata` rather than a decision taken in the sweep.

## 2. The websocket is the mechanism; the sweep is the fallback

`apply_userdata_event` is how watched state normally arrives, **and it is free**: the
server sends the changed values themselves, so an episode finished on a phone is
written from the message that announced it rather than from a request that goes and
asks.

Payload is `{UserId, ServerId, UserDataList: [...]}`, each entry a `UserItemDataDto` —
`ItemId`, `Played`, `PlaybackPositionTicks`, `PlayCount`, `IsFavorite`. Measured
against 10.11.11 and 12.0.0, which agree.

**Not every save produces one, and the exception is the one that would otherwise
matter most.** The server drops `PlaybackProgress` saves before it ever builds this
message (`UserDataChangeNotifier.OnUserDataManagerUserDataSaved`), so a client
streaming somewhere else announces its *start* and its *stop* and **nothing in
between**.

That is why the push does not replace the sweep — and why it is not a problem that it
does not: a position this client did not see move is caught at the stop, and our own
playback is mirrored locally without the server's help.

Ids not in the catalog cost one indexed SELECT and are dropped, which is most of them
— the server adds each item's *parent* to the list for its own indicator refresh. This
runs on the **websocket thread**, so a list long enough to hold that thread up is
handed to the sweep instead of walked inline.

### What the sweep covers that the socket cannot

`_refresh_userdata` exists for the stretch where nothing was listening — offline,
logged out, not running — after which there is nothing to replay and only asking will
do. Plus the narrower case above: another client that finished something and never
reported its stop, which the server records and announces to nobody.

Without it the catalog is a download-time snapshot across exactly that gap, so offline
browsing shows a series you finished on the flight out as untouched, and "delete
watched downloads" (which reads `userdata_json` with no server fallback) quietly skips
it.

Batched — one request per `USERDATA_BATCH` ids per server, rather than the per-item
call the auto-download reaper makes, and spaced by `USERDATA_BATCH_PAUSE` so a large
catalog does not arrive as a burst. Nothing is waiting on it, so the spacing costs
nothing anyone can see.

## 3. The sweep schedule is a server *appearing*, not an interval

A sweep covers a stretch during which nothing was listening, and a server **becoming
reachable is the end of exactly such a stretch** — so that is the trigger, in place of
the interval this used to have. Startup needs no special case, since every server
transitions into the set on the first pass; `_sweep_due` starts True anyway so a
catalog is swept even on a machine with no servers configured yet.

**Watched here rather than subscribed to.** `clientManager` has an
`on_server_connected` hook that means almost precisely this, and two things argue
against hanging the sweep off it:

- it is a **single slot** that `mpvtk_browser/ui.py` already assigns, and it is
  assigned *after* `syncManager.start()` runs — so taking it would mean clobbering the
  browser's use of it or growing a fan-out for one more listener;
- it is a **notification**: it fires from five call sites, and a sixth path that
  reconnects without calling it would leave a gap that is invisible until somebody's
  catalog is stale.

The registry is the state itself, so a comparison against it cannot miss a transition
however the server came back — health check, websocket redial, or the user logging in —
and it is a set comparison on a loop that already runs every five seconds.

Disappearances are recorded but trigger nothing: there is nothing to catch up on with
a server that just went away.

### The floor defers, it does not drop

A suppressed trigger would be a stretch of time nobody ever looks at again. The flag is
set because something happened the websocket could not report, and that does not stop
being true because a sweep happened to run three minutes ago. So `_sweep_due` stays up
and the sweep goes out as soon as it is allowed to — which is what makes a flapping
server cost **one sweep per floor** rather than one per flap.

Two other things hold a due sweep back, and **neither consumes it**:

- **the settle** — nothing sweeps in the first `USERDATA_SWEEP_SETTLE` seconds after
  the catalog opens, so the first screen has the network to itself;
- **having nobody to ask** — the worker's first pass happens before `login_servers()`
  has registered a single client, and a sweep there reaches no server. Counting it
  burned the startup trigger and left the floor to defer the real one by five minutes.
  *A pass with no clients is not a sweep that found nothing; it is a sweep that did not
  happen.*

The floor is skipped while `_last_userdata` is zero, for the same reason it is measured
with `time.monotonic()`: that clock counts from boot on every platform this runs on, so
on a machine launching the app at startup "five minutes since the epoch" is a real
comparison, and it used to suppress the first sweep of the session.
