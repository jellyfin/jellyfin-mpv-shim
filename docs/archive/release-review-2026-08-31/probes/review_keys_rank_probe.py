import sys,time
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
import mpv
from jellyfin_mpv_shim import keysweep
p=mpv.MPV(vo='null',config=False,idle=True,input_default_bindings=True)
try:
    for cycle in range(3):
        p.command('define-section','dormant','f set fullscreen no','force')
        p.command('disable-section','dormant')
        bindings=[b for b in p.input_bindings if b.get('key')=='f']
        print('cycle',cycle+1,'bindings:',bindings)
        print('winning according to keysweep:',keysweep.winning(bindings)['f'])
        p.fullscreen=False
        p.command('keypress','f')
        time.sleep(.03)
        print('actual keypress fullscreen:',p.fullscreen)
        assert p.fullscreen is True
        assert keysweep.winning(bindings)['f']['section']=='dormant'
    print('CONFIRMED: inactive nonweak binding outranks active builtin only in keysweep')
finally:
    p.terminate()
