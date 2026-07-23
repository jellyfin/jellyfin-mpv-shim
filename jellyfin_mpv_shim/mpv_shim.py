#!/usr/bin/env python3

import logging
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

    # `kill -USR1 <pid>` dumps every thread's stack. The only time a hang is
    # diagnosable is while it is hanging, and by then it is too late to add
    # instrumentation.
    from .exit_watchdog import enable_manual_dumps

    enable_manual_dumps()

    try:
        # Use 'spawn' for the tray/browser child processes on every platform.
        # - macOS: avoids Objective-C fork crashes with GUI frameworks
        #   (3.14's 'forkserver' also crashes with Obj-C, issue #473).
        # - Linux/Windows: these children are forked *after* the timeline/action/
        #   sync worker threads start, so a plain fork can inherit a held lock
        #   (e.g. logging) and deadlock the child. 'spawn' gives a clean
        #   interpreter; the children already rely only on their IPC-supplied
        #   options, not inherited globals, so this is safe.
        multiprocessing.set_start_method("spawn")
    except RuntimeError:
        # Context already set, ignore
        pass

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
        log.info("Another instance is already running; exiting.")
        return

    # Created before anything can request a stop, so a `stop` arriving during
    # startup is honoured rather than acknowledged and dropped.
    halt = Event()
    single.on_stop = halt.set

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
        exit_watchdog.finish()


if __name__ == "__main__":
    main()
