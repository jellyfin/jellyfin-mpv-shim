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

    app_log_level = "debug" if args.debug else settings.mpv_log_level
    configure_log(sys.stdout, app_log_level)
    if settings.write_logs:
        log_file = conffile.get(APP_NAME, "log.txt")
        configure_log_file(log_file, app_log_level)

    log = root_logger

    if sys.platform.startswith("darwin"):
        try:
            # Use 'spawn' to avoid Objective-C fork crashes with GUI frameworks.
            # - Python 3.7: default is 'fork' (unsafe with Obj-C)
            # - Python 3.8+: default is 'spawn' (this is a no-op but explicit)
            # - Python 3.14: 'forkserver' also crashes with Obj-C (issue #473)
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            # Context already set, ignore
            pass

    user_interface = None
    mirror = None
    use_gui = False
    gui_ready = None
    get_webview = lambda: None
    if settings.enable_gui:
        try:
            # Tkinter is optional in some Python builds; probe it before
            # committing to the GUI so we cleanly fall back to the CLI.
            import tkinter  # noqa: F401
            from .gui_mgr import user_interface

            use_gui = True
            gui_ready = Event()
            user_interface.gui_ready = gui_ready
        except Exception:
            log.warning(
                "Cannot load GUI. Falling back to command line interface.",
                exc_info=True,
            )

    if settings.display_mirroring:
        try:
            from .display_mirror import mirror

            get_webview = mirror.get_webview
        except ImportError:
            mirror = None
            log.warning("Cannot load display mirror.", exc_info=True)

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
    playerManager.get_webview = get_webview
    user_interface.open_player_menu = playerManager.menu.show_menu
    eventHandler.mirror = mirror
    syncManager.start(lambda server_uuid: clientManager.clients.get(server_uuid))
    user_interface.start()
    user_interface.login_servers()

    if not load_success:
        log.error("Your configuration file is not valid JSON! It has been ignored!")
        log.info("Tip: Open the JSON file in VS Code to see what is wrong.")

    try:
        if mirror:
            user_interface.stop_callback = mirror.stop
            # If the webview runs before the systray icon, it fails.
            if use_gui:
                gui_ready.wait()
            mirror.run()
        else:
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


if __name__ == "__main__":
    main()
