import random
import datetime
import math

from ..clients import clientManager

# This started as a copy of some useful functions from jellyfin-chromecast's helpers.js and translated them to Python.
# Only reason their not put straight into __init__.py is to keep the same logical separation that
# jellyfin-chromecast has.
#
# I've since added some extra functions, and completely reworked some of the old ones such that it's
# not directly compatible.
#
# Should this stuff be in jellyfin_apiclient_python instead?
# Is this stuff in there already?
#
# FIXME: A lot of this could be done so much better with format-strings


# noinspection PyPep8Naming,PyPep8Naming
def getUrl(serverAddress, name):

    if not name:
        raise Exception("Url name cannot be empty")

    url = serverAddress
    url += "/" if not serverAddress.endswith("/") and not name.startswith("/") else ""
    url += name

    return url


# noinspection PyPep8Naming,PyPep8Naming
def getBackdropUrl(item, serverAddress):
    if item.get("BackdropImageTags"):
        return getUrl(
            serverAddress,
            "Items/"
            + item["Id"]
            + "/Images/Backdrop/0?tag="
            + item["BackdropImageTags"][0],
        )
    elif item.get("ParentBackdropItemId"):
        return getUrl(
            serverAddress,
            "Items/"
            + item["ParentBackdropItemId"]
            + "/Images/Backdrop/0?tag="
            + item["ParentBackdropImageTags"][0],
        )
    else:
        return None


# noinspection PyPep8Naming,PyPep8Naming
def getLogoUrl(item, serverAddress):
    if item.get("ImageTags", {}).get("Logo", None):
        return getUrl(
            serverAddress,
            "Items/" + item["Id"] + "/Images/Logo/0?tag=" + item["ImageTags"]["Logo"],
        )
    elif item.get("ParentLogoItemId") and item.get("ParentLogoImageTag"):
        return getUrl(
            serverAddress,
            "Items/"
            + item["ParentLogoItemId"]
            + "/Images/Logo/0?tag="
            + item["ParentLogoImageTag"],
        )
    else:
        return None


# noinspection PyPep8Naming,PyPep8Naming
def getPrimaryImageUrl(item, serverAddress):
    if item.get("AlbumPrimaryImageTag"):
        return getUrl(
            serverAddress,
            "Items/"
            + item["AlbumId"]
            + "/Images/Primary?tag="
            + item["AlbumPrimaryImageTag"],
        )
    elif item.get("PrimaryImageTag"):
        return getUrl(
            serverAddress,
            "Items/" + item["Id"] + "/Images/Primary?tag=" + item["PrimaryImageTag"],
        )
    elif item.get("ImageTags", {}).get("Primary"):
        return getUrl(
            serverAddress,
            "Items/"
            + item["Id"]
            + "/Images/Primary?tag="
            + item["ImageTags"]["Primary"],
        )
    else:
        return None


# noinspection PyPep8Naming
def getDisplayName(item):
    name = item.get("EpisodeTitle", item.get("Name"))

    if item["Type"] == "TvChannel":
        if item["Number"]:
            return item["Number"] + " " + name
        else:
            return name
    # NOTE: Must compare to None here because 0 is a legitimate option
    elif (
        item["Type"] == "Episode"
        and item.get("IndexNumber") is not None
        and item.get("ParentIndexNumber") is not None
    ):
        number = f"S{item['ParentIndexNumber']} E{item['IndexNumber']}"
        if item.get("IndexNumberEnd"):
            number += "-" + item["IndexNumberEnd"]
        name = number + " - " + name

    return name


# noinspection PyPep8Naming
def getRatingHtml(item):
    html = ""

    if item.get("CommunityRating"):
        html += (
            "<div class='starRating' title='"
            + str(item["CommunityRating"])
            + "'></div>"
        )
        html += '<div class="starRatingValue">'
        html += str(round(item["CommunityRating"], 1))
        html += "</div>"

    if item.get("CriticRating") is not None:
        # FIXME: This doesn't seem to ever be triggering. Is that actually a problem?
        if item["CriticRating"] >= 60:
            html += '<div class="fresh rottentomatoesicon" title="fresh"></div>'
        else:
            html += '<div class="rotten rottentomatoesicon" title="rotten"></div>'

        html += '<div class="criticRating">' + str(item["CriticRating"]) + "%</div>"

    # Jellyfin-chromecast had this commented out already
    # Where's the metascore variable supposed to come from?
    # # if item.get(Metascore) and metascore !== false: {
    # #     if item['Metascore'] >= 60:
    # #         html += '<div class="metascore metascorehigh" title="Metascore">' + item['Metascore'] + '</div>'
    # #     elif item['Metascore'] >= 40):
    # #         html += '<div class="metascore metascoremid"  title="Metascore">' + item['Metascore'] + '</div>'
    # #     else:
    # #         html += '<div class="metascore metascorelow"  title="Metascore">' + item['Metascore'] + '</div>'

    return html


def __convert_jf_str_datetime(jf_string):
    # datetime doesn't quite support fractions of a second the same way Jellyfin does them.
    # Best we can do is strip them out entirely.
    # FIXME: I think this loses timezone information, but are we getting any at all anyway?
    return datetime.datetime.strptime(jf_string.partition(".")[0], "%Y-%m-%dT%H:%M:%S")


# noinspection PyPep8Naming,PyPep8Naming,PyPep8Naming
def getMiscInfoHtml(item):
    # FIXME: Flake8 is complaining this function is too complex.
    #        I agree, this needs to be cleaned up, a lot.
    # FIXME: This shouldn't return HTML, the template should take care of that.

    miscInfo = []

    if item["Type"] == "Episode":
        if item.get("PremiereDate"):
            date = __convert_jf_str_datetime(item["PremiereDate"])
            text = date.strftime("%x")
            miscInfo.append(text)

    if item.get("StartDate"):
        date = __convert_jf_str_datetime(item["StartDate"])
        text = date.strftime("%x")
        miscInfo.append(text)

        if item["Type"] != "Recording":
            pass
            # Jellyfin-chromecast had this commented out already
            # # text = LiveTvHelpers.getDisplayTime(date)
            # # miscInfo.push(text)

    if item.get("ProductionYear") and item["Type"] == "Series":
        if item["Status"] == "Continuing":
            miscInfo.append(f"{item['ProductionYear']}-Present")
        elif item["ProductionYear"]:
            text = str(item["ProductionYear"])
            if item.get("EndDate"):
                endYear = __convert_jf_str_datetime(item["EndDate"]).year
                if endYear != item["ProductionYear"]:
                    text += "-" + str(endYear)
            miscInfo.append(text)

    if item["Type"] != "Series" and item["Type"] != "Episode":
        if item.get("ProductionYear"):
            miscInfo.append(str(item["ProductionYear"]))
        elif item.get("PremiereDate"):
            text = str(__convert_jf_str_datetime(item["PremiereDate"]).year)
            miscInfo.append(text)
    if item.get("RunTimeTicks") and item["Type"] != "Series":
        if item["Type"] == "Audio":
            # FIXME
            raise Exception("Haven't translated this to Python yet")
            # miscInfo.append(datetime.getDisplayRunningTime(item["RunTimeTicks"]))
        else:
            # Using math.ceil instead of round because I want the minutes rounded *up* specifically,
            # mostly because '1min' makes more sense than '0min' for a 1-59sec clip
            # FIXME: Alternatively, display '<1min' if it's 0?
            minutes = math.ceil(item["RunTimeTicks"] / 600000000)
            miscInfo.append(f"{minutes}min")

    if (
        item.get("OfficialRating")
        and item["Type"] != "Season"
        and item["Type"] != "Episode"
    ):
        miscInfo.append(item["OfficialRating"])

    if item.get("Video3DFormat"):
        miscInfo.append("3D")

    return "&nbsp;&nbsp;&nbsp;&nbsp;".join(miscInfo)


# For some reason the webview 2.3 js api will send a positional argument of None when there are no
# arguments being passed in. This really long argument name is here to catch that and hopefully not
# eat other intentional arguments.
# noinspection PyPep8Naming
def getRandomBackdropUrl(_positional_arg_that_is_never_used=None, **params):
    # This function is to get 1 random item, so ignore those arguments
    params["SortBy"] = "Random"
    params["Limit"] = 1

    # Use sensible defaults for all other arguments.
    # Based on jellyfin-chromecast's behaviour.
    params["IncludeItemTypes"] = params.get("IncludeItemTypes", "Movie,Series")
    params["ImageTypes"] = params.get("ImageTypes", "Backdrop")
    params["Recursive"] = params.get("Recursive", True)
    params["MaxOfficialRating"] = params.get("MaxOfficialRating", "PG-13")

    # This application can have multiple client connections different servers at the same time.
    # So just pick a random one of those clients to query for the random item.
    client = random.choice(list(clientManager.clients.values()))
    item = client.jellyfin.user_items(params=params)["Items"][0]

    return getBackdropUrl(item, client.config.data["auth.server"])
