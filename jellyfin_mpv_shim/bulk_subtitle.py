from .media import XMLCollection
from .utils import get_plex_url

from collections import namedtuple
import urllib.parse
import requests
import time

Part = namedtuple("Part", ["id", "audio", "subtitle"])
Audio = namedtuple("Audio", ["id", "language_code", "name", "plex_name"])
Subtitle = namedtuple("Subtitle", ["id", "language_code", "name", "is_forced", "plex_name"])

messages = []
keep_messages = 6

def render_message(message, show_text):
    messages.append(message)
    text = "Selecting Tracks..."
    for message in messages[-6:]:
        text += "\n   " + message
    show_text(text,2**30,1)

def process_series(mode, url, player, m_raid=None, m_rsid=None):
    messages.clear()
    show_text = player._player.show_text
    c_aid, c_sid = None, None
    c_pid = player._video._part_node.get("id")

    success_ct = 0
    partial_ct = 0
    count = 0

    xml = XMLCollection(url)
    for video in xml.tree.findall("./Video"):
        name = "s{0}e{1:02}".format(int(video.get("parentIndex")), int(video.get("index")))
        video = XMLCollection(xml.get_path(video.get("key"))).tree.find("./")
        for partxml in video.findall("./Media/Part"):
            count += 1
            audio_list = [Audio(s.get("id"), s.get("languageCode"), s.get("title"),
                          s.get("displayTitle")) for s in partxml.findall("./Stream[@streamType='2']")]
            subtitle_list =  [Subtitle(s.get("id"), s.get("languageCode"), s.get("title"),
                              "Forced" in s.get("displayTitle"), s.get("displayTitle"))
                              for s in partxml.findall("./Stream[@streamType='3']")]
            part = Part(partxml.get("id"), audio_list, subtitle_list)

            aid = None
            sid = "0"
            if mode == "subbed":
                audio, subtitle = get_subbed(part)
                if audio and subtitle:
                    render_message("{0}: {1} ({2})".format(
                        name, subtitle.plex_name, subtitle.name), show_text)
                    aid, sid = audio.id, subtitle.id
                    success_ct += 1
            elif mode == "dubbed":
                audio, subtitle = get_dubbed(part)
                if audio and subtitle:
                    render_message("{0}: {1} ({2})".format(
                        name, subtitle.plex_name, subtitle.name), show_text)
                    aid, sid = audio.id, subtitle.id
                    success_ct += 1
                elif audio:
                    render_message("{0}: No Subtitles".format(name), show_text)
                    aid = audio.id
                    partial_ct += 1
            elif mode == "manual":
                if m_raid < len(part.audio) and m_rsid < len(part.subtitle):
                    audio = part.audio[m_raid]
                    aid = audio.id
                    render_message("{0} a: {1} ({2})".format(
                            name, audio.plex_name, audio.name), show_text)
                    if m_rsid != -1:
                        subtitle = part.subtitle[m_rsid]
                        sid = subtitle.id
                        render_message("{0} s: {1} ({2})".format(
                            name, subtitle.plex_name, subtitle.name), show_text)
                    success_ct += 1
            
            if aid:
                if c_pid == part.id:
                    c_aid, c_sid = aid, sid

                args = {
                    "allParts": "1",
                    "audioStreamID": aid,
                    "subtitleStreamID": sid
                }
                url = "/library/parts/{0}".format(part.id)
                requests.put(get_plex_url(urllib.parse.urljoin(xml.server_url, url), args), data=None)
            else:
                render_message("{0}: Fail".format(name), show_text)
    
    if mode == "subbed":
        render_message("Set Subbed: {0} ok, {1} fail".format(
            success_ct, count-success_ct), show_text)
    elif mode == "dubbed":
        render_message("Set Dubbed: {0} ok, {1} audio only, {2} fail".format(
            success_ct, partial_ct, count-success_ct-partial_ct), show_text)
    elif mode == "manual":
        render_message("Manual: {0} ok, {1} fail".format(
            success_ct, count-success_ct), show_text)
    time.sleep(3)
    if c_aid:
        render_message("Setting Current...", show_text)
        if player._video.is_transcode:
            player.put_task(player.set_streams, c_aid, c_sid)
            player.timeline_handle()
        else:
            player.set_streams(c_aid, c_sid)
  
def get_subbed(part):
    japanese_audio = None
    english_subtitles = None
    subtitle_weight = None

    for audio in part.audio:
        lower_title = audio.name.lower() if audio.name is not None else ""
        if audio.language_code != "jpn" and not "japan" in lower_title:
            continue
        if "commentary" in lower_title:
            continue
        
        if japanese_audio is None:
            japanese_audio = audio
            break
    
    for subtitle in part.subtitle:
        lower_title = subtitle.name.lower() if subtitle.name is not None else ""
        if subtitle.language_code != "eng" and not "english" in lower_title:
            continue
        if subtitle.is_forced:
            continue

        weight = dialogue_weight(lower_title)
        if subtitle_weight is None or weight < subtitle_weight:
            subtitle_weight = weight
            english_subtitles = subtitle
    
    if japanese_audio and english_subtitles:
        return japanese_audio, english_subtitles
    return None, None

def get_dubbed(part):
    english_audio = None
    sign_subtitles = None
    subtitle_weight = None

    for audio in part.audio:
        lower_title = audio.name.lower() if audio.name is not None else ""
        if audio.language_code != "eng" and not "english" in lower_title:
            continue
        if "commentary" in lower_title:
            continue
        
        if english_audio is None:
            english_audio = audio
            break
    
    for subtitle in part.subtitle:
        lower_title = subtitle.name.lower() if subtitle.name is not None else ""
        if subtitle.language_code != "eng" and not "english" in lower_title:
            continue
        if subtitle.is_forced:
            sign_subtitles = subtitle
            break

        weight = sign_weight(lower_title)
        if weight == 0:
            continue

        if subtitle_weight is None or weight < subtitle_weight:
            subtitle_weight = weight
            sign_subtitles = subtitle
    
    if english_audio:
        return english_audio, sign_subtitles
    return None, None

def dialogue_weight(text):
    if not text:
        return 900
    lower_text = text.lower()
    has_dialogue = "main" in lower_text or "full" in lower_text or "dialogue" in lower_text
    has_songs = "op/ed" in lower_text or "song" in lower_text or "lyric" in lower_text
    has_signs = "sign" in lower_text                                       
    vendor = "bd" in lower_text or "retail" in lower_text
    weight = 900

    if has_dialogue and has_songs:
        weight -= 100
    if has_songs:
        weight += 200
    if has_dialogue and has_signs:
        weight -= 100
    elif has_signs:
        weight += 700
    if vendor:
        weight += 50
    return weight

def sign_weight(text):
    if not text:
        return 0
    lower_text = text.lower()
    has_songs = "op/ed" in lower_text or "song" in lower_text or "lyric" in lower_text
    has_signs = "sign" in lower_text                                       
    vendor = "bd" in lower_text or "retail" in lower_text
    weight = 900

    if not (has_songs or has_signs):
        return 0
    if has_songs:
        weight -= 200
    if has_signs:
        weight -= 300
    if vendor:
        weight += 50
    return weight
