"""Characterization-test harness: snapshot the browser's rendered scene.

The mpvtk UI is declarative — ``build()`` returns a widget tree, ``layout()``
turns it into the flat list of nodes that is JSON-serialized and pushed to the
renderer. That flat list IS the UI: if it is byte-identical before and after a
change, the pixels are identical too.

That makes exact characterization testing cheap here in a way it usually is
not for a GUI. A refactor that is meant to preserve behaviour can be checked
against every snapshotted screen at once, and any difference is shown as a
readable diff rather than "looks the same to me".

Two things have to be normalized or the snapshots are not reproducible:

* **strip/bitmap ``src``** — a path into a per-process temp dir, or a raw
  malloc address on the libmpv path. Both are allocation artifacts.
* **``v``** — StripStore's monotonic content version, whose absolute value
  depends on how many bitmaps were composited before this one.

Neither carries meaning across processes; both are replaced by a stable token
that still distinguishes *different* bitmaps within one scene, so a genuine
"this tile now shows different artwork" change is still caught.

Usage::

    from tests._scene_snapshot import snapshot, ROUTES

    scene = snapshot(build_browser(), {"kind": "home", "server": "s1"})

Regenerate the committed baselines after an INTENDED UI change::

    python3 tests/test_scene_snapshots.py --update
"""

import contextlib
import datetime
import json
import os
import re
import time

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "snapshots")

#: Wall clock every snapshot is captured at. Arbitrary but fixed.
FROZEN = datetime.datetime(2020, 1, 2, 3, 4, 5)
FROZEN_TS = FROZEN.timestamp()


class _FrozenDatetime(datetime.datetime):
    """``datetime`` whose ``now``/``utcnow``/``today`` do not move."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is None else FROZEN.replace(tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return FROZEN

    @classmethod
    def today(cls):
        return FROZEN


@contextlib.contextmanager
def frozen_clock():
    """Pin wall-clock time for the duration of a capture.

    Screens render clock-derived text — the detail pane's "Ends at HH:MM" is
    ``datetime.now() + remaining``. Without this the baseline rots within the
    minute and every snapshot test becomes a flake, which is worse than no
    snapshot at all because people learn to regenerate on red.

    Deliberately NOT a normalizer that masks time-shaped strings: masking
    would also hide a genuine change to how a time is formatted or which
    clock it came from. Freezing keeps the assertion exact.

    ``time.monotonic`` is left alone — it feeds timers and back-off, never
    rendered text, and stopping it can hang a poll loop.
    """
    real_dt, real_time = datetime.datetime, time.time
    datetime.datetime = _FrozenDatetime
    time.time = lambda: FROZEN_TS
    try:
        yield
    finally:
        datetime.datetime = real_dt
        time.time = real_time

# A file path into a temp dir, or "&<address>" from MemoryStore.
# Either separator: on Windows the cache path arrives with backslashes,
# missed the forward-slash-only pattern, and the raw temp path (which
# carries a pid and a random suffix) went straight into the snapshot.
_VOLATILE_SRC = re.compile(r"^(&\d+|.*[/\\][^/\\]*\.bgra)$")


def normalize(nodes):
    """Strip allocation artifacts, preserving within-scene distinctness.

    Distinct bitmaps keep distinct tokens (bitmap#0, bitmap#1, ...) in first
    appearance order, so "these two tiles share one strip" and "this tile's
    artwork changed" both still show up as a diff.
    """
    tokens = {}
    out = []
    for node in nodes:
        node = dict(node)
        src = node.get("src")
        if isinstance(src, str) and _VOLATILE_SRC.match(src):
            if src not in tokens:
                tokens[src] = "bitmap#%d" % len(tokens)
            node["src"] = tokens[src]
        if "v" in node:
            # Same reasoning: the counter's absolute value is meaningless,
            # its identity within the scene is not.
            node["v"] = "v#%d" % len(tokens)
        out.append(node)
    return out


def snapshot(browser, route, size=(1280, 720)):
    """Load ``route`` into ``browser``, settle its async work, and return the
    normalized scene.

    Settling matters: ``_load_route`` submits to the pool and returns, so
    rendering immediately snapshots a spinner. Every route then looks the
    same, and the snapshot proves nothing.
    """
    from jellyfin_mpv_shim.mpvtk.layout import layout

    with frozen_clock():
        browser.nav_stack = [dict(route)]
        browser._load_route(browser.route)
        browser._pool.shutdown(wait=True)
        nodes, _handlers = layout(browser.build(size), *size)
    return normalize(nodes)


def dumps(nodes):
    """One node per line: stable, and a real diff when something moves."""
    return "\n".join(json.dumps(n, sort_keys=True, default=str)
                     for n in nodes) + "\n"


def path_for(name):
    return os.path.join(SNAPSHOT_DIR, "%s.jsonl" % name)


def load(name):
    with open(path_for(name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def store(name, nodes):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(path_for(name), "w", encoding="utf-8") as fh:
        fh.write(dumps(nodes))
