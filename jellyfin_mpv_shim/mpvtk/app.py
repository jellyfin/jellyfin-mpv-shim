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

from .layout import layout, set_metrics
from .metrics import measure_font

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
        self._dirty = True

    def _push_metrics(self):
        """Measured glyph advances -> layout engine + renderer, so both
        sides agree on real text widths (falls back to the heuristic
        table when no font is measurable)."""
        m = measure_font()
        if not m:
            return
        set_metrics(m["widths"])
        self.backend.command(
            "script-message", "mpvtk-metrics", json.dumps(m)
        )

    def _render(self):
        if self.size is None or self._build is None:
            return
        tree = self._build(self.size)
        nodes, handlers = layout(tree, *self.size)
        self._handlers = handlers
        scene = {"v": 1, "w": self.size[0], "h": self.size[1], "nodes": nodes}
        self.backend.command(
            "script-message", "mpvtk-scene", json.dumps(scene)
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
            self._dispatch(evt)
            # coalesce whatever else is queued before re-rendering
            while True:
                try:
                    kind, evt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "__quit":
                    return
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
