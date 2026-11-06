#!/usr/bin/env python3
"""Chapter thumbnails against a disposable Jellyfin 10.11.8, API side and app side.

    JH_JF_WORK=$W tools/limited_run.sh -t 600 -- env -u WAYLAND_DISPLAY xvfb-run -a \
        -s "-screen 0 1280x720x24 -nolisten tcp" python3 tools/jf_disposable/chapter_probe.py [api|app]

Needs the media from make_chapter_media.py. Starts the server of jf_run.py (loopback,
temp state, removed at the end), creates four libraries -- extraction of chapter images
ON for MoviesImg/ShowsImg, OFF for MoviesPlain/ShowsPlain -- and reports, per item, what
the server says (Chapters[].ImageTag) and whether the image URL jellyfin-web builds
(/Items/{id}/Images/Chapter/{i}?tag=) serves a picture. `app` also drives one JellyHome
instance to each item's page and reports what the Scenes row drew.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv, _argv = [sys.argv[0], "server"], sys.argv   # jf_run reads argv[1] as its phase
import jf_run as J                                      # noqa: E402
sys.argv = _argv
import requests                                         # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "api"
MEDIA = os.path.join(J.WORK, "media")
LIBS = [  # name, collection type, folder, extraction
    ("MoviesImg", "movies", "MoviesImg", True),
    ("MoviesPlain", "movies", "MoviesPlain", False),
    ("ShowsImg", "tvshows", "ShowsImg", True),
    ("ShowsPlain", "tvshows", "ShowsPlain", False),
    ("MoviesVer", "movies", "MoviesVer", True),
]
OUT = {}


def wizard():
    J.wait_for(lambda: requests.get(J.BASE + "/Startup/Configuration", headers=J.hdr(), timeout=3).status_code == 200,
               "server up", 120)
    pw, upw = J.secrets.token_urlsafe(12), J.secrets.token_urlsafe(12)
    J.SECRETS.extend([pw, upw])
    J.api("POST", "/Startup/Configuration", json={"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
    J.api("GET", "/Startup/User")
    J.api("POST", "/Startup/User", json={"Name": "tmpadmin", "Password": pw})
    J.api("POST", "/Startup/RemoteAccess", json={"EnableRemoteAccess": False, "EnableAutomaticPortMapping": False})
    J.api("POST", "/Startup/Complete")
    tok = J.api("POST", "/Users/AuthenticateByName", json={"Username": "tmpadmin", "Pw": pw}).json()["AccessToken"]
    J.SECRETS.append(tok); J.ADMIN = tok
    J.api("POST", "/Users/New", tok, json={"Name": "viewer", "Password": upw})
    return tok, upw


def libraries(tok):
    for name, ctype, folder, extract in LIBS:
        opts = {"EnableRealtimeMonitor": False, "EnableChapterImageExtraction": extract,
                "ExtractChapterImagesDuringLibraryScan": extract,
                "EnableTrickplayImageExtraction": False, "ExtractTrickplayImagesDuringLibraryScan": False,
                "EnableInternetProviders": False, "EnablePhotos": False, "MetadataSavers": [],
                "PathInfos": [{"Path": os.path.join(MEDIA, folder)}]}
        J.requests.post(J.BASE + "/Library/VirtualFolders",
                        params={"name": name, "collectionType": ctype, "refreshLibrary": "false"},
                        headers=J.hdr(tok), json={"LibraryOptions": opts}, timeout=30).raise_for_status()
    J.api("POST", "/Library/Refresh", tok)


def find_items(tok):
    want = {"Angel Face Img": "Movie", "Angel Face Plain": "Movie",
            "Chapter Show Img - S01E01": "Episode", "Chapter Show Plain - S01E01": "Episode",
            "Angel Face Ver": "Movie"}

    def got():
        r = J.api("GET", "/Items", tok, params={"Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                                               "Fields": "Path,MediaSources"}).json()["Items"]
        out = {}
        for it in r:
            for key in want:
                if it["Name"].startswith(key) or key in (it.get("Path") or ""):
                    out[key] = it
        return out if len(out) == len(want) else None
    return J.wait_for(got, "all items scanned", 180, every=2)


def api_facts(tok, items, wait_images=True):
    facts = {}
    for key, it in items.items():
        d = None
        deadline = time.time() + (150 if wait_images and "Img" in key else 5)
        while time.time() < deadline:
            d = J.api("GET", "/Items/%s" % it["Id"], tok).json()
            ch = d.get("Chapters") or []
            if len(ch) >= 2 and (not ("Img" in key and wait_images) or all(c.get("ImageTag") for c in ch)):
                break
            time.sleep(3)
        ch = d.get("Chapters") or []
        rows = []
        for i, c in enumerate(ch):
            tag = c.get("ImageTag")
            row = {"i": i, "name": c.get("Name"), "start": c.get("StartPositionTicks"), "tag": tag}
            if tag:  # the URL jellyfin-web's chaptercardbuilder builds
                r = requests.get(J.BASE + "/Items/%s/Images/Chapter/%d" % (it["Id"], i),
                                 params={"maxWidth": 320, "tag": tag, "quality": 90},
                                 headers=J.hdr(tok), timeout=20)
                row["status"] = r.status_code
                row["type"] = r.headers.get("content-type")
                row["bytes"] = len(r.content)
            rows.append(row)
        srcs = d.get("MediaSources") or []
        if len(srcs) > 1:
            J.log("VERSIONS", key, json.dumps([{"id": x.get("Id"), "name": x.get("Name"), "has_Chapters_key": "Chapters" in x,
                                                "n": len(x.get("Chapters") or [])} for x in srcs]))
            for x in srcs:
                a = J.api("GET", "/Items/%s" % x["Id"], tok).json()
                J.log("VERSION-ITEM", x["Id"], "type", a.get("Type"), "chapters",
                      json.dumps([(c.get("Name"), bool(c.get("ImageTag"))) for c in a.get("Chapters") or []]))
        facts[key] = {"id": it["Id"], "type": d.get("Type"), "sources": len(srcs),
                      "source_ids": [s.get("Id") for s in srcs], "chapters": rows}
        J.log("API", key, json.dumps(facts[key]))
    return facts


def main():
    t0 = time.time(); rc = 1
    try:
        J.start_server()
        tok, upw = wizard()
        libraries(tok)
        items = find_items(tok)
        facts = api_facts(tok, items)
        OUT["facts"] = facts
        if MODE == "app":
            import app_stage
            app_stage.run(J, OUT, tok, upw, facts)
        rc = 0
    except Exception as e:
        import traceback
        J.log("FAILED:", type(e).__name__, J.redact(str(e)), J.redact(traceback.format_exc()[-1500:])); rc = 1
    finally:
        with open(os.path.join(J.OUT, "chapter_probe.json"), "w", encoding="utf-8") as fh:
            json.dump(OUT, fh, indent=1, default=str)
        J.teardown(t0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
