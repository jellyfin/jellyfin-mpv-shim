import ast
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import sys
sys.path.insert(0, '/home/izzie/bookmarks/scripts/jellyfin-mpv-shim')
from jellyfin_mpv_shim.media import Video
from jellyfin_mpv_shim.conf import settings

# Execute the production method without importing the module-level mpv singleton.
p = Path('/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/player.py')
cls = next(n for n in ast.parse(p.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'PlayerManager')
fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_apply_auth_headers')
module = ast.Module(body=[fn], type_ignores=[])
ns = {'log': logging.getLogger('probe')}
exec(compile(ast.fix_missing_locations(module), str(p), 'exec'), ns)
source = {'Protocol': 'Http', 'Path': 'https://cdn.example.invalid/movie.mkv', 'SupportsDirectPlay': True, 'SupportsDirectStream': True, 'Id': 'source', 'MediaStreams': []}
item = {'Type': 'Movie', 'Name': 'Remote shortcut', 'MediaSources': [source]}
client = NS(config=NS(data={'auth.server':'https://jellyfin.example.invalid', 'auth.token':'SYNTHETIC_REVIEW_TOKEN'}), http=NS(_get_authenication_header=lambda: 'MediaBrowser Token="SYNTHETIC_REVIEW_TOKEN"'), jellyfin=NS(get_item=lambda _: item))
v = Video('movie', NS(client=client, is_local=True))
pm = NS(_player=NS(), _mpv_alive=True)
v.auth_via_header = ns['_apply_auth_headers'](pm, v)
v.media_source = source
with patch.object(settings, 'direct_paths', True):
    url = v._get_url_from_source()
print('Selected stream URL:', url)
print('Foreign subtitle hosts:', v.foreign_subtitle_hosts())
print('mpv Authorization headers:', pm._player.http_header_fields)
assert url.startswith('https://cdn.example.invalid/')
assert 'SYNTHETIC_REVIEW_TOKEN' in pm._player.http_header_fields[0]
