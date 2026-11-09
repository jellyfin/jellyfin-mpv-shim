#!/usr/bin/env bash
# Launcher wrapper for si_session.py (jf_run.py scroll mode calls it as JH_JF_APP). It starts the real app
# (JH_SI_REAL_APP, normally ab_app.sh: the repository source) with the scroll recorder on:
#   JMS_FRAME_DIAG=1  JMS_SCROLL_DIAG=$JH_SI_LOG
# and nothing else changed: no cadence override, no mpv option, no config edit. A `stop` call passes straight
# through, and the Xvfb login-seeding instance (which runs before this wrapper is used) never sees the variables.
set -eu
: "${JH_SI_REAL_APP:?}" "${JH_SI_LOG:?}"
for a in "$@"; do
    if [ "$a" = "stop" ]; then exec "$JH_SI_REAL_APP" "$@"; fi
done
export JMS_FRAME_DIAG=1 JMS_SCROLL_DIAG="$JH_SI_LOG"
unset JMS_TICK_MS
exec "$JH_SI_REAL_APP" "$@"
