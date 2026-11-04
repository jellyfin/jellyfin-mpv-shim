#!/usr/bin/env bash
# Run ONE command (and every child it starts) inside a single systemd user
# scope with a combined memory ceiling, a timeout and a headroom watchdog.
#
#   tools/limited_run.sh [-t SECONDS] [-- ] COMMAND [ARGS...]
#
# Why this exists: one 3 GiB allowance *per process* is not a limit when a test
# runner starts a process per module and each opens a real mpv window. The
# cgroup below is shared by the whole process tree, so 3 GiB is the sum.
#
# What it enforces (all per-scope; nothing global is changed):
#   MemoryMax   3G     hard ceiling for the whole tree (kernel OOM-kills inside
#                      the scope only)
#   MemoryHigh  2816M  reclaim pressure before the hard ceiling is reached
#   MemorySwapMax 0    the workload cannot push the desktop into swap
#   TasksMax    1024   fork-bomb guard
#   RuntimeMaxSec      the timeout, enforced by systemd itself, so it still
#                      fires if this wrapper is killed
#
# What it does from OUTSIDE the scope (so it survives the workload dying):
#   * refuses to start unless MemAvailable >= 4 GiB;
#   * samples MemAvailable every second and stops the scope (SIGTERM, then
#     SIGKILL after 10 s, both aimed at the scope's own cgroup only) if it
#     falls below 4 GiB;
#   * stops the scope on EXIT/INT/TERM/HUP so nothing outlives a failed run;
#   * prints peak scope memory, lowest MemAvailable and leftover tasks.
#
# It never retries, never raises the limit by itself, and never signals a
# process by name -- only the cgroup it created.
#
# Player monitoring (JH_MAX_MPV=N, off when unset): every second the watchdog
# lists ALL processes in the scope's cgroup *and its nested cgroups* (not the
# parent/child tree, so orphans count) and counts those named mpv. More than N
# stops the scope. This is a monitored stop threshold polled once a second, NOT
# a kernel-enforced limit: a burst inside one sample interval is not prevented.
# After the command exits, anything still in the cgroup is a leftover: the run
# is reported as cleanup-FAILED (exit 79), the scope is then stopped, and the
# leftovers are named. They are never silently killed and called a success.
#
# Exit codes: the command's own; 75 not enough headroom to start; 76 stopped
# by the headroom watchdog; 78 more than JH_MAX_MPV players seen; 79 processes
# left in the scope after the command exited; 124 timed out (RuntimeMaxSec).
set -u

MEM_MAX="${JH_MEM_MAX:-3G}"
MEM_HIGH="${JH_MEM_HIGH:-2816M}"
MIN_AVAIL_KIB=$(( ${JH_MIN_AVAIL_MIB:-4096} * 1024 ))
TIMEOUT=300
MAX_MPV="${JH_MAX_MPV:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        -t) TIMEOUT="$2"; shift 2 ;;
        --) shift; break ;;
        -*) echo "limited_run: unknown option $1" >&2; exit 64 ;;
        *) break ;;
    esac
done
[ $# -gt 0 ] || { echo "usage: $0 [-t SECONDS] [--] COMMAND..." >&2; exit 64; }

avail_kib() { awk '/^MemAvailable:/ {print $2}' /proc/meminfo; }

start_avail=$(avail_kib)
if [ "$start_avail" -lt "$MIN_AVAIL_KIB" ]; then
    echo "limited_run: only $((start_avail / 1024)) MiB available, need $((MIN_AVAIL_KIB / 1024)); not starting." >&2
    exit 75
fi

UNIT="jh-limited-$$-$RANDOM"
SCOPE="$UNIT.scope"
STATE="$(mktemp -d)"

cg_dir() {
    local rel
    rel=$(systemctl --user show -p ControlGroup --value "$SCOPE" 2>/dev/null)
    [ -n "$rel" ] && [ -d "/sys/fs/cgroup$rel" ] && echo "/sys/fs/cgroup$rel"
}

# Every pid in the scope, nested cgroups included (orphans and double-forks stay).
scope_pids() {
    local d
    d=$(cg_dir) || return 0
    find "$d" -name cgroup.procs -exec cat {} + 2>/dev/null | sort -un
}

# "pid:comm" for each pid in the scope.
scope_procs() {
    local pid
    for pid in $(scope_pids); do
        [ -r "/proc/$pid/comm" ] && echo "$pid:$(cat /proc/$pid/comm 2>/dev/null)"
    done
}

stop_scope() {
    systemctl --user stop "$SCOPE" >/dev/null 2>&1
}

(
    # Watchdog: lives outside the scope on purpose.
    low=$start_avail; peak=0; tasks_max=0; killed=0; maxmpv=0; breach=0
    while :; do
        a=$(avail_kib); [ "$a" -lt "$low" ] && low=$a
        d=$(cg_dir) || d=""
        if [ -n "$d" ]; then
            p=$(cat "$d/memory.peak" 2>/dev/null || echo 0)
            c=$(cat "$d/memory.current" 2>/dev/null || echo 0)
            [ "$c" -gt "$p" ] && p=$c
            [ "$p" -gt "$peak" ] && peak=$p
            t=$(cat "$d/pids.current" 2>/dev/null || echo 0)
            [ "$t" -gt "$tasks_max" ] && tasks_max=$t
            oom=$(awk '/^oom_kill /{print $2}' "$d/memory.events" 2>/dev/null)
            n=$(scope_procs | grep -c ':mpv')
            [ "$n" -gt "$maxmpv" ] && maxmpv=$n
            echo "$(date +%T) mpv=$n tasks=$t" >> "$STATE/samples"
            if [ -n "$MAX_MPV" ] && [ "$n" -gt "$MAX_MPV" ] && [ "$breach" = 0 ]; then
                echo "limited_run: $n mpv processes in scope (> $MAX_MPV) -- stopping $SCOPE" >&2
                breach=1; stop_scope
            fi
            echo "$peak $low $tasks_max ${oom:-0} $killed $maxmpv $breach" > "$STATE/stats"
        fi
        if [ "$a" -lt "$MIN_AVAIL_KIB" ] && [ "$killed" = 0 ]; then
            echo "limited_run: MemAvailable ${a} KiB < $MIN_AVAIL_KIB KiB -- stopping $SCOPE" >&2
            killed=1; echo "$peak $low $tasks_max ${oom:-0} 1 $maxmpv $breach" > "$STATE/stats"
            stop_scope
        fi
        sleep 1
    done
) &
WATCH=$!

cleanup() {
    stop_scope
    kill "$WATCH" 2>/dev/null
    wait "$WATCH" 2>/dev/null
}
trap 'cleanup; exit 130' INT TERM HUP

systemd-run --user --scope --collect --quiet --unit="$UNIT" \
    -p MemoryMax="$MEM_MAX" -p MemoryHigh="$MEM_HIGH" -p MemorySwapMax=0 \
    -p TasksMax=1024 -p RuntimeMaxSec="$TIMEOUT" -p TimeoutStopSec=10 \
    -- "$@"
rc=$?

# The command has exited. Give an orderly shutdown 3 s to finish, then whatever
# is still in the cgroup is a leftover.
leftovers=""
for _ in 1 2 3 4 5 6; do
    leftovers=$(scope_procs); [ -z "$leftovers" ] && break; sleep 0.5
done
left_mpv=$(printf '%s\n' "$leftovers" | grep -c ':mpv')
sleep 1.1   # let the watchdog take a last sample
end_avail=$(avail_kib)
read -r peak low tasks oom killed maxmpv breach < "$STATE/stats" 2>/dev/null || { peak=0; low=$end_avail; tasks=0; oom=0; killed=0; maxmpv=0; breach=0; }
cleanup
left=0
for _ in 1 2 3 4 5 6 7 8 9 10; do   # scope teardown is asynchronous
    systemctl --user is-active --quiet "$SCOPE" || break; sleep 0.5; done
systemctl --user is-active --quiet "$SCOPE" && left=1
trap - INT TERM HUP
[ -n "${JH_KEEP_SAMPLES:-}" ] && cp "$STATE/samples" "$JH_KEEP_SAMPLES" 2>/dev/null
rm -rf "$STATE"

[ "$killed" = 1 ] && rc=76
[ "$breach" = 1 ] && rc=78
[ -n "$leftovers" ] && [ "$killed" != 1 ] && [ "$breach" != 1 ] && rc=79
# systemd reports a RuntimeMaxSec stop as SIGTERM (143) / SIGKILL (137).
{
    echo "limited_run: exit=$rc  limit=$MEM_MAX swap=0  scope-peak=$((peak / 1048576)) MiB"
    echo "limited_run: mpv max-seen=$maxmpv  leftover-mpv=$left_mpv  leftover-procs=$(printf '%s' "$leftovers" | grep -c .)  cleanup=$([ -z "$leftovers" ] && echo ok || echo FAILED)"
    [ -n "$leftovers" ] && echo "limited_run: LEFTOVERS (pid:comm): $(echo $leftovers)"
    echo "limited_run: lowest-MemAvailable=$((low / 1024)) MiB  max-tasks=$tasks  oom_kills=$oom  scope-still-active=$left"
} >&2
exit "$rc"
