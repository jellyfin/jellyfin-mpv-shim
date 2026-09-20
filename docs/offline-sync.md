# Offline sync

`sync/` downloads items for offline playback and keeps a local catalog of what is
held. This file covers the part that is not obvious from the code: **who is allowed
to write watched state, in which direction, and what schedule the catalog is kept
fresh on**.

## 0. The five principles everything here is a consequence of

**What this feature is for is `docs/offline-sync-goals.md`, G1 to G5, in the
owner's words.** Read that first; it is shorter than this and it is the
authority. This section is the layer underneath it: how those goals resolve
when two of them pull against each other, worked out one case at a time over
thirty-two rulings and stated here so the next case can be *derived* instead of
adjudicated again. The fifth is not about what to do at all; it is the one that
says how much a question is worth.

Most of the rules below look arbitrary on their own, and several look like
defects. A change that satisfies a rule while breaking the principle under it
is a regression however green the suite is.

**1. Do not lose the user's data** — G5, the only absolute, and G4. Their
files, their credentials, their library. The store deletes inside `<root>/server/` and identifies what it
deletes rather than inferring it (section 5); a move carries the user's own
files and a failed copy may not lose them; a file that has vanished is fetched
again rather than reasoned about, because files should not randomly go missing.

**2. Do not lose or corrupt the user's watch state — and when those conflict,
lose it.** G2 says *"err on the side of caution to avoid losing a user's
progress that happened elsewhere"*; this is what "elsewhere" turned out to
mean, and it is the one that looks wrong at a site. Watched marks recorded
against a copy we cannot attribute are *discarded* rather than carried onto an
account, several times over, and each discard is a real viewing the user does
not get back.

The asymmetry is blast radius. **Losing local state is bounded**: one machine's
record of one viewing, the server's own copy untouched, and the user can
re-mark it. **Misattributing it is not**: the replay queue pushes it as some
account, and the server's watch activity — the authoritative record every other
client of theirs reads — is now wrong, in a way that is indistinguishable from
real viewing and correspondingly hard to find or undo. A wrong mark that
propagates outranks a right one that is lost.

Three consequences that otherwise read as inconsistencies: progress that cannot
be attributed is recorded locally so resume works but is **never queued**; the
push is advance-only while the pull may retreat (section 1); and one account
being *shown* another's state is a defect while one account's state being
*written* from another's is a blocker — because a read stops at the screen and
a write leaves the machine.

**3. When the information is incomplete, do the most reasonable thing with what
is there — and never let missing evidence read as agreement.** The one with no
goal behind it: G4 asks for sound rather than perfect, and this is the rule
that kept being needed to decide what "sound" meant in a specific case. A download whose
origin cannot be established gets its own category rather than a guessed one; a
playlist scope nobody can determine matches only the unscoped row and never
every row; an id collision with no comparable byte count on either side is
treated as a mismatch and downloaded fresh. The failure this is written against
is the quiet one, where absent information is taken for a match and the wrong
file plays.

**4. A thing belongs to the account that did it, not to the connection it
arrived on.** G3's "multi-user and multi-server in a sane way", sharpened by
the cases into something predictive. A saved login is an address that answered, and one person can
hold several for one server while one machine can hold several people. So
watched state files under `(ServerId, UserId)`; auto-download belongs to the
account that turned it on and survives the server being re-added; a download
records the account that asked for it; and the acting identity reaches the
filing authority **unnormalized**, because a resolver that substitutes a
plausible account one frame earlier produces a record that is consistent and
wrong — which no check downstream can catch.

**5. This store is a clone, and the effort owed to a failure is bounded by what
it actually costs somebody.** G4 and G3's test, and the one that decides when
to *stop* rather than what to do. Two questions, in order: **what was lost that
the server does not have?** — a downloaded file, a playlist grouping, cached
artwork, a tombstone are all clones, recoverable by re-syncing, and queued
offline progress is the only state on this machine that is not one — and then
**would anyone file it?** A severe-sounding outcome with no population is a
sentence in a document, not a mechanism.

The absence of this one is not hypothetical. The tiebreak in principle 2 was
ruled five times (R2, R15, R24, R31, R32) over a case whose exposure is close
to nil: every download row carries a DTO naming its server (measured 2000/2000
on 12.0.0, 14/14 on a real catalog), so an orphan needs a catalog loss, a
manifest written before `_home_row` stamped ServerId into it, a viewing while
orphaned, and a re-home — to cost one resume position on one item. The original
hedge, *"leaning towards discard"*, was correctly calibrated. Four of the five
rounds were not, and nothing in the rules said so.

Each is drawn from the decisions in `docs/rulings-log.md`, which records every
one with the case it was ruled on. Where a rule below cites a ruling, that is
the specific decision; this section is the reason there was one.

**Worth knowing, because it is the argument for keeping this section short and
the goals shorter:** the tiebreak in principle 2 was ruled five separate times
(R2, R15, R24, R31, R32) before anyone wrote down why. Two of three independent
readers given only the cases predicted the *opposite* rule — the same one the
code itself held for a fortnight. It is not recoverable from the sites; it has
to be stated.

## 1. Watched state belongs to a person, and its writers differ on purpose

**Watched state and resume positions are per actor**, in `item_userdata`, keyed
on `(item_id, ServerId, Jellyfin UserId)`. Not per machine, and not on the saved
login: one server can carry several logins for several addresses, so two of them
can be one person. There is **no second copy**: the `userdata_json` blob on the
downloads row held the download-time snapshot, was kept only for a downgrade,
and went with that guarantee in 3.0.0 (CX8) — nothing had read it since the
per-actor table arrived.

This is not cosmetic. The blob is overlaid onto the DTO the offline browser hands
out, the detail page builds Resume from that DTO, and playing from that offset
queues progress for whoever is playing. One blob per item meant the second
account to press Resume told *its own* server it had watched what the first
account watched.

**Two places, and the split is the point.** `SyncManager.actor_of` answers *who
is acting*: the pair the websocket already sends, else the login doing the
acting **as itself even when it is on another server**, else — with no acting
login at all, which is the offline case — the active local profile's account on
that row's server. When nobody can be named it answers `NO_ACTOR`, a real key
rather than NULL, since `NULL = NULL` is false in SQL.

`SyncDB._row_sync_state` then answers *what may be done about it*, and it is the
only thing that decides a filing key. Both tables consult it — the per-actor
userdata and the outgoing queue — so the rule has one implementation rather than
one per table. Four verdicts: `sync` (the acting person's server is the row's;
file it, it may be queued), `local` (the row's server is unknown, or nobody can
be named on it; record it here and never send it), `refuse` (the row belongs to
another server, so this is not our film), and `no_row`.

**Why the resolver does not do both.** It used to substitute: handed a login on
server B and asked about a row on server A, it passed B over and answered with
the profile's account on A. That pair *matches the row*, so any check downstream
accepted it and the mark landed on a film that account had never opened — the
record became consistent and more wrong. A check cannot catch a mismatch that
was resolved away before it runs, so the acting identity arrives intact and the
store is what refuses it.

Progress that cannot be attributed is still recorded locally so resume works,
and never queued, because there is no account to send it as. An **orphan** row —
one whose content server is unknown — files under the sentinel on *both* halves:
no server means no account, so it is one machine-wide bucket, and it never syncs
in either direction.

**Deletion is the deliberate exception and stays machine-wide.** One file on
disk, so one decision: `db.played_by_anyone` is what "delete watched downloads"
and the auto-download reaper ask. On a shared machine one person finishing an
episode can therefore reap another's unwatched copy when auto-delete is on. That
is accepted; making it narrower needs a claim system recording who *requested*
each download, which is a third key again and is not built.
[iw], 2026-09-12.

Each writer's direction is a separate question from whose state it is. Getting a
direction wrong is invisible until someone browses offline.

| writer | trigger | direction |
|---|---|---|
| `mirror_playstate` | our own playback | **advance-only** |
| `apply_userdata_event` | a `UserDataChanged` push | applies the payload |
| `_refresh_userdata` | the periodic sweep | advances, and **retreats** when the server says unwatched |
| `record_watched` | the user picks Mark played/unplayed | **verbatim, both ways** |
| `OfflineVideo._mirror_locally` / `record_offline_progress` | playback of a downloaded item | **advance-only** (written online too, not just offline) |
| `db.set_reading_position` | a page turn in the built-in reader | **verbatim** — a book is a cursor |

Every one of them takes the actor as a required keyword. That is deliberate: the
scope used to be implicit, and a caller that has not thought about whose state it
is writing gets a signature error rather than filing it against the wrong person.

**A series or season id fans out.** `db.watched_targets` resolves one id to
every episode under it by scanning the catalog — there is nobody to ask when
the server is away, which is the whole point of holding the rows locally. It
answers with each row's **content server**, which is what the caller resolves
the actor against. Never the row's saved login: that names whoever
*downloaded* the copy, which on a shared machine is a different person from
the one marking it, and it is tried before the server — so wherever the
downloader's credential still existed it won.

**Playback is advance-only** because reports arrive out of order and a position that
went backwards is a stale one.

**A deliberate mark is not.** Mark played / Mark unplayed is the only signal in the
app that is authoritative in *both* directions, so `record_watched` writes verbatim
through `db.set_watched`.

Un-watching is the half that did not work before that existed. Every writer of the
column was advance-only, so a downloaded item un-watched from this app's own menu
stayed watched on the copy on disk **forever** — the sweep was advance-only too, so
nothing would ever have corrected it. Offline browsing showed a tick the user had just
removed, and "delete watched downloads" was still willing to throw the item away.

The sweep retreats now (below), but `apply_userdata_event` still does not, so an
un-watch made on *another* device arrives at the next sweep rather than on the socket
that announced it. Ratified rather than left open: a sweep runs at launch, and an
un-watch made on *this* client goes through `record_watched`, which writes both ways
at once. `docs/do-not-fix.md` F47.

`record_watched` is called **unconditionally** at its call sites, with no check of
whether the item is downloaded, because `db.watched_targets` answers with nothing for
an item we hold no copy of. That is what keeps the check from being forgotten at a
call site again.

**The pull retreats; the push does not.** This was the one thing here worth
revisiting, and [iw] revisited it: *"server can sync unwatched state back to the
offline store if it was previously observed as being watched last time it checked.
Locally though an unwatched state shouldn't clobber a remote watched state unless the
user was recorded deliberately marking something as unwatched, as stale unwatched
state could sync up to the server and destroy recorded progress otherwise."*

So the sweep may clear an actor's `played` when the server says unwatched — through
`db.update_userdata(allow_retreat=True)`, which nothing else passes — **unless the
replay queue still owes that actor a watched advance**, meaning a `pending_playstate`
row with `played = 1`. That entry is a mark this machine made and has not delivered, so
the server's "unwatched" predates it.

**Not "any pending row".** `pending_playstate.played` is nullable and ordinary offline
progress queues position-only entries, which say nothing about an unsent mark; blocking
on those would make the retreat unreachable for anyone who watches anything offline. A
position-only entry keeps its queued position while `played` retreats.

The look and the clear are one critical section, because playback can otherwise create
local state and its queue row on either side of a separate look.

**Named hole:** online playback whose timeline report fails queues nothing, so a
genuine local view can be retreated. The advance-only version had the same hole from
the other side.

### A refusal is logged, because it used to be invisible

`_row_sync_state` can answer `refuse` -- the row's `content_server_id` is not the
server the acting account is on -- after which `_write_userdata` returns,
`upsert_playstate` answers False, and `userdata` answers "nothing stored". All three
are correct, and none of them used to say anything anywhere, so the symptom reaching a
user was watched state that quietly stopped syncing with a clean log. It is also what
let the e2e suite sit red for fourteen commits.

`SyncDB._note_refusal` says it twice over: a DEBUG line per refusal naming the item,
and a summary at **INFO**, which is the level a default configuration writes
(`log_utils.root_logger.level`) -- an issue report has to carry this without the
reporter having been told to turn anything on first. The summary is rate-limited per
(row server, acting server) pair, because a mismatch fires again on every progress
report for that item, and it carries the count suppressed since the last one. In a
healthy single-server install it never fires at all, which is what makes one line at
INFO affordable.

`no_row` is deliberately **not** logged at either level: every `UserDataChanged` the
server pushes about anything undownloaded lands there, so it is the ordinary case and
not a fault. `local` gets a DEBUG line from the queue door only -- keeping
unattributable progress and never queuing it is a ruling rather than a fault, but it
is the answer to "my offline progress never reaches the server".

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
watched downloads" quietly skips it.

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

**A profile switch is not a trigger of its own** — it is a reconnect, and that is
deliberate (D1). `switch_user` stops every client and calls `connect_all`, so the
connected set empties and refills with the new profile's uuids, which the edge above
already reads. There used to be an explicit `syncManager.request_profile_sweep()`
here; it was deleted because all three things it did are either unnecessary or the
descope:

- *clearing the answered set:* unnecessary. It is keyed on the **account** now, so
  the new profile's accounts were never in it and the first reap waits for a sweep as
  them.
- *trusting the reconnect edge:* its reason for not trusting it was that two profiles
  can hold one credential uuid, which rested on `force_unique` — passed by nothing, and
  removed in step 6, so `_finalize_login` always mints a fresh uuid4.
- *clearing the floor and resetting the settle:* dropped. With no deferred-sweep
  obligation a switch waits out both like any other reconnect, so a switch inside
  `USERDATA_SWEEP_FLOOR` of the last sweep sees this account's state up to five
  minutes late. Do not add the trigger back without a ruling: the model it belonged
  to is what R16 removed.

**One index from a ServerId to a way of talking to it**, and two questions that must not
be merged. `SyncManager.routes_for(content_server_id)` answers *which door is open* — a
lookup on `_connected_routes`, the inverse of `content_id_for`, built once per pass.
`_client_for_actor(server_id, user_id)` answers *who can speak as this person*, and may
never answer with somebody else's client. The download queue asks both, in order, through
`_client_for_row`: the account the row was queued for (R19), then any door to its content
server for a row that names no account, then the row's own login for an orphan that has
nothing else (F48 in `docs/do-not-fix.md`). None of the three means the row stays pending
and the queue moves past it — [iw]: *"let the download fall back to a queue."*

**The sweep is grouped by content server, not by the login on the row.** One box
reached through two addresses is two logins and one server, and two people on one box
are two logins and one server. Grouped by login, half a catalog silently stopped being
swept while a live client sat right there, and the other half was refreshed as whoever
downloaded it. Both were observed against the QA server, which answers one `ServerId`
on two addresses and holds twelve accounts.

**Within a server, the sweep asks about the union R21 names** (R16 in the narrow
form): the rows *the signed-in account downloaded* — `downloads.requested_server_id` /
`requested_user_id`, written at enqueue since R19 — plus **all** of that server's rows
when the account is one this machine auto-downloads for. The second half is ongoing
interest; the first is R16's own scoping. What it stops paying for is a shared machine
sweeping the whole catalog as whoever is signed in: a request per batch, and a second
account's userdata row written for every item somebody else downloaded. Push is
untouched and stays universal, which is how another person's watches still reach their
server.

An account that is **not** connected is not asked and not waited for: its rows keep the
state they have until its profile is next active. That is the whole of the laziness —
no second client, no per-credential auth, and no change to the connection model, which
groups a server's credentials into one fallback chain and connects exactly one.

### Lazy, per connected account

The sweep asks as whichever account is connected for a server and files what
comes back under that actor. **An account that is not connected is not asked
and not waited for** — its rows keep whatever was last recorded for them
until it signs in again.

That is what makes switching profiles need no schedule of its own: signing in
is what triggers the work, so there is nothing to remember and nothing to
catch up. The alternative — holding a second client per server so absent
accounts could be swept too — was considered and declined, because it doubles
the connections to serve state nobody is looking at.

It has a cost, and it is accepted rather than unnoticed: deletion is
machine-wide (section 1), so a deferred account's stale `played = 1` can
authorise deleting a file that account has not actually finished. That is
`docs/do-not-fix.md` F46, and narrowing it needs a claim system recording who
*requested* each download — a third key, which is not built.

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

## 3a. The replay queue: what a server has not been told, and by whom

`pending_playstate` is the list of changes made while a server was away.
**It is keyed on the actor**, `(item_id, ServerId, Jellyfin UserId)`, with a
unique index, and it drains through *any* live login that speaks for that
person. That last part matters: one person can hold several logins for one
server -- a LAN address and a remote one are two -- and the login that comes
back is often not the one they were signed in as offline. Keyed on the login
that queued it, such an entry stayed pending with a working route open beside
it. `downloads.server_uuid` on a queue row is the login that queued it and is
dead; nothing reads it.

**Nothing unattributed is ever queued.** `upsert_playstate` refuses an entry
with no person behind it, because there is no account to send it as: it could
never be drained and would sit in the table for good. Such progress is still
recorded locally, where resume needs it. That refusal is also what retired a
real bug: the key used to allow NULL, `NULL = NULL` is false in SQL, so the
lookup matched nothing and **every write inserted a fresh duplicate row**.

The migration folds those duplicates into one entry, keeping the furthest
position and any watched mark, and drops entries nobody can be named for.
Two ordering traps, both found only by running it against a real catalog and
both now pinned by tests: the unique index cannot be created in the schema
script (`CREATE TABLE IF NOT EXISTS` is a no-op on an existing catalog, so the
columns are not there yet), and it cannot be created before the fold (the
duplicates violate it, and the migration fails on exactly the catalogs that
carry them).

### A queued viewing whose login is gone is kept

An entry is a promise to tell some server what some person did. When its saved
login no longer resolves — you watched an episode offline, then removed that
server and added it back — the promise cannot be kept, and the migration used
to **delete** the entry on a rule that appears in no register: *"if it names an
actor migrate it, otherwise drop it"*. Measured on a real catalog, that deleted
a local watched mark whose file is still on disk.

**R18: the viewing survives.** Its server half is recovered from the item's own
download row — the same join `drop_unsyncable_playstate` performs — and no
person is named. What it is worth is then **local only**: "watched" on screen,
`played_by_anyone` and the reaper's grace period; nothing sends it, and it is
never an answer to a *named* server asking who watched this. R15 still drops
the `(@none, @none)` bucket when the copy is re-homed, because the per-account
state supersedes it — ruled again as **R32** when it turned out the code had
been merging it instead, and delivered there.

**Where it survives is `item_userdata`, not the queue** — ruled 2026-09-19,
and this paragraph used to say the opposite. R18 kept the row in
`pending_playstate` and named those three readers for it. All three read
`item_userdata`; nothing outside `sync/db.py` reads `pending_playstate` at
all. So the kept entry was worth none of what it was kept for — and
`manager._open_and_run` runs `drop_unsyncable_playstate()` on every launch,
which deleted an orphan row's entry three statements after the migration logged
that it had kept it. The mark is merged into `item_userdata` under
`(item, server, @none)` — the key a row whose server is known gets when its
reader cannot be named — and the queue row goes, because the queue is
advance-only and an entry that can never be sent is a block rather than a
wait. That key is **not** the one homing drops (R15/R32): that one is
`(@none, @none)`, and this has a server precisely because the download row
could supply it.

An entry about an item this machine no longer holds has no server to recover.
It survives anyway, under the sentinel on both halves: no server means no
account, which is the one machine-wide bucket `item_userdata` already has.

## 3b. One row per item id, across every server

**Item ids are not unique across servers.** Jellyfin derives one from the
media's path with no server component in it, so two installs both mounting
their library at `/media` hand out the same id for *different files*
(`docs/jellyfin-api-notes.md` 13b, with the runnable derivation).
`downloads.item_id` is the catalog-wide primary key, so only one of the two
copies can ever be held.

The catalog therefore **answers only for the server that owns the row**.
`is_complete`, the three `downloaded_*_ids` sets and `library_id` take a
required scope, and `enqueue` refuses rather than letting `INSERT OR REPLACE`
take the other server's row and orphan its files. Without that, asking for a
film on one server silently played the other server's file at the same path.

**The scope is the Jellyfin `ServerId`, not a saved login**, and the
difference is the whole of it. Our `uuid` names a *login*; one server can
carry several — two accounts, or two addresses for one box — and "do we hold
this item" is a property of the server and the media, so they must all get
one answer. Scoping content on the login instead made a second account on one
server lose the badge, the library answer and the ability to download a film
the machine was already holding. The column is `downloads.content_server_id`,
written by both row writers from the item's own `ServerId` and backfilled on
existing catalogs from `item_json`. It is **not** `downloads.server_id`, which
is the dead on-disk path key (`docs/do-not-fix.md` 1).

Callers outside `sync/` hold a login uuid, so exactly one place translates:
`SyncManager.content_id_for`, reached through `SyncManager`'s facade methods.
It is the one door, and `UserManager.server_id_for` is what it asks behind that
door — naming the second as the translation is what let a caller reach past the
first and keep an older convention. Adding a third is how the two keys got
mixed up in the first place. The scope is keyword-only, so a call site that
predates the change fails loudly instead of scoping on the wrong key.

**Unscoped is asked for by name.** `ANY_SERVER` is the value that means "every
server", and it is not falsy — a falsy scope now means *a login that resolves to
no server* and matches no rows. The two used to be one answer, which served a
read (where permissive is right) and a narrowing (where permissive is maximally
wrong) with the same value.

Two different things widen a read, and they are not the same mechanism — one is
the **scope asked for**, the other is the **row asked about**:

- **fully offline asks `ANY_SERVER`** — no client and so no login named, which
  means no second server to confuse a row with; the pseudo-server `"offline"` is
  recognised here rather than being passed to a query as itself;
- **a row with no content server answers only the unscoped ask** — reachable
  for a row whose manifest would not parse, since both writers set it and the
  migration backfills it. It used to answer *every* named server, on the true
  premise that it cannot be shown to be somebody else's — and that was the
  wrong direction to be permissive in: it is how one server's tile ticks for
  another's film, and how the wrong local copy substitutes for a stream. What
  that branch bought was a file on disk staying visible and deletable, and
  that is bought elsewhere now: every unscoped caller still sees it, and the
  offline library lists it under **Orphaned Items** (R25). Offline playback is
  unaffected, because that path asks with `ANY_SERVER`;
### Visibility is not permission to substitute

The widening above is about **visibility** — is this download listed, badged and
deletable. It is deliberately generous, because a download that cannot be shown
to belong to somebody else must not become an invisible, undeletable file on
disk.

**Playing the local file instead of streaming is a different question and gets a
stricter answer.** Jellyfin derives an item id from the media's path with no
server component in it, so two servers over one library hand out the same id for
different files; the generous answer, handed to playback, played one server's
file for another server's item. `sync/offline_media.py:_may_substitute` is the
rule, and it lives beside the substitution rather than in the catalog on purpose:

- **a live client is asking** — substitution requires an identical item id *and*
  an identical server id. `ANY_SERVER` does not qualify, a login that resolves to
  no server does not, and neither does a row with no server recorded;
- **nothing asked** — no client, or `work_offline` is on. Ungated: the item id
  came out of the catalog row itself, so there is no second candidate.

It keys on whether a client is asking, **not** on the resolved scope value,
because `_asking_server` answers `ANY_SERVER` both when nothing asked and when
the login lookup *raised*. A rule reading its answer cannot tell those apart and
would give a client whose server could not be established the ungated treatment
meant for offline playback. Refusing substitution there falls back to streaming;
the downloads list stays full, which is the direction that matters.

**Inside a SyncPlay group, the size must match too.** A matching id is unlikely
to name a different *video*, but the server's copy may have been replaced since
the download, and a different cut desyncs every member of a group against one
shared timeline. So the row's `size_bytes` is compared with the current
MediaSource `Size`, and a size that cannot be established counts as
disagreement — a group implies a reachable server, so refusing costs a fallback
rather than the film.

`watched_targets` used to be listed here as a third. It is not: it takes the
content scope like every other read, through the same clause, and its second
return value is the row's own content server rather than an owner to
attribute a mark to. It was unscoped because the downloads screen passed the
pseudo-server and scoping on that matched no row — an objection that goes
away once the caller translates its login first, which is what every content
read already did.

A colliding id still cannot be downloaded from both servers. That needs a
composite key and is written up as open in `docs/do-not-fix.md`, F43.

**Progress is a different question from content and takes a different key.**
Watched marks and resume positions belong to a *person*, so they are actor
scoped where content is server scoped. That is section 1.

### One door decides whose a copy is, and it may delete

Two entrances write rows keyed on an id that is not unique across servers:
`enqueue`, when you ask for a download, and `_adopt_orphan`, when the startup
sweep finds media on disk with no row. Only one of them used to check. They
share `SyncManager.claim_identity` now, and the reason to keep it single is
that it is a rule with teeth — it deletes files.

**It derives the content key from the item itself**, not from the credential
the request arrived on. The two entrances used to derive it differently, and
where they disagreed — a credential whose `Id` was never written, a server
whose `ServerId` was regenerated, a login resolved through another local
profile — nothing matched any row we held, so every answer was "refused",
`enqueue` raised `DownloadCollision`, and the item could never be downloaded
again. The DTO is the right source because it is the same value `_add_row`
stores in `content_server_id`: the question asked and the answer stored
cannot drift apart.

Seven answers. `free` (no row), `ours` (already this server's), `refused` (the
row names a different server, so this is somebody else's film), `rehomed`
(an orphan the evidence says is the same content — it becomes this server's
where it stands, with no re-download, because the bytes are already right),
`reaped` (an orphan the evidence does not agree about — row, files and local
watched state go, and the caller downloads afresh), `stale` (the same case
asked with `may_reap=False`: nothing deleted, ask again when the deletion is
justified), and `busy` (it would have reaped, but a worker is writing into
that directory now).

**The evidence is bytes, never metadata.** `Type` is an input to the id
derivation and so adds nothing; `Name` is editable; two encodes of one film
routinely share a runtime. So the comparison is the declared size of the
MediaSource the downloader *would* pick against the bytes actually on disk,
and it is three-valued: agree, differ, or **no comparable count on either
side** — which is treated as "differ". Missing evidence must not read as
agreement; that is the silent failure the whole scoping rule exists to
prevent. One consequence worth knowing rather than rediscovering: a `Book`
carries no MediaSources at all (`docs/readers.md` section 1), so a book
collision always falls to the unknown branch and downloads fresh. That is
cheap for a book and it is the safe direction.

**Why refusal is right without being knowledge.** A differing `ServerId`
proves a different namespace, not a different film — we refuse because we
cannot tell, not because we know. That is also why the reap is allowed at
all: [iw], *"the user already asked for a download, an orphan shouldn't stop
it"*. A copy the catalog cannot account for loses to a person asking for one.

**Two things here look like defects and are not.** The `stale` verdict exists
so the *refusal* can run before `_add_row`'s `INSERT OR REPLACE` while the
*reap* stays behind the filters that decide whether the request even wants
the item — an item the request then declines was never asked for, and
deleting it would lose an episode to a download that never happened. And a
second guard on the adoption path is the shape that produced a repair loop
once: both entrances go through the one door, or the rule has two
implementations and they drift.

## 4. Auto-download

Keeps upcoming episodes on disk without being asked. Runs as a scheduled job on
the sync worker's idle loop, and **only while nothing is playing** — downloading
the next episode is worthless if it costs the one you are watching its bandwidth.

### The two sources

- **Next Up** — the server's own Next Up list, i.e. the next episode of every
  series you have started. Broad, and scales with how many shows you have going.
  (Roughly 50 entries on a real library.)
- **Lookahead** — for series you already hold downloads for, the next N episodes
  from where you are *watching*. Narrow, follows a binge.

Independently switchable.

### Which server's downloaded copy a book reader gets

`db.get` is a primary-key read and **item ids collide across servers**
(docs/jellyfin-api-notes.md 13b), so it can answer with a row another server's
copy wrote. It takes a keyword-only `server_id` through `_content_clause` now,
defaulting to `ANY_SERVER`: the readers inside `sync/` are unscoped on purpose,
because they already hold the row's identity and are asking about the file on
disk.

The caller that is asking a server's question is the browser's
`book_download_state`, and it takes its scope **without a default** — as does
`open_downloaded_file`, which is what hands a path to the desktop. A colliding
id on server B would otherwise open server A's downloaded file, and for a book
that file is not an offline convenience: the app cannot render one, so it is
the only way to read the thing at all (CX7).

Every caller already had a server in hand — the three book pages take the
route's, not the browser's current one, because the catalog-changed hook can
fire after the user has moved on. The one exception was the
pending-reads map in `item_actions`, which is keyed by item id and now carries
`(name, server_uuid)` as its value: the key cannot say which server a Read was
pressed on, and the flush that opens the file has nothing else to go by.

### A playlist is identified by (id, server)

**Jellyfin hashes a playlist id from its *name*.** Measured, not inferred:
`Example Playlist` came back `cf0ce7fc72247deaa755cc40b9219e0d` from both the
QA 10.11 container and a personal server, and `md5(type+path)` does not
reproduce it. So two unrelated servers hand out the same id, and with
`playlists.playlist_id TEXT PRIMARY KEY` the second download **overwrote** the
first server's name, membership and ownership — and a delete of either took
both.

The catalog keys both playlist tables on `(playlist_id, server_id)` now, as a
`COALESCE(server_id,'')` unique index rather than a composite primary key:
`server_id` is nullable ("could not tell", which every content read admits on
every scope), SQLite allows NULLs in an ordinary table's PRIMARY KEY, and a
UNIQUE index treats two NULLs as distinct — so either of the obvious spellings
would let `INSERT OR REPLACE` pile up one unscoped row per write.

- Every write and every read is scoped, keyword-only and **defaultless**
  (`SyncDB._playlist_scope`); a defaulted scope is the permissive-by-omission
  shape the rest of this work removed. `None` names the unscoped row and is
  never "any".
- `playlist_items` **stores** its server rather than joining through
  `downloads`: `delete_playlist` deletes membership, and a join cannot see a
  membership row whose item row is already gone.
- `playlist_ownership` is the one deliberately unscoped read — the Downloads
  screen browses every server at once and `downloads.item_id` is unique
  machine-wide — but its value carries the server so the grouping can tell two
  same-named playlists apart.
- **An existing catalog is migrated by rebuilding both tables**, because SQLite
  cannot drop a PRIMARY KEY. Each row's server is derived from its members: the
  one distinct `content_server_id` among the downloads it lists, and NULL when
  they disagree. Derived at migration and at download time, never at read time
  — visibility would otherwise follow which members happen to be *complete*.
- **A read-only open runs neither the schema nor the migration**, so an
  unmigrated catalog answers these reads **unscoped**, exactly as it did before
  the column existed. Without that the offline library raises `no such column`,
  `reload` turns any exception into an empty library, and every download is
  invisible on the one launch that has nothing else.

**Its cached poster is scoped the same way**, at
`<root>/server/playlist/<content_server_id>/<playlist_id>/`, through the one
helper the writer and the offline reader share (`db.playlist_art_dir`). They
held separate copies of that layout before, each with a comment explaining why
the server was *not* in the path — which is how a layout change becomes a tile
that silently stops drawing. `server` stays a literal: `downloads.server_id` is
NULL on every row, so the store is one directory, and the *content* server goes
below `playlist/`. A scope that is not a plain id becomes `unscoped` rather
than being joined, since this builds a filesystem path out of a value the
server supplies.

Posters already on disk are moved by `SyncManager._rehome_playlist_art` at
startup: `os.replace` per file, atomic within a filesystem, and the pass is
**unconditional on every open** (R23) — so an interrupted move leaves each
poster at one path or the other, never neither, and the next launch finishes
it. The only file it deletes is a duplicate the destination already holds,
which is the newer write. Orphaned directories — a playlist deleted while its
poster stayed — are untouched, as they always have been; that is why
`playlist` is in `RESERVED_STORE_DIRS`.

**The offline library spells a playlist's id with its server in it** —
`constants.offline_playlist_id`, in the manner of `offline:movies` beside it.
That library is one pseudo-server showing every download at once, and the
browser routes a tile by its DTO `Id`, so two tiles sharing one are one tile as
far as routing is concerned: whichever server's items were built last answered
for both. The DTO carries `ServerId` too, because the delete gesture reads it
off the DTO the way it does for a real one.

Two callers reverse it, and `split_offline_playlist_id` answers an ordinary id
unchanged so neither has to test first: the delete gesture, which needs the two
halves apart because the catalog does; and the downloaded-tile badge, whose set
holds the catalog's plain ids — compared raw, every playlist tile in the offline
library loses its tick while every other tile there keeps one.

**And the duplicates are named apart**, since the id is a hash of the name and
so the two tiles are called the same thing: `Music (izzie-fileserver)` and
`Music (stdjflib)`. Only where there is a duplicate — one server and a bare
name is the normal case, and qualifying every playlist would be noise on every
machine to serve the few with two. The server's name comes from any saved
credential (`UserManager.server_name_for`, injected into the offline source
like `actor_on`); when nothing names it the label is left alone, because a raw
ServerId on a tile is worse than an ambiguous one.

### Which servers it may pull from

An explicit allow-list, because a logged-in server is not necessarily *yours*
and pointing unattended downloads at a friend's box is a rude default. **Empty
means none**, and switching the feature on seeds the server you were looking at
when you did it, or it would come on and fetch nothing.

**It is keyed on the account, and it lives in `users.json`** — one list per
local profile, of `(ServerId, UserId)` pairs (R14; `users.set_auto_download`).
Both halves of that were a silent failure before:

- keyed on the **login uuid**, a server answering at two addresses is two
  uuids and one account, and `clients._connect_all` registers the live client
  under whichever address answered first. So unattended fetching was
  configured, enabled and doing nothing for the whole of a trip — and the
  "enabled but no servers" warning could not fire, because the list was not
  empty.
- stored in the **global config**, one profile's ticks applied to everybody on
  the machine.

The old `auto_download_servers` config key is adopted into the profiles at load
and cleared (`users._adopt_legacy_auto_download`). Adoption is marked per
profile — `None` never adopted, `[]` adopted and empty — rather than by the key
being empty, so a clear that could not be written does not undo an untick on
the next launch. **No reader of that key is left**; adding one restores the
uuid keying for anyone whose clear never landed.

Not gated on who is in the user picker. R14 asked for that and withdrew it: the
constraint is resource usage, not liveness (`docs/rulings-log.md` R14, R21).

One consequence of the account keying, **ruled** (R22): removing a server and
adding it back keeps unattended downloading on for it, where the uuid keying
turned it off by accident (the re-added login got a fresh uuid). Deleting a
connection is not turning the setting off. Re-authentication was never affected
either way, since `open_reauth` keeps the uuid on purpose.
`test_a_login_removed_and_added_again_keeps_the_setting` is the pin.

### The `auto:` origin is the whole safety story

Everything auto-download fetches is marked with an `auto:` origin naming the
source that queued it (`db.ORIGIN_*`). **The reaper only ever considers auto
rows**, so nothing the user asked for is deleted to make room, however tight the
cap. Asking for an auto-downloaded item by hand **promotes** it to user-owned and
takes it out of the reaper's reach for good. Recording the source also lets the
downloads manager show each as its own subtree.

Promotion is one-way.

**`origin` is nullable, and three-valued logic is the trap.** `NULL GLOB 'auto*'`
is NULL, not false, so `auto_size` / `list_auto` exclude un-backfilled legacy
rows. **Do not rewrite those queries into a form that matches NULL** (for example
`origin IS NOT 'user'`) — that would make every legacy row reapable, i.e. would
delete manual downloads.

**The reaper runs before the planner**, so a run that is over budget can free
space and then use it rather than skipping for a whole interval.

### The watched grace period, and the clock it is measured from

`auto_download_keep_watched_hours` holds a finished auto-download on disk for a
while instead of deleting it on the next pass — so it can be rewatched, or seen
by somebody else in the house, without fetching the whole episode again. `0` deletes
on the next pass.

**The default is 24 hours, and that is load-bearing rather than a preference.** The
reaper no longer asks the server whether a row is watched; it reads what the last sweep
wrote, and when there is no server to sweep from, this window is the only thing holding
a file back. Offline is exactly when a downloaded copy is worth the most.

**The clock is the catalog's `watched_at` column, stamped by the reaper the first pass that
observes the item played**, and cleared when it is observed unplayed. Not the
server's `LastPlayedDate`, which is the obvious choice and is absent with the
server away and on some Mark Played paths: an anchor that can fail to exist
decides by accident which side of the deadline an item falls on. The cost is
that an episode finished on a phone while this machine was off starts its window
when the machine next looks, which is the harmless direction — nothing was going
to delete it during a stretch when nothing was running.

**Cleared whenever the item is observed unplayed, whether or not
`auto_download_delete_watched` is on.** Nothing else in the app writes that
column, so this is its only eraser — and while the clearing sat under that
setting a stamp outlived the un-watch that should have voided it any time the
rule was off in between. The next viewing then measured its window from a
viewing the user had already taken back, and the item was deleted on the first
pass with none of the grace the setting promises.
`test_the_stamp_does_not_survive_the_setting_being_turned_off` is the guard.

**Stamped once, never re-stamped.** Re-stamping on every observation is the
whole failure mode available here: the deadline then walks ahead of the clock by
one interval per pass and the item is never deleted at all, silently, for as
long as the app keeps running. `test_the_deadline_does_not_move_when_passes_keep_running`
is a run of hourly passes across the window for exactly that.

**A watched item inside its window does not fall through to the age rule.** That
rule deletes what it calls "unwatched", which is the one thing this item is not,
and reaching it would delete what the grace period exists to keep while logging a
false reason.

**Retention waits; the cap does not.** The size rule below still evicts a watched
item inside its window, because a grace period the cap respected would stop
auto-download altogether for the length of the window whenever the budget was
full. That is the decision, not an oversight.

**Including at *exactly* the cap.** The eviction loop breaks on `size < cap`, not
`size <= cap`. At the boundary nothing would be evicted, `free_budget()` would return
zero and the planner would queue nothing — and a capped folder settles into precisely
that state, so with a day-long grace it would stop fetching for a day. `<` makes
exactly-full behave like over-full and land strictly under.

### The lookahead is anchored on watch progress, never on what is on disk

Anchoring on the furthest episode held is the obvious reading of "keep N ahead"
and **it is a ratchet**: each pass starts where the last pass finished
downloading, so the window walks the whole series whether or not anybody watches
it, and only the size cap ever stops it — by which point the disk is full of
unwatched episodes the reaper may not evict.

Anchored on the server's Next Up for the series, the window only advances when
the user does, so a series that is not being watched settles at N episodes and
stays there. Already-held episodes inside the window are skipped by `fill`, so in
the steady state this queues nothing until an episode is watched.

When the anchor is unknown — the series is finished, or the server will not say —
the window is **not** extended. The wrong guess there is the runaway this exists
to avoid.

Pinned by `test_the_window_does_not_walk_the_series_on_its_own` and
`test_the_window_advances_when_you_watch`.

### Held episodes are counted as ids, intersected with the window

**Ids rather than a count**, and the caller intersects them with the window. That
is the whole correctness of the hysteresis, and it is not what the first version
did: that one counted every held episode of the series, so somebody holding
twenty *old* episodes was above any minimum for ever and **the series was never
topped up again** — silently, with no downloads and no error. The requirement is
"at least the minimum number of *upcoming* episodes", and **upcoming** is the
word doing the work.

**Queued and in-progress count, not just complete.** Without that, every pass
re-queues the same episodes for as long as the first batch takes, which is a
stampede.

**Errored rows do not count.** Those are episodes we tried and failed to get, and
treating a failure as stock is how a series quietly stops being topped up — the
same failure as above, reached another way.

`None` (not an empty set) means "unknown"; an empty set means "hold nothing", and
conflating them tops up on every pass.

### Hysteresis is both or neither

A half-configured `auto_download_lookahead_min` / `_max` pair is something a
person can type into the JSON, and guessing the other half is worse than
declining: "min 5" with no max could mean top up to 5, or top up to the old flat
window, and those differ by however large the series is.

**Declined loudly**, the same way `allowed_accounts` reports "enabled but no
servers" — silently doing nothing is otherwise indistinguishable from a bug. Also
declined when max < min, which is the same class of typo. Pinned by
`test_half_configured_declines_loudly` and
`test_max_below_min_is_the_same_class_of_typo`.

A hand-typed negative flat lookahead falls back; it is **not** clamped to 1.

### The cap is a soft ceiling

`fill` enforces the cap against *anticipated* sizes, which the server sometimes
under-reports or omits — so a pass can overshoot by up to one item plus whatever
the estimates got wrong. Real on-disk bytes are what `auto_size()` measures on
the next pass, so **an overshoot throttles the pass after it rather than
compounding**.

`_MAX_PER_PASS` is 20 (#661 asked for that number). `_UNKNOWN_SIZE` is 2 GB,
because free items would otherwise let an unbounded number through. `0` means
unlimited and a negative value allows nothing — a "fix" that clamps to 1 breaks
the second.

### The reaper asks nobody, and runs behind the sweep

It used to ask the server per row — one blocking `get_userdata_for_item` per candidate,
tens of round trips on a real Next Up list — because the catalog's userdata was a
download-time snapshot. It is not one any more: the sweep writes into it,
`apply_userdata_event` writes into it, and `mirror_playstate` writes into it for every
item played whether or not the copy on disk is the one being watched.

So the round trip is replaced by an **ordering**: `SyncManager._auto_after_sweep`
requests a sweep, waits for it to land, and only then lets `auto.tick()` run. Removed
rather than kept as a fallback, and that is the point — a per-row live read is a second
authority over watched state, and it asked as `downloads.server_uuid`, the login that
*downloaded* the copy.

Three outcomes, not two:

- **the sweep landed** — run the pass;
- **it has not, and somebody could still answer** — hold, and try again next loop;
- **nobody could answer** — run the pass anyway. Offline requires no sweep, and holding
  for one would switch retention off for the whole of a trip. A client-list *failure*
  counts as offline: we cannot tell it from one, and holding on what we cannot tell is
  the starvation direction.

The hold is bounded by `REAP_SWEEP_HOLD` (900s), so a server that answers the
connection but never the request cannot switch retention off for the life of the
process. "Answered" means every batch covering that server returned — the sweep clears
its own due flag before asking anything and swallows per-server failures, so *a sweep
happened* is true of a pass that refreshed nothing, and `_refresh_userdata` breaks out
of a server's batch loop on the first failure, so a server can be half done.

Answered is per **session**, not per pass — meaning the *hold* never fires again for a
server that has answered once. Every pass still asks for a sweep and still gets one;
what it stops doing is waiting for the result. What this closes is a reaper reading a
download-time snapshot, and once the catalog has been written for a server the websocket
keeps it live, so blocking on a second reading adds delay rather than safety — and
per-pass would make the hold routine instead of exceptional, since any pass whose hour
fell inside the sweep floor would wait the floor out for nothing. A profile switch
invalidates it.

**What the ordering does not buy.** The reap reads `played_by_anyone`, an aggregate
over every actor, and the sweep refreshes only the connected one. A deferred account's
stale `played = 1` therefore stays deletion-authoritative and can lose a file that
account has not watched. Accepted with its reasons in `docs/do-not-fix.md`.

### Only watched items may be evicted for space

The single most consequential rule in the planner. Pinned by
`test_the_cap_never_evicts_something_unwatched` and
`test_staying_over_the_cap_stops_the_fill`.

The tombstone table (`auto_discarded_scoped`) exists so the age rule can tell
"never downloaded" from "downloaded and reaped"; without it the planner
re-queues what it just deleted.

**It is keyed `(item_id, server_id)`, and there is only one of it.** There used
to be a second table keyed on the item id alone, whose rows bound *every*
server — and item ids are not unique across servers, so once one server's copy
was discarded and deleted, another server's **different film of the same id**
was invisible to the planner forever, with nothing left in the catalog to
explain it. `SyncDB._migrate_discard_scopes` folds those rows in where a held
download still names the server and drops the rest.

So **a discard that cannot name a server records nothing at all.** That reads
like the re-queue loop coming back and is not: the planner only ever asks on
behalf of a *connected* server, so a candidate it finds there names one, and the
fetch it was not suppressed from produces a row that names one too — making the
next discard scoped and permanent. One redundant fetch, and only for a row whose
own server is unknown (an adopted orphan, listed under Orphaned Items).

### Server shapes this relies on

- **`UserData` is not an `ItemFields` value** — it rides on `EnableUserData`.
- **`/NextUp` omits `MediaSources` unless asked.** Without it every candidate
  falls back to `_UNKNOWN_SIZE`, so the cap is spent against a guess for 100% of
  items. Both: `docs/jellyfin-api-notes.md` §4.
- `get_episodes(start_item_id=…)` is **inclusive** — the first entry is the anchor.
- Only the server knows what other clients have done, which is why the planner
  asks rather than inferring from the catalog.

## 5. The file store, and the one place it deletes

The tree is `<root>/catalog.db`, its `catalog.db.bak`, and **one** directory
literally named `server`, holding `<item_id>/` per download plus `series/`,
`season/` and `playlist/`, which are *shared artwork caches* and not items.

**It is one folder, not one per server, and as of 3.0.0 it cannot be anything
else.** `_item_dir` spelled the level as `row["server_id"] or "server"` — a
per-server layout that never existed, because `_add_row` wrote `None`
unconditionally and the only writer that could produce anything else was
`_adopt_orphan` reporting a directory that must already exist, so a store of
all-`NULL` rows could not bootstrap a non-`NULL` value. Measured on the
reference install: 14 rows, 0 non-`NULL`, one directory. [iw] ruled the layout
is not a requirement (*"One folder is probably fine in practice"*,
`docs/offline-sync-goals.md`), and CX8 then **dropped the column**: the level is
`sync.db.STORE_DIR`, one constant for all five paths that spell it, and the sweep
walks that one directory.

That distinction was worth days: one word doing two jobs — where bytes live, and
which server content came from — is what `content_server_id` exists to separate,
and every reader who took the spelling of `_item_dir` at face value re-derived
the confusion. **The tests had it too**: every `_reconcile_disk` fixture put its
media under `<root>/srv/`, so the suite exercised a sharded store that did not
exist and the one-directory case was untested.

`<root>` is owned by the store: `relocate` moves every entry out of it, refuses
a destination that is non-empty or inside the current root, and verifies each
cross-volume copy against its source before dropping the original; the sweep
below deletes inside it.

#### Choosing the destination

The path is typed, with no folder picker behind it, so the message a refusal
carries is the only thing that says what to type instead. Four things about it,
each of which was a wrong message rather than a wrong outcome:

- **The path is normalized first** (`normalize_root`, shared with `start()` so a
  hand-edited `sync_path` cannot mean a different folder from the one the
  settings field shows). Surrounding whitespace and a matched pair of quotes go:
  Windows Explorer's *Copy as path* yields `"C:\…"`, quotes included, and a
  double quote cannot appear in an NTFS name — so the quoted spelling names
  nothing that can ever exist, and the refusal was `Can't create that folder`,
  a sentence about permissions for a path that was only mis-typed.
- **"The same folder" is `same_directory`, not `==`.** Windows is
  case-insensitive, so a user who retyped their own download folder with the
  drive letter in the other case fell past the equality check into the
  *containment* one, and was told — correctly, and uselessly — that the folder
  is inside itself. `normcase` over `realpath`, so a junction or symlink to the
  store is recognised as the store too.
- **An empty destination is probed by writing to it.** `makedirs(exist_ok=True)`
  answers without touching a folder that already exists, so one this process
  may list and not write — another account's, a read-only mount, a share
  exported ro — passed every check, and the move stopped the download worker
  and closed the catalog before failing. `os.access(W_OK)` is not the test:
  on Windows it reports mode bits that do not describe an ACL at all.
- **`EACCES`/`EPERM` during the move gets its own message**, the same way
  `ENOSPC` does. It is the backstop behind the probe, which writes one file at
  the top of the tree and cannot speak for what is under it.

`tests/integration/test_download_relocate.py` runs all of this against a real
catalog with real media, on both platforms; its cross-volume legs want
`JMS_ALT_VOLUME` and otherwise force `os.rename` to refuse so the copy path is
still covered. Every case ends by asserting the store is still a store — a move
that returns `(True, "")` and leaves a folder that cannot describe itself has
failed, and no assertion about the return value can see that.

`SyncManager._reconcile_disk` is the only code that deletes media the user did
not ask to delete, and every one of its four tests is load-bearing. It acts on
a directory only when:

1. **the catalog is readable** — `SyncDB.healthy()`, the one strict read;
2. **its server directory is one the catalog names** — never everything in the
   root;
3. **its name is shaped like an item id** — `_looks_like_item_id`;
4. **and it is not a live row**, nor one `_adopt_orphan` can rebuild.

Each of those exists because inferring instead deleted something. An
unreadable catalog answers `[]` to every query, and one zeroed 4 KiB page (the
`downloads` b-tree root) of a 64 KiB catalog therefore deleted 60 of 60
downloads on startup. A download folder pointed at a directory the user
already had files in lost `~/Videos/Holidays/2019 Italy` on the *second*
launch — the first was covered by the empty-catalog guard in `_open_and_run`
and no launch after it.

### The catalog is the only thing that says what the files are

Names on disk are ids, so without it the UI cannot list, play or delete a
download: the folder stops being an offline library and becomes unlabelled
weight only a file manager can clear. Four mechanisms keep it:

- **A read-only probe before anything opens it writable.** Opening writable is
  not a read: the constructor runs the schema and the migration, so on a
  zero-byte file it *creates* the tables and `healthy()` then says yes of a
  catalog with nothing in it — trusted, empty, and never restored from. The
  probe is `SyncDB(read_only=True).healthy()`, the same strict read, so there
  is only ever one answer to "can the rows be read". A writable open can still
  fail where the probe passed (the schema touches every table, the probe reads
  `downloads`), and that failure is a verdict, never an exception past the
  caller.
- **`catalog.db.bak`**, written after every clean open. An **empty** catalog
  never replaces a backup that has rows — otherwise the backup deletes itself
  on precisely the launch it exists for.
- **Restore**, when the catalog is unreadable *or missing* — **one function
  for both**, since they differ only in whether there is a bad file to set
  aside. Written as two branches it drifted at once, and the second copy had
  neither of the steps below. The copy is staged and renamed, so a restore
  that fails on a full disk has changed nothing and the next start tries
  again; the old file is kept as `catalog.db.corrupt-<ts>` because it is
  still the better copy of anything the backup predates, and its
  `-wal`/`-shm` move aside with it — left at the live name they replay the
  bad pages into the restored file, and a catalog that went *missing* can
  still have its WAL beside it. **A failed restore leaves no catalog at
  all**: the launch runs on a read-only handle rather than letting sqlite
  create the empty one, which is *readable*, so every later launch would find
  nothing wrong with it and never retry.
- **`_adopt_orphan`**, which rebuilds a row from the download's own
  `item.json` + media. This is what stops a restore being a *delayed* wipe:
  everything downloaded after the snapshot has files and no row, which is the
  orphan shape. A restored catalog is known stale, so its launch does not
  sweep at all — that is what protects a part-downloaded item, which cannot be
  adopted.

A download that cannot be described — an unparseable `item.json` — is
**left alone**, not deleted. The recoverable error is keeping something the
user could remove by hand; the unrecoverable one is the reverse.

#### Adding a column to `downloads`

Three lists, and the one that is easy to forget is the one that decides
whether the value is stored at all.

1. The `CREATE TABLE`, for a fresh catalog.
2. `db._ADDED_COLUMNS`, for an existing one. A new nullable column, backfilled
   in place. **Not because the migration must be additive** — that guarantee was
   cut in 3.0.0 (CX8; see `_migrate`'s docstring) and two migrations now rebuild
   a table — but because an `ALTER TABLE ADD COLUMN` is the cheap case and a
   rebuild is not. A drop or a rewrite is a deliberate boundary, and it goes
   through a rebuild with its own test.
3. **`db.COLUMNS`, which is the whole `INSERT`** — or `db.NOT_UPSERTED`, if
   a re-download should leave the value alone rather than reset it, which is
   what `watched_at` wants.

Miss the third and nothing breaks: the column exists, the migration adds it,
reads work, and every caller's value is discarded on the way in. `library_id`
lived that way for the whole life of the shader-profile library scope — the
download path resolved it, `upsert` dropped it, and the column was `NULL` for
everyone. `EveryColumnIsAccountedForTest` in `tests/test_sync_manager.py` is
the guard; it compares the two lists against the table itself, so a column in
neither is a failure naming it.

The read side deserves the same suspicion. The test that should have caught
this stubbed the catalog lookup, so it asserted that the play path trusts an
answer while the write that supplied one did nothing.

### Two ownerships, and both must be released together

A row is protected from the reaper by `origin` and from a playlist delete by
`playlist_items.owned`. Asking for an item by hand releases **both**
(`set_origin` and `_claim_from_playlists`); releasing only the first meant
deleting the playlist still deleted a film the user had separately downloaded.

**Releasing is a property of row creation, not of who asked for it.**
`_add_row` releases any claim standing over an item the catalog does not have,
on every origin — while only the user's own request released one, a scheduled
auto-download wrote its row straight into a claim and handed the playlist a
file it never pulled in. The exception is a playlist download re-queuing a
member it already holds a row for, which `enqueue` decides
(`claims_its_members`) because it is a fact about the *request*:
`_record_playlist` recomputes ownership from `pre_existing` a few lines later,
so dropping the claim first would disown the copy the playlist pulled in
itself. `_claim_from_playlists` therefore states one proposition and takes no
exception of its own — it used to read the type out of `_adopt_orphan`'s
manifest, a file some other build may have written.

`_adopt_orphan` releases both too — and not because it is a user gesture.
`ORIGIN_USER` there means only "the reaper may not take this", since a row this
method invented has no evidence the download was ever scheduled; it does not
mean the user claimed the item, and `_delete_playlist` does not consult
`origin` at all. Releasing one claim and not the other protects the file from
whichever deleter happens not to run first.

**Ownership never outlives its row.** `replace_playlist_items` writes an entry
only for an item the catalog has, and checks that *inside* the transaction that
writes it. The caller cannot: `_record_playlist` decides membership from a
`pre_existing` snapshot taken before it queued anything, and nothing serialises
it against a delete — `enqueue`, `delete_item` and `delete` take no
manager-wide lock and the browser runs them on a worker pool. A claim left
standing over an item with no row is a claim on whatever writes that row next,
which is `_adopt_orphan`, on a file it is trying to keep.

`_cancelled` is a third, transient claim — a delete waiting for the worker to
notice between chunks, which can take the 60 s read timeout. `enqueue`
withdraws it (`_uncancel`), or changing your mind inside that window queued
the item and had the worker's unwind delete it underneath.
