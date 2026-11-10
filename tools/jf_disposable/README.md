# Disposable Jellyfin 10.11.8 test server and client drivers

Starts a **throw-away Jellyfin 10.11.8 on 127.0.0.1:18097** with a temporary config, two accounts with random
passwords, and a library you provide, then drives a JellyHome Player client against it. Nothing here touches a real
server, real credentials or real media. All server and client state lives in a temporary directory that is removed at
the end; the sanitised logs and a `samples.csv` of memory use stay in `$JH_JF_WORK/out`.

Needs: Linux with systemd user scopes (`tools/limited_run.sh`), `requests`, Xvfb, xdotool, ImageMagick `import` (the
screenshots of the basic mode), ffmpeg, mpv/libmpv, Pillow (for `make_library.py`) and the repository's Python deps.
Run it inside `tools/limited_run.sh`: the server and the client share the one memory limit.

## Setup

    W=/path/with/600MB; export JH_JF_WORK=$W; mkdir -p $W/server $W/media/Movies $W/dl
    # 1. the exact server release (the archive keeps 10.11.8; the stable listing does not). No publisher
    #    checksum is served; the file used here had sha256
    #    d7fba5f34da1e12c5095b17a9cd053d603aaf514e293973a31851f0885633ca1
    curl -L -o $W/dl/jf.tgz https://repo.jellyfin.org/archive/server/linux/stable/v10.11.8/amd64/jellyfin_10.11.8-amd64.tar.gz
    sha256sum $W/dl/jf.tgz        # compare with the value above before unpacking
    tar xzf $W/dl/jf.tgz -C $W/server --no-same-owner
    # 2. a library, either of these:
    #    a) many posters, for scrolling tests:
    python3 tools/jf_disposable/make_library.py "$W/media/Movies" 120
    #    b) one DVD-style film, for playback tests: `make_vobsub.py $W/vob` writes a genuine VobSub .idx/.sub
    #       drawn from text, then
    #    ffmpeg -f lavfi -i "testsrc2=size=720x576:rate=50,interlace=scan=tff,setsar=64/45" \
    #      -f lavfi -i sine=frequency=440:sample_rate=48000 -f lavfi -i sine=frequency=880:sample_rate=48000 \
    #      -i $W/vob.idx -t 20 -map 0:v -map 1:a -map 2:a -map 3:s -c:v mpeg2video -pix_fmt yuv420p \
    #      -b:v 4M -maxrate 8M -bufsize 1835k -flags +ildct+ilme -aspect 16:9 -r 25 -c:a ac3 -b:a 192k \
    #      -ac 2 -c:s copy -metadata:s:a:0 language=eng -metadata:s:a:1 language=spa \
    #      -metadata:s:s:0 language=eng "$W/media/Movies/SyntheticDVD (2001)/SyntheticDVD (2001).mkv"
    #       (plus a small `.en.srt` beside it for the external-text case)

Without `JH_JF_WORK` the work directory is `tools/jf_disposable/work/` (gitignored).

## Modes

`jf_run.py server` starts the server only, scans the library and reports what Jellyfin's probe found. `jf_run.py full`
adds a client, and the environment picks which kind:

| Mode | Selected by | Where the window opens | What it does |
|---|---|---|---|
| basic | (default) | virtual display (`xvfb-run`) | Logs in through the GUI form with xdotool, plays the first movie by remote control, pause/seek/track switches, screenshots, clean stop. The client's mpv config forces `vo=x11`. |
| session | `JH_JF_MODE=session JH_JF_CMDS=<file>` | virtual display (`xvfb-run`) | Keeps server and client up and executes one command per line appended to the file (`click`, `conf`, `ipc`, `key`, `move`, `pause`, `play`, `quit`, `restart`, `shot`, `stop`, `unpause`); answers go to `out/session.out`. Ends on `quit` or after 240 s without a command. Also forces `vo=x11`. |
| desktop | `JH_JF_DESKTOP=1` | **your real desktop**, one window, normal rendering settings | Seeds the login (below), plays the first movie and reads mpv properties over IPC (video output, decoder, deinterlace state, dropped frames) with window screenshots. |
| scroll | `JH_JF_MODE=scroll JH_JF_APP=<launcher> JH_JF_FLAG=<file> JH_JF_EXPECT=<N>` | **your real desktop**, one window, normal rendering settings | Waits until the library has N movies that all have a primary image, seeds the login, runs `<launcher> --config <dir> --mpv-loglevel info` once, and leaves it for you to use until the flag file exists, you close the window, or the scope times out; then `<launcher> --config <dir> stop`. Environment variables are passed through to the launcher. |

Run the virtual-display modes like this, **with Wayland unset**:

    JH_MAX_MPV=2 tools/limited_run.sh -t 240 -- env -u WAYLAND_DISPLAY xvfb-run -a -s "-screen 0 1280x720x24 -nolisten tcp" \
        python3 tools/jf_disposable/jf_run.py full

With `WAYLAND_DISPLAY` set, mpv draws on your real Wayland desktop instead of the virtual display.

## Things to know

* **`vo=x11` is a workaround, not a recommendation.** On an isolated Xvfb mpv refuses software rendering, so its GPU
  output cannot start. The basic and session modes sidestep that by forcing `vo=x11`; the alternative that keeps the normal
  output is the opt-in `tools/xvfb_gl_shim/`. The desktop and scroll modes use `vo=x11` only for the one-time login
  seeding and then run with no rendering option set.
* **The desktop and scroll modes seed the login through the real sign-in form in a private Xvfb on display `:98`**
  (xdotool cannot drive a Wayland window, and `add` is not a one-shot command). The script does not check that `:98` is
  free; make sure it is.
* The login coordinates in `jf_client.py` assume the 1280x720 sign-in card.
* The accounts are `tmpadmin` and `viewer`; their passwords are random per run and redacted from the saved logs.
* The server is Jellyfin 10.11.8 only, started with `--nowebclient`; the port is fixed (18097).
* **Stopping the server.** The driver asks it to shut down through its API, waits 25 s, sends SIGTERM, waits 20 s more,
  then SIGKILLs it. Jellyfin 10.11 optimizes its database on the way out (its log ends with "Running query planner
  optimizations in the database... This might take a while"), and with a 120-movie library the `server` phase was
  killed in 2 of 2 runs on the development laptop (and in a third that first waited for the whole library to be indexed); earlier `scroll` runs, which had a client connected and had been up
  longer, exited by themselves (rc 0) and I do not know what made the difference. Nothing is lost either way: the
  database is a temporary one and is removed afterwards. The timeouts are the driver's own and were not changed.
* These are test harnesses, not part of the application. They have no unit tests, because they need a real server.

## Per-version chapters (`version_chapters_probe.py`)

A multi-version movie is several items, and each carries only its own file's chapters and chapter images. This probe checks
that the server, the player's scrub previews (`media.Video`) and the Scenes row (`LibrarySource.get_chapters`) agree, version
by version. It builds two small films with ffmpeg (no network) under `$JH_JF_WORK/media_versions`: `VerImg` (chapter-image
extraction on) with versions of 5, 3, 1 and 0 chapters, and `VerPlain` (extraction off) with 3 and 2.

    JH_JF_WORK=$W tools/limited_run.sh -t 600 -- dist/local-test/venv/bin/python tools/jf_disposable/version_chapters_probe.py
    python3 tools/jf_disposable/version_chapters_probe.py --make-media-only      # only writes the test videos

It checks each version's chapter count, names and start times; that tagged chapters serve a picture under the version's own
id, that the pictures differ between versions, and that an image index a version does not have is not served under another
version's id (404); that a lone chapter, a chapterless version and a library with extraction off have no image tags; and that
the player and the Scenes row read the same times, counts and images from the real server. Exit status 0 only if all hold;
problems are printed as `PROBLEM:` lines and everything is in `$JH_JF_WORK/out/version_chapters_probe.json`.

Needs ffmpeg and the venv python (the consumers import `jellyfin_apiclient_python`). It refuses to start a server outside a
`limited_run.sh` scope (`--no-scope-check` overrides that, for the unit tests). Only the throw-away server of `jf_run.py` and
the generated media are involved; the server's temporary state and processes are removed at the end (as above, the server may
need the driver's SIGKILL on the way out, which loses nothing). Chapter images are extracted asynchronously after the scan, so
a run takes a minute or two. Its pure parts (the specification, the checks, the scope guard) are covered by
`tests/test_version_chapters_probe.py`.

## Blinded cadence comparison (`ab_manual.sh`)

A person-driven comparison of the renderer's 30 ms and 16.7 ms frame-cadence floors (`JMS_TICK_MS`, honoured only with
`JMS_FRAME_DIAG`) using the **real** mouse wheel and touchpad on your desktop; no synthetic wheel events are sent.

    tools/jf_disposable/ab_manual.sh plan --pilot --session DIR     # draws and seals the arms; starts nothing
    tools/jf_disposable/ab_manual.sh run --session DIR --dry-run    # checks prerequisites; starts nothing
    tools/jf_disposable/ab_manual.sh run --session DIR              # next run: one window, five guided phases
    tools/jf_disposable/ab_manual.sh validate --session DIR         # is the data complete? (names no tick)
    tools/jf_disposable/ab_manual.sh reveal --session DIR           # only when every run is done and rated

Each run goes through `jf_run.py` scroll mode inside one `tools/limited_run.sh` scope (3 GiB, no swap, `JH_MAX_MPV=1`,
leftover check). `ab_launch.sh` wraps the launcher (`ab_app.sh`: the repository source on the local-test venv, not its older installed copy) to set the diagnostics and an mpv IPC socket for two read-only property reads
(the effective tick, the video output); the run is discarded if the app does not report the assigned tick. Recorded: the
app's frame log, the scope's cgroup CPU and memory (peak and per phase), the app tree's PSS and CPU, the AMD `gpu_busy_percent`
counter where the kernel has one, and queued-event lines of the app log. A pilot is two runs (one per arm, order drawn);
`--full` is A-B-B-A or B-A-A-B. `WAYLAND_DEBUG` is not used.

Limits: frame figures are what the renderer *submitted*, not what reached the screen. The cgroup CPU includes the throw-away
server. The GPU counter is coarse and absent on other vendors. How hard and fast you scroll varies between runs, so
compare distributions across several runs, not single frames. A run that crashes is recorded as failed and not retried.

## Recording real wheel and touchpad input (scroll recorder)

`JMS_SCROLL_DIAG=<file>` (honoured only together with `JMS_FRAME_DIAG=1`, off otherwise) makes `renderer.lua` write one `E`
line per wheel/touchpad event (time, interval, direction, the scale mpv delivered, whether it was treated as a notch `W`
or a fine delta `T`, the stored offset before/after/asked-for, clamp, glide state, snap flags) and one `F` line per frame
while a gesture is live (frame gap, stored and drawn offset, per-frame drawn displacement, glide position). Containers are
numbered `c1..`, never named; memory is a 128-line buffer; the file stops at 100000 lines. The field list is the comment above
`state.scroll_diag_new` in `jellyfin_mpv_shim/mpvtk/renderer.lua`.

    tools/jf_disposable/si_manual.sh run --dry-run             # checks only, starts nothing
    tools/jf_disposable/si_manual.sh run                       # about 4 minutes, opens ONE window on your desktop
    tools/jf_disposable/si_manual.sh report DIR_OR_FILE        # the analysis again

`run` uses the same throw-away server, 120-poster library and `limited_run.sh` scope as the A/B, and guides six phases (slow
wheel 3-6 notches/s, fast wheel, reversals, the ends of the grid, slow and fast touchpad). Nothing is sent to the app and
`WAYLAND_DEBUG` is not used. `si_analyze.py` reports, per phase, the scales actually delivered (including any `>= 0.99`
event within 25 ms of the previous one, which is how a hard touchpad frame taken for a notch would show), and the drawn
motion per frame against the stored offset. It measures what the renderer decided to draw, not what reached the screen.
