#!/usr/bin/env bash
# The app the A/B run launches: the REPOSITORY SOURCE (run.py) on the local-test venv's interpreter and
# libmpv, with the same isolated XDG state as dist/local-test/launch.sh. The installed copy in that venv
# can be older than the source and lack the frame diagnostics the comparison needs (the cadence floor is
# only honoured, and reported, by a renderer that has them), so launch.sh is not used here.
# Both arms run this same code; nothing is overridden.
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
lt="$repo/dist/local-test"
export XDG_CONFIG_HOME="$lt/state/config" XDG_DATA_HOME="$lt/state/data" XDG_CACHE_HOME="$lt/state/cache"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
cd "$repo"
exec "${JH_PY:-$lt/venv/bin/python}" "$repo/run.py" "$@"
