"""
subscribers.py - Plex Remote Subscribers

Modeled after Plex Home Theater Remote implementation:
    https://github.com/plexinc/plex-home-theater-public/blob/pht-frodo/plex/Remote/
"""
import logging
from threading import Event

from .utils import Timer

# give clients 90 seconds before we time them out
SUBSCRIBER_REMOVE_INTERVAL = 90

log = logging.getLogger('subscribers')
subscriber_events = {}

class RemoteSubscriberManager(object):
    subscribers = {}

    def addSubscriber(self, subscriber):
        if subscriber.uuid in self.subscribers:
            log.debug("RemoteSubscriberManager::addSubscriber refreshed %s" % subscriber.uuid)
            self.subscribers[subscriber.uuid].refresh(subscriber)
        else:
            log.debug("RemoteSubscriberManager::addSubscriber added %s [%s]" % (subscriber.url, subscriber.uuid))
            self.subscribers[subscriber.uuid] = subscriber

        # timer.SetTimeout(PLEX_REMOTE_SUBSCRIBER_CHECK_INTERVAL * 1000, this);

    def updateSubscriberCommandID(self, subscriber):
        if subscriber.uuid in self.subscribers:
            self.subscribers[subscriber.uuid].commandID = subscriber.commandID

    def removeSubscriber(self, subscriber):
        if subscriber is not None and subscriber.uuid in self.subscribers:
            log.debug("RemoteSubscriberManager::removeSubscriber removing subscriber %s [%s]" % (subscriber.url, subscriber.uuid))
            self.subscribers.pop(subscriber.uuid)

    def findSubscriberByUUID(self, uuid):
        if uuid in self.subscribers:
            return self.subscribers[uuid]

    def getSubscriberURL(self):
        urls = []
        for uuid, subscriber in self.subscribers.items():
            urls.append(subscriber.url)
        return urls

class RemoteSubscriber(object):
    def __init__(self, uuid, commandID, ipaddress="", port=32400, protocol="http", name=""):
        self.poller         = False
        self.uuid           = uuid
        self.commandID      = commandID
        self.url            = ""
        self.name           = name
        self.lastUpdated    = Timer()

        if ipaddress and protocol:
            self.url = "%s://%s:%s" % (protocol, ipaddress, port)

    def refresh(self, sub):
        log.debug("RemoteSubscriber::refresh %s (cid=%s)" % (self.uuid, sub.commandID))

        if sub.url != self.url:
            log.debug("RemoteSubscriber::refresh new url %s", sub.url)
            self.url = sub.url

        if sub.commandID != self.commandID:
            log.debug("RemoteSubscriber::refresh new commandID %s", sub.commandID)
            self.commandID = sub.commandID

        self.lastUpdated.restart()

    def get_poll_evt(self):
        if not self.uuid in subscriber_events:
            subscriber_events[self.uuid] = Event()
        return subscriber_events[self.uuid]

    def set_poll_evt(self):
        if self.uuid in subscriber_events:
            subscriber_events[self.uuid].set()
        subscriber_events[self.uuid] = Event()

    def shouldRemove(self):
        if self.lastUpdated.elapsed() > SUBSCRIBER_REMOVE_INTERVAL:
            log.debug("RemoteSubscriber::shouldRemove "
                      "removing %s because elapsed: %d" % (self.uuid, self.lastUpdated.elapsed()))
            return True

        log.debug("RemoteSubscriber::shouldRemove will not "
                  "remove %s because elapsed: %d" % (self.uuid, self.lastUpdated.elapsed()))
        return False


remoteSubscriberManager = RemoteSubscriberManager()
