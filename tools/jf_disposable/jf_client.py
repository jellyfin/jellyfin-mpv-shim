# exec'd into jf_run's globals. Drives the real client against the disposable server.
import glob as _glob

def nap(sec, t0):
    end = time.time() + sec
    while time.time() < end:
        sample(t0); time.sleep(1.0 if end - time.time() > 1 else max(0, end - time.time()))

def shot(name):
    subprocess.run(["import", "-window", "root", os.path.join(OUT, name)], capture_output=True, timeout=20)

def session(tok, did=None):
    for s in api("GET", "/Sessions", tok).json():
        if "JellyHome" in (s.get("Client") or "") and s.get("SupportsRemoteControl"):
            return s
    return None

def ps_of(tok):
    s = session(tok) or {}
    p = s.get("PlayState") or {}
    return s, p

def basic_phase(tok, user_pw, item_id, viewer_id, t0):
    cfg = os.path.join(D, "client-cfg"); os.makedirs(cfg)
    # Xvfb-only workaround (NOT an app default): the GPU VO cannot initialise on a virtual display.
    open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("vo=x11\ninput-ipc-server=%s\n" % os.path.join(D, "mpv.sock"))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = [sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "--gui", "--debug"]
    clog = open(os.path.join(OUT, "client.log.raw"), "w", encoding="utf-8")
    procs["client"] = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=clog, stderr=subprocess.STDOUT, start_new_session=True)
    log("client pid", procs["client"].pid)
    result = {}
    try:
        # --- log in through the real login form (coordinates from the rendered 1280x720 login card)
        def _login_ready():
            return "showing login" in open(os.path.join(OUT, "client.log.raw"), encoding="utf-8", errors="replace").read()
        wait_for(_login_ready, "login screen is up (client log says so)", 40, 0.5, t0=t0)
        nap(1.5, t0); shot("c0_login_screen.png")
        def click(x, y):
            subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True); time.sleep(0.4)
            subprocess.run(["xdotool", "click", "1"], check=True); time.sleep(0.4)
        def typ(text):
            subprocess.run(["xdotool", "type", "--delay", "40", text], check=True); time.sleep(0.4)
        click(715, 309); typ(BASE)
        click(715, 357); typ("viewer")
        click(715, 405); typ(user_pw)
        shot("c0b_form_filled.png")
        click(851, 458)
        log("login form submitted through the GUI")
        sess = wait_for(lambda: session(tok), "client registers a controllable session", 60, t0=t0)
        log("client session: Client=%s Version=%s Device=%s user=%s SupportsRemoteControl=%s PlayableMediaTypes=%s"
            % (sess["Client"], sess.get("ApplicationVersion"), sess["DeviceName"], sess["UserName"], sess["SupportsRemoteControl"], sess.get("PlayableMediaTypes")))
        result["login"] = True
        nap(3, t0); shot("c1_after_login.png")
        sid = sess["Id"]
        # --- start playback (server-initiated remote control => the client negotiates PlaybackInfo itself)
        api("POST", "/Sessions/%s/Playing" % sid, tok, params={"playCommand": "PlayNow", "itemIds": item_id, "startPositionTicks": 0})
        s, p = wait_for(lambda: (lambda sp: sp if sp[0] and sp[0].get("NowPlayingItem") else None)(ps_of(tok)), "playback starts", 40, t0=t0)
        log("PLAYBACK STARTED: NowPlaying=%s" % s["NowPlayingItem"].get("Name"))
        nap(3, t0)
        s, p = ps_of(tok)
        log("play state @~3s: PlayMethod=%s Pos=%.1fs Paused=%s Audio=%s Sub=%s TranscodingInfo=%s"
            % (p.get("PlayMethod"), (p.get("PositionTicks") or 0) / 1e7, p.get("IsPaused"), p.get("AudioStreamIndex"), p.get("SubtitleStreamIndex"), s.get("TranscodingInfo")))
        result["method"] = p.get("PlayMethod"); result["transcoding"] = s.get("TranscodingInfo")
        shot("c2_playing.png")
        try:
            _ipc = Ipc(os.path.join(D, "mpv.sock"))
            if _ipc.connect(5):
                log("IPC pipeline (Xvfb):", json.dumps({k: _ipc.get(k) for k in ("current-vo", "hwdec-current", "video-codec", "deinterlace")}))
        except Exception as e:
            log("IPC read failed:", type(e).__name__)
        pos1 = p.get("PositionTicks") or 0
        nap(2, t0); s, p = ps_of(tok); pos2 = p.get("PositionTicks") or 0
        log("position advanced %.1fs over ~2s (sustained playback)" % ((pos2 - pos1) / 1e7)); result["sustained"] = pos2 > pos1
        # --- pause / resume
        api("POST", "/Sessions/%s/Playing/Pause" % sid, tok)
        nap(2, t0); s, p = ps_of(tok); a = p.get("PositionTicks") or 0; paused = p.get("IsPaused")
        nap(2, t0); s, p = ps_of(tok); b = p.get("PositionTicks") or 0
        log("PAUSE: IsPaused=%s position moved %.2fs in 2s" % (paused, (b - a) / 1e7)); result["pause"] = bool(paused) and abs(b - a) < 3e6
        api("POST", "/Sessions/%s/Playing/Unpause" % sid, tok)
        nap(3, t0); s, p = ps_of(tok); c = p.get("PositionTicks") or 0
        log("RESUME: IsPaused=%s position moved %.2fs in ~3s" % (p.get("IsPaused"), (c - b) / 1e7)); result["resume"] = (not p.get("IsPaused")) and c > b
        # --- seek
        api("POST", "/Sessions/%s/Playing/Seek" % sid, tok, params={"seekPositionTicks": 120000000})
        nap(3, t0); s, p = ps_of(tok); d = (p.get("PositionTicks") or 0) / 1e7
        log("SEEK to 12.0s: reported position %.1fs" % d); result["seek"] = 11.5 <= d <= 16
        # --- audio track selection
        api("POST", "/Sessions/%s/Command" % sid, tok, json={"Name": "SetAudioStreamIndex", "Arguments": {"Index": "3"}})
        nap(2, t0); s, p = ps_of(tok); log("AUDIO select 3 (spa): reported AudioStreamIndex=%s" % p.get("AudioStreamIndex")); result["audio_switch"] = p.get("AudioStreamIndex") == 3
        # --- subtitles: embedded VobSub (bitmap) then external SRT then off
        api("POST", "/Sessions/%s/Playing/Seek" % sid, tok, params={"seekPositionTicks": 150000000}); nap(1, t0)
        api("POST", "/Sessions/%s/Command" % sid, tok, json={"Name": "SetSubtitleStreamIndex", "Arguments": {"Index": "4"}})
        nap(1.5, t0); s, p = ps_of(tok); log("SUBTITLE select 4 (VobSub): reported SubtitleStreamIndex=%s Pos=%.1fs" % (p.get("SubtitleStreamIndex"), (p.get("PositionTicks") or 0) / 1e7))
        result["vobsub_selected"] = p.get("SubtitleStreamIndex") == 4
        shot("c3_vobsub_cue3.png")
        api("POST", "/Sessions/%s/Playing/Seek" % sid, tok, params={"seekPositionTicks": 110000000}); nap(1, t0)
        api("POST", "/Sessions/%s/Command" % sid, tok, json={"Name": "SetSubtitleStreamIndex", "Arguments": {"Index": "0"}})
        nap(1.5, t0); s, p = ps_of(tok); log("SUBTITLE select 0 (external SRT): reported SubtitleStreamIndex=%s Pos=%.1fs" % (p.get("SubtitleStreamIndex"), (p.get("PositionTicks") or 0) / 1e7))
        result["srt_selected"] = p.get("SubtitleStreamIndex") == 0
        shot("c4_srt_cueB.png")
        s, p = ps_of(tok)
        log("final play state: PlayMethod=%s TranscodingInfo=%s" % (p.get("PlayMethod"), s.get("TranscodingInfo")))
        # --- stop
        api("POST", "/Sessions/%s/Playing/Stop" % sid, tok)
        wait_for(lambda: not (ps_of(tok)[0] or {}).get("NowPlayingItem"), "playback stops", 20, t0=t0)
        log("STOP: NowPlayingItem cleared on the server"); result["stop"] = True
        ud = api("GET", "/Users/%s/Items/%s" % (viewer_id, item_id), tok).json().get("UserData", {})
        log("server-side progress after stop: PlaybackPositionTicks=%s (%.1fs) PlayCount=%s Played=%s" % (ud.get("PlaybackPositionTicks"), (ud.get("PlaybackPositionTicks") or 0) / 1e7, ud.get("PlayCount"), ud.get("Played")))
        result["progress_reported"] = bool(ud.get("PlaybackPositionTicks"))
        # --- server evidence of any transcode
        tl = sorted(_glob.glob(os.path.join(D, "log", "FFmpeg*")))
        log("FFmpeg transcode logs on the server:", [os.path.basename(x) for x in tl] or "none")
        ff = subprocess.run(["pgrep", "-c", "-x", "ffmpeg"], capture_output=True, text=True).stdout.strip()
        log("ffmpeg processes alive now:", ff)
        nap(1, t0)
    finally:
        log("--- client quit")
        subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "stop"], cwd=REPO, capture_output=True, timeout=30)
        try:
            procs["client"].wait(15); log("client exited rc", procs["client"].returncode)
        except subprocess.TimeoutExpired:
            log("client did not exit after `stop`")
        raw = open(os.path.join(OUT, "client.log.raw"), errors="replace", encoding="utf-8").read()
        open(os.path.join(OUT, "client.log"), "w", encoding="utf-8").write(redact(raw))
        log("results:", json.dumps({k: (v if not isinstance(v, dict) else "dict") for k, v in result.items()}))
    return 0 if all(result.get(k) for k in ("login", "sustained", "pause", "resume", "seek", "stop")) else 1


# ---------------------------------------------------------------------------------------------
# Real-desktop mode (JH_JF_DESKTOP=1): normal GPU output (no vo override), credentials added with the
# non-interactive `add` command, properties read and window screenshots taken over mpv's own IPC
# socket (xdotool cannot drive or capture Wayland windows, and desktop screenshots would capture
# other windows). Deinterlace conditions are set by writing mpv's `deinterlace` property, which is
# what the app itself writes; the HUD gear toggle is NOT exercised here.
import socket as _socket

class Ipc:
    def __init__(self, path):
        self.path, self.n, self.sock, self.buf = path, 0, None, b""
    def connect(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.sock = _socket.socket(_socket.AF_UNIX); self.sock.settimeout(5); self.sock.connect(self.path); return True
            except OSError:
                time.sleep(0.5)
        return False
    def call(self, *cmd):
        self.n += 1; rid = self.n
        self.sock.sendall((json.dumps({"command": list(cmd), "request_id": rid}) + "\n").encode())
        while True:
            while b"\n" not in self.buf:
                self.buf += self.sock.recv(65536)
            line, self.buf = self.buf.split(b"\n", 1)
            m = json.loads(line)
            if m.get("request_id") == rid:
                return m
    def get(self, prop):
        m = self.call("get_property", prop)
        return m.get("data") if m.get("error") == "success" else None
    def set(self, prop, val):
        return self.call("set_property", prop, val).get("error")

PROPS = ["time-pos", "deinterlace", "vf", "video-frame-info", "frame-drop-count", "decoder-frame-drop-count",
         "vo-delayed-frame-count", "estimated-vf-fps", "estimated-display-fps"]

def desktop_phase(tok, user_pw, item_id, viewer_id, t0):
    cfg = os.path.join(D, "client-cfg"); os.makedirs(cfg)
    sockp = os.path.join(D, "mpv.sock")
    # ONLY an IPC socket: no `vo`, no gpu options -- the application's normal output settings.
    open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("input-ipc-server=%s\n" % sockp)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    # Seed the saved login through the real GUI form in a PRIVATE Xvfb (never on the desktop): `add` is
    # not one-shot (it is a normal launch) and xdotool cannot drive a Wayland window.
    xdisp = ":98"
    xv = subprocess.Popen(["Xvfb", xdisp, "-screen", "0", "1280x720x24", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    procs["xvfb"] = xv
    try:
        wait_for(lambda: os.path.exists("/tmp/.X11-unix/X98"), "private Xvfb up", 15, 0.3)
        open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("vo=x11\n")   # seeding only; replaced below
        senv = {k: v for k, v in env.items() if k != "WAYLAND_DISPLAY"}; senv["DISPLAY"] = xdisp
        seed = subprocess.Popen([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "--debug"], cwd=REPO, env=senv,
                                stdout=open(os.path.join(OUT, "seed.log.raw"), "w", encoding="utf-8"), stderr=subprocess.STDOUT, start_new_session=True)
        procs["seed"] = seed
        wait_for(lambda: "showing login" in open(os.path.join(OUT, "seed.log.raw"), encoding="utf-8", errors="replace").read(), "seed login screen up", 40, 0.5, t0=t0)
        nap(1.5, t0)
        def click(x, y):
            subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True, env=senv); time.sleep(0.4)
            subprocess.run(["xdotool", "click", "1"], check=True, env=senv); time.sleep(0.4)
        def typ(text):
            subprocess.run(["xdotool", "type", "--delay", "40", text], check=True, env=senv); time.sleep(0.4)
        click(715, 309); typ(BASE); click(715, 357); typ("viewer"); click(715, 405); typ(user_pw); click(851, 458)
        wait_for(lambda: session(tok), "seed client logs in", 40, t0=t0)
        log("login seeded through the GUI form in a private Xvfb")
        subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "stop"], cwd=REPO, capture_output=True, timeout=30, env=senv)
        seed.wait(20)
        wait_for(lambda: not session(tok), "seed session gone", 15, 0.5, t0=t0)
    finally:
        for k in ("seed", "xvfb"):
            pr = procs.get(k)
            if pr and pr.poll() is None:
                os.killpg(pr.pid, signal.SIGTERM)
                try:
                    pr.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(pr.pid, signal.SIGKILL)
    # the real run: ONLY an IPC socket in mpv.conf -- no `vo`, no gpu options
    open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("input-ipc-server=%s\n" % sockp)
    cmd = [sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "--debug", "--mpv-loglevel", "info"]
    clog = open(os.path.join(OUT, "client.log.raw"), "w", encoding="utf-8")
    procs["client"] = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=clog, stderr=subprocess.STDOUT, start_new_session=True)
    log("client pid", procs["client"].pid, "(ONE window on the real desktop, normal GPU settings)")
    ipc = Ipc(sockp); result = {}; metrics = {}
    quit_dur = None
    try:
        sess = wait_for(lambda: session(tok), "client registers a controllable session", 60, t0=t0)
        log("client session:", sess["Client"], sess.get("ApplicationVersion"), "user", sess["UserName"])
        if not ipc.connect(20):
            raise RuntimeError("mpv IPC socket never appeared (input-ipc-server not honoured?)")
        sid = sess["Id"]
        def play():
            api("POST", "/Sessions/%s/Playing" % sid, tok, params={"playCommand": "PlayNow", "itemIds": item_id, "startPositionTicks": 0})
            wait_for(lambda: (ps_of(tok)[1] or {}).get("PositionTicks") is not None and (ps_of(tok)[0] or {}).get("NowPlayingItem"), "playback starts", 40, t0=t0)
        play(); nap(2, t0)
        vo = ipc.get("current-vo")
        info = {k: ipc.get(k) for k in ("current-vo", "current-gpu-context", "gpu-api", "hwdec", "hwdec-current", "hwdec-interop",
                                        "video-codec", "audio-codec-name", "container-fps", "display-fps", "fullscreen", "osd-dimensions/w", "osd-dimensions/h")}
        info["video-params"] = {k: ipc.get("video-params/" + k) for k in ("pixelformat", "w", "h", "dw", "dh", "aspect", "par", "colormatrix")}
        log("VIDEO PIPELINE:", json.dumps(info, sort_keys=True)); metrics["pipeline"] = info
        if not vo:
            first = [l for l in open(os.path.join(OUT, "client.log.raw"), encoding="utf-8", errors="replace") if "ERROR" in l or "Failed" in l][:6]
            log("GPU/VO did not start. First errors:", redact(" | ".join(x.strip() for x in first)))
            raise RuntimeError("no video output; NOT falling back to another output")
        s, p = ps_of(tok)
        log("server view: PlayMethod=%s TranscodingInfo=%s Sub=%s Audio=%s" % (p.get("PlayMethod"), s.get("TranscodingInfo"), p.get("SubtitleStreamIndex"), p.get("AudioStreamIndex")))
        conds = [("A app-default", None, None), ("B deinterlace=auto", "auto", None), ("C deinterlace=yes", "yes", None),
                 ("D inspect: app-default + VobSub", None, 4), ("E inspect: deinterlace=yes + VobSub", "yes", 4)]
        for ci, (label, deint, sub) in enumerate(conds):
            if ci:
                play(); nap(1.5, t0)
            if sub is not None:
                api("POST", "/Sessions/%s/Command" % sid, tok, json={"Name": "SetSubtitleStreamIndex", "Arguments": {"Index": str(sub)}})
            if deint:
                log("   set mpv deinterlace ->", deint, "(", ipc.set("deinterlace", deint), ")")
            nap(1, t0)
            rows, lat = [], []
            shots = ["%s_%d_%s.png" % ("desk", ci, k) for k in ("a", "b")]
            span = 18 if ci >= 3 else 11
            for i in range(span):
                a = time.time(); row = {k: ipc.get(k) for k in PROPS}; lat.append((time.time() - a) / len(PROPS) * 1000)
                rows.append(row)
                if i == 2 or i == (8 if ci < 3 else 14):
                    ipc.call("screenshot-to-file", os.path.join(OUT, shots[0 if i == 2 else 1]), "window")
                sample(t0); time.sleep(1)
            last = rows[-1]
            vfs = sorted({",".join(f.get("name", "?") for f in (r_["vf"] or [])) for r_ in rows})
            fi = [r_["video-frame-info"] for r_ in rows if r_["video-frame-info"]]
            log("COND %-36s deinterlace=%s vf=%s | interlaced-flag-seen=%s tff=%s | drops: vo=%s decoder=%s delayed=%s | est vf fps=%s display fps=%s | IPC latency avg %.1f ms max %.1f ms"
                % (label, last["deinterlace"], vfs, sorted({x.get("interlaced") for x in fi}), sorted({x.get("tff") for x in fi}),
                   last["frame-drop-count"], last["decoder-frame-drop-count"], last["vo-delayed-frame-count"],
                   last["estimated-vf-fps"], last["estimated-display-fps"], sum(lat) / len(lat), max(lat)))
            metrics[label] = {"last": last, "vf_chains": vfs, "ipc_ms_avg": sum(lat) / len(lat), "ipc_ms_max": max(lat)}
            api("POST", "/Sessions/%s/Playing/Stop" % sid, tok); nap(2, t0)
        result["ran"] = True
        json.dump(metrics, open(os.path.join(OUT, "desktop_metrics.json"), "w", encoding="utf-8"), indent=1, default=str)
    finally:
        log("--- client quit (timed)")
        q0 = time.time()
        subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "stop"], cwd=REPO, capture_output=True, timeout=30)
        try:
            procs["client"].wait(20); quit_dur = time.time() - q0; log("client exited rc %s, %.1fs after `stop`" % (procs["client"].returncode, quit_dur))
        except subprocess.TimeoutExpired:
            log("client did not exit within 20s after `stop`")
        raw = open(os.path.join(OUT, "client.log.raw"), encoding="utf-8", errors="replace").read()
        open(os.path.join(OUT, "client.log"), "w", encoding="utf-8").write(redact(raw))
    return 0 if result.get("ran") else 1


def client_phase(tok, user_pw, item_id, viewer_id, t0):
    if os.environ.get("JH_JF_MODE") == "session":
        return session_phase(tok, user_pw, item_id, viewer_id, t0)
    if os.environ.get("JH_JF_MODE") == "scroll":
        return scroll_phase(tok, user_pw, item_id, viewer_id, t0)
    if os.environ.get("JH_JF_DESKTOP") == "1":
        return desktop_phase(tok, user_pw, item_id, viewer_id, t0)
    return basic_phase(tok, user_pw, item_id, viewer_id, t0)


# ---------------------------------------------------------------------------------------------
# Interactive session (JH_JF_MODE=session, run inside xvfb-run like the basic mode): server, client and
# the virtual display stay up while commands are read from JH_JF_CMDS (one per line) and results are
# appended to OUT/session.out. For driving the real UI (HUD gear menu, Settings) and reading what
# reached mpv (IPC) and the saved config. Bounded by the scope's timeout and an idle limit.
def session_phase(tok, user_pw, item_id, viewer_id, t0):
    cfg = os.path.join(D, "client-cfg"); os.makedirs(cfg)
    sockp = os.path.join(D, "mpv.sock")
    open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("vo=x11\ninput-ipc-server=%s\n" % sockp)  # Xvfb workaround
    cmdf = os.environ["JH_JF_CMDS"]; outf = os.path.join(OUT, "session.out")
    open(cmdf, "w", encoding="utf-8").close(); open(outf, "w", encoding="utf-8").close()
    def say(*a):
        with open(outf, "a", encoding="utf-8") as fh:
            fh.write(redact(" ".join(str(x) for x in a)) + "\n")
    state = {"proc": None}
    def launch():
        raw = open(os.path.join(OUT, "client.log.raw"), "a", encoding="utf-8")
        state["proc"] = subprocess.Popen([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "--debug"], cwd=REPO,
                                         env=dict(os.environ, PYTHONUNBUFFERED="1"), stdout=raw, stderr=subprocess.STDOUT, start_new_session=True)
        procs["client"] = state["proc"]
    def gui_login():
        wait_for(lambda: "showing login" in open(os.path.join(OUT, "client.log.raw"), encoding="utf-8", errors="replace").read(), "login screen", 40, 0.5, t0=t0)
        nap(1.5, t0)
        def click(x, y):
            subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True); time.sleep(0.4)
            subprocess.run(["xdotool", "click", "1"], check=True); time.sleep(0.4)
        click(715, 309); subprocess.run(["xdotool", "type", "--delay", "40", BASE], check=True); time.sleep(0.4)
        click(715, 357); subprocess.run(["xdotool", "type", "--delay", "40", "viewer"], check=True); time.sleep(0.4)
        click(715, 405); subprocess.run(["xdotool", "type", "--delay", "40", user_pw], check=True); time.sleep(0.4)
        click(851, 458)
    launch(); gui_login()
    sess = wait_for(lambda: session(tok), "client session", 60, t0=t0)
    sid = sess["Id"]; ipc = Ipc(sockp); ok = ipc.connect(20)
    say("READY session=%s ipc=%s config_dir=%s" % (sid, ok, cfg))
    done, last_cmd, finished = 0, time.time(), False
    while not finished and time.time() - last_cmd < 240:
        lines = open(cmdf, encoding="utf-8").read().splitlines()
        if len(lines) <= done:
            sample(t0); time.sleep(0.5); continue
        for ln in lines[done:]:
            done += 1; last_cmd = time.time()
            parts = ln.strip().split(" ", 1); c = parts[0]; arg = parts[1] if len(parts) > 1 else ""
            try:
                if c == "quit":
                    say("OK quit"); finished = True; break
                elif c == "shot":
                    shot(arg or "session.png"); say("OK shot", arg)
                elif c == "move":
                    x, y = arg.split(); subprocess.run(["xdotool", "mousemove", x, y], check=True); say("OK move", arg)
                elif c == "click":
                    x, y = arg.split(); subprocess.run(["xdotool", "mousemove", x, y], check=True); time.sleep(0.3)
                    subprocess.run(["xdotool", "click", "1"], check=True); say("OK click", arg)
                elif c == "key":
                    subprocess.run(["xdotool", "key", arg], check=True); say("OK key", arg)
                elif c == "play":
                    api("POST", "/Sessions/%s/Playing" % sid, tok, params={"playCommand": "PlayNow", "itemIds": item_id, "startPositionTicks": 0}); say("OK play")
                elif c == "pause":
                    api("POST", "/Sessions/%s/Playing/Pause" % sid, tok); say("OK pause")
                elif c == "unpause":
                    api("POST", "/Sessions/%s/Playing/Unpause" % sid, tok); say("OK unpause")
                elif c == "stop":
                    api("POST", "/Sessions/%s/Playing/Stop" % sid, tok); say("OK stop")
                elif c == "ipc":
                    say("IPC", arg, "=", json.dumps(ipc.get(arg)))
                elif c == "conf":
                    p = os.path.join(cfg, "conf.json")
                    d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
                    say("CONF", "no conf.json yet" if d is None else json.dumps({k: d.get(k) for k in arg.split()}))
                elif c == "restart":      # graceful stop, then relaunch the SAME config (login is saved)
                    subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "stop"], cwd=REPO, capture_output=True, timeout=30)
                    state["proc"].wait(30); say("client stopped rc", state["proc"].returncode)
                    wait_for(lambda: not session(tok), "old session gone", 20, 0.5, t0=t0)
                    launch(); sess = wait_for(lambda: session(tok), "client session again", 60, t0=t0); sid = sess["Id"]
                    ipc = Ipc(sockp); say("OK restart; ipc", ipc.connect(20), "(login was reused from the saved config)")
                else:
                    say("ERR unknown command", c)
            except Exception as e:
                say("ERR", c, type(e).__name__, str(e)[:120])
    say("SESSION END")
    subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "stop"], cwd=REPO, capture_output=True, timeout=30)
    try:
        state["proc"].wait(20)
    except subprocess.TimeoutExpired:
        pass
    raw = open(os.path.join(OUT, "client.log.raw"), encoding="utf-8", errors="replace").read()
    open(os.path.join(OUT, "client.log"), "w", encoding="utf-8").write(redact(raw))
    return 0



# ---------------------------------------------------------------------------------------------
# Hold-for-scrolling mode (JH_JF_MODE=scroll): the login is seeded in a private Xvfb exactly as in desktop_phase,
# then ONE window of the application named by JH_JF_APP (a launcher such as dist/local-test/launch.sh) opens on the
# real desktop with normal rendering settings and stays until the file JH_JF_FLAG exists, the window is closed, or
# the scope's timeout. Extra env (e.g. JH_SCROLL_TRACE) is inherited. JH_JF_EXPECT = movies to wait for in the scan.
def scroll_phase(tok, user_pw, item_id, viewer_id, t0):
    cfg = os.path.join(D, "client-cfg"); os.makedirs(cfg)
    app = os.environ["JH_JF_APP"]; flag = os.environ["JH_JF_FLAG"]
    expect = int(os.environ.get("JH_JF_EXPECT", "1"))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    def scanned():
        items = api("GET", "/Items", tok, params={"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "PrimaryImageAspectRatio"}).json()["Items"]
        return items if len(items) >= expect and all("Primary" in (i.get("ImageTags") or {}) for i in items) else None
    items = wait_for(scanned, "library scan finds %d movies with posters" % expect, 240, 2.0, t0=t0)
    log("library ready: %d movies, all with a Primary image" % len(items))
    xdisp = ":98"
    xv = subprocess.Popen(["Xvfb", xdisp, "-screen", "0", "1280x720x24", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    procs["xvfb"] = xv
    try:
        wait_for(lambda: os.path.exists("/tmp/.X11-unix/X98"), "private Xvfb up", 15, 0.3)
        open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("vo=x11\n")   # seeding only
        senv = {k: v for k, v in env.items() if k not in ("WAYLAND_DISPLAY", "JH_SCROLL_TRACE")}; senv["DISPLAY"] = xdisp
        seed = subprocess.Popen([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg], cwd=REPO, env=senv,
                                stdout=open(os.path.join(OUT, "seed.log.raw"), "w", encoding="utf-8"), stderr=subprocess.STDOUT, start_new_session=True)
        procs["seed"] = seed
        wait_for(lambda: "showing login" in open(os.path.join(OUT, "seed.log.raw"), encoding="utf-8", errors="replace").read(), "seed login screen up", 40, 0.5, t0=t0)
        nap(1.5, t0)
        def click(x, y):
            subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True, env=senv); time.sleep(0.4)
            subprocess.run(["xdotool", "click", "1"], check=True, env=senv); time.sleep(0.4)
        def typ(text):
            subprocess.run(["xdotool", "type", "--delay", "40", text], check=True, env=senv); time.sleep(0.4)
        click(715, 309); typ(BASE); click(715, 357); typ("viewer"); click(715, 405); typ(user_pw); click(851, 458)
        wait_for(lambda: session(tok), "seed client logs in", 40, t0=t0)
        log("login seeded through the GUI form in a private Xvfb")
        subprocess.run([sys.executable, os.path.join(REPO, "run.py"), "--config", cfg, "stop"], cwd=REPO, capture_output=True, timeout=30, env=senv)
        seed.wait(20)
        wait_for(lambda: not session(tok), "seed session gone", 15, 0.5, t0=t0)
    finally:
        for k in ("seed", "xvfb"):
            pr = procs.get(k)
            if pr and pr.poll() is None:
                os.killpg(pr.pid, signal.SIGTERM)
                try:
                    pr.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(pr.pid, signal.SIGKILL)
    open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8").write("")     # normal rendering: nothing overridden
    clog = open(os.path.join(OUT, "client.log.raw"), "w", encoding="utf-8")
    procs["client"] = subprocess.Popen([app, "--config", cfg, "--mpv-loglevel", "info"], cwd=REPO, env=env, stdout=clog,
                                       stderr=subprocess.STDOUT, start_new_session=True)
    log("client pid", procs["client"].pid, "(ONE window on the real desktop, normal rendering settings)")
    if os.path.exists(flag): os.unlink(flag)
    wait_for(lambda: session(tok), "client registers", 60, t0=t0)
    log("READY: window is up; waiting for the flag file %s" % flag)
    try:
        while not os.path.exists(flag) and procs["client"].poll() is None:
            sample(t0); time.sleep(2.0)
    finally:
        pr = procs["client"]
        if pr.poll() is None:
            s0 = time.time()
            subprocess.run([app, "--config", cfg, "stop"], capture_output=True, timeout=30, env=env)
            try:
                pr.wait(20); log("client exited rc", pr.returncode, "%.2fs after stop" % (time.time() - s0))
            except subprocess.TimeoutExpired:
                os.killpg(pr.pid, signal.SIGTERM); log("client had to be terminated")
        else:
            log("client exited by itself (window closed) rc", pr.returncode)
    return 0
