#!/bin/bash
# Fail only on NEW mypy errors, measured against a committed baseline.
#
# The tree has ~98 pre-existing findings, almost all `var-annotated` and
# `assignment` noise in code that was never written to be type-checked.
# Fixing them is a separate project; blocking on them would just mean the
# check gets ignored. So this compares against tools/mypy-baseline.txt and
# fails only on findings that are not already in it.
#
#   tools/mypy_gate.sh              # check for new errors
#   tools/mypy_gate.sh --update     # re-baseline after INTENTIONAL changes
#
# The run takes under a second, so there is no reason not to run it beside
# the unit suite.
#
# WHAT THIS DOES AND DOES NOT CATCH. It does not currently catch the
# "forgotten move" class -- a lambda calling a method that no longer exists
# -- because the callback parameters are unannotated, so mypy infers Any.
# tests/test_late_bound_calls.py covers that. See docs/archive/REFACTORING_METHOD.md
# §1.4 for the measurement behind that split.
set -u

cd "$(dirname "$0")/.." || exit 1
BASELINE="tools/mypy-baseline.txt"

# Errors only, line numbers stripped.
#
# Notes are excluded deliberately. mypy emits follow-on notes ("See
# https://mypy.readthedocs.io/...") whose presence depends on which modules
# it happened to follow, so adding a new module can make a note appear
# against an untouched file. That fired on the first real use of this gate
# and was pure noise -- an error is a finding, a note is commentary on one.
#
# Line numbers go because a finding that merely moved down a file is not new.
normalise() {
    grep -E "^[^ ].*: error:" | sed -E 's/^([^:]+):[0-9]+:/\1:/' | sort -u
}

current="$(mypy jellyfin_mpv_shim/ 2>&1 | normalise)"

if [ "${1:-}" = "--update" ]; then
    printf '%s\n' "$current" > "$BASELINE"
    echo "baselined $(printf '%s\n' "$current" | grep -c .) findings in $BASELINE"
    exit 0
fi

if [ ! -f "$BASELINE" ]; then
    echo "no baseline yet — run: tools/mypy_gate.sh --update"
    exit 1
fi

new="$(comm -13 "$BASELINE" <(printf '%s\n' "$current"))"
if [ -n "$new" ]; then
    echo "NEW mypy findings (not in $BASELINE):"
    printf '%s\n' "$new" | sed 's/^/  /'
    echo
    echo "Fix them, or if they are intended: tools/mypy_gate.sh --update"
    exit 1
fi

fixed="$(comm -23 "$BASELINE" <(printf '%s\n' "$current"))"
if [ -n "$fixed" ]; then
    echo "$(printf '%s\n' "$fixed" | grep -c .) baseline finding(s) no longer occur."
    echo "Re-baseline to lock the improvement in: tools/mypy_gate.sh --update"
fi
echo "no new mypy findings"
