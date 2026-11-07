#!/usr/bin/env python3
"""Disposable Jellyfin 10.11.8 + JellyHome playback test. Runs INSIDE one limited scope.

phase=server : start server, wizard, accounts, library scan, report probe/NFO, stop.
phase=full   : also start the client, play via remote control, control, report, stop.
Everything is loopback-only; all state lives in a temp dir that is removed at the end.
"""
import json, os, re, secrets, shutil, signal, subprocess, sys, tempfile, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("JH_JF_WORK", os.path.join(HERE, "work"))   # server/, media/ and outputs live here
OUT = os.path.join(WORK, "out"); os.makedirs(OUT, exist_ok=True)
PORT = 18097
BASE = "http://127.0.0.1:%d" % PORT
PHASE = sys.argv[1] if len(sys.argv) > 1 else "server"
SERVER_BIN = os.path.join(WORK, "server", "jellyfin", "jellyfin")
MEDIA = os.environ.get("JH_JF_MEDIA") or os.path.join(WORK, "media", "Movies")   # JH_JF_MEDIA: another library folder
REPO = os.path.dirname(os.path.dirname(HERE))
D = tempfile.mkdtemp(prefix="jf-disposable-")
SECRETS = []
procs = {}
ADMIN = None
samples = []          # (t, name, rss_kib, cpu_ticks)


def redact(s):
    for x in SECRETS:
        s = s.replace(x, "REDACTED")
    return re.sub(r'(?i)(token|api_?key|password|pw)(["\'=: ]+)[^&"\', }]+', r'\1\2REDACTED', s)


def log(*a):
    print(redact(" ".join(str(x) for x in a)), flush=True)


AUTH = 'MediaBrowser Client="jh-smoke", Device="smoke", DeviceId="jh-smoke-setup", Version="1.0"'


def hdr(token=None):
    return {"Authorization": AUTH + (', Token="%s"' % token if token else ""), "Content-Type": "application/json"}


def api(method, path, token=None, ok=(200, 204), **kw):
    r = requests.request(method, BASE + path, headers=hdr(token), timeout=30, **kw)
    if r.status_code not in ok:
        raise RuntimeError("%s %s -> %s %s" % (method, path, r.status_code, redact(r.text[:300])))
    return r


def tree_pids(root):
    """root pid + descendants (ppid walk); cgroup membership is the real boundary, this is for labels."""
    kids = {}
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            st = open("/proc/%s/stat" % p, encoding="utf-8").read(); pp = int(st[st.rindex(")") + 2:].split()[1]); kids.setdefault(pp, []).append(int(p))
        except OSError:
            pass
    out, todo = [], [root]
    while todo:
        x = todo.pop(); out.append(x); todo += kids.get(x, [])
    return out


def stat(pid):
    try:
        st = open("/proc/%d/stat" % pid, encoding="utf-8").read(); f = st[st.rindex(")") + 2:].split()
        rss = int(open("/proc/%d/statm" % pid, encoding="utf-8").read().split()[1]) * 4
        return rss, int(f[11]) + int(f[12])
    except (OSError, ValueError, IndexError):
        return None


def sample(t0):
    for name, pr in list(procs.items()):
        if pr.poll() is not None:
            continue
        rss = ticks = 0
        for p in tree_pids(pr.pid):
            s = stat(p)
            if s:
                rss += s[0]; ticks += s[1]
        samples.append((round(time.time() - t0, 1), name, rss, ticks))


def wait_for(fn, what, timeout=60, every=1.0, t0=None):
    end = time.time() + timeout; last = None
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:                      # noqa
            last = e
        if t0 is not None:
            sample(t0)
        time.sleep(every)
    raise TimeoutError("%s (last error: %s)" % (what, redact(str(last))))


def start_server():
    for sub in ("data", "config", "cache", "log"):
        os.makedirs(os.path.join(D, sub))
    open(os.path.join(D, "config", "network.xml"), "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="utf-8"?><NetworkConfiguration>'
        '<InternalHttpPort>%d</InternalHttpPort><PublicHttpPort>%d</PublicHttpPort>'
        '<EnableHttps>false</EnableHttps><RequireHttps>false</RequireHttps>'
        '<AutoDiscovery>false</AutoDiscovery><EnableUPnP>false</EnableUPnP><EnableIPv6>false</EnableIPv6>'
        '<EnableIPv4>true</EnableIPv4><EnableRemoteAccess>false</EnableRemoteAccess>'
        '<LocalNetworkAddresses><string>127.0.0.1</string></LocalNetworkAddresses>'
        '</NetworkConfiguration>' % (PORT, PORT))
    env = dict(os.environ, JELLYFIN_LOG_DIR=os.path.join(D, "log"))
    cmd = [SERVER_BIN, "--datadir", os.path.join(D, "data"), "--configdir", os.path.join(D, "config"),
           "--cachedir", os.path.join(D, "cache"), "--logdir", os.path.join(D, "log"),
           "--ffmpeg", shutil.which("ffmpeg"), "--nowebclient"]
    procs["server"] = subprocess.Popen(cmd, stdout=open(os.path.join(OUT, "server.stdout"), "w", encoding="utf-8"),
                                       stderr=subprocess.STDOUT, env=env, start_new_session=True)
    log("server pid", procs["server"].pid)


def setup_server(t0):
    wait_for(lambda: requests.get(BASE + "/Startup/Configuration", headers=hdr(), timeout=3).status_code == 200, "server finished starting (wizard API answers 200)", 120, t0=t0)
    pub = requests.get(BASE + "/System/Info/Public", timeout=5).json()
    log("server:", pub.get("ProductName"), pub.get("Version"), "startup wizard completed:", pub.get("StartupWizardCompleted"))
    admin_pw, user_pw = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    SECRETS.extend([admin_pw, user_pw])
    api("POST", "/Startup/Configuration", json={"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
    api("GET", "/Startup/User")
    api("POST", "/Startup/User", json={"Name": "tmpadmin", "Password": admin_pw})
    api("POST", "/Startup/RemoteAccess", json={"EnableRemoteAccess": False, "EnableAutomaticPortMapping": False})
    api("POST", "/Startup/Complete")
    r = api("POST", "/Users/AuthenticateByName", json={"Username": "tmpadmin", "Pw": admin_pw})
    tok = r.json()["AccessToken"]; SECRETS.append(tok); global ADMIN; ADMIN = tok
    api("POST", "/Users/New", tok, json={"Name": "viewer", "Password": user_pw})
    users = api("GET", "/Users", tok).json()
    viewer = next(u for u in users if u["Name"] == "viewer")
    pol = viewer["Policy"]
    log("viewer account: IsAdministrator=%s EnableMediaPlayback=%s EnableVideoPlaybackTranscoding=%s EnableAudioPlaybackTranscoding=%s EnablePlaybackRemuxing=%s"
        % (pol["IsAdministrator"], pol["EnableMediaPlayback"], pol["EnableVideoPlaybackTranscoding"],
           pol["EnableAudioPlaybackTranscoding"], pol["EnablePlaybackRemuxing"]))
    cfgu = viewer.get("Configuration", {})
    log("viewer Configuration (subtitle/audio preferences): " + json.dumps({k: cfgu.get(k) for k in (
        "SubtitleMode", "SubtitleLanguagePreference", "AudioLanguagePreference", "PlayDefaultAudioTrack", "RememberAudioSelections", "RememberSubtitleSelections")}))
    libopts = {"EnableRealtimeMonitor": False, "EnableChapterImageExtraction": False, "ExtractChapterImagesDuringLibraryScan": False,
               "EnableTrickplayImageExtraction": False, "ExtractTrickplayImagesDuringLibraryScan": False, "SaveTrickplayWithMedia": False,
               "EnableInternetProviders": False, "EnablePhotos": False, "DownloadImagesInAdvance": False,
               "TypeOptions": [{"Type": "Movie", "MetadataFetchers": [], "ImageFetchers": []}],
               "MetadataSavers": [], "PathInfos": [{"Path": MEDIA}]}
    requests.post(BASE + "/Library/VirtualFolders", params={"name": "Movies", "collectionType": "movies", "refreshLibrary": "true"},
                  headers=hdr(tok), json={"LibraryOptions": libopts}, timeout=30).raise_for_status()
    item = wait_for(lambda: next(iter(api("GET", "/Items", tok, params={"Recursive": "true", "IncludeItemTypes": "Movie"}).json()["Items"]), None),
                    "library scan finds the movie", 120, t0=t0)
    return tok, user_pw, item["Id"], viewer["Id"], pub


def main():
    t0 = time.time(); rc = 1; client = None; xinfo = {}
    try:
        start_server()
        tok, user_pw, item_id, viewer_id, pub = setup_server(t0)
        log("movie item id:", item_id, " (library scan done in %.0fs)" % (time.time() - t0))
        lst = subprocess.run(["ss", "-ltnup"], capture_output=True, text=True).stdout
        mine = [l for l in lst.splitlines() if "jellyfin" in l or str(PORT) in l]
        log("listening sockets of the server process:"); [log("  ", l.strip()) for l in mine]
        info = api("GET", "/Items/%s" % item_id, tok, params={"userId": viewer_id}).json()
        ms = info["MediaSources"][0]
        log("server's source defaults: DefaultAudioStreamIndex=%s DefaultSubtitleStreamIndex=%s" % (ms.get("DefaultAudioStreamIndex"), ms.get("DefaultSubtitleStreamIndex")))
        log("server's probe: container=%s runtimeTicks=%s bitrate=%s" % (ms.get("Container"), ms.get("RunTimeTicks"), ms.get("Bitrate")))
        for s in ms["MediaStreams"]:
            log("  stream", s["Index"], s["Type"], s.get("Codec"), "profile=%s" % s.get("Profile"),
                "%sx%s" % (s.get("Width"), s.get("Height")) if s["Type"] == "Video" else "",
                "AR=%s interlaced=%s fps=%s" % (s.get("AspectRatio"), s.get("IsInterlaced"), s.get("RealFrameRate")) if s["Type"] == "Video" else "",
                "ch=%s lang=%s title=%s ext=%s def=%s" % (s.get("Channels"), s.get("Language"), s.get("Title"), s.get("IsExternal"), s.get("IsDefault")),
                "DisplayTitle=%s" % s.get("DisplayTitle"))
        if PHASE == "full":
            rc = run_client(tok, user_pw, item_id, viewer_id, t0)
        else:
            rc = 0
    except Exception as e:
        log("FAILED:", type(e).__name__, redact(str(e))); rc = 1
    finally:
        teardown(t0)
    return rc


def run_client(tok, user_pw, item_id, viewer_id, t0):
    exec(open(os.path.join(HERE, "jf_client.py"), encoding="utf-8").read(), globals())
    return client_phase(tok, user_pw, item_id, viewer_id, t0)


def teardown(t0):
    log("--- teardown")
    if ADMIN and procs.get("server") and procs["server"].poll() is None:
        try:
            requests.post(BASE + "/System/Shutdown", headers=hdr(ADMIN), timeout=5); log("asked server to shut down via API")
            procs["server"].wait(25)
        except Exception as e:
            log("graceful shutdown did not finish:", type(e).__name__)
    for name in ("client", "server"):
        pr = procs.get(name)
        if pr and pr.poll() is None:
            try:
                os.killpg(pr.pid, signal.SIGTERM)
            except OSError:
                pass
    for name in ("client", "server"):
        pr = procs.get(name)
        if pr:
            try:
                pr.wait(20); log(name, "exited rc", pr.returncode)
            except subprocess.TimeoutExpired:
                os.killpg(pr.pid, signal.SIGKILL); log(name, "had to be SIGKILLed")
    # copy sanitised server log
    for f in sorted(os.listdir(os.path.join(D, "log"))) if os.path.isdir(os.path.join(D, "log")) else []:
        txt = open(os.path.join(D, "log", f), errors="replace", encoding="utf-8").read()
        open(os.path.join(OUT, "server_" + f), "w", encoding="utf-8").write(redact(txt))
    with open(os.path.join(OUT, "samples.csv"), "w", encoding="utf-8") as fh:
        fh.write("t_s,proc,rss_kib,cpu_ticks\n"); [fh.write("%s,%s,%d,%d\n" % s) for s in samples]
    shutil.rmtree(D, ignore_errors=True); log("temp dir removed:", not os.path.exists(D))


if __name__ == "__main__":
    sys.exit(main())
