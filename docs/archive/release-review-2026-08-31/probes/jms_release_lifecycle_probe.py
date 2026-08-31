import os, sys, threading
from unittest import mock
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/tests/integration')
sys.argv = ['review-probe']
from jellyfin_mpv_shim.conf import settings
settings.health_check_interval = None
from jellyfin_mpv_shim import clients as mod
from jellyfin_mpv_shim.users import UserManager
from test_clients_concurrency import FakeClient, make_manager, server

for iteration in range(3):
    users = UserManager()
    users.save = lambda: None
    old = users.add_user('Old user')
    new = users.add_user('New user')
    users.set_active(old['id'])
    srv = server('old-credential', address='http://old-server:8096')
    users.set_active_credentials([srv])
    started, release = threading.Event(), threading.Event()
    def authenticate(client):
        started.set()
        assert release.wait(5)
    fake = FakeClient(on_authenticate=authenticate)
    cm = make_manager(lambda: fake)
    with mock.patch.object(mod, 'userManager', users):
        cm._adopt_active_user()
        pending = threading.Thread(target=cm.connect_client, args=(cm.credentials[0],))
        pending.start()
        assert started.wait(5)
        assert cm.switch_user(new['id'])
        assert users.active_id == new['id'] and not cm.credentials and not cm.clients
        release.set()
        pending.join(5)
        print('switch iteration', iteration, 'active=new', 'new_credentials=', cm.credentials,
              'registered=', list(cm.clients), 'old_client_stopped=', fake.stopped)
        cm.stop()

from jellyfin_mpv_shim import users as users_mod
from jellyfin_mpv_shim.mpvtk_browser.ui import UserInterface
from jellyfin_mpv_shim.mpvtk_browser.gateway import deps
from tests._shell_harness import FakeSource, FakeController, _NeverPool
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

users = UserManager()
users.save = lambda: None
locked_user = users.add_user('PIN protected')
users.set_active(locked_user['id'])
users.set_pin(locked_user['id'], '4321', require_startup=True)
srv = server('srv1', address='http://not-a-real-network.invalid:8096')
users.set_active_credentials([srv])
fake = FakeClient(sessions=[{'DeviceId': locked_user['device_id']}])
fake.config.data.update({'auth.token': 'fake-token', 'auth.user_id': 'protected-jellyfin-user', 'auth.server': srv['address']})
cm = make_manager(lambda: fake)
with mock.patch.object(mod, 'userManager', users), mock.patch.object(users_mod, 'userManager', users), mock.patch.object(deps, 'clientManager', cm):
    cm._adopt_active_user()
    b = MpvtkBrowser(app=None, source=FakeSource(), controller=FakeController())
    b._pool.shutdown(wait=False)
    b._pool = _NeverPool()
    ui = UserInterface()
    ui._browser = b
    cm.on_server_connected = ui._on_server_connected
    for iteration in range(3):
        cm.stop_all_clients()
        b.source = FakeSource()
        b.server = None
        b.show_locked()
        assert b._locked and b.route['kind'] == 'locked'
        cm.check_all_clients()
        print('PIN iteration', iteration, 'startup_needs_unlock=', users.startup_needs_unlock(),
              'browser_locked=', b._locked, 'route=', b.route['kind'], 'clients=', list(cm.clients))
    b.shutdown()
    cm.stop()
