#!/usr/bin/env bash
# Records REAL mouse-wheel and touchpad input and what JellyHome drew for it (no blinding, no ratings).
#
#   tools/jf_disposable/si_manual.sh run [--dry-run] [--yes] [--out DIR]   one guided session, about 4 minutes
#   tools/jf_disposable/si_manual.sh report FILE_OR_DIR                    the analysis again, from a saved file
#
# `run` opens ONE JellyHome window on your desktop against a throw-away Jellyfin on 127.0.0.1:18097 (120-poster
# library) with the scroll recorder on (JMS_FRAME_DIAG + JMS_SCROLL_DIAG, set by si_launch.sh only for the app),
# in the same tools/limited_run.sh scope as the A/B (3 GiB, no swap, JH_MAX_MPV=1, leftover check exit 79).
# The six phases are guided in the terminal. Nothing is sent to the app; no WAYLAND_DEBUG.
set -u
here="$(cd "$(dirname "$0")" && pwd)"; root="$(dirname "$(dirname "$here")")"
cmd="${1:-}"; [ $# -gt 0 ] && shift
dry=0; yes=0; out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry=1 ;; --yes) yes=1 ;;
        --out) [ $# -ge 2 ] || { echo "--out needs a value" >&2; exit 64; }; out="$2"; shift ;;
        *) rest="${rest:-} $1" ;;
    esac
    shift
done
work="${JH_JF_WORK:-$here/work}"; py="${JH_PY:-$root/dist/local-test/venv/bin/python}"
case "$cmd" in
    run)
        [ -n "$out" ] || out="$work/si/$(date -u +%Y%m%dT%H%M%SZ)"
        if [ "$dry" = 1 ]; then
            exec env JH_JF_WORK="$work" JH_PY="$py" JH_MAX_MPV=1 python3 "$here/si_session.py" --out "$out" --dry-run
        fi
        ss -ltn 2>/dev/null | grep -q ':18097 ' && { echo "si_manual: port 18097 is already in use" >&2; exit 65; }
        if [ "$yes" != 1 ]; then
            echo "This opens a JellyHome window on your desktop and asks you to scroll with your real mouse wheel and touchpad."
            read -r -p "Go? [y/N] " ans; [ "$ans" = y ] || [ "$ans" = Y ] || { echo "not started"; exit 1; }
        fi
        env JH_JF_WORK="$work" JH_PY="$py" JH_MAX_MPV=1 \
            "$root/tools/limited_run.sh" -t 900 -- python3 "$here/si_session.py" --out "$out"
        exit $? ;;
    report)
        f="${rest:-}"; f="${f# }"
        [ -n "$f" ] || { echo "usage: $0 report FILE_OR_DIR" >&2; exit 64; }
        [ -d "$f" ] && f="$f/scroll_diag.tsv"
        exec python3 "$here/si_analyze.py" "$f" ;;
    *) sed -n 2,11p "$0"; exit 64 ;;
esac
