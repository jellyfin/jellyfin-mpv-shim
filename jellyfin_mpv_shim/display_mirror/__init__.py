import threading
import importlib.resources

import jinja2   # python3-jinja2 in Debian, Jinja2 in pypi

# So, most of my use of the webview library is ugly but there's a reason for this!
# Debian's python3-webview package is super old (2.3), pip3's pywebview package is much newer (3.2).
# I would just say "fuck it, install from testing/backports/unstable" except that's not any newer either.
# So instead I'm going to try supporting *both* here, which gets rather ugly because they made very significant changes.
#
# The key differences seem to be:
# 3.2's create_window() returns a Window object immediately, then you need to call start()
# 2.3's create_window() blocks forever effectivelly calling start() itself
#
# 3.2's Window has a .loaded Event that you need to subscribe to notice when the window is ready for input
# 2.3 has a webview_ready() function that blocks until webview is ready (or timeout is passed)
import webview  # Python3-webview in Debian, pywebview in pypi

from ..clients import clientManager
from . import helpers


# This makes me rather uncomfortable, but there's no easy way around this other than importing display_mirror in helpers.
# Lambda needed because the 2.3 version of the JS api adds an argument even when not used.
helpers.on_escape = lambda _=None: load_idle()


class DisplayMirror(object):
    display_window = None

    def __init__(self):
        self.open_player_menu = lambda: None

    def run(self):
        # Webview needs to be run in the MainThread.

        # Prepare for version 2.3 before calling create_window(), which might block forever.
        self.display_window = webview
        # Since webview.create_window might take exclusive and permanent lock on the main thread,
        # we need to start this wait_load function before we start webview itself.
        if 'webview_ready' in dir(webview):
            threading.Thread(target=lambda: (webview.webview_ready(), load_idle())).start()

        maybe_display_window = webview.create_window(title="Jellyfin MPV Shim Mirror", js_api=helpers, fullscreen=True)
        if maybe_display_window is not None:
            # It returned a Window object instead of blocking, we're running on 3.2 (or compatible)
            self.display_window = maybe_display_window

            # 3.2's .loaded event runs every time a new DOM is loaded as well, so not suitable for this purpose
            # However, 3.2's load_html waits for the DOM to be ready, so we can completely skip waiting for that ourselves.
            threading.Thread(target=load_idle).start()

            webview.start()

    def stop(self):
        webview.destroy_window()

    def DisplayContent(self, client, arguments):
        item = client.jellyfin.get_item(arguments['Arguments']['ItemId'])
        html = get_html(server_address=client.config.data["auth.server"], item=item)
        self.display_window.load_html(html)
        # print(html)
        # breakpoint()
        return


mirror = DisplayMirror()


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
            'jellyfin_css': str(open(jellyfin_css).read()),
        })

        try:
            tpl = jinja2.Template(importlib.resources.read_text(__package__, 'index.html'))
            return tpl.render(jinja_vars)
        except Exception:
            breakpoint()


def load_idle():
    # FIXME: Add support for not actually having an idle screen and instead hide/close/something the window
    # Load the initial page before displaying any content,
    # and when refreshing to a blank page after idling.
    html = get_html()
    mirror.display_window.load_html(html)
