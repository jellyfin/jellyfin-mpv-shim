import sys, threading
from types import SimpleNamespace as NS
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
import _harness as h
pm_mod = h.import_player_with_fake_mpv()
from jellyfin_mpv_shim.media import Video
from unittest.mock import patch

# The API boundary, with no actual credentials or remote requests.
class Client:
    config = NS(data={})
    def __init__(self, journal):
        self.jellyfin = self
        self.journal = journal
    def get_item(self, item_id):
        return {'Id': item_id, 'Type': 'Movie', 'Name': item_id, 'RunTimeTicks': 1000000000}
    def item_played(self, item_id, watched):
        self.journal.record('server', 'mark', item_id, watched)
    def session_stop(self, options):
        self.journal.record('server', 'stop', options['ItemId'], options['PositionTicks'])

pm = h.build_player(pm_mod)
client = Client(pm.journal)
parent = NS(client=client, is_local=True, seq=0, has_next=False, has_prev=False, queue=[{'Id':'movie', 'PlaylistItemId':'pid'}])
v = Video('movie', parent)
v.media_source = {'Id':'movie', 'MediaStreams':[]}
v.playback_info = {'PlaySessionId':'session'}
pm._video = v
pm._player.playback_abort = False
pm._player.playback_time = 95
pm._player.duration = 100
pm.should_send_timeline = True
pm.start_time = 1
pm.journal.reset()
# Simulates an earlier slow report already occupying the real reporter.
entered, release = threading.Event(), threading.Event()
def delay_report():
    entered.set()
    assert release.wait(5)
pm._reporter.submit(delay_report, 'earlier slow request')
assert entered.wait(2)
try:
    with patch.object(pm_mod, 'discord_presence', False), patch.object(pm_mod.settings, 'stop_cmd', None):
        pm.unwatched_quit()
    pm.journal.mark('quit and mark unwatched returned')
finally:
    release.set()
    assert pm._reporter.drain(5)
print(pm.journal.render())
pm.journal.order('server.mark:movie=False', 'server.stop:movie=950000000')
pm._reporter.stop()
