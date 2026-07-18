#!/bin/bash
# Headless test harness for the shim's OSC lua scripts.
#
# Drives the real `mpv` binary under Xvfb with REAL X11 input (xdotool)
# and the JSON IPC socket for state assertions. Because it exercises the
# external mpv binary via --script/--input-ipc-server, the lua-side
# behavior it validates is exactly what the mpv_ext (jsonipc) backend
# runs; the libmpv backend loads the same scripts into the same player
# core. (Python<->lua integration across both shim backends is covered
# by tests/integration/test_realmpv_smoke.py instead.)
#
# Usage: tools/osc-test/osc-test.sh <scenario> [args]
#   shot [out.png] [sheet]  screenshot the OSC (optionally with the
#                           sub|audio|settings sheet open) using fake
#                           shim state
#   clicks                  click routing: video-click pause, buttons,
#                           bar dead space, back button
#   restart                 idle -> new file cycle, then click routing
#                           (regression: section re-raise bug)
#   flicker                 trickplay overlay churn while hovering the
#                           seekbar paused (regression: volumebar clear)
#
# Env: OSC_SCRIPT to test a different script (default trickplay-jf-osc),
#      DISPLAY_NUM to pin the Xvfb display (default :77), KEEP_LOG=1 to
#      print the mpv log path instead of deleting it.
#
# Requires: mpv, Xvfb, xdotool, socat, python3.

set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OSC_SCRIPT="${OSC_SCRIPT:-$REPO/jellyfin_mpv_shim/trickplay-jf-osc.lua}"
THUMBFAST="$REPO/jellyfin_mpv_shim/thumbfast.lua"
D="${DISPLAY_NUM:-:77}"
WORK="$(mktemp -d /tmp/jms-osc-test-XXXXXX)"
SOCK="$WORK/mpv.sock"
LOG="$WORK/mpv.log"
SCENARIO="${1:-shot}"

MPV_PID=""
XVFB_PID=""
cleanup() {
    [ -n "$MPV_PID" ] && kill "$MPV_PID" 2>/dev/null || true
    [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    if [ "${KEEP_LOG:-0}" = "1" ]; then
        echo "log: $LOG"
    else
        rm -rf "$WORK"
    fi
}
trap cleanup EXIT

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 2; }; }
need mpv; need Xvfb; need xdotool; need socat; need python3

start_mpv() {  # extra mpv args after $@
    Xvfb "$D" -screen 0 1280x720x24 &
    XVFB_PID=$!
    sleep 1
    DISPLAY="$D" JMS_JF_OSC_DEBUG=1 mpv --no-config --osc=no --no-border \
        --script-opts=osc-visibility=always \
        --script="$OSC_SCRIPT" \
        --ao=null --geometry=1280x720 --input-ipc-server="$SOCK" \
        --msg-level=trickplay_jf_osc=info,thumbfast=info --msg-time \
        "$@" \
        "av://lavfi:testsrc2=duration=120:size=1280x720:rate=30" \
        > "$LOG" 2>&1 &
    MPV_PID=$!
    sleep 3
}

get() {
    echo '{ "command": ["get_property", "'"$1"'"] }' | socat - "$SOCK" \
        | head -1 | python3 -c "import json,sys; print(json.load(sys.stdin)['data'])"
}
cmd() { echo "$1" | socat - "$SOCK" >/dev/null; }
click() {
    DISPLAY="$D" xdotool mousemove "$1" "$2"
    sleep 0.5
    DISPLAY="$D" xdotool click 1
    sleep 2
}
check() {  # name actual expected
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1 (got $2, expected $3)"
        FAILED=1
    fi
}
FAILED=0

case "$SCENARIO" in
shot)
    OUT="${2:-$PWD/osc-shot.png}"
    SHOT_PATH="$OUT" SHOT_SHEET="${3:-}" start_mpv \
        --script="$HERE/drivers/shot-driver.lua"
    # the driver quits mpv after taking the screenshot
    wait "$MPV_PID" 2>/dev/null || true
    MPV_PID=""
    [ -f "$OUT" ] && echo "PASS screenshot: $OUT" || { echo "FAIL no screenshot"; FAILED=1; }
    ;;
clicks)
    start_mpv
    # warm-up (first synthetic XTEST click can be swallowed at startup)
    click 640 300
    p0=$(get pause)
    click 640 300
    check "video-click toggles pause" "$(get pause)" \
        "$([ "$p0" = True ] && echo False || echo True)"
    p1=$(get pause)
    click 83 694
    check "playpause button toggles" "$(get pause)" \
        "$([ "$p1" = True ] && echo False || echo True)"
    p2=$(get pause)
    click 400 694
    check "bar dead space is inert" "$(get pause)" "$p2"
    click 1243 694
    check "fullscreen button" "$(get fullscreen)" "True"
    ;;
restart)
    start_mpv --idle=yes --end=4
    sleep 5
    check "idle after eof" "$(get idle-active)" "True"
    cmd '{ "command": ["loadfile", "av://lavfi:testsrc2=duration=60:size=1280x720:rate=30"] }'
    sleep 2
    check "playing again" "$(get idle-active)" "False"
    click 640 400   # ensure pointer state settled
    p0=$(get pause)
    click 83 694
    check "playpause after restart" "$(get pause)" \
        "$([ "$p0" = True ] && echo False || echo True)"
    ;;
flicker)
    python3 - "$WORK/fake_bif.bin" <<'EOF'
import sys
colors = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),
          (255,0,255),(128,128,128),(255,128,0),(0,128,255),(200,200,200)]
with open(sys.argv[1], "wb") as f:
    for b, g, r in colors:
        f.write(bytes([b, g, r, 255]) * (160 * 90))
EOF
    BIF_PATH="$WORK/fake_bif.bin" start_mpv \
        --script="$THUMBFAST" --script="$HERE/drivers/bif-driver.lua"
    DISPLAY="$D" xdotool mousemove 640 662   # hover mid-seekbar
    sleep 2
    cmd '{ "command": ["set_property", "pause", true] }'
    sleep 10
    OPS=$(grep -cE "overlay-add|overlay-remove" "$LOG" || true)
    # one add for the hover (plus maybe one frame-boundary re-add)
    if [ "$OPS" -le 3 ]; then
        echo "PASS overlay churn while paused: $OPS ops"
    else
        echo "FAIL overlay churn while paused: $OPS ops (expected <= 3)"
        grep -E "overlay-add|overlay-remove" "$LOG" | head -10
        FAILED=1
    fi
    ;;
*)
    echo "unknown scenario: $SCENARIO (shot|clicks|restart|flicker)" >&2
    exit 2
    ;;
esac

exit "$FAILED"
