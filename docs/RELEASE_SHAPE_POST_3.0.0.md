# Release shape after 3.0.0 — one release, not two

**Kind: a decision, and the evidence that moves it.** The measurement steps are
already done and live in `docs/RISK_MAP_2026-09.md` (the map) and
`docs/POSTMORTEM_3.0.0.md` (the rot and the per-issue tracing). This document
answers the question those two leave open: *what is the next release, and what
is in it.* **Izzie's decisions of 2026-09-08 are folded in and marked [iw].**

**Policy [iw]: every GitHub user interaction on this project must be written by
Izzie.** Per Jellyfin project rules. So "ask the reporter", "answer the issue"
and "hand over a CI build" are never tasks this document's author can complete —
only prepare. Reading via `gh` is fine and is how the issue text here was
obtained. Plan exit evidence accordingly.

**Verified read-only against `enrich-e2e-tests` at `91bb3e9f` on 2026-09-08.**
No code was modified to produce it. Claims are marked **CONFIRMED** (read at
`file:line`, or a command run) or **PLAUSIBLE** (pattern-matched). Where the
answer is "I could not determine this", it says so — §8.

---

## The recommendation

**Neither option as stated: ship ONE release, after a week of issue reports,
and it is a 3.1.0.**

Two things settle this, and only the first is technical.

**[iw] The release cadence decides the shape.** Releases on this project are
expensive, so the plan is to wait about a week for more reports before shipping
anything. **That removes the only argument a patch release had — speed.** And
[iw] *"am disinclined to fix #736 with another workaround just to land #739
earlier"*: with the font work done properly, the paste path bounded, and the
aspect override landing, the release carries three behaviour changes. That is a
3.1.0 by semver, and a 3.0.1 would now be a release whose contents are defined
by what we are willing to leave broken.

**The evidence moved two of the postmortem's three headline items.** Its §4
split was written as an input to be tested, and testing it gave:

- **#737's cause is not site A4.** It is a leaked forced key binding, and the
  site count that §4 gave as the reason to defer it to 3.1.0 is now done: **1
  unpaired of 14**, zero exemptions needed. The deferral's stated ground is
  gone.
- **#739's "unexplained" half is explained**, and the explanation does not
  license §4's fix — which has an unpriced hang path on the one platform it
  targets.

Everything else in §4 survives. The doc corrections in particular survive
intact, and — with the release now a week out — they should land on `master`
immediately rather than waiting for it.

**And the highest-value item is not on §4's list at all.** The update notifier
announces a GitHub tag to users who cannot act on it; see §5. It is smaller
than the #737 fix and it affects every Flatpak user on every release.

---

## 1. What changed under verification

### 1.1 #739 — the open question is closed, and it does not license the fix

`docs/POSTMORTEM_3.0.0.md` §3.6 and its Appendix leave this open:

> an XWayland session should have had a working x11 clipboard, so #739's
> reported failure is **not established**.

**It is established. `--clipboard-xwayland` defaults to `no`, and the x11
backend refuses to initialise in a Wayland session without it.** CONFIRMED at
`player/clipboard/clipboard-x11.c:65` in both pins:

```c
if (!xwayland && (getenv("WAYLAND_DISPLAY") || getenv("WAYLAND_SOCKET"))) {
    MP_VERBOSE(x11, "Stopping init due to suspected wayland environment\n");
    goto err;
}
```

```
git -C ~/Desktop/mpv-build/mpv show 41f6a645:player/clipboard/clipboard-x11.c | sed -n '60,70p'
git -C ~/Desktop/mpv-build/mpv show 182fa6ca:DOCS/man/options.rst | grep -A6 'clipboard-xwayland'
```

So the postmortem's wrong-claim #3 is right that the recorded
`--clipboard-backends` default is wrong at four sites (v0.41.0's real default
**is** `win32,mac,wayland,x11,vo` with x11 enabled —
`player/clipboard/clipboard.c:83-89`, CONFIRMED), and wrong that this leaves a
mystery. x11 is listed, enabled, and then declines to start.

**Two corrections to the remediation, and they point opposite ways.**

*First, a worry of mine that is refuted.* I assumed `wl-paste` needs the same
`ext-data-control-v1` that mpv's own Wayland backend needs
(`player/clipboard/clipboard-wayland.c`), so that shipping `wl-clipboard` would
fail for the same reason mpv did. **False.** wl-clipboard falls back to core
`wl_data_device_manager` through a temporary surface; only `wl-paste --watch`
requires wlroots data-control. CONFIRMED from the installed man page. So §4's
item (a) *can* work where mpv's backend does not.

*Second, a failure mode §4 does not price.* The same man page, BUGS, verbatim:

> In some cases the Wayland compositor doesn't give focus to the popup surface,
> which prevents wl-clipboard from accessing the clipboard and **manifests as a
> hang**.

And `clip_call` (`jellyfin_mpv_shim/mpvtk/renderer.lua:337-341`) runs every
helper through mpv's **synchronous** `subprocess`, which **has no timeout
argument at all** — CONFIRMED against `DOCS/man/input.rst`'s `subprocess`
parameter list (`args`, `playback_only`, `capture_size`, `capture_stdout`,
`capture_stderr`, `detach`; no `timeout`).

```
git -C ~/Desktop/mpv-build/mpv show 182fa6ca:DOCS/man/input.rst | grep -A45 '^``subprocess``'
zcat /usr/share/man/man1/wl-clipboard.1.gz | sed -n '160,175p'
```

**Consequence — the hang is already reachable today**, and the split is by
packaging:

- **In the Flatpak**, paste cannot hang, for the accidental reason that
  `wl-paste` is not installed — the subprocess fails immediately and paste
  silently does nothing. That is #739 as reported.
- **On a native Wayland install** (pip, AUR, distro) where `wl-clipboard` *is*
  present and the compositor does not implement `ext-data-control-v1`,
  `clip_get` already shells out to `wl-paste` synchronously, and the man page's
  BUGS section says the popup-surface fallback "manifests as a hang". **The
  freeze is a present-tense defect for those users**, not a hypothetical.

So adding `wl-clipboard` to the manifest without bounding the call does not
introduce the hang — it *extends it to Flatpak users*, converting a silent
no-op into a frozen UI on the exact platform being fixed. Bounding means moving
the paste path onto `command_native_async` + `mp.abort_async_command` + a timer.
That is a redesign of the paste path, not a manifest line. **The two must land
together or not at all.**

The `elseif` half (§4 item b) is the more defensible one, and codex is right
that `renderer.lua:317-319`'s "XWayland is a different clipboard" comment is too
absolute — whether the two selections interoperate is compositor-dependent, and
`clipboard.py:22-26` already tries both families unconditionally. But it is
still a change nobody on the project has run on the affected platform.

### 1.2 #737 — the cause is the leaked binding, and the site count is done

§4 defers `mpvtk_skip_click`'s leak to 3.1.0 on this ground:

> the *class* is "a forced binding whose removal site is somewhere else", and
> nobody has counted the siblings among the 17 `remove_key_binding` calls and
> their `add_forced_key_binding` partners. Count first; that window closes on
> the first patch.

**Counted.** Every `add_forced_key_binding` in `renderer.lua` paired against
every `remove_key_binding`:

| distinct names added | 14 |
|---|---|
| paired with a removal | **13** |
| unpaired | **1** — `mpvtk_skip_click`, added `renderer.lua:5934`, removed nowhere |

The one that looks unpaired and is not is `mpvtk_text` (`:2894`): it is appended
to `text_key_names` and removed by the loop at `:2938`, not by literal name. A
grep-and-pair lint must resolve that table or it will report a false positive.

**So the pairing lint of §5.6 would ship with zero exemptions**, and the reason
the leak was held back no longer holds.

**And the leak, not A4, is what fits #737.** Every link read from source:

| # | link | evidence |
|---|---|---|
| 1 | binds only when the pause button is not the left one | `renderer.lua:5933` `if not state.phud.click_pauses then` — matches *"switching back to left click to pause this behavior never occurs"* |
| 2 | arms by default | `conf.py:573-574` `segment_intro`/`segment_outro` = `"ask"`, so any server publishing MediaSegments shows a Skip button — *"usually two or three videos"* |
| 3 | never removed | `phud_skip_unbind` (`:5950-5953`) removes only `mpvtk_skip_enter` |
| 4 | after the button hides, the handler eats the click | falls to `begin-vo-dragging`, and mpv's `defaults.lua` comments the simple-binding path as *"for mouse, 'down' does nothing, 'up' runs the command"* — so the drag is issued on **release** and starts nothing. The click is consumed and nothing visible happens. |
| 5 | the forced section is re-enabled constantly | `mp.register_idle(mp.flush_keybindings)`; the flush re-runs `input_define_section` **and `input_enable_section`** for `input_forced_<script>` whenever any binding changed |
| 6 | re-enabling means "move to the top" | `input/input.c:1458-1471` disables then re-inserts; lookup runs `for (i = num_active_sections - 1; i >= 0; i--)` at `:485`. Last enabled wins. |
| 7 | the browser's mouse has no priority class of its own | `mpvtk_mouse` is an ordinary named section (`renderer.lua:4968`), and scripts cannot request `MP_INPUT_ON_TOP` — `player/command.c:7919` accepts only `default` / `exclusive` / `allow-hide-cursor` / `allow-vo-dragging` |
| 8 | and `ui_resume` cannot mask it | it enables `mpvtk_mouse` at `:5702` and then adds forced bindings at `:5725-5727`; the idle flush puts the forced section back on top afterwards |

**The renderer already knows this rule.** `ui_resume`'s own comment at
`renderer.lua:5714-5721`, explaining why the key block is installed *first*:

> **First, before every exact binding below.** The block is `any_unicode`, which
> outranks an exact key, and between two forced bindings of one key **the LATER
> one wins** -- so installing it last made it outrank the UI's own keys.

That is the spec, written down, and mpv's own header states the same thing for
sections (`input/input.h:159-165`: *"the section is appended on top ... If the
section was already active, it's moved to the top as well"*). The leak is that
rule applied to a binding nobody remembers to remove -- this repo's signature
shape, and the reason the repair in §2 is a lint and not a line.

**Net: every left click in the library is consumed for the rest of the mpv
session.** Keyboard survives — those bindings live in the same forced section
and still resolve. It survives quitting playback with `q`. Only a restart
clears it. That is #737 clause for clause, including the two clauses A4 cannot
reach: persistence, and surviving the end of playback.

**Status: the mechanism is CONFIRMED; that it is #737's cause is
PLAUSIBLE-strong, not closed.** The one unverified precondition is that a skip
segment was actually delivered to that reporter — see §6.

**The postmortem's other escalation candidate is refuted.** §3.3 names mpv's
context menu as a candidate. `player/lua/context_menu.lua` force-binds
`ANY_UNICODE`, and `input/input.c:519-520` resolves printable keyboard input
through it — so a context menu still holding input would eat `q`. The reporter
says keyboard shortcuts still work. Recorded so nobody re-runs it.

### 1.3 A4 is real, and it must not be sold as closing #724 issue 2

A4 (`renderer.lua:6020-6023`) is confirmed exactly as the postmortem describes:
`phud_bind_summon` sets `state.kb_summon = true` and returns before binding
anything when `click_pauses` is off, on the strength of a comment
(`:6012-6019`) whose premise our own pin move falsified. Both halves re-read:

```
git -C ~/Desktop/mpv-build/mpv show 41f6a645:etc/input.conf | grep MBTN_RIGHT   # cycle pause
git -C ~/Desktop/mpv-build/mpv show 182fa6ca:etc/input.conf | grep MBTN_RIGHT   # select/context-menu
```

**But `gh issue view 724` carries a comment the postmortem does not
reconcile:** *"Issue 2 is fixed in the latest development builds, issue 1
isn't."* The postmortem traces issue 2 to A4 and confirms A4 open at HEAD. Both
cannot be true.

The likeliest reconciliation — **PLAUSIBLE, offered as a hypothesis and not
claimed** — is that the #700 hover work in the same window changed whether the
HUD auto-hides under a stationary pointer, so the reported sequence no longer
reaches the state while A4 stays open behind it.

**Fix A4 for correctness. Do not put "closes #724 issue 2" in the changelog
until the reporter re-confirms.** #724 issue 1 is separately maintainer-deferred
on the issue (*"I'll have to restore it. Deferring that until after v3.0.0"*)
and is not a 3.0.1 item.

---

## 2. What the release contains

Now a 3.1.0, so the two items the postmortem could only defer — #736's real
repair and #739's bounded paste path — are **in** it rather than held. Rows 1-7
are the ones a 3.0.1 could also have carried; 8-11 are what waiting buys.

| # | change | evidence | risk |
|---|---|---|---|
| 1 | `mp.remove_key_binding('mpvtk_skip_click')` in `phud_skip_unbind` (`renderer.lua:5950`) | §1.2; leak CONFIRMED, count 1 of 14 | **Low.** Removes state; adds no branch. |
| 2 | The binding-lifetime pairing lint (postmortem §5.6) + `tests/test_no_unpaired_bindings.py` | zero exemptions needed, §1.2 | **Low**, and it is the *repair* rather than a guard: it converts "remember to unbind" from attention into a check. Must resolve `text_key_names` or it false-positives on `mpvtk_text`. |
| 3 | A4: bind `mbtn_right` at `renderer.lua:6020-6023` when `click_pauses` is off, as `:6024` binds `mbtn_left` when it is on | §1.3 | **Low-moderate.** Deletes an assumption rather than adding a guard. Does **not** touch `mbtn_left`, so the VO-dragging rationale at `:6013-6015` is untouched. It lands on the input arbiter — the surface with the worst regression record here (`do-not-fix.md` F37) — so land it alone. |
| 4 | The four wrong claims still in the repo | all CONFIRMED — see the table below | **None.** |
| 5 | Strike ISSUES's "delete the dead branch" instruction (postmortem §2 C3); write the owed `do-not-fix.md` entries for #726 and #727 item 1 | CONFIRMED absent: `grep -n '726\|727' docs/do-not-fix.md` returns nothing | **None.** |
| 6 | #736 stopgap: document `JELLYFIN_MPV_SHIM_UI_FONT` in `docs/configuration.md` — **DONE**; the reply to the issue is Izzie's to post (see the policy note below) | `pilfont.py:739`; was CONFIRMED in no `.md` file | **None.** The docs state the cost: `_env_extra` prepends the path to **every** candidate list including emoji, so the workaround trades colour emoji away. Covers Korean and Traditional Chinese too, which have the same cause. |
| 7 | #688: **verify locally rather than waiting** — [iw] asked for a retest and got no response. Linux verified live (2026-09-08: RAQM and BASIC render Arabic and Hebrew differently, so shaping is on). **Windows is BLOCKED and the block is itself the finding** — see below | `7ed41bce` is contained in the tag | **None to ship**, but it is a test task rather than a doc task, and it shares the Windows-artifact gap with #736 (§5.4): the shipped PyInstaller build has its own Pillow wheel and its own DLL set. |
| 8 | **#736 properly**: coverage-based face selection — **DONE** (`cc3660e5`, branch `fix-font-selection`) | Measured 45 us/codepoint on Linux and 7.5 us on Windows, ~20 ns memoized, verdict size-independent 8→96 px. On the VM: zh-Hans `msgothic`→`msyh`, ko →`malgun`, both with zero tofu; ja and zh-Hant unchanged. 5781 tests / 214 modules green on both platforms. | The site count was done first and came out smaller than this row assumed: `strips.py`, `banner.py`, `cast.py` and `epub/paint.py` all reach selection through `_split`'s two resolvers, so there was **one** chokepoint plus `epub/fonts.face`. Two corrections to this row's own evidence: notdef **can** be blank (`simsun.ttc`) and **can** equal the space glyph (`NotoColorEmoji.ttf`), so "never equal to the space glyph" was false and an assertion resting on it fails on stock Debian. |
| 9 | **#739**: bounded async paste (`command_native_async` + `mp.abort_async_command` + a timer) **together with** the concatenated `clip_tools()` list and `wl-clipboard` in the manifest | §1.1 | **Moderate, and indivisible.** Shipping the packaging half alone extends a present-tense hang to Flatpak users. Gated on the measurement in §6. |
| 10 | **Make `notify_updates` tri-state** (`default` / `enabled` / `disabled`), resolve `default` by platform — off inside a Flathub Flatpak, on elsewhere — and add a `CONFIG_VERSION` 6 migration | §5.3 | **Low-moderate.** The risk is the migration, not the setting: conf.json persists every key, so a stored `true` is indistinguishable between "never touched" and "explicitly wanted", and migrating it to `default` silently loses the second. Migrate a stored `false` to `disabled` so a deliberate opt-out is never stomped. |
| 10a | **Retarget the notice's action inside Flatpak** — say `flatpak update`, drop the [Open] link — for the users who switch it back on, and for hand-delivered builds marked `JMS_UPDATE_CHECK=1` | §5.3 | **Low.** Independent of 10; the banner still exists wherever it is enabled, and [Open] on a Flathub install has never been the right action. |
| 10b | **A persisted per-version skip**, beside the existing session-scoped [Dismiss] | §5.3; `has_notified` is in-memory and `_dismiss_update` only repaints — CONFIRMED | **Low.** One `str` key, `INTERNAL` in `test_docs_coverage.py`, equality not ordering. Matters most on Windows and pip, where the notice stays on by default and the every-launch nag is what drives people to disable it. |

The four wrong claims, each re-verified for this document:

| # | site | claim | check |
|---|---|---|---|
| 1 | `mpvtk_browser/config.py:766-770` | mpv changed `MBTN_RIGHT` "at 0.41" | **False.** `v0.41.0` binds `cycle pause`; the change is master-only. The fact the note asserts is right; the boundary is not. |
| 2 | `docs/ISSUES_2026-09.md:894-897` | `2035d720` "landed after pre14 was cut" | **False.** `git merge-base --is-ancestor 2035d720 v3.0.0pre14` → true. |
| 3 | `clipboard.py:41`, `mpvtk_browser/dialogs.py:1052`, `mpvtk/renderer.lua:298-299`, `tests/lua/test_renderer.lua:284` | `--clipboard-backends` defaults to `win32,mac,wayland,vo` | **False.** `win32,mac,wayland,x11,vo`, x11 enabled. One of the four sites is a test, which is why nothing caught it. |
| 4 | `CLAUDE.md` | four required dependencies | **False.** `pyproject.toml:18-29` has five; `pillow` is required since the browser replaced Tk. |

**This is the highest-value row in the release.** Three separate wrong verdicts
trace to items 1-3, and none of them carries code risk.

---

**#688 on Windows cannot be checked from the VM as it stands, and that is worth
more than the check would have been.** Measured 2026-09-08: the VM's venv has
`features.check("raqm")` **False** and no FriBiDi at all, so `ImageFont.truetype`
silently returns a Basic-layout font — right-to-left text draws unreordered and
unjoined and nothing gets GPOS kerning. That is *expected* for a bare venv
rather than a defect: Pillow's Windows wheel carries libraqm and HarfBuzz but
loads FriBiDi at runtime, and only the packaged build ships `fribidi-0.dll`
beside the exe.

The consequence is bigger than #688. **Every Windows leg of every suite has
been running without text shaping**, so a green RTL test there is not evidence
about what ships — it is Codex's "there is an owner for `pilfont.py` but none
for the shipped artifact rendering a multilingual corpus" (§4 of the postmortem
handoff) made concrete and measurable. Closing it needs a `fribidi-0.dll` on the
VM, and `tools/build_win_fribidi.py` targets MSVC through meson `--vsenv`, which
**this VM cannot run** — it has no C++ workload, the same gap that blocks the
Vulkan loader. So the options are a mingw cross-build from the Linux box, the VS
C++ workload, or testing a real PyInstaller artifact; all three are a decision
rather than a step.

## 3. Held out of the release

| item | why |
|---|---|
| **#739, entirely** | The mechanism is now half-known and the remediation is not. §4's packaging line has a hang path on the target platform (§1.1), and the bounded version is a redesign of the paste path. Nobody on the project has run any of it on KDE Wayland. See §6. |
| **#736 properly** | Any `pilfont.py` change joins the cluster that produced five of the postmortem's top ten generators. The `_load` coverage check (§5.4) is 3.1.0 work with its own site count first — and `epub/fonts.py:128-134` is a second site of the same rule. |
| **#733** | Unchanged: suppressing mpv's OSD has no owner anywhere in the tree, so it is new code plus a design decision already deferred on the issue. |
| **R8 / `fullscreen_disable`** | Unchanged: `mpvtk_browser/ui.py:368` is a second non-persist caller in the opposite direction. Two intents, one flag; the repair is a parameter. |
| **F42 / `_sync_path`** | Unchanged: `do-not-fix.md` already states why the obvious fix is wrong, and it drives a destructive store Move. |
| **The other two lints** (§5.1 frozen key literals, §5.3 `audit_act_targets`) | These are the 3.1.0 work. They are not fixes, so they carry none of the measured fix-side rot. |

---

## 4. What happens to `enrich-e2e-tests`

**Correction to the handoff.** Its list of held-back behaviour changes is
**stale**. CONFIRMED:

```
git diff --stat master...HEAD -- 'jellyfin_mpv_shim/**'   # 3 files, +66 -3
git diff master...HEAD -- jellyfin_mpv_shim/conf.py jellyfin_mpv_shim/player_window.py   # empty
```

`comic_fit = "page"` (`conf.py:668`), `show_picture` stopping music, and
`_sync_window_geometry` on picture loads are **already in `master`** — they
shipped in 3.0.0. The branch carries ~28 commits of tests, docs, the risk map
and the postmortem, plus **one** production change: the aspect-override
ownership.

That change is also not the hazard the handoff describes. The unconditional
`_play_media` write mirrors the deinterlace write directly above it, which is
already unconditional in `master`, inside its own `try`.

**But it carries two instances of the shape this whole postmortem is about, and
they are in the commit that was removing an authority.** Both CONFIRMED:

- `mpvtk_browser/gateway/hud.py:92-94` states *"Through the manager's own
  setter, not a bare attribute write: it is the one that takes the player
  lock."* **`PlayerManager.set_speed` (`player.py:4883-4884`) has no
  `@synchronous`.** True of `set_aspect`, false of `set_speed` — the right rule
  at 1 of 2 sites, asserted by a confident comment. This is precisely the
  failure CLAUDE.md's comment rules name: a wrong comment raises the reader's
  prior about the code under it.
- `clear_aspect_override` (`player.py:3474`) has no `@synchronous` either, and
  its own docstring says **"Unlike that one, this DOES write mpv."**
  `run_action`'s docstring (`player.py:2149-2150`) states the invariant it is
  breaking: *"the deferred path holds no lock, which is why targets that mutate
  the player carry their own `@synchronous`."* Whether it is harmful is **not
  established**; that it violates the stated rule is.

**Recommendation:**

1. **Merge the tests, docs, risk map and postmortem to `master` now.** The
   postmortem's own SZZ pass over `v3.0.0..HEAD` found **zero** commits touching
   a `.py`/`.lua` line the release window wrote. It is additive coverage, and it
   is what makes the next release verifiable at all.
2. **Split the aspect-override commit out, repair the two findings above, and
   land it in 3.1.0.** It is a behaviour change — an override that used to
   persist across the library door now clears — and it is not what any user is
   waiting for.

---

## 5. Packaging, Flathub and the update notifier

Not on the postmortem's list, and **the notifier is the cheapest real win
here**: it affects every Flatpak user on every release and it is smaller than
the #737 fix.

### 5.1 The Flathub lag was a MetaInfo review hold

The 3.0.0 Flathub build was slow to become installable. The gamepad permission
was the suspected cause. **It was not**, and the evidence does not depend on any
timing measurement:

- Flathub's review triggers on `finish-args` changes in the submitted manifest.
  **PR #77 ("Release 3.0.0") changed no `finish-args` at all** — its diff is the
  websocket-client bump, `pypi-dependencies.json`, `pypi-pybind11.json` and the
  `shared-modules` submodule. There was no escalation to review.
- The gamepad permission went in separately as **PR #75, merged in 22 minutes**
  on 2026-08-21, and was already live. Escalations that get gated sit for days.
- [iw] Flathub has a carveout for mature maintained projects, and this one is
  six years old, so the newer "is this AI slop" gating does not apply either.

**What held it — DETERMINED [iw].** Flathub holds a build for human review on
either of two triggers: **static permission changes**, and **critical MetaInfo
changes — app name, developer name, app summary, or license.** The 3.0.0 release
changed the summary:

```
-  <summary>Cast-only client for Jellyfin Media Server</summary>
+  <summary>MPV-Based Jellyfin Client with Offline Sync</summary>
```

CONFIRMED: `git diff v3.0.0pre14..v3.0.0 -- jellyfin_mpv_shim/integration/*.appdata.xml`.
That is the hold. It was not a bug on Flathub's end and it was not queue
latency; the gate is the **publish job**, which is the step that makes a build
installable via `flatpak install`, and it waited on a human.

**The summary change itself was right** [iw], and overdue: *"Cast-only client
for Jellyfin Media Server"* had stopped being true of a release whose headline
features are a library browser and offline sync. This is not a mistake to avoid
repeating — it is a cost to schedule.

**Release-process rule that follows:** change app name, developer name, summary
or license **out of band**, in a release of its own, never in the same one as a
version bump. The cost is not the review, it is that the review lands on the
release everyone is waiting for.

**And the reason the rule is needed at all is an observability gap, not the
policy** [iw]: nothing in the Flathub PR says "this will be held for review",
and the dashboard presents a held build in a way that reads as a flake rather
than as a queue position behind a human. A hold that announced itself would have
cost one line of patience; an unexplained one cost an overnight wait, a wrong
suspicion of the gamepad permission, and a good part of the analysis in this
section. **If any of this is worth reporting upstream, it is that** — the policy
is reasonable and the signalling is not.

### 5.2 The `x-checker-data` misconfiguration — FIXED in this tree

Flathub PR #78 proposes moving mpv to **v0.41.0**, which is a downgrade from the
current `182fa6ca` master pin.

**Cause, and it recurs on every checker run:** the source carries
`x-checker-data` with a `tag-pattern`, but the pin is a bare master **commit**.
The checker can only order *tags*, cannot place a commit among them, and so
reports the newest matching tag as an update, forever.

**Do not merge #78.** The #687 fix is `345e783643` ("vo_gpu_next: use video
colorspace for overlays only when requested", 2026-08-19); CONFIRMED **in**
`182fa6ca` and **not in** `41f6a645`. Taking it would reintroduce the HDR
overlay bug — and would mask #737's A4 symptom by restoring
`MBTN_RIGHT cycle pause`, which is the wrong reason to accept a downgrade.

**The fix is a version floor, applied to `flatpak/` in this tree** (it reaches
Flathub with the next release sync):

```json
"x-checker-data": {
    "type": "git",
    "tag-pattern": "^v([\\d.]+)$",
    "versions": { ">": "0.41.0" }
}
```

**Applied in both places, uncommitted in the second:**
`flatpak/com.github.iwalton3.jellyfin-mpv-shim.json` here, and
`~/Desktop/com.github.iwalton3.jellyfin-mpv-shim` (branch `release-3.0.0`) —
[iw] left uncommitted there because that branch needs rebasing before the next
release. The Flathub copy carries no `//mpv-pin-note`, so its floor note names
the #687 commit itself rather than pointing at a neighbour.

Silent until v0.42.0, then it opens the PR that is actually wanted — [iw] *"the
next version notification I do genuinely want to switch to when it lands"*. The
`//checker-floor-note` beside it says to raise the floor whenever the pin moves,
because the failure mode is a reader deleting the checker to stop the noise and
losing the notification with it.

### 5.3 The update notifier's ACTION is wrong, not its timing

`update_check.py:22` follows
`https://github.com/jellyfin/jellyfin-mpv-shim/releases/latest` and announces
**the GitHub tag**. There is no packaging awareness anywhere in the tree —
`grep -rn 'flatpak-info\|FLATPAK_ID\|is_flatpak' --include=*.py .` returns
nothing (CONFIRMED). So every Flatpak user gets a banner the moment a GitHub
release is pushed, for something their client will not offer for hours.

The banner is `Update available: <version>` with **[Open]** and **[Dismiss]**
(`window_chrome.py:458-467`), and [Open] calls `_open_url` on
`release_url + "latest"` — **the GitHub releases page**.

**That is the defect, and it is not the timing.** [iw] *"an app telling me there
is an update might be the only thing reminding me `flatpak update` exists"* —
on a platform where nothing updates until someone runs a command, the notice is
doing useful work. What is wrong is that its only action sends a Flatpak user to
a page whose answer is either "wait" or "download a bundle", and the bundle
strands them on a dead origin (§5.4).

**Suppressing it outright is the wrong answer**, for two reasons.
`conf.py:499 notify_updates` already exists and defaults `True`, so suppressing
by platform would silently override a preference the user can already express.
And [iw] rules out the other trigger-shaped fix — poking Flathub's API makes a
banner depend on a third party being up, which is brittle rather than careful.

**The app cannot tell the two apart by itself.** A real `/.flatpak-info` read out
of the installed 3.0.0 sandbox carries `name`, `runtime`, `app-path`,
`app-commit`, `branch`, `[Context]` — and **no origin or remote** (CONFIRMED).
There is an accidental discriminator: Flathub builds `branch=stable` while this
repo's manifest sets no branch and CI passes no `--default-branch`, so a local
bundle gets flatpak-builder's `master`. It works today, it is undeclared, and it
would break silently the first time anyone builds with `--default-branch`. Do
not rest user-visible behaviour on it.

**Corrected decision: keep the notice everywhere, and change what it says and
does inside Flatpak.**

| install | message | action |
|---|---|---|
| not Flatpak (pip, Windows, macOS) | unchanged | **[Open]** → releases page, unchanged |
| Flatpak, unmarked (i.e. Flathub) | *"Update available: X — run `flatpak update`"* | **[Dismiss]** only; no link to a page that cannot help |
| Flatpak, `JMS_UPDATE_CHECK=1` (this repo's CI/dev manifest) | unchanged | **[Open]** → releases page; this build cannot update itself and its user was handed it deliberately (§5.4) |

**[iw]'s marker decision survives intact** — Flathub's manifest stays clean, this
repo's CI/dev manifest carries `--env=JMS_UPDATE_CHECK=1` — but it is now
selecting *which advice is correct* rather than gating suppression. That is a
better job for it: the two channels genuinely need different instructions,
because `flatpak update` does nothing for a bundle install with a dead origin.

#### Decision: no delay, no API call — a tri-state setting and a migration

**[iw]: definitely do not add update-delay logic or extra API calls.** With
§5.1's cause found, the premise for them is gone: the overnight wait was an
avoidable MetaInfo review, not unpredictable publish latency, so a delay would
guard against a thing that has an owner and a fix.

**Ruled out, so it is not re-proposed:** holding the notice for two days keyed
off `published_at` from the GitHub REST API. It works — the API returns the
right date — but it adds a JSON call to a module that deliberately uses
redirect-following only, needs a fail-open/fail-closed split to stay safe next
to the persistent skip, and guards a cause that is now known. The Atom feed is
not an alternative: `releases.atom` carries **no `<published>` element at all**,
only `<updated>`, which is last-modified by definition.

**The deciding fact is about users, not mechanism [iw]: the maintainer is an
outlier.** KDE Discover notifies about Flatpak updates and GNOME Software very
likely does too, so **~95% of Flathub users are already told by their desktop**.
The reminder function §5.3 was protecting is real for the person who only ever
runs `flatpak update` by hand — and that person is rare. Against the platform
convention (users do not expect applications to notify them of updates), a
banner that duplicates the desktop's own notification is noise for almost
everyone.

The counterweight is genuine and specific to this app: it is **a client whose
compatibility depends on a moving server**, so "you are out of date" can be real
information rather than nagging. That argues for the setting existing and being
findable — not for it being on by default where the platform already covers it.

**The design [iw]: make `notify_updates` tri-state and migrate Flathub users to
off.**

| value | meaning |
|---|---|
| `default` | resolve by platform — **off** inside a Flathub Flatpak, **on** everywhere else |
| `enabled` | the user asked for it; never overridden |
| `disabled` | the user turned it off; never overridden |

Four implementation facts, all checked:

- **A migration is required, and this is why.** `SettingsBase.dict()`
  (`settings_base.py:110-128`) iterates **every** field and `conf.py:910`
  `json.dump`s the lot, so **every existing conf.json already carries
  `notify_updates: true` explicitly** (CONFIRMED). A platform-dependent default
  alone would therefore change nothing for anyone who has ever run the app. The
  migration is a `CONFIG_VERSION` 6 step, which is exactly what `conf.py:22-23`
  says that counter is for.
- **Migrate by old value, not blindly.** A stored `false` means someone
  deliberately turned it off → `disabled`. A stored `true` is indistinguishable
  between "never touched it" and "explicitly wanted it" → `default`. **That
  second case silently loses a real preference for the few who had explicitly
  enabled an already-on setting**, and there is no way to tell them apart after
  the fact. Name it in the migration comment; it is one-time and re-enabling is
  one row in Settings.
- **Type and UI need nothing new.** `str` is already in `object_types`, and
  `config.py:407` has the `[(label, value), ...]` enum mechanism. **The exact
  structural precedent is `window_controls`** (`config.py:531-536`) — a
  three-way whose first option resolves at runtime, with a comment saying so:
  *"'auto' is not a guess about the desktop, it is MPV reporting whether
  anything decorated this window."* The same sentence shape should sit on
  `default` here.
- **"Installed from Flathub" is still "in Flatpak and unmarked"**, per the
  marker in §5.3 above. The dev/CI manifest's `JMS_UPDATE_CHECK=1` keeps
  hand-delivered builds notifying, which is the case that most needs it.

**What survives from the earlier design:** the retargeted message and action for
Flatpak (say `flatpak update`, drop the [Open] link), because the setting can
still be switched on. What is dropped: the two-day delay, the REST call, and the
fail-open/fail-closed split that only existed to make an unsolicited banner
accurate. **This is the trade CLAUDE.md names** — a platform-resolved default
*removes the app's claim to be an update authority on Flatpak*, where the delay
stack was three guards mirroring state (Flathub's publish state) owned
elsewhere.

#### The same platform probe answers a second question: SteamOS → gamepad on

**[iw], recorded for 3.1.0 and deliberately not started now: do this when the
Flatpak detection and the notifier disable are done, because it is the same
probe and the same migration step.**

Jellyfin Media Player already does this detection and it is worth copying rather
than re-deriving — `jellyfin-media-player/src/system/SystemComponent.cpp`:

```cpp
QFile flatpakOsFile {"/run/host/os-release"};
if (flatpakOsFile.exists()) {
  // ... read it, then:
  if (flatpakOsFileString.contains("NAME=\"SteamOS\"")) {
```

The mechanism is the part to keep: inside a Flatpak, `/etc/os-release` describes
the *runtime*, so the host's identity is only at **`/run/host/os-release`**. That
makes it the same file the Flathub-vs-elsewhere question is already reading, so
the probe is written once and asked twice.

**The decision, and it is narrower than the notifier's:** on SteamOS, force
`input_gamepad` **on**. `conf.py:505` defaults it `False`, and a Steam Deck is a
machine where the gamepad is the only pointing device most users have — so the
default being off is simply wrong there.

- **Not tri-state.** The notifier needed three states because "on" is a real
  preference the platform default would otherwise silently overrule. Gamepad
  input is additive — enabling it takes nothing away and breaks no other input
  path — so there is nothing to protect and no `default`/`enabled`/`disabled`
  distinction to carry. A plain `bool` forced on in the migration is the whole
  change. **Do not copy the tri-state here just because it is next door.**
- **Force it in the migration**, in the same `CONFIG_VERSION` step, for the
  reason that section already establishes: `SettingsBase.dict()` writes every
  field, so every existing conf.json already carries `input_gamepad: false`
  explicitly and a changed default alone would reach nobody.
- **A gamepad capability probe is not needed for the decision** — the shim
  already has one at 0.34 ms and mpv's `sdl2-gamepad` can be disabled at build
  time, so the setting being on is not a promise that a pad is present. That is
  the existing behaviour on every other platform and needs no change here.

#### An interface language selector — [iw], recorded for later

**The mechanism already exists; the work is exposing it.** `conf.py:500` has
`lang: Optional[str] = None`, and `i18n.configure()` already prefers it over
the system locale:

```python
if settings.lang is not None:
    lang = settings.lang
else:
    lc = locale.getdefaultlocale()      # robust on Windows, unlike gettext's own
```

So a user *can* override the language today — by hand-editing `conf.json`.
`lang` is not in `config.py`'s `TAB_SECTIONS`, so nothing in the UI offers it,
which is the same "real setting, no way to reach it" shape as the `kb_*` keys.

**It requires a restart, and that is measured rather than assumed.** There are
**22 module-scope `_()` / `_p()` call sites** (`syncplay.py:21`,
`video_profile.py:32`, `users.py:36`, `menu.py:29` …). Those evaluate at
import, so re-running `configure()` live would swap the translation object and
leave those strings in the old language — a half-translated UI, which is worse
than asking for a restart. So the row belongs in `RESTART_REQUIRED`
(`config.py:299`), and that set's own docstring is the standard to meet:
*"'Requires restart' means literally nothing happened"* — true here for those
22, and the rest would repaint into the new language on the next draw, which is
the ugly middle state the flag exists to prevent.

##### What to list — measured 2026-09-08

86 locales, 1075 msgids in the template. Completeness, counting non-fuzzy
translated entries:

| completeness | locales |
|---|---|
| 95–100% | **3** — `zh_Hans`, `it`, `ca` |
| 75–95% | 2 — `pt_PT`, `pt` |
| 50–75% | 7 — `de`, `es`, `da`, `ar`, `lt`, `nl`, `en_GB` |
| **25–50%** | **41** |
| 5–25% | 19 |
| 0–5% | 14 |

**That 41-locale cluster is the jellyfin-web seed line** ([iw]: the seeded
strings "don't cover a lot of the UI"), and it is what makes a threshold the
wrong instrument: put the bar at 50% and the picker offers twelve languages,
excluding French, Russian, Polish, Japanese and most of the list a user would
actually look for. Put it at 25% and it means nothing.

So **list them all and show the number** — "Deutsch — 62%" — computed from the
`.po` files at build time and regenerated with the translations. It is honest,
it needs no arbitrary bar, and it tells a would-be translator where the gaps
are, which is the population most likely to open this menu. `window_controls`
(`config.py:532`) is the structural precedent for a three-way whose first
option resolves at runtime; here that option is **"Use the system language"**,
which is exactly what `lang = None` already means.

**Endonyms, not English names.** Someone reaching for this control is by
definition someone who cannot read the current one.

##### Where — and why further than jellyfin-web goes

[iw]: **top of the settings landing page**, because a user may be pointing a
phone camera at the screen to read it.

**jellyfin-web does not have one on its login page** — checked: the login
controllers reference no language or localization at all, and the selector
lives in Display preferences (`LabelDisplayLanguage`). The setup wizard's
`selectLanguage` is `HeaderPreferredMetadataLanguage`, i.e. metadata language,
a server setting.

**Our case is not theirs, and the difference is measured.** jellyfin-web runs
in a browser, which reports the user's language reliably. We ask Python, and on
Linux that is wrong three different ways — see below. So a user whose desktop
*is* German can still get an English UI with no indication why, and the control
that fixes it has to be findable without reading anything. That argues for the
login screen too, since a first run reaches login before settings.

##### Locale detection on Linux is unreliable — three measured faults

`i18n.configure()` uses `locale.getdefaultlocale()`, chosen because it
"supports Windows correctly" (the comment says so). On Linux it is wrong in
three separate ways, all measured on this box:

- **`LANGUAGE` is ignored.** With `LANG=en_US.UTF-8 LANGUAGE=de_DE:en` it
  answers `('en_US','UTF-8')`. `LANGUAGE` is the variable GNOME and KDE set for
  UI language, and gettext's *own* lookup honours it first.
- **`LANG` only works if the locale has been generated.** `LANG=de_DE.UTF-8`
  answers `('C','UTF-8')` on a box where only `C.utf8` and `en_US.utf8` exist —
  and generating locales is opt-in on Debian/Ubuntu and absent in most
  containers. The user's language is simply discarded.
- **It is deprecated**, with removal in **Python 3.15**
  (`DeprecationWarning` today on 3.13). This will stop working, not degrade.

With no environment at all — a systemd service — it answers `('C','UTF-8')`,
which falls back to English rather than crashing.

##### The translations have been largely inert, and that is the headline

[iw]: *"surprised no one ever reported the locale detection issues — most of
the translations were inert."* Measured, and it holds for two large
populations for two different reasons:

- **KDE, on any packaging.** Plasma splits *formats* from *translations*:
  `~/.config/plasma-localerc` carries `[Formats] LANG=...`, while the display
  language a user picks in **Region & Language** goes to `LANGUAGE` —
  `kcm_regionandlang` and both `startplasma-x11` / `startplasma-wayland`
  reference it. We ignore `LANGUAGE`, so **changing the display language on
  KDE does nothing to this app**. That is #737's reporter's platform family.
- **The Flatpak, for almost everyone.** Measured inside the shipped sandbox,
  the generated locales are `C`, `POSIX` and **twenty English variants — and
  nothing else**: `en_AG en_AU en_BW en_CA en_DK en_GB en_HK en_IE en_IL en_IN
  en_NG en_NZ en_PH en_SG en_US en_ZA en_ZM en_ZW`. No `de`, `fr`, `es`, `zh`.
  So `LANG=de_DE.UTF-8` resolves to `C` for want of a generated locale, and
  `LANGUAGE=de` is ignored — **both paths fail**, and the UI is English
  whatever the user asked for. (Flatpak *does* forward `LANGUAGE` into the
  sandbox — verified — so this is our detection and not the sandbox.)
  Locales arrive with the per-language `.Locale` extension, which is installed
  according to the user's configured languages; none is present here.

**This reframes the 25-50% translation cluster.** It is not only that the
jellyfin-web seed did not cover our UI — it is that a large share of users
could never see their own language, so nobody was moved to finish it. Fixing
detection is the input to the feedback loop, not a footnote to it.

##### The fix, and the deadline

On non-Windows, defer to gettext's own environment handling (`LANGUAGE`,
`LC_ALL`, `LC_MESSAGES`, `LANG`, in that order, and **no generated-locale
requirement**) and keep the `getdefaultlocale` path only where it earns its
keep — the Windows case the comment cites. `gettext.translation(languages=...)`
already accepts an explicit list, so the change is confined to
`i18n.configure()`.

**There is a deadline attached**: `locale.getdefaultlocale()` is removed in
**Python 3.15**. This is not drift we can absorb — it stops working. Doing it
alongside the selector means one round of translator-facing testing rather than
two.

Together that makes a localization package rather than a feature:

1. fix detection (a bug, with a Python-3.15 deadline);
2. the selector, so a user can override it when it is still wrong;
3. surface completeness in the picker, so the people most able to help can see
   where the gaps are.


### 5.4 Hand-delivered builds are a real distribution channel

**[iw] and this is the practice for user-facing issues now:** hand a CI build to
the person who logged the bug. It gives them an interim fix *and* it gives the
project **a real end-user confirmation that the fix works on the platform that
reported it**. That is not a courtesy — for three of the four issues here it is
the only exit evidence obtainable at all (§6).

**[iw] Pull the `.flatpak` files from the 3.0.0 release now that Flathub has it,
and publish bundles only for pre-releases.** The decisions compose: every
hand-delivered or pre-release build is one that cannot update itself, which is
exactly the population §5.3's marker keeps the notice for.

**[iw] And Flatpak does not auto-update everywhere** — on many setups, this
maintainer's included, nothing moves until someone runs `flatpak update`. That
is the argument *for* keeping the notice, not against it (§5.3): with no
automatic update, the banner may be the only thing that reminds anyone the
command exists. An app that silently stopped mentioning updates on the one
platform where updates are manual would be the worst of the available
designs.

**Not pursued: `--repo-url`.** `artifacts.sh:20` runs `flatpak build-bundle`
with no `--repo-url`, which is why a bundle install gets a dead origin and never
updates. Pointing it at Flathub would let a canary converge automatically —
[iw] but that needs the branch gating worked out first, *"otherwise a stable
update breaks a nightly on an unstable track"*. Recorded, not scheduled. The
zero-risk substitute is a release-notes line:
`flatpak install --reinstall flathub com.github.iwalton3.jellyfin-mpv-shim`.

---

## 6. Exit criteria

**For the release:**

1. **A failing test derived from #737's words before the fix exists.**
   `tests/lua/fake_mp.lua` models the binding registry, so a Lua test can assert
   `mpvtk_skip_click` is absent after the Skip button hides. **It cannot prove
   the routing consequence** — the fake says so itself at `:337-344`: *"a fake
   cannot model mpv's section stack, so a test can call a binding that is
   currently suspended."* So the Lua test is necessary and not sufficient.
2. **A real-mpv leg for the same property:** `click_pauses = false` → a skip
   segment arms → the button hides → return to the browser → left-click a tile.
   That is the cross-product no existing suite covers.
3. **A4 through real `MBTN_RIGHT` down/up over bare video with the HUD
   auto-hidden.** `tests/e2e/test_mouse_routing.py`'s two `MBTN_RIGHT` calls are
   at `:358` and `:456` and are both on nodes — CONFIRMED, and it is the honest
   limit the postmortem records at §5.7.
4. **The pairing lint green with zero exemptions.**
5. **A CI build to each reporter (§5.4) — Izzie's to send, per the policy
   above — which is the strongest evidence available here and the only kind for
   #739.** #737: does the freeze stop
   recurring — and, before the fix, *were Skip Intro / Credits buttons appearing
   on the episodes that triggered it?* #724: *what fixed issue 2 for you?*, since
   they report it already gone while A4 is demonstrably still open (§1.3).
   #736: does the packaged Windows build draw their library correctly.
6. Remember **CI runs no tests** — the `.github` workflows build only. A green
   tick means the artifacts compiled, which is exactly why the reporter's
   confirmation is load-bearing rather than decorative.

**#739 needs its mechanism established before a fix is written, and the CI-build
route is the cheaper instrument.** Hand the reporter a **diagnostic** build
first — not a fix — that logs:

- `current-clipboard-backend`,
- the selected VO and GPU context (native Wayland vs XWayland changes the
  answer — see §8),
- whether `wl-paste` returns or hangs.

That is better than the Plasma/Wayland VM, which reproduces *a* KDE Wayland
session rather than *theirs*; the VM stays the fallback if the reporter goes
quiet. **Do not send a fix build as the diagnostic.** A fix that happens to work
would confirm nothing about why, and §8's second open question — whether the
`vo` backend was even reachable — is precisely the thing a working fix would
hide.

---

## 7. What the other options cost

**Postmortem §4's 3.0.1 as written.** It would claim #737 closed through A4 — a
change that cannot explain the escalation, so the issue reopens — and ship a
Wayland clipboard change that extends a present-tense hang to the reporting
platform. Both are CLAUDE.md's non-convergence case: *the fix was right about
what it was told, and the target keeps moving because nobody wrote down what the
thing must guarantee.*

**A fast 3.0.1 of the safe subset.** This was the live alternative until the
cadence decision, and what it costs is now the deciding argument against it:
shipping #737 and the doc corrections quickly means shipping **#736's env-var
workaround as the answer to a garbled UI**, because the real font repair is not
a week's work done under release pressure. [iw] declined that trade explicitly.
The rot argument never reached these items either way — **both mouse defects
predate the measured window**, CONFIRMED: `git merge-base --is-ancestor
97c55819 v3.0.0pre13` → true, and the same for `3f221d41` — so the case for the
patch release rested on speed alone, and speed is what was given up.

**Two releases, a 3.0.1 then a 3.1.0.** Twice the release cost on a project
where [iw] releases are already the annoying part, for the benefit of shipping a
non-default-config input fix about a week earlier. It also doubles the exposure
to the Flathub publish behaviour in §5.1, which nobody has explained yet.

The two analyses the postmortem refuses to average both survive this decision,
and neither dominates it. The rot measurement is why the release contains a lint
and not five fixes; the envelope-mismatch reading is why #739 waits for a
measurement on the platform that reported it. They argue for different halves of
the same release.

---

## 8. What I could not determine

Recorded rather than guessed at, because this repo has been bitten three times
by verdicts resting on unverified premises.

- **Why #724 issue 2 stopped reproducing on dev builds while A4 is
  demonstrably still open.** §1.3 offers a hypothesis and does not claim it.
- **If mpv selected a native Wayland VO for #739's reporter, why the `vo`
  clipboard backend did not answer either.** That backend uses core
  `wl_data_device_manager` and does **not** need `ext-data-control-v1`, so
  §1.1's finding is a complete explanation only if mpv was on XWayland. This is
  the single largest reason #739 waits.
- **Whether a skip segment was actually delivered to #737's reporter.** The
  issue text establishes the `mouse_click_pauses` half and not this half. One
  question closes it.
- ~~What Flathub's publish job waited on for 3.0.0.~~ **Answered [iw]: a
  MetaInfo review hold, triggered by the app-summary change. See §5.1.**

---

## Appendix — method

Read-only throughout: no checkout, reset or stash, no test run, and no file in
the repository modified other than this one. Every `file:line` above was read at
`91bb3e9f`; every mpv claim was read from `~/Desktop/mpv-build/mpv` at
`41f6a645` (v0.41.0) or `182fa6ca` (the Flatpak's master pin), named at each
use. Issue text came from `gh issue view` rather than from the working notes.

A second opinion (codex, asked to refute rather than converge) ran against the
same tree. It **refuted one of my premises** — that `wl-paste` needs mpv's
`ext-data-control-v1` — and found the unbounded-subprocess hang path in §1.1
that I had missed; both are verified above from the man page and mpv's own
`subprocess` argument list rather than taken on its word. It **confirmed** the
mpv section-stack chain in §1.2 with an additional citation
(`input/input.h:159-164` documents the re-enable-moves-to-top behaviour
explicitly), and it **refuted the postmortem's context-menu candidate** on the
`ANY_UNICODE` grounds recorded there. It also correctly downgraded my
attribution of #737 from closed to leading, which §1.2 reflects.

**Refuted during the round, recorded so none of it is re-proposed.** That
`wl-paste` needs mpv's `ext-data-control-v1` (it falls back to a core-protocol
surface, so shipping `wl-clipboard` can work where mpv's backend cannot). That
the Atom feed can date a release (it has no `<published>`). That the GitHub
release could be held as a pre-release until Flathub publishes (PyPI has to ship
before the Flathub build can be updated, so the release is not the last link).
That mpv's context menu explains #737's escalation (it force-binds
`ANY_UNICODE`, which would eat `q`, and the reporter's keyboard works). And that
`begin-vo-dragging` would visibly drag the window — it fires on mouse-up, so the
click is merely eaten, which fits the report better.

Two claims I checked and did not carry forward: that `begin-vo-dragging` would
visibly drag the window (it fires on mouse-up, so it does not — the click is
merely eaten, which fits the report better); and that concatenating
`clip_tools()`'s lists is refuted by the "different clipboard" comment at
`renderer.lua:317-319` (that comment is too absolute, and `clipboard.py:22-26`
is the same rule's other implementation, written in the same commit, that does
not make the distinction).
