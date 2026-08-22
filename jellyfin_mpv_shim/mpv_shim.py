#!/usr/bin/env python3

import hashlib
import logging
import os
import platform
import signal
import sys
import multiprocessing
from threading import Event

from . import conffile
from . import i18n
from .args import get_args
from .conf import settings
from .constants import APP_NAME
from .log_utils import (
    configure_log,
    configure_log_file,
    enable_sanitization,
    root_logger,
)

logging.getLogger("requests").setLevel(logging.CRITICAL)


def scratch_namespace():
    """The directory this instance's scratch caches live in, inside whichever
    base is chosen.

    Everything in it is reclaimable by whoever holds it, so the name is keyed
    on exactly what bounds that claim: the config directory (what the
    single-instance lock covers) and the host (a home directory can be
    shared, and ``flock`` is host-local on plenty of network filesystems).
    Hashed because this is an identity, not a label; the cost is that a
    machine which renames itself abandons its old namespace. Derivation in
    docs/architecture.md section 2.3.
    """
    key = "%s\0%s" % (platform.node(),
                      os.path.abspath(conffile.confdir(APP_NAME)))
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    return "%s.%s" % (APP_NAME, digest[:8])


def _claim_sigterm(halt):
    """Make SIGTERM run the orderly shutdown, and take the signal before SDL
    can.

    Two things at once. With no handler, `kill` skipped the whole shutdown
    sequence -- no final progress report, no geometry saved, no credentials
    flushed -- and `kill` is what systemd and a session logout send.
    Installing one is also what KEEPS it: SDL replaces only a handler still
    at ``SIG_DFL``, so SIGTERM at CPython's default was taken by SDL, turned
    into an ``SDL_QUIT``, and dropped by mpv's gamepad loop.

    Ordering is why this lives here rather than beside the mpv construction:
    it must be in place before SDL initialises, and `playerManager` is a
    module-level singleton whose *import* creates mpv, so every import that
    reaches it is below this line. The main thread is also the only thread
    `signal.signal` may be called from, and this is it.

    Sets the event the run loop waits on rather than exiting: everything that
    makes a shutdown orderly happens in `main`'s `finally`, and
    `exit_watchdog` is already armed there to force the exit if a step wedges.

    The measurements, and the ``SDL_NO_SIGNAL_HANDLERS`` layer that remains
    underneath for the external backend's child mpv, are in
    docs/architecture.md section 2.1 and docs/mpv-backends.md section 4.
    """
    def on_sigterm(signum, frame):
        # No logging from inside a signal handler -- it can land while the
        # logging lock is held on another thread, and a deadlock here is a
        # process that cannot be stopped, which is the bug being fixed.
        halt.set()

    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError, AttributeError):
        # Not the main thread, or a platform without SIGTERM. Nothing here is
        # required for correct operation on a machine that never sends one.
        #
        # `root_logger`, not `log`: that name is a LOCAL inside main(), and
        # reaching for it here raised NameError on exactly the platforms this
        # branch exists to tolerate -- i.e. the handler failing to install
        # would have taken startup down with it.
        root_logger.debug("Could not install a SIGTERM handler.",
                          exc_info=True)


def _use_spawn_start_method():
    """Force 'spawn' for the tray child, whatever already resolved a context.

    - macOS: avoids Objective-C fork crashes with GUI frameworks (3.14's
      'forkserver' also crashes with Obj-C, issue #473).
    - Linux/Windows: the child is started *after* the timeline/action/sync
      worker threads, so a plain fork can inherit a held lock (e.g. logging)
      and deadlock the child. 'spawn' gives a clean interpreter; the child
      relies only on its IPC-supplied options, not on inherited globals.

    ``force=True`` because a context resolves on the first *read*, not only
    on a set, and ``run.py``'s ``freeze_support()`` had already pinned it to
    **fork** -- so without it this call silently did nothing when launched
    from source or from the frozen Windows build. A forked child inherits
    our SIGTERM handler and therefore swallows every ``TrayManager.stop()``;
    `tray._reset_inherited_signals` and the escalation in that method are
    the layers under this. See docs/architecture.md section 2.2.

    Reported rather than merely attempted: a start method that is not spawn
    is a real hazard on every platform, and the failure is silent.
    """
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except (RuntimeError, ValueError):
        # A platform without a spawn context. Nothing here is required for
        # correct operation; the tray child is simply started the other way.
        root_logger.debug("Could not select the spawn start method.",
                          exc_info=True)
    # allow_none=False: if the set above failed, the default is what the
    # children will actually be started with, and naming it is the point.
    actual = multiprocessing.get_start_method()
    if actual != "spawn":
        root_logger.warning(
            "Child processes will be started with the %r method, not "
            "'spawn'. They inherit this process's signal handlers and "
            "locks, which can leave the system tray process behind on "
            "the way out.", actual)


def main():
    args = get_args()

    conf_file = conffile.get(APP_NAME, "conf.json")
    load_success = settings.load(conf_file)
    i18n.configure()

    # CLI overrides applied after config load so they win.
    if args.enable_gui is not None:
        settings.enable_gui = args.enable_gui
    if args.start_minimized is not None:
        settings.start_minimized = args.start_minimized
    if args.mpv_loglevel is not None:
        settings.mpv_log_level = args.mpv_loglevel
    if args.ui_scale is not None:
        # In-memory only: settings.save() elsewhere would otherwise persist
        # a scale the user asked for on ONE run. Resolved on the mpvtk
        # ready event (app._resolve_scale), which reads settings.ui_scale.
        settings.ui_scale = args.ui_scale

    if settings.sanitize_output:
        enable_sanitization()

    # Trickplay frame files are named per generation and unlinked on the way
    # out; a crash or a kill leaves them behind, and nothing else collects
    # them. Cheap, and it runs before any player exists.
    try:
        from .trickplay import cleanup_stale_files

        cleanup_stale_files()
    except Exception:
        pass

    app_log_level = "debug" if args.debug else settings.mpv_log_level
    configure_log(sys.stdout, app_log_level)
    if settings.write_logs:
        log_file = conffile.get(APP_NAME, "log.txt")
        configure_log_file(log_file, app_log_level)

    log = root_logger

    # Before anything can import PIL.ImageFont, and after logging so the
    # result is recorded. Pillow resolves FriBiDi once, at extension init,
    # and a Windows build without it silently draws right-to-left text
    # unshaped and measures every string unkerned -- see win_fribidi.
    from .win_fribidi import describe, preload

    preload()
    log.info(describe())

    # Before anything builds a player: the settings this clears are applied
    # while one is being constructed, so clearing them afterwards would not
    # help the launch that had to ask for it.
    if args.reset_shaders:
        from .video_profile import reset_saved_shader_settings

        changed = reset_saved_shader_settings()
        if changed:
            for key, old in changed:
                log.info("Reset %s (was %s).", key, old)
        else:
            log.info("Shader settings were already at their defaults.")

    # `kill -USR1 <pid>` dumps every thread's stack. The only time a hang is
    # diagnosable is while it is hanging, and by then it is too late to add
    # instrumentation.
    from .exit_watchdog import enable_manual_dumps

    enable_manual_dumps()

    _use_spawn_start_method()

    from .single_instance import SingleInstance

    # `stop` is a request to the *other* process, so it must run before we try
    # to become the primary ourselves — and before any of the services below
    # start, since this launch is never going to play anything.
    if "stop" in args.command:
        single = SingleInstance()
        if single.request_stop():
            log.info("Asked the running instance to shut down.")
            return
        if single.is_running():
            log.error("%s is running but did not respond to the stop request. "
                      "It may be wedged; terminate it manually.", APP_NAME)
            sys.exit(1)
        log.info("%s is not running.", APP_NAME)
        return

    # If we're not the first launch, ask the running instance to surface its
    # window (un-minimize) and exit, rather than starting a second copy.
    single = SingleInstance()
    if not single.acquire():
        if args.reset_shaders:
            # That copy loaded the old values at startup and will write them
            # back when it next saves, quietly undoing this.
            log.warning(
                "The running copy still has the old shader settings loaded. "
                "Run `%s stop` and start it again for the reset to stick.",
                APP_NAME)
        log.info("Another instance is already running; exiting.")
        return

    # Created before anything can request a stop, so a `stop` arriving during
    # startup is honoured rather than acknowledged and dropped.
    halt = Event()
    single.on_stop = halt.set
    _claim_sigterm(halt)

    # Give this configuration's scratch caches their own directory, and with
    # it the right to reclaim everything already in it -- but only against a
    # lock that was really taken, since acquire() fails open and a copy that
    # merely *believes* it is alone would reclaim the first one's cache out
    # from under it. See set_instance_namespace and docs/architecture.md 2.3.
    from .mpvtk.rawimage import set_instance_namespace

    if single.holds_lock:
        set_instance_namespace(scratch_namespace())

    user_interface = None
    use_gui = False
    if settings.enable_gui:
        try:
            # The browser rasterizes tiles with Pillow; probe it here so a
            # missing optional dep degrades to the CLI with one clear
            # message, rather than failing somewhere deep in a view.
            import PIL  # noqa: F401
            from .mpvtk_browser.ui import user_interface

            use_gui = True
        except Exception:
            log.warning(
                "Cannot load the library browser (is Pillow installed?). "
                "Falling back to the command line interface.",
                exc_info=True,
            )
            # Same landing place as the no-lua fallback below, and it needs
            # the same thing: with the browser gone nothing sets
            # `on_hud_menu`, and `toggle_settings_menu` refuses the OSD
            # menu while the resolved style is "mpvtk" -- so without this
            # a machine that merely lacks Pillow has no menu at all.
            from .player import playerManager as _pm

            _pm.set_osc_style("mpv" if _pm.lua_works() else "none")

    if use_gui:
        # ...and the other thing the browser cannot do without: lua.
        #
        # Everything the shim DRAWS is lua, so a build without it must fall
        # all the way back to the CLI with the OSD menu enabled -- **[iw]**:
        # "the osc setting doesn't matter because MPV's default OSC *needs
        # lua*". Why the probe exists and what it costs:
        # docs/mpv-backends.md section 2.
        #
        # Importing player here rather than below: the probe needs the live
        # mpv, and this decision has to be made before the UI is started.
        from .player import playerManager as _pm

        if not _pm.lua_works():
            user_interface = None
            use_gui = False
            # There is no OSC to fall back to -- every one of them is lua --
            # so the OSD menu is the only surface left, and it is reachable
            # exactly because the resolved style is no longer "mpvtk".
            _pm.set_osc_style("none")

    if not user_interface:
        from .cli_mgr import user_interface

    from .clients import clientManager
    from .player import playerManager
    from .action_thread import actionThread
    from .event_handler import eventHandler
    from .timeline import timelineManager
    from .sync.manager import syncManager
    from .sync.offline_media import offline_video_factory
    from .media import set_video_factory

    set_video_factory(offline_video_factory)
    clientManager.callback = eventHandler.handle_event
    timelineManager.start()
    playerManager.timeline_trigger = timelineManager.trigger
    actionThread.start()
    playerManager.action_trigger = actionThread.trigger
    # Resolve the menu at call time: even though the OSDMenu now survives mpv
    # re-creation, binding through playerManager keeps this correct if that
    # ever changes.
    user_interface.open_player_menu = lambda: playerManager.menu.show_menu()
    syncManager.start(
        lambda server_uuid: clientManager.clients.get(server_uuid),
        # Auto-download sweeps every logged-in server, and stands down while
        # anything is playing so it never competes with streaming for
        # bandwidth. is_playing() is False when idle or paused-at-idle, which
        # is exactly when fetching ahead is free.
        get_clients=lambda: clientManager.clients,
        is_busy=lambda: playerManager.is_playing())
    user_interface.start()
    single.on_activate = getattr(user_interface, "activate", lambda: None)
    user_interface.login_servers()

    if not load_success:
        log.error("Your configuration file is not valid JSON! It has been ignored!")
        log.info("Tip: Open the JSON file in VS Code to see what is wrong.")

    try:
        user_interface.stop_callback = halt.set
        try:
            while not halt.wait(timeout=1):
                pass
        except KeyboardInterrupt:
            print("")
            log.info("Stopping services...")
    finally:
        from . import exit_watchdog

        # Armed BEFORE the sequence, not after: the failure we are guarding
        # against is a step that never returns, and anything placed after
        # such a step is unreachable. On expiry it dumps every thread, which
        # is what identifies the wedged step.
        exit_watchdog.arm()

        def final_action():
            """The last thing this process does, on **either** way out.

            Registered with the watchdog rather than written below the
            shutdown loop, because the loop is not reached when a step
            wedges -- and that is precisely when a user who pressed
            *Restart Now* most needs the app to come back rather than
            simply vanish.

            The release is here as well as in the loop because the wedge can
            be anywhere: on the forced path the lock may never have been
            given up, and a new copy that finds it held hands off to this
            dying process and exits. It is idempotent, so the ordinary path
            pays nothing for it.
            """
            # The tray first: it is a separate process, and the shutdown
            # loop only reaches it in step six of seven. On the forced path
            # the wedge can be anywhere before that, so without this a
            # restart would put a live replacement beside a tray icon whose
            # app is gone -- and on `start_minimized` that dead icon is the
            # only thing the user would try to click.
            #
            # getattr: the CLI user interface has no tray.
            stop_tray = getattr(user_interface, "stop_tray", None)
            if stop_tray is not None:
                try:
                    stop_tray()
                except Exception:
                    log.debug("could not stop the tray", exc_info=True)
            single.release()
            from . import restart

            restart.relaunch_if_requested()

        exit_watchdog.set_final_action(final_action)
        # Covers the quit paths that do not start at a window close (tray
        # Quit, Ctrl-C): mpv is about to go away either way, and no reply
        # is worth minutes now.
        from .player import bound_ipc_replies

        bound_ipc_replies()
        # Logged per step for the same reason — the last line in the log
        # names the step that hung, even if the dump is unavailable.
        for name, stop in (
            ("player", playerManager.terminate),
            ("timeline", timelineManager.stop),
            ("action thread", actionThread.stop),
            ("sync manager", syncManager.stop),
            ("clients", clientManager.stop),
            ("user interface", user_interface.stop),
            ("instance lock", single.release),
        ):
            log.info("Shutting down: %s", name)
            try:
                stop()
            except Exception:
                # One component failing to stop must not strand the rest —
                # a half-shut-down app is exactly what leaves the stray
                # threads this sequence exists to clean up.
                log.exception("Error shutting down %s", name)
        log.info("Shutdown complete.")
        # Nothing may be added below this line: `finish` ends in `os._exit`
        # and never returns, which is why its docstring says to call it as
        # the last statement of main. The restart was originally written
        # here, after it, and was simply dead code -- it armed, the app
        # quit, and nothing came back. Whatever has to happen last goes
        # through `set_final_action` above, which also covers the exit this
        # line never reaches.
        exit_watchdog.finish()


if __name__ == "__main__":
    main()
