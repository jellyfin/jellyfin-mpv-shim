import sys, time, os
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0,'/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
sys.argv=['probe']
from test_mpvtk_browser import TestLongDropdownScroll
case=TestLongDropdownScroll()
case.setUp()
def wait(): time.sleep(.25)
def move(x,y): case.handle.command('mouse',int(x),int(y)); wait()
def press(): case.handle.command('keydown','MBTN_LEFT'); wait()
def release(): case.handle.command('keyup','MBTN_LEFT'); wait()
def center(id):
    n=next(n for n in case.app._nodes if n.get('id')==id)
    return n['x']+n['w']/2,n['y']+n['h']/2
try:
    assert case.app.ready.wait(10)
    time.sleep(.5)
    for i in range(3):
        move(*center('long-dd')); press(); release()
        st=case.app.debug_state(); g=st['dd_geo']
        assert st.get('dd_open')
        th=max(18,(g['n']*g['ih']-8)*g['n']/g['count'])
        move(g['x']+g['w']-6,g['y']+4+th/2); press()
        case.app.set_active(False); wait()
        release()
        case.app.set_active(True); case.app.invalidate(); wait()
        move(*center('under-btn'))
        print('suspend-dropdown',i,'hover=',case.app.debug_state().get('hover'))
        press(); release()
        move(*center('under-btn'))
        print('after-one-click',i,'hover=',case.app.debug_state().get('hover'))
finally:
    case.tearDown()
