import sys,time
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
import _harness as h
mod=h.import_player_with_fake_mpv()
import mpv
from jellyfin_mpv_shim import keysweep
pm=h.build_player(mod)
p=mpv.MPV(vo='null',config=False,idle=True,input_default_bindings=True)
pm._player=p
p.event_callback('client-message')(pm._on_client_message)
p.command('define-section','dormant','f set fullscreen no','force')
p.command('disable-section','dormant')
try:
    for cycle in range(3):
        p.command('disable-section',pm.KEY_SECTION)
        p.fullscreen=False
        p.command('keypress','f')
        time.sleep(.05)
        before=p.fullscreen
        p.fullscreen=False
        pm.claim_keys('fullscreen',{keysweep.FULLSCREEN})
        p.command('keypress','f')
        time.sleep(.05)
        after=p.fullscreen
        print('cycle',cycle+1,'f without claim:',before,'f with claim:',after,'claimed action:',pm._key_actions['f'])
        assert before is True
        assert after is False
    print('CONFIRMED: standing claim copies disabled binding and breaks fullscreen')
finally:
    p.terminate()
