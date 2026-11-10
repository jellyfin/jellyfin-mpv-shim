#!/usr/bin/env python3
"""Per-version chapters, against a disposable Jellyfin 10.11.8.

    JH_JF_WORK=$W tools/limited_run.sh -t 600 -- \
        dist/local-test/venv/bin/python tools/jf_disposable/version_chapters_probe.py [--make-media-only]

Why: a multi-version movie is several items, each with only its own file's chapters and chapter
images. The player's scrub previews (media.Video) and the Scenes row (LibrarySource.get_chapters)
must use the version being played, not the primary. This builds two small films of several
versions each, scans them in a throw-away server, and checks what the server serves and what
the two consumers read.

    VerImg   (chapter-image extraction ON)   Theatrical 5 chapters, Directors Cut 3, Single 1, Bare 0
    VerPlain (extraction OFF)                A 3 chapters, B 2 -- chapters, but no images

Checked, per version: the chapter count, names and start times; that a tagged chapter's image is
served as a picture under the version's own id (and differs from the other versions' pictures);
that a chapter's image is NOT served under another version's id that has no such chapter; that
a version with one chapter or with extraction off has no image tags; and that media.Video and
LibrarySource.get_chapters, fed by the real server, return the same times, counts and images.
Exit status 0 only if every check holds. Results: $JH_JF_WORK/out/version_chapters_probe.json.

Disposable only: the media is generated under $JH_JF_WORK/media_versions with ffmpeg (no
network); the server, its database and its users are the temporary ones of jf_run.py, removed
at the end (processes included); no library, URL or account of yours is read or written. It
refuses to start outside a tools/limited_run.sh scope (3 GiB, no swap) unless --no-scope-check
is given (for the unit tests and for generating media only).

Run it with the venv python: the consumers import jellyfin_apiclient_python.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

SECONDS = 30
TICKS = 10_000_000

#: (library, folder, film, extraction, [(label, [(start_s, title)], lavfi source)])
LIBRARIES = [
    ("VerImg", "Test Film (2001)", "Test Film", True, [
        ("Theatrical", [(0, "T1"), (6, "T2"), (12, "T3"), (18, "T4"), (24, "T5")], "testsrc2"),
        ("Directors Cut", [(0, "D1"), (14, "D2"), (20, "D3")], "testsrc"),
        ("Single", [(0, "S1")], "smptebars"),
        ("Bare", [], "rgbtestsrc"),
    ]),
    ("VerPlain", "Plain Film (2002)", "Plain Film", False, [
        ("A", [(0, "A1"), (10, "A2"), (20, "A3")], "testsrc2"),
        ("B", [(0, "B1"), (15, "B2")], "testsrc"),
    ]),
]


# -- pure parts (unit-tested; nothing here starts anything) ----------------------------------

def media_dir(work):
    return os.path.join(work, "media_versions")


def media_path(work, library, folder, film, label):
    year = folder[folder.rindex("(") + 1:-1]
    return os.path.join(media_dir(work), library, folder,
                        "%s (%s) - %s.mkv" % (film, year, label))


def ffmetadata(chapters):
    out = ";FFMETADATA1\n"
    for n, (start, title) in enumerate(chapters):
        end = chapters[n + 1][0] if n + 1 < len(chapters) else SECONDS
        out += "[CHAPTER]\nTIMEBASE=1/1000\nSTART=%d\nEND=%d\ntitle=%s\n" % (start * 1000, end * 1000, title)
    return out


def ffmpeg_command(out, source, meta_path=None):
    """The ffmpeg argv for one version; ``meta_path`` carries its chapters, or none are written."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source + "=size=640x360:rate=10",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    if meta_path:
        cmd += ["-i", meta_path, "-map_metadata", "2", "-map_chapters", "2"]
    else:
        cmd += ["-map_chapters", "-1"]
    return cmd + ["-t", str(SECONDS), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-c:a", "aac", out]


def expect_image(extraction, chapters):
    """Whether the server gives each of ``chapters`` an image: extraction on, and more than one
    chapter (Jellyfin 10.11.8 tags none of a lone chapter)."""
    return bool(extraction) and len(chapters) > 1


def expected(extraction, chapters):
    img = expect_image(extraction, chapters)
    return [{"name": t, "start": s, "image": img} for s, t in chapters]


def check_server(extraction, chapters, got):
    """Problems between one version's expected chapters and ``got`` (the server's facts):
    ``{"chapters": [{name, start, tag, status, type, bytes, sha, foreign}...]}``."""
    bad = []
    want = expected(extraction, chapters)
    rows = got.get("chapters") or []
    if len(rows) != len(want):
        return ["%d chapters, expected %d" % (len(rows), len(want))]
    for i, (w, r) in enumerate(zip(want, rows)):
        if r.get("name") != w["name"]:
            bad.append("chapter %d named %r, expected %r" % (i, r.get("name"), w["name"]))
        if r.get("start") != w["start"]:
            bad.append("chapter %d starts at %r s, expected %r" % (i, r.get("start"), w["start"]))
        if bool(r.get("tag")) != w["image"]:
            bad.append("chapter %d %s, expected %s" % (
                i, "has an image tag" if r.get("tag") else "has no image tag",
                "one" if w["image"] else "none"))
        if w["image"]:
            if r.get("status") != 200 or not str(r.get("type") or "").startswith("image/") \
                    or (r.get("bytes") or 0) < 100:
                bad.append("chapter %d image: status %r type %r bytes %r" % (
                    i, r.get("status"), r.get("type"), r.get("bytes")))
            if r.get("foreign") == 200:
                bad.append("chapter %d image is also served under another version's id" % i)
    return bad


def check_distinct(shas):
    """``{label: [sha of each image]}`` -- the pictures of different versions must differ."""
    seen, bad = {}, []
    for label, hs in shas.items():
        for h in hs:
            if h in seen and seen[h] != label:
                bad.append("%s and %s serve the same chapter picture" % (seen[h], label))
            seen.setdefault(h, label)
    return sorted(set(bad))


def check_consumers(extraction, chapters, player, scenes):
    """``player``: {times, n_images} from media.Video; ``scenes``: the cleaned list from
    LibrarySource.get_chapters as [(name, start_s, has_tag)]."""
    bad = []
    want = expected(extraction, chapters)
    imaged = [w for w in want if w["image"]]
    if player.get("times") != [float(w["start"]) for w in imaged]:
        bad.append("player previews at %r, expected %r" % (player.get("times"), [w["start"] for w in imaged]))
    if player.get("n_images") != len(imaged):
        bad.append("player fetched %r images, expected %d" % (player.get("n_images"), len(imaged)))
    if [tuple(x) for x in scenes] != [(w["name"], w["start"], w["image"]) for w in want]:
        bad.append("Scenes row source %r, expected %r" % (scenes, [(w["name"], w["start"], w["image"]) for w in want]))
    return bad


def in_limited_scope():
    import ab_metrics as M
    cg = M.cgroup_dir()
    return bool(cg and "jh-limited-" in cg)


# -- media ----------------------------------------------------------------------------------

def make_media(work):
    for library, folder, film, _ext, versions in LIBRARIES:
        for label, chapters, source in versions:
            out = media_path(work, library, folder, film, label)
            if os.path.exists(out):
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            meta = None
            try:
                if chapters:
                    with tempfile.NamedTemporaryFile("w", suffix=".ffmeta", delete=False) as fh:
                        fh.write(ffmetadata(chapters))
                        meta = fh.name
                subprocess.run(ffmpeg_command(out, source, meta), check=True)
            finally:
                if meta:
                    os.unlink(meta)


# -- the server run --------------------------------------------------------------------------

def run(work):
    sys.argv, argv = [sys.argv[0], "server"], sys.argv      # jf_run reads argv[1] as its phase
    import chapter_probe as CP
    sys.argv = argv
    J = CP.J
    import requests
    from unittest import mock
    sys.path.insert(0, ROOT)
    from jellyfin_mpv_shim.media import Video
    from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
    from jellyfin_mpv_shim.mpvtk_browser.pages.detail import _clean_chapters

    t0, out, problems = time.time(), {}, []
    try:
        J.start_server()
        tok, _upw = CP.wizard()
        for library, _f, _film, ext, _v in LIBRARIES:
            opts = {"EnableRealtimeMonitor": False, "EnableChapterImageExtraction": ext,
                    "ExtractChapterImagesDuringLibraryScan": ext,
                    "EnableTrickplayImageExtraction": False, "ExtractTrickplayImagesDuringLibraryScan": False,
                    "EnableInternetProviders": False, "EnablePhotos": False, "MetadataSavers": [],
                    "PathInfos": [{"Path": os.path.join(media_dir(work), library)}]}
            J.requests.post(J.BASE + "/Library/VirtualFolders",
                            params={"name": library, "collectionType": "movies", "refreshLibrary": "false"},
                            headers=J.hdr(tok), json={"LibraryOptions": opts}, timeout=30).raise_for_status()
        J.api("POST", "/Library/Refresh", tok)
        movies = J.wait_for(lambda: (lambda m: m if len(m) == len(LIBRARIES) else None)(
            J.api("GET", "/Items", tok, params={"Recursive": "true", "IncludeItemTypes": "Movie"}).json()["Items"]),
            "both films scanned", 180, every=3)
        uid = next(u["Id"] for u in J.api("GET", "/Users", tok).json() if u["Name"] == "tmpadmin")

        def get_item(item_id, fields=None):
            r = requests.get(J.BASE + "/Users/%s/Items/%s" % (uid, item_id), params={"Fields": fields},
                             headers=J.hdr(tok), timeout=20)
            r.raise_for_status()
            return r.json()

        class Jf:                                           # the apiclient calls the consumers make
            def get_item(self, item_id, fields=None):
                return get_item(item_id, fields)

            def get_chapter_image(self, dest, item_id, index, tag=None, max_width=None, quality=None):
                r = requests.get(J.BASE + "/Items/%s/Images/Chapter/%d" % (item_id, index),
                                 params={"maxWidth": max_width, "tag": tag, "quality": quality},
                                 headers=J.hdr(tok), timeout=20)
                r.raise_for_status()
                dest.write(r.content)

        class Api:                                          # LibrarySource's two
            def get_item(self, item_id, fields=None):
                return get_item(item_id, fields)

            def image_url(self, item_id, art, max_width=None, tag=None, index=None, **kw):
                return "%s/Items/%s/Images/%s/%d" % (J.BASE, item_id, art, index)
        client = type("C", (), {"jellyfin": Jf()})()
        source = LibrarySource.__new__(LibrarySource)
        source._conns = {"srv": mock.Mock(api=Api())}

        shas = {}
        for library, folder, film, ext, versions in LIBRARIES:
            primary = next(m for m in movies if m["Name"].startswith(film))
            pid = primary["Id"]
            by_label = {s["Name"]: s["Id"] for s in
                        get_item(pid, "MediaSources").get("MediaSources") or []}
            if sorted(by_label) != sorted(v[0] for v in versions):
                problems.append("%s: versions %r, expected %r" % (library, sorted(by_label), [v[0] for v in versions]))
                continue
            # extraction is asynchronous: wait for every tag this library should end up with
            deadline = time.time() + (150 if ext else 5)
            while time.time() < deadline:
                done = all(all(c.get("ImageTag") for c in get_item(i, "Chapters").get("Chapters") or [])
                           or len(get_item(i, "Chapters").get("Chapters") or []) < 2 for i in by_label.values())
                if done:
                    break
                time.sleep(4)
            for label, chapters, _src in versions:
                vid = by_label[label]
                other = next((i for lb, i in by_label.items() if lb != label
                              and len(get_item(i, "Chapters").get("Chapters") or []) < len(chapters)), None)
                rows, hashes = [], []
                for i, c in enumerate(get_item(vid, "Chapters").get("Chapters") or []):
                    tag = c.get("ImageTag")
                    row = {"name": c.get("Name"), "start": (c.get("StartPositionTicks") or 0) // TICKS, "tag": tag}
                    if tag:
                        r = requests.get("%s/Items/%s/Images/Chapter/%d" % (J.BASE, vid, i),
                                         params={"maxWidth": 320, "tag": tag}, headers=J.hdr(tok), timeout=20)
                        row.update(status=r.status_code, type=r.headers.get("content-type"), bytes=len(r.content))
                        hashes.append(hashlib.sha256(r.content).hexdigest())
                        if other:                           # a version with fewer chapters has no such image
                            row["foreign"] = requests.get(
                                "%s/Items/%s/Images/Chapter/%d" % (J.BASE, other, i), params={"maxWidth": 320},
                                headers=J.hdr(tok), timeout=20).status_code if i >= len(
                                get_item(other, "Chapters").get("Chapters") or []) else None
                    rows.append(row)
                shas[label] = hashes
                got = {"id": vid, "primary": vid == pid, "chapters": rows}
                srv_bad = check_server(ext, chapters, got)

                v = Video.__new__(Video)
                v.item_id, v.client, v.media_source = pid, client, {"Id": vid}
                v.item = get_item(pid, "Chapters")
                player = {"times": [c["start"] for c in v.get_chapters()],
                          "n_images": len(list(v.get_chapter_images(320, 80)))}
                scenes = [(c["Name"], c["StartPositionTicks"] // TICKS, bool(c["ImageTag"]))
                          for c in _clean_chapters(source.get_chapters("srv", vid))]
                bad = srv_bad + check_consumers(ext, chapters, player, scenes)
                out["%s/%s" % (library, label)] = dict(got, player=player, scenes=scenes, problems=bad)
                problems += ["%s/%s: %s" % (library, label, b) for b in bad]
        problems += check_distinct({k: v for k, v in shas.items()})
    except Exception:
        import traceback
        problems.append("probe failed: " + J.redact(traceback.format_exc()[-900:]))
    finally:
        out["problems"] = problems
        with open(os.path.join(J.OUT, "version_chapters_probe.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=str)
        J.teardown(t0)
    for p in problems:
        print("PROBLEM:", p)
    print("version_chapters_probe:", "FAILED (%d)" % len(problems) if problems else "ok")
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--make-media-only", action="store_true", help="write the test videos and stop")
    ap.add_argument("--no-scope-check", action="store_true", help="skip the limited_run.sh scope requirement")
    args = ap.parse_args(argv)
    work = os.environ.get("JH_JF_WORK", os.path.join(HERE, "work"))
    if not args.no_scope_check and not args.make_media_only and not in_limited_scope():
        print("version_chapters_probe: not inside a tools/limited_run.sh scope; refusing to start a server",
              file=sys.stderr)
        return 2
    make_media(work)
    if args.make_media_only:
        return 0
    return run(work)


if __name__ == "__main__":
    sys.exit(main())
