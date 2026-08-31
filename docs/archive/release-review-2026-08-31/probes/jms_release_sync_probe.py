import errno
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
sys.argv = [sys.argv[0]]
from jellyfin_mpv_shim.sync.db import SyncDB
from jellyfin_mpv_shim.sync.manager import SyncManager

logging.disable(logging.CRITICAL)

# The server call is a controlled scheduling point for an offline stop report.
for iteration in range(3):
    with tempfile.TemporaryDirectory() as root:
        m = SyncManager()
        m.db = SyncDB(os.path.join(root, 'catalog.db'))
        m.db.upsert_playstate('server', 'episode', position_ticks=10)
        sent = []
        def get_userdata(item):
            m.db.upsert_playstate('server', item, position_ticks=100, played=True)
            return {'PlaybackPositionTicks': 0, 'Played': False}
        api = SimpleNamespace(get_userdata_for_item=get_userdata,
                              update_userdata_for_item=lambda item, data: sent.append(data))
        m.get_client = lambda server: SimpleNamespace(jellyfin=api)
        m._sync_playstate()
        assert sent == [{'PlaybackPositionTicks': 10}], sent
        assert m.db.list_playstate() == [], m.db.list_playstate()
        print('replay race', iteration, 'sent=', sent, 'pending=', m.db.list_playstate())
        m.db.close()

# Simulate a cross-device move that fills the destination after the catalog
# copies successfully. Only the worker body is disabled, not recovery logic.
with tempfile.TemporaryDirectory() as root:
    old = Path(root) / 'old'
    new = Path(root) / 'new'
    old.mkdir()
    m = SyncManager()
    m.root = str(old)
    m.db = SyncDB(str(old / 'catalog.db'))
    m.db.upsert({'item_id': 'episode', 'server_id': 'server',
                 'file_path': 'server/episode/media.mkv', 'status': 'complete'})
    media = old / 'server/episode/media.mkv'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'media')
    copy_tree = m._copy_tree
    def copy_or_full(src, dst, state, progress):
        if os.path.basename(src) == 'server':
            raise OSError(errno.ENOSPC, 'destination full')
        return copy_tree(src, dst, state, progress)
    m._copy_tree = copy_or_full
    m._run = lambda gen: None
    real_listdir = os.listdir
    def catalog_first(path):
        return sorted(real_listdir(path))
    with patch('os.rename', side_effect=OSError(errno.EXDEV, 'cross device')), patch('os.listdir', side_effect=catalog_first):
        ok, message = m.relocate(str(new))
    assert not ok
    assert m.root == str(old)
    assert m.db.get('episode') is None
    stranded = SyncDB(str(new / 'catalog.db'), read_only=True)
    assert stranded.get('episode') is not None
    assert not media.exists()
    print('failed relocation', 'returned=', ok, 'old_catalog=', m.db.list(),
          'new_catalog_has_episode=', bool(stranded.get('episode')), 'media_still_old=', media.exists(),
          'media_at_new=', (new / 'server/episode/media.mkv').exists())
    stranded.close()
    m.stop()
