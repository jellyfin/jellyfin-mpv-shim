"""Fixtures for the end-to-end suite: a real Jellyfin server, a real mpv.

Everything else in `tests/` runs against a fake. That is deliberate and it is
why those suites are fast and deterministic — but it also means the request
the shim actually sends is never validated by anything, and the DTOs it
reasons about are hand-written by us. `docs/E2E_PLAN.md` has the evidence:
`enable_images=False` to a method with no such argument, `is_movies` for
`IsMovie` (with a unit test asserting the wrong spelling, which is why it was
green), a SyncPlay ping sent as a float against a `long`. None of those are
reachable without a server on the other end.

The server is stdjflib's:

    ./stdjflib.py serve ~/Desktop/std-jf-lib --live-tv
    JMS_E2E_SERVER=http://127.0.0.1:8096 python3 tests/e2e/run_e2e.py

With `JMS_E2E_SERVER` unset — or set and unreachable — every test here skips,
the same discipline as the integration suite's capability gating. A bare
machine exits clean.

Not named `test_*` so unittest discovery ignores it. `tests/e2e/` has no
`__init__.py`, so `python3 -m unittest discover tests` never recurses in.
"""

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

# The integration harness already probes mpv/ffmpeg/display, primes the arg
# parser and knows how to tell a FakeMPV-bound player module from a real one.
# Importing it beats a second copy that drifts: these are the same questions,
# and the answers have to stay the same.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "integration"))
import _harness as h  # noqa: E402

SERVER = (os.environ.get("JMS_E2E_SERVER") or "").rstrip("/")
BACKEND = h.BACKEND

# Done at import, before any test module can pull a shim module in. Two traps,
# both of which bite whichever module imports first rather than the one at
# fault:
#   * the app parses sys.argv the first time anything resolves the config dir,
#     and under a test runner argv carries tokens its argparse rejects — it
#     exits with a usage message, which reads as a broken test file.
#   * without an isolated config dir the suite reads and writes the
#     developer's real one.
_CONFIG_DIR = tempfile.mkdtemp(prefix="jms-e2e-config-")
os.environ["XDG_CONFIG_HOME"] = _CONFIG_DIR
h.prime_args()

# stdjflib's fixed accounts. Password is the same for all of them except
# qa-nopassword, whose whole point is not having one.
PASSWORD = "stdjflib"

# The server uuid a LibrarySource built by `Session.library_source` answers to.
SOURCE_UUID = "e2e"


def public_users():
    """`/Users/Public` — the login list a client offers without credentials.

    Deliberately raw rather than through the apiclient: this endpoint is what
    an unauthenticated client sees, so asking it with a token would be asking
    a different question.
    """
    with urllib.request.urlopen(SERVER + "/Users/Public", timeout=10) as resp:
        return [u.get("Name") for u in json.loads(resp.read())]


def login_refused(account, password=PASSWORD, device_id=None):
    """True when the server refuses this login.

    Returns rather than raises so a test can say what it means. `Session`
    raises on a refusal because every other test wants that; here the refusal
    *is* the assertion.
    """
    try:
        session = Session(account, password, device_id=device_id)
    except AssertionError:
        return True
    session.stop()
    return False


# --------------------------------------------------------------------------
# Capability gate
# --------------------------------------------------------------------------

_reachable = None


def server_reachable():
    """Is there a Jellyfin on the other end? Probed once, cached.

    Requires a parseable payload, not merely a socket that accepts: a server
    still shutting down on the same port accepts and closes, and treating that
    as "up" makes the next call fail with a connection error pointing nowhere
    near the cause. (stdjflib's own `wait_until_up` learned this the same way.)
    """
    global _reachable
    if _reachable is not None:
        return _reachable
    _reachable = False
    if SERVER:
        try:
            with urllib.request.urlopen(
                    SERVER + "/System/Info/Public", timeout=5) as resp:
                _reachable = bool(json.loads(resp.read()).get("Id"))
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            _reachable = False
    return _reachable


def require_server(obj):
    return unittest.skipUnless(
        server_reachable(),
        "no server: set JMS_E2E_SERVER to a running Jellyfin "
        "(see docs/E2E_PLAN.md)",
    )(obj)


def require_server_and_mpv(obj):
    return require_server(h.require_real_mpv(obj))


# --------------------------------------------------------------------------
# Settings + the player module
# --------------------------------------------------------------------------

def quiet_settings():
    """Turn off everything that is not under test, and pin the backend.

    **Must run before `jellyfin_mpv_shim.player` is imported** — player.py
    picks its mpv backend at import time and builds its singleton there.
    """
    from jellyfin_mpv_shim.conf import settings
    h.prime_args()
    settings.thumbnail_enable = False
    settings.shader_pack_enable = False
    settings.menu_mouse = False
    settings.svp_enable = False
    settings.discord_presence = False
    settings.check_updates = False
    # "none" keeps the OSC lua out of in-process libmpv (its teardown at
    # interpreter exit goes racy with a script loaded) and suppresses mpv's
    # own OSC — same reasoning as test_realmpv_smoke.
    settings.osc_style = "none"
    settings.fullscreen = False
    settings.auto_play = True
    settings.mpv_ext = (BACKEND == "jsonipc")
    settings.mpv_ext_start = True
    return settings


def isolate_config():
    """Point the config dir at a throwaway.

    The suite writes credentials and playstate; a developer running it must
    not find their real `cred.json` rewritten. Called before any shim import.
    """
    path = tempfile.mkdtemp(prefix="jms-e2e-config-")
    os.environ["XDG_CONFIG_HOME"] = path
    return path


_PLAYER = None


def ensure_real_player():
    """The process's one real player, built on first use and torn down at exit.

    `playerManager` is a **module-level singleton**, so it is shared by every
    test class in the interpreter — which makes per-class teardown wrong.
    Terminating it in `tearDownClass` leaves the next class holding a dead
    player, and the two backends do not fail the same way: in-process libmpv
    quietly re-creates itself and the run looks fine, while external mpv is a
    separate process that is simply gone, and the next `play` dies with
    `BrokenPipeError: socket is closed`. Exactly the kind of one-backend
    divergence the matrix exists to surface — it caught this on its first run.
    """
    global _PLAYER
    if _PLAYER is None:
        _PLAYER = import_real_player()
        import atexit
        atexit.register(_terminate_player)
    return _PLAYER


def _terminate_player():
    try:
        if _PLAYER is not None:
            _PLAYER.playerManager.terminate()
    except Exception:
        pass


def import_real_player():
    """Import `jellyfin_mpv_shim.player` bound to a REAL mpv backend.

    In a full-suite run the fake-mpv legs may already have imported player
    against `FakeMPV`, and player.py caches its singleton at import time — a
    plain import would hand that back and this suite would silently smoke-test
    the fake. Ask the *player module* what it is bound to rather than
    `sys.modules`, which the integration harness deliberately restores.
    """
    quiet_settings()
    player_mod = sys.modules.get("jellyfin_mpv_shim.player")
    if player_mod is not None and _is_fake(getattr(player_mod, "mpv", None)):
        sys.modules.pop("jellyfin_mpv_shim.player")
    for name in ("mpv", "python_mpv_jsonipc"):
        if _is_fake(sys.modules.get(name)):
            del sys.modules[name]
    import jellyfin_mpv_shim.player as player_module
    assert not _is_fake(player_module.mpv), "e2e suite is bound to FakeMPV"
    return player_module


def _is_fake(mod):
    return mod is not None and getattr(mod, "MPV", None) is h.FakeMPV


# --------------------------------------------------------------------------
# A logged-in connection
# --------------------------------------------------------------------------

class Session:
    """One authenticated connection, built the way the app builds one.

    The token half is `clients.ClientManager.login`; the "we already hold a
    token, just bring the HTTP session up" half is `repository.ServerConn`.
    Between them that is every way the app reaches a server.
    """

    def __init__(self, account="qa-user", password=PASSWORD, device_id=None):
        from jellyfin_apiclient_python import JellyfinClient
        from jellyfin_mpv_shim.constants import (
            CLIENT_VERSION, USER_APP_NAME, USER_AGENT,
        )

        # Deterministic, one per account, and NOT a fresh uuid per Session.
        # A real client has one persistent device id, and the server keeps a
        # Device record per id forever: a random one leaked a device per
        # Session ever constructed (119 of them before this was noticed),
        # which is junk on the server and makes `/Sessions` lookups
        # ambiguous. Tests that genuinely need to look like a second device
        # — qa-onesession — pass `device_id` explicitly.
        self.device_id = device_id or ("jms-e2e-" + account)
        self.account = account

        client = JellyfinClient(allow_multiple_clients=True)
        client.config.data["app.default"] = True
        client.config.app(USER_APP_NAME, CLIENT_VERSION, "jms-e2e", self.device_id)
        client.config.data["http.user_agent"] = USER_AGENT
        client.config.data["auth.ssl"] = False

        client.auth.connect_to_address(SERVER)
        result = client.auth.login(SERVER, account, password or "")
        if "AccessToken" not in result:
            raise AssertionError(
                "login failed for %s: %r" % (account, result))
        self.token = result["AccessToken"]
        self.user_id = result["User"]["Id"]

        client.config.auth(SERVER, self.user_id, self.token, False)
        client.logged_in = True
        client.start(websocket=False)

        self.client = client
        self.api = client.jellyfin

    def stop(self):
        """Revoke the token, then close the HTTP session.

        `client.stop()` alone only closes the socket — the session stays
        registered on the server, which is how a suite that logs in freely
        exhausts `qa-onesession`'s cap with its own leftovers. Logging out is
        also what keeps `/Sessions` readable for the tests that consult it.
        """
        try:
            self._request("/Sessions/Logout", method="POST")
        except Exception:
            pass
        try:
            self.client.stop()
        except Exception:
            pass

    def _request(self, path, method="GET", body=None):
        """A raw authenticated call, for the handful of endpoints the
        apiclient does not expose (logout, the Devices and Policy admin
        APIs)."""
        headers = {"Authorization": 'MediaBrowser Token="%s"' % self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(SERVER + path, method=method,
                                     data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None

    def policy(self):
        return (self.api.get_user() or {}).get("Policy") or {}

    def set_policy(self, policy):
        """Replace this user's policy. Admin only.

        Used by the Live TV timer tests, which cannot run at all without
        `EnableLiveTvManagement` — stdjflib grants it to nobody, not even
        qa-admin. They grant it, run, and put the original back.
        """
        self._request("/Users/%s/Policy" % self.user_id, method="POST",
                      body=policy)

    def purge_devices(self, account):
        """Delete every Device record belonging to `account`. Admin only.

        For `qa-onesession`, whose cap counts what the server still believes
        is connected: a session left behind by a crashed run refuses the next
        one, and the failure looks like the cap working rather than like
        litter. Called from `setUp` so the test starts from a known state
        however the last run ended.
        """
        devices = self._request("/Devices") or {}
        items = devices.get("Items", devices if isinstance(devices, list) else [])
        for device in items:
            if device.get("LastUserName") != account:
                continue
            try:
                self._request("/Devices?id=" + urllib.parse.quote(device["Id"]),
                              method="DELETE")
            except Exception:
                pass

    def library_source(self):
        """A real `LibrarySource` over this session's credentials.

        The browser is handed one of these and does everything through it, so
        it is the seam the contract tests belong at — above the apiclient,
        below the UI.
        """
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
        source = LibrarySource(
            [{"uuid": SOURCE_UUID, "name": "e2e", "address": SERVER,
              "user_id": self.user_id, "token": self.token}],
            self.device_id, "jms-e2e", False)
        return source

    # -- lookup ------------------------------------------------------------
    #
    # Always by NAME, never by id. Ids are assigned by the server on scan and
    # change on every reprovision; a baked-in GUID is a test that passes once.

    def view(self, name):
        for item in self.api.get_views()["Items"]:
            if item["Name"] == name:
                return item
        raise AssertionError("no library named %r (have: %s)" % (
            name, [i["Name"] for i in self.api.get_views()["Items"]]))

    def find(self, name, library=None, item_type=None, fields=None):
        """One item by exact `Name`, optionally within a library.

        Filtered client-side on purpose. **`NameStartsWith` matches SortName,
        not Name**, and SortName has the leading article stripped — measured
        against this server: `NameStartsWith="The Standard Show"` returns
        nothing at all, `"Standard"` returns it, and the item's SortName is
        `"standard show"`. Narrowing the query with the name you can see is a
        filter that silently matches nothing, which is the exact shape of bug
        this suite exists to catch, so it does not get to live in the harness.
        """
        items = self.find_all(library=library, item_type=item_type,
                              fields=fields)
        exact = [i for i in items if i["Name"] == name]
        if not exact:
            raise AssertionError("no %s named %r in %r (saw %d items: %s)" % (
                item_type or "item", name, library, len(items),
                [i["Name"] for i in items[:10]]))
        return exact[0]

    def find_all(self, library=None, item_type=None, fields=None,
                 parent_id=None, **params):
        query = {"Recursive": True}
        if parent_id is not None:
            query["ParentId"] = parent_id
        elif library is not None:
            query["ParentId"] = self.view(library)["Id"]
        if item_type:
            query["IncludeItemTypes"] = item_type
        if fields:
            query["Fields"] = fields
        query.update(params)
        return self.api.user_items(params=query)["Items"]

    def episodes(self, series_name, season=None, library="Shows"):
        """A series' episodes in broadcast order."""
        series = self.find(series_name, library=library, item_type="Series")
        eps = self.find_all(parent_id=series["Id"], item_type="Episode",
                            SortBy="ParentIndexNumber,IndexNumber",
                            SortOrder="Ascending")
        if season is not None:
            eps = [e for e in eps if e.get("ParentIndexNumber") == season]
        return eps

    # -- state -------------------------------------------------------------

    def user_data(self, item_id):
        return self.api.get_item(item_id).get("UserData") or {}

    def reset_played(self, *item_ids):
        """Clear watched state and resume position so a run is repeatable.

        Registered with `addCleanup` by the tests that dirty it. Playstate is
        the one piece of server state these tests mutate that another test can
        actually see.
        """
        for item_id in item_ids:
            try:
                self.api.item_played(item_id, False)
            except Exception:
                pass

    def my_session(self):
        """This device's entry in the server's session list, if it has one.

        The server's own view of what we are playing — the half of the
        reporting loop `test_realmpv_smoke` fakes.
        """
        try:
            for sess in self.api.sessions():
                if sess.get("DeviceId") == self.device_id:
                    return sess
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------
# Driving the player
# --------------------------------------------------------------------------

def build_media(session, item_ids, seq=0):
    """A real `Media` over real server DTOs — the app's own play path.

    `event_handler.play_media` builds exactly this before handing
    `media.video` to the player; going through `Media` rather than a stand-in
    Video is the point of the suite, since it is what fetches PlaybackInfo and
    resolves the stream URL against the server.
    """
    from jellyfin_mpv_shim.media import Media
    return Media(session.client, list(item_ids), seq=seq,
                 user_id=session.user_id)


def pump_until(pm, predicate, timeout=45, interval=0.05):
    """Drive the action queue by hand until `predicate` holds.

    The real `ActionThread` pumps `playerManager.update()`; doing it here
    instead keeps the timing under the test's control, exactly as the
    integration suite does. Returns the predicate's final value.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pm.update()
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    pm.update()
    try:
        return bool(predicate())
    except Exception:
        return False


def wait_for(predicate, timeout=15, interval=0.25):
    """Poll a server-side condition. Reporting is asynchronous — a progress
    post lands on a background thread — so an assertion made the instant after
    playback starts is a race the test loses, not a bug."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception:
            pass
        time.sleep(interval)
    try:
        return predicate()
    except Exception:
        return None


class E2ETestCase(unittest.TestCase):
    """Base: an isolated config, a real player, one qa-user session."""

    account = "qa-user"

    @classmethod
    def setUpClass(cls):
        isolate_config()
        cls.player_module = ensure_real_player()
        cls.pm = cls.player_module.playerManager
        # Own the pump rather than starting the real ActionThread singleton.
        import threading
        cls.pm.action_trigger = threading.Event()
        cls.pm.timeline_trigger = threading.Event()

    # No tearDownClass: the player is process-wide and outlives this class.
    # See ensure_real_player.

    def setUp(self):
        self.session = Session(self.account)
        self.addCleanup(self.session.stop)
        # Never leave a player running into the next test: a live session keeps
        # reporting and the next test's assertions read its progress.
        self.addCleanup(self._safe_stop)

    def _safe_stop(self):
        try:
            self.pm.stop()
        except Exception:
            pass

    def pump_until(self, predicate, timeout=45):
        """`pump_until` against this case's player."""
        return pump_until(self.pm, predicate, timeout=timeout)
