#!/usr/bin/env python3
"""Clear the e2e suite's playlist litter off a QA server before a run.

Jellyfin (12.0 and 10.11) leaves a deleted playlist's folder on disk --
`<state>/.../playlists/<name>/playlist.xml` -- and the next library scan
imports every leftover folder as a live playlist again. So `jms-e2e-*`
playlists the suite deleted come back mid-run, fill the Playlists shelf past
the first screen, and fail every test that opens one; and while ANY playlist
exists every account gets a Playlists view, which `test_account_policy` reads
as a policy leak. Deleting through the API alone is not enough: the folders
have to go too, and only the server's own machine can reach them.

Two steps, both limited to names starting ``jms-e2e-``:

1. every such playlist, deleted through the API as qa-admin;
2. every such folder under the server's state directory (``--state``).

Then it checks, and exits 1 if any remain -- so litter is one clear error
before a run instead of four timeouts inside it.

**Only while no test run is using the server.** Signing in as qa-admin
revokes any other qa-admin session with the same device id, which is what a
running e2e module holds (docs/testing.md).

    JMS_E2E_SERVER=http://127.0.0.1:8096 python3 tools/e2e_preclean.py \\
        --state /tmp/stdjflib-1000/std-jf-lib-4d987e68/jellyfin [-n]
"""

import argparse
import os
import shutil
import sys

PREFIX = "jms-e2e-"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def playlist_dirs(state):
    """Every `playlists` directory under ``state`` that holds a playlist.

    Searched for rather than assumed: `serve` puts it at data/data/playlists
    and the container at config/data/playlists."""
    found = []
    for root, dirs, _files in os.walk(state):
        if os.path.basename(root) == "playlists" and any(
                os.path.isfile(os.path.join(root, d, "playlist.xml"))
                for d in dirs):
            found.append(root)
            dirs[:] = []                # nothing of ours below it
    return found


def litter_folders(state):
    return sorted(os.path.join(d, name)
                  for d in playlist_dirs(state)
                  for name in os.listdir(d)
                  if name.startswith(PREFIX)
                  and os.path.isfile(os.path.join(d, name, "playlist.xml")))


def litter_items(admin):
    """(id, name) of every `jms-e2e-*` playlist the admin can see.

    Raises on a failed read: here "could not tell" must not pass as clean."""
    views = admin._request("/Users/%s/Views" % admin.user_id)["Items"]
    view = next((v for v in views
                 if v.get("CollectionType") == "playlists"), None)
    if view is None:
        return []                       # no playlist exists at all
    items = admin._request("/Items?parentId=%s&userId=%s"
                           % (view["Id"], admin.user_id))["Items"]
    return sorted((i["Id"], i["Name"]) for i in items
                  if str(i.get("Name") or "").startswith(PREFIX))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--state", help="the QA server's state directory (its "
                   "folders are what a scan resurrects); omitted, only the "
                   "API half runs")
    p.add_argument("-n", "--dry-run", action="store_true")
    args = p.parse_args(argv)

    sys.argv = [sys.argv[0]]            # _e2e imports the app, which parses it
    sys.path[:0] = [REPO_ROOT, os.path.join(REPO_ROOT, "tests", "e2e")]
    import _e2e

    admin = _e2e.Session("qa-admin")
    try:
        items = litter_items(admin)
        for item_id, name in items:
            print("%s playlist %s (%s)" % (
                "would delete" if args.dry_run else "delete", name, item_id))
            if not args.dry_run:
                admin._request("/Items/%s" % item_id, method="DELETE")
        folders = litter_folders(args.state) if args.state else []
        for path in folders:
            print("%s folder %s" % (
                "would remove" if args.dry_run else "remove", path))
            if not args.dry_run:
                shutil.rmtree(path)
        if args.dry_run:
            print("dry run: %d playlist(s), %d folder(s)"
                  % (len(items), len(folders)))
            return 0
        left_items = litter_items(admin)
    finally:
        admin.stop()
    left_folders = litter_folders(args.state) if args.state else []
    if not args.state:
        print("warning: no --state, so leftover folders were not removed "
              "and the next library scan may bring playlists back",
              file=sys.stderr)
    if left_items or left_folders:
        print("still there: %s" % ", ".join(
            [n for _i, n in left_items] + left_folders), file=sys.stderr)
        return 1
    print("clean: %d playlist(s) and %d folder(s) removed"
          % (len(items), len(folders)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
