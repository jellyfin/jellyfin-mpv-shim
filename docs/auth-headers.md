# Where the access token may go

The Jellyfin access token authenticates every request this client makes. mpv
makes some of them on our behalf — the stream, and every external subtitle
sidecar — and there are two ways to give it the credential:

- **`Authorization: MediaBrowser Token="…"`**, via mpv's `http-header-fields`.
  The scheme that is *not* gated behind `EnableLegacyAuthorization`, and the
  one that keeps the token out of logs, out of `ps` output and out of every
  proxy in the path.
- **`?ApiKey=…`** in the URL. `ApiKey`, not `api_key`: the server reads both
  in the same place, but `api_key` is gated on `EnableLegacyAuthorization`,
  off by default from Jellyfin v12.

Everything awkward here follows from one fact: **`http-header-fields` is
global and persistent.** It applies to every request mpv makes, not just the
one we set it for, and mpv is not re-created between queue items
(`docs/mpv-backends.md` section 6). So a per-item decision has to be
implemented on an option with no per-item scope.

The tables below are enforced by `tests/test_auth_header_truth_table.py`.

## 1. Two decisions, at two times

| | Where | When | Asks |
|---|---|---|---|
| **A** | `_apply_auth_headers` (`player.py`) | before the URL is built | may mpv hold our header at all? |
| **B** | the `same_origin` check in `play()` | after the URL is built | does the media actually come from us? |

Neither can move. A has to precede the URL because `_get_url_from_source`
needs to know whether the URL must carry a token itself. B has to follow it
because the stream's host does not exist until PlaybackInfo has run.

## 2. Decision A — is the header installed?

Every row after the first answers "no", and **every "no" must still clear the
option**: the previous item's token is sitting in it, and declining to set a
new one is not the same as taking the old one back. The clear is at the top of
the function rather than on each `return False` for exactly that reason.

| Condition | Installed | Why |
|-----------|-----------|-----|
| all clear | yes | the ordinary case |
| mpv is not alive | no | the handle is dead and `_ensure_mpv` re-creates it moments later, holding no header of ours. Writing a property on a destroyed libmpv handle takes the process with it, so this returns *before* the clear as well |
| no client | no | a half-built client must answer "no header", not `AttributeError` out of a start |
| the header cannot be built | no | same |
| the header carries no `Token=` | no | an unauthenticated probe. Claiming success would strip a URL that needs one |
| the item has a **foreign subtitle host** | no | `http-header-fields` is global: mpv would send our token to whoever hosts that subtitle. There is no per-URL header option, so the only safe answer is not to set it |
| mpv refuses the option | no | fall back to the URL |

`foreign_subtitle_hosts()` reads each subtitle's **`Path`**, not its
`DeliveryUrl`, because this has to be answerable before PlaybackInfo. The
server sets `IsExternalUrl` exactly when `Path` is already an absolute
`http(s)` URI (`StreamInfo.cs:1264-1274`), so the same test on `Path` is the
honest pre-check. Both sides fail closed, and they have to, because only one of them can. A
raise that escapes the check is caught by `_apply_auth_headers`, which then
answers "foreign". A failure *inside* it — `auth.server` unparseable, or a
malformed port — never reaches the caller at all, so the function answers
`{"unknown"}` itself. Returning an empty set there reads to the caller as
"nothing foreign", and it installs the header: the one shape where a guard
above cannot help, whatever the caller does.

**`IsExternalUrl` does not mean "third party."** It means the path was
absolute, which is often the same host (a plugin, a co-located file server)
and sometimes not, and the DTO does not say which. So the test everywhere in
this codebase is the origin, never the flag: `utils.same_origin`, one
implementation, used by the player, the sync downloader **and
`foreign_subtitle_hosts`** — which for a long time kept its own copy of the
tuple comparison while this paragraph claimed otherwise.

That copy is why the port rule is worth stating: an origin compares
scheme, host and port, and **a port the URL does not spell out is the
scheme's default**. Comparing raw ports made `https://host` and
`https://host:443` two origins, so a sidecar on our own server, written with
its port, was ruled foreign — which refuses the header *and* makes
`reauthorize_sidecars` skip it, leaving that URL with no credential of any
kind. `tests/test_auth_header_truth_table.py` pins the two against each
other, so a second copy fails the suite rather than the invariant.

## 3. The invariant across both

> **If mpv is not carrying our header, our own same-origin sidecar URLs must
> carry a token themselves.**

`map_streams` builds a sidecar URL from its `DeliveryUrl` and deliberately
adds no token, on the assumption the header will cover it. So every path that
ends with the header off strands those URLs with no credential at all — a 401
and no captions.

This is stated once, on the *outcome* (`if not video.auth_via_header:` in
`play`), and not per branch. It used to live inside decision B's revoke, which
is one of the several ways the header ends up off — and the miss was worse
than random, because decision A's most likely refusal is the foreign-subtitle
one, where the reason the header is declined **is** that the item has external
subtitles. There is always one of ours beside the third-party one.

`reauthorize_sidecars` is same-origin-gated and idempotent, which is what
makes saying it once for every outcome safe where saying it per branch was
not. A sidecar on somebody else's host still gets nothing, which is the rule
the whole subsystem exists to enforce; `test_and_the_foreign_one_still_gets_nothing`
is the control on that.

Decision B's other half: when the header is revoked because the *media* is on
another host, the URL is left as the server gave it and never gains a token.
That is correct — `_get_url_from_source` returns a foreign path unchanged and
has nothing to lose.

## 4. The same rule in the downloader

`sync/manager.py` fetches media, artwork, trickplay tiles, subtitles and
playlist posters itself, and authenticates all of them through one function,
`_headers_for`. It is one function rather than a line per call site because
the call sites *were* the failure mode: the subtitle sidecar was converted to
the header and the other six requests were not, so an offline download went on
fetching everything else with `?ApiKey=` in the URL. Six of the seven swallow
their own exceptions, so against a proxy requiring the header they fail
silently — the download completes and the artwork is simply absent.

`tests/test_sync_auth_headers.py` enumerates the call sites so a new one
cannot repeat it. Add a request to that module and add it to that test.
