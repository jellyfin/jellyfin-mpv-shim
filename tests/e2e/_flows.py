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


def login(app, relay, account="qa-user"):
    """Sign in through the login form, against the relay's address."""
    app.wait_for(lambda f: _app.shown(f, "login-server"), timeout=60,
                 what="the login screen")
    app.move_to("login-server")
    app.type(relay.address)
    app.move_to("login-user")
    app.type(account)
    app.move_to("login-pass")
    # Keyed by the upstream server: the published password is per server.
    password = _accounts.password_for(account, _e2e.SERVER)
    app.type(password)
    # What was typed is what the fields hold -- the password as its length
    # only, which is all the observer ever publishes of a masked field.
    f = app.wait_for(
        lambda f: _app.fields(f).get("login-pass") == "*" * len(password),
        timeout=10, what="the password field filled")
    got = _app.fields(f)
    if (got.get("login-server"), got.get("login-user")) != (relay.address,
                                                           account):
        raise AssertionError("the login fields hold %r, not what was typed"
                             % ({k: got.get(k) for k in
                                 ("login-server", "login-user")},))
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                 what="the home screen after signing in as %s" % account)


def open_by_search(app, query, item_id, section="Movies"):
    """Search from the top bar and open the result: ends on its detail page
    with Play focused."""
    app.move_to("nav-search")
    app.type(query)
    app.key("ENTER")
    tile = "search-%s-%s" % (section, item_id)
    app.wait_for(lambda f: _app.shown(f, tile), timeout=30,
                 what="%s in the search results" % item_id)
    app.move_to(tile)
    # press_until: an ENTER on a result tile has been seen to go unanswered
    # while the results page settles (candidate finding, see the register).
    app.press_until("ENTER", lambda f: f.get("nav") == "btn-play",
                    what="the detail page with Play focused")


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


class Catalog:
    """The app's sync catalog, read the way a test may: read-only, and never
    by copying the file (a copy drops the WAL the app is still writing)."""

    def __init__(self, config_dir):
        self.path = os.path.join(config_dir, "offline", "catalog.db")

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


def add_profile(app, name):
    """Settings > Servers & Users > Add User. Leaves Settings open."""
    app.move_to("nav-settings")
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "stab-servers"), timeout=15,
                 what="the settings tabs")
    app.move_to("stab-servers")
    app.key("ENTER")
    app.wait_for(lambda f: _app.shown(f, "su-newuser"), timeout=15,
                 what="the users list")
    app.move_to("su-newuser")
    app.type(name)
    app.move_to("su-adduser")
    app.key("ENTER")


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


def switch_profile(app, index):
    """The top bar's profile drop-down, by keyboard: the entry at
    ``index`` (0 = first). Settings' own user rows take no focus."""
    app.move_to("nav-user")
    app.key("ENTER")
    app.wait_for(lambda f: f.get("dd_open") == "nav-user", timeout=10,
                 what="the profile list open")
    for _ in range(index):
        app.key("DOWN")
    app.key("ENTER")


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


def relaunch(app, relay=None, cut=False, timeout=90):
    """Quit cleanly, optionally cut the network, and start the app again on
    the same config directory. Returns the new App."""
    rc = app.quit()
    if rc != 0:
        raise AssertionError("the app exited with %s before the relaunch"
                             % rc)
    if cut:
        relay.cut()
        assert relay.probe_refused(), "the cut is not in effect"
    again = _app.App(backend=app.backend, config_dir=app.config_dir)
    again.start(timeout=timeout)
    return again
