"""MpvtkApp: owns the mpv instance (or attaches to one), pushes scenes,
and dispatches renderer events to Python callbacks.

Backends mirror player.py: python-mpv-jsonipc (external mpv process) or
python-mpv (libmpv in-process). The renderer script and the protocol are
identical for both; only spawn/attach and event plumbing differ.
"""

import json
import logging
import os
import queue
import threading
import time

from .layout import layout, set_metrics
from .metrics import extend_metrics, measure_font

log = logging.getLogger("mpvtk")

_RENDERER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderer.lua")

_SPAWN_OPTS = {
    "idle": "yes",
    "force_window": "yes",
    "osc": "no",
    "config": "no",
    "load_scripts": "no",
    "input_default_bindings": "no",
    "cursor_autohide": "no",
    "background_color": "#141414",
    # A UI window resizes freely; without this mpv snaps the window back
    # to the video aspect ratio (and browse mode has no video at all).
    "keepaspect_window": "no",
    "title": "mpvtk demo",
}


class JsonIpcBackend:
    def __init__(self, geometry="1280x720"):
        import python_mpv_jsonipc

        self._cb = None
        self._quit_cb = None
        opts = dict(_SPAWN_OPTS)
        opts["geometry"] = geometry
        self.mpv = python_mpv_jsonipc.MPV(
            start_mpv=True, quit_callback=self._on_quit, **opts
        )

        @self.mpv.on_event("client-message")
        def _client_message(evt):
            args = evt.get("args") or []
            if self._cb:
                self._cb(args)

    def _on_quit(self):
        if self._quit_cb:
            self._quit_cb()

    def command(self, *args):
        self.mpv.command(*args)

    def on_client_message(self, cb):
        self._cb = cb

    def on_quit(self, cb):
        self._quit_cb = cb

    def stop(self):
        try:
            self.mpv.terminate()
        except Exception:
            pass


class LibmpvBackend:
    def __init__(self, geometry="1280x720"):
        import mpv as libmpv

        self._cb = None
        self._quit_cb = None
        opts = {k.replace("_", "-"): v for k, v in _SPAWN_OPTS.items()}
        opts["geometry"] = geometry
        self.mpv = libmpv.MPV(**opts)

        @self.mpv.event_callback("client-message")
        def _client_message(event):
            if hasattr(event, "as_dict"):
                event = event.as_dict()
                if "args" in event:
                    event["args"] = [d.decode("utf-8") for d in event["args"]]
            if "event_id" in event:
                args = event["event"]["args"]
            else:
                args = event.get("args") or []
            if self._cb:
                self._cb(args)

        @self.mpv.event_callback("shutdown")
        def _shutdown(event):
            if self._quit_cb:
                self._quit_cb()

    def command(self, *args):
        self.mpv.command(*args)

    def on_client_message(self, cb):
        self._cb = cb

    def on_quit(self, cb):
        self._quit_cb = cb

    def stop(self):
        try:
            self.mpv.terminate()
        except Exception:
            pass


class MpvtkApp:
    """Event loop: build(size) -> element tree, pushed to the renderer.

    ``build`` is called on ready/resize and after any callback batch that
    called invalidate(). Callbacks run on the loop thread.
    """

    def __init__(self, backend="jsonipc", geometry="1280x720"):
        if backend == "libmpv":
            self.backend = LibmpvBackend(geometry)
        else:
            self.backend = JsonIpcBackend(geometry)
        # Same-process mpv can take images via overlay-add '&<address>'
        # (rawimage.MemoryStore) instead of scratch files.
        self.in_process = backend == "libmpv"
        self.size = None
        self._queue = queue.Queue()
        self._handlers = {}
        self._metrics = None
        self._dirty = False
        self._build = None
        self._debug_state = None
        self._debug_evt = threading.Event()
        self.ready = threading.Event()
        self.backend.on_client_message(self._on_message)
        self.backend.on_quit(lambda: self._queue.put(("__quit", None)))
        self.backend.command("load-script", _RENDERER)

    # ------------------------------------------------------------ events

    def _on_message(self, args):
        if not args or args[0] != "mpvtk-event":
            return
        try:
            evt = json.loads(args[1])
        except (ValueError, IndexError):
            log.warning("bad mpvtk-event: %r", args[1:])
            return
        self._queue.put(("evt", evt))

    def invalidate(self):
        """Request a re-render. Thread-safe: callable from background
        workers (thumbnail pool, download progress, playback timers) —
        wakes the loop instead of waiting for the next renderer event."""
        self._dirty = True
        self._queue.put(("wake", None))

    def _push_metrics(self):
        """Measured glyph advances -> layout engine + renderer, so both
        sides agree on real text widths (falls back to the heuristic
        table when no font is measurable)."""
        m = measure_font()
        if not m:
            return
        self._metrics = m
        set_metrics(m["widths"], m.get("kern"))
        self.backend.command(
            "script-message", "mpvtk-metrics", json.dumps(m)
        )

    @staticmethod
    def _scene_texts(nodes):
        texts = []
        for n in nodes:
            for key in ("text", "ph"):
                v = n.get(key)
                if v:
                    texts.append(v)
            items = n.get("items")
            if items:
                texts.extend(items)
        return texts

    def _extend_metrics(self, texts):
        """On-demand glyph/pair measurement for novel text (full
        unicode can't be pre-enumerated). Returns True if the tables
        grew — callers then re-push/re-layout."""
        if not self._metrics or not texts:
            return False
        if not extend_metrics(self._metrics, texts):
            return False
        m = self._metrics
        set_metrics(m["widths"], m.get("kern"))
        self.backend.command(
            "script-message", "mpvtk-metrics", json.dumps(m)
        )
        return True

    def _render(self):
        if self.size is None or self._build is None:
            return
        t0 = time.perf_counter()
        tree = self._build(self.size)
        t1 = time.perf_counter()
        nodes, handlers = layout(tree, *self.size)
        if self._extend_metrics(self._scene_texts(nodes)):
            # novel glyphs got measured: lay out once more with the
            # accurate widths (builds cost ~0.3ms; this is rare)
            nodes, handlers = layout(self._build(self.size), *self.size)
        self._handlers = handlers
        scene = {"v": 1, "w": self.size[0], "h": self.size[1], "nodes": nodes}
        t2 = time.perf_counter()
        self.backend.command(
            "script-message", "mpvtk-scene", json.dumps(scene)
        )
        t3 = time.perf_counter()
        log.info(
            "render: build %.1fms, layout %.1fms, push %.1fms (%d nodes)",
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t3 - t2) * 1000,
            len(nodes),
        )
        self._dirty = False

    def _dispatch(self, evt):
        t = evt.get("t")
        if t in ("ready", "resize"):
            self.size = (evt["w"], evt["h"])
            self._dirty = True
            if t == "ready":
                self._push_metrics()
                self.ready.set()
            return
        if t == "debug_state":
            self._debug_state = evt
            self._debug_evt.set()
            return
        if t in ("change", "submit"):
            # typed text may contain glyphs we've never measured; the
            # metrics push makes the renderer re-render with real widths
            v = evt.get("value")
            if isinstance(v, str):
                self._extend_metrics([v])
        h = self._handlers.get(evt.get("id"), {})
        fn = h.get(t)
        if fn is None:
            return
        try:
            if t == "click":
                fn()
            elif t in ("change", "submit"):
                fn(evt.get("value", ""))
            elif t == "select":
                fn(evt.get("index", 0), evt.get("value"))
            elif t == "scroll":
                fn(evt.get("offset", 0), evt.get("max", 0))
            elif t == "context":
                fn(evt.get("x", 0), evt.get("y", 0))
            elif t == "dismiss":
                fn()
        except Exception:
            log.exception("mpvtk handler for %s failed", evt)

    def run(self, build):
        """Blocks until mpv quits (window closed / quit())."""
        self._build = build
        while True:
            try:
                kind, evt = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                break
            if kind == "__quit":
                break
            if kind == "evt":
                self._dispatch(evt)
            # coalesce whatever else is queued before re-rendering
            while True:
                try:
                    kind, evt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "__quit":
                    return
                if kind == "evt":
                    self._dispatch(evt)
            if self._dirty:
                self._render()
        self.backend.stop()

    def quit(self):
        self._queue.put(("__quit", None))

    # -------------------------------------------------- test/debug hooks

    def debug(self, **cmd):
        self.backend.command(
            "script-message", "mpvtk-debug", json.dumps(cmd)
        )

    def debug_state(self, timeout=2.0):
        self._debug_evt.clear()
        self.debug(cmd="state")
        self._debug_evt.wait(timeout)
        return self._debug_state

    def screenshot(self, path):
        self.backend.command("screenshot-to-file", path, "window")
