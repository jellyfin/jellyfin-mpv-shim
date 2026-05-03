from pypresence import Client
from pypresence.types import ActivityType, StatusDisplayType
import time
import logging

log = logging.getLogger("rich_presence")

client_id = "743296148592263240"
RPC = None


def _ensure_connected():
    global RPC
    if RPC is not None:
        return True
    try:
        rpc = Client(client_id)
        rpc.start()
        RPC = rpc
        log.info("Connected to Discord Rich Presence.")
        return True
    except Exception:
        log.debug("Discord is not available yet.", exc_info=True)
        return False


def register_join_event(syncplay_join_group: callable):
    if _ensure_connected():
        RPC.register_event("activity_join", syncplay_join_group)


def send_presence(
    title: str,
    subtitle: str,
    playback_time: float = None,
    duration: float = None,
    playing: bool = False,
    syncplay_group: str = None,
    media_type: str = None,
):
    if not _ensure_connected():
        return

    small_image = "play-dark3" if playing else None
    start = None
    end = None
    if playback_time is not None and duration is not None and playing:
        start = int(time.time() - playback_time)
        end = int(start + duration)

    payload = {
        "activity_type": ActivityType.LISTENING if media_type and media_type == "Audio" else ActivityType.WATCHING,
        "status_display_type": StatusDisplayType.DETAILS,
        "state": subtitle if subtitle else "Unknown Media",
        "details": title,
        "instance": False,
        "large_image": "jellyfin2",
        "start": start,
        "end": end,
        "large_text": "Jellyfin",
        "small_image": small_image,
    }

    if syncplay_group:
        payload["party_id"] = str(hash(syncplay_group))
        payload["party_size"] = [1, 100]
        payload["join"] = syncplay_group

    try:
        RPC.set_activity(**payload)
    except Exception:
        log.warning("Discord connection lost.", exc_info=True)
        RPC = None


def clear_presence():
    global RPC
    if RPC is None:
        return
    try:
        RPC.clear_activity()
    except Exception:
        log.warning("Discord connection lost.", exc_info=True)
        RPC = None
