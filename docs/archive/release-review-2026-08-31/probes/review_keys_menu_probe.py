import sys
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
import _harness as h
p = h.import_player_with_fake_mpv()
from jellyfin_mpv_shim.menu import OSDMenu
from unittest.mock import patch
pm=h.build_player(p)
pm.menu=OSDMenu(pm,pm._player)
for cycle in range(3):
    old=pm._player
    old.terminate()
    pm._mpv_alive=False
    old.playback_abort=True
    pm.menu_action("settings")
    new=pm._player
    bindings=[b for b in new.input_bindings if b.get('section')=='jms_menu']
    print('cycle',cycle+1,'recreated=',new is not old,'menu_shown=',pm.menu.is_menu_shown,'menu_binding_count=',len(bindings))
    assert new is not old
    assert pm.menu.is_menu_shown
    assert not bindings
    pm.menu.hide_menu()
print('CONFIRMED: all three reopened menus have no jms_menu bindings')
