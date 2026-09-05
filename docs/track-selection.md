# Track selection

Which audio and subtitle track a queue item plays with. Four things decide it,
they live in three modules, and the order between them is where every bug in
this area has been.

Read this before touching `resolve_tracks_for_negotiation`, `map_streams`,
`_apply_remembered_tracks`, or `configure_streams`. The tables are enforced by
`tests/test_track_truth_table.py`; that file is the contract and this one is
the reasoning.

## 1. The pipeline

`PlayerManager.play(video, offset, is_initial_play=False, apply_memory=True)`:

| Step | Where | Does |
|------|-------|------|
| 1 | `video.resolve_tracks_for_negotiation()` — `media.py`, `sync/offline_media.py` | applies `language_config`, sets `_tracks_resolved` |
| 2 | `_apply_remembered_tracks(video)` — `player.py` | applies the previous item's track over the rule |
| 3 | `video.get_playback_url()` → `map_streams()` | re-applies the rule **only if step 1 did not run**, then fills in the source defaults |
| 4 | `configure_streams()` — `player.py` | writes the result to mpv |
| 5 | `_capture_track_memory(video)` — `player.py` | stores `(media_source, aid, sid)` for the next item |

Two things about the order that are load-bearing and look arbitrary:

- **Steps 1 and 2 both precede the negotiation.** `get_playback_url` posts
  `aid`/`sid` to PlaybackInfo and the server *bakes the audio index into
  `TranscodingUrl`*, so a track settled after that call cannot be heard on a
  transcode — while the HUD and the progress report happily name it.
  `configure_streams` cannot repair that: it skips audio on a transcode, and
  correctly, because the audio is already encoded into the stream.
- **The rule is step 1 and the memory is step 2**, so memory wins. That
  precedence used to come for free (the rule ran inside `map_streams`, after
  the memory) and now has to be kept deliberately. `_tracks_resolved` is what
  stops step 3 running the rule a second time and inverting it.

`is_initial_play=True` clears the memory rather than applying it, so step 2 is
a no-op for the first item of a queue. `apply_memory=False` does the same for a
restart (a quality change, a forced transcode), where the tracks on the object
are already the answer.

## 2. Audio

Highest wins. `_resolve` in the test file walks exactly this.

| # | Source | Applies when |
|---|--------|--------------|
| 1 | An explicit pick | `explicit_tracks` — the browser's own pickers, `gateway/playback.py` |
| 2 | The previous item's track | memory present, `remember_audio_track`, `prev_aid is not None`, and `_rank_stream` scores ≥ 3 |
| 3 | `language_config` | a rule matched and named audio |
| 4 | `DefaultAudioStreamIndex` | filled in by `map_streams`, **after** the negotiation |
| 5 | nothing | `aid` stays `None`; mpv keeps its own default track |

Row 4 is deliberately not resolved before the negotiation: posting no index is
what makes Jellyfin apply `DefaultAudioStreamIndex` itself, so client and
server agree. Resolving it client-side and posting it would be a behaviour
change. This is the one place where "what PlaybackInfo was asked for" and
"what `video.aid` ends at" legitimately differ, and only in that direction.

## 3. Subtitles

The same chain with one extra row, and that row is the whole difference.

| # | Source | Applies when |
|---|--------|--------------|
| 1 | An explicit pick | `explicit_tracks`, including `sid = -1` for "no subtitles" |
| 2 | A remembered **off** | memory present, `remember_subtitle_track`, `prev_sid == -1` |
| 3 | The previous item's track | as above with `prev_sid` a real index and `_rank_stream` scoring ≥ 3 |
| 4 | `language_config` | a rule matched and named a subtitle |
| 5 | `DefaultSubtitleStreamIndex` | filled in by `map_streams` |
| 6 | nothing | `sid` stays `None`; `configure_streams` sets `sub = "no"` |

### `-1` and `None` are different memories

This is the distinction the whole subtitle table turns on, and it has no line
of its own to sit on — it is a property of a value that is passed through four
functions.

- **`-1` is a decision.** It is what the OSD menu's "None" entry (`menu.py`)
  and the HUD's "Off" entry (`osc_bridge.py`) send. Somebody switched
  subtitles off, and carrying that to the next episode is the point of
  `remember_subtitle_track`.
- **`None` is an absence.** It is what `video.sid` holds when nothing ever
  resolved a subtitle: the item had no subtitle track, or no rule matched and
  the source named no default. Nobody decided anything.

`_apply_remembered_tracks` treated them as one for the life of the feature and
forced `-1` for both. Row 2 therefore fired on absence, overwriting row 4 —
so **one episode in a season with no subtitle track turned subtitles off for
every episode after it**, which is exactly the per-episode fiddling the
Subbed/Dubbed presets exist to stop. The audio branch beside it has always
guarded on `prev_aid is not None`; this was that guard, missing on the side
where the sentinel made its absence look intentional.

The symptom had already been patched one layer away without the cause being
found: `get_playback_url` pins `MediaSourceId` only for an index `>= 0`, and
its comment says why — "the remembered track sets it on every advance whose
previous item had subs off, so keying off 'not None' pinned `MediaSources[0]`
for almost every play". That "almost every play" was this bug, measured and
worked around at the call site it happened to break.

## 4. Two implementations

`OfflineVideo` reimplements steps 1 and 3: there is no PlaybackInfo to get
ahead of and no sidecar URL to build from a `DeliveryUrl`. It is a second copy
of a precedence chain, which is where a precedence stops being the same, so
`TheTwoImplementationsAgreeTest` runs both tables — the same
`AUDIO_CASES`/`SUBTITLE_CASES` lists, not a paraphrase of them — against it as
well. (It ran three hand-written cases while saying it ran the tables, which
is the whole justification for the class, so the claim mattered more than the
gap: the behaviour agreed on all 19 rows once they were actually run.)

Note that offline, step 1 resolves against `media_source or _source` (the
downloaded source, from `source_json`) while step 2 ranks against
`source_for_track_rules()` (the item manifest, from `item_json`). Both carry
the server's `Index` values so they agree, but they are two different objects
answering one question — worth knowing before adding a third caller.

### One edge the table does not cover

`resolve_tracks_for_negotiation` returns **without** setting
`_tracks_resolved` when `source_for_track_rules()` answers nothing. Step 3
then sees the flag clear, re-runs the rule against the negotiated source, and
overwrites the memory — the inversion the flag exists to prevent, in the one
case where the flag was never set.

Reaching it needs an item whose `MediaSources` is empty on `get_item` and
non-empty from PlaybackInfo. Probing 400 items on the QA server found none:
`TvChannel` carries its sources, and the only empty ones were `Photo` (which
short-circuits before `map_streams`) and `Season` (not playable). So this is
recorded rather than fixed — the obvious repair, setting the flag before the
early return, makes `language_config` permanently inert on such an item
instead, and choosing between two behaviours for a case nobody can produce is
a guess either way. Whoever finds a real trigger should move the predicate
rather than harden it in place: what step 3 needs to know is "has anything
decided these yet", and `_tracks_resolved` only answers "did the rule run".

## 5. How a deliberate pick reaches the next episode

`explicit_tracks` is checked by three of the four steps — both
`resolve_tracks_for_negotiation`s and both `map_streams`s — and **not** by
`_apply_remembered_tracks`. That asymmetry is deliberate.

`Media.get_next` does not forward the flag, and should not. The flag travels
with the raw stream index the pick was made on, and a stream index means
nothing without the source it indexes into: an episode with one extra audio
track renumbers everything after it. Forwarding it carries a stale number.

The memory carries the *choice* instead, and `_rank_stream` re-matches it
against this item's own streams by language, title, codec and position. That
is a better statement of what the user meant than either the index or the
server's session memory, so the memory step is allowed to run over an explicit
pick rather than deferring to it.

What protects a pick on the item it was actually made for is the caller, not a
guard in this chain:

| The pick | What protects it |
|----------|------------------|
| chosen in the browser, then played | `is_initial_play=True` clears the memory |
| chosen mid-playback (`set_streams`) | `restart_playback` passes `apply_memory=False` |
| chosen last episode | nothing needs to — the memory *is* the mechanism |

`TheHeuristicCarriesAPickForwardTest` pins this with the previous item's audio
streams in the opposite order, so an index carried verbatim lands on the wrong
language and only a re-match can pass. It runs three advances, because a
re-match that drifts one position per episode is invisible in one step.

## 6. What a test in this area has to assert

- **The negotiated value, not `video.aid` afterwards.** Asserting the object
  is what the coverage before `tests/test_track_negotiation.py` did, and it is
  precisely the assertion an ordering bug passes: both ordering bugs above
  left the object correct and the stream wrong.
- **`remember_subtitle_track` on.** Every memory test written before the truth
  table set it `False`, which is why row 2 was untested for the life of the
  feature — the setting that exposes it was off in every test that could have.
- **Both implementations.** See section 4.
