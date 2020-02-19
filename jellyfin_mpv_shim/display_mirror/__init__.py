import threading
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
        # Since webview.create_window takes exclusive and permanent lock on the main thread,
        # we need to start this wait_load function before we start webview itself.
        threading.Thread(target=self.wait_load).start()

        # Webview needs to be run in the MainThread.
        # Which is the only reason this is being done in the userinterface part anyway
        webview.create_window("Jellyfin MPV Shim", js_api=helpers, fullscreen=True)
        # FIXME: Do I need to also run webview.start() here?
        #        Documentation implies I do, but that function doesn't exist for me, perhaps I'm running an older version

    def wait_load(self):
        webview.webview_ready()
        html = get_html()
        webview.load_html(html)


userInterface = UserInterface()


# FIXME: Add some support for some sort of theming beyond Jellyfin's css, to select user defined templates
# FIXME: jellyfin-chromecast uses html & CSS, should've started from there
def get_html(server_address=None, item=None):
    if item:
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
            'genres': item['Genres'],

            # I believe these are all specifically for albums
            'poster_src': helpers.getPrimaryImageUrl(item, server_address) or '',
            'title': 'title',  # FIXME
            'secondary_title': 'secondary',  # FIXME
            'artist': 'artist',  # FIXME
            'album_title': 'album',  # FIXME
        }
    else:
        jinja_vars = {
            'random_backdrop': True,  # Make the jinja template load some extra JS code for random backdrops
            'backdrop_src': helpers.getRandomBackdropUrl(),  # Preinitialise it with a random backdrop though
            'display_name': "Ready to cast",
            'overview': "\n\nSelect your media in Jellyfin and play it here",  # FIME: Mention the player_name here
        }
    with importlib.resources.path(__package__, 'glyphicons.css') as glyphicons_css, \
            importlib.resources.path(__package__, 'jellyfin.css') as jellyfin_css:
        jinja_vars.update({
            'glyphicons_css': str(glyphicons_css),
            'jellyfin_css': str(jellyfin_css),
        })

        try:
            tpl = jinja2.Template(importlib.resources.read_text(__package__, 'index.html'))
            return tpl.render(jinja_vars)
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
    html = get_html(server_address=client.config.data["auth.server"], item=item)
    webview.load_html(html)
    # print(html)
    # breakpoint()
    return
