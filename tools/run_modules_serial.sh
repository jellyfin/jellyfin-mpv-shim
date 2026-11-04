#!/usr/bin/env bash
# One test module per fresh subprocess, one at a time, each in its OWN limited
# scope (tools/limited_run.sh: 3 GiB, no swap, timeout, headroom watchdog, and
# a stop if more than 2 mpv processes appear). Exactly one worker.
#
#   JH_LOG_DIR=/some/dir tools/run_modules_serial.sh test_gamepad test_keysweep
#
# Hazards stop the sequence at once (no retry, nothing further started):
#   exit 75/76 headroom, 78 >2 players, 79 processes left in the scope,
#   124/137/143 timeout, or a test failure that is NOT in the baseline file.
# Known baseline failures (tools/known_baseline_failures.txt, matched by the full
# "FAIL: test (module.Class.test)" line, never by a count) are recorded and do
# not block independent modules, but the final exit status is still 1.
# Other module failures are recorded, the run continues, and the final status
# is 1 as well. The final status is 0 only if every module passed outright.
#
# Complete output of every module (sanitised: tokens/keys redacted) is kept in
# $JH_LOG_DIR/<module>.log together with the exit status and per-second
# samples; nothing is filtered out of those files.
# Needs Xvfb (xvfb-run) so no window reaches the desktop. WAYLAND_DISPLAY is unset for the child ON PURPOSE:
# with it inherited, mpv prefers the real Wayland compositor over the virtual X display and the "hidden"
# test windows open on the user's desktop (found 2026-11-04).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
TMO="${JH_MODULE_TIMEOUT:-120}"
LOGDIR="${JH_LOG_DIR:-$(mktemp -d)}"
BASELINE="$HERE/known_baseline_failures.txt"
mkdir -p "$LOGDIR"
cd "$ROOT" || exit 64
[ $# -gt 0 ] || { echo "usage: $0 test_module [test_module ...]" >&2; exit 64; }
echo "logs: $LOGDIR   per-module timeout: ${TMO}s   (not raised automatically)"

sanitise() {
    sed -E 's/((api_?key|access_?token|x-mediabrowser-token|token|password|pw)[=:"'"'"' ]+)[^&" '"'"',}]+/\1REDACTED/Ig'
}

overall=0
for m in "$@"; do
    m="${m%.py}"
    echo "=== $m"
    before=$(pgrep -xc mpv || true)
    samples="$LOGDIR/$m.samples"
    t0=$(date +%s)
    JH_MAX_MPV=2 JH_KEEP_SAMPLES="$samples" "$HERE/limited_run.sh" -t "$TMO" -- \
        env -u WAYLAND_DISPLAY -u WAYLAND_SOCKET \
        xvfb-run -a -s "-screen 0 1280x720x24 -nolisten tcp" \
        python3 "$HERE/run_tests_parallel.py" --worker "$m.py" 2>&1 \
        | sanitise > "$LOGDIR/$m.log"
    rc=${PIPESTATUS[0]}
    dur=$(( $(date +%s) - t0 ))
    echo "exit_status=$rc duration=${dur}s" >> "$LOGDIR/$m.log"

    # Everything that is not a traceback body is shown as-is.
    grep -E '^(JMS-RESULT|PLAYER-TEARDOWN|limited_run|exit_status|Ran |OK|FAILED|FAIL:|ERROR:)' "$LOGDIR/$m.log"
    backend=$(sed -n 's/.*backend=\([a-z]*\).*/\1/p' "$LOGDIR/$m.log" | head -1)
    echo "RESULT $m: rc=$rc duration=${dur}s backend=${backend:-n/a} host-mpv before=$before after=$(pgrep -xc mpv || true) (host count includes any mpv not ours)"

    case "$rc" in
        0) ;;
        1)  # tests failed: separate documented baseline from anything new
            new=0
            while IFS= read -r line; do
                if [ -f "$BASELINE" ] && grep -qxF -- "$line" "$BASELINE"; then
                    echo "  baseline: $line"
                else
                    echo "  NEW FAILURE: $line"; new=1
                fi
            done < <(grep -E '^(FAIL|ERROR): ' "$LOGDIR/$m.log")
            overall=1
            if ! grep -qE '^(FAIL|ERROR): ' "$LOGDIR/$m.log"; then
                echo "  NEW FAILURE: exit 1 with no FAIL/ERROR lines (crash or import failure?)"; new=1
            fi
            if [ "$new" = 1 ]; then
                echo "STOP: $m has failures that are not in the baseline. Full log: $LOGDIR/$m.log"; exit 1
            fi ;;
        *)  echo "STOP: $m ended with rc=$rc (75 headroom, 76 watchdog, 78 >2 players, 79 cleanup failed, 124/137/143 timeout). Not continuing, not retrying. Full log: $LOGDIR/$m.log"
            exit "$rc" ;;
    esac
done
exit "$overall"
