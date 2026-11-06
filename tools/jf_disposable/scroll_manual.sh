#!/usr/bin/env bash
# Opt-in Movies-grid scroll comparison on YOUR display (real GPU output). About a minute.
#
#   tools/jf_disposable/scroll_manual.sh [--dry-run] [--config-only] [--yes] [--matrix]
#                                        [--tick MS] [--wheel-ms MS] [--label NAME] [NOTCHES]
#
# Without --matrix/--tick/--wheel-ms/--label this is the single run it always was, reports in
# $JH_JF_WORK/out. For the cadence experiment (does the renderer's 30 ms frame floor pace scrolling?):
#   --tick MS      the app's frame-cadence floor, 5..100 (JMS_TICK_MS inside the app; default: the app's 30)
#   --wheel-ms MS  interval between wheel notches the probe sends, 1..500 (default 30)
#   --label NAME   keep this run in $JH_JF_WORK/out/runs/NAME-<UTC time>/ (never overwritten); implied by
#                  --tick/--wheel-ms as t<tick>_w<wheel>
#   --matrix       the four combinations, one after the other, each in its own scope and run directory:
#                  30/30, 16.7/30, 30/8, 16.7/8 (tick ms / wheel ms). Stops at the first failure.
#   --dry-run      checks every configuration and the prerequisites, starts nothing.
#   --config-only  checks only the configurations (no server, posters, python or display needed).
#
# What it does: starts a throw-away Jellyfin 10.11.8 on 127.0.0.1:18097 with a temporary config and the
# 120-poster library, signs ONE JellyHome in (a temporary config dir that also holds its artwork cache), opens
# the Movies grid in ONE window on your desktop, and sends the same wheel input twice over mpv's own IPC
# (default 90 notches down and back up, 30 ms apart, pointer untouched): first while the posters are arriving, then
# once they are cached. It records the video output actually in use, frame submission gaps, time inside the
# overlay-add calls, artwork timings and peak memory, then prints a plain-language reading.
#
# Limits: everything runs in one systemd user scope (tools/limited_run.sh): 3 GiB memory cap, no swap, a
# 4 GiB-available headroom check, at most one mpv (JH_MAX_MPV=1), a timeout, and a leftover-process check that
# fails the run (exit 79) if anything outlives it. Diagnostics (JMS_FRAME_DIAG, JMS_ART_DIAG) are set only for the
# app this script starts; nothing is changed in your own config, and no real server or library is contacted.
#
# Needs: the server unpacked and the posters made as in tools/jf_disposable/README.md (JH_JF_WORK=...), and a
# Python with the app's dependencies (default dist/local-test/venv; override with JH_PY).
set -u
here="$(cd "$(dirname "$0")" && pwd)"; root="$(dirname "$(dirname "$here")")"
dry=0; yes=0; notches=90; cfgonly=0; matrix=0; tick=""; wheelms=""; label=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry=1 ;;
        --config-only) dry=1; cfgonly=1 ;;
        --yes) yes=1 ;;
        --matrix) matrix=1 ;;
        --tick|--wheel-ms|--label)
            [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 64; }
            case "$1" in --tick) tick="$2" ;; --wheel-ms) wheelms="$2" ;; --label) label="$2" ;; esac
            shift ;;
        ''|*[!0-9]*) echo "unknown argument $1" >&2; exit 64 ;;
        *) notches="$1" ;;
    esac
    shift
done
if [ "$matrix" = 1 ] && [ -n "$tick$wheelms$label" ]; then
    echo "--matrix chooses its own tick, wheel interval and labels" >&2; exit 64
fi
work="${JH_JF_WORK:-$here/work}"; py="${JH_PY:-$root/dist/local-test/venv/bin/python}"
fail() { echo "scroll_manual: $*" >&2; exit 65; }

# The configurations to run: "label tick_ms wheel_ms", tick/wheel empty = the probe's defaults.
configs=()
if [ "$matrix" = 1 ]; then
    configs=("t30_w30 30 30" "t16.7_w30 16.7 30" "t30_w8 30 8" "t16.7_w8 16.7 8")
else
    if [ -z "$label" ] && [ -n "$tick$wheelms" ]; then label="t${tick:-default}_w${wheelms:-30}"; fi
    configs=("$label $tick $wheelms")
fi
# wheel ms -> the probe's seconds
delay_of() { [ -n "$1" ] && python3 -c 'import sys; print("%.6f" % (float(sys.argv[1]) / 1000))' "$1"; }
# Run the probe's own --check for one configuration; prints its resolved settings.
check_cfg() {   # label tick wheelms
    local d=""; if [ -n "$3" ]; then
        case "$3" in ''|*[!0-9.]*) echo "scroll_manual: --wheel-ms $3 is not a number" >&2; return 64 ;; esac
        d=$(delay_of "$3") || return 64
    fi
    env -u JMS_TICK_MS JH_RUN_LABEL="$1" JH_TICK_MS="$2" JH_SCROLL_DELAY="$d" \
        python3 "$here/scroll_probe.py" --check "$notches"
}
for c in "${configs[@]}"; do
    set -- $c
    if [ "$matrix" = 1 ]; then l="$1"; t="$2"; w="$3"; else l="$label"; t="$tick"; w="$wheelms"; fi
    out=$(check_cfg "$l" "$t" "$w") || fail "bad configuration (label '$l', tick '$t', wheel '$w' ms)"
    echo "config ok: $out"
done
[ "$cfgonly" = 1 ] && { echo "(config only: nothing started)"; exit 0; }

[ -x "$work/server/jellyfin/jellyfin" ] || fail "no server at $work/server/jellyfin (set JH_JF_WORK; see README.md)"
n=$(find "$work/media/MoviesGrid" -name poster.jpg 2>/dev/null | wc -l)
[ "$n" -ge 120 ] || fail "need 120 posters in $work/media/MoviesGrid (found $n): python3 tools/jf_disposable/make_library.py"
[ -x "$py" ] || fail "no python at $py (set JH_PY to one with the app's dependencies)"
[ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] || fail "no display: this test is meant for your desktop"
ss -ltn 2>/dev/null | grep -q ':18097 ' && fail "port 18097 is already in use"
echo "ready: server $work/server, $n posters, python $py, display ${WAYLAND_DISPLAY:-$DISPLAY}, $notches notches"
[ "$dry" = 1 ] && { echo "(dry run: ${#configs[@]} configuration(s) checked, nothing started)"; exit 0; }
if [ "$yes" != 1 ]; then
    echo "This opens a JellyHome window on your desktop, ${#configs[@]} time(s) in turn, for about a minute each. Leave the mouse and keyboard alone."
    read -r -p "Go? [y/N] " ans; [ "$ans" = y ] || [ "$ans" = Y ] || { echo "not started"; exit 1; }
fi
final=0; i=0
for c in "${configs[@]}"; do
    set -- $c
    if [ "$matrix" = 1 ]; then l="$1"; t="$2"; w="$3"; else l="$label"; t="$tick"; w="$wheelms"; fi
    i=$((i + 1))
    # Each run starts only when the last one is fully gone: its scope has been checked for leftovers by
    # limited_run (exit 79), and the server port must be free again.
    for _ in 1 2 3 4 5 6 7 8 9 10; do ss -ltn 2>/dev/null | grep -q ':18097 ' || break; sleep 1; done
    ss -ltn 2>/dev/null | grep -q ':18097 ' && { echo "scroll_manual: port 18097 still in use before run $i; stopping" >&2; final=65; break; }
    d=""; [ -n "$w" ] && d=$(delay_of "$w")
    echo "=== run $i of ${#configs[@]}: label '${l:-default}' tick '${t:-30 (default)}' ms, wheel '${w:-30 (default)}' ms ==="
    env -u JMS_TICK_MS JH_JF_WORK="$work" JH_MAX_MPV=1 JH_RUN_LABEL="$l" JH_TICK_MS="$t" JH_SCROLL_DELAY="$d" \
        "$root/tools/limited_run.sh" -t 420 -- "$py" "$here/scroll_probe.py" "$notches"
    rc=$?
    if [ -n "$l" ]; then
        echo "report: $work/out/runs/$l-*/scroll_report.txt   numbers: .../scroll_probe.json   frames: .../frame_log.tsv"
    else
        echo "report: $work/out/scroll_report.txt   numbers: $work/out/scroll_probe.json"
    fi
    if [ "$rc" != 0 ]; then echo "scroll_manual: run $i exited $rc; the remaining runs are not started" >&2; final=$rc; break; fi
done
exit $final
