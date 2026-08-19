# How the Jellyfin server actually behaves

Facts about the **server**, not about our UI. Almost everything here has been
measured against a real server — usually both a 10.11 and a 12.0 — or read out
of the server source, and every one of them has cost a bug at least once.

The rule that generates most of this file: **the server almost never tells you
that you got it wrong.** It drops what it cannot parse and answers 200. So a
parameter name, an enum spelling or a `Fields` value taken from an SDK or from
jellyfin-web without measuring may simply not be applied, and the screen looks
completely normal.

## 1. Jellyfin silently drops what it does not recognise

Measured against 12.0 and 10.11 on a 1131-item library:

- **An unknown parameter name is dropped.** `IsDefinitelyNotAParameter=true`
  and `IsDefinitelyNotAParameter=false` both answer the unfiltered total.
- **An unparseable enum value is dropped.** `VideoTypes=Nonsense` and
  `VideoTypes=Blu-ray` both answer 1131 — identical to sending nothing — while
  `VideoTypes=BluRay` answers 0 and `VideoTypes=VideoFile` answers 1131.
- **Parameter names and enum values are case-insensitive**: `Is4K` and `is4K`
  answer identically; `BluRay` / `Bluray` / `bluray` / `BLURAY` all match.
- **The `Fields` comma binder drops names it cannot parse** instead of
  rejecting the request, which is why doing so was invisible. A stricter server
  would 400 the whole query.
- **An unknown sort name is ignored, not refused.** `DateLastMediaAdded` is not
  in the server's `ItemSortBy` enum — it is `DateLastContentAdded` — so the
  wrong name looks like a working sort control that always returns name order.
  Pinned by `tests/test_library_queries.py`.

**So the evidence that a value parses is the *contrast* with a deliberately
bogus one, never that the request succeeded.**
`tests/e2e/test_filter_matrix.py` does this for every filter the panel offers,
and carries a control test that sends a nonsense parameter name in both
directions and asserts both answer the unfiltered total.

**Do not use a partition test as the discriminator.** `Is4K=true` is 5 and
`Is4K=false` is 0 against a library of 1131, so the two do not sum to the whole
— a partition test would fail on a parameter that works. The discriminator is
that `true` and `false` do not *both* answer the unfiltered total.

This is the same failure shape as a test that cannot fail, one layer out: the
artifact is well formed and merely wrong, so nothing downstream ever notices.

## 2. Every item query goes to `GET /Items`

`jellyfin-apiclient-python`'s `get_user_items` calls **`GET
/Users/{userId}/Items`**, which is `[Obsolete("Kept for backwards
compatibility")]` in Jellyfin's `ItemsController` and which upstream plans to
remove.

**It is the same handler** — the legacy action's whole body is `=> await
GetItems(...)` — but it delegates **positionally** and passes a literal `[]`
for the language arrays. Diffing the two signatures: of 88 parameters, the
legacy route drops exactly three — **`audioLanguages`, `subtitleLanguages` and
`indexNumber`** (deliberately; commit `068b3fd58d`, "remove language filters
from old Items endpoint").

Measured on the v12 QA server: `AudioLanguages=eng` is **1108 of 1131** through
`/Items` and **1131** through the legacy route. So a filter can be perfectly
well formed, perfectly supported by the server, and do nothing at all for us.
**Measure a filter on `/Items` before concluding the server ignores it** — that
mistake cost a wrong entry in `docs/UI_FIXES_4.md` §19 that had to be retracted.

`items_api.py` is the modern twin. Three things about it:

- The user goes in as a **query parameter**. `{UserId}` is a template the http
  layer substitutes into params as well as into the URL
  (`http.py:_replace_user_info`), so it stays a client-side concern and no
  caller has to hold a user id.
- **`build_query` RAISES on an unknown keyword** rather than dropping it. A
  keyword the module does not map is a filter that would silently stop being
  applied, which is indistinguishable from a library that genuinely matches
  everything — the entire defect this module exists to stop repeating.
  `tests/test_items_api.py` diffs `_QUERY_KEYS` against the apiclient's own
  signature for the same reason. `None` means "not sent", so the server's own
  default applies and a caller can pass every argument unconditionally.
- The escape hatch for a parameter the apiclient does not name is **`params`**,
  which `build_query` merges last (this is how `StudioIds` is sent).

**Version-gate by asking the server, not by sniffing.** `audioLanguages` landed
on master 2026-05-10 and is in no 10.11 or 10.10 release. jellyfin-web's design
is to render the control only when **`/Items/Filters2`** returns non-empty
options, since a server without the query parameter also returns no options.
`Filters2`, not `Filters` — see section 6.

## 3. Filters: what composes and what does not

- **`Filters=IsUnplayed,IsPlayed` is HTTP 400 on 12.0 and an empty result on
  10.11.** Neither is usable, and the 400 puts an error banner over a library
  that works. `dialogs.MUTUALLY_EXCLUSIVE` — `played`↔`unplayed`, `hd`↔`sd`,
  symmetric so whichever is ticked clears the other — refuses the pair
  client-side. **It is enforced once, at the toggle**, because that layer is
  the only one that can un-tick the box the user is looking at; anything that
  learns to set filters *without* going through the toggle owes the same check.
  `tests/e2e/test_filter_matrix.py` asserts "refused **or** empty" and never
  non-empty, which would mean the server ORed them.
- **Played and Unplayed do partition** (unlike the video flags), because every
  item is one or the other.
- **`IsHd` is tri-state and there is no `IsSd`.** jellyfin-web does not make
  them exclusive, and there the second check silently wins (its handler assigns
  `isHd` twice, SD last) — so ticking both shows two ticks and filters by one
  of them.
- **Video quality flags UNION rather than intersect.** `IsHd=true` answered 49
  and `IsHd=true&Is4K=true` answered 54, which is 49 + the 5 the 4K filter
  matches alone. That is the opposite of how every other filter on the panel
  composes.
- **`Is4K=false` does not mean "not 4K"** — true = 5, false = 0, library =
  1131. `Is3D`, `HasSubtitles` and `HasTrailer` do partition.
- **`IsFavorite` has its own parameter**, so putting the equivalent `Filters`
  enum member in as well sends it twice.
- **`Filters=Likes` is still answered by the server**; it is dropped from the
  panel only because this client cannot set a like.
- Live TV categories AND rather than OR — section 9.3.

## 4. `Fields`: what has to be asked for, and what is free

**Most of what a tile draws is not a `Field` at all.** `ProductionYear`,
`Artists`, `Album`, `RunTimeTicks` and the ratings are unconditional
`BaseItemDto` properties that `DtoService` sets whether or not they were asked
for. Listing them achieves nothing, and because the binder drops unparseable
names (section 1), doing so was invisible.

Things that genuinely have to be asked for, all of which read as *absent*
rather than as errors when you forget:

- **`MediaSourceCount`.** A Video's DTO carries the count only under this
  field, *and* the server omits it entirely when it is 1 — so an absent value
  means "one version", not "not asked for". It costs nothing extra to answer
  (it is the length of the item's own alternate-version lists, not a
  media-source resolution — that is `MediaSources`, which a browse query must
  not pay for).
- **`CanDelete`.** A list query omits it entirely (measured: the key is absent,
  not `False`), so a "Delete from Disk" entry keyed off it would never appear.
  A single-item fetch returns it whatever `Fields` says, but relying on that
  undocumented default is how a music album came to offer Delete on its detail
  page and never from its tile menu. Cost: **+1.8 KB on a 165 KB hundred-item
  grid**, no change in query time — worth having measured, because the per-item
  permission fields on this API are exactly the ones that have been expensive
  before.
- **`ExternalUrls`** is absent from list queries unless asked for (measured on
  10.11 and 12.0). The single-item routes fill it in unconditionally; list
  routes do not.
- **`PrimaryImageAspectRatio`**, which is what row shape is resolved from.

And two that arrive without being asked:

- **`UserData` comes back whatever `Fields` says**, which is what makes an
  `Ids` lookup nearly free to strip (section 6).
- **A Season DTO carries its series' backdrop** — `ParentBackdropImageTags`
  plus `ParentBackdropItemId` — with no `Fields` at all.

**Body cost.** `Overview` is about a third of a hundred-item response (154 KB →
108 KB for a hundred series). `ItemCounts` on albums was free.

**The apiclient's default `Fields` is expensive.** It is `info()`, a ~29-field
payload including `MediaSources`, `People`, `Studios` and `RecursiveItemCount`:
`MediaSources` forces per-item media-source resolution and the rest add joins.
On an instant mix of 200 items the per-item lookups turned a single query into
hundreds and took **~25 s on a spinning-disk server**.

## 5. Images on the wire

- **`EnableImageTypes` is a whitelist.** A tag the query does not name is
  absent from `ImageTags` — the whole of the Banner bug: `Banner`, `Logo` and
  `Disc` are not in the browse set, so a view set to one of them fell through
  to the item's thumbnail and drew it letterboxed in a 5.4:1 frame. The request
  must name the **whole fallback chain**, not just the type asked for, or the
  Banner→Logo fallback looks for a tag the query told the server to omit.
  (About half a TV library carries no banner: measured 107 of 200 on one
  server, 199 of 200 on another.)
- An **empty** `EnableImageTypes=` is ignored, not "none". Only
  `EnableImages=false` suppresses tags — worth ~12% of a program-list body —
  and the apiclient's `get_programs` has no such argument, so a guide query can
  drop the expensive *fields* but not the tags.
- **`ImageTypeLimit=1` matters**: without it every backdrop tag comes back, and
  items routinely carry five to ten.
- **Backdrops are a numbered set.** The server does serve `/Images/Backdrop`
  unindexed, but only the indexed form is guaranteed to match the tag a bitmap
  was cached under.
- **A playlist has a server-generated Primary image**, worth requesting even
  with no tag in the DTO.
- **People entries carry a bare `PrimaryImageTag`**, not an `ImageTags` dict.
- **`ParentBackdropItemId` is the series' id, not a request for its backdrop** —
  `/Items/{ParentBackdropItemId}/Images/Primary` resolves to the series' poster.
- **Jellyfin caches images per exact pixel size**, so a request one pixel wider
  costs full price. That is why every window size re-does every poster, and why
  a first paint that keeps the server busy past the apiclient's 30 s timeout
  does not make the library slow — it fails the browse query outright.
- Image fetches use the apiclient's non-legacy `MediaBrowser` `Authorization`
  header, so no token appears in a query string.

## 6. What a query costs

Measured against a slow remote server, 100-item pages, unless stated.

**Untyped library queries cost seconds, and it is the folders, not the media.**
The server builds a `Folder`'s `UserData` by walking everything underneath it.
One page of a 1334-film library took **8.0 s**; the same page with
`IncludeItemTypes=Movie&Recursive=true` took **0.3 s**, and so did
`EnableUserData=false`, which is the proof of where the time goes. jellyfin-web
always names the type.

Recursion is what keeps the contents the same afterwards — films inside those
folders are still listed, just as films — and **`Recursive` only ever travels
*with* a type filter**: on its own it is how a TV library answers with 43,000
episodes. Only a library **root** carries a collection type, so this can never
flatten a folder the user opened.

**`Items/Filters` untyped walks every episode** of every series to collect the
genres of their shows: **3.7 s** on a 950-series library, **0.4 s** typed.

**Both filter endpoints are needed; they are not supersets.** The legacy
`Filters` has Years and OfficialRatings and no languages; `Filters2` has the
languages and no Years. Switching wholesale to the newer one would have
silently emptied the Year picker. jellyfin-web calls both for the same reason.
Both are absent on Jellyfin 11 and earlier, where the query parameters do not
exist either — so an empty answer *is* the version gate and no version check is
needed. `Filters2` returns `NameValuePair`s: `Name` is `"English (eng)"`,
`Value` is what the query wants. On pre-`Items/Filters` servers only genres and
years are available at all.

**`EnableTotalRecordCount=false` is worth sending.** The count is a separate,
wider `COUNT(*)` server-side. But then `TotalRecordCount` cannot be trusted —
falling back to `len(items)` tells a paginator the first page is the whole
list, which is how it came to re-serve page 1 as page 2.

**`Fields` on an `Ids` lookup is nearly free to drop.** 60 ids against 12.0:
**73 ms / 191 KB** with the apiclient's default against **13 ms / 60 KB** with
`fields=""`, for byte-identical `UserData`.

**A search with no `Fields` costs three things at once** —
`PrimaryImageAspectRatio`, `MediaSourceCount` and `CanDelete` were all absent;
adding them measured **+47 KB on a 1.28 MB response**.

**Request-URI limits are real.**

- `Ids=` is batched at **100 GUIDs**: a big queue's ids as one parameter
  overflows the server's request-URI limit (HTTP 414).
- Guide channel ids overflow the request-line limit in Kestrel and common
  reverse proxies (414/431) past roughly **150–200 GUIDs**, which is what
  bounds `CHANNEL_PAGE` at 100. jellyfin-web pages at 500 and gets away with it
  only because it splits nothing.

**Keep the session alive.** With the apiclient's `keep_alive` off, the session
is torn down after every request and each browse call pays a fresh TLS
handshake.

**Budgets worth matching to jellyfin-web**: `getItemsForPlayback` caps every
playback query at **300** unless the caller passes its explicit "unlimited"
sentinel, which only a photo album does; search asks for **800** in total
across every row; people and artists get **100** each on their own endpoints.

## 7. DisplayPreferences: one document, several clients' settings

The home screen layout, per-view settings, display preferences and the Live TV
guide preferences are all **stored on the server**, in the document
jellyfin-web uses:

- **id `usersettings`** (the apiclient's `get_user_settings` /
  `update_user_settings` address exactly that one document)
- **client `emby`** — jellyfin-web's legacy namespace. **Any other client
  string reads a different, empty preference set.**

That is not incidental compatibility: a user who sorts their guide by channel
number in the web client expects the same order here, and writing these
anywhere else would give them two settings with one name.

**Saving is read-modify-write of the whole DTO.** There is no partial-update
path on this API, so a save must GET the document, mutate `CustomPrefs` and
POST it back. Dropping fields we do not understand clobbers jellyfin-web's
other settings (landing screens, `tvhome`, …) — and posting only our keys drops
the home layout. One cached `CustomPrefs` blob covers the whole document,
because the home layout, the guide settings, the display preferences and every
per-view setting live in it.

**CustomPrefs booleans are STRINGS.** They go out as `"true"` / `"false"`
because jellyfin-web reads them with `toBoolean(...)` over a value it wrote
with `val.toString()`. A JSON boolean reads as false there, so the setting
appears to revert every time the web client is opened. Reading is tolerant of
both, because some clients do write a JSON `true`.

### 7.1 Home screen layout: `homesection0` .. `homesection9`

- **An absent or empty slot means *this slot's* default, not "none".** Only the
  literal string `"none"` blanks a slot. jellyfin-web's settings UI rewrites the
  default option's value to `""` before saving, so a user who never touched the
  screen and one who explicitly picked the default are indistinguishable on the
  wire — both round-trip through the per-slot default.
- **A slot holding its own default is written back as `""`**, which keeps a
  never-customised layout from being pinned to today's defaults if the server's
  change later.
- **A stored `"folders"` is a pre-10.x alias for `smalllibrarytiles`**, and it
  remaps to **slot 0's** default rather than the current slot's.
- **Section types we cannot draw are preserved on save**, never rewritten to
  `"none"`, so configuring the shim never degrades the same user's web home
  screen.
- The slot count grew 7 → 10. Reading all ten against an older server is
  harmless (missing keys fall back to their slot default), so there is **no
  version sniffing**. Per-slot defaults mirror `constants/homeSectionType.ts`,
  itself synced with the server's `DisplayPreferencesController`.

`home_sections.py` is the pure logic; the I/O is `LibrarySource.get_home_prefs`
and `save_home_layout`.

### 7.2 Which display settings sync at all

jellyfin-web's test is `userSettings.set(key, value, enableOnServer)` with
`enableOnServer !== false`. `maxDaysForNextUp`, `enableRewatchingInNextUp`,
`libraryPageSize` and `enableBlurhash` are **localStorage-only** and do not
sync even in web. `useEpisodeImagesInNextUpAndResume` defaults **false**, which
reads as `inheritThumb: true`.

### 7.3 Per-view settings keys are not fully knowable from web's source

`getSettingsKey` (`list.js:1265`) builds `items-<type-or-parentId>-…` and
appends a route type **only when the route carried one**, so the same library
reached two ways has two keys. A real setting observed in the wild:
`items-f4415c72cc16920fce19d78d636a3ce7-Folder-imageType: thumb`. So
`view_prefs.keys_for` returns candidates in priority order (typed first, bare
`items-<parentId>-<setting>` last) and a write goes back to the key it was read
from. `viewType` is a key this client used to write and **nothing in
jellyfin-web has ever read**.

### 7.4 Live TV guide preferences

`livetv-channelorder`, `livetv-favoritechannelsattop`,
`guide-colorcodedbackgrounds` and `guide-indicator-*` are CustomPrefs keys in
the same document, written as the strings above. Defaults follow what
jellyfin-web's `guide.js` actually *draws* rather than what its settings dialog
shows — the dialog gives "new" no `data-default` so it renders unchecked, while
the guide tests `!== 'false'` and draws it.

## 8. Home rows: `LatestItemsExcludes` and `ParentId`

`LatestItemsExcludes` — the "Display in home screen sections" toggle — is read
off `GET /Users/Me` → `Configuration.LatestItemsExcludes`.

**The server applies it itself for Continue Watching and Next Up, but only when
the query carries no `ParentId`.** The per-library "Latest" rows are
deliberately one request each **with** `ParentId`, which bypasses it — so that
exclusion is applied **client-side** in `get_home_rows`, exactly as
jellyfin-web does in `recentlyAdded.ts`.

**The resume rows must keep passing no `ParentId`.** Scoping them by library to
"narrow" the query would silently bypass the user's exclusions. Pinned live by
`tests/e2e/test_home_layout.py`, by `parent_id` rather than by title — every
library's title is a translated string and a substring of some other's.

Two more home-screen facts:

- **`/Latest` answers with a bare list, not an `Items` dict.**
- **Live TV gating comes free from `/Views`.** The server adds that view only
  when the user may use Live TV *and* a tuner is configured
  (`UserViewManager` consults `LiveTvManager.GetEnabledUsers`, which is
  `EnableLiveTvAccess && tuner hosts exist`). Its presence is the whole gate,
  at no extra request.

## 9. Live TV

The UI side is `docs/live-tv.md`. These are the server's rules.

### 9.1 Times are UTC with seven fractional digits

Jellyfin serialises .NET ticks, i.e. **seven** fractional digits, which
`datetime.fromisoformat` rejects outright before 3.11 and still will not take
in that width. And the timestamps are **UTC**.

`live_tv.parse_time` is written out longhand because every shorter way of
parsing one produces a *plausible datetime that is wrong* rather than an error:
dropping the fraction with `partition('.')` also drops the `Z`, yielding a
naive datetime the guide then lays out as if it were local. The whole grid
slides by the UTC offset, which reads as "my guide is five hours out" rather
than as a parse failure.

`repository._iso_utc` exists for the mirror-image reason on the way out. The
server binds these dates with `AdjustToUniversal`, which **accepts an
offset-less string without shifting it** — so a bare `str(datetime)` queries a
window that is out by the local UTC offset and answers *successfully* with the
wrong programmes. Always send the `Z`.

### 9.2 The guide window bounds are asymmetric on purpose

`LiveTv/Programs` takes **`MaxStartDate` = the window *end*** and **`MinEndDate`
= the window *start***. That is the pair jellyfin-web uses, and it is what
includes a programme that began before the window opened. Both are nudged by a
second (`end - 1s`, `start + 1s`), as jellyfin-web does, so a programme that
ends exactly as the window opens does not occupy a zero-width cell at the edge.

### 9.3 Channel categories are columns and tags at the same time

The server writes a channel's categories one way and queries them another
(verified against the server source):

| Category | Written to the channel as | Filtered by | Works |
|---|---|---|---|
| Movies | `IsMovie` column, OR'd over its programmes (`Guide/GuideManager.cs:299`) | the column | yes |
| Kids | `AddTag("Kids")` (`GuideManager.cs:303-306`) | the tag | yes |
| Sports | `IsSports` column | tag `"Sports"`, never added to a channel | **no** |
| News | `IsNews` column | tag `"News"`, never added | **no** |

Filtering lives in `BaseItemRepository.TranslateQuery.cs:120-170`:
`IsSports`/`IsNews`/`IsKids` append to a **tag** list while `IsMovie` is a
separate column `.Where` — so the flags also **AND** rather than OR. Untick
Sports and the query becomes `IsMovie == true AND tag ∈ {Kids, News}` → zero
channels.

Two consequences:

- **Sending category flags to `LiveTv/Programs` looks equivalent to filtering
  the channel list and is not** — two categories AND together and the guide
  comes back empty. So `category_kwargs` goes to `get_channels` only, and the
  guide's programmes are filtered by *drawing*.
- jellyfin-web sends the same query and shows the same symptoms, so
  reproducing the oddity is **parity, not a bug of ours**. Do not go add
  metadata to the test tuner source: its XMLTV categories are already mapped
  correctly by `XmlTvListingsProvider.cs:205-208`, and the working Kids filter
  is the proof the pipeline functions. If it should actually work, the fix is
  client-side and small — channel DTOs already carry `IsSports`/`IsNews`, so
  `get_channels` could drop those two flags and filter the returned list
  itself. That is a deliberate divergence; it needs asking about first, and
  filing upstream.

The server parameter is **`IsMovie`, singular** — the only one of the four
whose flag is not its own name.

### 9.4 A program list needs `ChannelInfo,ChannelImage`

`ChannelInfo` alone gets `ChannelName` and `ChannelNumber`. The channel's
**logo** is gated separately on `ChannelImage`:
`LiveTvManager.AddInfoToProgramDto` sets `ChannelPrimaryImageTag` only under
`hasChannelImage`. Most guide data carries no artwork of its own and the
channel logo is the whole fallback, so asking for only the first turns every
listing row into a wall of letter glyphs. jellyfin-web asks for the pair.

The exception is a query drawing **text only** — the guide grid and the channel
listing — where `ChannelImage` costs a **channel lookup per programme**, across
a whole window or a thousand rows, for a tag nothing draws.

### 9.5 Other Live TV endpoint facts

- **Recording state is attached regardless of `EnableUserData`.** `TimerId`,
  `SeriesTimerId` and `Status` come from `LiveTvManager.AddRecordingInfo`;
  `EnableUserData` only ever gates the DTO's `UserData` block.
- **A recording DTO carries no timer state**, so an `is_in_progress` query is
  the only thing that knows a recording is still being written.
- **On a *program* DTO the server never emits a cancelled `TimerId`** —
  `AddRecordingInfo` sets it only when the status is neither Cancelled nor
  Error.
- **`LiveTv/Channels` has no way to skip the total record count**, so the
  unbounded call returns every channel with artwork and user data attached. An
  IPTV line-up runs to thousands.
- **`LiveTv/Timers` takes no sort arguments.** Sort client-side.
- **`HasAired=False` means "has not *finished*"**, not "has not started" — so
  the first entry of such a listing is what is on air right now.
- **`Timers/Defaults` is a real call, not a convenience.** It fills in padding,
  keep-until and priority from the server's DVR configuration as well as the
  programme's channel and times.
- **The timer update endpoint replaces the whole DTO.** Read-modify-write, or a
  padding change blanks the channel and the times.
- **`EnableLiveTvManagement` gates writes only.** `/LiveTv/Timers` and
  `/LiveTv/SeriesTimers` answer **200** for an account without it (measured);
  the 403 is on the mutations.
- **Two spellings of a channel number**: `LiveTv/Channels` answers with
  `Number`, the ordinary item endpoint with `ChannelNumber`.
- **`CurrentProgram` is populated only by the channel list query.**
- **A `Program` plays its `ChannelId`, not its own id.**
- **A `SeriesTimerInfoDto` is not an item** — no `ImageTags`; its only artwork
  pointer is `ParentPrimaryImageItemId` + `ParentPrimaryImageTag`. A plain
  `TimerInfoDto` has neither, nor any `ImageTags` at all.
- **A live TV MediaSource reports neither `SupportsDirectPlay` nor a
  `Bitrate`**, which is why source selection must not start from weight 0.
- **A live stream leaks without an explicit close.** A source that
  direct-streams (the usual HDHomeRun path) still holds a tuner server-side,
  and there is no reaper — it is freed only by an explicit close, a stop report
  carrying the `LiveStreamId`, or a server restart. On a single-tuner box a
  leak means no more live TV until the server comes back.
- **There is no "recording started" websocket message.** The four the server
  does push are `TimerCreated`, `TimerCancelled`, `SeriesTimerCreated`,
  `SeriesTimerCancelled`.

## 10. `UserDataChanged` and userdata semantics

Measured against 10.11.11 and 12.0.0, which agree, and cross-checked against
the server source. Pinned live by `tests/e2e/test_offline_sync.py`.

**The payload carries the values, so applying it costs no request.**
`{UserId, ServerId, UserDataList: [UserItemDataDto, …]}`, each entry with
`ItemId`, `Played`, `PlaybackPositionTicks`, `PlayCount`, `IsFavorite` and
`LastPlayedDate`. The server also appends each changed item's **parent** to the
list ("go up one level for indicators"), so the ordinary message is two entries
and most ids in one are not the item that changed.

**A progress report announces NOTHING.**
`UserDataChangeNotifier.OnUserDataManagerUserDataSaved` returns early on
`UserDataSaveReason.PlaybackProgress`. Three progress reports produce zero
events. The shim's own comment claimed the opposite ("fires every few seconds
while watching") for years, and two debounce constants were sized against it.

**Not even the progress report that finishes the item** — the obvious guess and
the wrong one. `SessionManager.OnPlaybackProgress` saves the completion under
`PlaybackProgress` like any other; the only thing it does differently is
`Video.PropagatePlayedState`, which returns immediately for a video with no
alternate versions and skips the item itself in any case. So another device can
watch something to the end and you are told nothing until it sends its
**stop**, which a client killed mid-playback never does. *That is why a full
catalog sweep still has to exist.*

**What does fire:** playback start, playback stop, mark played/unplayed,
favourite, `POST UserItems/{id}/UserData`. Coalesced by a **500 ms timer**, so
a bulk change is one message rather than a burst — but a bulk mark can still
produce hundreds of entries, which is why a message past a size threshold is
answered by scheduling a sweep instead of walking it on the websocket thread.

**Delivery is not filtered by origin** — it goes to every session the *user*
has, including the one that caused it. The app is told about its own marks.

**`MinDateLastSavedForUser` is not a userdata watermark.** It looks like the
"what changed since T" parameter and is not:
`BaseItemRepository.TranslateQuery.cs:282` filters `e.DateLastSaved`, the
item's *metadata* save date, identically to `MinDateLastSaved`. Measured right
after marking an item played: a year-old mark returns all 3,224 items, an
hour-old mark returns none. Do not build catch-up on it.

**Resume rules differ by write path, which matters when testing.**
`UserDataManager.UpdatePlayState` — the *playback reporting* path — applies
`MinResumePct` 5 / `MaxResumePct` 90 / `MinResumeDurationSeconds` 300, so on
short clips **any** reported position at ≥5% marks the item played instead of
storing a position. `POST UserItems/{id}/UserData` applies none of it and
stores what you give it. Test exact positions through the second. (The
audiobook arm of the same method is stated in *minutes* — section 12.)

**A per-item userdata read is STALE after a container mark** (12.0.0). Mark a
*series* played and its episodes come back `Played: true` from `GET /Items?ids=`
but `Played: false` from **both** per-item endpoints — `GET
/UserItems/{id}/UserData` and `GET /Users/{uid}/Items/{id}`. The fan-out really
happened (`Folder.MarkPlayed` walks the children); the per-item answer is the
wrong one, and it is not fixture-specific. So a sweep only works because it
uses `get_items`.

Two smaller ones: **`PlayedPercentage` is derived** and the position is the
truth; **`UnplayedItemCount`** is what a Series/Season watched badge reads.

## 11. Playback: `PlaybackInfo`, profiles and transcoding

- **Everything is 10,000,000 ticks per second.**
- **Authenticate with `ApiKey`, not `api_key`.** The server reads both in the
  same place, but `api_key` is gated on `EnableLegacyAuthorization`, which is
  **off by default from Jellyfin v12**
  (`AuthorizationContext.GetAuthorizationInfoFromDictionary`).
- **An empty `Container` in a `DirectPlayProfile` means "any"** —
  `ContainerHelper.ContainsContainer` treats empty as accept-all. This is also
  why the shim does not use the `/Audio/universal` endpoint jellyfin-web uses
  for music: that endpoint has no wildcard and forces the full enumeration. The
  `PlaybackInfo` round trip a wildcard profile drives is ~20 ms.
- **`MaxAudioChannels` exists on `TranscodingProfile` only** and cannot force a
  transcode. At `StreamBuilder.cs`'s `channelsExceedsLimit`, a 7.1 track that
  could have been stream-copied alongside a video transcode instead gets
  re-encoded and downmixed to the limit.
- **`TranscodeReasons` is not on the MediaSource DTO on 10.11.** The reasons
  ride in the transcoding URL as a comma-joined flags string; `VideoCodec`,
  `AudioCodec` and `TranscodeReasons` are all query parameters of
  `TranscodingUrl`, and each codec parameter may name **several** codecs.
- **`TranscodeReasons` says why direct play was refused, not what the
  transcoder does with each stream.** `AudioCodecNotSupported` can appear on a
  session whose transcoding profile then happily copies the audio.
- **Play-method table**, measured against a live 10.11 server with three device
  profiles built to force each row: same video + same audio → **Remux**; same
  video + re-encoded audio → **DirectStream**; re-encoded video →
  **Transcode**. jellyfin-web's *display* vocabulary differs from Jellyfin's
  `PlayMethod` enum — it shows "Direct playing" for a `static=true` stream as
  well as for a file opened directly.
- **Jellyfin refuses to serve PGS subtitles as external.**
- **`IsExternalUrl` is set exactly when a subtitle's `Path` is an absolute
  http(s) URI** (`StreamInfo.cs:1264-1274`).
- **A Photo has no MediaSources, so `PlaybackInfo` is skipped entirely** —
  there is no source to negotiate, no play-session id and nothing to transcode.
  It follows that there is no timeline reporting for photos; reporting one
  anyway would put every picture you looked at into Continue Watching.
- **A `.strm` carries no `RunTimeTicks` on the Item.** A library scan never
  probes one (the probe is gated on the item not being a shortcut). The server
  *does* probe it, but only during the `PlaybackInfo` request, and that runtime
  lands on the **MediaSource**. Measured against 12.0: the remote probe is
  gated on the *item's* path ending in `.strm`
  (`MediaSourceManager.GetPlaybackMediaSources`), and a version set's
  `item.Path` is its **primary's**. A source standing for the item's own file
  carries the item's id, which is Jellyfin's own convention.
- **`MediaSegment` types** are Intro, Outro, Commercial, Preview, Recap.

## 12. Books on the wire

`docs/readers.md` owns the reader side and the full `RunTimeTicks` progress
encoding. The server-side shape:

- **`Book : BaseItem` is not `IHasMediaSources`.** Its DTO carries no
  `MediaSources`, no `Container` and **no size under any `Fields` value** —
  measured: `Fields=Size`, `Fields=MediaSources` and `Fields=Container` all
  come back empty. `GET /Items/{id}/Download` is the only path to its bytes;
  nothing serves a page, an archive entry or a spine document. **`PlaybackInfo`
  has nothing for a Book.**
- **The only statement of a book's format is `Path`**, which is what
  jellyfin-web reads too (`bookPlayer` gates on `item.Path?.endsWith('epub')`)
  and which **is served to non-admins under `Fields=Path`** — verified against
  a non-admin account, which is what makes it viable rather than an admin-only
  trick. The download's `Content-Disposition` then confirms it.
- **`AudioBook : Audio` is an ordinary audio item** — real `MediaSources`, real
  duration, real transcode negotiation, real progress reporting.
- **Neither `Book` nor `AudioBook` carries `ParentId`** under the `Fields` the
  downloader asks for, and the two halves disagree about which field could put
  a folder back together: a **`Book` carries `SeriesName`** (the folder, or a
  real series when the file is tagged) **and no `Album`**; an **`AudioBook`
  carries `Album`/`AlbumArtist` and no `SeriesName`** — and `Album` is
  tag-derived, so an untagged rip has nothing joining its files but the
  directory.
- **`RunTimeTicks` is overloaded as a fake progress unit and the encoding
  depends on the format** (`pages * 10000` for comic/PDF, one second for epub,
  absent for mobi/azw). Both spellings are pinned against the two
  implementations that define them — `ProbeProvider.FetchAsync(Book)` writes
  the durations, jellyfin-web's players write the positions. The server's own
  comments call this a placeholder for "multiple progress types": treat it as a
  wire format to interoperate with, never as a design to build on. Full table
  in `docs/readers.md` §2.
- **Page counts need the `PDFtoImage` probe that landed in Jellyfin 12.0**, so
  a 10.11 server returns no runtime for PDFs or comics at all.
- **The audiobook resume rule is stated in MINUTES, and it is not the video
  one.** `UserDataManager.UpdatePlayState` has an `AudioBook` arm:
  `MinAudiobookResume` (**5 minutes**) discards a position less than five
  minutes in, and `MaxAudiobookResume` (**5 minutes**) discards one with less
  than five minutes left *and marks the book finished*. So **an audiobook under
  ten minutes can hold no resume position at all**, and one under five cannot
  even be finished by playing it — only by marking it. A `Book` is excluded
  from **both** arms and stores its position verbatim.
- **`.cba` is not a book.** It exists only as a MIME mapping on the server and
  is invisible to the library, so a file with that extension is not a `Book` at
  all.
- **`EnableContentDownloading` is fatal here rather than merely
  inconvenient.** Everywhere else it costs offline viewing; for a book it gates
  the only path to the content. (For a **photo** it is the opposite: the same
  picture comes back from the image endpoint, which needs no permission, so the
  download URL is an original-quality *upgrade* rather than the only way in.)
  See `docs/PERMISSION_GAPS.md` §4b.
- Books are genre-tagged like everything else and a books library has its own
  Genres tab. jellyfin-web's *global* Favorites screen has no book rows; a
  favourited book is reached through the books library's own Favorites tab.
- **`media_types="Book"` is what Continue Reading must ask for** — the two book
  entity types are unrelated, and an `AudioBook` is an `Audio` that would land
  in Continue Listening.

## 13. User policy

`GET /Users/Me` → `Policy`. The policy also arrives on the login response, but
credentials are restored from `cred.json` on every run after the first, so
there is usually no login response to read.

**`SyncPlayAccess` is three-valued, not a boolean**: `CreateAndJoinGroups` /
`JoinGroups` / `None`. `JoinGroups` may join an existing group but not create
one, so a client that treats it as on/off either hides a feature that works or
offers one that 403s.

**An absent policy key means an older server with no such setting — fail
open.**

## 14. Item types, sorting, pagination and other shapes

- **`Type` depends on which resolver ran; `MediaType` is the stable axis.** The
  same clip is a `Video` under a Home Videos library and a `Movie` under a
  movies one (`MovieResolver.cs:158-215`). `MediaType` asks the question
  actually being asked — stream or container? — and is the axis jellyfin-web's
  `playbackManager` filters on.
- **`PhotoAlbum` is its own type.** A Home Videos directory holding both clips
  and images comes back as `PhotoAlbum` with `IsFolder: true` — the single
  reason a mixed home-video library had holes in it before that was treated as
  a folder.
- **Servers before 10.7 label a finished recording `Recording`**; newer ones
  hand back a `Movie`/`Episode`/`Video` wearing its own artwork.
- **A `BoxSet` DTO has `CollectionType: null`** — only `CollectionFolder` and
  `UserView` carry one — so keying query shape on collection type never affects
  opening a collection. A BoxSet can also gather items from several libraries,
  so a collections grid is server-wide and recursive.
- **A playlist's declared type and its contents can diverge**, so a playlist
  cannot be classified as music or video up front. Playlist item order **is**
  the playlist and must not be re-sorted. `GET /Playlists/{id}` (exposing
  `OpenAccess` and shares) is absent on older servers.
- **The server decides whether collections appear in the main browse**, and
  whether movies are grouped into collections for a library request.
- **Track `SortName` is not the track's name.** The server builds it from the
  disc and track numbers with the title only as a tie-break, which is what
  makes an album's own listing come out in play order — but ask a whole library
  for it and the ordering is by track number *across albums*. jellyfin-web
  spells its "Track Name" option `Name` for this reason.
- **"Date Added" on a TV library is when the *series* was created.**
  `DateLastContentAdded` is the newest episode — jellyfin-web's
  `OptionDateEpisodeAdded`.
- **`Ids=` does not preserve the requested order.**
- **`/Items` does not reliably answer with artists.** Measured on one server,
  an item query returned 9 artists against `/Artists`' 13 for the same term
  (the artists endpoint includes track-level and featured artists that are not
  library items), and on at least one real server it returned none at all.
  Artists and people get their own endpoints, exactly as in jellyfin-web.
- **Search sorting is the server's and it does not interleave by type**, so one
  shared result budget is taken off the bottom in whole rows rather than spread
  evenly — a term matching a lot of episodes spent a 60-item allowance on them
  and the Movies row never appeared at all (#641).

## Image requests: `maxWidth`, never the fill parameters

Measured on 12.0. Asking the server to produce a picture of a shape it does not have
costs three things and buys nothing:

- **It does not crop.** `fillWidth`/`fillHeight` scale the artwork to *cover* the box
  and hand back the whole frame, so the shape asked for bought nothing.
- **It forces a re-encode.** The same backdrop is **115 KB as `maxWidth`** (the file,
  untouched) against **462 KB through fill** — four times the bytes, plus a server-side
  resize per header.
- **The height lands in the cache key.** Once the client's decode box stopped depending
  on it, each 64px step of a drag-resize stored another byte-identical copy of one
  picture.

A reporter's access log is what turned this up: a `fillWidth=3328&fillHeight=576` for a
backdrop that is 1920 wide. The crop is the client's to do — `compose_banner`'s
`scale_to_cover` was always going to redo it anyway — so all the server is asked for is
"this picture, no wider than this".

Note the request is `maxWidth` **only**: `image_url`'s non-fill branch sends no height,
because the apiclient has no `max_height`.

**The server caches per exact pixel size**, which is why every distinct width is a full
price fetch and why the client quantises its requests. See `docs/artwork-pipeline.md`.
