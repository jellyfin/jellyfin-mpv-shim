import requests
import datetime
import logging
import re
import time
import webbrowser

from urllib.parse import urljoin, urlsplit

from .constants import CLIENT_VERSION
from .conf import settings
from .i18n import _
from .version import is_newer

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type

log = logging.getLogger("update_check")

release_url = "https://github.com/jellyfin/jellyfin-mpv-shim/releases/"
release_urls = [release_url]
one_day = 86400

#: The one host a release URL may live on. Checked against ``hostname``, which
#: is lower-cased and, crucially, is the *real* host of something like
#: ``https://github.com@example.invalid/`` -- which a string comparison
#: against the raw netloc reads as ours.
GITHUB_HOST = "github.com"

#: GitHub owners a repository move will be followed into.
#:
#: GitHub answers the old URL of a renamed or transferred repository with a
#: permanent redirect to its new one, and following that is the only thing
#: keeping everyone who installed a build from being told about updates
#: forever after a move. But a redirect is not evidence of anything: it says
#: where the account that holds the name is pointing today, so following one
#: unconditionally means an account takeover can re-home every existing
#: install's update notice at whatever it likes. These three are the places
#: this project could legitimately end up -- the org it is in, the org for
#: its incubated projects, and the author -- and a move anywhere else is
#: refused and logged rather than followed. The repository *name* is
#: deliberately not pinned: a rename inside one of these is the ordinary case
#: this exists for.
ALLOWED_OWNERS = frozenset(("jellyfin", "jellyfin-labs", "iwalton3"))

#: How many redirects to follow before giving up. A move is one hop and the
#: latest -> tag lookup is another; the slack is for a repository that has
#: been moved twice without GitHub having collapsed the chain.
MAX_HOPS = 4

_REDIRECTS = (301, 302, 303, 307, 308)

#: Wall-clock budget for the WHOLE chain, in seconds.
#:
#: ``check()`` is called from ``PlayerManager._play_media``, which holds the
#: player's ``_lock`` -- the one thirty-odd methods share -- so this runs with
#: pause, seek and stop blocked behind it. One request could stall for
#: ``3 + 10`` seconds; following redirects one at a time made the worst case
#: that times ``MAX_HOPS``, which is most of a minute of dead transport
#: controls on the first play of the day against a captive portal or a stalled
#: proxy. The budget puts the ceiling back where it was. The ordinary case is
#: untouched: it is one request, and the first hop still gets the full read
#: timeout.
CHAIN_BUDGET = 15.0

#: A tag has to look like a version before it is compared with one.
#:
#: ``is_newer`` falls back to string ordering for anything it cannot parse, and
#: **every** unparseable tag sorts as newer -- so a release named ``stable``,
#: or a redirect to a bare ``/releases/tag/``, announced a permanent update to
#: a version with no number in it. ``has_notified`` latches and ``new_version``
#: is never cleared, so "MPV Shim vtag Update Available" would have been the
#: rest of that session.
_VERSION_TAG = re.compile(r"^v?\d")

#: ``/<owner>/<repo>/releases/<rest>``. The owner and repository are held to
#: the character set GitHub allows in a name, so the base this rebuilds is a
#: URL we composed out of known-shaped parts rather than a server's string
#: with our scheme in front of it.
_RELEASE_PATH = re.compile(
    r"^/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)"
    r"/releases/(.*)$")


def parse_release_url(url: str):
    """``(base, rest)`` for a GitHub releases URL worth following, else None.

    ``base`` comes back rebuilt as
    ``https://github.com/<owner>/<repo>/releases/`` and ``rest`` as whatever
    followed it (``latest``, ``tag/v3.0.0``), so the
    caller only ever requests a URL this function has vouched for rather than
    the string a server handed us. Query and fragment are dropped with it.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != GITHUB_HOST:
            return None
        # Reading .port is what raises on a malformed authority, so it is
        # inside the try with urlsplit rather than in one of its own.
        if parts.port not in (None, 443):
            return None
    except ValueError:
        return None
    match = _RELEASE_PATH.match(parts.path)
    if match is None:
        return None
    owner, repo, rest = match.groups()
    if owner.lower() not in ALLOWED_OWNERS:
        return None
    # The charset allows a name of nothing but dots, which no repository has
    # and which would put a dot segment in a URL we then hand to a browser.
    if repo.strip(".") == "":
        return None
    return "https://%s/%s/%s/releases/" % (GITHUB_HOST, owner, repo), rest


class UpdateChecker:
    def __init__(self, player_manager: "PlayerManager_type"):
        self.playerManager = player_manager
        self.has_notified = False
        self.new_version = None
        self.last_check = None
        #: Where the releases live *now*. Starts at the compiled-in URL and
        #: moves with the repository, so the notice and the menu item open the
        #: page the version we just read came from rather than a redirect.
        self.release_url = release_url

    def _resolve_latest(self, base: str):
        """``(version, base)`` for the newest release under ``base``.

        ``base`` comes back because it may not be the one that went in: GitHub
        redirects the old URL of a moved repository, and the answer is only
        meaningful together with the place it came from. ``None`` if the chain
        does not end at a tag under an owner we trust (see ``ALLOWED_OWNERS``).
        """
        url = base + "latest"
        seen = {url}
        deadline = time.monotonic() + CHAIN_BUDGET
        for hop in range(MAX_HOPS):
            remaining = deadline - time.monotonic()
            if hop and remaining <= 0:
                log.warning("Release lookup ran out of time.")
                return None
            response = requests.get(
                url, allow_redirects=False,
                timeout=(3, max(1.0, min(10.0, remaining))))
            if response.status_code not in _REDIRECTS:
                log.warning("Release page returned bad status code %s.",
                            response.status_code)
                return None
            sent = response.headers.get("location")
            if not sent:
                # Named rather than folded into the join below: urljoin("")
                # answers the URL we asked for, which then trips the loop
                # detector and reports a malformed response as a GitHub
                # redirect loop -- pointing debugging at the wrong thing.
                log.warning("Release redirect carried no location.")
                return None
            # Relative against the URL we asked for: a Location may legally be
            # a path, and joining it is also what turns "/other/repo" into
            # something parse_release_url can judge.
            location = urljoin(url, sent)
            target = parse_release_url(location)
            if target is None:
                log.warning("Refusing to follow release redirect to %s.",
                            location)
                return None
            base, rest = target
            if rest.startswith("tag/"):
                # .../releases/tag/v2.10.0 -> 2.10.0. Everything after "tag/",
                # not the last path segment: a git tag may legally contain a
                # slash, and taking the tail of one silently invents a version.
                # The "v" is stripped rather than assumed, so the tags dropping
                # it some day changes nothing here.
                tag = rest[4:].strip("/")
                if not _VERSION_TAG.match(tag):
                    log.warning("Release tag is not a version: %r", tag)
                    return None
                return (tag[1:] if tag[:1] in ("v", "V") else tag), base
            if rest != "latest":
                log.warning("Release redirect went somewhere unexpected: %s",
                            location)
                return None
            url = base + rest
            if url in seen:
                log.warning("Release redirect loops through %s.", url)
                return None
            seen.add(url)
        log.warning("Release page redirected too many times.")
        return None

    def _check_updates(self):
        log.info("Checking for updates...")
        for base in release_urls:
            try:
                found = self._resolve_latest(base)
                if found is None:
                    continue
                version, base = found
                # Adopt the resolved home whatever the answer was: if the
                # repository has moved, the menu item and the notice should
                # open where it moved to, not a URL that works only for as
                # long as GitHub keeps redirecting it.
                self.release_url = base
                # A difference is not an upgrade: this also runs on
                # pre-releases and local builds, whose version is *ahead* of
                # the newest stable tag. /releases/latest never points at a
                # pre-release, so there is nothing here to opt in or out of.
                if is_newer(version, CLIENT_VERSION):
                    self.new_version = version
                    break
                log.info("Up to date (running %s, latest release %s).",
                         CLIENT_VERSION, version)
            except Exception:
                log.error("Could not check for updates.", exc_info=True)
        return self.new_version is not None

    def check(self):
        if not settings.check_updates:
            return

        if (
            self.last_check is not None
            and (datetime.datetime.utcnow() - self.last_check).total_seconds() < one_day
        ):
            log.info("Update check performed in last day. Skipping.")
            return

        self.last_check = datetime.datetime.utcnow()
        if self.new_version is not None or self._check_updates():
            if not self.has_notified and settings.notify_updates:
                self.has_notified = True
                log.info("Update Available: {0}".format(self.new_version))
                self.notify()

    def notify(self):
        """Surface the available update. When a UI is running (it sets
        ``notify_update``) the notice goes to the browser window; otherwise it
        falls back to an MPV OSD toast for CLI/headless users."""
        notify_ui = getattr(self.playerManager, "notify_update", None)
        if notify_ui is not None:
            try:
                notify_ui(self.new_version, self.release_url + "latest")
                return
            except Exception:
                log.error("Could not send update notice to the UI.", exc_info=True)
        self.playerManager.show_text(
            _(
                "MPV Shim v{0} Update Available\nOpen menu (press c) for details."
            ).format(self.new_version),
            5000,
            1,
        )

    def open(self):
        self.playerManager.set_fullscreen(False)
        try:
            webbrowser.open(self.release_url + "latest")
        except Exception:
            log.error("Could not open release URL.", exc_info=True)
