import sys
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
import _harness as h
mod=h.import_player_with_fake_mpv()
import mpv
from jellyfin_mpv_shim import keysweep
pm=h.build_player(mod)
p=mpv.MPV(vo='null',config=False,idle=True,input_default_bindings=True)
pm._player=p
p.command('define-section','custom','RIGHT seek 30 exact\nLEFT seek -30 exact','force')
p.command('enable-section','custom',pm.SECTION_FLAGS)
pm.seek=lambda amount,**kwargs: calls.append((amount,kwargs))
try:
    actual=keysweep.winning(p.input_bindings)['RIGHT']
    print('Actual RIGHT binding:',actual)
    print('Cached sweep RIGHT:',[s for s in pm._swept_keys() if s[0]=='RIGHT'])
    for cycle in range(3):
        calls=[]
        pm.kb_seek('right')
        print('remote right',cycle+1,'asked:',calls)
        assert calls == [(5,{'exact':False})]
    print('CONFIRMED: custom 30s exact keyboard seek becomes stock 5s remote seek')
finally:
    p.terminate()
