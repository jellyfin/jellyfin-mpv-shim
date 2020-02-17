import importlib.resources

import webview  # Python3-webview in Debian, pywebview in pypi
import jinja2   # python3-jinja2 in Debian, Jinja2 in pypi

from ..clients import clientManager
from . import helpers


class UserInterface(object):
    """Mostly copied from cli_mgr.py"""
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop = lambda: None

    def login_servers(self):
        clientManager.cli_connect()

    def run(self):
        # Webview needs to be run in the MainThread.
        # Which is the only reason this is being done in the userinterface part anyway
        webview.create_window("Jellyfin MPV Shim", fullscreen=True)
        # FIXME: Do I need to also run webview.start() here?
        #        Documentation implies I do, but that function doesn't exist for me, perhaps I'm running an older version


userInterface = UserInterface()


# FIXME: Add some support for some sort of theming beyond Jellyfin's css, to select user defined templates
# FIXME: jellyfin-chromecast uses html & CSS, should've started from there
def get_html(item: dict, server_address: str):
    jinja_vars = {
        # 'waiting_backdrop_src':
        'backdrop_src': helpers.getBackdropUrl(item, server_address) or '',
        'image_src': helpers.getPrimaryImageUrl(item, server_address),
        'logo_src': helpers.getLogoUrl(item, server_address),
        'played': item['UserData'].get('Played', False),
        'played_percentage': item['UserData'].get('PlayedPercentage', 0),
        'unplayed_items': item['UserData'].get('UnplayedItemCount', 0),
        'is_folder': item['IsFolder'],
        'display_name': helpers.getDisplayName(item) or '',
        'overview': item.get('Overview', ''),
        # I think this would be better with ', ' but jellyfin-chromecast used ' / '
        'genres': ' / '.join(item['Genres']),

        # I believe these are all specifically for albums
        'poster_src': helpers.getPrimaryImageUrl(item, server_address) or '',
        'title': 'title',  # FIXME
        'secondary_title': 'secondary',  # FIXME
        'artist': 'artist',  # FIXME
        'album_title': 'album',  # FIXME

        # FIXME: Use a <link> thing to load this directly
        'chromecast_css': importlib.resources.read_text(__package__, 'index.css'),
    }

    try:
        tpl = jinja2.Template(importlib.resources.read_text(__package__, 'index.html'))
        return tpl.render(jinja_vars, theme='dark')
    except Exception:
        breakpoint()


def DisplayContent(client, arguments):
    # If the webview isn't ready yet, just don't bother.
    # I could try and be more clever and check if we've loaded this module first,
    # but more complexity leaves more room for bugs and I don't think we need to care about DisplayContent() happening too early.
    #
    # NOTE: timeout=0 and timeout=None mean 2 different things.
    if not webview.webview_ready(timeout=0):
        return

    item = client.jellyfin.get_item(arguments['Arguments']['ItemId'])
    html = get_html(item, server_address=client.config.data["auth.server"])
    webview.load_html(html)
    # print(html)
    # breakpoint()
    return
