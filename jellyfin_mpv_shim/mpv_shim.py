#!/usr/bin/env python3

import logging
import sys
import multiprocessing
from threading import Event

from . import conffile
from . import i18n
from .conf import settings
from .clients import clientManager
from .constants import APP_NAME
from .log_utils import (
    configure_log,
    configure_log_file,
    enable_sanitization,
    root_logger,
)

logging.getLogger("requests").setLevel(logging.CRITICAL)


def main():
    conf_file = conffile.get(APP_NAME, "conf.json")
    load_success = settings.load(conf_file)
    i18n.configure()

    if settings.sanitize_output:
        enable_sanitization()

    configure_log(sys.stdout, settings.mpv_log_level)
    if settings.write_logs:
        log_file = conffile.get(APP_NAME, "log.txt")
        configure_log_file(log_file, settings.mpv_log_level)

    log = root_logger

    if sys.platform.startswith("darwin"):
        multiprocessing.set_start_method("forkserver")

    user_interface = None
    mirror = None
    use_gui = False
    gui_ready = None
    get_webview = lambda: None
    if settings.enable_gui:
        try:
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

    from .player import playerManager
    from .action_thread import actionThread
    from .event_handler import eventHandler
    from .timeline import timelineManager

    clientManager.callback = eventHandler.handle_event
    timelineManager.start()
    playerManager.timeline_trigger = timelineManager.trigger
    actionThread.start()
    playerManager.action_trigger = actionThread.trigger
    playerManager.get_webview = get_webview
    user_interface.open_player_menu = playerManager.menu.show_menu
    eventHandler.mirror = mirror
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
        clientManager.stop()
        user_interface.stop()

if __name__ == "__main__":
    main()
