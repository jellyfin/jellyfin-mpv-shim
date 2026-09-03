# Live TV

`live_tv.py`, `guide_view.py`, `mpvtk_browser/pages/livetv.py`,
`gateway/livetv.py`, `livetv_dialogs.py`.

jellyfin-web's six Live TV screens — Programs, Guide, Channels, Recordings,
Schedule, Series — as **one tabbed page** rather than six controllers, plus two
routes of their own: a `program` page carrying the Record buttons and a
`channel` page carrying that channel's listings.

This file is the reference for the subsystem. The facts about how the *server*
behaves (time formats, window bounds, which query parameter is a column
predicate and which is a tag filter, which `Fields` value a program list needs)
are in `docs/jellyfin-api-notes.md` and are cited from here rather than
repeated.

## 1. Shape and the read/write split

- `live_tv.py` is **pure** — no network, no widgets. The parts that are easy to
  get subtly wrong (timezones, cell widths, the "which recording state is this
  in" ladder) are therefore testable without a server. It reads exactly one
  setting, and not directly: `fmt_time` defers to `mpvtk_browser/timefmt.py`
  for whether the clock is 12- or 24-hour, because the "Ends at" labels on a
  detail page and on the player controls have to agree with the guide.
- `guide_view.py` draws the grid. It is a component in the `components/` sense
  (render resources and callbacks, no `nav`/`source`/`route`) but its own
  module, because nothing else draws anything like it and a 200-line grid next
  to `busy()` and `action_btn()` would swamp them.
- `pages/livetv.py` holds `LiveTvPage` (the six tabs, one `_load_*` /
  `_render_*` pair each), `ProgramPage` and `ChannelPage`.
- `livetv_dialogs.py` holds the two modals: the timer/series editor and the
  guide settings. Its own mixin rather than more of `dialogs.py` — they are a
  self-contained feature sharing nothing with the add-to picker or the download
  dialog beyond `_show_dialog`. **Both edit somebody else's document** (the
  DisplayPreferences jellyfin-web reads; a timer belonging to the server's
  DVR), so neither writes anything until Save and both re-read rather than
  assuming their edit landed.

**Reads live in `repository.LibrarySource`; mutations live in
`gateway/livetv.py`.** Same split as playlist editing and for the same reason:
the repository is the seam an offline source stands in for, and **an offline
source cannot schedule a recording.**

**Every method in the gateway RAISES on failure.** These are all button
presses, and a swallowed error looks exactly like a recording that was
scheduled — the worst failure mode this feature has, because you find out when
the programme did not record. `ItemActions.edit` is what callers wrap them in;
it rolls the optimistic state back and says so.

Two gateway details worth keeping:

- `create_timer` is **two calls, not one**. The server's `Timers/Defaults`
  fills in padding, keep-until and priority from *its* configuration as well as
  the programme's channel and times. Hand-building the DTO silently ignores the
  user's DVR settings — which is how a recording ends up with no pre-roll on a
  server configured for two minutes of it.
- `update_timer` / `update_series_timer` are **read-modify-write**: the
  endpoint replaces the whole DTO, so a payload carrying only the two padding
  fields would blank the channel and the times with it.

`live_tv_apis()` probes whether the installed apiclient can schedule at all,
and checks **every** method the mixin calls including the two the editor's
read-modify-write needs — a probe that clears while `update_timer` would still
`AttributeError` is not a probe. On an apiclient too old, the Record
affordances hide entirely rather than rendering and failing; browsing the guide
still works.

## 2. Live TV routes re-read themselves; nothing else in the library does

A programme ends, a recording starts, another client sets a timer. This is the
one screen a **third party** changes while you are looking at it, and a stale
Schedule tab is not cosmetic — it is the screen telling you something will be
taped when it will not.

`LIVE_KINDS = {"livetv", "channel", "program"}` in `app.py` is the set.

**Three triggers, and all three are *loads*, not reloads** (`nav.load` /
`_load_route`): no epoch bump, so nothing in flight is cancelled, and every
Live TV loader writes its result in place, so the screen keeps its data until
the new data lands rather than blinking a spinner over what you are reading.

1. **Returning to a cached tab.** `_tab_cache` still paints instantly — that is
   what stops a Guide → Channels → Guide flip paying for the guide fetch twice
   — but it is no longer the last word. Serving the cache and stopping there is
   how the Schedule tab came back without an in-progress recording that had
   begun while the screen was up.
2. **`_poll_live_tv`**, started from the render path and `restartable` like the
   logs tail: the thread only notices the route has changed on its next tick,
   so leaving and coming straight back would otherwise find the slot taken and
   leave nobody polling. `LIVE_POLL_SECS = 120` — jellyfin-web's own staleness
   guard is five minutes, but it re-renders on every tab change and this screen
   is often left sitting on the Guide, so a two-minute floor keeps "on now"
   meaning now without making the guide fetch a background job.
3. **The four timer websocket events** — `EventHandler.LIVE_TV_EVENTS` =
   `TimerCreated`, `TimerCancelled`, `SeriesTimerCreated`,
   `SeriesTimerCancelled`, the set jellyfin-web subscribes to — via
   `eventHandler.live_tv_changed` → `MpvtkBrowser.refresh_live_tv`.

**There is deliberately no "recording started" event: the server has no such
message.** That is why polling is not redundant with the socket.

### A refresh nobody asked for has to be invisible

`refresh_live_tv` runs off the websocket thread and off the poller, so it must
be safe off the loop thread and cheap when it does not apply. It **defers,
never cancels**:

- `self._menu` or `self._dialog` is up — the user is acting on what is on
  screen.
- `route["_loading"]` marks a page-in in flight. `Paginator.more` computes its
  merge against the list length **at submit time**, so replacing the list under
  it would duplicate a page or skip one — permanently, since `len >= total`
  then ends the list early.
- `route["_refreshing"]` is the refresh's own guard, so a scroll landing
  *after* the refresh was submitted cannot page in against a list the refresh
  is about to replace. The deferral is symmetric; either direction alone leaves
  the race. `_route_async` clears the flag however the load ends.

Skipping costs at most one poll interval.

**Scroll survives** for two reasons together: the container id does not change,
and the renderer applies a parked `off0` only to a container it has no offset
for yet (pinned by `tests/lua/test_renderer.lua`, "off0 yanked the user back on
a later frame").

**`_load_channels` re-reads `max(CHANNEL_PAGE, len(_data))`** for the same
reason: the tab pages in on scroll, and asking for one page would drop every
page after it out from under a scroll already past them — the renderer clamps
to the shorter content and the list jumps to the top. `max()`, because the
initial load has nothing yet.

The generic half of this pattern (Home does it too) is
`docs/browser-shell.md` section 4.

### The Guide re-seeds its window

`_reseed_window` follows the clock unless the user has paged away from it.
Every one of the three triggers re-fetched the *same* window, so "on now"
stopped being now as soon as the clock left the first column — within the hour
on a narrow window — and after four hours the grid was entirely aired data.
jellyfin-web clears its `currentDate` before an auto-reload for exactly this
reason.

The converse is what makes it safe, and it is why this is a **flag**
(`_start_pinned`) rather than a staleness test: once the arrows have moved the
window it is the user's, and a background refresh that yanks them back to now
while they are reading tomorrow evening is worse than a stale grid. The Now
button hands it back.

## 3. A channel tile and a guide channel cell are LINKS, not play buttons

Both open `ChannelPage`. jellyfin-web routes a `TvChannel` to `#/details`, and
its `guide-channelHeaderCell` is a button with `data-action="link"`.

Tuning in is the page's first button and the tile's context menu. It used to be
the *only* thing either gesture could do, which left no way at all to see what
was on a channel later without going back out to the guide and finding the row
again.

`ChannelPage` is jellyfin-web's `renderChannelGuide`: **`HasAired=False` sorted
by start**, which is "has not finished yet" rather than "has not started" — so
the first row is what is on air, not what is on next. That is also why the
header does not repeat it: `_now_playing_program` draws the current programme
in the header *only* while the listing is still loading, from the `CurrentProgram`
the seeding tile carries (the ordinary item endpoint the channel is re-fetched
from does not populate it).

`ProgramPage` is jellyfin-web's `recordingcreator` dialog as a **page**, and
`ChannelPage` is a page for the same reason: Watch lives on both, and a modal
that starts playback has to tear itself down over a window it no longer owns.
Both seed from the tile that was clicked so they draw instantly, then replace
it with the authoritative DTO — which is the only one carrying live
`TimerId`/`SeriesTimerId`/`Status`. `ProgramPage._refresh` re-reads after a
recording change rather than flipping optimistically: the server decides
whether a "record this" became a series rule, what the timer's id is, and
whether the request was honoured at all, and every one of those changes which
buttons the page should show.

## 4. The guide window is paged, not scrolled

jellyfin-web renders a full 24-hour grid and scrolls it horizontally, with the
channel column and time header scroll-synced by hand. Two things make that the
wrong shape here: mpvtk's scroll containers are owned by the renderer (Python
is told about a scroll after the fact, debounced), so keeping three in step
would mean a Python round trip in the wheel path and visible tearing; and this
UI is driven as often by a remote as by a mouse, where "page forward two hours"
is a better verb than "scroll right".

So the grid draws one window — `MIN_CELLS = 2` to `MAX_CELLS = 8` thirty-minute
columns, two hours by default, fewer on a narrow surface (`MIN_CELL_W = 120`,
below which a programme name has no room) — and the page moves it.

**The fetch always covers `MAX_CELLS`, however many columns are drawn.** The
visible width is a render-time decision; fetching only the visible columns
would make every resize a new guide request, *including one from inside
`build()`, which would reload the route mid-render*. Drawing fewer columns from
a wider fetch costs nothing: `row_segments` clips.

**Cell widths come from cumulative pixel positions**, differenced, so they sum
to exactly the grid width. Rounding each duration independently accumulates
error and leaves the last cell one or two px short of the edge, which on a row
of fixed-width boxes reads as a ragged right margin. For the same reason the
Row carries **no gap** — a gap would add to that sum and slide every cell right
of the first out from under its time-header column, so `_slot` gives each cell
its full computed width and paints a slightly narrower box inside it. The time
header is built from the same `row_segments` maths (one pseudo-programme per
slot) so the two cannot drift: laying the header out by dividing evenly and the
cells out by clipping is exactly how a guide ends up with the 20:30 label over
the 20:00 column.

Other grid facts:

- Rows are **virtualized** against the scroll — a screen either side of the
  viewport. A 900-channel line-up is 900 rows, and an art cell composites a
  bitmap into the strip cache as it is *built*, so building one per channel
  would evict the rows actually on screen.
- Dead air is drawn as a recessed box, not skipped, so the row still spans the
  window and the cells after it stay aligned.
- A cell narrower than `TEXT_MIN_W = 34` gets no text (an ellipsis in a 10px
  filler is worse than nothing); narrower than `SUBLINE_MIN_W = 150` gets no
  second line.
- `CHANNEL_W = 218`. Widened from 168, where the label got 114px after the logo
  and padding and anything past about "Channel 4 +1" was cut. It costs the grid
  no columns — the window is a whole number of cells capped at `MAX_CELLS`, so
  the extra 50px makes each cell 5–7px narrower from 800px up.
- `floor_to_cell` rounds **down**, not to-nearest: the window has to contain
  what is on *now*, and rounding 10:29 up to 10:30 would open the guide on the
  next programme while the current one is still airing.
- `clamp_window` keeps the start inside what `LiveTv/GuideInfo` says the
  provider has; an empty or unparsable one imposes no limit, because a guide
  the shim refuses to page through is worse than one that pages into an empty
  day. The `cells` count **travels with the arrow press**: the clamp is in
  units of drawn columns, and clamping a two-column window as if it were eight
  stops the arrows up to three windows early.

## 5. Scroll snapping: section tops vs row pitch

The stacked-carousel tabs (Programs, Recordings, Schedule) scroll-snap to
**section tops** via `components.section_offsets`, like the home screen.
Explicit content-y breakpoints rather than a uniform pitch, because
auto-shaped rows differ in height — an auto-shaped poster row is half as tall
again as a landscape one, so a fixed step drifts out of alignment within two
sections. These tabs are bitmap-heavy (one composited strip per section) and
long (Programs is six carousels, Schedule one per day), so landing between two
rows with a caption band across the top of the window is the state alignment
avoids.

**The Guide keeps the cheaper uniform row-pitch snap**: its rows are all
`ROW_H` (62, `GAP` 3), so `snap=ROW_H + GAP` is exact.

`ChannelPage` windows its day sections rather than snapping: headings are
always drawn (one node, at most a couple of dozen — cheaper than the arithmetic
to place a heading-shaped hole), and only the row blocks, whose height is
exactly `n * ROW_H + (n - 1) * ROW_GAP`, get a measured stand-in.

## 6. Row shape follows the artwork, per row

`TileRenderer.auto_geom` is jellyfin-web's `cardBuilder.setCardData`: the
**median** `PrimaryImageAspectRatio` across a row picks one shape for the whole
row.

- `>= 1.33` → landscape, `image_type="Thumb"` (jellyfin-web's
  `preferThumb: 'auto'` — thumbs only where the row came out landscape)
- `> 0.8` → square
- otherwise → poster

The **median, not the mean**: one oddly-shaped entry in a row of twenty must
not reshape the row. **Per row rather than per tile** because a strip is
composited at one tile size. Items with no ratio at all fall back to the
caller's default, which for Live TV is landscape — most guide entries have no
art of their own.

This is why a row of films comes out as posters and a row of guide stills does
not, on the same screen. **Only the Live TV rows use it**; the other home rows
keep their collection-type classification.

One exception, which jellyfin-web also makes: **Upcoming Movies is pinned to
portrait** with preferThumb off (`livetvsuggested.js:87-91`) rather than letting
the artwork decide. Films are the one guide category that reliably carries
poster art, and a median over a handful of them lands on landscape often enough
to make the row inconsistent between refreshes. It still goes through
`caption_geom`, because it is the one poster-width listing row that would not
otherwise be asked whether its air times need a third line — they do, at 150px.

## 7. Recording state: "which icon" and "what does the button do" are different questions

`live_tv.timer_state(item)` answers **which symbol to draw**. It is
jellyfin-web's `getTimerIndicator` with one addition (`InProgress` split out
from `"timer"`, because the tile paints it differently), and returns one of
`None`, `"timer"`, `"recording"`, `"series"`, `"series_inactive"`. It returns
`"series"` for a showing covered by a series rule **even when that showing also
has its own timer**.

`live_tv.single_timer_state(item)` answers **what a Record button should do**.
It is jellyfin-web's `recordingfields` test — `program.TimerId &&
program.Status !== 'Cancelled'` — which never consults `SeriesTimerId` at all.

Driving the button off `timer_state` left every episode of a series you are
recording offering "Record" (a second timer for a programme that already had
one), with no way to skip one showing and no way to stop one in progress. The
two questions are independent in jellyfin-web and a programme can legitimately
answer yes to both.

A third, also deliberately separate: **`is_recording_now`** answers "what
colour". A programme covered by a series rule and airing right now *is* being
recorded, but its `timer_state` is `"series"` — so keying the colour off the
state left every series-recorded programme with an ordinary blue progress bar.
`_recording` is the stamp `recordings_page` puts on results from an
`is_in_progress` query, because a recording DTO carries no timer state of its
own.

A cancelled single timer reads as no timer at all. On a *program* DTO the
server never emits one anyway — `AddRecordingInfo` sets `TimerId` only when the
status is neither Cancelled nor Error — so that branch is reached only by a
`Type == "Timer"` DTO, where jellyfin-web would draw the dot and we would
rather not claim a cancelled timer is recording.

## 8. Preferences are jellyfin-web's, in jellyfin-web's document

`livetv-channelorder`, `livetv-favoritechannelsattop`,
`guide-colorcodedbackgrounds` and `guide-indicator-*` are CustomPrefs keys in
the same DisplayPreferences document the home layout uses. Values are the
**strings** `"true"`/`"false"`. Saving is read-modify-write of the whole DTO.
The wire details are `docs/jellyfin-api-notes.md` section 7.

That is not incidental compatibility: a user who sorts their guide by channel
number in the web client expects the same order here, and writing these
anywhere else would give them two settings with one name. It is also why the
guide's settings dialog saves through the repository rather than into
`conf.settings`.

`INDICATOR_DEFAULTS` follows what `guide.js` actually *draws* rather than what
its settings dialog shows: the dialog gives "new" no `data-default` so it
renders unchecked, while the guide tests `!== 'false'` and draws it. Both sides
here read the one table rather than reproducing a disagreement between a
checkbox and the screen it controls.

### Guide prefs are adopted on the loop thread

`cache_live_tv_prefs` is called from `_guide_save` **before** `on_save`.

Saving the guide settings repaints the guide; repainting means re-fetching it,
and that fetch is a pool job whose first act is `get_live_tv_prefs`. That job
is submitted *first*, so no amount of care inside the save worker wins the
race: by the time the save runs, the reload has already read the old cache and
the guide comes back drawn with the settings the user just changed away from.
So the cache moves on the thread that ordered both, where there is no race to
lose. A dict assignment, which is why that is safe.

The persist itself is fire-and-forget on the pool, because these are view
settings and making the guide wait on a DisplayPreferences round trip to redraw
would be a second of nothing happening. `save_live_tv_prefs` adopts the cache
before the write and **rolls it back if the write fails** — the alternative is
a cache that disagrees with the server for the rest of the session.

## 9. Categories and indicators

**Categories filter the channel list, never the programmes.**
`live_tv.category_kwargs` goes to `get_channels`; the guide's cells are emptied
in place by `program_displayed` (jellyfin-web's `displayInnerContent`). The
server-side reason — `IsMovie` is a column predicate while
`IsSports`/`IsNews`/`IsKids` become a tag filter, so two categories AND
together and the guide comes back empty — is
`docs/jellyfin-api-notes.md` section 9.3.

The drawing reason stands on its own: the cell keeps its size, its place in the
row and its click, and only its *contents* are suppressed. Dropping the
programmes instead turns a filtered guide into a field of dead air with no way
to tell a filtered showing from a hole in the listings.

`CATEGORY_FLAGS` is spelled out rather than derived as `"is_" + name` because
exactly one of the four is not its own name (`movies` → `is_movie`, singular).
Deriving it produced `is_movies` and every category-filtered fetch raised
`TypeError` before reaching the server.

Empty **or all four** means "no filter at all", which is what jellyfin-web
sends and is not the same as passing all four.

**One deliberate divergence.** Zero boxes ticked reads as "no filter" here and
as "hide everything" in jellyfin-web, which always appends an "all" sentinel so
the empty selection is still a selection. Its version draws a grid of blank
cells and offers no way back except the settings dialog; a filter that hides
its own escape hatch is not worth reproducing.

`CATEGORY_ORDER` is the one ladder jellyfin-web checks a programme against —
first match decides both the colour-coding class and which checkbox governs it,
checked kids → sports → news → movie, so a kids' sports programme reads as
kids. Its last two columns differ for exactly one entry (the checkbox is
"movies", the colour class is "movie"), which is why both are stored rather
than one derived from the other.

`program_indicators` returns **one** badge, not several: jellyfin-web picks the
first that applies in Live → Premiere → New → Repeat order, and a cell is 120px
wide. Both the New and Repeat tests check `IsSeries`, as jellyfin-web does —
"Repeat" is a statement about an episode, and a film shown twice is not one.

Categories are **session state** on the route, like jellyfin-web's
`categoryOptions`: a way to look through a big line-up, not a preference worth
persisting. The Channels tab's Favorites toggle is the same (jellyfin-web's
filter lives in that tab's query object and is gone when you leave);
`favorites_first` in the guide settings is the durable half and a different
question.

## 10. Permissions

**Every tab, to every user who can see Live TV at all.**
`EnableLiveTvManagement` gates *changing* the DVR, not reading it. Schedule and
Series used to be hidden without it, which took away information the user is
entitled to — what is going to record, and which series rules exist, are things
a household member wants to know whether or not they may change them. The
server agrees: `/LiveTv/Timers` and `/LiveTv/SeriesTimers` answer 200 for an
account with no management permission (measured against a real server; the 403
is on the writes). jellyfin-web draws the same conclusion — its `getTabs`
consults no policy and it gates the mutating context-menu entries instead.

Actions are gated: the Record buttons via `ItemActions.can_record`, and the
timer editor opens read-only. `can_record` asks two independent questions — can
this apiclient schedule at all (`live_tv_apis`, cached, since it is a question
about imported code), and may this user on this server
(`EnableLiveTvManagement`, not cached here because it is per server and the
client already caches it). It **fails open** in both, exactly as `can_edit`
does: only a probe that positively answers False hides anything, and the API
call is the real check.

## 11. Transparent channel artwork

Channel logos arrive as artwork on a transparent background and are *dark* ink
drawn for the white page every other client puts them on, which is the opposite
convention from a film's white Logo artwork. `live_tv.is_channel_artwork` is
the line between the two settings (`logo_legibility_live_tv`, default on;
`logo_legibility_library`, default off — #637). It answers yes for all four
Live TV DTO types, not just the two in `repository.LIVE_TYPES`:

- `TvChannel` wears the logo itself.
- `Program` mostly has no artwork of its own, so the channel logo is the whole
  fallback.
- `Timer` has **neither** `ImageTags` nor `ParentPrimaryImage*`, so it always
  falls through to the channel-logo branch of `image_spec` — which is what
  jellyfin-web's schedule shows too, via `showChannelLogo`. Leaving it out made
  the Schedule tab the one Live TV screen that drew its channel logos unplated.
- `SeriesTimer` reaches the same branch whenever the series the rule was made
  from has no poster. One that *does* resolves to the series poster and gets
  asked the Live TV question about it; that imprecision is deliberate, since a
  poster is opaque, `plate_for` returns `None` for it, and no plate rule applies
  either way.

A **finished recording is not included** and does not need to be: the server
hands it back as a Movie/Episode/Video wearing its own art, and
`recordings_page` never asks for `ChannelImage`, so it cannot reach the
channel-logo branch at all.

The plating machinery itself (`imageutil.plate_for`, the edge-histogram drop
shadow, `StripStore.retag`) is in `docs/artwork-pipeline.md`.

## 12. Odds and ends worth not rediscovering

- **`live_tv.parse_time` is not cheap** — it exists precisely because the
  shorter ways of parsing a Jellyfin timestamp are wrong (see
  `docs/jellyfin-api-notes.md` section 9.1). `ChannelPage._groups` caches the
  day grouping keyed on the programme list's *identity*, because re-grouping a
  thousand programmes on every repaint was the whole of the residual cost once
  the rows themselves were windowed.
- **`group_by_day` builds consecutive runs, not a dict.** The caller's list is
  already in start order and that order *is* the grouping. Bucketing by label
  would put a channel's Thursday showings under Wednesday's heading if the list
  ever came back unsorted — the failure that looks like data loss.
- **`channel_number` reads two spellings.** `LiveTv/Channels` answers with
  `Number` and the ordinary item endpoint with `ChannelNumber`, so a page that
  reaches a channel by id sees the other one from the tile that linked to it.
- **`program_title` shows `Name` with `EpisodeTitle` under it.** Guide data
  puts the *series* in `Name` and the episode in `EpisodeTitle`, the opposite of
  a library Episode — so the ordinary tile labelling would show every showing of
  a series as the same string.
- **`get_timers` sorts client-side.** `LiveTv/Timers` takes no sort arguments
  and the screen groups by day, which only reads as a schedule in time order.
- **`get_program_sections` fans out.** Six independent requests walked serially
  is six round trips before the screen can draw anything, and guide queries are
  not fast. Order is preserved by collecting in submit order; one failed row
  costs only that row; empty rows are dropped, so a provider with no sports data
  has no Sports row rather than an empty heading.
- **`search_live_tv` is two requests, not jellyfin-web's seven.** It splits
  programmes into movies/episodes/sports/kids/news/other so each row can have
  its own card shape, then draws them all the same. One Programs row is the same
  information for a fifth of the traffic. It never raises — a search that half
  worked beats a search screen reporting failure because the tuner was busy.
- **A guide cell always opens the program page**, even for something airing
  now: it is one click from there to Watch, and it is the only place Record
  lives. jellyfin-web makes the same split (a dialog on desktop; immediate
  playback only in its TV layout, where there is nowhere to put the recording
  controls).
- **The keep-up-to dropdown offers round numbers plus whatever the rule is
  actually set to.** jellyfin-web offers every integer 0–50; a 51-row dropdown
  does not fit over a 720p window. Without the "plus current" part, a series set
  to 12 in the web client read back as "As many as possible", because an
  unlisted value has no row to select.
