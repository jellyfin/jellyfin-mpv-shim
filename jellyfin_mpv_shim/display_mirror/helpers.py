# This is a copy of some useful functions from jellyfin-chromecast's helpers.js and translated them to Python.
# Only reason their not put straight into __init__.py is to keep the same logical separation that jellyfin-chromecast has.
#
# Should this stuff be in jellyfin_apiclient_python instead?
# Is this stuff in there already?
#
# FIXME: A lot of this could be done so much better with format-strings


def getUrl(serverAddress, name):

    if not name:
        raise Exception("Url name cannot be empty")

    url = serverAddress
    url += '/' if not serverAddress.endswith('/') and not name.startswith('/') else ''
    url += name

    return url


def getBackdropUrl(item, serverAddress):
    if item.get('BackdropImageTags'):
        return getUrl(serverAddress, 'Items/' + item['Id'] + '/Images/Backdrop/0?tag=' + item['BackdropImageTags'][0])
    elif item.get('ParentBackdropItemId'):
        return getUrl(serverAddress, 'Items/' + item['ParentBackdropItemId'] + '/Images/Backdrop/0?tag=' + item['ParentBackdropImageTags'][0])
    else:
        return None


def getLogoUrl(item, serverAddress):
    if item.get('ImageTags', {}).get('Logo', None):
        return getUrl(serverAddress, 'Items/' + item['Id'] + '/Images/Logo/0?tag=' + item['ImageTags']['Logo'])
    elif item.get('ParentLogoItemId') and item.get('ParentLogoImageTag'):
        return getUrl(serverAddress, 'Items/' + item['ParentLogoItemId'] + '/Images/Logo/0?tag=' + item['ParentLogoImageTag'])
    else:
        return None


def getPrimaryImageUrl(item, serverAddress):
    if item.get('AlbumPrimaryImageTag'):
        return getUrl(serverAddress, 'Items/' + item['AlbumId'] + '/Images/Primary?tag=' + item['AlbumPrimaryImageTag'])
    elif item.get('PrimaryImageTag'):
        return getUrl(serverAddress, 'Items/' + item['Id'] + '/Images/Primary?tag=' + item['PrimaryImageTag'])
    elif item.get('ImageTags', {}).get('Primary'):
        return getUrl(serverAddress, 'Items/' + item['Id'] + '/Images/Primary?tag=' + item['ImageTags']['Primary'])
    else:
        return None


def getDisplayName(item):
    name = item.get('EpisodeTitle', item.get('Name'))

    if item['Type'] == "TvChannel":
        if item['Number']:
            return item['Number'] + ' ' + name
        else:
            return name
    # NOTE: Must compare to None here because 0 is a legitimate option
    elif item['Type'] == "Episode" and item['IndexNumber'] is not None and item['ParentIndexNumber'] is not None:
        number = "S" + item['ParentIndexNumber'] + ", " + "E" + item['indexNumber']
        if item['IndexNumberEnd']:
            number += "-" + item['IndexNumberEnd']
        name = number + " - " + name

    return name


def getRatingHtml(item):
    html = ""

    if item.get('CommunityRating'):
        html += "<div class='starRating' title='" + item['CommunityRating'] + "'></div>"
        html += '<div class="starRatingValue">'
        html += round(item['CommunityRating'], 1)
        html += '</div>'

    if item.get('CriticRating') is not None:

        if (item['CriticRating'] >= 60):
            html += '<div class="fresh rottentomatoesicon" title="fresh"></div>'
        else:
            html += '<div class="rotten rottentomatoesicon" title="rotten"></div>'

        html += '<div class="criticRating">' + item['CriticRating'] + '%</div>'

    # # Where's the metascore variable supposed to come from?
    # if item.get(Metascore) and metascore !== false: {
    #     if item['Metascore'] >= 60:
    #         html += '<div class="metascore metascorehigh" title="Metascore">' + item['Metascore'] + '</div>'
    #     elif item['Metascore'] >= 40):
    #         html += '<div class="metascore metascoremid"  title="Metascore">' + item['Metascore'] + '</div>';
    #     else:
    #         html += '<div class="metascore metascorelow"  title="Metascore">' + item['Metascore'] + '</div>';

    return html
