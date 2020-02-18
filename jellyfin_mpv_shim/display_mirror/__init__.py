import random
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
        webview.create_window("Jellyfin MPV Shim", fullscreen=True)
        # FIXME: Do I need to also run webview.start() here?
        #        Documentation implies I do, but that function doesn't exist for me, perhaps I'm running an older version

    def wait_load(self):
        webview.webview_ready()
        # Pick a random connected client for loading the backdrop data from
        client = random.choice(list(clientManager.clients.values()))
        html = get_html(client=client)
        webview.load_html(html)


userInterface = UserInterface()


# FIXME: Add some support for some sort of theming beyond Jellyfin's css, to select user defined templates
# FIXME: jellyfin-chromecast uses html & CSS, should've started from there
def get_html(client, item_id: str = ''):
    server_address = client.config.data["auth.server"]
    if item_id:
        item = client.jellyfin.get_item(item_id)
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
        # Get a single random movie or TV series available to the current user with PG-13 or less rating.
        # PG-13 because even though the current user might be 18+, that doesn't mean the device isn't in a shared location.
        # This logic is copied from jellyfin-chromecast.
        item = client.jellyfin.user_items(params={
            'SortBy': "Random",
            'IncludeItemTypes': "Movie,Series",
            'ImageTypes': 'Backdrop',
            'Recursive': True,
            'MaxOfficialRating': 'PG-13',
            'Limit': 1,
        })['Items'][0]
        jinja_vars = {
            'backdrop_src': helpers.getBackdropUrl(item, server_address),
            'logo_src': server_address + ('/' if not server_address.endswith('/') else '') + "favicon.ico",
            'display_name': "Ready to cast",
            'overview': "\n\nSelect your media in Jellyfin and play it here",
        }
    with importlib.resources.path(__package__, 'glyphicons.css') as glyphicons_css, importlib.resources.path(__package__, 'jellyfin.css') as jellyfin_css:
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

    html = get_html(client=client, item_id=arguments['Arguments']['ItemId'])
    webview.load_html(html)
    # print(html)
    # breakpoint()
    return
