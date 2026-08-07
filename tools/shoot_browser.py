"""Photograph real browser screens, against a real server, through real mpv.

    JMS_E2E_SERVER=http://127.0.0.1:8096 \\
        xvfb-run -a python3 -m tools.shoot_browser books

The scene tests read a widget tree and the route walk proves every screen
builds — neither can tell you whether a screen is *decent to look at*. Bars
too crowded to use, a control that reads as a different kind of thing from
its neighbours, text colliding with a button: all of those build perfectly
and lay out without error. This is the tool for the question those cannot
answer, and it is the same trick ``tools/theme_preview.py`` uses, pointed at
the library browser instead of a widget sample.

Real everything, deliberately. A fake source would let the shots disagree
with what a user sees in exactly the ways that matter — artwork that is
missing, titles that are longer than the fixture's, a folder whose contents
turn out not to be what the page assumed.

Shots are written to a temp directory and the paths printed. Pass
``--size WxH`` to photograph a screen at a width it has to survive; the
now-playing bar in particular is a different layout below 1100px.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "integration"))
import _harness as h  # noqa: E402

h.prime_args()

from jellyfin_apiclient_python import JellyfinClient  # noqa: E402
from jellyfin_mpv_shim.constants import (  # noqa: E402
    CLIENT_VERSION, USER_APP_NAME, USER_AGENT)
from jellyfin_mpv_shim.mpvtk.app import MpvtkApp  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource  # noqa

SERVER = (os.environ.get("JMS_E2E_SERVER") or "").rstrip("/")
ACCOUNT = os.environ.get("JMS_SHOT_USER", "qa-user")
PASSWORD = "stdjflib"
UUID = "shot"


def _capture(app, path):
    """Photograph the window. ``True`` if a file was written.

    mpv's own screenshot is tried first and will usually FAIL here: the
    browser draws ASS overlays over an idle player with no file loaded, and
    `screenshot-to-file` has no video frame to write. So the real path is
    the X root window, which is what `tools/theme_preview.py` falls back to
    for the same reason. Kept in that order anyway — with something playing
    (`--playing` against a real queue) mpv's own is the higher-fidelity one.
    """
    try:
        app.screenshot(path)
        if os.path.exists(path):
            return True
    except Exception:
        pass
    subprocess.run(["import", "-window", "root", path], check=False,
                   timeout=20)
    return os.path.exists(path)


class _SyncPool:
    """Route loads run inline, so a screen has its data before it is
    photographed rather than being caught mid-spinner."""

    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


def login():
    client = JellyfinClient(allow_multiple_clients=True)
    client.config.data["app.default"] = True
    client.config.app(USER_APP_NAME, CLIENT_VERSION, "jms-shots", "shots-1")
    client.config.data["http.user_agent"] = USER_AGENT
    client.config.data["auth.ssl"] = True
    client.auth.connect_to_address(SERVER)
    result = client.auth.login(SERVER, ACCOUNT, PASSWORD)
    if "AccessToken" not in result:
        raise SystemExit("could not log in as %s: %r" % (ACCOUNT, result))
    creds = client.auth.credentials.get_credentials()["Servers"][0]
    return creds["UserId"], creds["AccessToken"]


def find(source, **where):
    """One item by NAME, never by id — ids change on every reprovision."""
    api = source._conn(UUID).api
    params = {"Recursive": True, "Fields": "Path,ParentId,Album"}
    params.update({k: v for k, v in where.items() if k != "name"})
    for item in (api.user_items(params=params) or {}).get("Items", []):
        if item.get("Name") == where["name"]:
            return item
    raise SystemExit("no item named %r (%r)" % (where["name"], where))


def routes(source, which):
    """The screens to photograph, as (label, route) pairs."""
    libs = source.get_libraries(UUID)
    books = next((li for li in libs
                  if li.get("CollectionType") == "books"), None)
    if books is None:
        raise SystemExit("this server has no books library")
    out = [("books-library",
            {"kind": "books", "server": UUID, "parent_id": books["Id"],
             "item_id": books["Id"], "collection_type": "books",
             "title": books["Name"]})]
    if which in ("books", "all"):
        chapter = find(source, name="Chapter 01",
                       IncludeItemTypes="AudioBook")
        out.append(("audiobook-folder",
                    {"kind": "books", "server": UUID,
                     "parent_id": chapter["ParentId"],
                     "item_id": chapter["ParentId"],
                     "collection_type": "books",
                     "title": chapter.get("Album") or "Audiobook"}))
        author = find(source, name="Ines Imani", IncludeItemTypes="Folder")
        out.append(("book-folder",
                    {"kind": "books", "server": UUID,
                     "parent_id": author["Id"], "item_id": author["Id"],
                     "collection_type": "books", "title": author["Name"]}))
        book = find(source, name="The Standard Manual",
                    IncludeItemTypes="Book")
        out.append(("book-page",
                    {"kind": "book", "server": UUID, "item_id": book["Id"],
                     "title": book["Name"]}))
    if which in ("home", "all"):
        out.append(("home", {"kind": "home", "server": UUID}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="books",
                    choices=("books", "home", "all"))
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--playing", action="store_true",
                    help="fake an audiobook playstate so the now-playing "
                         "bar is in the shot")
    args = ap.parse_args()
    if not SERVER:
        raise SystemExit("set JMS_E2E_SERVER")
    width, height = (int(x) for x in args.size.split("x"))

    user_id, token = login()
    source = LibrarySource(
        [{"uuid": UUID, "name": "shots", "address": SERVER,
          "user_id": user_id, "token": token}], "shots-1", "jms-shots", False)
    shots = routes(source, args.which)

    outdir = tempfile.mkdtemp(prefix="jms-shots-")
    app = MpvtkApp()
    browser = MpvtkBrowser(app=app, source=source)
    browser._pool = _SyncPool()
    browser.server = UUID
    if args.playing:
        # The bar is drawn from a playstate push, not from the player, so a
        # snapshot is all it needs -- and it is the only way to photograph
        # the two-row audiobook layout without holding real playback open.
        browser.on_playstate({
            "stopped": False, "is_audio": True, "is_audiobook": True,
            "title": "The Lantern Keeper", "artist": "Elena Farrow",
            "album": "The Lantern Keeper", "position": 742.0,
            "duration": 2400.0, "volume": 70, "paused": False,
            "queue_len": 1, "repeat": "none",
            "chapters": [{"title": "Chapter %d" % (i + 1), "time": i * 300.0}
                         for i in range(8)],
        })

    written = []

    def drive():
        app.ready.wait(20)
        time.sleep(1.5)
        for label, route in shots:
            print("-> %s" % label, flush=True)
            try:
                browser.navigate(dict(route))
            except Exception as exc:      # a screen that will not open is
                print("!! %s: %s" % (label, exc))   # worth saying, not fatal
                continue
            # Two beats: one for the loaders, one for the artwork the tiles
            # ask for once they know their size.
            time.sleep(2.5)
            browser.invalidate()
            time.sleep(1.5)
            path = os.path.join(outdir, "%s-%dx%d.png"
                                % (label, width, height))
            if _capture(app, path):
                written.append(path)
                print("shot %s" % label, flush=True)
            time.sleep(0.3)
        app.quit()

    threading.Thread(target=drive, daemon=True).start()
    app.run(browser.build)
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
