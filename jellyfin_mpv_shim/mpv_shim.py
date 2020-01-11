#!/usr/bin/env python3

import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(asctime)s [%(levelname)8s] %(message)s")

from . import conffile
from .conf import settings
from .player import playerManager
from .timeline import timelineManager
from .action_thread import actionThread
from .clients import clientManager

APP_NAME = 'jellyfin-mpv-shim'
log = logging.getLogger('')
logging.getLogger('requests').setLevel(logging.CRITICAL)

def main():
    conf_file = conffile.get(APP_NAME,'conf.json')
    settings.load(conf_file)

    def callback(client, event_name, data):
        print(client, event_name, data)

    clientManager.callback = callback
    clientManager.connect()

    timelineManager.start()
    playerManager.timeline_trigger = timelineManager.trigger
    actionThread.start()
    playerManager.action_trigger = actionThread.trigger

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("")
        log.info("Stopping services...")
    finally:
        playerManager.stop()
        timelineManager.stop()
        actionThread.stop()
        clientManager.stop()

if __name__ == "__main__":
    main()

