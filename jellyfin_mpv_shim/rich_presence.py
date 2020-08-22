from pypresence import Presence
import time

client_id = "743296148592263240"
RPC = Presence(client_id)
RPC.connect()


def send_presence(
    title: str,
    subtitle: str,
    playback_time: float = None,
    duration: float = None,
    playing: bool = False,
):
    small_image = "play-dark3" if playing else None
    start = None
    end = None
    if playback_time is not None and duration is not None and playing:
        start = int(time.time() - playback_time)
        end = int(start + duration)
    RPC.update(
        state=subtitle,
        details=title,
        instance=False,
        large_image="jellyfin2",
        start=start,
        end=end,
        large_text="Jellyfin",
        small_image=small_image,
    )


def clear_presence():
    RPC.clear()
