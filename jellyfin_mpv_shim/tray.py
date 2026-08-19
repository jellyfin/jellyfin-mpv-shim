"""System tray icon (pystray / AppIndicator).

**It runs in a separate PROCESS, not a thread**, because pystray needs its
own process's main thread for its GTK/AppIndicator loop and pystray + libmpv
in one process segfaults with GNOME AppIndicator. What lives in *this*
process is a small pump thread reading the child's command queue.

Per the optional-dependency policy: a missing or broken pystray logs a
warning and leaves the app running headless-but-functional.

The whole design — the probes below, the icon size, the Wayland forcing and
the escalating stop — is written up in docs/architecture.md section 3.
"""

import logging
import multiprocessing
import os
import signal
import sys
import threading
from multiprocessing import Process, Queue

from .constants import APP_NAME, USER_APP_NAME
from .i18n import _
from .utils import get_resource

log = logging.getLogger("tray")


#: The bus name every StatusNotifierItem host registers, whoever wrote it.
#: Ayatana kept KDE's spelling, and GNOME's "AppIndicator and
#: KStatusNotifierItem Support" extension takes the same name -- which is
#: precisely why its absence is a usable answer.
SNI_WATCHER = "org.kde.StatusNotifierWatcher"

#: What sni_watcher_present() returns when NOBODY owns the watcher name, as
#: opposed to a watcher being there with no host behind it. Falsy, so every
#: "is there a StatusNotifier tray" test reads the same as before -- but
#: distinguishable, because this case and no other starts libappindicator's
#: GtkStatusIcon fallback (docs/architecture.md section 3.2).
class _NoWatcher(int):
    def __repr__(self):
        return "NO_WATCHER"


NO_WATCHER = _NoWatcher(0)

#: pystray backends that talk to a native tray API which is always there.
_NATIVE_BACKENDS = ("win32", "darwin")
#: ...ones that publish a StatusNotifierItem over D-Bus -- and, when nothing
#: is listening for one, quietly dock a GtkStatusIcon instead, which is why
#: tray_will_render has to ask both questions for these.
_SNI_BACKENDS = ("appindicator", "ayatana_appindicator")
#: ...and ones that dock an XEmbed window into an X11 system tray.
_XEMBED_BACKENDS = ("gtk", "xorg")


def backend_name(icon_cls):
    """The short name of the pystray backend behind ``Icon``.

    pystray picks a backend at import time and exposes it only as the module
    the ``Icon`` class came from (``pystray._appindicator``); there is no
    public accessor. Returns "" if it cannot be read, which every caller
    treats as "unknown backend, assume it works".
    """
    return getattr(icon_cls, "__module__", "").rsplit(".", 1)[-1].lstrip("_")


def sni_watcher_present(timeout_ms=2000):
    """Whether a StatusNotifierItem host is listening on the session bus.

    ``None`` means the question could not be asked; ``NO_WATCHER`` means
    nobody owns the name at all, which is a different answer from a watcher
    with no host behind it. This is the check pystray cannot do for us --
    ``visible = True`` says the icon object exists, not that anything drew
    it. See docs/architecture.md section 3.2 for the three outcomes.

    Uses GDBus through PyGObject, which the backends this matters for
    already require, rather than adding a D-Bus dependency of our own.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        log.debug("no PyGObject; cannot probe for a StatusNotifier host",
                  exc_info=True)
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        owned = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (SNI_WATCHER,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE,
            timeout_ms, None).unpack()[0]
    except Exception:
        log.debug("StatusNotifierWatcher probe failed", exc_info=True)
        return None
    if not owned:
        # No watcher on the bus at all. libappindicator's GtkStatusIcon
        # fallback engages in exactly this case and no other, which is why
        # tray_will_render distinguishes it below.
        return NO_WATCHER
    # A watcher with no host registered is a watcher nothing draws for. Rare,
    # but it is the difference between "the extension is installed" and "the
    # extension is enabled". Failing open here: the name IS owned, so someone
    # is answering, and a property we could not read is not evidence against
    # them.
    try:
        return bool(bus.call_sync(
            SNI_WATCHER, "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (SNI_WATCHER,
                                  "IsStatusNotifierHostRegistered")),
            GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE,
            timeout_ms, None).unpack()[0])
    except Exception:
        log.debug("IsStatusNotifierHostRegistered unreadable", exc_info=True)
        return True


def xembed_tray_present():
    """Whether an X11 system tray owns the ``_NET_SYSTEM_TRAY_S<n>``
    selection. ``None`` if it could not be asked.

    Same shape of failure as the SNI one: docking an XEmbed window with no
    tray to dock into is not an error, it is an icon nobody sees. Asked of
    the XEmbed backends and of the AppIndicator ones, which fall back to
    docking when no StatusNotifier host exists -- see ``tray_will_render``.
    """
    if not os.environ.get("DISPLAY"):
        return False        # no X server to hold the selection at all
    # ctypes against libX11 rather than python-xlib, which is not a
    # dependency of ours or of the backends that need this answer, and
    # rather than GDK, which cannot give it: gdk_selection_owner_get_for_
    # display resolves the owner window through GDK's own table and returns
    # NULL for a window belonging to another client -- which every tray is.
    # Verified against a real i3bar: Xlib says 0x0010000d, GDK says None.
    x11 = None
    try:
        import ctypes
        import ctypes.util

        # By SONAME first. ctypes.util.find_library shells out to
        # `ldconfig -p` on every call -- a fork out of the tray's GTK main
        # loop, since the watch callbacks reach here -- and in a bundle with
        # no ldconfig and no compiler it returns None, which would make this
        # answer "cannot ask" inside a process that has libX11 mapped
        # already. That is the i3 case this exists for, silently reverted.
        for name in ("libX11.so.6", "libX11.so", "libX11.6.dylib"):
            try:
                x11 = ctypes.CDLL(name)
                break
            except OSError:
                continue
        if x11 is None:
            found = ctypes.util.find_library("X11")
            x11 = ctypes.CDLL(found) if found else None
    except Exception:
        x11 = None
    if x11 is None:
        log.debug("no libX11; cannot probe for an XEmbed tray")
        return None
    display = None
    try:
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.c_int]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XGetSelectionOwner.restype = ctypes.c_ulong
        x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(None)
        if not display:
            # A confident no even though this covers auth failure as well
            # as "no server". GTK in this same process opens the same
            # display for the icon itself, so if we cannot reach it neither
            # can the thing that would draw.
            return False
        selection = b"_NET_SYSTEM_TRAY_S%d" % x11.XDefaultScreen(display)
        # only_if_exists: nobody has ever docked here if the atom is unknown
        atom = x11.XInternAtom(display, selection, 1)
        return bool(atom) and x11.XGetSelectionOwner(display, atom) != 0
    except Exception:
        log.debug("XEmbed tray probe failed", exc_info=True)
        return None
    finally:
        if display:
            try:
                x11.XCloseDisplay(display)
            except Exception:
                pass


def tray_will_render(backend, sni=None, xembed=None):
    """Whether an icon on ``backend`` will actually reach a screen.

    ``None`` means "could not tell", and every caller must read that as
    *yes* — a probe that cannot run has no business taking a working tray
    away from someone. Only a confident ``False`` changes behaviour.

    The probes are injectable so this stays answerable without a desktop.
    """
    if backend in _NATIVE_BACKENDS:
        return True
    if backend == "dummy":
        # pystray's own "there is no tray here" backend.
        return False
    if backend in _XEMBED_BACKENDS:
        return (xembed or xembed_tray_present)()
    if backend in _SNI_BACKENDS:
        watcher = (sni or sni_watcher_present)()
        if watcher:
            return True
        if watcher is False:
            # A watcher IS on the bus, with no host registered behind it.
            # The item will register with it and never fall back, so an
            # XEmbed tray on the same desktop is irrelevant -- asking would
            # turn an invisible icon into a confident yes.
            return False
        # No watcher is NOT the end of the story, and reading it that way is
        # what made this wrong on X11 (#4). libappindicator and its
        # ayatana fork both keep a GtkStatusIcon fallback -- see
        # `start_fallback_timer` in libayatana-appindicator3 -- and use it
        # exactly when no StatusNotifierWatcher owns the name. So on a
        # desktop with an old-style XEmbed tray and no D-Bus host (i3 with
        # i3bar's tray, xfce4-panel, tint2, most of X11 that is not KDE)
        # the icon appears perfectly well, and the app was offering
        # "Keep Running in Background" to people who had a working tray in
        # front of them. Confirmed by watching the icon dock into i3bar
        # while the watcher name was unowned.
        fallback = (xembed or xembed_tray_present)()
        if fallback:
            return True
        # Only now, and only if both probes actually ran.
        return False if watcher is not None and fallback is False else None
    return None


def tray_unavailable_advice(env=None):
    """One line telling the user how to get the app back, tailored to the
    desktop they are on. A tray that does not appear is only a problem
    because of what the app does about it, so the message has to name the
    way out, not just the diagnosis."""
    desktop = ((env if env is not None else os.environ)
               .get("XDG_CURRENT_DESKTOP") or "")
    if "gnome" in desktop.lower():
        return _(
            "GNOME does not draw a system tray on its own. Install the "
            "\"AppIndicator and KStatusNotifierItem Support\" extension to "
            "get one, or turn on \"Allow Background\" in Settings to run "
            "without a window anyway.")
    return _(
        "No system tray is running on this desktop. Turn on \"Allow "
        "Background\" in Settings to run without a window anyway.")


def wants_x11_backend(env):
    """Whether to force GTK onto X11 (XWayland) for the tray process.

    GNOME's Wayland session only: pystray's GTK loop crashes there at
    startup, and forcing the X11 backend dodges it (#506). Forcing it
    *everywhere* is what #646 is -- on every other Wayland compositor the
    indicator then reports ``visible = True``, raises nothing, and simply
    never registers with the StatusNotifierWatcher, so the tray silently
    does not appear. Wayfire + wf-panel-pi was the report; the same code
    registers immediately with the backend left alone.

    So both halves have to hold. GNOME on X11 needs nothing forced (it is
    already there), and a non-GNOME Wayland session must be left to use its
    own backend. ``XDG_CURRENT_DESKTOP`` is a colon-separated list and names
    GNOME variously ("GNOME", "ubuntu:GNOME", "GNOME-Classic:GNOME"), hence
    the substring test rather than an equality one.

    Takes the environment as an argument so it is answerable without one.
    """
    desktop = env.get("XDG_CURRENT_DESKTOP") or ""
    if "gnome" not in desktop.lower():
        return False
    return bool(env.get("WAYLAND_DISPLAY")) or (
        (env.get("XDG_SESSION_TYPE") or "").lower() == "wayland")


def _reset_inherited_signals():
    """Put this child's signal dispositions back to the default.

    **SIGTERM** is the one that matters, and only a *forked* child has it to
    undo. ``mpv_shim._claim_sigterm`` installs a handler that sets an
    ``Event`` the run loop waits on; a fork copies it, and in the child that
    ``Event`` is a private copy nothing reads -- so ``TrayManager.stop()``,
    which is a ``terminate()``, i.e. a SIGTERM, is *received and ignored*,
    and the tray outlives the app until somebody SIGKILLs it. The app asks
    for ``spawn`` precisely so this cannot happen
    (``mpv_shim._use_spawn_start_method``); this is the layer under that,
    because the failure is a stray process and the cause is two files away.

    **SIGINT** is reset in both kinds of child, and is cosmetic. Every child
    arrives with CPython's ``default_int_handler`` (measured, fork and spawn
    alike), so a Ctrl-C -- which the terminal sends to the whole process
    group -- raises ``KeyboardInterrupt`` from wherever the GTK loop happens
    to be and prints a traceback on the way out. The parent's own shutdown
    is what stops the tray; the child just needs to go quietly.
    """
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or a platform that will not take it.
            log.debug("could not reset %s in the tray child", name,
                      exc_info=True)


class TrayProcess(Process):
    """The pystray loop. Everything it can do is "put a command name on the
    queue" — it holds no references to the player or the browser, because
    with the 'spawn' start method it is a fresh interpreter anyway."""

    def __init__(self, r_queue: "Queue"):
        self.r_queue = r_queue
        self.icon_stop = None
        # Gio.bus_watch_name id; held so the watch is not collected out from
        # under the loop it was registered on. See _watch_for_tray.
        self._name_watch = None
        Process.__init__(self, daemon=True, name="jellyfin-mpv-shim-tray")

    def run(self):
        # First, before anything can be interrupted or asked to stop: a
        # forked child arrives holding the parent's handlers, and one of
        # them makes SIGTERM a no-op here. See _reset_inherited_signals.
        _reset_inherited_signals()

        # These variables only mean anything to GTK on Linux/BSD; pystray
        # uses native APIs on Windows and macOS, so leave the env alone.
        if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
            if wants_x11_backend(os.environ):
                os.environ.pop("WAYLAND_DISPLAY", None)
                os.environ["GDK_BACKEND"] = "x11"

        # Spawned child: it never ran main(), so gettext is unconfigured and
        # the menu would come out untranslated.
        try:
            from . import i18n

            i18n.configure()
        except Exception:
            log.debug("tray i18n setup failed", exc_info=True)

        try:
            from PIL import Image
            from pystray import Icon, Menu, MenuItem
        except Exception as e:
            log.error("Failed to import pystray: %s", e)
            self.r_queue.put(("tray_died", None))
            return

        def send(command):
            def wrapper():
                self.r_queue.put((command, None))

            return wrapper

        def die():
            # icon.stop() crashes on Linux, so let the parent tear us down.
            if sys.platform == "linux":
                self.r_queue.put(("quit", None))
            else:
                self.icon_stop()

        menu_items = [
            # default=True makes this the CLICK ACTION, not just a menu
            # entry: clicking the icon is what people expect to reopen the
            # window. Honoured only where the backend reports a primary
            # click (Icon.HAS_DEFAULT_ACTION -- win32, gtk, xorg); elsewhere
            # it is simply the first menu entry, so there is nothing to
            # guard. Why appindicator and darwin cannot:
            # docs/architecture.md section 3.1.
            MenuItem(_("Show Library Browser"), send("show"), default=True),
            MenuItem(_("Configure Servers"), send("show_preferences")),
            MenuItem(_("Show Console"), send("show_console")),
            MenuItem(_("Open Config Folder"), send("open_config")),
            MenuItem(_("Quit"), die),
        ]

        icon = Icon(APP_NAME, title=USER_APP_NAME, menu=Menu(*menu_items))
        try:
            # The source is 128px: every pystray backend produces the size
            # IT wants, so larger than the panel is the supported direction
            # and smaller is what cannot be recovered from. The artwork is
            # integration/jellyfin-128.png, the same mark on transparency --
            # logo.png is NOT interchangeable, its opaque dark background is
            # a square tile in a light panel. Per-backend sizing behaviour:
            # docs/architecture.md section 3.1.
            icon.icon = Image.open(get_resource("systray.png"))
        except Exception:
            log.debug("tray icon image missing", exc_info=True)
        self.icon_stop = icon.stop

        backend = backend_name(Icon)

        def setup(tray_icon):
            tray_icon.visible = True
            # `visible = True` is pystray telling us the icon object exists,
            # not that anything drew it -- see sni_watcher_present. Ask the
            # desktop directly before claiming a tray the user does not have.
            # Done here rather than before run() so the answer is as late as
            # it can be, which matters at login: we may well have started
            # before the shell extension that owns the watcher.
            renders = tray_will_render(backend)
            if renders is False:
                log.warning("The system tray icon will not be displayed. %s",
                            tray_unavailable_advice())
                self.r_queue.put(("tray_died", "not_rendered"))
            else:
                self.r_queue.put(("ready", None))
            # ...and keep asking. An autostarted copy can lose the race with
            # the extension that registers the watcher, and a shell restart
            # takes the watcher away and brings it back; libappindicator
            # (re-)registers on its own when it returns, so both transitions
            # are ours to report rather than to have been wrong about once.
            self._watch_for_tray(backend)

        try:
            icon.run(setup=setup)
        except Exception:
            log.error("System tray failed to start.", exc_info=True)
            self.r_queue.put(("tray_died", None))
            return
        # icon.run only returns on a clean stop (Quit on Windows/macOS).
        self.r_queue.put(("quit", None))

    def _watch_for_tray(self, backend):
        """Follow the StatusNotifier host coming and going, for as long as
        the tray loop runs.

        Only the SNI backends: this rides pystray's own GLib main loop, and
        the XEmbed ones have no equally cheap way to be told. Failing to set
        the watch up is not fatal -- the startup answer stands.
        """
        if backend not in _SNI_BACKENDS:
            return
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except Exception:
            return

        def appeared(*_a):
            # Re-run the full probe rather than trusting the name: the watch
            # only reports ownership, while the probe also asks whether a
            # host is registered behind it. Reporting "ready" straight off
            # the name would overwrite the more careful startup answer with
            # a less careful one.
            if tray_will_render(backend) is False:
                return
            log.info("A StatusNotifier host appeared; the tray icon is live.")
            self.r_queue.put(("ready", None))

        seen_host = [False]

        def appeared_seen(*_a):
            seen_host[0] = True
            appeared()

        def vanished(*_a):
            if not seen_host[0]:
                # GLib calls this at registration when the name is already
                # unowned, which on the desktops this fallback exists for
                # (i3, xfce with only a systray) is every launch. Nothing
                # went away; there was never a host.
                return
            # The full probe again, for the same reason `appeared` re-runs
            # it: losing the D-Bus host is not losing the tray if this
            # desktop also has an XEmbed one, and libappindicator falls
            # back to it by itself. Saying otherwise would take Close to
            # Tray away from a user whose icon is still on screen.
            if tray_will_render(backend) is not False:
                log.info("The StatusNotifier host went away; the icon falls "
                         "back to the desktop's own tray.")
                return
            log.warning("The StatusNotifier host went away; "
                        "the tray icon is no longer displayed.")
            self.r_queue.put(("tray_died", "watcher_gone"))

        try:
            # Held on self so it outlives this call: dropping the watcher id
            # is how a Gio name watch gets garbage collected out from under
            # the loop it was registered on.
            self._name_watch = Gio.bus_watch_name(
                Gio.BusType.SESSION, SNI_WATCHER,
                Gio.BusNameWatcherFlags.NONE, appeared_seen, vanished)
        except Exception:
            log.debug("could not watch for a StatusNotifier host",
                      exc_info=True)


class TrayManager:
    """Owns the tray process and pumps its commands to ``handlers``.

    ``handlers`` maps the command names the child emits ("show",
    "show_preferences", "show_console", "open_config", "quit") to callables.
    Unknown commands are ignored, so the child and the parent can disagree
    about the menu without crashing either -- which is what lets an older
    installed copy of the child keep working after a menu entry is removed.
    """

    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.ready = threading.Event()
        self.available = False
        # None until the child has said either way; see dispatch.
        self._reported = None
        self._queue = None
        self._process = None
        self._thread = None
        self._halt = threading.Event()

    def start(self):
        try:
            self._queue = multiprocessing.Queue()
            self._process = TrayProcess(self._queue)
            self._process.start()
        except Exception:
            log.warning("Could not start the system tray.", exc_info=True)
            self._process = None
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="tray-pump")
        self._thread.start()
        return True

    def _pump(self):
        while not self._halt.is_set():
            queue = self._queue
            if queue is None:
                return              # stop() released it; nothing left to read
            try:
                command, param = queue.get(timeout=0.5)
            except Exception:
                # Empty, or the queue died with the child. A child that
                # CRASHED sends nothing, and a GTK process has ways to die
                # that do not run our code (Xlib's I/O error handler calls
                # exit(), GDK's calls _exit()) -- so without this the app
                # goes on hiding behind an icon nobody can click, which is
                # its only way back on screen.
                if (self._process is not None and self.available
                        and not self._process.is_alive()):
                    self.dispatch("tray_died", "child_gone")
                continue
            self.dispatch(command, param)

    #: Why the child says there is no tray -> what to tell the user. The
    #: default ("pystray failed to import or start") was the only case there
    #: used to be, and it is the wrong story for a desktop that simply draws
    #: no tray: nothing is missing or broken there, and telling someone to
    #: reinstall a package they already have sends them the wrong way.
    _DEATH_REASONS = {
        "not_rendered": "nothing on this desktop displays it",
        "child_gone": "the tray process exited",
        "watcher_gone": "the desktop's tray host went away",
    }

    def dispatch(self, command, param=None):
        """Apply one command from the tray child. Never raises: a broken
        handler must not take the pump (and with it the whole tray) down."""
        # Both states can now be reported repeatedly (the child follows the
        # tray host coming and going), so log on the transitions only --
        # otherwise a shell that restarts a few times fills the log.
        if command == "ready":
            if self._reported is not True:
                log.info("System tray is up.")
            self._reported = True
            self.available = True
            self.ready.set()
            return
        if command == "tray_died":
            if self._reported is not False:
                log.warning("System tray is unavailable (%s).",
                            self._DEATH_REASONS.get(
                                param, "missing pystray/AppIndicator"))
            self._reported = False
            self.available = False
            self.ready.set()   # unblock anyone waiting, don't hang
            return
        handler = self.handlers.get(command)
        if handler is None:
            log.debug("tray: no handler for %r", command)
            return
        try:
            handler()
        except Exception:
            log.error("tray handler %r failed", command, exc_info=True)

    #: How long the child gets to act on the terminate() before it is
    #: killed. Short on purpose: this runs inside the shutdown sequence,
    #: which has its own deadline, and there is nothing the child can be
    #: doing that is worth waiting on -- it holds no state of ours.
    TERMINATE_GRACE = 2.0

    def stop(self):
        """Terminate the tray child, and make sure it is really gone.

        ``terminate()`` alone is a *request* -- a SIGTERM, which a forked
        child can be holding a handler for (see
        ``_reset_inherited_signals``) -- and the parent then ``os._exit``s
        without reaping it. So the request is checked and escalated. Both
        waits are bounded and neither failure is fatal: a child that
        outlives even this is worth a line in the log, not a shutdown that
        stalls.
        """
        self._halt.set()
        process, self._process = self._process, None
        if process is None:
            self._release_queue()
            return
        try:
            process.terminate()
            process.join(self.TERMINATE_GRACE)
            if process.is_alive():
                log.warning("The system tray process ignored SIGTERM; "
                            "killing it.")
                process.kill()
                process.join(self.TERMINATE_GRACE)
            if process.is_alive():
                log.warning("The system tray process (pid %s) could not be "
                            "stopped.", process.pid)
        except Exception:
            log.debug("tray terminate failed", exc_info=True)
        self._release_queue()

    def _release_queue(self):
        """Drop the command queue once nothing is left to send on it.

        Closed rather than left to multiprocessing's resource tracker,
        whose stderr notice would otherwise be the last thing printed on
        every quit (``exit_watchdog.finish`` leaves by ``os._exit``).
        ``cancel_join_thread`` because anything still unsent is a command
        for a child that is already gone; the pump is joined first, bounded
        by one poll interval, since it reads this queue.
        """
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(1.0)
        queue, self._queue = self._queue, None
        if queue is None:
            return
        try:
            queue.cancel_join_thread()
            queue.close()
        except Exception:
            log.debug("tray queue close failed", exc_info=True)
