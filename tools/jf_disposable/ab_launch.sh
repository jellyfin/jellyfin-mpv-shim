#!/usr/bin/env bash
# Launcher wrapper for ab_session.py (jf_run.py scroll mode calls it as JH_JF_APP).
#
#   ab_launch.sh --config DIR [--mpv-loglevel info]      starts the real app (JH_AB_REAL_APP)
#   ab_launch.sh --config DIR stop                       passes straight through
#
# For the start only, it (1) adds an mpv IPC socket to the config's mpv.conf, used by ab_session.py for
# two read-only property reads, and (2) sets the frame/artwork diagnostics and the cadence floor in the
# app's environment. Setting them HERE rather than in jf_run's environment keeps the Xvfb login-seeding
# instance, which runs first with the same variables otherwise, out of the frame log. The `stop` call is a
# second process and must not write diagnostics either.
# Nothing else about the app, its config or its rendering is changed.
set -eu
: "${JH_AB_REAL_APP:?}" "${JH_AB_IPC:?}" "${JH_AB_FRAME_LOG:?}" "${JH_AB_ART_DIAG:?}" "${JH_AB_TICK_MS:?}"
cfg=""; prev=""; stopping=0
for a in "$@"; do
    [ "$prev" = "--config" ] && cfg="$a"
    [ "$a" = "stop" ] && stopping=1
    prev="$a"
done
if [ "$stopping" = 1 ]; then exec "$JH_AB_REAL_APP" "$@"; fi
[ -n "$cfg" ] || { echo "ab_launch: no --config" >&2; exit 64; }
printf 'input-ipc-server=%s\n' "$JH_AB_IPC" >> "$cfg/mpv.conf"
export JMS_FRAME_DIAG=1 JMS_FRAME_LOG="$JH_AB_FRAME_LOG" JMS_ART_DIAG="$JH_AB_ART_DIAG" JMS_TICK_MS="$JH_AB_TICK_MS"
exec "$JH_AB_REAL_APP" "$@"
