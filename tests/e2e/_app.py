"""The shipped app, launched and driven from outside, the way a person uses it.

Nothing in the app is patched, and nothing in it is built by the test:
`run.py` runs as a subprocess against a config directory made for the test,
and everything else is the app's own -- the gateway, the client manager,
the profiles, the sync manager, the timeline reporter.

The test reaches it through one channel, set up the way a user would:

* **libmpv** (the default backend): `input-ipc-server=` in the config
  directory's own `mpv.conf`, which the app loads (`mpv_options.py`,
  `config_dir`).
* **jsonipc** (external mpv): `mpv_ext` and `mpv_ext_ipc` in `conf.json`,
  the user-facing settings for exactly this (`docs/configuration.md`). The
  app passes its own `--input-ipc-server`, which overrides `mpv.conf`, so
  only this backend's setting names the endpoint.

Over that channel it sends real keypresses (mpv's own input path; the OS
layer below it is left to the hand checks) and reads what the renderer
last drew, from the test-only observer `JMS_TEST_OBSERVE` switches on
(`renderer.lua`, `state.observe`).

Process ownership is the harness's job, not the test's: every process the
app starts dies with it. POSIX puts the app in its own session and kills
the group; Windows puts it in a Job Object that kills every descendant (tray,
restart child, external mpv) when the harness closes the job. A test asserts
a clean exit with `quit()` before anything is killed.

The plan: ~/Desktop/mpv-shim-offline-e2e-plan.md, "Architecture".
"""

import contextlib
import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
RUN_PY = os.path.join(REPO_ROOT, "run.py")
IS_WINDOWS = os.name == "nt"


class AppError(AssertionError):
    pass


# -- where a test's time goes ---------------------------------------------

#: {step: [calls, seconds, keys]} since the last take_steps(). Times are
#: inclusive, so nested steps (login > type_into > move_to) overlap; keys
#: are the keypresses sent while the step was open.
_STEPS = {}
_OPEN = []


@contextlib.contextmanager
def step(name):
    t0 = time.monotonic()
    _OPEN.append(name)
    try:
        yield
    finally:
        _OPEN.pop()
        s = _STEPS.setdefault(name, [0, 0.0, 0])
        s[0] += 1
        s[1] += time.monotonic() - t0


def timed(name):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            with step(name):
                return fn(*a, **kw)
        return inner
    return wrap


def _count_key():
    for name in set(_OPEN) | {"*"}:
        _STEPS.setdefault(name, [0, 0.0, 0])[2] += 1


def take_steps():
    """The tally since the last call, then reset; tests._outcomes records it
    per test."""
    out = {k: [v[0], round(v[1], 2), v[2]] for k, v in _STEPS.items()}
    _STEPS.clear()
    return out


# -- Windows: a job object that takes every descendant with it ------------

def _windows_job():
    """A job that kills every process in it when its handle closes."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC),
                    ("IoInfo", _IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                       ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return k32, job


class _Watch:
    """Watch mode, for a person looking at the real window: every input
    action is narrated and paced. JMS_E2E_WATCH=<seconds> pauses that long
    before each action; JMS_E2E_STEP=1 waits for Enter instead. Narration
    goes to stderr and to JMS_E2E_NARRATE (default: e2e-watch.txt in the
    temp dir), for a second terminal to `tail -f`."""

    def __init__(self):
        self.delay = float(os.environ.get("JMS_E2E_WATCH") or 0)
        self.step = bool(os.environ.get("JMS_E2E_STEP"))
        self.on = self.delay > 0 or self.step
        self.n = 0
        self.depth = 0
        self.path = os.environ.get("JMS_E2E_NARRATE") or os.path.join(
            tempfile.gettempdir(), "e2e-watch.txt")

    @staticmethod
    def _test():
        """The test method running this, found on the stack: narration
        without touching a single test."""
        import inspect
        for fr in inspect.stack():
            if fr.function.startswith(("test_", "setUp")):
                owner = fr.frame.f_locals.get("self")
                cls = type(owner).__name__ if owner is not None else "?"
                return "%s.%s" % (cls, fr.function)
        return "?"

    def __call__(self, what):
        if not self.on or self.depth:
            return
        self.n += 1
        line = "%s  step %d  [%s]  %s" % (time.strftime("%H:%M:%S"), self.n,
                                          self._test(), what)
        sys.stderr.write(line + "\n")
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if self.step:
            try:
                with open("/dev/tty", encoding="utf-8") as tty:
                    tty.readline()
            except OSError:
                time.sleep(self.delay or 1.0)
        else:
            time.sleep(self.delay)


WATCH = _Watch()


class App:
    """One launch of the app against one config directory.

    ``App(backend="libmpv")`` makes a fresh config dir; pass ``config_dir``
    to relaunch against an existing one (a restart scenario). ``start()``,
    then drive it; ``quit()`` for a clean exit, which a test should assert;
    ``kill()`` is the emergency path and is what ``close()`` falls back to.
    """

    def __init__(self, backend="libmpv", config_dir=None, conf=None,
                 env=None, files=None):
        if backend not in ("libmpv", "jsonipc"):
            raise ValueError(backend)
        self.backend = backend
        self.owns_dir = config_dir is None
        self.config_dir = config_dir or tempfile.mkdtemp(prefix="jms-app-")
        name = "jms-e2e-%s" % uuid.uuid4().hex[:12]
        # A bare name on Windows (the pipe namespace is implied), a socket
        # path elsewhere. What the *app* is told differs by backend: mpv.conf
        # wants the full pipe path, mpv_ext_ipc wants the bare name.
        if IS_WINDOWS:
            self._ipc_name = name
            self.ipc_path = r"\\.\pipe" + "\\" + name
        else:
            self._ipc_name = os.path.join(tempfile.gettempdir(), name)
            self.ipc_path = self._ipc_name
        self._extra_conf = dict(conf or {})
        self._extra_env = dict(env or {})
        #: {relative path: text} written into the config dir before launch,
        #: as a person's own files would be (an input.conf, say).
        self._files = dict(files or {})
        self.proc = None
        self.mpv = None
        self._job = None
        self.incarnation = 0
        #: Presses that had no effect and were repeated (press_until). Kept
        #: so a flaky input path stays visible instead of being absorbed.
        self.lost_keys = []

    @classmethod
    def copy_of(cls, template, **kw):
        """A launch on a fresh copy of config dir ``template``: it starts
        where the launch that wrote the template quit. Owned like a fresh
        dir, so close() removes it."""
        app = cls(**kw)
        shutil.copytree(template, app.config_dir, dirs_exist_ok=True)
        return app

    # -- configuration, as a user would write it -----------------------

    def _seed(self):
        conf_path = os.path.join(self.config_dir, "conf.json")
        conf = {}
        if os.path.exists(conf_path):
            with open(conf_path, encoding="utf-8") as fh:
                conf = json.load(fh)
        # Logs are how a failure is read afterwards; not a behaviour.
        conf["write_logs"] = True
        # Closing quits. The default minimizes to the tray where there is
        # one (the Windows VM has one), and quit() closes the window; the
        # tray path is its own scenario, not every test's way out.
        conf.setdefault("close_to_tray", False)
        if self.backend == "jsonipc":
            conf["mpv_ext"] = True
            conf["mpv_ext_ipc"] = self._ipc_name
        else:
            conf["mpv_ext"] = False
        conf.update(self._extra_conf)
        with open(conf_path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh, indent=2)
        lines = []
        if self.backend == "libmpv":
            lines.append("input-ipc-server=%s" % self.ipc_path)
        device = os.environ.get("JMS_E2E_AUDIO_DEVICE")
        if device:
            lines.append("audio-device=%s" % device)
        for rel, text in self._files.items():
            with open(os.path.join(self.config_dir, rel), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
        with open(os.path.join(self.config_dir, "mpv.conf"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # -- lifecycle -------------------------------------------------------

    @timed("start")
    def start(self, timeout=60):
        self._seed()
        env = dict(os.environ)
        env["JMS_TEST_OBSERVE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
        env.update(self._extra_env)
        kwargs = {}
        if IS_WINDOWS:
            # Suspended until it is in the job: a venv's python.exe is a
            # launcher that spawns the real interpreter at once, and a child
            # started before the job is assigned escapes it.
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | 0x00000004)   # CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        self._stdout = open(os.path.join(self.config_dir, "app.stdout.txt"),
                            "ab")
        self.proc = subprocess.Popen(
            [sys.executable, RUN_PY, "--config", self.config_dir],
            cwd=REPO_ROOT, env=env, stdout=self._stdout,
            stderr=subprocess.STDOUT, **kwargs)
        if IS_WINDOWS:
            import ctypes
            k32, job = _windows_job()
            handle = int(self.proc._handle)
            if not k32.AssignProcessToJobObject(job, handle):
                self.proc.kill()
                raise AppError("could not put the app in a job object: %s"
                               % ctypes.get_last_error())
            ctypes.WinDLL("ntdll").NtResumeProcess(handle)
            self._job = (k32, job)
        self.incarnation += 1
        self._attach(timeout)
        return self

    def _endpoint_ready(self):
        if not IS_WINDOWS:
            return os.path.exists(self.ipc_path)
        import ctypes
        return bool(ctypes.windll.kernel32.WaitNamedPipeW(self.ipc_path, 0))

    def _attach(self, timeout):
        from python_mpv_jsonipc import MPV

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AppError("the app exited during startup (rc=%s); see %s"
                               % (self.proc.returncode, self.log_path))
            if self._endpoint_ready():
                try:
                    self.mpv = MPV(start_mpv=False, ipc_socket=self._ipc_name
                                   if IS_WINDOWS else self.ipc_path)
                    return
                except Exception:
                    pass
            time.sleep(0.2)
        raise AppError("no IPC endpoint at %s after %ss" % (self.ipc_path,
                                                           timeout))

    @property
    def log_path(self):
        return os.path.join(self.config_dir, "log.txt")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @timed("quit")
    def quit(self, timeout=30):
        """Close the window the way a person does, and require a clean exit.

        CLOSE_WIN, which is what the window manager's close button sends and
        what the app binds (player._on_close_win). NOT mpv's `quit` command:
        that bypasses the app's close handling entirely, and a scenario that
        quit that way once raced the timeline into the crash-recovery path
        and left the app running windowless -- a state no person can reach.

        Raises if the app is still running after ``timeout``: a hang on the
        way out is a bug, and killing it first would hide that."""
        try:
            self.mpv.command("keypress", "CLOSE_WIN")
        except Exception:
            pass
        self._detach()
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AppError("the app did not exit within %ss of quit"
                           % timeout)
        self._release_job()
        return rc

    def kill(self):
        """Emergency cleanup: every process the app started, gone."""
        self._detach()
        if self.proc is not None and self.proc.poll() is None:
            if IS_WINDOWS:
                self.proc.kill()
            else:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        elif not IS_WINDOWS and self.proc is not None:
            # The app is gone; a descendant (external mpv) may not be.
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._release_job()

    def close(self):
        self.kill()
        if not IS_WINDOWS:
            try:
                os.unlink(self.ipc_path)    # mpv leaves its socket behind
            except OSError:
                pass
        if getattr(self, "_stdout", None):
            self._stdout.close()
        # JMS_E2E_KEEP_CONFIG keeps it (with log.txt) for reading afterwards.
        if self.owns_dir and not os.environ.get("JMS_E2E_KEEP_CONFIG"):
            shutil.rmtree(self.config_dir, ignore_errors=True)

    def _detach(self):
        if self.mpv is not None:
            try:
                self.mpv.terminate()
            except Exception:
                pass
            self.mpv = None

    def _release_job(self):
        if self._job is not None:
            k32, job = self._job
            k32.CloseHandle(job)     # KILL_ON_JOB_CLOSE takes the rest
            self._job = None

    # -- input -----------------------------------------------------------

    def key(self, name):
        WATCH("key %s" % name)
        _count_key()
        self.mpv.command("keypress", name)

    def keys(self, *names):
        for n in names:
            self.key(n)

    _KEY_NAMES = {" ": "SPACE", "#": "SHARP", "\n": "ENTER"}
    #: Keys sent before waiting for them to land (see type_into).
    TYPE_CHUNK = 4

    def point(self, x, y):
        """Move the pointer to (x, y). mpv drops a move to where it already
        is, so nudge first: the move is then always an event."""
        WATCH("pointer to (%d, %d)" % (x, y))
        self.mpv.command("mouse", int(x) + 8, int(y) + 8)
        time.sleep(0.15)
        self.mpv.command("mouse", int(x), int(y))

    def press(self, button):
        """A button down and up where the pointer is, with no move."""
        WATCH("press %s" % button)
        self.mpv.command("keydown", button)
        time.sleep(0.1)
        self.mpv.command("keyup", button)

    def summon_hud(self, tries=3):
        """Bring the playback HUD up with the pointer, the way a hand does:
        a move, and another if nothing came. On jsonipc the FIRST move just
        after a video starts is sometimes dropped (the register,
        2026-09-27); a moving hand never notices, a single scripted move
        does."""
        f = self.frame() or {}
        w, h = f.get("w", 1280), f.get("h", 720)
        for n in range(tries):
            self.point(w / 2 + 20 * n, h / 2)
            try:
                return self.wait_for(lambda f: f.get("phud_shown"),
                                     timeout=2, what="the HUD")
            except AppError:
                if not self.alive():
                    raise
        raise AppError("the HUD never came up on %d pointer moves" % tries)

    def click(self, target, *a, **kw):
        """Narrated once in watch mode, however many keys or moves it
        takes (see _click)."""
        WATCH("click %s" % target)
        WATCH.depth += 1
        try:
            return self._click(target, *a, **kw)
        finally:
            WATCH.depth -= 1

    @timed("click")
    def _click(self, node_id, button="MBTN_LEFT", timeout=5):
        """Point at ``node_id``'s centre, wait until the renderer says the
        pointer is on it, then press: a click that proves where it went."""
        n = node(self.frame(), node_id)
        if n is None:
            raise AppError("%s is not on screen to click" % node_id)
        self.point(n["x"] + n["w"] / 2, n["y"] + n["h"] / 2)
        self.wait_for(lambda f: f.get("hover") == node_id, timeout=timeout,
                      what="the pointer to rest on %s" % node_id)
        self.press(button)

    def type(self, text, masked=False):
        """Type ``text`` key by key through mpv's input layer. ``masked``
        keeps it out of the watch-mode narration (a password)."""
        WATCH("type %s" % ("*" * len(text) if masked else repr(text)))
        WATCH.depth += 1
        try:
            self._type_keys(text)
        finally:
            WATCH.depth -= 1

    def _type_keys(self, text):
        for ch in text:
            self.key(self._KEY_NAMES.get(ch, ch))

    # -- observation -----------------------------------------------------

    @timed("type_into")
    def type_into(self, field, text, masked=False, timeout=10):
        """Focus text field ``field``, type ``text``, and wait until the field
        holds it -- before anything else is pressed. Typing then moving on at
        once let a TAB overtake the last characters on the jsonipc backend
        (a server URL arrived two digits short). A field that never reaches
        the text raises, naming what it holds: a lost keystroke is a finding,
        a late one is not.

        Typed a few keys at a time, each chunk waited for. mpv DROPS
        keypress commands past `input-key-fifo-size` (default 7) while its
        core is busy, silently -- a whole string sent at once lost keys
        mid-stream whenever a scene push landed during it. No person types
        that fast; the harness must not either (register, 2026-09-27)."""
        self.move_to(field)
        before = fields(self.frame()).get(field) or ""
        for end in range(self.TYPE_CHUNK, len(text) + self.TYPE_CHUNK,
                         self.TYPE_CHUNK):
            self.type(text[end - self.TYPE_CHUNK:end], masked=masked)
            shown = before + text[:end]
            want = "*" * len(shown) if masked else shown
            try:
                self.wait_for(lambda f: fields(f).get(field) == want,
                              timeout=timeout, what="a typed chunk")
            except AppError:
                break                       # reported below, with the trace
        want = "*" * len(before + text) if masked else before + text
        try:
            return self.wait_for(lambda f: fields(f).get(field) == want,
                                 timeout=timeout,
                                 what="%s to hold what was typed" % field)
        except AppError:
            f = self.frame() or {}
            got = fields(f).get(field)
            # What the renderer's text paths received, and when their
            # bindings came and went (renderer obs_key): the evidence for
            # where a lost key went.
            keys = [(round(k.get("t") or 0, 3), k.get("w"), k.get("d"))
                    for k in (f.get("keys") or [])][-48:]
            raise AppError("%s holds %r after typing %r: keystrokes lost\n"
                           "renderer key trace: %r"
                           % (field, got if not masked else
                              "<%d chars>" % len(got or ""),
                              text if not masked else "<%d chars>" % len(text),
                              keys))

    def prop(self, name):
        """An mpv property of the app's own player, over its IPC. None when
        mpv does not have it (yet): reading one is observation, not a step."""
        try:
            return self.mpv.command("get_property", name)
        except Exception:
            return None

    def playing_path(self, timeout=60):
        """What mpv was handed to play: its `path`, once there is one.

        Returns at once if something is ALREADY playing -- audio keeps
        playing across navigation -- so a second play in one test must wait
        for the path to change instead (TwinPlaylistsOnTwoServersTest)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = self.prop("path")
            if path:
                return path
            time.sleep(0.2)
        raise AppError("mpv was never given anything to play in %ss"
                       % timeout)

    def clear_field(self, field, timeout=10):
        """Empty text field ``field`` the way a person does: select all,
        then backspace."""
        self.move_to(field)
        self.key("ctrl+a")
        self.key("BS")
        return self.wait_for(lambda f: not fields(f).get(field),
                             timeout=timeout, what="%s to be empty" % field)

    def frame(self):
        """The renderer's last finished frame, or None before the first."""
        try:
            return self.mpv.command("get_property", "user-data/mpvtk/observe")
        except Exception:
            return None

    def history(self):
        try:
            return self.mpv.command("get_property",
                                    "user-data/mpvtk/observe_hist") or []
        except Exception:
            return []

    def after(self, rev, timeout=10):
        """The first frame newer than ``rev`` -- what a keypress produced."""
        return self.wait_for(lambda f: f.get("rev", 0) > rev, timeout=timeout,
                             what="a frame after rev %s" % rev)

    @timed("press_until")
    def press_until(self, key, predicate, what, timeout=30, retry_after=5):
        """Press ``key`` and wait for ``predicate``. If nothing it asked
        for happened within ``retry_after`` seconds, press it ONCE more and
        record the lost press in ``lost_keys``. A person presses again too;
        the record is what keeps that from hiding a real bug."""
        self.key(key)
        try:
            return self.wait_for(predicate, timeout=retry_after, what=what)
        except AppError:
            if not self.alive():
                raise
        self.lost_keys.append((key, what))
        self.key(key)
        try:
            return self.wait_for(predicate, timeout=timeout, what=what)
        except AppError as exc:
            # Where each press went: nav and the page, frame by frame.
            trail = [(h.get("rev"), h.get("nav"), h.get("top"))
                     for h in self.history()[-60:]]
            dedup = [t for i, t in enumerate(trail)
                     if i == 0 or t[1:] != trail[i - 1][1:]]
            raise AppError("%s\nframes (rev, nav, page): %r" % (exc, dedup))

    def move_to(self, target, *a, **kw):
        """Narrated once in watch mode, however many keys or moves it
        takes (see _move_to)."""
        WATCH("move focus to %s" % target)
        WATCH.depth += 1
        try:
            return self._move_to(target, *a, **kw)
        finally:
            WATCH.depth -= 1

    @timed("move_to")
    def _move_to(self, target, key=None, limit=60):
        """Put keyboard focus (nav) on ``target``.

        By arrows first, steering on the rects the observer publishes
        (`_steer`); TAB when steering cannot be trusted. Raises when neither
        gets there -- a target the keyboard cannot reach is a finding, and
        carrying on types into whatever has focus instead. ``key`` walks
        with that one key only."""
        f = self.frame() or {}
        if f.get("nav") == target:
            return f
        if key is None:
            f = self._steer(target)
            if f is not None:
                return f
        with step("move_to:tab"):
            return self._tab_walk(target, key, limit)

    #: Arrow presses before steering gives up to the TAB walk.
    STEER_LIMIT = 30

    def _steer(self, target):
        """UP/DOWN to the target's row, then LEFT/RIGHT along it: the
        renderer's spatial nav (mpvtk GUIDE.md section 2). Returns the
        frame with nav on ``target``, or None to fall back to TAB:

        * a text field, as target or focused: a focused field owns the
          arrows, and arrows reaching one only ring it, where TAB lands in
          it ready to type -- what type_into needs;
        * no rect for either end (nothing focused yet, a sizeless menu);
        * a press that moves nothing, or a node visited twice (a wrap or a
          row that does not overlap the way its rects say)."""
        seen = set()
        for _ in range(self.STEER_LIMIT):
            f = self.frame() or {}
            nav = f.get("nav")
            if nav == target:
                return f
            if f.get("focus") or target in fields(f) or nav in seen:
                return None
            seen.add(nav)
            cur, tgt = node(f, nav), node(f, target)
            if not cur or not tgt or any(
                    n.get(k) is None for n in (cur, tgt)
                    for k in ("x", "y", "w", "h")):
                return None
            if tgt["y"] >= cur["y"] + cur["h"]:
                k = "DOWN"
            elif tgt["y"] + tgt["h"] <= cur["y"]:
                k = "UP"
            elif tgt["x"] + tgt["w"] / 2 > cur["x"] + cur["w"] / 2:
                k = "RIGHT"
            else:
                k = "LEFT"
            self.key(k)
            try:
                self.wait_for(lambda f: f.get("nav") != nav, timeout=2,
                              what="focus to move")
            except AppError:
                if not self.alive():
                    raise
                return None
        return None

    def _tab_walk(self, target, key, limit):
        """TAB first, then shift+TAB: a long page (Home, with its rows) can
        put the top bar further away forwards than backwards. Each press
        waits briefly for focus to move and carries on without it."""
        for k in ((key,) if key else ("TAB", "shift+TAB")):
            for _ in range(limit):
                was = (self.frame() or {}).get("nav")
                self.key(k)
                # Wait for focus to MOVE, not for any newer frame: a focused
                # textbox's caret blink draws frames of its own, and one
                # drawn before the renderer handled this key read as "the
                # key did nothing", so the next press overshot by one (a
                # TAB meant for Settings landed on the first library).
                try:
                    f = self.wait_for(lambda f: f.get("nav") != was,
                                      timeout=2, what="focus to move")
                except AppError:
                    if not self.alive():
                        raise
                    continue
                if f.get("nav") == target:
                    return f
        raise AppError("the keyboard never reached %r (nav=%r)"
                       % (target, (self.frame() or {}).get("nav")))

    def wait_for(self, predicate, timeout=30, what="the condition"):
        """Poll frames until ``predicate(frame)`` is true; returns that frame.

        Each poll reads a *newer* frame than the last one checked, or waits.
        No sleep stands in for an event."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if not self.alive():
                raise AppError("the app exited while waiting for %s" % what)
            f = self.frame()
            if f and (last is None or f.get("rev") != last):
                last = f.get("rev")
                if predicate(f):
                    return f
            time.sleep(0.05)
        f = self.frame() or {}
        seen = [n["id"] for n in f.get("nodes", [])
                if n.get("id") and n.get("vis")
                and not n["id"].startswith("r.")]
        raise AppError("timed out after %ss waiting for %s; last frame "
                       "rev=%s nav=%r focus=%r modal=%r visible=%s"
                       % (timeout, what, last, f.get("nav"), f.get("focus"),
                          f.get("modal_open"), seen[:25]))


# -- frame helpers -------------------------------------------------------

def node(frame, node_id):
    for n in (frame or {}).get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None


def shown(frame, node_id):
    """The node is in the frame, inside its viewport, and not covered.

    A node of the open dialog (``mod``) is drawn above the dialog's own
    layer, which is itself registered as an occluder for everything under
    it; so occluders are not held against dialog nodes."""
    n = node(frame, node_id)
    if not n or not n.get("vis"):
        return False
    if n.get("mod"):
        return True
    cx, cy = n["x"] + n["w"] / 2, n["y"] + n["h"] / 2
    for o in frame.get("occluders", []):
        if o["x1"] <= cx <= o["x2"] and o["y1"] <= cy <= o["y2"]:
            return False
    return True


def texts(frame):
    return [n["text"] for n in (frame or {}).get("nodes", [])
            if n.get("text") and n.get("vis")]


def fields(frame):
    """{text field id: contents}. An empty Lua table arrives as a JSON list,
    so "no fields yet" is [] on the wire."""
    got = (frame or {}).get("fields") or {}
    return got if isinstance(got, dict) else {}
