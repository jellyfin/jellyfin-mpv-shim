import os
import sys
import time

ROOT = '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim'
sys.path[:0] = [ROOT, ROOT + '/tests/integration']
sys.argv = [sys.argv[0]]
from test_mpvtk_hud import TestPlaybackHudLifecycle

case = TestPlaybackHudLifecycle(methodName='runTest')
case.setUp()

def prop(name):
    try:
        if os.environ.get('JMS_TEST_BACKEND') != 'jsonipc':
            return case.handle._get_property(name)
        return case.handle.command('get_property', name)
    except Exception:
        return None

def type_command(text):
    for ch in text:
        case.handle.command('keypress', 'SPACE' if ch == ' ' else ch)
    case.handle.command('keypress', 'ENTER')

try:
    case.ctl.key_opts = {'grab': False, 'key': 'ENTER', 'hide': 60,
                         'mode': 'hover', 'click': True}
    case._play_video()
    case._wait(lambda: case._state().get('phud_mode'))
    for cycle in range(3):
        if case._state().get('phud_shown'):
            case.handle.command('keypress', 'ESC')
            case._wait(lambda: not case._state().get('phud_shown'))
        case.handle.command('script-binding', 'console/enable')
        case._wait(lambda: prop('user-data/mpv/console/open') is True,
                   msg='real console did not open')
        case.handle.command('mouse', 180 + cycle * 30, 170)
        time.sleep(0.2)
        case.handle.command('mouse', 210 + cycle * 30, 190)
        case._wait(lambda: case._state().get('phud_shown'),
                   msg='HUD did not summon while real console held keys')
        token = 'round%s' % cycle
        type_command('set user-data/review-console-result ' + token)
        case._wait(lambda: prop('user-data/review-console-result') == token,
                   msg='console command lost to the summoned HUD')
        case.handle.command('keypress', 'ESC')
        time.sleep(0.25)
        if prop('user-data/mpv/console/open') is True:
            case.handle.command('keypress', 'ESC')
        case._wait(lambda: prop('user-data/mpv/console/open') is not True,
                   msg='real console did not close')
        case.handle.command('mouse', 250 + cycle * 30, 230)
        time.sleep(0.2)
        case.handle.command('mouse', 280 + cycle * 30, 260)
        case._wait(lambda: any(n.get('id') == 'hud-pp' for n in case.app._nodes))
        case.ctl.calls.clear()
        case._real_click('hud-pp')
        case._wait(lambda: 'toggle_pause' in case.ctl.calls,
                   msg='HUD play/pause mouse binding lost after real console')
        case.handle.command('keypress', 'ENTER')
        case._wait(lambda: case._state().get('phud_kbd'),
                   msg='HUD keyboard wake lost after real console')
        print(os.environ.get('JMS_TEST_BACKEND'), 'cycle', cycle,
              'console command, close, HUD real click and key wake passed', flush=True)
finally:
    case.tearDown()
    case.doCleanups()
