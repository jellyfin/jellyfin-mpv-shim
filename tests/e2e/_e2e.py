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

import atexit
import json
import os
import shutil
import subprocess
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

_SINK_MODULE = None
SINK_NAME = "jms-e2e-sink"

#: Every device id this suite registers starts with this, and `purge_devices`
#: will not delete one that does not. The QA server is a machine a developer
#: also points a real client at, and a device record is a live session: a
#: purge that matched on the account alone signed that client out mid-run.
DEVICE_PREFIX = "jms-e2e-"


def dummy_audio_device():
    """An mpv `audio-device` that makes noise nowhere. Created on first use.

    The playback tests decode real media, so mpv opens a real audio output —
    which on a developer's box means the suite is audible, fights whatever
    else is playing, and can fail on a device another process holds. (A run
    against the real device already produced "Audio device underrun
    detected".)

    A PipeWire/PulseAudio **null sink** is better than `ao=null` here: mpv
    genuinely opens an output and the whole audio path runs — device
    selection, format negotiation, the AudioMixin settings — it just ends
    nowhere. `ao=null` would skip all of that, and audio behaviour is
    something this suite should be able to grow tests for.

    The sink is created explicitly and addressed explicitly; **the default
    sink is never changed**, so nothing about the developer's audio moves.
    Unloaded at exit.

    Returns None when there is no `pactl` or the sink cannot be made, in
    which case the caller falls back to mpv's own null device — quiet, and
    still no contention, just less of the path exercised.
    """
    global _SINK_MODULE
    if _SINK_MODULE is not None:
        return "pulse/" + SINK_NAME
    # The runner makes one sink for the whole matrix and names it here, so the
    # eleven legs (and the children they spawn) share it instead of each
    # loading a module — eleven sinks with one requested name is eleven
    # deduplicated names, and any leg that dies takes its unload with it.
    from_runner = os.environ.get("JMS_E2E_AUDIO_DEVICE")
    if from_runner:
        return from_runner
    if not shutil.which("pactl"):
        return None
    if _sink_exists():
        # Left by a crashed run, or a sibling process. Reuse it and do not
        # register an unload: this process did not create it and unloading
        # someone else's sink mid-run would pull the device out from under
        # them. A stray null sink is harmless and gets reused next time.
        return "pulse/" + SINK_NAME
    try:
        out = subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             "sink_name=" + SINK_NAME,
             "sink_properties=device.description=" + SINK_NAME],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    module_id = (out.stdout or "").strip()
    if out.returncode != 0 or not module_id.isdigit():
        return None
    _SINK_MODULE = module_id
    atexit.register(_unload_sink)
    return "pulse/" + SINK_NAME


def _sink_exists():
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split("\t")[1:2] == [SINK_NAME]
               for line in (out.stdout or "").splitlines())


def _unload_sink():
    global _SINK_MODULE
    if _SINK_MODULE is None:
        return
    module_id, _SINK_MODULE = _SINK_MODULE, None
    try:
        subprocess.run(["pactl", "unload-module", module_id],
                       capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def quiet_settings():
    """Turn off everything that is not under test, and pin the backend.

    **Must run before `jellyfin_mpv_shim.player` is imported** — player.py
    picks its mpv backend at import time and builds its singleton there.
    """
    from jellyfin_mpv_shim.conf import settings
    h.prime_args()
    # "null" rather than leaving it unset: mpv's own null device is the
    # fallback when there is no sound server to make a sink in, and it is
    # still better than opening whatever the box's default output is.
    settings.audio_device = dummy_audio_device() or "null"
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
    """Shut the player down while the interpreter is still healthy.

    `PlayerManager.terminate` tears the handle down only for **external** mpv
    (`if is_using_ext_mpv: self._player.terminate()`); in-process libmpv is
    left for CPython's finalization. That is fine for the app — a desktop
    process exiting lets the OS reap it — and racy under a test runner, where
    finalization runs with atexit handlers and daemon threads still in play.
    It showed up as a rare SIGSEGV *after* "OK" was printed, which the runner
    then reported as a failed leg on a run whose assertions all passed:
    roughly one in five to eight, libmpv only.

    So terminate the handle here too, explicitly, before the interpreter
    starts pulling itself apart. Harness-only — nothing about the app's own
    exit path changes.
    """
    if _PLAYER is None:
        return
    manager = _PLAYER.playerManager
    handle = getattr(manager, "_player", None)
    try:
        manager.terminate()
    except Exception:
        pass
    if not getattr(_PLAYER, "is_using_ext_mpv", False) and handle is not None:
        try:
            handle.terminate()
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
        # DEVICE_PREFIX, not a literal: `purge_devices` will not delete a
        # device whose id does not carry it, so an id built any other way
        # becomes litter this suite can no longer clear up after itself.
        self.device_id = device_id or (DEVICE_PREFIX + account)
        if not self.device_id.startswith(DEVICE_PREFIX):
            raise AssertionError(
                "device_id %r does not start with %r, so purge_devices "
                "cannot clean it up and it becomes permanent litter on the "
                "server" % (self.device_id, DEVICE_PREFIX))
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

    def set_policy(self, policy, user_id=None):
        """Replace a user's policy (this user's by default). Admin only.

        A fallback for the Live TV timer tests. Current stdjflib grants
        `EnableLiveTvManagement` to `qa-user`, so they need no mutation at
        all — but an older server, or one provisioned before that fix, has it
        on nobody, and there the tests grant it and put the original back
        rather than skipping the whole DVR surface.
        """
        self._request("/Users/%s/Policy" % (user_id or self.user_id),
                      method="POST", body=policy)

    def user_by_name(self, name):
        """A user record by name. Admin only."""
        for user in self._request("/Users") or []:
            if user.get("Name") == name:
                return user
        raise AssertionError("no user named %r" % name)

    def purge_devices(self, account):
        """Delete THIS SUITE's Device records for `account`. Admin only.

        For `qa-onesession`, whose cap counts what the server still believes
        is connected: a session left behind by a crashed run refuses the next
        one, and the failure looks like the cap working rather than like
        litter. Called from `setUp` so the test starts from a known state
        however the last run ended.

        **Ours only.** A Device record is a live session, and deleting one
        signs that client out. This matched on the account alone, which meant
        a run against the QA server signed out whatever real client a
        developer had pointed at it under the same login -- observed exactly
        once, with a client on `qa-nosyncplay` while `RevokedSessionTest`
        purged that account. The litter this exists to clear is always ours,
        and ours always carries `DEVICE_PREFIX`, so there is nothing to
        trade off: `qa-onesession`'s own ids are `jms-e2e-1session-a/b`.

        A foreign device holding the cap is a real possibility and is
        deliberately left to fail loudly -- deleting somebody's session to
        make a test pass is the wrong way round.
        """
        devices = self._request("/Devices") or {}
        items = devices.get("Items", devices if isinstance(devices, list) else [])
        for device in items:
            if device.get("LastUserName") != account:
                continue
            if not str(device.get("Id") or "").startswith(DEVICE_PREFIX):
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
