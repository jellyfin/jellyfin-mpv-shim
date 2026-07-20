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

    # If we're not the first launch, ask the running instance to surface its
    # window (un-minimize) and exit, rather than starting a second copy.
    from .single_instance import SingleInstance

    single = SingleInstance()
    if not single.acquire():
        log.info("Another instance is already running; exiting.")
        return

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
    syncManager.start(lambda server_uuid: clientManager.clients.get(server_uuid))
    user_interface.start()
    single.on_activate = getattr(user_interface, "activate", lambda: None)
    user_interface.login_servers()

    if not load_success:
        log.error("Your configuration file is not valid JSON! It has been ignored!")
        log.info("Tip: Open the JSON file in VS Code to see what is wrong.")

    try:
        halt = Event()
        user_interface.stop_callback = halt.set
        try:
            while not halt.wait(timeout=1):
                pass
        except KeyboardInterrupt:
            print("")
            log.info("Stopping services...")
    finally:
        playerManager.terminate()
        timelineManager.stop()
        actionThread.stop()
        syncManager.stop()
        clientManager.stop()
        user_interface.stop()
        single.release()


if __name__ == "__main__":
    main()
