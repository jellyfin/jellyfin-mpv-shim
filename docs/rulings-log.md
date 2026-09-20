# Rulings log — raw capture, un-promoted

**This file is a capture, not a design.** Each entry is [iw]'s own words at the moment a rule
surfaced, with the context that provoked it and what it settles. Nothing here has been woven into
the reference docs, and doing that is a separate, deliberate step.

**Why verbatim, and why the agent must not paraphrase.** Every entry below exists because a session
had just got the rule *wrong*. The model that just misunderstood a rule is the worst available
summariser of it: paraphrase bends the wording back toward the misunderstanding that caused the
violation. So the quote is the record, and the agent's reading of it is commentary, marked as such.

**Why capture at all.** A redirect — *"wait, no, that's not correct"* — is an undocumented rule
surfacing after violation, and it is the only source of rules whose adjudication has **already been
paid for**: the decision was made, stated, and then absorbed into a transcript and lost. Every other
way of getting rules (an authoring pass, agent-proposed amendments) *creates* work for the one person
who can least afford it. This costs nothing but writing it down.

**The bias to know about.** This is a lower bound over rules that were visible enough in some
output to provoke a reaction. A rule broken silently produces no redirect and no entry, and the
silent class is exactly the one that diffs are blind to. It does not replace enumerating a
population; the blind spots are disjoint.

Related: `docs/do-not-fix.md` (where the settled ones eventually belong), and COD-18's
drive-by-requirements design, which is where the mechanism this file imitates by hand is specified.

---

## Session of 2026-09-13 — the offline-sync coherence arc

Context for the whole batch: four review rounds and a 1,006-line diagnosis had failed to converge
on what `server_id` meant. Most of these were provoked by a session presenting a wrong or incoherent
reading and being corrected.

### R1 — the download store is one folder, and the layout is not a requirement

> "One folder is probably fine in practice, it might slow down things if someone opens a file browser
> there but that's about it."

*Provoked by:* a session having read an earlier, weaker remark — *"I don't have a strong feeling
about it, just don't want a huge number of files in one folder. We could do the 'subfolder is first
letter of the Guid' trick"* — as a **requirement**, and deriving four clauses, three open questions,
an artwork migration and a stranded-partial class from it.
*Settles:* the store stays flat. `downloads.server_id` is not a path key, because there is no
per-server path. Sharding is a separate concern if ever wanted.
*The lesson attached to it, which is worth more than the rule:* "I don't have a strong feeling" is a
non-requirement, and reading one as a requirement manufactures work that then has to be refuted.

### R2 — watched state recorded before a copy's origin is known is discarded

> "Leaning towards discard, otherwise anonymous state would get re-homed cross-user. The other answer
> could be it ties to the local multi-user id, but that's an extra id that means something else that
> isn't the server user id."

*Settles:* on learning a copy's origin, anonymous watched state is dropped, not carried onto a name.
*Two rejected alternatives, with reasons, which is the part a code comment would have lost:*
re-homing crosses users; a local multi-user id introduces a third identity that is not the server's.

### R3 — a vanished file is re-downloaded, even if the reaper then takes it

> "Am inclined to say let it be re-downloaded and reaped, files shouldn't randomly go missing."

*Provoked by:* being shown that a watched auto-download whose grace has expired, if its file
vanishes, is re-fetched over the network and then deleted on the next pass.
*Settles:* the disappearance is the anomaly. The wasted fetch is the accepted price of correcting it.

### R4 — one credential per server **per user**; multiple addresses are fine

> "My verdict does indeed sound incoherent, the rule should be one credential per server per user,
> multiple addresses fine, however that refactor might touch the account manager and I am not
> inclined to pile that into an already fraught branch unless we must"

**Supersedes R4a**, stated earlier the same session:

> "No, one account per server allowed on a profile. Separate profiles can contain the same server
> under different users. The app should refuse trying to re-add the same server to the same profile a
> second time."

*Why it changed:* R4a plus "collapse the duplicates" was shown to be self-contradictory — two
addresses for one server are normally the *same account*, so "same server and same account" catches
the fallback route it was meant to protect.
*Also settles a scope question:* deferred off the branch unless shown necessary. It was shown
unnecessary, so it is deferred.
*Carried into that work:* the revised rule permits two accounts on one server, but `_connect_all`
chains by server `Id` alone and stops at the first address that answers, so the second account would
never connect.

### R5 — treat behaviour that never reached master as never having happened

> "If the behavior for adoption never hit master assume it never happened."

*Settles:* the population a migration must repair is what shipped, not what a branch produced
mid-flight. **A decision rule, not a fact about one bug** — it generalises to every "do we need a
backfill" question.
*Its own first application refined it:* the behaviour in question *had* shipped, but the column that
contradicts it had not, so the branch's own migration would have created the inconsistency. Which
produced R6.

### R6 — fix an inconsistency where it is created, not where it is detected

> *(chosen from options as)* "Fix it where it's created"

*Settles:* if a migration would introduce a contradiction, the migration fixes both halves in one
pass rather than leaving a detect-and-repair step behind it.

### R7 — provenance of an identifier decides whether a collision has teeth

> "Where does the playlist id come from? If it is a path hash that has teeth because an identically
> named playlist on two servers is likely to have the same id, if it is a random guide it's fine
> as-is."

*Not a ruling — a question, and the most productive single move of the session.* Measured against the
QA server: create/delete/recreate a playlist of the same name returns the identical Guid, and the
path is `<server data dir>/data/playlists/<name>`. So it is a name hash, it has teeth, and against a
global primary key two servers' "Music" cannot both be held.
*The generalisable rule:* before accepting a collision as improbable, ask where the identifier comes
from. "Probably random" and "derived from a name" have opposite risk profiles.

### R8 — never silently downgrade https to a local http connection

> "what I don't ever want the app to do is downgrade an https connection to a local http connection,
> but some users might want that if the local direct connection is faster than the hairpin nat."

*Provoked by:* a session having just described the shim's single-`address`-per-credential storage as a
**flattening** of the apiclient's richer model — the apiclient merges server entries by `Id` and carries
`ManualAddress`, `LocalAddress` and `LastConnectionMode` on one entry (`credentials.py:85-115`), the
shim keeps one `address` string (`users.py:239-241`) — and having suggested that carrying those fields
through would be cheap.
*Settles:* it is not a flattening. **One address per credential is how this rule is implemented.**
Because each address is a credential the user explicitly added, connecting over a local http one is
honouring their configuration rather than a decision the app made. Put both addresses on one credential
and the app is *choosing* between them, which is silent by construction — and `_connect_all` sorts
same-subnet first (`clients.py:390-409`), so it would pick the local one.
*Amends R4:* one sign-in per server per user, several addresses permitted — **and the app never
automatically prefers a less-secure address over a more-secure one.** A user may prefer the direct local
route (hairpin NAT is slower); that has to be their explicit choice, not a fallback the app takes on its
own.
*Not offline-sync-specific.* This is a client-wide transport rule that surfaced here, and it is the kind
of rule `docs/offline-sync-goals.md` deliberately excludes — worth a home in the client's own docs.
*Scope consequence:* the deferred credential work is **not** "plumb a field through". Any change that
lets one credential hold several addresses has to carry this rule with it.

### R9 — for music, `Played=true` with `PlayCount=0` is the normal shape

> "if it's music, music is usually too short for jellyfin to accept it as actually played"

*Provoked by:* a session treating one row with `Played=true, PlayCount=0` as an anomaly worth chasing,
after a measurement artifact made it look like the only row whose watched mark failed to migrate.
*Settles:* Jellyfin does not count a short track as a play, so for audio the **`Played` flag is the
only progress signal there is** — `PlayCount` and `PlaybackPositionTicks` are both legitimately 0 on a
track the user has finished many times.
*Checked against the tree, and nothing is broken by it:* `auto._is_watched` reads the catalog's
`played` column rather than position or count, and `db.py`'s position logic (`:1440-1455`) only governs
a near-end report on an *already*-played item. Any future test of "does this have progress" that keys
on position or play count would make every music download invisible to it.

### R10 — a move carries the user's own files; a failed copy may not lose them

> "moving the download folder including unrelated files is fine and expected, they just can't get lost
> due to a failed copy."

*Provoked by:* a session reporting `relocate`'s enumeration of everything in the old root as a G5
violation, on the grounds that the folder might hold the user's own files.
*Settles:* it is not a violation. The user picked a folder the app told them it manages — a non-empty
destination is refused with exactly that wording — so carrying their files along is the expected
behaviour. G5 applies to this path as **durability**, not ownership.
*What it therefore required:* a size check on every copied entry before any original is discarded
(`63ca67fc`). ENOSPC already raised and was handled; a copy that returns *without* raising and is short
anyway did not, and nothing compared the two sides.
*The distinction worth keeping:* the sweep's G5 question is "can we prove this is ours?"; the move's is
"can anything be lost?". Same goal, two different tests, and answering the move with the sweep's
question produces a false finding — which is what happened.

---

## Process rulings from the same session

### P1 — planning finishes before a fix lands, even an urgent one

> "Fix the data loss now after we finish planning"

*Settles:* a data-loss fix found mid-planning is sequenced *after* the plan is closed, not bundled
into it. Keeps the fix its own commit with its own failing test, and keeps the plan from acquiring a
second subject.

### P2 — a plan is ratified only after a hole check, and this one has a history

> "Yes, after we ensure there are no holes in it, have lost count of the number of times we have
> tried to reshape this thing in this branch"

*Settles:* ratification is gated on an explicit hole check, not on the plan reading well. The second
clause is the reason and belongs with it.

### P3 — review order: the cheaper adversary first, the stronger one when it goes quiet

> "I do agree we should have codex pull on the loose ends of our plan and then have Fable do a review
> when codex stops finding gaps/loose ends."

*Settles:* iterate with the cheaper reviewer until it stops finding things, then spend the expensive
one. **Note where this failed in practice:** the cheaper reviewer never went quiet, and its second
round's findings were mostly defects in the first round's own corrections — at which point the
standing fixes-on-fixes rule said stop, and the two rules pointed opposite ways. [iw] broke the tie
toward the stronger reviewer, and it was the round that found the frame error.

### P4 — when a plan is defective repeatedly, hand the whole system out, not the plan

> "Descope to A2, hand the rest to fable: the architecture, my answers, and what is broken about the
> system and have fable propose a new, standalone plan that can change anything about offline sync to
> make it coherent"

*Settles:* after repeated rounds of a plan being refuted, the move is not another revision by the
same author. Ship the one verified piece, and hand an uninvolved author the architecture, the
rulings, and the defect list — with permission to change anything.

### P5 — the defect class review cannot see

> "Which those are the hardest defects to catch because they are in the shape of asking invariant
> questions about the entire codebase and NOT reviewing diffs or touched modules for correctness."

*Settles the diagnosis of the whole arc.* The defects review caught were local and diff-shaped; the
ones it missed were invariant-shaped and absent from every diff — a scope on a column nobody writes,
a migration running before its dependency loads, an allow-list whose keys cannot match, an
identifier whose provenance nobody checked. Three of those four share one shape: **something whose
job was to narrow, filter or fire, and which could not.**
*Acted on:* `tools/audit_vacuous_narrowing.py`, which enumerates that population and asks the
question mechanically rather than advising anyone to remember it.

### P6 — a further review must be scoped to violations of the acceptance criteria

> "Yeah... the review rounds kept finding flaws in invented requirements that didn's cohere with the
> acceptance criteria. If we run another review, it should be scoped to only being violations of the
> acceptance criteria."

*Settles:* review scope is bounded by `docs/offline-sync-goals.md`, not by the code or by a plan. A
finding that does not name a goal it violates is a coherence artifact and is out of scope by
construction.
*Why it matters more than it sounds:* across this arc, two invariant audits produced 26 findings and
the goals later retired most of them. The rounds were not sloppy — they were unbounded, and an
unbounded review against an artifact with no acceptance criteria finds flaws in the artifact's own
inventions. This is the smallest change that fixes that, and it is a change to the *brief*, not to the
reviewer.

### P7 — the branch gets a cleanup rebase before it is accepted

> "this branch probably deserves a cleanup rebase too when it's accepted too, it's full of conflicting
> docs and walked back changes"

*Settles:* 40 commits including ratified-then-refuted documents, a withdrawn store-layout design, and
two rounds of corrections to corrections. The `cleanup-rebase` skill exists for exactly this — meld the
fixups into what they repair, then prove the tree is unchanged. Do it at acceptance, not before: the
walked-back changes are still the evidence for why the current shape is what it is.

---

---

## Session of 2026-09-15/16 — the third diagnosis

Context: two independent review rounds returned 20 findings; `/triage-review` fired Step 0 on
three of them; `/diagnose-breakfix` ran for the third time on this branch. The report is
the third diagnosis of this branch, whose §11 carried this exchange verbatim.
**None of the twenty findings was applied.** Several of these entries exist because the agent put
a concrete case and [iw] refused the frame it assumed.

### The goal, which sits above every rule below

> "offline sync is a mess, most of that code never landed, **the goal is a working feature that
> makes sense**"

*Why it is first:* three diagnoses have looked for what this branch is measured against. It is
not the branch. **Most of the work never shipped, so the 49 commits are not a thing to preserve**,
and an argument of the form "that is too large a change for a branch this size" is not an
argument here. Also in `docs/offline-sync-goals.md`, where a reviewer will actually meet it.

### R11 — identity splits by operation: a download asks a server, state sync asks a person

> "Colliding server ids across different users are the same for downloads, HOWEVER a user might
> not have access to the same libraries as another user. Use a user token matching the downloading
> user, downloads don't need an active session so this is fine. For STATE syncing, the user MUST be
> the same, don't allow cross-user sync of watch state, instead defer sync for users who are not
> focused in the switcher."

*Also settled, as concrete cases marked "the same server":* two addresses with one `ServerId`;
two accounts with one `ServerId`; the same `ServerId` after a reinstall.
*Settles:* content identity and state identity are **different keys on one row**. Confirmed
against the worked case — A downloads it, B watches it: *"B's watch state syncs as B; the file
stays A's"*.
*The deferral clause is superseded by R16 if that proposal is adopted.*

### R12 — a broken `users.json` stops the sync subsystem, and the file must not break

> "If users.json is broken, don't load the sync subsystem. We should protect that file from
> corruption, if there is a way it can get corrupted we need to fix that."

*Provoked by:* being offered four options for what the catalog may conclude when the registry is
unreadable. All four assumed the subsystem runs in that state. It does not.
*Clarified,* when shown that not loading also confiscates offline playback on a launch where the
user may be offline:

> "Ahh that was directed at 'the critical config files for corrupted not it is offline'. My goal
> is for that file to never corrupt, ideally we have a backup we can restore if the power fails
> mid-write or something."

*What it dissolves:* the `''`-sentinel question that stopped this branch converging. Its only
producer is the withheld resolver, and in that state nothing loads, so there are no consumers.
*The route found, and not fixed:* `users.py:199-213` `save()` is write-temp-then-rename with **no
`fsync`** before `os.replace` and none on the directory — the rename is atomic, the contents are
not durable, which is the power-loss case named above. **Three separate things are wanted:**
flush+fsync then a directory fsync; a copy of the last *good* bytes; and a restore path that
prefers it to a failed parse. `users.json.unreadable-<ts>` is none of them — it rescues the
*corrupt* bytes after the fact.
*The distinction it rests on was already in this branch:* R10 drew "atomicity is not durability"
for the download-folder move and it was not carried to the one file whose corruption starts the
loss chain.

### R13 — a queued viewing carrying both ids is kept; a properly orphaned one is dropped

> "If it has a server id and user id keep it so someone deleting and re-creating a server
> connection doesn't lose their watch state. If it is properly orphaned, drop it."

> "For the case before, unsure if we can recover it? Ideally we rescue all the rows but if they
> get orphaned we can't do anything with it. I'm probably tripping over details I don't have
> proper context for."

*The closure, worked out in session and the part a code comment would lose:* for an entry written
before the actor columns existed, **the server half IS recoverable** — join `downloads` on
`item_id` and read `content_server_id`, the join `drop_unsyncable_playstate` already performs.
**The person half is not**, and R11 forbids the only workaround. So the drop is *forced, not
chosen*, and the rule and its reason agree after all.
*What is NOT settled by it:* such an entry keeps **local** value — `played_by_anyone`, the reaper,
"watched" in the UI. "Cannot be synced" and "the screen forgets you watched it" are different
losses and only the first is ruled.
*Supersedes* the sentence at `sync/db.py:403` attributed to [iw] — *"if it names an actor migrate
it, otherwise drop it"* — which appears in no register and is the rule the deleting code cites.

### R14 — auto-download belongs to the account that turned it on, and is **not** picker-gated

> "The account that turns it on gets it, auto downloads only run when the account that turned it
> on is selected in the picker, should be able to figure out which user turned it on based on the
> default connection settings which include if a server gets auto download."

**Then withdrawn in part**, when shown that R11's own reason makes the gate unnecessary:

> "You're right, the picker rule isn't needed for that. Watch status also doesn't require picker
> gating either afaik…"

*Settles:* per **account**, not per sign-in; **no picker gate**. The real constraint is resource
usage (G3), not liveness — and saying so matters, because written as a flat rule the next session
greps, finds *"downloads don't need an active session"*, and undoes it.
*Where it lives today:* **implemented 2026-09-19.** One `auto_download` list per profile in
`users.json`, of `(ServerId, UserId)` pairs, written by `users.set_auto_download` and read by
`auto.allowed_accounts`. `conf.py`'s `auto_download_servers` is legacy: adopted at load by
`users._adopt_legacy_auto_download`, cleared, and read by nothing.
*Where it lived before:* a comma-separated string of **login uuids** in the global settings file.
*Two migrations, not one:* uuid→account is mechanical; **global→per-profile is the real one**,
because `conf.Settings` has no per-profile dimension and `object_types` would not accept a map —
so it belongs in `users.json`. **It is derivable rather than lossy:** credential lists are
per-profile, so a ticked uuid already identifies exactly one profile.
*What it fixes:* today the allow-list is keyed on sign-ins, so where one server answers at two
addresses, unattended fetching is on, configured, and silently does nothing for the whole of a
trip.

### R15 — the `(server, nobody)` bucket is dropped when the copy is re-homed

> "Thinking out loud, it gets superseded by the current state for the server rehomed item per
> account, so it makes sense to drop when the re-home happens."

*And when shown the case where nothing supersedes it* — the copy was watched while its origin was
unknown and no named account ever watched it:

> "The view can't go anywhere, makes more sense in my mind to copy down the user-specific watch
> state that we can attribute. So it would be lost."

*Settles the half of R2 that was explicitly open.* R2's **reason** governs its **text** on the
person half (never re-home local-only state onto a user), and this settles the server half: the
anonymous bucket does not survive homing. The loss is deliberate and accepted.
*The fact that moved it:* the test defending the carry pins, four lines below its own assertion,
that a named account on that very server reads nothing there — so the mark was visible only to a
profile holding no account on the server it belongs to, which is the one that cannot report it.

### R16 — descope the deferred-sweep model — **RULED 2026-09-18, in the narrow form**

> "The more I think about it the whole queue while not selected concept is likely causing more
> complexity than of session pull/push would. The resource usage argument is the real reason. The
> multi-user support is for shared HTPCs, maybe we make offline watch state initial pull and sweep
> optional or scoped to whoever downloaded the file, and watches by other users sync up
> opportunistically and advance only but we don't do the expensive sweep unless it is turned on
> for the user?"

**Recorded as a proposal.** It supersedes R11's deferral clause if adopted.
*What it would dissolve:* the `_answered_servers` epoch race — Step 0's trigger T1, and the one
finding both reviewers reached independently — because with no cross-profile deferred sweep
obligation, `request_profile_sweep` stops having a job. And F46, whose narrowing is currently
blocked on a claim system.
*Two things it needs written down first, because both are the shape that has already drifted twice:*
1. "Scoped to whoever downloaded the file" is **mostly already recorded** — `downloads.server_uuid`
   is the enqueuing login and login→profile is derivable, so this does **not** need a new
   `requested_by` column. But it makes that column load-bearing as *a record of who asked*, while
   F48 and the review say resolving a *client* by it is the defect. One column, two uses; state the
   distinction or the next round removes it.
2. "Advance only" reintroduces a direction split that the direction rule removed for the sweep, and F47
   already records the socket not retreating while the sweep does. That would be three writers and
   two directions.
*And it lands on per-profile settings a second time* ("unless it is turned on for the user"), which
argues for solving that storage question once rather than twice.

**Ruled 2026-09-18. Source: [iw] selecting one of three written options — not their own
sentence, and recorded that way because the process gate says an inferred ruling may not be
used to dismiss a finding.** The option taken, verbatim as it was put:

> Push (draining a profile's own queued progress) stays universal and unchanged — it is bounded
> and is the only non-clone state. The expensive PULL/sweep becomes scoped to the account that
> downloaded the item, plus an opt-in per user. No cross-profile deferred sweep obligation, so
> `request_profile_sweep` and `_answered_servers` are deleted rather than repaired.

*So, settled:* the deferral clause of R11 is superseded; `request_profile_sweep` and
`_answered_servers` are **deleted**, which dissolves T1 (CR3+CX5) and F46 without an epoch or a
claim system.
*Amended when it was implemented, 2026-09-19 (D1), and the amendment is [iw]'s:* the **answered
set is re-keyed to the account rather than deleted**. Deleting it would also remove the reaper's
hold, which the reap ordering established and which the option above did not mean to reopen; the epoch
race dies with `request_profile_sweep` alone. Re-keyed, a profile switch invalidates the record
by construction, which is what made the method deletable in the first place.
*Settled against the proposal as spoken:* the **"advance only" writer is NOT adopted**. The
option taken says why — another person's watches reach their server through their own queue
drain, which already exists and runs in one direction — so note 2 above does not arise.
*Note 1 stands and is now load-bearing:* `downloads.server_uuid`/`requested_by` is a record of
*who asked*, while resolving a live *client* by it is the defect. One column, two uses, and the
plan says to state the distinction where the column is written.
*Measured for it, 2026-09-18:* the derivation R16 leans on is already possible — 14 of 14 rows
in [iw]'s live catalog resolve to an existing login. **But** two comments in the tree claim a
credential uuid can appear in two profiles, and neither is a measurement; the plan makes
settling that a precondition of the step rather than part of it.
*Delivered:* D1 and step 3, both complete.

---

### R17 — the ratified design is the work, and this branch is where it happens

**Ruled 2026-09-18. Source: [iw] selecting a written option**, answering the question which
three diagnoses left open and which nothing else could be scheduled behind.

*Settles:* the ratified design is executed rather than re-derived; it lands on
`sync-and-lifecycle-fixes`; the twenty findings are placed inside its steps rather than applied
as a pass of their own.
*Consequence recorded so it is not rediscovered:* the design's **step 1 is superseded, not
owed** — `e632f3f8` made the resolver load the registry itself, so the startup order it was
going to fix no longer decides anything. Measured 2026-09-18, not read off a document.
*Delivered:* all seven steps complete, verified 2026-09-20 against the tree rather than against
the plan's own headings — one of which still said "PART DONE" after its last three pieces had
landed. `docs/do-not-fix.md` carries its own status again.

### R18 — an orphaned queued viewing keeps its **local** half, and new entries carry the pair

**Ruled 2026-09-18**, answering the half R13 left open. *Source: [iw], selecting a written option
and adding the second clause in their own words:*

> "Keep it locally, no person, newer database entries should track remote user and server id so
> it can be re-homed"

*The case it was ruled on, measured the same day:* a queued viewing whose saved login no longer
resolves — you watched an episode offline, then removed that server from the app and added it
back — is **deleted** today by `sync/db.py:_migrate_playstate_actors`, taking the local watched
mark with it. The file is still on disk.

*Settles:*
- The row is **kept**. Its server half is recovered from the item's own download row — the join
  `drop_unsyncable_playstate` already performs — and its person half stays empty.
- What it is worth is **local only**: "watched" on screen, `played_by_anyone`, the reaper's grace
  period. It is never pushed and it is never an answer to a *named* server asking who watched
  this. R2's person half is untouched: nothing is re-homed onto a name.
- **Entries written from now on carry `(ServerId, UserId)`**, which is what makes R13's
  delete-and-re-create case lossless rather than merely survivable — the pair is already stamped
  by the migration for every row whose login still resolves (measured: 1 of 1 in the probe), so
  this clause is mostly about never regressing it.
- R15 is unchanged and still governs the other direction: this bucket is **dropped** when the
  copy is re-homed, because the current per-account state supersedes it.
- **Not taken:** the reading that a named account on that server *should* see the anonymous
  mark. The test pinning that it reads nothing there stands.

*Supersedes, finally,* the sentence at `sync/db.py:403` — *"if it names an actor migrate it,
otherwise drop it"* — which is in no register, and which R13 already flagged and this now
replaces outright. Correcting that comment is part of the work, not an aside: a classification
ranks a behaviour and never a falsehood.
*Delivered:* step 5, complete.

> **ERRATUM — 2026-09-19, triage round `2026-09-19-sync-lifecycle-branch`.** The ruling's
> **property stands and is now delivered**; the **mechanism this entry describes does not**,
> and R13's *Supersedes* line above shares its stale citation.
>
> *What was wrong:* "the row is **kept**", in `pending_playstate`, with an empty person half.
> The three readers this entry names for it — "watched" on screen, `played_by_anyone`, the
> reaper's grace period — all read `item_userdata`, and nothing outside `sync/db.py` reads
> `pending_playstate` at all. So the kept row delivered none of the three, and
> `manager._open_and_run`'s `drop_unsyncable_playstate()` deleted an orphan row's entry on
> every launch, three statements after the migration logged the keep.
>
> *What replaces it:* the viewing is merged into `item_userdata` under `(item, server, @none)`
> — the key `_move_local_userdata` already uses for state recorded before a row knew its
> server — and the queue row is retired. Everything else here is unchanged: the server half is
> still recovered from the item's own download row, no person is ever named, nothing is ever
> pushed, and R15 still governs the other direction.
>
> *Was left open here and is now closed by **R32** (2026-09-20):* R15 says this bucket is
> **dropped** when the copy is re-homed, while `home_content_server` **merged** it onto
> `(server, @none)`. R15 stands as written and the code now drops; the merge and the argument
> its docstring made are gone.
>
> *Citation correction:* `sync/db.py:403` holds `STORE_DIR` today. The superseded sentence is
> quoted at `sync/db.py:1003`, inside `_migrate_playstate_actors`'s docstring, where it is
> already framed as history. R13 above cites the same stale line.

---

## Process rulings from the same session

### P8 — the finding sort runs even when the diagnosis is triggered

> "Yeah that makes sense. I think the epistemic honesty about *what the findings are* is probably
> load-bearing with large finding lists or finding lists from multiple sources in particular."

*Provoked by:* `/triage-review` Step 0 firing and, as written, pre-empting its own sort. [iw]
overrode it, and the sort then produced things the diagnosis could not have got for itself: one
High finding shown invalid before it could be inherited as fact, site counts that reclassified
three findings framed as one-liners, the merges that made a cluster legible as one missing rule,
and the concrete cases that the owner questions were built from.
*Acted on:* `triage-review`'s Step 0 now runs Steps 1 and 2 and hands the sort to the diagnosis as
evidence, while Steps 3-5 wait so the owner is asked once, by the diagnosis, after its deposition.
*The cost that decided the split:* asking during the triage broke the diagnosis's
deposition-before-owner ordering, and the deposition in this run was written with two of the
owner's answers already in hand.


### R19 — `requested_by` is the **account** that asked, stored as `(ServerId, UserId)`

*Provoked by:* step 3 saying "`requested_by` written at enqueue" while R16 note 1 says a new
column is **not** needed, because `downloads.server_uuid` already names the enqueuing login and
login → profile is derivable. Put to [iw] as an ambiguity rather than resolved by inference.

[iw]'s first answer was a hypothesis, not a ruling — *"Maybe that was for recording the user who
requested the item?"* — so it was worked out through three cases that could be marked wrong,
then ruled on the corrected reading.

> **Ruled:** the account pair, at enqueue. `downloads` gains the same `(server_id, user_id)`
> shape `pending_playstate` already carries under R18. `server_uuid` stays as the enqueuing login
> and **stops being an identity**.

*The case that decided it is R14's own bug:* one account reachable at two addresses is two login
uuids and one `(ServerId, UserId)`. Keyed on the login, unattended fetching is "on, configured,
and silently does nothing for the whole of a trip"; keyed on the account it works. A login key
also loses the record the first time a server connection is deleted and re-created, which the
round already named as the falsifier for the derive-it-instead option.

*Measured before ruling (2026-09-19, live catalog via `VACUUM INTO`):* 14/14 download rows
resolve to a saved login, all to one profile, and 14/14 manifests name a `ServerId` agreeing with
their login's `Id`. So the backfill is available **today** — which is what makes recording it now
a durability change rather than a rescue.

*Reconciles:* R16 note 1 is about *scoping*, which genuinely needs no new column today; R19 is
about *durability and the account key*, which R14 needs and the derivation cannot promise.
*Where the work is:* step 3.

### R20 — the per-user sweep opt-in gets a per-profile toggle in Settings — **SUPERSEDED by R21**

> **Ruled:** "A toggle in Settings, per profile."

*Provoked by:* R16's "unless it is turned on for the user" needing a surface, and the storage
landing in `users.json` rather than `conf.Settings` (R14: no per-profile dimension, and
`object_types` would not take a map).

*What it commits to, stated because each has a cost the ruling did not price:* a new
translatable string plus its `#.` translator context in `base.pot` (`docs/i18n.md` 9); a tab
decision under `docs/settings-curation.md`; and a settings control that reads and writes
`users.json` rather than the config, which the existing settings machinery does not do.
*Not ruled:* which tab, and whether the toggle lands inside step 3 or in its own pass after it.


### R21 — the sweep opt-in needs **no UI**: auto-download already is the per-user signal

**Supersedes R20 within hours of it**, which is worth recording rather than
tidying away: R20 was answered from the three options offered, and the question
never asked whether the setting had to exist at all.

> [iw], verbatim: *"Does the user download need UI at all? Auto download is
> enabled per server and it already keyed to user profile by construction."*

*Settled:* there is no new setting and no new control. The expensive pull/sweep
runs for the union of two things the tree already records — **the account that
downloaded the item** (R16's own scoping, durable under R19) and **the servers
this profile has auto-download enabled for**. A ticked server is in that
profile's credential list, so `auto_download_servers` identifies exactly one
profile *by construction*; that is the same fact R14 leans on to call its
migration derivable rather than lossy.

*Checked against R16's purpose before adopting, as four cases:* a profile that
downloaded manually with auto-download off is still swept (the downloader half);
a profile with neither is not swept, which is the resource saving R16 wanted;
another person's watches still reach their server through their own queue drain,
because push stays universal; and a profile with auto-download on for a server it
has not downloaded from yet is swept, which is the ongoing interest the opt-in was
for.

*What it dissolves,* and this is why it is the better answer: the new translatable
string and its `#.` context, the settings-tab decision, and — the ugliest part —
a settings control that would have had to read and write `users.json` instead of
the config, which the existing machinery does not do.

*What still stands from R14:* `auto_download_servers` itself still moves from the
global config to per-profile storage in `users.json`. That migration is the work;
the **existing** per-server auto-download UI is what drives it, and it gains no
new string.
*Where the work is:* step 3. **The storage move landed 2026-09-19** and cost no new
string, as ruled: the Servers tab's existing checkbox writes the registry through
three gateway methods. The union rule R21 states is what the *sweep* scoping needs
and is still ahead, in D1 -- this half only makes its second term exist.



### R22 — re-adding a server keeps its auto-download setting

Put to [iw] as a **derived** consequence of R14's account keying, with the derivation shown and
the invitation to mark it wrong: keyed on the login uuid, removing a server and adding it back
turned unattended downloading off by accident (the re-added login got a fresh uuid); keyed on the
account it stays on.

> [iw], verbatim: *"The re-add keeping the setting is fine"*

*Settles:* deleting a connection is not turning the setting off. **Ruled, not inferred** — which
matters because the entry it was derived from (account-keyed state survives connection churn, the
`_answered_servers` re-key) is a different question, and a later round finding this by grepping
for "derived" would have had a licence to undo it.
*Not affected either way:* re-authentication, which `open_reauth` deliberately routes through the
**same** uuid so a server's downloads survive.
*Checked by:* `tests/test_auto_download_accounts.py:test_a_login_removed_and_added_again_keeps_the_setting`.


### R23 — step 4's three open questions, answered 2026-09-19

Put as three decisions the session had made or deferred, each with its cost stated.

**The art path move:**

> [iw], verbatim: *"Moving is fine, we should just make it transactional so it doesn't strand
> files."*

*Settles:* the posters already on disk **move** to
`<root>/server/playlist/<content_server_id>/<playlist_id>/` rather than the reader learning two
locations — and the move owes crash-safety, not just correctness. So: `os.replace` per file
(atomic within a filesystem), and the pass is **unconditional and re-run on every open**, which
is the shape `_migrate`'s `origin` backfill uses. Interrupted, a poster is at one path or the
other and never neither, and the next open finishes the job.

**The offline library's playlist routing:**

> [iw], verbatim: *"The playlist situation is *probably* fine for now, but if we're redesigning
> that database table anyways it might make sense to fix."*

*Settles:* do it now, while the schema change is unreleased — a lean rather than a demand, and
recorded that way. The cost is that the offline DTO's `Id` becomes a scoped synthetic one, which
reaches routing, the tiles and the delete gesture.

**The downgrade cost of the table rebuild:**

> [iw], verbatim: *"The downgrade cost is fine."*

*Settles:* an older build opening a migrated catalog no longer has `playlists.playlist_id`
unique, so its own `INSERT OR REPLACE` inserts where it used to replace and it would list a
playlist twice. **Accepted.** `_migrate`'s corrected docstring is where that is written down, and
it stays written: the next reader of that promise needs the cost, not just the fact.


### R24 — the `userdata_json` blob migration is dropped, and the loss is accepted

The ratified design said to drop it (R2, *"agree, and go further"*: attributing a blob to
the downloading login is the same wrong attribution R2 rejects). **The code disagreed, in a
reasoned docstring** — `_backfill_item_userdata`: *"It is a read rather than a guess, and the
alternative discards real user data on every install."* Both readings are true, and the cost falls
on real rows, so it was put to [iw] with the cost named: 14 rows on their own catalog lose their
local watched/position state, restored by the first sweep for servers still connected and
**permanently lost for a server already removed**.

> [iw], verbatim: *"I think it's fine for a one-time watch position loss for already deleted
> servers on a one-time migration where we basically recohered the offline sync feature."*

*Settles:* drop the migration. The loss is bounded to a removed server's snapshot, it happens
once, and F43 already says such a snapshot never syncs anyway.
*What has to go with it:* `_backfill_item_userdata`'s docstring argues the opposite case and must
not simply be deleted quietly — the argument it makes was right about the trade and wrong about
which side to take, and the next reader needs the ruling rather than a silence where the function
was. Done: a comment stands where the function did, carrying its argument verbatim.
*Measured while implementing it, and it makes the cost smaller than stated when the question was
asked:* **nothing is deleted.** No production code reads `userdata_json` at all, and two writers
still fill it at download time — which is what keeps an *older* build showing watched state if it
opens this catalog. So the loss is "this build stops interpreting it", not "the value is gone": for
a server still connected the first sweep restores it within the minute, and only a server already
removed has no second source. The correction is recorded because the ruling was given against the
larger cost.
*Where the work is:* step 5.


### R25 — an unattributable download gets its own category in the browser

Step 5 takes `_content_clause`'s NULL branch out: a download whose manifest named no server stops
answering *named* servers, because answering them is how one server's tile ticks for another's
film and how the wrong local copy substitutes for a stream. The branch existed for a real reason
— such a row stayed visible and deletable — so removing it needed somewhere for those rows to go,
and that was put to [iw] rather than decided.

> [iw], verbatim: *"yes the items should probably be displayed under an \"orphaned items\"
> category in the browser."*

*Settles:* the offline library gains an **Orphaned Items** library, offered only when there is one
(no empty category on an ordinary install). Such a row is listed there and **nowhere else** — an
item in two categories at once is worse than an odd one — and it stays playable offline, since
that path asks with `ANY_SERVER`.
*One new translatable string*, with its `#.` context: "orphaned" in the sense of *has no parent
server*, not *damaged*.
*What it costs:* online, an orphan no longer substitutes for a stream. It streams instead — the
same fallback a size mismatch takes (R-B).

### R26 — series and season art share one cache on purpose

Step 7 dropped the dead `server_id` parameter from `_download_series_art`, which made visible that
this cache is **not** scoped by content server while `playlist_art_dir` is — so two servers can
share one cached series poster. Raised as an open collision of the same shape step 4 fixed for
playlists.

> [iw], verbatim: *"series and season art aren't content-scoped -- this is probably fine, if the
> ids collide it means it's the same folder path"*

*Settles:* it stays unscoped, and the reason is that the two ids are **keyed on different things**.
A playlist id hashes its *name*, so two unrelated playlists collide on name alone — measured, 0.3.
A series id is derived from the media path, so two servers handing out the same one are describing
the same folder, and one poster for it is correct rather than a collision.
*The failure it leaves:* two servers serving *different* content at the same library path would
share a poster. Nothing suggests that configuration exists, and the cost is a wrong thumbnail.
*Why it is written down rather than left implicit:* the next reader finds a scoped art path beside
an unscoped one and has every reason to "fix" the second. The note at `_download_series_art` says
not to, and says why.

## Session of 2026-09-19 — the triage round on this branch

**How these were captured, and it is not the same as the entries above.** They come from the
planning half of `/triage-review` run `2026-09-19-sync-lifecycle-branch`, where the questions were
multiple choice. **The option text is the agent's; only the choice is [iw]'s** — so an option
quoted below is not [iw] composing a sentence, it is [iw] selecting one, and a reader must not
weigh it as if it were their own wording. Where they typed instead of selecting, it says so and
the words are theirs. The full record — every question, every answer, and the plan they produced —
is the triage run directory `triage/2026-09-19-sync-lifecycle-branch` under this repository's git
common directory. **It is not in the working tree**, so it is not citable as a document here and
will not survive a fresh clone; what has to outlive it is in this entry.

### R27 — what "critical" means on this branch: a read is not, a write is

The one entry here in [iw]'s own words. Asked which of four concrete cases had to be closed before
the branch merges, they took three and then corrected the fourth rather than taking or leaving it:

> "Accounts seeing another's viewing isn't necessarily critical, but it corrupting someone else's
> viewing is"

*Settles:* severity on this subsystem is about the **direction** of the access. One account being
shown another's state is a defect and not a blocker; one account's state being **written** from
another's is. It is what ranked the frozen-snapshot leak below the playlist-ownership write in the
round's plan, and both were fixed either way.
*The three cases taken as given:* a catalog that eats a download, a registry that loses saved
logins, a ruling that shipped without its behaviour.

### R28 — R18's local half lives in `item_userdata`, not in the replay queue

*Selected:* **"In `item_userdata`, where the readers look."**

*Provoked by:* R18 kept an orphaned queued viewing so its local watched mark would survive, and
named three readers for it — "watched" on screen, `played_by_anyone`, the reaper's grace period.
All three read `item_userdata`. Nothing outside `sync/db.py` reads `pending_playstate` at all, so
the kept row delivered none of them, and `_open_and_run`'s `drop_unsyncable_playstate()` then
deleted an orphan row's entry on every launch — three statements after the migration logged that it
had kept it.

*Settles:* the mark merges into `item_userdata` under `(item, server, @none)`, the key
`_move_local_userdata` already uses for state recorded before a row knew its server, and the queue
row is retired. R18's **property** stands and is now delivered; the **table** it named does not.
See R18's erratum.
*Was named rather than decided here, and is now **R32** (2026-09-20):* R15 says this bucket is
dropped when the copy is re-homed, while `home_content_server` merged it. R15 stands; the drop is
keyed on `(@none, @none)`, so **the mark this ruling puts under `(server, @none)` is not touched
by it**.

### R29 — a playlist is its id and its **items'** content server

*Selected:* **"(id, content server); unknown matches only the unscoped row."**

*Provoked by:* `enqueue` keyed the playlist row on `content_id_for(login)` while every member row
carried the item's own `ServerId`, and `content_id_for` answers `ANY_SERVER` when the registry
lookup *raises*. A read takes that as "every server" while all three playlist writers fold it to
the NULL row. Separately, `_repair_playlist_member_scopes` correlated on `playlist_id` alone and
ran on every open — and a playlist id hashes its *name* (R26), so two servers holding an "Example
Playlist" is ordinary rather than exotic.

*Settles:* a playlist is `(playlist_id, content server)`, the server being the one its items came
from. An unknown scope matches **only** the NULL row, never every server. **Nothing moves a row
between scopes on a guess** — so the repair acts only where the id names exactly one playlist, and
reports the rest rather than deciding them.
*What it does not touch, and a later reader should not "finish":* `ANY_SERVER` still means a
genuinely unscoped read for a caller with no server to name — the offline browser listing every
playlist, and R25's offline playback. `test_the_unscoped_sentinel_reaches_none_of_these_as_a_parameter`
pins that. The fix was to stop the sentinel reaching the *write* path at source, not to redefine it.

### R30 — Retry reconnects; it does not switch

*Selected:* **"Retry reconnects; it does not switch."**

*Provoked by:* `reconnect_server` always ended in `set_source(source, server_uuid=uuid)`, which
moves the browsed server and resets the nav stack. The top-bar switcher passes an `on_success`
carrying the switch's handover — leave the SyncPlay group on the server being left, remember the
new one — and both Retry buttons passed none. So Retry performed a switch without its handover.

*Settles:* the switcher switches; Retry gets a server back and leaves you where you are.
*Why the one-line version is wrong:* `_switch_server` guards `uuid == self.server` before its
handover and `reconnect_server` has none, and Retry is offered for the server being browsed too —
so moving the handover into `reconnect_server` would `sync_leave` the server just reconnected.

### R31 — a snapshot is not a viewing, and nobody inherits one

*Selected:* **"Unwatched — nobody inherits a snapshot."**

*Provoked by:* `_add_row` stores the server's DTO verbatim, so `item_json` carries the watched flag
and resume position of the account that asked for the download. `_item_from_row` overlaid the
browsing person's own state only `if userdata:`, so a profile with nothing recorded was shown the
downloader's and could resume from it. `_userdata_for`'s docstring asserted the opposite — empty
"is what the snapshot already says" — and that assumption was the defect.

*Settles:* the overlay is unconditional. An actor with nothing recorded reads as never-opened.
*The cost, stated when the question was asked and taken anyway:* the account that downloaded it
also reads unwatched until they play it on this machine. It has its own test so it is not later
read as a regression.

### R32 — the anonymous bucket is dropped at the homing, and the single-user exception is declined

*Provoked by:* the question R18's erratum and R28 both left standing, and neither could decide:
R15 says the `(@none, @none)` bucket is **dropped** when a copy is re-homed, while
`home_content_server` **merged** it onto `(server, @none)` and its docstring argued why. The two
readings had never met while R18's half lived in `pending_playstate`; R28 moved it into
`item_userdata`, where they do. The code merged, and nothing ruled it.

> "For anonymous watched-state, it should NOT be re-merged. (If there is only one user I guess
> re-merging would actually be find, that's a drive-by design change.)"

*Settles:* **R15 as written, and the code now does it.** `_drop_local_userdata` deletes
`(item, @none, @none)` at both homing sites — `home_content_server` and the migration's
`_backfill_content_server_id` — and carries nothing.
*The argument the merge had, and why it does not survive:* both keys are anonymous, so the move
attributes nothing to a *person*. True, and not the objection. `(@none, @none)` is one bucket for
the whole machine, while `(server, @none)` is one per server and is read by every local profile
that holds **no** account there. So the carry took whatever the bucket held — including marks a
*named* account left before the row knew its server, which that account never reads back either
way — and handed them to the profiles that cannot be named, at the one moment the machine starts
being able to tell them apart.
*The exception, considered and declined:* on a single-profile machine the bucket is certainly that
person's and the merge would be safe. Making the behaviour conditional on the profile count is the
drive-by design change, so it is not taken. One rule, both shapes of machine.
*What it costs, measured before it was applied:* on the live pre-branch catalog, nothing at all —
`item_userdata` does not exist in a shipped catalog, so the migration's homing of all 14 rows has
no bucket to drop (2026-09-20, `VACUUM INTO` probe). The loss is confined to what R15 already ruled
on: a copy watched while its origin was unknown, in *this* build, that is later re-homed.
*What it does not touch:* R28's `(server, @none)` mark and every named account's state. The drop is
keyed on the sentinel pair, not on the item — `DELETE ... WHERE item_id=?` passes every
"it is gone" assertion and takes both with it, so two tests exist for that one mutation.

---

**Most of these are consequences of four commitments**, identified by [iw] on 2026-09-20 after
thirty-two of them had been made one at a time: don't lose the user's data; don't lose or corrupt
their watch state, and where those conflict lose it; when information is incomplete do the most
reasonable thing with what is there; and a thing belongs to the account that did it rather than to
the connection it arrived on. They are written up, with the reason the second one resolves the way
it does, in `docs/offline-sync.md` section 0.

Four entries do not reduce to them and are worth knowing as exceptions: R8 (security), R30 (an
action does what it says and no more), R1 and R16 (do not over-specify; cost). The P-series below
is process rather than product and never did.

*Why it matters that this was written down late:* the tiebreak inside the second principle — lose
the state rather than misattribute it — was rediscovered in R2, R15, R24, R31 and R32, five
separate rounds asking one question. A rule that keeps coming back is usually a missing level
above it.

- Every entry is from one session. A log with one day in it is an anecdote.
- Nothing here has a check. R2, R3 and R6 are behavioural and could each carry a test that fails when
  the rule stops holding; none does yet, and until they do this file has the same failure mode as
  every other prose document here — believed, and therefore able to suppress scrutiny.
- R1's lesson ("a non-requirement read as a requirement") and P5 are process rules with no home in
  any reference doc. They are the entries most likely to be lost.
