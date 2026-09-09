# Break-fix rot postmortem — jellyfin-mpv-shim v3.0.0

**Range:** `v3.0.0pre14` (`a903c16d`, 2026-08-28) .. `v3.0.0` (`9970b2dc`, 2026-09-07T17:36:17-04:00).
**Verified against the repo, read-only.** 238 commits in range; 194 non-Weblate;
**187 non-Weblate non-merge** (the other 7 are merge commits, which carry no
diff of their own and are excluded from every rate below). All "Translated
using Weblate" commits are excluded everywhere — they are bot noise and would
halve every ratio for free.

Ten days, 187 commits, peaking at **53 in one day** (2026-08-31):

| date | 08-29 | 08-30 | 08-31 | 09-01 | 09-02 | 09-03 | 09-04 | 09-05 | 09-06 | 09-07 |
|---|---|---|---|---|---|---|---|---|---|---|
| commits | 5 | 1 | **53** | 5 | 23 | 19 | 15 | 27 | 14 | 25 |

---

## 1. The rot, measured — and what the measurement cannot see

### Method

An SZZ-lite pass, script kept at
`…/scratchpad/szz.py`, output at `…/scratchpad/szz.json`:

1. For each of the 187 commits, `git diff --unified=0 <c>^ <c>`.
2. For every hunk that **deletes or modifies** at least one line of a `.py` or
   `.lua` file outside `docs/`, `git blame -l -L <start>,<end> <c>^ -- <file>`.
3. A commit is a **self-fix** if any blamed SHA is itself inside
   `v3.0.0pre14..v3.0.0` — i.e. it changed a line that this same release window
   wrote.
4. Then subtract the four **prose-compression** commits, proved AST-identical
   (docstrings/comments stripped, `ast.dump` compared) so they cannot be
   defects: `9b7355e5`, `e8286e0f`, `980c1f34`, `1f0aa4f4`.

> One methodological note, because it changed the answer: the first run reported
> **0.0%**. `git blame --no-color` is an *ambiguous option* (`--no-color-lines`
> / `--no-color-by-age`), every blame call errored to empty, and the script
> happily reported a clean release. It looked exactly like a real result. The
> figures below are from the corrected run. This is the repo's own
> "gate on the value, not the marker" failure, reproduced inside its own
> postmortem.

### Numbers — three instruments, three units

Standing rule here: **never quote one figure as "the" rot rate** — different
instruments give different units and the variance *is* the finding.

| instrument | numerator | denominator | rate |
|---|---|---|---|
| **A — any in-range blame** (loosest) | 56 self-fixes | 127 commits that modify existing code | **44.1%** |
| **A′ — same, over all commits** | 56 | 187 non-merge non-Weblate | **29.9%** |
| **B — majority in-range** (>50% of blamed lines written in-range) | 40 | 127 | **31.5%** |
| **C — shipping code only** (`jellyfin_mpv_shim/**`) | 48 | 107 | **44.9%** |

All four exclude the prose commits. So: **roughly 30–45% of the release-window
commits that touched existing code were repairing code this same window had
written.** For orientation only,
that sits at the top of the repo's own recorded 8–34% / 19% / 38–92% band —
which is itself three instruments in three units, so it is a rhyme, not a
comparison.

### Why this is a floor, not a ceiling

`git blame` can only see a fix that **deletes or edits** the defective line.
The normal shape of a correctness fix in this tree is *adding a guard*, and
blame is blind to it.

Measured: **22** of the 187 commits change `.py`/`.lua` and delete **nothing**
(pure additions). **16 of those add to a file an earlier in-range commit had
already touched** — the invisible zone. At least four are self-evidently
self-fixes by their own subject lines:

- `b49eb858` "Fix two traps in the hover-cost work: a zero divide and a stale
  import" — the hover-cost work is `e6847189`, in range, three commits earlier.
- `559fa2ac` "Never write over a config we could not read" — on top of
  `7b75a670`, in range, one day earlier.
- `9fd940bb` "Stop a theme gradient and Custom OSC painting over a comic page".
- `686282a9` "Close F41: a yield overtaken by enter_browse, pinned by a test".

Adding just those four moves instrument A to 60/127 = **47.2%**. The true
figure is somewhere above the table and cannot be recovered by blame at all.

### The rate over time — this is the part that matters

CLAUDE.md: *"the stop signal is not 'a round found nothing', it is 'the findings
are now in code that predates this session'."* The opposite happened.

| date | commits touching existing code | self-fixes | rate |
|---|---|---|---|
| 08-29 | 3 | 0 | 0.0% |
| 08-31 | 33 | 9 | 27.3% |
| 09-01 | 5 | 1 | 20.0% |
| 09-02 | 14 | 11 | **78.6%** |
| 09-03 | 15 | 11 | **73.3%** |
| 09-04 | 13 | 3 | 23.1% |
| 09-05 | 16 | 7 | 43.8% |
| 09-06 | 11 | 6 | 54.5% |
| 09-07 | 17 | 8 | 47.1% |

It starts at zero, spikes to ~75% on 09-02/03 (the font and sync clusters), and
**never returns to the opening level**. The last four days run at ~44%. By
CLAUDE.md's own trigger — *"when a round's findings land mostly in code that
earlier rounds fixed, stop applying them one at a time"* — that trigger fired on
2026-09-02 and the release shipped five days later without it being honoured.

### The generators

Commits most often blamed by a *later* in-range commit:

| later fixes | lines | inducing commit |
|---|---|---|
| 8 | 155 | `c0b26b46` Stop the download store deleting what it cannot identify |
| 5 | 81 | `f7e363cb` Draw emoji in colour in baked text |
| 5 | 69 | `cb6708b8` Collapse the sync manager's duplicated rules into one site each |
| 5 | 40 | `f00b477d` Never let a playlist's claim outlive the row it is a claim on |
| 5 | 37 | `aecd940e` Give stars and ticks a font of their own, not Arial's tofu (#713) |
| 4 | 34 | `25dd4317` Let a symbol win when there is nothing else in the string |
| 4 | 12 | `65b34eb1` player: don't send the access token to a third-party stream host |
| 3 | 70 | `91cc2084` One "MPV UI" instead of two, and never a fallback with no controls |
| 3 | 57 | `a0888e35` Let mpv's own OSC drive our seek previews where it can (#724 follow-on) |
| 3 | 56 | `3b7bfab9` Repair the previous four commits, where the review found most of the defects |

Two things to read off this. First, `3b7bfab9` — *a repair commit that is itself
one of the top ten generators*. Second, `cb6708b8` — the cross-cutting
"collapse the duplicated rules" repair — is the third-largest generator, which
is the CLAUDE.md caveat made concrete: *a cross-cutting repair is not
automatically the safer choice; it is a bigger guess and it fails wider.*

### Two controls, both of which matter

**It was not a shortage of tests.** Diffstat across the window:

| area | files | +lines | −lines |
|---|---|---|---|
| production (`jellyfin_mpv_shim/**.py`, `**.lua`) | 61 | 5,843 | 1,183 |
| `tests/` | 256 | 19,273 | 355 |
| `tools/` | 11 | 1,227 | 17 |
| `docs/` | 15 | 3,226 | 21 |

**3.3 lines of test per line of production change**, and four issues still
landed within a day of the tag. The gap is not test volume, it is *what the
tests enumerate* — which is CLAUDE.md's "widen what is enumerated rather than
how carefully the diff is read", stated as a ratio.

**The current branch is not continuing the loop.** Running the same SZZ pass
over `v3.0.0..HEAD` (28 non-merge, non-Weblate commits on `enrich-e2e-tests`):
**zero** touch a `.py`/`.lua` line the release window wrote. It is additive
coverage and instrumentation, not more patching. That is the healthy state to
preserve, and it is the argument for spending 3.1.0 on instruments rather than
on another fix round.

---

## 2. Clusters, by the rule violated

### C1 — "resolve the remappable key at use; never write the literal" — **OPEN, no lint**

The rule is written down at `player.py:5087-5090`:

> `select_key()` per press, not a `_NAV_KEYPRESS` entry: that dict is a class
> attribute built at import, so a value read there would be whatever the setting
> said when `player` was first imported.

Sites, verified at HEAD (2026-09-08):

| # | site | applies the rule? |
|---|---|---|
| 1 | `player.py:5093` `self._NAV_KEYPRESS.get(action) or conf.select_key()` | **yes** — the model |
| 2 | `player.py:4949` `"back": "ESC"` in `_NAV_KEYPRESS`, while `conf.py:455 kb_menu_esc` is equally remappable | **no** (risk map R1) |
| 3 | `mpvtk_browser/app.py:1875` `return tuple(self.SHELL_VOLUME_KEYS) + ("m", "SPACE")` while `conf.py:461 kb_pause = "space"` | **no** (R2) |
| 4 | `mpvtk/renderer.lua:5863` `PHUD_SUMMON_KEYS = { …, 'ENTER' }`, force-bound at `:6001-6006` with only `key ~= phud_wake_key()` skipped | **no** (R3) |
| 5 | `mpvtk/renderer.lua:5919-5923` `phud_skip_bind` — resolves `phud_wake_key()` on line 5919, then hardcodes `remove_key_binding('mpvtk_summon_ENTER')` and `add_forced_key_binding('ENTER', 'mpvtk_skip_enter', …)` on the next two | **no** (R10 / do-not-fix **F35**) |
| 6 | `mpvtk/renderer.lua:5988-5992` `phud_bind_wake` uses `phud_wake_key()` | **yes** |
| 7 | `mpvtk/renderer.lua:4735` `return k[3] and (state.select_key or 'ENTER') or k[1]` | **yes** |

**4 of 7 sites correct.** R10 sits *one screen* from R3, inside a function that
resolves the very setting it then hardcodes — and per `docs/RISK_MAP_2026-09.md`
it was found by writing the predicate down, after four surveys and a full manual
session walked past it.

**Concrete failure (site 5), CONFIRMED by reading:** set **`hud_wake_key`** to
`a`. Play something with a skippable intro. `phud_skip_bind` fires;
`phud_wake_key()` returns `'a'`, so `:5919`'s `if phud_wake_key() == 'ENTER'`
is false and `mpvtk_wake` is **not** removed; `:5923` force-binds `ENTER` to Skip
regardless. ENTER now skips the intro even though the user moved the key off it,
and `a` still summons. Two keys, one job, neither doing the whole of it.

Site 5 is **already logged**, at `docs/do-not-fix.md` §4 as **F35**, with a
deliberate deferral: *"it is a `hud_wake_key` bug, and it wants a decision about
whether the idle Skip offer follows the wake key or `ui_select_key`."* So this is
not an unnoticed defect — it is a known one whose *cluster membership* nobody
counted. That is the point: F35 was filed as one site.

**The triage that deferred this is wrong, on two counts — and I checked the
tables, not the prose.**

`docs/RISK_MAP_2026-09.md:306-309` puts R1/R2/R3/R10 in **Tier 3 — config-file
edit only; defer past 3.0.0**, justified by *"None of these appear in
`TAB_SECTIONS` at all, so reaching them means hand-editing `conf.json`."*

1. **Wrong setting.** R3 and R10 are governed by `phud_wake_key()`, which reads
   `state.phud.wake_key` ← the HUD opts ← **`hud_wake_key`** (`conf.py:719`).
   `ui_select_key` (`conf.py:732`) is a *different* setting that happens to share
   the default `"ENTER"`; it reaches the renderer as `state.select_key`
   (`renderer.lua:4735`, site 7, which is correct). The risk map's R3 row cites
   `conf.py:730`'s `ui_select_key` comment as its spec and conflates the two.
2. **Wrong tier.** `hud_wake_key` is at `mpvtk_browser/config.py:238`, inside
   `TAB_SECTIONS["playback"]` → group `_("Player Controls")` — which is **not** in
   `ADVANCED_GROUPS` (`config.py:378-381` = `{"Advanced", "Download Tuning"}`).
   By the map's own measurement rule that makes it **Tier 2: one non-advanced
   setting, on the normal Settings screens.**

`ui_select_key` also moved after the map was measured (at `1f0aa4f4`):
`c3e98a33` gave it a label (`config.py:663`), a note (`:744`) and search terms
(`:1162`), rationale at `config.py:167-173` — *"It falls through to Advanced,
which is a real editable row — searched like any other."* So it too is no longer
conf.json-only. Only `kb_*` still is (`grep '"kb_' config.py` → nothing), so
**R1 and R2 stay Tier 3; R3 and R10 are Tier 2.**

**The consequence.** The scoping rule's own conclusion — *"the key-literal lint
… every one of them needs a config-file edit to reach. By this rule it is
post-3.0.0 work"* — rests on a mis-attributed setting and a table the same
release was still editing. The cheapest instrument in the tree was demoted by a
measurement error, not by a judgement.

**Cheap enforceable repair — removes an authority, does not add a guard.** A
lint in the shape of the two that already work here: for every `conf.py` key
whose value *is a key name* (`kb_*`, `ui_select_key`, `hud_wake_key`), flag
every string literal in `player.py`, `mpvtk_browser/app.py` and `renderer.lua`
that equals one of their default values, and require a declaration. This is
`tools/audit_video_predicate.py`'s exact structure (declare-the-site, no
correctness judgement) applied to a second rule; it costs ~150 lines and one
`tests/test_no_frozen_key_literals.py`. It would have found R10 by itself, which
is the whole argument.

### C2 — "one owner for state the renderer holds" — **PARTLY REPAIRED, on release day**

`67a4f352` landed **44 minutes before the release tag** (2026-09-07 16:52 vs
17:36). Its own message names the shape:

> The same rule applied to one of the two transitions it names — the shape this
> tree keeps producing.

Mechanism: `mpvtk/app.py`'s `claim_keys` (`:1045-1054`) and `set_picture_pan`
(`:1074-1082`) are compare-and-skip caches mirroring state owned by
`renderer.lua`. `set_active` forgot them to `()` / `None`, which is a true
statement about `mpvtk-active no` (it calls `keyclaim.set({})`) and a **false**
one about `yes` (which drops nothing). Net effect: play music → stop → play a
video, and SPACE and `m` are dead for that video while `p` works.

The repair added a sentinel (`app.py:30-40` `_Unknown`, used at `:985-986`) —
i.e. **a guard, not the removal of an authority**. Two mirrors remain; a third
call site that pushes without going through `set_active` would reintroduce it.
The one mitigating fact, checked: `mpvtk_browser/ui.py:481-492`
`on_mpv_recreated` builds a **fresh** `MpvtkApp`, so mpv re-creation does not
strand these caches. CONFIRMED.

### C3 — "the pause gesture must be bound in both HUD states" — **OPEN, 1 of 6 sites missing**

The dominant cluster of the release, and the one the post-release issues sit on.
Full site table, trace and evidence in §3.3; the summary is that
`phud_bind_summon` (`renderer.lua:6020-6023`) returns before binding anything
when `mouse_click_pauses` is off, on the strength of a comment
(`renderer.lua:6012-6019`) whose premise **our own mpv pin move invalidated**
(`16ad0bb4`, inside the pre13→pre14 window) — **and the same premise was
corrected in `mpvtk_browser/config.py:766-770` and not in the code.**

Two structural facts that belong here rather than in the issue notes:

**The window handoff has exactly two unshared paths, and the plan of record
recommends deleting one of them.** Under a classic OSC the cure for a live
`mpvtk_mouse` is `set_active(False)` → `ui_suspend` (`renderer.lua:5761-5764`),
reached from `_yield()`. `_yield` is reached either from
`load_feedback.clear()` → `_hand_off` (`app.py:234`) — which **early-returns**
when `starting is None` (`load_feedback.py:140-142`), and `starting` is set only
by `_start()`'s video branch (`app.py:2364-2365`), fired only from `ItemActions`
(`item_actions.py:147,205`), i.e. only a play begun *inside the browser* — or
from `on_playstate`'s `if self._browsing: self._yield()` (`app.py:2578-2579`),
which is the **only** path for a remote "Play To", a SyncPlay start or an EOF
queue advance.

`docs/ISSUES_2026-09.md`, Cross-cutting A, ends:

> **While there, delete the dead branch:** `app.py:2371`'s
> `if self._browsing: self._yield()` cannot fire for video, because `_start`
> cleared `_browsing` first.

**That is false for every non-browser start**, and acting on it would remove the
sole mouse-release path for remote, SyncPlay and queue-advance playback.
Verified still present at `app.py:2578-2579`. **Strike it from the doc.**

**And item 7's status line is the process finding.** It reads *"done `a7324740`
— the repro DID exonerate our code; no production change"*, while the plan's own
step 3 had said *"if it is not enabled … the investigation restarts from the
log."* The probe answered the question it was pointed at and the enumeration was
never widened. #737 arrived the day after the tag. That is CLAUDE.md's
non-convergence case: the fix was right about what it was told, and the target
kept moving because nobody wrote down what the thing must guarantee.

### C4 — "`self._video` answers two questions" — **REPAIRED, and the repair works**

Worth recording as the positive control. Three sites were found one at a time;
the audit that fixed the first two missed the third *one call below the method it
fixed*; two more were still open after that. `572a9b5a` replaced careful reading
with `tools/audit_video_predicate.py` + `tests/test_no_raw_video_predicate.py` —
a **declare-the-site** lint that makes no correctness judgement.

Evidence it worked: risk-map row **R11** (`player.py:1518`, the OSD menu gated on
`_video is not None` during music) is **closed at HEAD** — `player.py:1524` now
reads `if self._video_on_screen() and self.on_hud_menu is not None`. The risk map
still lists it as open; the map is stale, the code is fixed.

### C5 — "one owner for the download row" — **REPAIRED, and it cost the most to get there**

The largest single generator (`c0b26b46`, 8 later fixes, 155 lines) sits here,
and the chain is nine commits deep:
`9cc36d1b` → `c0b26b46` → `f00b477d` → `f410173d` → `497ccefb` → `555fda6e` →
`2ca15135` → `cb6708b8` → `3b7bfab9`.

It ended the same way C4 did — with an enumerating instrument, not more care:
`913c8ce8` "Fail the suite when one rule grows a second owner" →
`tools/audit_owned_state.py` + `tests/test_no_second_owner.py`. Its docstring is
the best statement of the repo's defect shape anywhere in the tree
(`tools/audit_owned_state.py:1-36`).

**But it is declared for exactly two pieces of state** (`OWNED` at
`tools/audit_owned_state.py:51-73`): `SyncManager._cancelled` and
`SyncManager._active_item`. The four entries `docs/RISK_MAP_2026-09.md` §7 calls
*"the cheapest Tier-1 coverage available — a bookkeeping extension to a tool that
exists"* — `_sync_path`, `ThumbnailStore._gone`, `mpv.TIMEOUT`,
`_login["pass"]` — are **not declared**. CONFIRMED by grep.

### C6 — "app-initiated window changes must not write user-intent flags" — **OPEN (risk map R8), verified**

`player_window.py:404` writes `self.fullscreen_disable = not enabled` **above**
`if not persist: return` at `:405-406`. The docstring at `:365-367` warns about
exactly this for the neighbouring method.

**Concrete failure, CONFIRMED end to end:**
1. User has `settings.fullscreen = True`.
2. An update notice fires → `update_check.py:258` `set_fullscreen(False)` — no
   `persist`, because the app made this choice, not the user.
3. `:404` latches `self.fullscreen_disable = True`; `:405` returns before
   anything else.
4. `browse_yield` at `:1004` reads `if settings.fullscreen and not
   self.fullscreen_disable:` — **false for the rest of the process**. Every film
   from then on starts windowed, and no setting the user can see explains it.

**Do not fix this by moving one line.** `mpvtk_browser/ui.py:368`
`set_fullscreen(True)` is also non-persist and currently *clears* the latch;
moving the write below the gate would stop that too. Two callers, opposite
directions, one flag — the honest repair is to make `fullscreen_disable` a
parameter of the two intents rather than a side effect of a setter. That is a
3.1.0 change, not a patch.

### C7 — "one authority for a derived cache key" — repaired in-window, twice

`577c7738` ("Let the clock be 12-hour, in all three places that show one",
95 lines later re-touched) → `ed7853a3` ("Stop retagging every strip for a clock
the cache key already knows", 74/74 lines all in-range) → `fe752e76` (tests) →
`1fe13889`. A four-commit chain where a display setting and an artwork cache key
were two authorities on the same fact. Closed, but it is the C2/C5 shape again in
a third subsystem.

### C8 — "a script is not covered just because the face you found opens" — **OPEN, and it is #736**

The rule is written into the tree by `9d5138d7`, at `pilfont.py:56-61`, for
Hebrew. It is applied by *measurement*: `jellyfin_mpv_shim/mpvtk/GUIDE.md` §12 carries a per-face
coverage table for **Hebrew** (§12.1), **Arabic** (§12.1), **emoji** (§12.2) and
**symbol** (§12.4).

There is **no coverage table for CJK**. Verified: `grep -n "CJK|Han|Hangul|
Chinese|Korean|Japanese" jellyfin_mpv_shim/mpvtk/GUIDE.md` returns six hits, none
of them a face-coverage measurement. CJK is the one script list in `pilfont.py`
that was never measured — N-1 of N, in the documentation rather than the code,
which is why review could not see it.

This cluster is the single largest generator group in §1's table
(`aecd940e` → `41ab71fa` → `25dd4317` → `f7e363cb` → `9d5138d7` → `28f2157c` →
`a57e412c` → `bd966c13`, five of the top ten inducers) — eight commits refining
face selection for symbols, emoji, Hebrew and Arabic, in the same file, over four
days, **without once touching the CJK list.** Details of the resulting defect in
§3.1.

---

## 3. The open issues, against the code

### 3.1 #736 — Chinese characters garbled/incomplete — **CONFIRMED, and NOT in-range rot**

**Root cause.** `jellyfin_mpv_shim/mpvtk/pilfont.py:42-55`, the `"cjk"` candidate
list. `_load` (`pilfont.py:387-406`) returns the **first name that opens**, with
no coverage check. On Windows the first seven entries are Linux/macOS paths and
fail; entry eight, `pilfont.py:50` `"msgothic.ttc"` — **MS Gothic, a Japanese
face** — opens. `simsun.ttc` is entry eleven (`:53`) and is never reached.
`font()` then caches it under `("cjk", size, bold)` for the session
(`pilfont.py:416-456`).

`msyh.ttc` (Microsoft YaHei, Windows' Simplified-Chinese UI font) and Microsoft
JhengHei / MingLiU appear **nowhere in the repository** — verified by grep.

**Evidence, measured against the issue's screenshot.** The 41 characters split
100% cleanly: everything that renders is inside `shift_jis_2004` (≈ MS Gothic's
Han repertoire); everything that tofus is outside it and inside `gb2312`. The one
character in an otherwise-good caption that boxes — 莲 (U+83B2) — is the one
character in that line that is not in JIS X 0213.

**Provenance.** `git blame -L 42,55` puts the entire block at `14bb070d`
(2026-07-19), first shipped in v3.0.0pre8. **It is not a v3.0.0 regression** —
it is a partial fix that was never completed, surfacing now because 3.0.0 is the
first stable release with the browser. Reported honestly: this one is *not* rot
from the release window, and the §1 numbers do not claim it.

**Second site, same list, same repair:** `jellyfin_mpv_shim/epub/fonts.py:128-134`
routes every non-Latin script straight into `pilfont.font(script, …)`, so a
Simplified-Chinese ebook in the built-in reader is broken the same way on Windows.

**Two siblings that follow from the same rule (PLAUSIBLE, unreported):**
`pilfont.py:260-261` puts Hangul and Traditional Chinese in the same `"cjk"`
bucket, so **Korean on Windows also resolves to MS Gothic** ahead of
`malgun.ttf`, and Traditional Chinese gets JIS's partial coverage.
`tests/test_mpvtk_pilfont.py:36` asserts `script_of("오징어 게임") == "cjk"` and
stops there.

**Why no test could fail.** `tests/test_mpvtk_pilfont.py:1-7` states the
doctrine: *"which concrete font file gets loaded is a property of the host, so we
only assert that something usable comes back."*
`test_cjk_request_never_raises` (`:57-60`) asserts non-`None`. All twelve
CJK-named tests assert **script names, never coverage**. Nothing in the suite can
fail when the CJK face cannot draw the string — the exact shape
`docs/testing.md` names as *"unreachable while reporting a pass"*.

**Undocumented workaround that fixes it today.** `pilfont.py:730-745` reads
`JELLYFIN_MPV_SHIM_UI_FONT` and prepends that path to every candidate list.
`JELLYFIN_MPV_SHIM_UI_FONT=C:\Windows\Fonts\msyh.ttc` resolves #736. Verified:
`grep -rn JELLYFIN_MPV_SHIM_UI_FONT --include=*.md .` returns **nothing** — it is
documented in no `.md` file in the repo.

### 3.2 #688 (RTL) — **very likely already fixed; does not share #736's rule**

Root cause is FriBiDi, stated at `jellyfin_mpv_shim/win_fribidi.py:1-21`:
Pillow's binary wheels ship no FriBiDi and its absence is **silent** —
`ImageFont.truetype` downgrades to `Layout.BASIC` with no exception, losing bidi
reorder *and* Arabic joining. `7ed41bce` ("Ship FriBiDi on Windows") is contained
in **v3.0.0pre14 and v3.0.0**. The issue is open with no reporter retest.

The reporter's component split is not arbitrary — it is exactly the two-path
boundary in `jellyfin_mpv_shim/mpvtk/GUIDE.md:766-773`: what rendered correctly (title bar, OSD
title, breadcrumb, overview) goes **ASS → libass**, which does its own shaping;
what broke (home card titles, Cast & Crew, hero title, genre line) is **baked
bitmaps → Pillow**.

**They are different rules.** #688 is a *shaping engine* fault, and the
"one face for the whole RTL line" rule it is adjacent to lives inside `_split`
(`pilfont.py:564-565`), which both `_measure` and `draw_text` call — so it cannot
be applied at N-1 sites by construction. #736 is a *coverage* fault. The rhyme is
the enclosing structure: two text paths, and only the Pillow one has to
reimplement fontconfig by hand.

**One residual, same shape, third instance (PLAUSIBLE):** `script_of`
(`pilfont.py:296-302`) returns the *first* non-Latin script in a string, and
`_split` gives an RTL line one face from it. A single unwrapped line mixing CJK
then Arabic draws the Arabic in the CJK face. `tests/test_mpvtk_pilfont.py:693-722`
pins the **wrapped** direction only.

### 3.3 The mouse cluster — **one rule, broken at 1 of 6 sites** (#724 issue 2, #737; #726 is its mirror)

Two independent passes (mine, and a dedicated tracing pass) converged on the
same site. What the issues actually say:

- **#724** (Asinin3, Windows 11, pre14): *"Disable Left Click Pauses Playback …
  push right mouse to pause and don't move it. Once the controls disappear,
  push right mouse again and **nothing will happen**. Move the mouse and observe
  that it keeps working as long as the mouse position changed."*
- **#737** (iamtherobin, Linux Flatpak/X11, 3.0.0): *"using **right click to
  pause** … the UI becomes unresponsive after playing a few videos … **Keyboard
  shortcuts still work.** If I quit playback with `q`, the entire MPV is no
  longer responding to any mouse clicks … **Switching back to left click to
  pause this behavior never occurs.**"*

That last clause is the whole localisation: `mouse_click_pauses` defaults
**True** (`conf.py:715`), and both reports live only on the `False` branch.

**The rule**, as the code enforces it elsewhere: *the pause gesture must be bound
in both HUD states — summoned and hidden — because `ui_suspend` disables
`mpvtk_mouse` whenever the HUD hides* (`renderer.lua:5761`, reached from
`phud_hide` at `:6238`).

| # | HUD state | `click_pauses` | button | site | bound? |
|---|---|---|---|---|---|
| A1 | shown | on | left | `renderer.lua:3533-3534` → `state.pause_now()` | ✅ |
| A2 | **hidden** | on | left | `renderer.lua:6024-6035` `phud_bind_summon` → `mpvtk_phud_click` | ✅ |
| A3 | shown | off | right | `renderer.lua:3987-3990` `on_rclick` → `state.pause_now()` | ✅ |
| **A4** | **hidden** | **off** | **right** | `renderer.lua:6020-6023` — `phud_bind_summon` **returns before binding anything** | ❌ |
| A5 | hidden | off | left | `renderer.lua:5933-5945` `mpvtk_skip_click` (skip button only) | ✅ (but see the leak below) |
| A6 | shown | on | right = drag | `on_rclick` (`:3954-3993`) has no `begin-vo-dragging` branch | ❌ — this is **#726** |

`grep -rn "mbtn_right" jellyfin_mpv_shim/` returns **one** binding in the whole
package: `renderer.lua:4966`, inside `mpvtk_mouse`. There is no other
right-button claim anywhere.

**The trace for #724 issue 2, CONFIRMED step by step.** Defaults are
`hud_autohide = "hover"`, `hud_hide_secs = 4.0` (`conf.py:605,608`), so *hidden*
is the normal playback state. Pointer moves → `phud_summon('mouse')`
(`:5300`) → `ui_resume` → `enable_key_bindings('mpvtk_mouse')` (`:5702`) →
right-click reaches `on_rclick` → pauses. Pointer stops → auto-hide →
`phud_hide` (`:6223`) → `ui_suspend` (`:6238`) → `disable_key_bindings(
'mpvtk_mouse')` (`:5761`) → `phud_bind_summon` (`:6240`) binds **nothing** at
`:6020-6022`. Right-click now falls to mpv's builtin. Nothing pauses. Move the
mouse and it works again. Every clause of the report, with no hover strand
needed.

**A4's justification was true when written, and *we* falsified it.**
`renderer.lua:6012-6019` (blame `3f221d41`, 2026-08-08, ships in pre13):

> **Not bound at all in mpv's modality** (mouse_click_pauses off): a forced
> binding here is what stops the VO dragging the window with the left button …
> **Right-click-to-pause** and double-click-to-fullscreen are **mpv's own
> defaults and need nothing from us either way**.

The premise is that mpv's own `MBTN_RIGHT` pauses. Upstream changed that in
**`65a1852ba3` "input.conf: bind right click to context menu", 2026-06-02** —
which is **master-only and post-0.41.0**, not "at 0.41":

```
git -C mpv merge-base --is-ancestor 65a1852ba3 41f6a645   → false (not in v0.41.0)
git -C mpv show 41f6a645:etc/input.conf   → #MBTN_RIGHT  cycle pause
git -C mpv show 182fa6ca:etc/input.conf   → #MBTN_RIGHT  script-binding select/context-menu
```

**`16ad0bb4` "Ship a Vulkan loader on Windows and move both mpv pins to master"
(2026-08-28) is in pre14 and not in pre13, and it moved both pins across that
boundary:**

| platform | pre13 pin | pre14 / 3.0.0 pin | mpv's `MBTN_RIGHT` |
|---|---|---|---|
| **Flatpak** | v0.41.0 `41f6a645` | master `182fa6ca` | **`cycle pause` → context menu** |
| Windows | shinchiro `20260610` | shinchiro `20260828` | context menu in both¹ |

¹ inferred from shinchiro's build-date naming — a 2026-06-10 master snapshot
postdates the 2026-06-02 commit. Not verified against the binary.

So the comment was **correct for Flatpak on the day it was written** (pre13
shipped v0.41.0, where right-click really did pause) and **already false for
Windows on that same day**. It became false everywhere at `16ad0bb4`. This is
not upstream drift we failed to track: **our own pin move removed the default
the code was leaning on, in the pre13→pre14 window.**

That correction **was** made — in the help text:
`mpvtk_browser/config.py:766-770`, *"NOT 'right click to pause', which this said
and which is no longer true on the mpv we ship."* (That note also says "at
0.41", which is off by one release; the fact it asserts is right.)

**So the code site that depends on the fact was left behind while the
documentation site was fixed.** That is this repo's signature shape, and it is
the cleanest instance of it in the release.

**#737 is therefore a genuine, platform-specific pre14 regression with a named
cause.** Its reporter is on Linux Flatpak. On pre13 their right-click-to-pause
worked through the hidden HUD *because mpv's own default did it*, exactly as
`:6015-6016` claims; `16ad0bb4` took that away and left site A4 empty behind it.
CONFIRMED — the pin diff and both `input.conf` reads are quoted above.

**Do not count this as release-window rot.** `16ad0bb4` is 2026-08-28 and is
contained *in* `v3.0.0pre14`, so it sits before this postmortem's measured range
(`pre14..v3.0.0`) and contributes nothing to §1's rates — the same bookkeeping
the `mpvtk_skip_click` leak gets below. It belongs here because it is the cause
of a post-release issue, not because it is inside the window.

**#737's escalation — PLAUSIBLE, two candidate mechanisms, not mutually
exclusive.** Neither could be executed read-only, so neither is claimed:

1. **mpv's context menu, ungoverned.** On the flatpak's *current* mpv (master
   `182fa6ca`; **not** pre13's v0.41.0), `MBTN_RIGHT` →
   `select.lua` → `context_menu.lua`'s `open`, which force-binds `MOUSE_MOVE`,
   all three buttons, the arrows, `ENTER`, `ESC` and `ANY_UNICODE`, and sets
   `user-data/mpv/context-menu/open`. Because it is the only section binding
   `MOUSE_MOVE`, mpv makes it the `mouse_section` and routes all mouse input
   there. **`grep -rn "context.menu" jellyfin_mpv_shim/` finds exactly one hit,
   and it is a comment** (`config.py:769`) — no observer, no teardown, nothing.
   The shim *does* have exactly this observer for mpv's console
   (`renderer.lua:6815-6890`): the same rule, at 1 of 2 sites again. Keyboard
   survives because browse resume re-asserts `bind_nav_keys()` above
   `mpvtk_mouse` in the stack — which matches "keyboard still works, mouse does
   not, survives quitting playback, needs a restart".
2. **A leaked forced binding — CONFIRMED as a leak.**
   `renderer.lua:5934` `mp.add_forced_key_binding('mbtn_left',
   'mpvtk_skip_click', …)` has **no matching removal anywhere in the file**.
   Verified twice: the only other occurrence of the name is that line, and none
   of the 17 `remove_key_binding` calls can construct it (`bname` at `:2938` is
   from `text_key_names`, i.e. `mpvtk_kp_*`). `phud_skip_unbind` (`:5949-5952`)
   removes only `mpvtk_skip_enter`. The binding is gated at `:5933` on
   `not state.phud.click_pauses` — **so it exists only in the exact
   configuration both reporters are in** — and its else-branch is
   `begin-vo-dragging`. Once a Skip Intro/Credits button has appeared once, a
   whole-screen forced `mbtn_left` binding is installed for the life of the mpv
   session. Blame: `97c558191`, 2026-08-08 — **before pre14, so not
   release-window rot**, but in the same rule family, and `mouse_click_pauses`
   is a Player Controls row (`config.py:241`), i.e. Tier 2.

**A hypothesis to record as refuted, so nobody re-runs it.** I initially
suspected `state.mouse.down` latching `true` and permanently disabling the #700
hover repair (`renderer.lua:5152-5153`). It does not. `mp.set_key_bindings`
entries are `{key, cb, cb_down, cb_up}` (verified in mpv's own
`player/lua/defaults.lua:88-99` on this box), so `:4966-4967` is correctly
press→`true`, release→`nil`; `ui_suspend` clears it at `:5747`; and mpv re-queues
the cloned down command on release, so the "u" arrives even after the owning
section is disabled.

### 3.4 A factual error in the plan of record that produced a "nothing to do"

`docs/ISSUES_2026-09.md:894-897`:

> **Issue 2** … is the #700 hover strand, fixed by `2035d720`, which landed
> **after** pre14 was cut. The reporter confirms it is fixed on dev. Nothing to
> do; it ships in pre15.

**Verified: `git merge-base --is-ancestor 2035d720 v3.0.0pre14` → true.**
`2035d720` is 2026-08-24; `v3.0.0pre14` is 2026-08-28. The commit is *inside*
pre14, so the reporter was already running the claimed fix, and the "nothing to
do" verdict has no basis. Worse, the whole #700 synth-hover machinery landed in
the pre13→pre14 window — which is the very regression window #724 names — so it
is a *candidate cause*, not the cure. That inversion is why the doc's own open
question (*"Still unexplained: why the reporter calls it a pre13 regression"*,
`ISSUES_2026-09.md:951-955`) stayed open.

**That question is now answered for #737's reporter, and still open for
#724's.** The Flatpak half is settled by §3.3: `16ad0bb4` moved that pin from
v0.41.0 to master inside the pre13→pre14 window, and with it `MBTN_RIGHT` from
`cycle pause` to the context menu. **The Windows half is not.** Asinin3 is on
Windows 11, where shinchiro's pre13 `20260610` build already postdated
`65a1852ba3` (2026-06-02) — so on their machine the A4 gap should have been just
as reachable in pre13, and the pin move explains nothing. Either they did not
run that exact sequence on pre13, or #724 issue 2 has a different in-window
cause; the remaining candidate is the #700 hover machinery that landed in the
same window (`e727de17`, `2035d720`). **Recorded as unexplained rather than
attributed** — the plan of record's original error was attributing this half on
the strength of a plausible story, and repeating that with a better story is
the same mistake.

The doc's supporting version table (`ISSUES_2026-09.md:961-975`) is also wrong
where it matters: it attributes the change to "0.41" and then concludes *"Both
shipped pins are past that change — the Flatpak pin was `v0.41.0` in pre13"*.
With the real boundary that sentence refutes itself, because v0.41.0 is **not**
past a 2026-06-02 master commit.

None of the seven renderer commits between pre14 and 3.0.0 (`8e75fe31`,
`857e35d1`, `e6847189`, `1ac6d252`, `573a4f33`, `65fcb965`, `1ad8d4ea`) touches
`phud_bind_summon`, `on_rclick` or `ui_suspend`'s mouse section. **A4 is still
open at HEAD.**

### 3.5 #733 — CONFIRMED, and it *is* release-window rot

Reported: the scroll wheel changing volume still draws mpv's own OSD bar, and so
do `1`/`2` for contrast.

**Cause of the scroll half: `857e35d1` "mpvtk: give the wheel back while nothing
on the HUD can spend it" (2026-09-01) — in range.** `state.wheel_sync()`
(`renderer.lua:5031-5062`) now enables `mpvtk_wheel` only when a dropdown is
open, a page claimed the wheel, or a scrollable node is on screen (`:5033-5054`);
otherwise `disable_key_bindings('mpvtk_wheel')` (`:5060`). Handing the wheel back
to mpv hands mpv's OSD back with it. The maintainer says as much on the issue and
defers it.

The `1`/`2` half is by design: the key block is browse-only
(`renderer.lua:5725` `if not state.phud.mode then keyclaim.block_bind() end`,
also `:4894`, `:5819`), so mpv's own shortcuts keep their meaning over a video.

**Nothing in the shim ever suppresses mpv's OSD.** `grep -rn
"osd-bar|osd-level|osd-msg|no-osd" jellyfin_mpv_shim/` finds only
`keysweep.py:33-40` (parsing prefixes out of *user* bindings) and
`trickplay-osc.lua:2948,2980` (the forked OSC reading `osd-level`).
`mpv_options.py` sets no OSD option at all. So a fix for #733 has **no existing
owner** and is new code, not a tweak.

### 3.6 #739 — CONFIRMED: one rule, two implementations, applied at 1 of 3

Reported on Fedora KDE Wayland; the reporter correctly guessed the manifest.
**Two defects, and the second is the interesting one.**

**(a) Packaging.** `flatpak/com.github.iwalton3.jellyfin-mpv-shim.json:86` builds
`xclip`. `grep -n "wl-clipboard|wl-copy|xsel" flatpak/*.json` → **no hits**. So
`wl-paste` does not exist in the sandbox.

**(b) The renderer will not use the helper that *is* shipped.**
`renderer.lua:307-334` `clip_tools()` uses `elseif`:

```lua
elseif os.getenv('WAYLAND_DISPLAY') then    -- :316  → wl-copy / wl-paste only
elseif os.getenv('DISPLAY') then            -- :322  → xclip, xsel
```

On a Wayland session the X11 helpers are **never tried**, even though the flatpak
has one and XWayland is present. The Python half of the *same rule*, written in
the *same commit* (`15537a28`), gets it right — `clipboard.py:22-26` lists
`wl-copy`, `xclip`, `xsel` unconditionally in preference order.

**Observable consequence of the split:** Settings → Copy Log goes through
`clipboard.py` (`gateway/diagnostics.py:83`) and **works** on that box; textbox
paste goes through `renderer.lua:2860-2864` and **does not**. Same user, same
session, opposite answers — which is the cleanest possible demonstration that the
rule has two owners.

**And the error message misleads.** `mpvtk_browser/dialogs.py:1060-1063` appends
*"or use MPV 0.41 or newer"* to every clipboard failure. On the flatpak mpv is
already master, so the advice is stale for exactly the platform that hits it
most — which is why the reporter said *"that is somewhat odd to me."*

**This "0.41" is a different mpv change from §3.3's, and this one is real.**
Checked, because sweeping it into the `MBTN_RIGHT` correction would have been
the easy error: the x11 clipboard backend is `4f03bc1779` (2025-10-26), and
`git -C mpv ls-tree -r 41f6a645 | grep clipboard` shows `clipboard-x11.c`
**is** in v0.41.0. The version boundary in that string is therefore correct;
it is stale only in the narrow sense that the flatpak already satisfies it.

**One thing the repo asserts that is no longer true, found while checking the
above (NEW, not previously flagged).** Three sites say mpv's
`--clipboard-backends` default is `win32,mac,wayland,vo` —
`renderer.lua:298-301`, `clipboard.py:41-43`, `dialogs.py:1051-1052`. In
v0.41.0 the default is **`win32,mac,wayland,x11,vo`**
(`player/clipboard/clipboard.c:83-89`): x11 is in the list, enabled. That does
not change remediation (a) or (b) — the flatpak still has no `wl-paste`, and
`clip_tools()`'s `elseif` still refuses the `xclip` it does ship — but it does
mean the *reason* mpv's own `clipboard/text` failed for this reporter is **not
established**, since an XWayland session should have had a working x11 backend.
Left as an open question rather than guessed at.

---

## 4. 3.0.1 vs 3.1.0

### Ship in 3.0.1 — small, fenced, testable

| # | change | fence |
|---|---|---|
| **#724 / #737** | **Bind the pause button in the hidden-HUD state whichever button it is.** `renderer.lua:6020-6023`: instead of returning, bind `mbtn_right` when `click_pauses` is off, exactly as `:6024` binds `mbtn_left` when it is on. | **One site, site A4**, in a function that already branches on the flag. It *removes* an authority — the "mpv's own defaults cover it" assumption at `:6012-6019`, which **our own pin move** (`16ad0bb4`) falsified, not "0.41" — rather than adding a guard. Closes #724 issue 2 and removes #737's entry condition. |
| **#739 (a)** | Add `wl-clipboard` beside `xclip` in `flatpak/…json:86`. | Packaging only. |
| **#739 (b)** | `renderer.lua:316-333`: concatenate the tool lists instead of `elseif`, matching `clipboard.py:22-26` exactly. | Makes the Lua half agree with the Python half of the same rule, written in the same commit. Deletes a divergence. |
| **#739 (c)** | Drop the *"or use MPV 0.41 or newer"* tail at `dialogs.py:1060-1063` when mpv already is. While there, correct the stale `--clipboard-backends` default recorded at `renderer.lua:298-301`, `clipboard.py:41-43` and `dialogs.py:1051-1052` (x11 **is** in it as of v0.41.0). | One string plus three comments. This "0.41" is a *different*, still-valid mpv boundary (`4f03bc1779`) — it is **not** covered by §3.3's correction. |
| **#736 stopgap** | Document `JELLYFIN_MPV_SHIM_UI_FONT` in `docs/configuration.md`; answer the issue with `C:\Windows\Fonts\msyh.ttc`. Optionally add `msyh.ttc` / `msjh.ttc` to `pilfont.py:42-55`. | The env var already exists (`pilfont.py:730-745`) and is documented in **no** `.md` file. Zero code for the documentation half. |
| **#688** | Ask for a 3.0.0 retest and close. | `7ed41bce` is contained in the tag. |
| **Doc debt** | Strike the "delete the dead branch" instruction (§2 C3); correct `2035d720`'s provenance (§3.4); correct R3/R10's setting and tier (§2 C1); write the promised `do-not-fix.md` entries for **#726** and #727 item 1 that `ISSUES_2026-09.md:1505-1506` says are owed and that `grep` shows were never written. | Prose. It is also the highest-leverage item in the table, because three separate wrong verdicts trace to these. |

### Hold for 3.1.0 — the honest repair is structural

| item | why a patch is the wrong shape |
|---|---|
| **`mpvtk_skip_click`'s leaked forced binding** (§3.3) | A `remove_key_binding` in `phud_skip_unbind` is two lines and probably right — but the *class* is "a forced binding whose removal site is somewhere else", and nobody has counted the siblings among the 17 `remove_key_binding` calls and their `add_forced_key_binding` partners. Count first; that window closes on the first patch. |
| **mpv's context menu has no observer** (§3.3) | The console equivalent exists at `renderer.lua:6815-6890`; this is the same rule at 1 of 2 sites. But it is a new channel through the input arbiter — **the surface with the worst regression record here** (`do-not-fix.md` F37: *"three in 48 hours"*). |
| **#733** | Suppressing mpv's OSD has **no owner anywhere in the tree** (`mpv_options.py` sets no OSD option). It is new code plus a design decision the maintainer has already deferred on the issue. |
| **#736 properly** | Reordering the font list is one guard at one site and leaves Korean, Traditional Chinese and `epub/fonts.py:128-134` picking by the same unchecked rule. See §5.4. |
| **R8 / `fullscreen_disable`** (§2 C6) | Looks like moving one line; `mpvtk_browser/ui.py:368` is a second non-persist caller in the opposite direction, so moving it introduces a second bug. Two intents, one flag; the repair is a parameter. |
| **F42 / `_sync_path`** | `do-not-fix.md` already states why the obvious fix is wrong and that the right one is in `_retire_page`. Destructive (drives a store Move). |
| **The three lints** (§5.1-5.3) | These *are* the 3.1.0 work. They are not fixes, so they carry none of the measured fix-side rot. |
| **#726** | Maintainer verdict is as-designed. It needs the `do-not-fix.md` entry, not code. |

### Which of these fixes are themselves high-risk

- **The A4 fix is the safest thing in the release** and should not be deferred:
  one site, one function, deletes an assumption. But it lands in
  `mpvtk/renderer.lua`, which the risk map measures at **123 commits, all 123 in
  the last 120 days** — no cooled layer at all — and on the input arbiter. So:
  land it alone, with the failing case derived from #724's words **before** the
  fix exists, and run the real-mpv matrix rather than a unit test.
- **Any `pilfont.py` change joins the cluster that produced five of the top ten
  generators** in §1 (`aecd940e`, `f7e363cb`, `25dd4317`, `41ab71fa`,
  `28f2157c`). Keep 3.0.1's font work to the candidate list and the docs; the
  `_load` coverage check belongs in 3.1.0 with the site count done first.
- **The doc corrections carry no code risk and unblock three wrong verdicts.**
  They are the best value in the patch release.

**Sequencing, in the repo's own terms** — *measure → ratify → move the code*. The
measure step is done (`docs/RISK_MAP_2026-09.md`, plus §1 here). **Ratify next**:
the four doc corrections above, and F35's product question (does the idle Skip
offer follow `hud_wake_key` or `ui_select_key`?). Only then move code, and move
it with an instrument rather than a patch — because §1's daily table says the
patch-at-a-time mode is what produced the 44% trailing rate in the first place.

---

## 5. What would have caught it

Ranked by cost. Every one of these extends machinery that already exists in the
tree — CLAUDE.md's own finding is that *advisory prose does not change the rate*,
and this repo has twice ended a loop with an enumerating instrument (C4, C5) and
zero times with a careful re-read.

### 5.1 `tools/audit_frozen_key_literals.py` — BUILT, and it found three more

Covers C1 / R1, R2, R3, R10 — the cluster with the worst live record and no
instrument at all. Structure copied verbatim from
`tools/audit_video_predicate.py`: build the set of key-valued settings from
`conf.py` (`kb_*`, `ui_select_key`, `hud_wake_key`), scan `player.py`,
`mpvtk_browser/app.py` and `mpvtk/renderer.lua` for string literals equal to any
of their defaults, and require each to appear in a `DECLARED` table with a
reason. No correctness judgement — *declaration* is the whole ask, and the
declaration is where the next reader learns the resolver exists.

Guard: `tests/test_no_frozen_key_literals.py`. Precedent that it works: R10 was
found by writing this predicate down, not by reading the file.

**Built, with two departures from the spec above and one result.**

The spec said *"scan for string literals equal to any of their defaults"*. That
matches 85 sites, most of them noise — `'left'` is an alignment far more often
than it is a key, and the first run flagged a debug overlay's `'F'` glyph on
`kb_fullscreen`. So the vocabulary is the **multi-character** defaults only, and
matching is case-sensitive on mpv's uppercase spelling, which is exactly what
separates a key from a direction here. Both limits are stated in the file: a
frozen key written lowercase in binding code is invisible to it.

The second departure is the grain. Declaring 49 individual literals would have
been exempted into uselessness, so a declaration covers a *scope* — 20 of them.
That needed real scope tracking in Lua rather than "the last `function` line
seen": without it a file-scope constant is charged to whatever function is
above, which put **R3 inside `phud_wake_key`** — the resolver that is R3's own
counter-example.

**Three more findings, from the first clean run:**

- **R2 has a second site.** `_shell_claimed_keys` claims the literal `SPACE`
  and `_shell_key` dispatches on `key == "SPACE"`. Two independent freezes of
  `kb_pause`; the risk map records the claim only.
- **`phud_bind_summon`** compares the loop's key against a literal `ENTER` to
  choose its handler, so resolving R3's table would not by itself repair it.
- **`phud_bind_wake`** compares the *resolved* wake key against `ENTER` to
  decide whether waking the HUD also toggles pause — so moving `hud_wake_key`
  silently drops the pause half. That is a product question rather than a
  freeze, and it is declared as one.

Seven declared-open rows in all, none repaired here: every one needs a
config-file edit to reach, which is §7's reason for deferring them, and the
status is a field on the declaration rather than a word in its prose. The first
draft read `"OPEN" in why` and disagreed with itself about two rows — the
tool's own defect shape, in the tool.

### 5.2 Four lines in `tools/audit_owned_state.py` — DONE, and not four lines

Add `_sync_path`, `ThumbnailStore._gone`, `mpv.TIMEOUT` and `_login["pass"]` to
`OWNED`. `docs/RISK_MAP_2026-09.md` §7 already specifies them, already argues
they are the cheapest Tier-1 coverage in the tree, and the tool is already wired
into the suite. ~~This is bookkeeping, not engineering.~~

**It was not bookkeeping, and the reason is the finding.** Only
`ThumbnailStore._gone` fitted the tool as written; the other three did not, and
each in a different way:

- **`_sync_path` and `_login` are not confined to one file.** The tool took one
  module per entry, and the browser is one `self` spread across a dozen mixin
  modules — so scoping either of them to the file that touches it today would
  have been *this document's own defect shape*, the right rule at one of
  several sites, installed by the tool meant to catch it. `scope` now takes a
  directory, and owners are spelled `path:function`.
- **`mpv.TIMEOUT` does not hang off `self` at all.** It is a module global of
  the backend library. The walker only matched `self.<attr>`, so the entry
  would have found nothing and reported a clean tree forever — the failure
  `test_no_second_owner.py`'s second test exists to catch. `obj` now names what
  the state hangs off.
- **`_login["pass"]` is a dict key, and the entry is deliberately weaker than
  the risk it records.** The tool matches attributes, not subscripts, so the
  scope is the whole `_login` dict with six owners. Six is a weak check and it
  is the honest one; the *repair* — clearing `pass` after a successful login,
  the way `_pin` already clears — is a behaviour change and is not this.

Each of the three was mutation-tested: a second owner in the same file, one in
a sibling mixin module, and a second writer of the global from another module
are all reported.

Note the tool's own rule while doing it: *"`owners` records the sites that exist,
never the ones that may"* — a speculative name pre-authorises the very second
owner the audit exists to catch.

### 5.3 `tools/audit_act_targets.py` — ~~specified, never built~~ BUILT

`docs/RISK_MAP_2026-09.md` §7 says *"stays first among the lints: it covers R7
and R8 (Tier 1 and Tier 2)"*. ~~It does not exist~~ — it does now, with
`tests/test_no_act_reachthrough.py`. Its rule: every gateway `_act` property
write must route through a `PlayerManager` owner method. It covers C6 above and
R7 (`player_window.py:621` `set_picture_view` lacking the `_video is None and
not _loading` guard that `reset_picture_view` grew at `:597`).

**The site count, taken before any patch: ten, in two of the seventeen gateway
modules.** Four writes (`speed`, `video_aspect_override`, `mute`, `fullscreen`,
all in `hud.py`) and six reads, of which two are read-only by construction —
the Playback Data panel's seven counters, and the diagnostics screen handing the
handle to `clipboard.copy_or_save` as an argument.

**Three of the four writes have a `PlayerManager` method sitting there
unused** — `set_speed`, `set_mute` and `set_fullscreen` all exist. The fourth,
`set_aspect`, exists only on `enrich-e2e-tests`, whose one production change is
exactly this repair. So the lint's first run says the fix for most of this is
*calling what is already written*, and none of it is made here: routing a write
through `set_fullscreen` changes who takes the player lock, which is a behaviour
change and `set_speed` carries no `@synchronous` either, so the honest repair is
both halves at once and belongs in its own commit.

Two things the first run found that hand-reading did not. A reach spelled
`getattr(playerManager, "_player", None)` carries the handle as a **string**, so
no `Attribute` node holds it and an AST walk goes straight past — the package
has one. And writing the declaration list by hand produced **six** keys naming
functions that do not exist, because the enclosing `def` of a `_act` lambda is
often a nested one (`toggle_fullscreen.flip`, not `toggle_fullscreen`). The
stale-key check caught all six on the first run, which is the case it was
written for.

### 5.4 Make `_load` check coverage instead of reordering the list

Covers C8 / #736. The instinct is to move `msyh.ttc` above `msgothic.ttc` at
`pilfont.py:50`. **That is one guard at one site**, and it leaves Korean,
Traditional Chinese and `epub/fonts.py:128-134` picking by the same unchecked
rule. The repair that *removes an authority* is available: give `_load`
(`pilfont.py:387-406`) the string it is resolving for and reject a face that
cannot draw it — compare `face.getmask(ch)` against that face's own `.notdef`
render for a guaranteed-absent codepoint. "First name that opens" becomes "first
name that works", every script list stops depending on its ordering, and the
suite gains something that can fail.

Count the sites first (CLAUDE.md's deadline rule): browser captions
(`strips.py:1341-1378`), banner (`components/banner.py:27-205`), cast
(`cast.py:194,436`), ebook (`epub/paint.py:108,175`) — plus, if the check goes in
`_load`, every other candidate list that currently trusts its own order.

Pair it with the missing **CJK coverage table in `jellyfin_mpv_shim/mpvtk/GUIDE.md` §12**, which is
what makes the check auditable rather than another opaque guard, and with a test
that asserts a *drawn glyph is not `.notdef`* rather than that a face is
non-`None`.

### 5.5 A fake-contract field: `state.phud.mode` in the renderer fakes

**All three** bare-video mouse fall-throughs (`renderer.lua:3524`, `:3917`,
`:3988`) gate on `state.phud.mode and state.phud.shown` — the two states site A4
(§3.3) sits between, and the pair that classic-OSC modality never reaches at all.
Verified: `phud.mode` gates 12 sites in the file and nothing sets it outside the
HUD lifecycle. `docs/testing.md`'s discipline — *"a stand-in
that omits a field is how a property goes untested … it makes the path
unreachable while reporting a pass"* — applies exactly: a fake or fixture whose
`phud.mode` is always set can never exercise the swallowed-button path, and
`grep -rn phud tests/_*.py` finds nothing modelling it.
`tools/audit_fake_contracts.py` is the existing home for this.

**DONE, and not there — that pointer was wrong.** `audit_fake_contracts.py`
extracts what production *Python* reaches on a collaborator; `phud.mode` is
renderer state and never crosses into Python except as the `phud_mode` field of
the `debug_state` reply, which `tests/integration/test_mpvtk_hud.py` already
reads. There was nothing for it to audit.

The gap the section names is real, and it was a missing *case* rather than a
missing field: nothing had ever clicked bare video with HUD mode **off** and
`mpvtk_mouse` still enabled — the state a lua OSC leaves the renderer in, where
all three fall-throughs are dead and the buttons go nowhere. That case is now in
`tests/lua/test_renderer.lua`, next to the HUD-up block it is the negative of,
and it opens by asserting `phud_mode` is actually false, because a fixture that
quietly had the mode on would pass every one of its assertions for the wrong
reason.

Mutation-checked one gate at a time: ungating the click, the double click and
the right click each fails exactly its own assertion, and publishing
`phud_mode = true` fails the setup guard. The first draft shared one command log
across the three presses, so a left click that wrongly paused also answered the
right click's assertion — three assertions, one of them measuring the others.
They get a log each.

### 5.6 A binding-lifetime lint — the one instrument that fits `renderer.lua`

Every `mp.add_forced_key_binding(<key>, '<name>', …)` in `renderer.lua` should
have a `mp.remove_key_binding('<name>')` somewhere in the same file, or a
declared exemption. That is a ~40-line AST-free grep-and-pair check, and it
would have found `mpvtk_skip_click` (§3.3) immediately: 17 removals, and one
added name with no partner.

It is the right *shape* for this file for the reason the risk map gives —
`renderer.lua` is 123 commits in 120 days with no cooled layer, and six
independent claimants decide who owns a key (§3.3 of the risk map). A pairing
check is the only thing here that scales with the file rather than with the
reader's attention.

A second, cheaper pass in the same tool: **every `user-data/mpv/*/open`
property mpv's builtin scripts set should have an observer or a declared
"we do not care".** The shim observes `console/open` (`renderer.lua:6815-6890`)
and not `context-menu/open` — one rule, one of two sites, and §3.3 names it as a
live #737 candidate.

**DONE as the lint; the fix is deliberately not in it, and the measurement
changed what the fix would have to be.** `PUBLISHED` in the same tool now
enumerates all four `user-data/mpv/*` properties mpv's scripts set, each either
observed, declared irrelevant, or recorded as a gap that the test pins so a
*new* one has to be argued for.

`tools/probe_key_precedence.py` asks the question of a real mpv by pressing the
key, and it refutes the premise this section was written on. A standing forced
binding of ours does **not** outrank an overlay that opens after it — the
overlay wins. The exposure is the third case: we re-bind while it is up, which
the HUD does on pointer movement, and then the menu is drawn and dead. Same
answer for the console, so `renderer.lua`'s own comment about why its console
handler exists was wrong for a release and is corrected. Full table:
`docs/mpv-backends.md` §5.

The repair is one handler mirroring the console's, and it is not made here: it
lands on the input arbiter — `docs/do-not-fix.md` F37, the worst regression
surface in the tree — and #737's fix is still ahead of its own evidence one
branch below. What is measured is mpv's mechanism. What is **not** measured is
that a real session reaches the third case, and that is a real-mpv e2e leg.

### 5.7 Ship the branch's mouse e2e coverage — but know what it does and does not cover

`tests/e2e/test_mouse_routing.py` — real `MBTN_RIGHT` presses at `:358` and
`:456` — arrived on `enrich-e2e-tests` in `80835bd7`, `28413f4c`, `94d86357`
(2026-09-06). Verified: `git merge-base --is-ancestor <sha> v3.0.0` returns **NO**
for all three, so none of it shipped.

**Honest limit:** those press the right button on *library tiles* to open context
menus. They do **not** cover A4, which needs right-click **over bare video, with
`mouse_click_pauses` off, after the HUD auto-hides**. The coverage that would
have caught #724/#737 is a fourth state in the same file, not one of these three.

**That fourth state exists now**, as two tests rather than one, because the
cluster turned out to be two properties: the right button reaching the picture
(A4), and the left button still reaching a tile after a skip segment has come
and gone (#737). Both mutation-controlled, both green on both backends.

The second one is worth more than a test. Putting the leak back — deleting the
one `remove_key_binding` line — fails it on the click, which means the
preemption is now *shown* rather than argued from how mpv ranks bindings. That
was one of the two things §3.3 could not establish. The other, whether the
reporter's freeze is this leak, is unchanged: this predicts the freeze on the
first skip segment and the report says "after a video or two".
Ship the branch anyway — it is real coverage — but do not let it be mistaken for
this cluster's guard.

### 5.8 The process rule that actually fires

The daily table in §1 is computable in one command. **A release should not tag
while the trailing-3-day self-fix rate is above the window's opening rate.** On
2026-09-02 it went 20% → 79% and never came back down; the tag went out five days
later. This is CLAUDE.md's stated trigger — *"when a round's findings land mostly
in code that earlier rounds fixed, stop applying them one at a time and run
`/breakfix-review`"* — with a number attached so it can fire without anyone
noticing it should.

---

## Appendix — method, and what it cannot see

**Read-only throughout.** No `checkout`/`reset`/`stash`, no test run, no file in
the repo modified. The measurement scripts live in the scratchpad (`szz.py`,
`proseclass.py`, `szz2.py`) with their JSON output beside them.

**What the numbers cannot see.** `git blame` cannot attribute a fix that
supersedes by *adding* a guard, which is the normal shape of a correctness fix —
so §1's rates are a floor, quantified at 22 blind commits, 16 of them landing in
files the window had already touched. It also cannot distinguish a *repair* from
*iterative construction*; the four AST-identical prose commits are subtracted,
but a commit that legitimately refines its own three-day-old design still counts
as a self-fix.

**What was confirmed vs pattern-matched.** Every `file:line` in §2 and §3 was
read at HEAD. The two things I have labelled PLAUSIBLE — #737's escalation
mechanism and the CJK sibling breakage for Korean/Traditional Chinese — are
labelled that way because they need a running mpv and a Windows box respectively,
and neither was available read-only. #739's Wayland/XWayland premise rests on the
reporter's session, not on a measurement here.

**Not located:** which specific transition, if any, produces #737's *escalation*
(as opposed to its entry condition, which is site A4 and is confirmed). Two
candidate mechanisms are named in §3.3 and neither is claimed.

**Two of my own hypotheses were refuted during this work**, and are recorded so
nobody re-runs them: the `state.mouse.down` hover-repair latch (§3.3), and my
first SZZ run's 0.0% result, which was a `git blame --no-color` ambiguous-option
error rather than a clean release (§1). The second is the more instructive: it
produced a plausible, publishable number from a tool that had silently failed —
this repo's own "gate on the value, not the marker" failure, inside its own
postmortem.
