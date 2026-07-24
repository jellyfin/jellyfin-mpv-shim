#!/bin/bash
# Full-union coverage across every leg that can be measured in-process.
#
# Why a driver script: no single interpreter sees all of the code. player.py
# picks its mpv backend AT IMPORT TIME and wires module-level singletons, so
# run_integration.py gives each backend its own process — and a coverage run
# inside one of those processes only ever sees one backend's branches. This
# runs each measurable leg separately and unions the results, which is the
# only honest total.
#
#   tools/coverage_all.sh                 # report to stdout
#   tools/coverage_all.sh --sort=missing  # extra args go to the merge step
#
# Not covered by this and deliberately so: the tray (needs a real session
# bus), the Windows-only paths, and mpv_shim.main (the process entry point).
set -u

cd "$(dirname "$0")/.." || exit 1
OUT="${JMS_COV_DIR:-$(mktemp -d)}"
mkdir -p "$OUT"

run() {
    local label="$1"; shift
    printf '  %-34s' "$label"
    if "$@" >"$OUT/$label.log" 2>&1; then
        echo "ok"
    else
        echo "FAILED (see $OUT/$label.log)"
    fi
}

echo "collecting coverage into $OUT"
run unit python3 tools/coverage_report.py --integration --json "$OUT/unit.json"

# The fake-mpv legs: one process per backend, matching run_integration.py.
for backend in libmpv jsonipc; do
    run "fake-$backend" env JMS_TEST_BACKEND="$backend" python3 tools/coverage_report.py \
        --modules tests.integration.test_player_state_machine \
                  tests.integration.test_lifecycle \
                  tests.integration.test_keyboard_controls \
                  tests.integration.test_mpv_lifecycle \
        --json "$OUT/fake-$backend.json"
done

# The real-mpv legs. xvfb because a bare run throws ~25 windows onto the
# desktop; they self-skip if mpv/ffmpeg are missing, so a bare machine still
# produces a (smaller) report rather than an error.
XVFB=""
command -v xvfb-run >/dev/null && XVFB="xvfb-run -a"
run real-libmpv env JMS_TEST_BACKEND=libmpv $XVFB python3 tools/coverage_report.py \
    --modules tests.integration.test_realmpv_smoke \
              tests.integration.test_mpvtk_browser \
              tests.integration.test_mpvtk_hud \
              tests.integration.test_mpvtk_auth \
              tests.integration.test_e2e_offline \
    --json "$OUT/real-libmpv.json"

echo
python3 tools/coverage_report.py --merge "$OUT"/*.json --json "$OUT/merged.json" "$@"
echo
echo "per-leg and merged JSON in $OUT"
echo "drill into one file:  tools/coverage_report.py --merge $OUT/merged.json --functions mpvtk_browser/ui.py"
