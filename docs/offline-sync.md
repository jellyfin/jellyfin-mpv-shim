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
| `record_watched` | the user picks Mark played/unplayed | **verbatim, both ways** |
| `OfflineVideo._mirror_locally` / `record_offline_progress` | playback of a downloaded item | **advance-only** (written online too, not just offline) |
| `db.set_reading_position` | a page turn in the built-in reader | **verbatim** — a book is a cursor |

**A series or season id fans out.** `db.watched_targets` resolves one id to
every episode under it by scanning the catalog — there is nobody to ask when
the server is away, which is the whole point of holding the rows locally.

**Playback is advance-only** because reports arrive out of order and a position that
went backwards is a stale one.

**A deliberate mark is not.** Mark played / Mark unplayed is the only signal in the
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

**Declined loudly**, the same way `allowed_servers` reports "enabled but no
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

### Only watched items may be evicted for space

The single most consequential rule in the planner. Pinned by
`test_the_cap_never_evicts_something_unwatched` and
`test_staying_over_the_cap_stops_the_fill`.

The tombstone table (`auto_discarded`) exists so the age rule can tell "never
downloaded" from "downloaded and reaped"; without it the planner re-queues what
it just deleted.

### Server shapes this relies on

- **`UserData` is not an `ItemFields` value** — it rides on `EnableUserData`.
- **`/NextUp` omits `MediaSources` unless asked.** Without it every candidate
  falls back to `_UNKNOWN_SIZE`, so the cap is spent against a guess for 100% of
  items. Both: `docs/jellyfin-api-notes.md` §4.
- `get_episodes(start_item_id=…)` is **inclusive** — the first entry is the anchor.
- Only the server knows what other clients have done, which is why the planner
  asks rather than inferring from the catalog.

## 5. The file store, and the one place it deletes

The tree is `<root>/catalog.db`, its `catalog.db.bak`, and
`<root>/<server_id>/<item_id>/` per download — plus `series/`, `season/` and
`playlist/` under each server directory, which are *shared artwork caches* and
not items. `<root>` is owned by the store: `relocate` moves every entry out of
it and refuses a destination that is non-empty or inside the current root, and
the sweep below deletes inside it.

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
