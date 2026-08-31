import sys
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
import _harness as h
pm_mod = h.import_player_with_fake_mpv()
from jellyfin_mpv_shim.media import Video
from jellyfin_mpv_shim import conf
pm = h.build_player(pm_mod)
streams = [
    {'Type':'Audio','Index':1,'Codec':'aac','Language':'eng','DisplayTitle':'English'},
    {'Type':'Audio','Index':2,'Codec':'aac','Language':'deu','DisplayTitle':'German'},
]
source = {'Id':'episode2','Protocol':'File','SupportsDirectPlay':False,'SupportsDirectStream':False,'SupportsTranscoding':True, 'TranscodingUrl':'/master.m3u8?AudioStreamIndex=1', 'DefaultAudioStreamIndex':1, 'MediaStreams':streams}
class Client:
    config = NS(data={'auth.server':'https://jellyfin.example.invalid','auth.token':'SYNTHETIC'})
    http = NS(_get_authenication_header=lambda: 'MediaBrowser Token="SYNTHETIC"')
    def __init__(self): self.jellyfin = self
    def get_item(self, item_id): return {'Id':item_id,'Type':'Episode','MediaType':'Video','Name':'Episode 2','MediaSources':[source]}
    def get_play_info(self, item_id, profile, aid, sid, **kwargs):
        pm.journal.record('server','negotiate-audio',item_id,aid)
        return {'PlaySessionId':'session','MediaSources':[deepcopy(source)]}
client=Client()
parent=NS(client=client,is_local=True,has_next=False,seq=0,queue=[{'Id':'episode2','PlaylistItemId':'pid'}])
v=Video('episode2',parent)
pm._track_memory = ({'MediaStreams':deepcopy(streams)}, 2, -1)
real_play=pm._player.play
def fake_load(url):
    real_play(url)
    pm._player.duration=100
    pm._player.playback_abort=False
    pm._load_completed.set()
pm._player.play=fake_load
pm.journal.reset()
with patch.object(conf, 'any_segment_wanted', lambda:False), patch.object(conf.settings,'remember_audio_track',True):
    pm.play(v, no_initial_timeline=True)
print('Playback URL:',pm.url)
print('Selected audio reported by video:',v.aid)
print(pm.journal.render())
assert 'AudioStreamIndex=1' in pm.url
assert v.aid==2
pm.journal.order('server.negotiate-audio:episode2=None','mpv.play')
print('PROBE CONFIRMED: stream was negotiated for audio 1, final UI state says audio 2')
