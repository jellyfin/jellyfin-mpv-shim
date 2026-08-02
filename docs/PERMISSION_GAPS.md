# User-permission gaps — work items

Raised by `tests/e2e/test_account_policy.py` running against stdjflib's twelve
QA accounts, which is the first time anything automatic had exercised them.

The principle, and the reason these are worth doing: **showing UI the server
will refuse creates confusion and issue reports.** A user with SyncPlay
revoked who is offered a SyncPlay button does not conclude they lack
permission; they conclude the client is broken, and they are half right.

Nothing here is a crash, and none of it is asserted as a failure by the test
suite — `test_account_policy` pins what the code currently promises and its
docstring names these as known gaps.

## 1. SyncPlay is offered to users who do not have it

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

**Testable once fixed**: `qa-nosyncplay` must not be offered any of the three
entry points, and `qa-user` must still get all of them.

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
