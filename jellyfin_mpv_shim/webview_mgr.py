import pprint

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
        webview.create_window('')
        # FIXME: Do I need to also run webview.start() here?
        #        Documentation implies I do, but that function doesn't exist for me, perhaps I'm running an older version


userInterface = UserInterface()


# FIXME: This should be a real file somewhere.
#        Support selecting different files based on config for theming.
# FIXME: Just use separate files/templates for separate content types
# Types seen so far: Movie, Episode, Series, Season, Video
template = jinja2.Template("""<html>
    <!-- NOTE: Most class names and such taken from the Jellyfin HTML so as to allow Jellyfin's css to be useful -->
    <head>
        <!-- FIXME: Load the specific theme configured for the current Jellyfin user -->
        <link rel="stylesheet" type="text/css" href="{{base_url}}/web/assets/css/site.css">
        <link rel="stylesheet" type="text/css" href="{{base_url}}/web/assets/css/fonts.css">
        <link rel="stylesheet" type="text/css" href="{{base_url}}/web/assets/css/librarybrowser.css">
        <link rel="stylesheet" type="text/css" href="{{base_url}}/web/themes/dark/theme.css">
        <style>
            html {
                background-image: url("{{base_url}}/Items/{{Id}}/Images/Backdrop");
                background-size: cover;
            }
            .parentName { margin: .1em 0 .25em }
            .itemName .infoText {
                {% if Type == 'Episode' %}
                    margin: .25em 0 .5em;
                {% elif Type == 'Movie' %}
                    margin: .1em 0 .5em
                {% endif %}
            }
        </style>
    </head>
    <body>
        <div class="detailImageContainer portraitDetailImageContainer">
            <img class="itemDetailImage" src="{{base_url}}/Items/{{Id}}/Images/Primary">
        </div>
        {% if Type == 'Episode' %}
            <h1 class="parentName">{{SeriesName}} - {{SeasonName}}</h1>
            <!-- FIXME: Get the episode number -->
            <h3 class="itemName infoText">#. {{Name}}</h1>
        {% elif Type == 'Movie' %}
            <h1 class="itemName infoText">{{Name}}</h1>
        {% endif %}
        {% if Taglines %}
            <!-- FIXME: Include multiple tag lines? Pick a random tagline? -->
            <h3 class="tagline">{{Taglines[0]}}</h3>
        {% endif %}
        <p class="overview">{{Overview}}</p>
    </body>
</html>""")


def DisplayContent(client, arguments):
    print("Displaying Content:", arguments)
    item = client.jellyfin.get_item(arguments['Arguments']['ItemId'])
    item['base_url'] = client.config.data["auth.server"]
    html = template.render(item)
    webview.load_html(html)
    # print(html)
    # pprint.pprint(item)
    # breakpoint()
    return
