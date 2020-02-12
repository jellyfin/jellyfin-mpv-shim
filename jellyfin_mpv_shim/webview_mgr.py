import pprint
import importlib.resources

import webview  # Python3-webview in Debian, pywebview in pypi
import jinja2   # python3-jinja2 in Debian, Jinja2 in pypi

from .clients import clientManager


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
def get_html(jinja_vars):
    template_filename = f"{jinja_vars['Type']}.html"
    if importlib.resources.is_resource('jellyfin_mpv_shim.templates', template_filename):
        tpl = jinja2.Template(importlib.resources.read_text('jellyfin_mpv_shim.templates', template_filename))
        return tpl.render(jinja_vars, theme='dark')
    else:
        # FIXME: This is just for debugging
        return "<pre>" + pprint.pformat(jinja_vars) + "</pre>"


def DisplayContent(client, arguments):
    print("Displaying Content:", arguments)
    item = client.jellyfin.get_item(arguments['Arguments']['ItemId'])
    item['base_url'] = client.config.data["auth.server"]
    html = get_html(item)
    webview.load_html(html)
    # print(html)
    # breakpoint()
    return
