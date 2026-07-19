"""MpvtkApp: owns the mpv instance (or attaches to one), pushes scenes,
and dispatches renderer events to Python callbacks.

Backends mirror player.py: python-mpv-jsonipc (external mpv process) or
python-mpv (libmpv in-process). The renderer script and the protocol are
identical for both; only spawn/attach and event plumbing differ.
"""

import inspect
import json
import logging
import os
import queue
import threading
import time

from . import theme
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

    def get_property(self, name):
        # jsonipc: get_property is a raw IPC command with a return value
        try:
            return self.mpv.command("get_property", name)
        except Exception:
            return None

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

    def get_property(self, name):
        # slash paths ('user-data/...') can't use attribute access
        try:
            return self.mpv._get_property(name)
        except Exception:
            return None

    def on_client_message(self, cb):
        self._cb = cb

    def on_quit(self, cb):
        self._quit_cb = cb

    def stop(self):
        try:
            self.mpv.terminate()
        except Exception:
            pass


class AdoptBackend:
    """Attach to an *existing* mpv handle instead of spawning a new one.

    This is the production path: the browser UI shares the player's own
    mpv window (see player.PlayerManager.get_mpv) rather than opening a
    second window. The spawn backends above stay for the standalone
    demo/selftest only.

    Two things differ from the spawn backends:

    - **The window is shared.** ``stop()`` must NOT terminate the handle
      — the player owns its lifecycle. We only ever stop pushing scenes.
    - **We add a second client-message listener.** Both bindings store
      handlers in a set/list (jsonipc ``bind_event``; libmpv supports
      multiple ``event_callback``s), so ours coexists with the player's
      own ``shim-*`` handler on the same stream — the ``mpvtk-*``
      namespace doesn't collide.

    ``ext`` mirrors ``player.is_using_ext_mpv``: True for an external
    python-mpv-jsonipc process, False for in-process libmpv (which can
    take images via same-process ``&<address>`` — see MemoryStore).
    """

    def __init__(self, mpv_handle, ext):
        self.mpv = mpv_handle
        self._ext = ext
        self._cb = None
        self._quit_cb = None

        @self.mpv.event_callback("client-message")
        def _client_message(event):
            args = self._decode(event)
            if args and self._cb:
                self._cb(args)

        @self.mpv.event_callback("shutdown")
        def _shutdown(event=None):
            # The player owns teardown; we just let our loop thread exit.
            if self._quit_cb:
                self._quit_cb()

    def _decode(self, event):
        # Mirror the two proven decode paths from the spawn backends /
        # player.py: jsonipc hands us a plain dict; libmpv hands a struct
        # that needs .as_dict() + a utf-8 decode of the byte args.
        if self._ext:
            if hasattr(event, "as_dict"):
                event = event.as_dict()
            return (event.get("args") if isinstance(event, dict) else None) or []
        if hasattr(event, "as_dict"):
            event = event.as_dict()
            if "args" in event:
                event["args"] = [d.decode("utf-8") for d in event["args"]]
        if isinstance(event, dict):
            if "event_id" in event and isinstance(event.get("event"), dict):
                return event["event"].get("args") or []
            return event.get("args") or []
        return []

    def command(self, *args):
        self.mpv.command(*args)

    def get_property(self, name):
        try:
            if self._ext:
                return self.mpv.command("get_property", name)
            return self.mpv._get_property(name)
        except Exception:
            return None

    def on_client_message(self, cb):
        self._cb = cb

    def on_quit(self, cb):
        self._quit_cb = cb

    def stop(self):
        # Shared handle: never terminate the player's mpv.
        pass


class MpvtkApp:
    """Event loop: build(size) -> element tree, pushed to the renderer.

    ``build`` is called on ready/resize and after any callback batch that
    called invalidate(). Callbacks run on the loop thread.

    Spawn its own mpv (``backend=``, demo/selftest) or attach to the
    player's existing handle (``mpv_handle=`` + ``ext=``, production —
    see :meth:`attach`).
    """

    def __init__(self, backend="jsonipc", geometry="1280x720",
                 mpv_handle=None, ext=None):
        if mpv_handle is not None:
            if ext is None:
                raise ValueError(
                    "attaching requires ext=<player.is_using_ext_mpv>"
                )
            self.backend = AdoptBackend(mpv_handle, ext=ext)
            # In-process (libmpv) can take images via memory addresses;
            # an external jsonipc mpv needs BGRA scratch files.
            self.in_process = not ext
        elif backend == "libmpv":
            self.backend = LibmpvBackend(geometry)
            self.in_process = True
        else:
            self.backend = JsonIpcBackend(geometry)
            self.in_process = False
        self.size = None
        self._queue = queue.Queue()
        self._handlers = {}
        self._nodes = None  # last pushed scene, for node_rect()
        # called with True/False when keyboard/remote navigation
        # engages / a mouse press takes over (hide carousel arrows,
        # switch affordances). Runs on the loop thread.
        self.on_nav = None
        # called with True/False when the playback HUD is summoned /
        # auto-hides (see set_hud). Runs on the loop thread; the True
        # call should flip the build to the HUD scene + invalidate.
        self.on_hud = None
        # called when the user activates the renderer-drawn standalone
        # Skip Intro/Credits button while the HUD is idle (ENTER /
        # remote Select / click). Should perform the skip.
        self.on_hud_skip = None
        self._metrics = None
        self._dirty = False
        self._build = None
        self._debug_state = None
        self._debug_evt = threading.Event()
        self.ready = threading.Event()
        self.backend.on_client_message(self._on_message)
        self.backend.on_quit(lambda: self._queue.put(("__quit", None)))
        self.backend.command("load-script", _RENDERER)

    @classmethod
    def attach(cls, mpv_handle, ext, **kw):
        """Attach the UI to the player's existing mpv handle.

        ``mpv_handle`` is ``playerManager.get_mpv()``; ``ext`` is
        ``player.is_using_ext_mpv``. The renderer script is loaded into
        that live mpv and scenes are pushed over the same connection —
        no second window is opened."""
        return cls(mpv_handle=mpv_handle, ext=ext, **kw)

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

    def push_theme(self):
        """Forward the accent palette to the renderer.

        Python-side widgets read theme.ACCENT when they're built, but the
        renderer draws the focused textbox border, the open dropdown border
        and the slider fill itself, so it needs its own copy. Sent on ready
        and again by set_accent()."""
        self.backend.command(
            "script-message", "mpvtk-theme", json.dumps(theme.palette())
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
        self._nodes = nodes
        scene = {"v": 1, "w": self.size[0], "h": self.size[1], "nodes": nodes}
        t2 = time.perf_counter()
        self.backend.command(
            "script-message", "mpvtk-scene", json.dumps(scene)
        )
        t3 = time.perf_counter()
        # Per-frame timing: useful while the renderer was being built, pure
        # noise in a normal log now. Debug-level so it can still be turned
        # on when something is actually slow.
        log.debug(
            "render: build %.1fms, layout %.1fms, push %.1fms (%d nodes)",
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t3 - t2) * 1000,
            len(nodes),
        )
        self._dirty = False

    @staticmethod
    def _wants_mods(fn):
        """A click handler opts into the modifier payload by declaring a
        required positional parameter (``def f(mods)``, ``lambda m: …``).
        Zero-arg handlers and default-arg lambdas (``lambda i=item: …``)
        keep the bare call — a default first parameter is almost always
        a captured loop variable, not a mods slot."""
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        for p in sig.parameters.values():
            if p.kind == p.VAR_POSITIONAL:
                return True
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                return p.default is p.empty
            return False
        return False

    def _dispatch(self, evt):
        t = evt.get("t")
        if t in ("ready", "resize"):
            self.size = (evt["w"], evt["h"])
            self._dirty = True
            if t == "ready":
                self._push_metrics()
                self.push_theme()
                self.ready.set()
            return
        if t == "debug_state":
            self._debug_state = evt
            self._debug_evt.set()
            return
        if t == "nav":
            if self.on_nav is not None:
                try:
                    self.on_nav(bool(evt.get("active")))
                except Exception:
                    log.exception("on_nav handler failed")
            return
        if t == "hud":
            if self.on_hud is not None:
                try:
                    self.on_hud(bool(evt.get("active")))
                except Exception:
                    log.exception("on_hud handler failed")
            self._dirty = True
            return
        if t == "hudskip":
            if self.on_hud_skip is not None:
                try:
                    self.on_hud_skip()
                except Exception:
                    log.exception("on_hud_skip handler failed")
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
                if self._wants_mods(fn):
                    fn({
                        "shift": bool(evt.get("shift")),
                        "ctrl": bool(evt.get("ctrl")),
                    })
                else:
                    fn()
            elif t in ("change", "submit", "commit", "hover"):
                fn(evt.get("value", ""))
            elif t in ("dbl", "cancel", "hover_end"):
                fn()
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

    def set_accent(self, accent, **kw):
        """Set the toolkit accent and push it to the renderer. Equivalent to
        ``mpvtk.theme.set_accent`` plus :meth:`push_theme`."""
        theme.set_accent(accent, **kw)
        self.push_theme()
        self.invalidate()

    def set_active(self, active):
        """Suspend/resume the in-mpv renderer.

        Only meaningful for an attached app: while suspended the renderer
        unbinds its forced mouse/wheel sections and blanks the scene, so the
        player's OSC gets the input it needs. Pushing an empty scene is not
        enough — the bindings are what swallow the clicks."""
        self.backend.command(
            "script-message", "mpvtk-active", "yes" if active else "no"
        )

    def set_hud(self, on, opts=None):
        """Enter/leave the playback-HUD lifecycle (attached-but-idle).

        Unlike ``set_active(False)`` — which gets the renderer entirely
        out of the way for other OSCs — HUD mode keeps it attached
        during playback with a blank scene and only a lightweight
        summon surface bound (the wake key + mouse motion). Summoning
        rebinds the full input sections and fires ``on_hud(True)``;
        the ~4s inactivity timer drops back to idle with
        ``on_hud(False)``. ``set_active`` in either direction also
        leaves HUD mode.

        ``opts`` is the keyboard policy: ``{"grab": bool, "key": str}``
        — grab summons on all arrows/ENTER while idle; otherwise only
        ``key`` is taken over (mpv key name; ENTER also pause-toggles
        on wake)."""
        args = ["script-message", "mpvtk-hud", "yes" if on else "no"]
        if on and opts is not None:
            args.append(json.dumps(opts))
        self.backend.command(*args)

    def summon_hud(self):
        """Wake an idle HUD as if a nav key were pressed (no pause
        toggle). No-op unless the renderer is in HUD mode and hidden."""
        self.backend.command(
            "script-message", "mpvtk-hud-summon", "nav"
        )

    def set_hud_skip(self, label):
        """Tell the renderer whether a skippable segment is live
        (falsy = none). While the HUD is idle, entering a segment shows
        a standalone renderer-drawn skip button for a few seconds
        (pointer movement re-shows it); ENTER / remote Select / a click
        on it fires ``on_hud_skip``. While summoned, the scene's own
        button is authoritative and this only tracks the label."""
        self.backend.command(
            "script-message", "mpvtk-hud-skip", label or ""
        )

    def scroll(self, node_id, direction):
        """Page a scroll container (by id) by ~a viewport along its axis —
        the hook behind on-screen ◀ ▶ carousel arrows."""
        self.backend.command(
            "script-message", "mpvtk-scroll",
            json.dumps({"id": node_id, "dir": direction}),
        )

    def node_rect(self, node_id):
        """Laid-out geometry of a node from the LAST pushed scene —
        layout feedback for the next build (one frame stale by
        construction, which is fine for stable geometry like header
        heights above a virtualized list). Returns the scene node dict
        (x/y/w/h; content-space coords inside a scroll, plus cw/ch on
        scroll nodes) or None."""
        for n in self._nodes or []:
            if n["id"] == node_id:
                return n
        return None

    def scroll_offsets(self):
        """Synchronous snapshot of the renderer's live scroll offsets
        ``{id: px}`` — read it at build() time to window virtualized
        content tightly instead of trailing the throttled ``on_scroll``
        event. Empty when nothing has scrolled yet (or mpv < 0.36,
        which lacks ``user-data``)."""
        v = self.backend.get_property("user-data/mpvtk/scroll")
        return v if isinstance(v, dict) else {}
