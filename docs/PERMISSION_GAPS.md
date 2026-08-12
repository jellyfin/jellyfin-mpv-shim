# User-permission gaps — work items

Raised by `tests/e2e/test_account_policy.py` running against stdjflib's twelve
QA accounts, which is the first time anything automatic had exercised them.

The principle, and the reason these are worth doing: **showing UI the server
will refuse creates confusion and issue reports.** A user with SyncPlay
revoked who is offered a SyncPlay button does not conclude they lack
permission; they conclude the client is broken, and they are half right.

**Items 1, 3, 4 and 5 are now fixed** (`jellyfin_mpv_shim/user_policy.py`);
their sections below are kept as the record of what was wrong and why, with
what shipped noted at the end of each. Item 2 is still open.

## 1. SyncPlay is offered to users who do not have it  — FIXED

`SyncPlayAccess` is never read. Grep for it in `jellyfin_mpv_shim/` returns
nothing.

The account is `qa-nosyncplay` — "SyncPlay refused, so the client's SyncPlay
entry points must go". There are three of them, all unconditional:

| Entry point | Where |
| --- | --- |
| Top-bar nav button | `mpvtk_browser/window_chrome.py:189` (`nav-syncplay`) |
| Playback HUD button | `mpvtk_browser/hud.py` (the bar's own SyncPlay button) |
| OSD menu row | `menu.py:158` |

All three lead to the SyncPlay dialog (`mpvtk_browser/dialogs.py:623`), whose
join/create then fails against the server and surfaces "Could not join the
SyncPlay group." — which is indistinguishable from a network problem.

**Shape of the fix.** The policy arrives with the login response
(`result["User"]["Policy"]["SyncPlayAccess"]`, one of `CreateAndJoinGroups`,
`JoinGroups`, `None`) so it needs no extra request, and it is per-server —
whatever holds it has to be keyed by server the way `has_live_tv` is, or a
two-server user gets the wrong answer. Note `JoinGroups` is a third state, not
a boolean: that user should reach the dialog but not the create button.

**What shipped.** `user_policy.py` holds the answer, cached on the client
object rather than in any one owner — the browser reaches its clients through
`LibrarySource`, the player through `clientManager`, and both have to get the
same answer. `GET /Users/Me`, once per server per session, taken lazily: the
policy does arrive on the login response, but credentials are restored from
`cred.json` on every run after the first, so most of the time there is no
login response to read.

It **fails open** throughout, and that is asserted as much as the hiding is.
Only an answer the server actually gave closes a gate; a fetch that failed, a
source without the method, an older server with no such field — all leave the
button where it was. Taking a working feature away because a request failed
would be a worse bug than the one being fixed. (Same doctrine as
`ItemActions.can_edit`.)

The three entry points: the top-bar button (`window_chrome._may_syncplay`),
the HUD button and the OSD menu row (both via `osc_bridge._may_syncplay`,
which returns `None` from `_syncplay` — the shape hud.py already treats as
"no button"). `JoinGroups` keeps the dialog and loses only **New Group**.

Pinned by `tests/test_user_policy.py` and, against the real accounts,
`tests/e2e/test_account_policy.py:SyncPlayPermissionTest` — which is where
the field name itself is checked, since a unit test with a hand-written
policy dict proves the branch and not the spelling.

## 2. The home-screen editor offers Live TV sections to users without Live TV

**Browsing Live TV is already correctly gated** — do not "fix" that. The
server only adds the Live TV view to `/Views` when the user may use Live TV
*and* a tuner exists, `repository.get_libraries` derives `has_live_tv` from
its presence, and both the home rows (`repository.py:793`) and search
(`pages/search.py:46`) consult it. `test_account_policy.LiveTvAccessTest`
pins this against `qa-kid`, who has the right revoked, and it passes.

The gap is one screen further in. Settings → Home Screen builds its dropdowns
from `home_sections.section_labels()`, which lists every section type
unconditionally — including `LIVE_TV` and `ACTIVE_RECORDINGS`. So a user with
no Live TV access can select "Live TV" for a slot, save it to their server,
and get a slot that renders nothing, forever, with no explanation. Worse than
an error: it looks like the section is broken.

**Shape of the fix.** `section_labels()` takes no context today, so it either
gains an argument or the settings tab filters what it returns; the tab already
has a source and therefore `has_live_tv`. The screen already has the right
idiom for this — a section jellyfin-web can draw and the shim cannot is shown
with a note rather than silently rewritten (`settings/home.py:72`), and the
same treatment fits a section this *user* cannot have.

Care on save: `home_sections` deliberately preserves section types the shim
cannot draw so that configuring the shim never degrades the same user's
jellyfin-web home screen. A Live TV section they set elsewhere must be
preserved on the same reasoning — hidden from the picker is not the same as
removed from the layout.

**Testable once fixed**: the section choices offered to `qa-kid` exclude Live
TV and Active Recordings; those offered to `qa-user` include them; and a Live
TV section already present in `qa-kid`'s stored layout survives a save.

## Not a gap: playback permission

`qa-noplayback` (`EnableMediaPlayback: False`) plays a file start to finish —
PlaybackInfo returns no error and the `static=true` URL is served anyway.

This is not something the client can or should paper over. Jellyfin's video
endpoints are `AllowAnonymous`, so as far as the API is concerned the item id
*is* the credential; the server cannot structurally refuse playback, and no
client-side check would make it able to. Worth fixing upstream, but a whole
ecosystem has to move together, so hiding the play button here would only make
this client look broken against a server that will happily serve the stream.

Recorded so the next person to measure it does not file it as a shim bug.

## 3. Recording is offered to users who cannot record  — FIXED

Found while writing `tests/e2e/test_live_tv.py`, which could not schedule a
timer as any account on the server.

`EnableLiveTvManagement` is a **third** Live TV permission, separate from
`EnableLiveTvAccess`: watching Live TV and managing recordings are granted
independently. The shim never reads it — grep returns nothing — so the Record
button, the series-rule button and the whole Recordings/Schedule surface are
offered to every user who can see Live TV at all. Pressing Record then answers
`403 Forbidden` from `POST /LiveTv/Timers`, and the user sees a generic
failure.

Same family as the SyncPlay gap above and probably the same fix: the flag is
on the login response's policy, it is per-server, and whatever holds
`SyncPlayAccess` should hold this too.

**What shipped.** Same module, same fail-open rule. `can_record` already
existed as an *apiclient capability* probe with exactly this doctrine, so the
permission folded into it: both questions have to say yes, and it now takes
the server it is asking about (it is per server; the probe is not).

**The tabs all stay.** The first cut hid **Schedule** and **Series** without
the permission, on the reasoning that they are about scheduling. That was
wrong, and measuring the server says so: `GET /LiveTv/Timers` and
`GET /LiveTv/SeriesTimers` both answer **200 for `qa-restricted`**, an account
that has never had `EnableLiveTvManagement`. The permission gates the writes.
What is going to record, and which series rules exist, is information the
server hands to anyone who can see Live TV — and a household member who
cannot change the DVR still has every reason to want to know what it is
about to do.

jellyfin-web reaches the same place from the other direction: `getTabs`
(`livetvsuggested.js:158`) consults no policy at all, and the gating lives on
the *actions* — `itemContextMenu.js` hides Cancel Recording and Cancel Series
behind the permission, `itemDetails/index.js` hides the Record buttons.

So the gate moved to where the 403s are:

* the **Record** buttons, via `can_record` (unchanged);
* the **timer editor**, which now opens read-only — the form renders with the
  same rows and values, disabled, and Save / Cancel Recording / Cancel Series
  are not offered at all. Not greyed: a disabled Save invites "why", and the
  answer is a permission the user cannot do anything about from that dialog.

`tests/e2e/test_account_policy.py` pins the premise (the reads are allowed),
because no unit test can — that answer belongs to the server. If it ever
starts refusing them, hiding the tabs becomes right again and that test is
what will say so.

Pinned by `tests/test_user_policy.py` and
`tests/e2e/test_account_policy.py:LiveTvManagementPermissionTest`, which
checks it both ways round against `qa-user` (granted) and `qa-restricted`
(never granted, which is the default for any account created on a modern
server).

### Why this was invisible, and the stdjflib half (fixed)

Jellyfin has **two** sets of user-policy defaults and both are live, on
different paths:

| Path | Source | `EnableLiveTvManagement` |
| --- | --- | --- |
| Account **migrated** from an older install | `UserPolicy` constructor | `true` |
| Account **created** on a modern server | `AddDefaultPermissions` | `false` |

`MigrateUserDb` deserializes the old policy file into a `UserPolicy`, so any
field absent from it keeps the constructor's value. That is why someone who
has carried an install forward has this permission and never remembers
granting it, while a server built from nothing has it on nobody — and why the
DTO constructor is the wrong thing to read if you are asking about a new user.

There is no administrator bypass either: `UserPermissionHandler` asks
`HasPermission` and stops, so `IsAdministrator` does not help. jellyfin-web's
dashboard does not couple them — "Allow browsing Live TV" and "Allow Live TV
recording management" are independent checkboxes.

**Fixed in stdjflib** (`b32b586`): `qa-admin` now carries every management
permission — its policy was previously declared and then never applied,
because `provision` skipped the account it authenticates as — and `qa-user`
gets `EnableLiveTvManagement` as well, since its description already claimed
everything a non-admin can have. The other ten accounts still lack it, which
is the state this gap is about and is what makes it testable.

## 4. A photo will not open for a user who may not download  — FIXED

Found while researching what Jellyfin actually offers for books, which share
the same endpoint. `qa-nodownload` is the account.

`EnableContentDownloading` gates `GET /Items/{itemId}/Download`
(`[Authorize(Policy = Policies.Download)]`, `LibraryController.cs:669`), and
that endpoint is not only how a download is taken — it was the shim's
ordinary path to a **photo's** bytes (`media.py:get_playback_url`, the
`is_photo` branch). So for this account every picture in the library failed
to open. Same family as the gaps above, but worse in one way: SyncPlay
without permission gives a button that fails, which at least suggests
something was refused. A photo that will not open looks like a broken client.

It is also the only path to a **book's** bytes at all, since `Book` is not
`IHasMediaSources` and so has no stream endpoint — worth knowing before book
support is written, because there the permission is unavoidable and the
answer has to be a clear message rather than a fallback.

**What shipped.** `user_policy.may_download`, same module and the same
fail-open rule: only an answer the server actually gave closes the gate,
because closing it on a failed fetch would send every photo through the
resizer for no reason. The photo branch already had an image-endpoint path
for HEIC and raw — mpv's ffmpeg often cannot decode those, so the server
converts them — and the permission now routes into the same place. One
condition, one fallback, and the two roads are independent: a HEIC is
unaffected either way.

This is **parity, not a divergence**. jellyfin-web draws the same line in
`src/components/slideshow/slideshow.js:getImgUrl`, which reaches for
`getDownloadUrl` only when `user.Policy.EnableContentDownloading` is set and
otherwise serves the same picture from the image endpoint. The download url
is the original-quality *upgrade*, never the load-bearing path — which is
why the fix is a fallback rather than dropping the download url for
everybody.

Pinned by `tests/test_user_policy.py` (the accessor), `tests/test_photos.py`
(`PhotoDownloadPermissionTest`, the branch and the token rule) and
`tests/e2e/test_account_policy.py:ContentDownloadingPermissionTest` — which
is where the field spelling is checked, and where the *premise* is: that the
server really answers 401/403 on the download for `qa-nodownload` and really
serves 200 on the image endpoint. If it ever stops refusing, that test is
what will say the fallback is unnecessary.

## 4b. Books, where that same permission is not optional  — ADDRESSED

The consequence of §4 that could not be fixed with a fallback, now that book
support exists. For a photo, `EnableContentDownloading` is an
original-quality *upgrade* and the image endpoint is an unconditional
fallback. For a **book** there is no second road: `Book` is not
`IHasMediaSources`, so it has no stream endpoint, no PlaybackInfo and no
image of its contents — `GET /Items/{id}/Download` is the entire API for its
bytes. A user without the permission cannot read a book at all.

So the answer here is the one §4 predicted: **say so**. `books.py` documents
the model, `LibrarySource.can_download` reads the same fail-open accessor as
every other gate in this file, and `ItemActions.read_book` refuses with a
message that names the reason —

> Your account is not allowed to download from this server, and a book can
> only be read by downloading it.

— rather than enqueuing a fetch that 403s and leaving a Read button that
appears to do nothing. That distinction is the whole point of the file: a
refusal that explains itself is a different experience from a broken client.

Note this is *not* a gap in the sense the others are. Nothing is offered that
cannot work; what is offered is an explanation. The button stays visible on
purpose — an administrator can grant the permission, and a Read button that
had silently vanished would leave nothing to ask about.

**Not** the same as a download of a film: that one is a convenience, and a
user without it still watches the film online. This is the only case in the
app where the permission decides whether the content is reachable at all.

Pinned by `tests/test_shell_books.py`
(`test_without_the_permission_reading_says_why`, which asserts both halves —
nothing is enqueued, and the reason is said) on top of the existing
`tests/e2e/test_account_policy.py:ContentDownloadingPermissionTest`, which is
where the premise lives: that `qa-nodownload` really is refused.

## 5. Collections are offered to users who cannot edit them  — FIXED

Found while writing `tests/e2e/test_collections.py` against stdjflib's new
collection fixtures: `POST /Collections` answered **403 for every non-admin
account on the server**, and the shim had offered the button to all of them.

`EnableCollectionManagement` is a **fifth** independently-granted permission,
off by default for any account created on a modern server
(`UserEntityExtensions.cs:193` adds it as `false`). What it gates is not one
route but the whole of `CollectionController`: the `[Authorize(Policy =
Policies.CollectionManagement)]` is on the **controller**, so creating a
collection, adding an item to one and removing an item from one are one
permission and one refusal. Three entry points in the shim, all of them
unconditional:

| Entry point | Where |
| --- | --- |
| "Collections…" in the Add To dialog | `mpvtk_browser/dialogs.py` (`add-collections`) |
| The picker and its Create box behind it | `dialogs._show_add_to_collection` |
| "Remove from Collection" in the tile menu | `mpvtk_browser/tiles.py` (`uncollect`) |

All three were gated on `edit_apis()`, which asks whether the *apiclient* has
the methods — a real question, and a different one from whether this user may
call them. So the answer was yes for everybody, and pressing any of them
produced "The change could not be applied.", indistinguishable from a
network problem.

**There is no administrator bypass**, and this is the one place to be careful
copying jellyfin-web. `UserPermissionHandler` asks `HasPermission` and stops,
exactly as it does for Live TV management. jellyfin-web reads the flag as
`user.Policy.IsAdministrator || user.Policy.EnableCollectionManagement`
(`itemContextMenu.js:143`, `multiSelect.js:198`, `LibraryToolbar.tsx:69`),
which offers the button to an admin the API will refuse. That spelling is
right for `BoxSet.IsAuthorizedToDelete`, which really does check both — and
wrong for the endpoint the button calls. We ask what the endpoint asks.

**What shipped.** `user_policy.may_manage_collections`, same module and the
same fail-open rule, reached through `LibrarySource.can_manage_collections`
and `ItemActions.can_manage_collections` — the latter answering the two
questions together, as `can_record` does: can the apiclient, and may this
user, both have to say yes.

**Playlists deliberately do not move with it.** `PlaylistController` carries
no such policy, so a user who cannot touch a collection can still make a
playlist; gating both would have taken away something that works, which is
the shape of half the bugs in this document. The Add To dialog therefore
keeps its playlist half in full and loses only the door to the collections
picker. `tests/test_user_policy.py:TheCollectionAffordances` asserts that as
hard as it asserts the hiding.

**The stdjflib half.** `qa-user`'s description already claimed "everything a
non-admin can have" and this was missing from its policy, so no account on
the QA server except the administrator could reach the feature at all — the
same omission, and the same fix, as `EnableLiveTvManagement` in item 3.
Granted there now; the other ten accounts still lack it, which is the state
this gap is about and is what makes it testable.

Pinned by `tests/test_user_policy.py` (the accessor, the two affordances, and
that playlists survive) and `tests/e2e/test_collections.py:CollectionPermissionTest`,
which is where the field *spelling* is checked and where the premise lives:
that the server really would have refused. If Jellyfin ever relaxes this, that
test fails and hiding the button becomes the thing to reconsider.

## 6. Deleting media — the one gate we take from the item, not the account  — ADDED

Not a gap that was found; a feature that had to be built without making one.
Delete from Disk (#4, `docs/UI_FIXES_4.md`) is the first thing in the browser
that destroys anything on the server, so the question of who may press it had
to be answered before the button existed rather than after.

**The gate is the item's own `CanDelete`, and nothing else.** That is
jellyfin-web's test (`itemContextMenu.js:210`, `if (item.CanDelete && …)`) and
it is the only correct one available to a client: the server grants deletion
per *library* (`EnableContentDeletionFromFolders`), so an account can be
allowed to delete from one library and refused on another. Reading
`EnableContentDeletion` off the user policy — the shape every other entry in
this document uses — would be right about the account and wrong about half
their libraries, in both directions.

**It has to be asked for.** Measured against 10.11: a list query omits
`CanDelete` entirely (the key is absent, not `False`), so an entry keyed off
it would simply never appear; a single-item fetch returns it whatever `Fields`
says, but depending on that would make the detail page's button rest on an
undocumented default. It is therefore in `GRID_FIELDS`, `LIST_FIELDS` and
`DETAIL_FIELDS`. The cost is nothing measurable — +1.8 KB on a 165 KB
hundred-item grid, no change in query time — which is worth having measured,
because per-item permission fields on this API are exactly the ones that have
been expensive before.

**Absent means no, and that is deliberately the opposite of §4's fail-open.**
`may_download` fails *open* because a missing permission there costs a
convenience and a wrong guess costs a working feature. Here the asymmetry is
reversed: hiding a delete the user could have made is an inconvenience, and
offering one that 403s at the point of no return is not. There is no
`user_policy` accessor for this at all, on purpose — adding one would invite
exactly the account-level reading the first paragraph rules out.

**The stdjflib half.** The QA server's own administrator account has
`EnableContentDeletion: False` (measured), which is a *useful* default and has
been left alone: it makes "the entry is correctly absent" the state you get
without arranging anything, and the granting case the one that has to be set
up deliberately. Nothing in the test suite deletes from a real server.

Pinned by `tests/test_shell_delete_item.py` — the gate, that an absent field
is a refusal, that offline does not offer it, and that the confirmation says
what it destroys without truncating the sentence.
