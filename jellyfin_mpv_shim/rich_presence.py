from pypresence import Client
import time
import requests
import re

client_id = "743296148592263240"
RPC = Client(client_id)
RPC.start()


def register_join_event(syncplay_join_group: callable):
    RPC.register_event("activity_join", syncplay_join_group)


image_url = None
bashupload_url = None


def send_presence(
    title: str,
    subtitle: str,
    playback_time: float = None,
    duration: float = None,
    playing: bool = False,
    syncplay_group: str = None,
    artwork_url: str = None,
):
    small_image = (
        "https://cdn.discordapp.com/app-assets/463097721130188830/493061639994867714.png"
        if playing
        else "https://cdn.discordapp.com/app-assets/463097721130188830/493061640296595456.png"
    )
    small_text = "Playing" if playing else "Paused"
    start = None
    end = None
    if playback_time is not None and duration is not None and playing:
        start = int(time.time() - playback_time)
        end = int(start + duration)
    RPC.set_activity(
        state=subtitle,
        details=title,
        instance=False,
        large_image=upload_image(artwork_url),
        start=start,
        end=end,
        large_text=title,
        small_image=small_image,
        small_text=small_text,
        party_id=str(hash(syncplay_group)),
        join=syncplay_group,
    )


def clear_presence():
    RPC.clear_activity()


def upload_image(link):
    global image_url
    global bashupload_url
    if image_url == link:
        return bashupload_url
    image_url = link
    r = requests.get(link)
    files = {"file": ("image.jpg", r.content)}
    post = requests.post("https://bashupload.com/", files=files)

    regex = r"https(.*)"
    result = re.search(regex, post.text)
    bashupload_url = result.group(0)
    return bashupload_url
