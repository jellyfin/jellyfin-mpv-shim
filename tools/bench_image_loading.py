#!/usr/bin/env python3
"""Measure how a Jellyfin server behaves when a wall of posters is asked for.

The library grid is the screen that has drawn complaints ("ten minutes to
load, and then the server froze"), and the queries behind it are cheap and
paged -- so the cost is the artwork. This asks the server directly, with the
sizes and the concurrency the browser really uses, and reports the numbers
that decide whether a library is pleasant or unusable:

* **What a poster costs cold, and warm.** Jellyfin resizes on demand and
  caches the result *per exact pixel size*, so the first request for a size
  pays for a decode and a re-encode and every later one is a file read. The
  ratio is the whole story on a CPU-starved box.
* **Whether the unresized original is cheaper.** It costs the server no CPU
  at all -- just a file stream -- but it is many times the bytes, and the
  client then downscales it. Which side wins depends on the box and the
  link, so measure rather than assume.
* **Whether a size is cached at all**, by asking for one a pixel wider. If
  that costs full price, every window resize re-does every poster.
* **A whole first paint**, at the browser's real burst size and worker
  count. This is the number that matters: it is what the server is doing
  while the user waits, and while the *next* browse query is queued behind
  it. The client gives an API call 30s before it fails the screen with
  "Failed to load. Check the connection." -- so a first paint that occupies
  the server for longer than that is not slow, it is broken.

**What healthy looks like.** Two real servers, so a reporter's numbers have
something to sit against:

    a LAN server              cold 28.8ms   warm 10.8ms   first paint 0.7s
    a friend's, over the wan  cold  171ms   warm  78.2ms  first paint 2.6s

(Those two were run at --tiles 66, twice the real burst, so halve the paint
column to compare against a default run.)

The lesson in those is that the resize is **not** the dominant cost on a
real server: cold is only two or three times warm, and on the remote one a
*cached* poster still costs 78ms, which is the round trip and nothing else.
A first paint therefore lands between one and three seconds, nowhere near
the 30s that would break a screen. So a report of a library taking minutes
is not explained by "resizing is expensive" -- it needs a server that is
pathologically slower than either of these, and the point of running this is
to find out by how much and at which step.

(Watch for a *synthetic* library flattering the CPU half: against a local
test server with small posters, cold measured 19x warm, purely because warm
was 4ms. Ratios need their absolute numbers beside them.)

Read-only: it issues GETs and nothing else. It does leave the sizes it asks
for in the server's image cache, which is what any client browsing that
library would have done anyway; `--cold-size` keeps the measurement honest
across repeat runs by asking for a size nothing has used yet.

Usage:
    tools/bench_image_loading.py                       # default config dir
    tools/bench_image_loading.py --config ~/.config/jellyfin-mpv-shim
    tools/bench_image_loading.py --server Casper --library Movies
    tools/bench_image_loading.py --tiles 66 --workers 6 --samples 8

Credentials come from the config directory the app itself would use, so
whatever you are already signed in to is what it measures. Nothing is
written back.
"""

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

#: The poster tile the library grid draws at 1280x720 with no DPI scaling.
#: Both numbers reach the server as fillWidth/fillHeight, and both move with
#: the window size and the display scale -- which is the point of the
#: one-pixel test below.
TILE_W, TILE_H = 150, 225

#: What one first paint of a library grid asks for, counted by instrumenting
#: a real navigation into a 1000-item library at 1280x720: 33 distinct
#: posters. TileRenderer.row_window composites the viewport plus one screen
#: above and two below, and at the top of a list the "above" is empty -- so
#: this is about three times the ~11 tiles actually on screen.
FIRST_PAINT_TILES = 33

#: ThumbnailStore's pool: six workers over a blocking connection pool.
THUMB_WORKERS = 6

#: What the client gives an API call before it fails the route (the
#: apiclient's DEFAULT_HTTP_TIMEOUT). Not a limit on images -- those get
#: (5, 20) and a backoff -- but the ceiling a browse query queued behind a
#: burst of resizes has to clear.
API_TIMEOUT = 30

APP_NAME = "jellyfin-mpv-shim"


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", metavar="DIR",
                    help="config directory to read credentials from "
                         "(default: the one the app would use)")
    ap.add_argument("--server", metavar="NAME",
                    help="which saved server, by name or address "
                         "(default: the first saved one that answers)")
    ap.add_argument("--library", metavar="NAME",
                    help="which library to sample posters from "
                         "(default: the largest one with artwork)")
    ap.add_argument("--samples", type=int, default=8, metavar="N",
                    help="posters per sequential measurement (default 8)")
    ap.add_argument("--tiles", type=int, default=FIRST_PAINT_TILES,
                    metavar="N",
                    help="posters in the first-paint burst (default %d, what "
                         "one library screen asks for)" % FIRST_PAINT_TILES)
    ap.add_argument("--workers", type=int, default=THUMB_WORKERS, metavar="N",
                    help="concurrent fetches (default %d, the browser's "
                         "thumbnail pool)" % THUMB_WORKERS)
    ap.add_argument("--width", type=int, default=TILE_W)
    ap.add_argument("--height", type=int, default=TILE_H)
    ap.add_argument("--cold-size", type=int, default=None, metavar="PX",
                    help="width to use for the cold measurements. Default is "
                         "a size derived from the clock, so a repeat run "
                         "measures a cold cache again rather than its own "
                         "leftovers")
    ap.add_argument("--insecure", action="store_true",
                    help="do not verify TLS certificates")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="per-request timeout, seconds (default 120)")
    return ap.parse_args(argv)


# -- credentials -----------------------------------------------------------

def config_dir(explicit):
    """The directory the app itself would read, or the one given.

    Resolved by the app's own `conffile`, which is why sys.argv is rewritten
    first: that module asks `args.get_args()` for `--config`, and the app's
    parser knows nothing about this script's other flags.
    """
    sys.argv = [sys.argv[0]] + (["--config", explicit] if explicit else [])
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, here)
    from jellyfin_mpv_shim import conffile

    return conffile.confdir(APP_NAME)


def load_servers(path):
    """Saved servers, newest credential store first. Read-only.

    users.json is the current store (several users, each with their own
    servers); cred.json is the pre-3.0 one, still read because a config
    directory that has not run 3.0 yet has only that.
    """
    out = []

    users = os.path.join(path, "users.json")
    if os.path.isfile(users):
        with open(users, encoding="utf-8") as fh:
            data = json.load(fh)
        active = data.get("active")
        # The active user first: it is the one whose servers are on screen.
        for user in sorted(data.get("users") or [],
                           key=lambda u: u.get("id") != active):
            for cred in user.get("credentials") or []:
                out.append(cred)

    legacy = os.path.join(path, "cred.json")
    if not out and os.path.isfile(legacy):
        with open(legacy, encoding="utf-8") as fh:
            out = (json.load(fh) or {}).get("Servers") or []

    return [c for c in out if c.get("address") and c.get("AccessToken")]


def pick_server(servers, want, verify, timeout):
    """The named server, or the first saved one that answers.

    Tried rather than assumed: a config that has been around a while
    accumulates servers that have moved or gone, and dying on a connection
    error to a server the user was not asking about is a bad first
    impression for a diagnostic tool.
    """
    if want:
        for cred in servers:
            if want.lower() in (cred.get("Name") or "").lower() \
                    or want.lower() in cred["address"].lower():
                return Server(cred, verify=verify, timeout=timeout)
        raise SystemExit("no saved server matching %r; have: %s"
                         % (want, ", ".join(c.get("Name") or c["address"]
                                            for c in servers)))
    problems = []
    for cred in servers:
        srv = Server(cred, verify=verify, timeout=min(timeout, 15.0))
        try:
            srv.json("/System/Info/Public")
        except Exception as exc:
            problems.append("  %s: %s" % (cred.get("Name") or cred["address"],
                                          exc))
            continue
        srv.timeout = timeout
        return srv
    raise SystemExit("no saved server answered:\n" + "\n".join(problems))


# -- the server ------------------------------------------------------------

class Server:
    def __init__(self, cred, verify=True, timeout=120.0):
        import requests

        self.base = cred["address"].rstrip("/")
        self.user_id = cred.get("UserId") or ""
        self.name = cred.get("Name") or self.base
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = (
            'MediaBrowser Client="jellyfin-mpv-shim-bench", '
            'Device="bench", DeviceId="jms-image-bench", Version="1.0", '
            'Token="%s"' % cred["AccessToken"])
        self.session.headers["User-Agent"] = "jellyfin-mpv-shim-bench/1.0"

    def get(self, path, **params):
        """(seconds, bytes) for one GET. Raises on anything but 2xx."""
        t = time.perf_counter()
        resp = self.session.get(self.base + path, params=params or None,
                                timeout=self.timeout, verify=self.verify)
        body = resp.content
        dt = time.perf_counter() - t
        resp.raise_for_status()
        return dt, len(body)

    def json(self, path, **params):
        resp = self.session.get(self.base + path, params=params or None,
                                timeout=self.timeout, verify=self.verify)
        resp.raise_for_status()
        return resp.json()

    # -- discovery --

    def libraries(self):
        return (self.json("/Users/%s/Views" % self.user_id).get("Items")
                or [])

    def posters(self, parent_id, want, collection_type=""):
        """Up to `want` items from a library that actually have a poster.

        Asked for the way the grid asks -- typed and recursive where the
        collection type says so -- because an untyped query answers with the
        library's folders too, and a folder's poster is not what a grid
        draws.
        """
        types = {"movies": "Movie", "tvshows": "Series",
                 "music": "MusicAlbum", "musicvideos": "MusicVideo",
                 "boxsets": "BoxSet"}
        out, start, page = [], 0, {}
        while len(out) < want and start < want * 4:
            params = {"ParentId": parent_id, "StartIndex": start,
                      "Limit": min(200, want * 2),
                      "Fields": "PrimaryImageAspectRatio",
                      "SortBy": "SortName", "ImageTypeLimit": 1,
                      "EnableImageTypes": "Primary"}
            if collection_type in types:
                params["IncludeItemTypes"] = types[collection_type]
                params["Recursive"] = "true"
            page = self.json("/Users/%s/Items" % self.user_id, **params)
            items = page.get("Items") or []
            if not items:
                break
            for it in items:
                tag = (it.get("ImageTags") or {}).get("Primary")
                if tag:
                    out.append((it["Id"], tag))
            start += len(items)
        return out[:want], page.get("TotalRecordCount", 0)


def image_path(item_id, tag, width=None, height=None):
    p = "/Items/%s/Images/Primary" % item_id
    if width is None:
        return p, {}
    params = {"tag": tag, "quality": "90", "fillWidth": str(width)}
    if height:
        params["fillHeight"] = str(height)
    return p, params


# -- measurement -----------------------------------------------------------

def sequential(srv, items, width=None, height=None):
    """Fetch each poster in turn. Returns (times, total bytes)."""
    times, total = [], 0
    for item_id, tag in items:
        path, params = image_path(item_id, tag, width, height)
        try:
            dt, n = srv.get(path, **params)
        except Exception as exc:
            print("    ! %s" % exc)
            continue
        times.append(dt)
        total += n
    return times, total


def burst(srv, items, width, height, workers):
    """The first paint: `workers` at a time until they are all in.

    Returns (wall seconds, per-request times, bytes, failures) -- the wall
    time being the number that matters, since it is how long the server is
    busy and how long anything queued behind it waits.
    """
    times, total, failed = [], 0, 0
    t0 = time.perf_counter()

    def one(pair):
        path, params = image_path(pair[0], pair[1], width, height)
        return srv.get(path, **params)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(lambda p: _safe(one, p), items):
            if result is None:
                failed += 1
                continue
            dt, n = result
            times.append(dt)
            total += n
    return time.perf_counter() - t0, times, total, failed


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception:
        return None


def describe(times, total_bytes, label, n_items):
    """Print one row; return (mean seconds, KB per image) or (None, None).

    The size travels with the time because the one decision this report
    exists to inform -- resize or fetch the original -- turns on both, and a
    reader given only the milliseconds will read "originals are faster" as
    "use originals" and move fifteen times the bytes.
    """
    if not times:
        print("  %-38s  (nothing succeeded)" % label)
        return None, None
    mean = statistics.mean(times)
    kb = total_bytes / max(1, n_items) / 1024
    print("  %-38s mean %7.1fms   p95 %7.1fms   %6.1f KB/img"
          % (label, mean * 1000, _p95(times) * 1000, kb))
    return mean, kb


def _size(kb):
    return "%.0f KB" % kb if kb < 1024 else "%.1f MB" % (kb / 1024.0)


def _duration(seconds):
    if seconds < 90:
        return "%.0f seconds" % seconds
    if seconds < 5400:
        return "%.1f minutes" % (seconds / 60.0)
    return "%.1f hours" % (seconds / 3600.0)


def _p95(times):
    ordered = sorted(times)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


# -- report ----------------------------------------------------------------

def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    path = config_dir(args.config)
    if not os.path.isdir(path):
        raise SystemExit("no config directory at %s" % path)
    servers = load_servers(path)
    if not servers:
        raise SystemExit(
            "no saved servers in %s -- sign in with the app first, or point "
            "--config at a directory that has" % path)

    srv = pick_server(servers, args.server, not args.insecure, args.timeout)
    print("config   %s" % path)
    print("server   %s (%s)" % (srv.name, srv.base))

    libs = srv.libraries()
    if args.library:
        match = [v for v in libs
                 if args.library.lower() in (v.get("Name") or "").lower()]
        if not match:
            raise SystemExit("no library matching %r; have: %s"
                             % (args.library,
                                ", ".join(v.get("Name") or "?" for v in libs)))
        lib = match[0]
    else:
        # The biggest one that draws posters: the case that hurts.
        wanted = ("movies", "tvshows", "musicvideos", "boxsets")
        cands = [v for v in libs if (v.get("CollectionType") or "") in wanted]
        lib = cands[0] if cands else (libs[0] if libs else None)
    if lib is None:
        raise SystemExit("this account can see no libraries")

    ctype = lib.get("CollectionType") or ""
    need = max(args.samples, args.tiles)
    print("library  %s (%s)" % (lib.get("Name"), ctype or "untyped"))
    items, total = srv.posters(lib["Id"], need, ctype)
    if not items:
        raise SystemExit("no items with a Primary image in that library")
    print("         %d items, sampling %d with artwork\n"
          % (total, len(items)))

    seq = items[:args.samples]
    cold_w = args.cold_size or (args.width + 3 + int(time.time()) % 37)
    cold_h = int(round(cold_w * args.height / float(args.width)))

    print("one poster at a time (%d of them)" % len(seq))
    orig, orig_kb = describe(*sequential(srv, seq),
                             label="original, no resizing", n_items=len(seq))
    describe(*sequential(srv, seq), label="original again", n_items=len(seq))
    cold, cold_kb = describe(
        *sequential(srv, seq, cold_w, cold_h),
        label="resized to %dx%d (cold)" % (cold_w, cold_h), n_items=len(seq))
    warm, _kb = describe(*sequential(srv, seq, cold_w, cold_h),
                         label="the same size again (warm)", n_items=len(seq))
    near, _kb = describe(
        *sequential(srv, seq, cold_w + 1, cold_h + 1),
        label="one pixel wider (%dx%d)" % (cold_w + 1, cold_h + 1),
        n_items=len(seq))
    app, _kb = describe(
        *sequential(srv, seq, args.width, args.height),
        label="the app's own %dx%d" % (args.width, args.height),
        n_items=len(seq))

    print("\nfirst paint: %d posters, %d at a time"
          % (min(args.tiles, len(items)), args.workers))
    b_items = items[:args.tiles]
    bw = cold_w + 100          # cold again, and not a size measured above
    bh = int(round(bw * args.height / float(args.width)))
    wall, times, nbytes, failed = burst(srv, b_items, bw, bh, args.workers)
    print("  cold  %6.1fs wall   %5.1f img/s   %6.2f MB%s"
          % (wall, len(times) / max(wall, 1e-6), nbytes / 1048576.0,
             "   %d FAILED" % failed if failed else ""))
    wall2, times2, _b, failed2 = burst(srv, b_items, bw, bh, args.workers)
    print("  warm  %6.1fs wall   %5.1f img/s%s"
          % (wall2, len(times2) / max(wall2, 1e-6),
             "   %d FAILED" % failed2 if failed2 else ""))

    # -- what it means --
    print("\nverdict")
    if cold and warm:
        print("  * A cold resize costs %.0fx a cached one (%.0fms vs %.0fms)."
              % (cold / warm, cold * 1000, warm * 1000))
    if near and warm:
        if near > warm * 3:
            print("  * Sizes are cached per exact pixel size: one pixel wider "
                  "cost %.0fms against %.0fms warm. Every window resize "
                  "re-does every poster." % (near * 1000, warm * 1000))
        else:
            print("  * A neighbouring size was already cheap (%.0fms) -- this "
                  "server is not resizing per exact size, or something in "
                  "front of it is caching." % (near * 1000))
    if app and cold:
        print("  * The app's own %dx%d cost %.0fms against %.0fms for a size "
              "nothing has asked for: %s."
              % (args.width, args.height, app * 1000, cold * 1000,
                 "already cached" if app < cold / 2 else "cold too"))
    if orig and cold:
        ratio = (orig_kb / cold_kb) if cold_kb else 0
        if orig < cold:
            print("  * Unresized originals are faster here (%.0fms vs %.0fms) "
                  "and use no server CPU -- but at %.0fx the bytes (%.0f KB "
                  "vs %.0f KB each, so %s per first paint instead of %s). "
                  "Only a trade worth making on a link that has the "
                  "bandwidth to spare and a server that has not."
                  % (orig * 1000, cold * 1000, ratio, orig_kb, cold_kb,
                     _size(orig_kb * len(b_items)),
                     _size(cold_kb * len(b_items))))
        else:
            print("  * Resizing wins outright even cold: %.0fms and %.0f KB "
                  "against %.0fms and %.0f KB for the original."
                  % (cold * 1000, cold_kb, orig * 1000, orig_kb))
    print("  * One first paint keeps this server busy for %.1fs cold, %.1fs "
          "warm." % (wall, wall2))
    if wall > API_TIMEOUT:
        print("    !! Longer than the %ds an API call gets before the client "
              "gives up. A browse query issued during this can fail the "
              "screen with \"Failed to load. Check the connection.\""
              % API_TIMEOUT)
    elif wall > API_TIMEOUT / 3:
        print("    !  Within striking distance of the %ds API timeout; a "
              "slower moment or a bigger library would cross it."
              % API_TIMEOUT)
    if total > len(items) and cold:
        # Server-side work, not wall time: the workers overlap, but every one
        # of them is occupying a core on the other end.
        print("  * Scrolling asks for roughly this much again every few rows. "
              "All %d items at the cold rate is %s of server-side resizing."
              % (total, _duration(total * cold)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
