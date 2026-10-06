"""Steps a person takes, made of keypresses, for the real-app scenarios.

Every step here is something done at the keyboard of the app `_app.App`
launched: nothing reaches into the process. What a step *checks* is only
what the person would see, plus the catalog read from disk read-only.

The keyboard routes were found by driving the app, not assumed:
TAB walks focus and lands in text fields; search is three TABs from Home;
a result opens with ENTER onto Play; Download is reached by TAB and asks
for confirmation (`dl-ok`).
"""

import json
import os
import sqlite3
import time

import _accounts
import _app
import _e2e


@_app.timed('login')
def login(app, relay, account="qa-user"):
    """Sign in through the login form, against the relay's address."""
    app.wait_for(lambda f: _app.shown(f, "login-server"), timeout=60,
                 what="the login screen")
    # Each field is checked to hold what was typed before the next is
    # touched (App.type_into) -- the password as its length only, which is
    # all the observer ever publishes of a masked field.
    app.type_into("login-server", relay.address)
    app.type_into("login-user", account)
    # Keyed by the upstream server: the published password is per server.
    app.type_into("login-pass", _accounts.password_for(account, _e2e.SERVER),
                  masked=True)
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                 what="the home screen after signing in as %s" % account)


@_app.timed('open_by_search')
def open_by_search(app, query, item_id, section="Movies", landed=None):
    """Search from the top bar and open the result: ends on its detail page
    with Play focused, or, for a page with no Play (a series), once
    ``landed`` is on screen."""
    # The box keeps the last query, so a second search would append to it.
    app.clear_field("nav-search")
    app.type_into("nav-search", query)
    app.key("ENTER")
    tile = "search-%s-%s" % (section, item_id)
    app.wait_for(lambda f: _app.shown(f, tile), timeout=30,
                 what="%s in the search results" % item_id)
    app.move_to(tile)
    # press_until: an ENTER on a result tile has been seen to go unanswered
    # while the results page settles (candidate finding, see the register).
    if landed:
        app.press_until("ENTER", lambda f: _app.shown(f, landed),
                        what="%s's page" % item_id)
        return
    # Play, or Resume when the item holds a position: that is what gets
    # focus then, and waiting for Play made the retry press Resume.
    app.press_until("ENTER",
                    lambda f: f.get("nav") in ("btn-play", "btn-resume"),
                    what="the detail page with Play focused")


@_app.timed('download_open_item')
def download_open_item(app, catalog, item_id, timeout=120):
    """Download the item whose detail page is open, confirming the dialog,
    and wait for the catalog to call it complete."""
    app.move_to("act-download")
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "dl-ok"), timeout=15,
                 what="the download dialog")
    app.move_to("dl-ok")
    app.key("ENTER")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = catalog.download(item_id)
        if row and row.get("status") == "complete":
            return row
        time.sleep(0.5)
    raise AssertionError("%s never completed downloading (row: %r)"
                         % (item_id, catalog.download(item_id)))


@_app.timed('remove_download_open_item')
def remove_download_open_item(app, catalog, item_id, timeout=60):
    """Remove Download on the open detail page, confirmed, and wait for the
    catalog to drop the row."""
    app.move_to("act-undownload")
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "dlg-ok"), timeout=15,
                 what="the Delete Download confirmation")
    app.move_to("dlg-ok")
    app.key("ENTER")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if catalog.download(item_id) is None:
            return
        time.sleep(0.25)
    raise AssertionError("%s is still in the catalog after Remove Download: "
                         "%r" % (item_id, catalog.download(item_id)))


@_app.timed('open_settings_tab')
def open_settings_tab(app, tab):
    """Settings, then the tab called ``tab`` (its id is stab-<tab>)."""
    app.move_to("nav-settings")
    app.press_until("ENTER", lambda f: _app.shown(f, "stab-" + tab),
                    what="the settings tabs")
    app.move_to("stab-" + tab)
    app.key("ENTER")


class Catalog:
    """The app's sync catalog, read the way a test may: read-only, and never
    by copying the file (a copy drops the WAL the app is still writing).
    ``root`` is the download folder, when it is not the default."""

    def __init__(self, config_dir, root=None):
        self.path = os.path.join(root or os.path.join(config_dir, "offline"),
                                 "catalog.db")

    def _query(self, sql, args=()):
        if not os.path.exists(self.path):
            return []
        conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                               timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def download(self, item_id):
        rows = self._query("SELECT * FROM downloads WHERE item_id = ?",
                           (item_id,))
        if not rows:
            return None
        row = rows[0]
        row.pop("item_json", None)
        row.pop("source_json", None)
        return row

    def userdata(self, item_id):
        """{(server_id, user_id): row} -- one per actor, per the ruling that
        watched state belongs to a person (offline-sync.md section 1)."""
        return {(r["server_id"], r["user_id"]): r for r in self._query(
            "SELECT * FROM item_userdata WHERE item_id = ?", (item_id,))}

    def pending(self, item_id=None):
        if item_id is None:
            return self._query("SELECT * FROM pending_playstate")
        return self._query(
            "SELECT * FROM pending_playstate WHERE item_id = ?", (item_id,))


@_app.timed('add_profile')
def add_profile(app, name):
    """Settings > Servers & Users > Add User. Leaves Settings open."""
    open_settings_tab(app, "servers")
    app.wait_for(lambda f: _app.shown(f, "su-newuser"), timeout=15,
                 what="the users list")
    app.type_into("su-newuser", name)
    app.move_to("su-adduser")
    app.key("ENTER")


@_app.timed('add_server_from_anywhere')
def add_server_from_anywhere(app):
    """After a profile switch, reach the add-server form: it is either up
    already, or the profile landed in the offline library (a profile with
    no servers does, when the machine holds downloads -- gateway
    users.switch_user) and Configure Servers opens it.

    Only those two destinations are accepted: a switch made from Settings
    leaves Settings' own Add Server on screen for a frame or two, and taking
    that stale frame for the destination is how this helper once raced."""
    f = app.wait_for(lambda f: _app.shown(f, "login-server")
                     or _app.shown(f, "banner-servers"), timeout=30,
                     what="the login form or the offline banner")
    if _app.shown(f, "login-server"):
        return
    app.move_to("banner-servers")
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "login-server"), timeout=15,
                 what="the add-server form from Configure Servers")


def items(frame, dd_id):
    """A drop-down's entries as drawn, or [] when it is not on screen."""
    node = _app.node(frame, dd_id) or {}
    return list(node.get("items") or [])


@_app.timed('pick')
def pick(app, dd_id, index):
    """Open drop-down ``dd_id`` by keyboard and choose entry ``index``.

    Moves by the observed cursor, which starts on the CURRENT entry, not the
    first: ENTER with no arrows re-selects what is already chosen."""
    app.move_to(dd_id)
    app.key("ENTER")
    f = app.wait_for(lambda f: f.get("dd_open") == dd_id, timeout=10,
                     what="%s open" % dd_id)
    dd = f.get("dropdowns") or {}
    cur = f.get("nav_pidx")
    if cur is None:
        cur = ((dd.get(dd_id) if isinstance(dd, dict) else None)
               or {}).get("sel", 0)
    for _ in range(abs(index - cur)):
        app.key("DOWN" if index > cur else "UP")
    app.key("ENTER")


def selected(frame, dd_id):
    dd = frame.get("dropdowns") or {}
    return ((dd.get(dd_id) if isinstance(dd, dict) else None)
            or {}).get("sel")


@_app.timed('switch_profile')
def switch_profile(app, name, timeout=60):
    """Switch to the profile called ``name`` through the top bar's profile
    drop-down, by keyboard, and wait until the app says it is active.

    The drop-down's cursor starts on the CURRENT profile, not the first
    entry, so ENTER with no arrows re-selects who is already active -- an
    earlier version of this step did exactly that and scenario 4 went green
    without ever switching. Hence moving by the observed cursor, and the
    check at the end against users.json, which is what the app persists
    when a switch really happens."""
    names = [u.get("name") for u in
             (users(app.config_dir) or {}).get("users", [])]
    if name not in names:
        raise AssertionError("no profile %r (have %r)" % (name, names))
    pick(app, "nav-user", names.index(name))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if active_profile(app.config_dir) == name:
            return
        time.sleep(0.25)
    raise AssertionError("switching to %r did not happen (active: %r)"
                         % (name, active_profile(app.config_dir)))


def active_profile(config_dir):
    reg = users(config_dir) or {}
    for u in reg.get("users", []):
        if u.get("id") == reg.get("active"):
            return u.get("name")
    return None


def credentials(config_dir):
    """{profile name: [usernames it holds a saved login for]}."""
    reg = users(config_dir) or {}
    return {u.get("name"): [c.get("username") for c in
                            (u.get("credentials") or [])]
            for u in reg.get("users", [])}


def users(config_dir):
    """The profile registry as the app wrote it."""
    try:
        with open(os.path.join(config_dir, "users.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


@_app.timed('relaunch')
def relaunch(app, relay=None, cut=False, timeout=90, env=None):
    """Quit cleanly, optionally cut the network, and start the app again on
    the same config directory. Returns the new App, launched with the old
    one's environment unless ``env`` replaces it."""
    rc = app.quit()
    if rc != 0:
        raise AssertionError("the app exited with %s before the relaunch"
                             % rc)
    if cut:
        relay.cut()
        assert relay.probe_refused(), "the cut is not in effect"
    again = _app.App(backend=app.backend, config_dir=app.config_dir,
                     env=app._extra_env if env is None else env)
    again.start(timeout=timeout)
    return again
