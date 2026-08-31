"""Read-only browser release-review reproductions.
Run from repository: xvfb-run -a /home/izzie/.venv/bin/python /tmp/jms_release_browser_probe.py
"""
import sys
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.argv = ['probe']
import logging
logging.disable(logging.CRITICAL)
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
from jellyfin_mpv_shim.mpvtk_browser.async_runner import AsyncRunner
from jellyfin_mpv_shim.mpvtk_browser.pagination import Paginator
from tests._shell_harness import FakeSource, _DeferredPool, build_scene


def unfinished_back():
    b = MpvtkBrowser(app=None, source=FakeSource())
    b._pool.shutdown(wait=True)
    b._pool = _DeferredPool()
    r = {'kind':'grid','server':'srv1','parent_id':'lib1','title':'Movies'}
    try:
        b.navigate(r)
        _, handlers = build_scene(b)
        handlers['nav-search']['submit']('test')
        b._pool.drain()
        b.go_back()
        for _ in range(3):
            nodes, _ = build_scene(b)
            b._pool.drain()
        print('BACK:', {'route':b.route['kind'], 'items':r.get('_items'),
              'error':r.get('_error'), 'pending_jobs':len(b._pool.queued),
              'queries':len(b.source.queries), 'busy':any(n['t']=='busy' for n in nodes)})
        assert b.route is r and r.get('_items') is None and not b._pool.queued
        assert any(n['t']=='busy' for n in nodes)
    finally:
        b.shutdown()


def preferences_lost_update():
    class Api:
        def __init__(self):
            self.dto = {'Id':'user','Client':'emby','CustomPrefs':{
                'items-lib1-Movie-showTitle':'true',
                'items-lib1-Movie-showYear':'true','unrelated':'keep'}}
            self.reads = Barrier(2)
        def get_user_settings(self, **kw):
            snapshot = deepcopy(self.dto)
            self.reads.wait(timeout=2)
            return snapshot
        def update_user_settings(self, dto, **kw):
            self.dto = deepcopy(dto)
    api = Api()
    src = LibrarySource.__new__(LibrarySource)
    src._conns = {'srv1':SimpleNamespace(api=api)}
    src._custom_prefs = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(src.save_view_setting,'srv1','lib1','movies',key,False)
                for key in ('showTitle','showYear')]
        for job in jobs:
            job.result()
    custom = api.dto['CustomPrefs']
    print('PREFERENCES:', custom)
    assert sorted(custom['items-lib1-Movie-'+k] for k in ('showTitle','showYear')) == ['false','true']


def pagination_retry_loop():
    redraws = []
    run = AsyncRunner(lambda: redraws.append(1))
    run.shutdown(wait=True)
    run.pool = _DeferredPool()
    route = {'_total':100,'_page':1}
    paginator = Paginator(run,lambda *_:600,lambda _:True,lambda _:None,
                         lambda:None,lambda:True,lambda *_:5)
    calls = []
    def fetch(start, limit):
        calls.append(start)
        raise RuntimeError('server returns 503')
    for frame in range(3):
        paginator.ensure(route,10,fetch)
        run.pool.drain()
        print('PAGINATION:', {'frame':frame,'requests':len(calls),'redraws':len(redraws)})
    assert len(calls) == len(redraws) == 9

unfinished_back()
preferences_lost_update()
pagination_retry_loop()
print('All three current bugs reproduced.')
