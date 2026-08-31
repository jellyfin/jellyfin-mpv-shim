import sys, time, threading, json
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.argv=['probe']
import mpv
from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
from jellyfin_mpv_shim.mpvtk.widgets import Column, TextBox, Button
handle=mpv.MPV(idle=True, force_window=True, osc=False, config=False, load_scripts=False, input_default_bindings=True, terminal=False, vo='x11', geometry='800x450')
app=MpvtkApp.attach(handle,ext=False)
events=[]
def build(size):
    return Column([TextBox('entry',w=300,on_change=lambda s:events.append(('change',s)),on_submit=lambda s:events.append(('submit',s))),Button('Other',id='other')],w=size[0],h=size[1])
thread=threading.Thread(target=lambda:app.run(build),daemon=True)
thread.start()
try:
    assert app.ready.wait(10)
    time.sleep(.4)
    print('VERSION',handle.mpv_version)
    app.debug(cmd='click',id='entry')
    time.sleep(.3)
    handle.command('keypress','a')
    time.sleep(.3)
    print('BEFORE',events, 'focus',app.debug_state().get('focus'))
    handle.command('script-binding','console/enable')
    time.sleep(.6)
    print('CONSOLE',handle._get_property('user-data/mpv/console/open'))
    app.claim_keys(('SPACE',))
    time.sleep(.3)
    bindings=handle._get_property('input-bindings')
    print('KEYS', json.dumps([b for b in bindings if b.get('key') in ('ENTER','any_unicode','LEFT','SPACE')]))
    handle.command('keypress','b')
    handle.command('keypress','ENTER')
    time.sleep(.5)
    print('AFTER',events,'focus',app.debug_state().get('focus'),'tb',app.debug_state().get('tb'))
    handle.command('keypress','ESC')
    time.sleep(.3)
    print('CONSOLE AFTER ESC',handle._get_property('user-data/mpv/console/open'))
finally:
    app.quit(); thread.join(3); handle.terminate()
