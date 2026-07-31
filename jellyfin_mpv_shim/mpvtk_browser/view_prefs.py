"""Per-library view settings, shared with jellyfin-web.

The setting behind the Home Videos shape mismatch: a library remembers which
*image type* to draw its items with, and web skips its median-aspect rule
entirely when one is set. So two clients only agree for a user who has never
touched the control -- and the people who have are the ones who file issues.

Stored in the same DisplayPreferences ``CustomPrefs`` document as the home
layout, the guide settings and :mod:`user_prefs`.

**The key is not fully knowable from web's source.** ``getSettingsKey``
(``list.js:1265``) builds ``items-<type-or-parentId>-…`` and appends a route
type only when the route carried one, so the same library reached two ways
has two keys. A real setting observed in the wild was

    items-f4415c72cc16920fce19d78d636a3ce7-Folder-imageType: thumb

for a Home Videos library -- parent id *and* a ``Folder`` type. So rather
than pick one spelling and silently read nothing, :func:`keys_for` returns
the candidates in priority order and the reader takes the first that exists.
A write goes back to whichever key it was read from, so we never strand the
user's setting under a name their web client will not look at.
"""

#: Image types jellyfin-web offers per library view, and what each means for
#: the grid: ``(geometry attribute, image type requested)``.
#:
#: From ``ItemsView.tsx:75-88``. ``Primary`` is the odd one out -- it means
#: "no override", i.e. shape the row by its artwork, which is what
#: ``GridPage._grid_shape`` already does.
IMAGE_TYPES = {
    "primary": None,
    "thumb": ("geom_wide", "Thumb"),
    "banner": ("geom_banner", "Banner"),
    "disc": ("geom_square", "Disc"),
    "logo": ("geom_wide", "Logo"),
}

#: Web's default for every view except Studios (``settings.ts:22``), which we
#: have no equivalent of -- our Networks screen is a list route, not a
#: configurable library view.
DEFAULT_IMAGE_TYPE = "primary"

#: The route ``type`` web appends to the key, by collection type. Best-effort:
#: it depends on how the user navigated, so these are candidates rather than
#: facts. ``Folder`` is the observed one for a Home Videos library.
_ROUTE_TYPES = {
    "homevideos": ("Folder",),
    "photos": ("Folder",),
    "musicvideos": ("MusicVideo", "Folder"),
    "movies": ("Movie",),
    "tvshows": ("Series",),
    "music": ("MusicAlbum",),
    "boxsets": ("BoxSet",),
}


#: Boolean per-view settings and jellyfin-web's defaults for them
#: (``settings.ts:17-27``). Both on, which is what the shim did before any
#: of this existed -- so an untouched library looks exactly as it did.
BOOL_SETTINGS = {"showTitle": True, "showYear": True}

#: Stored ``viewType`` values that mean a list rather than a grid. Web's
#: legacy vocabulary is Poster / PosterCard / Thumb / ThumbCard / Banner /
#: List, of which only List is a different *renderer* -- the rest choose an
#: image type or a card style, which is what ``imageType`` covers here.
#: Anything unrecognised is a grid, so a value we do not draw degrades to
#: the normal view rather than to nothing.
LIST_VIEW = "List"
GRID_VIEW = "Poster"


def is_list_view(value):
    return str(value or "").strip().lower() == LIST_VIEW.lower()


def resolve_bool(custom_prefs, parent_id, collection_type, setting):
    """``(value, key)`` for one of :data:`BOOL_SETTINGS`.

    Written by web as the strings ``"true"``/``"false"``, same as every
    other CustomPrefs boolean -- see :mod:`user_prefs` for why that matters.
    """
    default = BOOL_SETTINGS[setting]
    prefs = custom_prefs or {}
    for key in keys_for(parent_id, collection_type, setting):
        raw = prefs.get(key)
        if raw is None or raw == "":
            continue
        return str(raw).strip().lower() == "true", key
    return default, None


def resolve_view_type(custom_prefs, parent_id, collection_type):
    """``(value, key)`` for the grid-or-list choice."""
    prefs = custom_prefs or {}
    for key in keys_for(parent_id, collection_type, "viewType"):
        raw = str(prefs.get(key) or "").strip()
        if raw:
            return raw, key
    return GRID_VIEW, None


def keys_for(parent_id, collection_type, setting="imageType"):
    """CustomPrefs keys that might hold ``setting`` for this library, best
    first.

    The bare ``items-<parentId>-<setting>`` is last rather than first: a
    typed key is more specific, and web writes one whenever the route it was
    on had a type. Reading the bare key first would shadow a real setting.
    """
    if not parent_id:
        return []
    out = ["items-%s-%s-%s" % (parent_id, route_type, setting)
           for route_type in _ROUTE_TYPES.get(collection_type or "", ())]
    out.append("items-%s-%s" % (parent_id, setting))
    return out


def resolve_image_type(custom_prefs, parent_id, collection_type):
    """``(image_type, key)`` -- the stored value and the key it came from.

    ``key`` is returned so a save lands where the reader looked; writing to
    a different one would leave the user's web client still reading the old
    value. ``None`` for the key means nothing was stored, and a write should
    use the first candidate.
    """
    prefs = custom_prefs or {}
    for key in keys_for(parent_id, collection_type):
        value = str(prefs.get(key) or "").strip().lower()
        if value in IMAGE_TYPES:
            return value, key
    return DEFAULT_IMAGE_TYPE, None


def shape_for(image_type):
    """``(geometry attribute, image type)`` for a stored value, or ``None``
    to leave the grid shaped by its artwork."""
    return IMAGE_TYPES.get((image_type or "").strip().lower())
