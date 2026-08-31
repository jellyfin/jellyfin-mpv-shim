import sys, time, threading, json
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.argv=['probe']
import mpv
from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
from jellyfin_mpv_shim.mpvtk.widgets import Column, Text
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from tests._shell_harness import FakeSource
handle=mpv.MPV(idle=True,force_window=True,osc=False,config=False,load_scripts=False,input_default_bindings=True,terminal=False,vo='x11',geometry='800x450')
app=MpvtkApp.attach(handle,ext=False)
b=MpvtkBrowser(app,FakeSource())
b.nav_stack=[{'kind':'reader','server':'srv1'}]
entered=threading.Event(); release=threading.Event(); armed=threading.Event()
original_render=b._render_route
# Pause only the content-render phase. build(), _yield(), page-key lookup,
# both wire methods, renderer input resolution, and keypress are all real.
def render(route,size):
    if armed.is_set():
        armed.clear(); entered.set(); assert release.wait(5)
    return Column([Text('Reader content')],w=size[0],h=size[1])
b._render_route=render
keys=[]
app.on_key=lambda k:keys.append(k)
thread=threading.Thread(target=lambda:app.run(b.build),daemon=True); thread.start()
try:
    assert app.ready.wait(10)
    time.sleep(.4)
    for trial in range(3):
        b._browsing=True; app.set_active(True); app.invalidate();time.sleep(.2)
        release.clear();entered.clear();armed.set();app.invalidate()
        assert entered.wait(5)
        # Same foreign-thread handoff used by a remote playstate update.
        b.on_playstate({'stopped':False,'is_audio':False,'id':'movie','title':'Movie'})
        release.set();time.sleep(.5)
        # Wait through several additional empty-scene frames.
        for _ in range(3): app.invalidate();time.sleep(.08)
        handle.pause=False
        before=len(keys);handle.command('keypress','SPACE');time.sleep(.2)
        st=app.debug_state()
        bindings=[x for x in handle.input_bindings if x.get('key')=='SPACE']
        print(json.dumps({'trial':trial,'browsing':b._browsing,'active':st['active'],'pause':handle.pause,'keys':keys[before:],'bindings':bindings}))
        assert not b._browsing and not st['active'] and handle.pause is False and keys[before:]==['SPACE']
        app.claim_keys(())
        time.sleep(.15)
        handle.command('keypress','SPACE');time.sleep(.15)
        assert handle.pause is True, 'control: releasing the reader claim must restore SPACE pause'
        print('control: released claim restores SPACE pause')
    print('Three playback handoffs retain the reader SPACE binding.')
finally:
    release.set();app.quit();thread.join(3);b.shutdown();handle.terminate()
