"""Shared fixture names and origin probing for the two `.strm` modules.

stdjflib ships stream files pointing at **two kinds of origin**, and which one
a test wants is a real choice rather than a detail:

* **Local origin** — `http://127.0.0.1:8410/...`, served by `stdjflib serve`
  itself out of `.stdjflib/origin/`, 30 seconds of h264+aac. Genuinely remote
  as far as Jellyfin is concerned (`Protocol=Http`, `IsRemote=true`, probed
  over HTTP like anything else); only the route is local. Anything that just
  needs "the media source is not a file on disk" belongs here — it is offline,
  fast, and cannot fail because somebody else's server is busy.

* **Catalogue origin** — archive.org over TLS. A real host, real TLS, real
  redirects. Exactly one test still needs it: the commented fixture, which is
  the file built out of everything `FetchShortcutInfo` tolerates.

Everything else is local, including the two shapes that used to force the
network — a clip long enough to hold a resume position (`LONG_MOVIE`, 400s
against the server's 300s floor) and a `.strm` grouped as an alternate version
(`VERSIONS`).

Tests that need the network say so with `require_origin`, which skips rather
than fails — somebody else's host being slow is not a defect in this client.

**Why the alternate in a version set has no runtime.** Not because the server
refuses to probe alternates as such: the probe is gated on the *item's* path
ending in `.strm`, and a version set's `item.Path` is its **primary's** — here
an `.mkv`. So the shortcut in the set is never probed, and pinning
`MediaSourceId` to it does not help. A loose `.strm`, whose own path is the
stream file, comes back probed. (`allowMediaProbe` is passed true either way,
so that is not the gate.)
"""

import urllib.error
import urllib.request

#: Local-origin fixtures, served from 127.0.0.1:8410 by `stdjflib serve`.
LOCAL_MOVIE = "Local Origin Stream Movie"           # 30s
#: 400s, and the only item in the library that can hold a resume position:
#: the server discards one below `MinResumeDurationSeconds` (300). The usable
#: seek window is 20s–360s (`MinResumePct` 5, `MaxResumePct` 90).
LONG_MOVIE = "Long Origin Stream Movie"
#: 10s local `.mkv` primary with a `.strm` alternate pointing at the 30s clip.
#: The alternate is never probed — see the note above — so it has no runtime.
VERSIONS = "Local Origin Versions"
#: The same shape the other way up: the `.strm` is named exactly like the
#: folder, so it takes the *primary* slot and `item.Path` ends in `.strm`,
#: which is what makes the probe gate fire. Both sources therefore carry a
#: real runtime — 30s remote primary, 20s local alternate — which is the only
#: way to assert that the shim uses an alternate's *own* number rather than
#: the Item's. Deliberately a second fixture rather than a change to
#: `VERSIONS`: renaming that one would flip which side goes unprobed and
#: destroy the shape that reproduces the bug.
PROBED_VERSIONS = "Origin Primary Versions"
#: `Remote Stream Show` S01E04. Looked up by index, not name — an episode
#: title is the sort of thing a fixture refresh renames.
LOCAL_EPISODE_INDEX = 4

#: Catalogue origin (archive.org). Only the parser fixture still needs one:
#: it is the file built out of exactly what `FetchShortcutInfo` tolerates,
#: and there is no local-origin equivalent yet.
COMMENTED = "Commented Stream File"

#: Neither of these reaches anybody: a loopback port with nothing listening,
#: and a filesystem path. They test the protocol field and the refusal.
RTSP = "RTSP Stream Movie"
LOCAL_PATH = "Stream File Naming A Local Path"

STRM_SHOW = "Remote Stream Show"

_reachable = {}


def url_reachable(url):
    """Whether a HEAD against `url` answers. Cached per URL.

    Deliberately takes the URL off the fixture rather than hardcoding a
    hostname: if stdjflib repoints these, this keeps asking the right
    question instead of testing a string we wrote down.
    """
    if url in _reachable:
        return _reachable[url]
    ok = False
    if url.startswith("http"):
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=20) as resp:
                ok = 200 <= resp.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            ok = False
    _reachable[url] = ok
    return ok


def source_url(session, item):
    """The origin URL an item's `.strm` names, straight off the DTO."""
    full = session.api.get_item(item["Id"])
    for source in full.get("MediaSources") or []:
        path = source.get("Path") or ""
        if path.startswith("http"):
            return path
    return ""


def origin_reachable(item_name, library="Movies"):
    """Can we reach the origin behind this fixture? One login, cached.

    Used to gate whole classes, so it builds and drops its own session rather
    than borrowing a test's.
    """
    key = "fixture:%s/%s" % (library, item_name)
    if key in _reachable:
        return _reachable[key]
    _reachable[key] = False
    session = None
    try:
        from _e2e import Session, server_reachable
        if not server_reachable():
            return False
        session = Session()
        _reachable[key] = url_reachable(
            source_url(session, session.find(item_name, library=library)))
    except Exception:
        _reachable[key] = False
    finally:
        if session is not None:
            session.stop()
    return _reachable[key]


def require_origin(item_name=LOCAL_MOVIE, library="Movies"):
    """Skip unless this fixture's origin answers."""
    import unittest
    return unittest.skipUnless(
        origin_reachable(item_name, library),
        "the origin behind %r is unreachable; a .strm cannot be played "
        "without it" % item_name)


def local_episode(session):
    """`Remote Stream Show` S01E04 — the episode whose `.strm` is local."""
    for episode in session.episodes(STRM_SHOW, season=1):
        if episode.get("IndexNumber") == LOCAL_EPISODE_INDEX:
            return episode
    raise AssertionError(
        "no S01E%02d in %r; stdjflib may predate the local-origin fixtures"
        % (LOCAL_EPISODE_INDEX, STRM_SHOW))


def strm_media(session, item_ids, srcid=None):
    """A real `Media`, optionally pinned to one media source.

    `_e2e.build_media` takes no `srcid`, and choosing the source is the whole
    subject of the alternate-version tests.
    """
    from jellyfin_mpv_shim.media import Media
    return Media(session.client, list(item_ids), user_id=session.user_id,
                 srcid=srcid)


def strm_video(session, item, srcid=None):
    """A real `Video` for `item`, resolved through `PlaybackInfo`.

    Going through `Media` is the app's own play path, so this is the request
    the shim actually sends — including the probe that is the only reason a
    stream file has a runtime at all.
    """
    video = strm_media(session, [item["Id"]], srcid=srcid).video
    assert video is not None, "Media built no video for %r" % item["Name"]
    video.get_playback_url()
    return video
