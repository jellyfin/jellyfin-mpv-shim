#!/usr/bin/env python3

import logging
import sys
import time
import multiprocessing
from threading import Event

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s")

from . import conffile
from .conf import settings
from .clients import clientManager
from .constants import APP_NAME

log = logging.getLogger('')
logging.getLogger('requests').setLevel(logging.CRITICAL)

def main():
    conf_file = conffile.get(APP_NAME, 'conf.json')
    settings.load(conf_file)

    if sys.platform.startswith("darwin"):
        multiprocessing.set_start_method('forkserver')

    userInterface = None
    mirror = None
    use_gui = False
    if settings.enable_gui:
        try:
            from .gui_mgr import userInterface
            use_gui = True
            gui_ready = Event()
            userInterface.gui_ready = gui_ready
        except Exception:
            log.warning("Cannot load GUI. Falling back to command line interface.", exc_info=1)
    
    if settings.display_mirroring:
        try:
            from .display_mirror import mirror
        except ImportError:
            log.warning("Cannot load display mirror.", exc_info=1)

    if not userInterface:
        from .cli_mgr import userInterface

    from .player import playerManager
    from .action_thread import actionThread
    from .event_handler import eventHandler
    from .timeline import timelineManager

    clientManager.callback = eventHandler.handle_event
    timelineManager.start()
    playerManager.timeline_trigger = timelineManager.trigger
    actionThread.start()
    playerManager.action_trigger = actionThread.trigger
    userInterface.open_player_menu = playerManager.menu.show_menu
    eventHandler.mirror = mirror
    userInterface.start()
    userInterface.login_servers()

    try:
        if mirror:
            userInterface.stop_callback = mirror.stop
            # If the webview runs before the systray icon, it fails.
            if use_gui:
                gui_ready.wait()
            mirror.run()
        else:
            halt = Event()
            userInterface.stop_callback = halt.set
            try:
                halt.wait()
            except KeyboardInterrupt:
                print("")
                log.info("Stopping services...")
    finally:
        playerManager.terminate()
        timelineManager.stop()
        actionThread.stop()
        clientManager.stop()
        userInterface.stop()

if __name__ == "__main__":
    main()

