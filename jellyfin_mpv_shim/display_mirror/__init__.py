import threading
import importlib.resources

import webview as webview_module  # Python3-webview in Debian, pywebview in pypi
import jinja2   # python3-jinja2 in Debian, Jinja2 in pypi

from ..clients import clientManager
from . import helpers

import threading

helpers.on_escape = lambda _: wait_load_home()
webview = webview_module.create_window("Jellyfin MPV Shim", js_api=helpers, fullscreen=True)

webview_ready_event = threading.Event()
webview.loaded += webview_ready_event.set

def webview_ready(timeout=None):
    return webview_ready_event.wait(timeout)

class UserInterface(object):
    """Mostly copied from cli_mgr.py"""
    def __init__(self):
        global horrible_hack
        self.open_player_menu = lambda: None
        self.stop = lambda: None

    def login_servers(self):
        clientManager.cli_connect()

    def run(self):
        # Since webview.create_window takes exclusive and permanent lock on the main thread,
        # we need to start this wait_load function before we start webview itself.
        threading.Thread(target=wait_load_home).start()

        # This makes me rather uncomfortable, but there's no easy way around this other than importing display_mirror in helpers.
        # Lambda needed because the JS api adds an argument even when not used.

        # Webview needs to be run in the MainThread.
        # Which is the only reason this is being done in the userinterface part anyway
        webview_module.start()

        # FIXME: Do I need to also run webview.start() here?
        #        Documentation implies I do, but that function doesn't exist for me, perhaps I'm running an older version


userInterface = UserInterface()


# FIXME: Add some support for some sort of theming beyond Jellyfin's css, to select user defined templates
def get_html(server_address=None, item=None):
    if item:
        jinja_vars = {
            'backdrop_src': helpers.getBackdropUrl(item, server_address) or '',
            'image_src': helpers.getPrimaryImageUrl(item, server_address) or '',
            'logo_src': helpers.getLogoUrl(item, server_address) or '',
            'played': item['UserData'].get('Played', False),
            'played_percentage': item['UserData'].get('PlayedPercentage', 0),
            'unplayed_items': item['UserData'].get('UnplayedItemCount', 0),
            'is_folder': item['IsFolder'],
            'display_name': helpers.getDisplayName(item),
            'misc_info_html': helpers.getMiscInfoHtml(item),
            'rating_html': helpers.getRatingHtml(item),
            'genres': item['Genres'],
            'overview': item.get('Overview', ''),

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
    with importlib.resources.path(__package__, 'jellyfin.css') as jellyfin_css:
        jinja_vars.update({
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
    if not webview_ready(timeout=0):
        return

    item = client.jellyfin.get_item(arguments['Arguments']['ItemId'])
    html = get_html(server_address=client.config.data["auth.server"], item=item)
    webview.load_html(html)
    # print(html)
    # breakpoint()
    return


def wait_load_home():
    # Wait for webview to be ready, then load the home page.
    # Useful for loading the initial page before displaying any content,
    # and for refreshing to a blank page after idling.
    webview_ready()

    html = get_html()
    webview.load_html(html)
