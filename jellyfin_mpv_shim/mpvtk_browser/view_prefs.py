"""Per-library view settings, shared with jellyfin-web.

The setting behind the Home Videos shape mismatch: a library remembers which
*image type* to draw its items with, and web skips its median-aspect rule
entirely when one is set. So two clients only agree for a user who has never
touched the control -- and the people who have are the ones who file issues.

Stored in the same DisplayPreferences ``CustomPrefs`` document as the home
layout, the guide settings and :mod:`user_prefs` -- one document, several
clients' settings: ``docs/jellyfin-api-notes.md`` section 7.

**The key is not fully knowable from web's source**, because ``getSettingsKey``
appends a route type only when the route carried one -- so the same library
reached two ways has two keys. :func:`keys_for` therefore returns candidates
in priority order and the reader takes the first that exists; a write goes
back to whichever key it was read from, so the user's setting is never
stranded under a name their web client will not look at. The observed
evidence is section 7.3.
"""

#: Image types jellyfin-web offers per library view, and what each means for
#: the grid: ``(geometry attribute, image type requested)``.
#:
#: **This is one axis, and it carries the list view too.** web's own picker
#: (``viewSettings.template.html``) is a single dropdown of primary / banner
#: / disc / logo / thumb / **list**, all written to ``-imageType``; both
#: readers agree that ``primary`` means auto -- legacy ``list.js`` falls
#: through to ``shape: 'autoVertical'`` and the modern ``ItemsView`` to
#: ``CardShape.Auto``. Poster and PosterCard are NOT on this axis: they are
#: the legacy tabbed library screens' ``<key>-_view`` setting, a different
#: key on a different screen, where Poster means shape 'portrait' and
#: PosterCard means the same with the title in a box under the art.
IMAGE_TYPES = {
    "primary": None,
    "thumb": ("geom_wide", "Thumb"),
    "banner": ("geom_banner", "Banner"),
    "disc": ("geom_square", "Disc"),
    "logo": ("geom_wide", "Logo"),
    # OURS, and there is no value on this axis to borrow: "primary" already
    # means auto in both of web's readers, and the one place web does say
    # "poster" is a setting for a screen we have no equivalent of. Auto
    # usually comes out as posters -- but a Home Videos library holding a few
    # portrait clips among landscape ones has a median that says landscape
    # and no way to argue with it. This is that argument.
    #
    # It asks the server for exactly what Auto does, so the two never
    # disagree about which artwork exists; only the shape is forced. web does
    # not recognise the value and falls through to its own auto branch, which
    # is the closest thing it has -- so sharing the setting degrades rather
    # than breaks.
    "poster": ("geom", "Primary"),
    # Not a shape: the table renderer. Web's sixth dropdown entry, and the
    # reason the list view has to live on THIS key -- see is_list.
    "list": None,
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

#: The list view, as web stores it: a value of ``imageType``, not a setting
#: of its own. ``viewSettings.js`` writes ``-imageType: list`` and
#: ``list.js`` renders a table for it (``settings.imageType === 'list'``).
LIST_IMAGE_TYPE = "list"

#: What ``imageType`` goes back to when the list view is switched off. Web's
#: dropdown has no "off" -- picking any other entry is how you leave the list
#: -- so this is the entry our checkbox picks on your behalf.
GRID_IMAGE_TYPE = DEFAULT_IMAGE_TYPE

#: Stored ``viewType`` values, which THIS CLIENT used to write and nothing in
#: jellyfin-web has ever read: its ``list.js`` reads ``-viewType`` with a
#: default of 'images' and no writer anywhere sets it. Kept as a read-only
#: fallback so a library someone put in list view before the setting moved
#: onto the shared key still comes up as a list.
LIST_VIEW = "List"
GRID_VIEW = "Poster"


def is_list_view(value):
    """The legacy read: our own ``viewType`` value. See :data:`LIST_VIEW`."""
    return str(value or "").strip().lower() == LIST_VIEW.lower()


def is_list(image_type, view_type=None):
    """Should this library be drawn as a table rather than a grid?

    The shared answer first -- ``imageType: list``, which is what web's own
    picker writes and reads -- then the value this client used to write to a
    key nothing else has ever read.
    """
    if str(image_type or "").strip().lower() == LIST_IMAGE_TYPE:
        return True
    return is_list_view(view_type)


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
