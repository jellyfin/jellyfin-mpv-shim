# Offline sync: the goals

**These are goals, not requirements.** They are what the feature is *for*. Requirements are derived
from them and are negotiable; these are not. [iw]'s words, 2026-09-13.

They exist as a document because this subsystem spent four days and four review rounds without them,
and the cost of their absence was specific: the branch's acceptance criterion became *"we stop
finding serious problems with it"*, which has no falsifier, so every plan was checked against the
code and every review against the plan. Nothing was checked against what the feature is for.

**The goal above all five, [iw]'s words, 2026-09-15:**

> "offline sync is a mess, most of that code never landed, **the goal is a working feature that
> makes sense**"

Read that before the table below, because it settles a question three diagnoses have circled:
**the branch is not the thing being preserved.** Most of this work never shipped, so "that is too
large a change for a branch this size" is not an argument here, and a descope is not a loss. What
is being protected is the user's data (G5) and their un-uploaded progress (G2) — not the diff.

---

## The goals

1. **Allow the user to sync media from their Jellyfin server for offline use.**
2. **Allow progress that happened while offline to sync back to the server it was downloaded from,
   but err on the side of caution to avoid losing a user's progress that happened elsewhere.**
3. **Handle multi-user and multi-server in a sane way without excessive resource usage.**
4. **Avoid data loss, but if a sync is lost it's not the end of the world as it is an offline clone
   of an authoritative server copy: we want it to be sound, but if the user does something
   adversarial the system doesn't need to be perfect.**
5. **Never ever allow the feature to destroy unrelated user data on the filesystem of the user's
   computer.**

> **Everything else is either an implementation detail or a requirement originating from
> implementation coherence.**

That last sentence is the one to apply first, and it is a *test*. For any rule this subsystem is
about to adopt, ask which of the five it serves. If the answer is "it makes the code consistent with
itself", it is a coherence artifact: it may still be worth doing, but it is not a requirement and it
must not be defended as one. Most of what this branch argued about was in that category.

**Where two of these pull against each other**, the resolutions are in `docs/offline-sync.md`
section 0 — worked out one case at a time across thirty-two rulings, because the goals say which
things matter and not which one yields. The sharpest is G2's: a user's progress recorded here can
be *lost* or it can be *misattributed*, and losing it is the lesser harm, because a local loss
stops at this machine while a wrong mark propagates to the server every other device reads.

---

## How to read each goal

### G4 is the one that sets severity, and it demotes most things

The store is **a clone of an authoritative copy**. Losing it costs a re-download. So "data loss" in
this subsystem means something much narrower than it sounds, and the question for any failure is
*what was lost that the server does not have?*

- A downloaded file, a playlist grouping, cached artwork, a tombstone, a partial download: **clones.**
  Recoverable by re-syncing. Annoying at worst.
- **Queued offline playback progress is the only state that is not a clone.** It exists on this
  machine and nowhere else until it drains. Everything in G2 hangs off that.

G4's second half — *"if the user does something adversarial the system doesn't need to be perfect"* —
retires a class of hardening. Deliberately pathological configurations get sound behaviour, not
airtight behaviour.

### G2's "err on the side of caution" is a decision rule

It resolves direction, not just priority, and it points the same way every time: **when unsure
whether progress belongs to a server or a person, do not push it and do not delete it.** Applied, it
settles several questions that otherwise look like open design choices — what an unattributable copy
answers when a named server asks, whether a queued mark outlives the file it describes, whether local
state may be discarded because the server probably has it.

### G3's "sane" has a test, and it is a revealed-preference one

> "Would a reasonable person become annoyed enough that I broke it that they would hassle me in an
> issue tracker about it?"

[iw]'s worked example, which fixes the calibration at both ends:

- **Not issue-worthy:** someone adds the same account twice to one profile, and progress syncs back
  against one credential instance rather than the other. Nobody files that.
- **Issue-worthy:** downloads not behaving sanely on a **split-horizon setup** — one server reachable
  at a local and a remote address. Somebody files that.

This is a band measured rather than assigned, and it is more useful than a severity label because it
can be applied to a behaviour nobody has classified yet.

### G3's "without excessive resource usage" is operative, and was invisible

It drove design decisions on this branch and appeared in no document, so no review round could check
against it — the reason it is written here. Its clearest instance is `890a4e55`, which chose **lazy**
per-account sync over eager and recorded the measurement behind it: the QA server answers one
`ServerId` on two addresses and holds twelve accounts, `_connect_all` connects exactly one login per
server, and grouping the sweep by `downloads.server_uuid` therefore *"left half a catalog never swept
with a live client sitting open."* The rule that came out of it: **an account that is not connected is
not asked and not waited for**; a profile switch schedules the sweep that refreshes it.

Note what that means for reviewing this subsystem: a proposal that makes the sweep ask more servers,
more accounts, or more often is spending against a goal, and needs to say so.

### G5 is the only absolute, and it is about the user's data, not ours

Every other goal trades. This one does not. [iw], asked whether a specific set of sweep conditions
*was* the goal:

> "It's about protecting user data. If someone throws something into the offline directory or they
> set their downloads folder as the download directory, avoid deleting anything that isn't ours out
> of the folder. How exactly we do that is an implementation detail."

So the goal is stated over the **folder**, not over the sweep: the store may be a directory the user
also uses for their own things, and the named case is somebody pointing the download folder at
`~/Downloads`. Two consequences that are easy to get backwards:

- The question is never "is this ours to delete?" answered permissively. It is **"can we prove this
  is ours?"**, and anything unproven stays. Deleting a stranger's file is unbounded harm; leaving one
  of ours behind costs disk space, which G4 already says is survivable.
- The sweep's current conditions — only inside a container the catalog names, only children shaped
  like an id it could have written, never the store root, nothing at all when the catalog is empty or
  unreadable — are **one implementation** of this, not the goal itself. They may be replaced by
  something better. What may not change is that a file the feature cannot prove it wrote survives.

**Moving the folder is the exception, and it is deliberate.** [iw]: *"moving the download folder
including unrelated files is fine and expected, they just can't get lost due to a failed copy."* So
`relocate` enumerating everything in the old root is **not** a G5 violation: the user chose a folder
the app told them it manages (a non-empty destination is refused with that wording), and carrying
their files along with it is the expected behaviour rather than a trespass. G5 bites on *durability*
here, not on ownership — a same-volume rename is atomic, a cross-volume copy keeps every original
until all entries are across and is now size-checked before any source is dropped, an error undoes
what went, and a kill leaves files at one root or both but never neither.

---

## Where the status of this work lives

**Here, and nowhere else.** It lived in a dated plan for two days and that plan went stale while
still describing itself as the register — its own step-3 heading read "PART DONE" after the last
three pieces had landed. The status below was re-derived from the tree on 2026-09-20, not read off
a document.

**Nothing is owed.** Every load-bearing item classified below is closed; the two rulings that
scoped the work are R16 (the deferred-sweep descope, in the narrow form) and R17, both in
`docs/rulings-log.md`.

This table has gone stale twice, in both directions, and the second time a reviewer scoped to it
chased three closed defects. If it disagrees with the code, the code is right and this is a bug.

## Applying this to the work in flight

Classified by *what was lost* (G4) and *would anyone file it* (G3's test).

**Load-bearing — G5.** Two properties, and they are different. For the **sweep**: a file the feature
cannot prove it wrote survives; any change that widens what it is willing to delete trades against
that, whatever form the conditions take. For the **move**: whose files they are is not the question —
nothing may be lost when a copy fails, which is now enforced by comparing sizes before any original
is discarded (`63ca67fc`).
(A defect of exactly this shape was written and withdrawn on 2026-09-13: granting the sweep authority
over a legacy container no row names, which would have deleted a partial download two launches after a
catalog loss. Note that under the `~/Downloads` case it would also have been walking a directory full
of the user's own files.)

**Load-bearing — G2, and each of these destroys the only non-clone state there is.**

**Status as of 2026-09-15, re-checked against the tree at `3a632d3c`.** The previous version of
this table said three of these were open when they were not, and listed a fifth that describes a
safeguard nobody ever built. It was edited *after* the fixes landed without the table being
touched, and it is the document `P6` points every review at — so a reviewer scoping to it chased
three closed defects. Re-check this table against the code before trusting it; that is the second
time it has gone stale.

| | Status |
|---|---|
| The migration deleted every queued entry because its resolver answered `None` for every login | fixed, `e632f3f8` |
| The same deletion via an unparseable `users.json` — an empty registry is indistinguishable from "no such login" | fixed, `0eb08ca2`; **and dissolved** by R12 — in that state the sync subsystem does not load, so there are no consumers of the sentinel. What remains is not a deletion bug but a durability one: `users.py:save()` has no `fsync`, and R12 wants a last-good backup and a restore path. |
| `drop_unsyncable_playstate` deletes a queued mark one launch after its download is deleted, though `db.delete` preserved it on purpose | fixed, `fbe7bc89` (the DELETE opens with `EXISTS`) |
| A local blob ahead of the server, discarded, after which the first sweep *retreats* local state to the server's older position | closed by `79dee9a9` — measured on a consistent snapshot and found already fixed by `e632f3f8`; the two shared one cause. "Two G2 items, not three." |
| ~~A refusal to open a newer catalog that routes into restore-from-backup~~ | **never existed.** `grep -rn user_version jellyfin_mpv_shim/` is empty; the schema check is column-presence via `PRAGMA table_info` (`sync/db.py:355-366`), additive, with no version refusal. This row described a hazard of a *proposed* design, not of the code. |

**That G2 item is CLOSED.** It was `_migrate_playstate_actors` deleting a queued viewing whenever
the registry answered "cannot say who", on a rule — *"if it names an actor migrate it, otherwise
drop it"* — that appeared in no register. R13 flagged it and **R18 replaced it**: the entry is
kept,
its server half recovered from the item's own download row and its person half left empty, worth
something locally and nothing to any named server. No G2 item is open.

**Load-bearing — G3.** Two servers' same-named playlists must both be holdable (playlist ids hash the
playlist's *name*, so this collides on name alone — issue-worthy). Split-horizon downloads must work
(issue-worthy, and `do-not-fix.md` F48 files the opposite as an accepted limitation. **Resolved
2026-09-15: neither document is simply live — a measurement gates it**, and [iw] offered a
direction with it: *"if the server is not connected right now because a fast switchable user isn't
keeping a connection alive, let the download fall back to a queue."* F48 also under-counts: it
names three sites and there are at least five, the extra two being the auto-download allow-list
and the followed-series scan, where the consequence is not a late fetch but no unattended fetching
at all for the whole of a trip. R14 re-keys that allow-list to the account, which removes that
pair.) Resource cost of any new sweep or fetch.

**Not load-bearing — coherence artifacts.** The `server_id`/`content_server_id` naming split; the
shard-by-Guid layout; the retention clock and `NOT_UPSERTED`; legacy artwork cache loss; the stranded
pre-change partial; playlist grouping lost in a migration; **and the downgrade guarantee** — "an older
build must open, list, play and delete" serves no goal, and under G4 a downgrade that loses the
catalog loses a clone. It generated the additive-only migration rule, the `user_version` refusal, and
a large fraction of two audits. It matters only where it destroys queued progress, which is G2.
**Acted on in 3.0.0** (CX8): the guarantee is cut at one stated boundary, and the three columns it was keeping alive are gone.
G2 is untouched by that — the queued state is kept and folded, never dropped on a schema
judgement.

---

## Who fetches, and who is asked — settled

This looked like a conflict between G3-resource and G2-caution. It is not, because **downloading and
sweeping have different requirements** and only one of them needs a live session. [iw]:

> "for simplicity sake we should just download items as the user that requests them, you don't need a
> live websocket session to download from a Jellyfin server"

> "For sync, lazy sync applies, determine if the apiclient is valid based on user and server foreign
> id match not the local API Client id"

So:

- **Downloads run as the account that requested them.** A download needs a credential, not a
  connected session, so there is no reason to hand somebody else's queued item to whoever happens to
  be signed in. This removes the cross-account hazard entirely rather than mitigating it, and it
  removes the reason the download path ever consulted the live client registry.
- **Sweeps stay lazy.** An account that is not connected is not asked and not waited for; a profile
  switch schedules the sweep that refreshes it (`890a4e55`).
- **An apiclient is the right one when its `(ServerId, UserId)` match — never when a local id
  matches.** The local API client id is a handle, not an identity. This is the rule the whole
  `server_id` confusion was an absence of: the catalog names *foreign* ids, and the local uuid is
  looked up from them at the moment of use.

That last rule is why "routes do not live in the catalog" is a real requirement rather than a tidiness
preference — a stored local id is an identity claim the local machine is not entitled to make. The
replay queue already works this way, matching the pair (`manager.py:_client_for_actor`).

## What the goals do not settle

- Two accounts on one server, both wanting to be swept: the foreign-id rule above says each is found
  by its own `(ServerId, UserId)`, and lazy sync says each is asked when connected — so both people's
  watched state can be correct without two live sessions. What is *not* settled is what happens to
  the account that is never connected again.
- How much resource usage is "excessive". There is no budget, only the comparison `890a4e55` made.
- Whether a *repair* to a clone is worth its risk. G4 says losing a clone is survivable, which cuts
  both ways: it also means a migration that rewrites clones is spending risk for little return.
