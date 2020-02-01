#!/usr/bin/env python3

import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(asctime)s [%(levelname)8s] %(message)s")

from . import conffile
from .conf import settings
from .clients import clientManager
from .constants import APP_NAME

log = logging.getLogger('')
logging.getLogger('requests').setLevel(logging.CRITICAL)

def main():
    conf_file = conffile.get(APP_NAME, 'conf.json')
    settings.load(conf_file)

    use_gui = False
    if settings.enable_gui:
        try:
            from .gui_mgr import userInterface
            use_gui = True
        except Exception:
            log.warning("Cannot load GUI. Falling back to command line interface.", exc_info=1)

    if not use_gui:
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
    userInterface.login_servers()

    try:
        userInterface.run()
    except KeyboardInterrupt:
        print("")
        log.info("Stopping services...")
    finally:
        playerManager.terminate()
        timelineManager.stop()
        actionThread.stop()
        clientManager.stop()

if __name__ == "__main__":
    main()

