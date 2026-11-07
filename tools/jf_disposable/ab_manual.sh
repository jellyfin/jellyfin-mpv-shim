#!/usr/bin/env bash
# Blinded 30 ms vs 16.7 ms renderer-cadence comparison with REAL mouse-wheel and touchpad input.
#
#   tools/jf_disposable/ab_manual.sh plan [--pilot|--full] [--session DIR]  create a session (starts nothing)
#   tools/jf_disposable/ab_manual.sh run --session DIR [--dry-run] [--yes]   the next run, for about 3-5 minutes
#   tools/jf_disposable/ab_manual.sh status --session DIR                    which runs have a record
#   tools/jf_disposable/ab_manual.sh validate --session DIR                  checks the data (shows no tick)
#   tools/jf_disposable/ab_manual.sh reveal --session DIR                    ticks + ratings + metrics, once rated
#
# `run` opens ONE JellyHome window on your desktop, against a throw-away Jellyfin on 127.0.0.1:18097 with the
# 120-poster library, and guides you through five phases in the terminal. Which of the two cadence floors the run
# uses is drawn in a sealed key and is not printed, logged or stored with the run. Everything runs in one
# tools/limited_run.sh scope: 3 GiB memory cap, no swap, JH_MAX_MPV=1, a timeout and the leftover-process check
# (exit 79). Run it with the mouse and touchpad you actually use; do not touch anything outside the phases.
#
# WAYLAND_DEBUG is NOT enabled here, and is not part of this procedure.
set -u
here="$(cd "$(dirname "$0")" && pwd)"; root="$(dirname "$(dirname "$here")")"
cmd="${1:-}"; [ $# -gt 0 ] && shift
mode=pilot; session=""; dry=0; yes=0
while [ $# -gt 0 ]; do
    case "$1" in
        --pilot) mode=pilot ;; --full) mode=full ;; --dry-run) dry=1 ;; --yes) yes=1 ;;
        --session) [ $# -ge 2 ] || { echo "--session needs a value" >&2; exit 64; }; session="$2"; shift ;;
        *) echo "unknown argument $1" >&2; exit 64 ;;
    esac
    shift
done
work="${JH_JF_WORK:-$here/work}"; py="${JH_PY:-$root/dist/local-test/venv/bin/python}"
fail() { echo "ab_manual: $*" >&2; exit 65; }
case "$cmd" in
    plan)
        [ -n "$session" ] || session="$work/ab/$(date -u +%Y%m%dT%H%M%SZ)-$mode"
        python3 - "$here" "$session" "$mode" <<'P' || exit $?
import sys
sys.path.insert(0, sys.argv[1])
import ab_config
try:
    plan = ab_config.new_session(sys.argv[2], sys.argv[3])
except ab_config.PlanError as e:
    sys.exit("ab_manual: %s" % e)
print("session %s: %d runs, phases %s" % (sys.argv[2], len(plan["runs"]),
      ", ".join("%s %ds" % (p["name"], p["seconds"]) for p in plan["phases"])))
print("The arms are sealed in %s/.key.json. Do not open it before `reveal`." % sys.argv[2])
P
        echo "next: $0 run --session $session --dry-run" ;;
    status|validate|reveal)
        [ -n "$session" ] || fail "--session is required"
        if [ "$cmd" = status ]; then
            python3 -c '
import sys; sys.path.insert(0, sys.argv[1]); import ab_config as C
for r in C.load_plan(sys.argv[2])["runs"]: print(r["run"], C.run_state(sys.argv[2], r["run"]))' "$here" "$session"
        else
            python3 "$here/ab_report.py" "$cmd" "$session"
        fi ;;
    run)
        [ -n "$session" ] || fail "--session is required"
        if [ "$dry" = 1 ]; then
            exec env -u JMS_TICK_MS JH_JF_WORK="$work" JH_PY="$py" JH_MAX_MPV=1 python3 "$here/ab_session.py" --session "$session" --dry-run
        fi
        ss -ltn 2>/dev/null | grep -q ':18097 ' && fail "port 18097 is already in use"
        if [ "$yes" != 1 ]; then
            echo "This opens a JellyHome window on your desktop and asks you to scroll with your real mouse wheel and touchpad."
            read -r -p "Go? [y/N] " ans; [ "$ans" = y ] || [ "$ans" = Y ] || { echo "not started"; exit 1; }
        fi
        t=720; grep -q '"mode": "full"' "$session/plan.json" 2>/dev/null && t=1200
        env -u JMS_TICK_MS JH_JF_WORK="$work" JH_PY="$py" JH_MAX_MPV=1 \
            "$root/tools/limited_run.sh" -t "$t" -- python3 "$here/ab_session.py" --session "$session"
        exit $? ;;
    *) sed -n 2,13p "$0"; exit 64 ;;
esac
