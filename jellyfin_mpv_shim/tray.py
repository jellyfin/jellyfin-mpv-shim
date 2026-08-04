"""System tray icon (pystray / AppIndicator), decoupled from the Tk GUI.

The tray used to live alongside the old Tk browser process; the in-window
browser has no such process, so this stands alone (and must — it pulls
in Tk), so the tray lives here and either UI can own one.

**It runs in a separate PROCESS, not a thread.** pystray needs its own
process's main thread for its GTK/AppIndicator loop, and historically
pystray + libmpv in one process segfaults with GNOME AppIndicator — that is
the whole reason the original was a ``Process``. What lives in *this*
process is a small pump thread reading the child's command queue and
dispatching to callbacks.

Per the optional-dependency policy: a missing or broken pystray logs a
warning and leaves the app running headless-but-functional.
"""

import logging
import multiprocessing
import os
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

#: pystray backends that talk to a native tray API which is always there.
_NATIVE_BACKENDS = ("win32", "darwin")
#: ...ones that publish a StatusNotifierItem over D-Bus.
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

    ``None`` means the question could not be asked.

    This is the check pystray cannot do for us. An AppIndicator is a D-Bus
    object plus a registration call; with no watcher on the bus there is
    nobody to register with, so libappindicator quietly does nothing, the
    icon reports ``visible = True`` and no error is raised anywhere. That is
    GNOME's default state -- the shell has drawn no tray since 3.26 and the
    AppIndicator extension is what puts one back -- so on stock GNOME with
    libayatana-appindicator installed, "the tray started fine" is a lie, and
    the app would go on to hide itself behind an icon that does not exist.

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
        return False
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
    selection — the equivalent question for the GtkStatusIcon and Xorg
    backends. ``None`` if it could not be asked.

    Same shape of failure as the SNI one: docking an XEmbed window with no
    tray to dock into is not an error, it is an icon nobody sees.
    """
    try:
        from Xlib import X
        from Xlib import display as xdisplay
    except Exception:
        return None
    display = None
    try:
        display = xdisplay.Display()
        atom = display.intern_atom(
            "_NET_SYSTEM_TRAY_S%d" % display.get_default_screen())
        return display.get_selection_owner(atom) != X.NONE
    except Exception:
        log.debug("XEmbed tray probe failed", exc_info=True)
        return None
    finally:
        if display is not None:
            try:
                display.close()
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
    if backend in _SNI_BACKENDS:
        return (sni or sni_watcher_present)()
    if backend in _XEMBED_BACKENDS:
        return (xembed or xembed_tray_present)()
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
            # default=True makes this the click action, not just a menu entry:
            # clicking the icon is what people expect to reopen the window, and
            # having to right-click and pick from a list to get the app back is
            # a poor greeting. It stays in the menu as well.
            #
            # Honoured only where the backend can report a primary click --
            # pystray's Icon.HAS_DEFAULT_ACTION. That is win32, gtk and xorg;
            # appindicator and darwin both set it False. So in practice this
            # helps on Windows, and on Linux only under PYSTRAY_BACKEND=gtk or
            # xorg.
            #
            # It is not a pystray shortcoming on appindicator: the Indicator
            # GObject exposes no signals at all (only a *secondary* activate
            # target, i.e. middle click), so there is no primary click to hook.
            # StatusNotifierItem does define Activate, but libappindicator
            # never surfaces it -- which is why Qt apps on the same desktop can
            # tell the buttons apart and this cannot.
            #
            # Where it isn't honoured this is simply the first menu entry, so
            # there is nothing to guard.
            MenuItem(_("Show Library Browser"), send("show"), default=True),
            MenuItem(_("Configure Servers"), send("show_preferences")),
            MenuItem(_("Show Console"), send("show_console")),
            MenuItem(_("Open Config Folder"), send("open_config")),
            MenuItem(_("Quit"), die),
        ]

        icon = Icon(APP_NAME, title=USER_APP_NAME, menu=Menu(*menu_items))
        try:
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

        def vanished(*_a):
            log.warning("The StatusNotifier host went away; "
                        "the tray icon is no longer displayed.")
            self.r_queue.put(("tray_died", "watcher_gone"))

        try:
            # Held on self so it outlives this call: dropping the watcher id
            # is how a Gio name watch gets garbage collected out from under
            # the loop it was registered on.
            self._name_watch = Gio.bus_watch_name(
                Gio.BusType.SESSION, SNI_WATCHER,
                Gio.BusNameWatcherFlags.NONE, appeared, vanished)
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
            try:
                command, param = self._queue.get(timeout=0.5)
            except Exception:
                continue  # Empty, or the queue died with the child
            self.dispatch(command, param)

    #: Why the child says there is no tray -> what to tell the user. The
    #: default ("pystray failed to import or start") was the only case there
    #: used to be, and it is the wrong story for a desktop that simply draws
    #: no tray: nothing is missing or broken there, and telling someone to
    #: reinstall a package they already have sends them the wrong way.
    _DEATH_REASONS = {
        "not_rendered": "nothing on this desktop displays it",
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

    def stop(self):
        self._halt.set()
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                log.debug("tray terminate failed", exc_info=True)
            self._process = None
