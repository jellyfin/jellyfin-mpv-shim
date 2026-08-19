"""``GET /Items``, which is where every item query should go.

`jellyfin-apiclient-python`'s ``get_user_items`` calls the ``[Obsolete]``
``GET /Users/{userId}/Items``. **That is the same handler**, but the legacy
action delegates **positionally** and passes a literal ``[]`` for the
language arrays, so it silently drops ``audioLanguages``,
``subtitleLanguages`` and ``indexNumber`` -- a filter can be perfectly well
formed, perfectly supported by the server, and do nothing. Moving a query
here changes which parameters survive the trip and nothing else. The
measurements, and why the user goes in as a query parameter, are in
docs/jellyfin-api-notes.md section 2.

Signature-compatible with ``get_user_items`` on purpose -- most call sites
spread a kwargs dict built somewhere else, and a rename that also reshuffled
arguments would be a migration nobody could review.
"""

#: Keyword -> query parameter, exactly as ``get_user_items`` maps them.
#: `tests/test_items_api.py` diffs this against the apiclient's own
#: signature, because a keyword we fail to map is not an error -- it is a
#: filter that silently stops being applied, which is the entire defect
#: this module exists to stop repeating.
_QUERY_KEYS = {
    "parent_id": "ParentId",
    "include_item_types": "IncludeItemTypes",
    "media_types": "MediaTypes",
    "recursive": "Recursive",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
    "start_index": "StartIndex",
    "limit": "Limit",
    "fields": "Fields",
    "filters": "Filters",
    "genres": "Genres",
    "genre_ids": "GenreIds",
    "years": "Years",
    "person_ids": "PersonIds",
    "artist_ids": "ArtistIds",
    "album_artist_ids": "AlbumArtistIds",
    "search_term": "SearchTerm",
    "is_favorite": "IsFavorite",
    "name_starts_with": "NameStartsWith",
    "name_less_than": "NameLessThan",
    "image_type_limit": "ImageTypeLimit",
    "enable_image_types": "EnableImageTypes",
    "enable_images": "EnableImages",
    "enable_user_data": "EnableUserData",
    "enable_total_record_count": "EnableTotalRecordCount",
}


def build_query(ids=None, params=None, **kwargs):
    """The query dict for :func:`get_items`. Pure, so it can be asserted on.

    ``None`` means "not sent", so the server's own default applies -- the
    same rule ``get_user_items`` follows, and the reason a caller can pass
    every argument unconditionally.
    """
    unknown = set(kwargs) - set(_QUERY_KEYS)
    if unknown:
        # Loudly, and at the call rather than in a response nobody reads. A
        # keyword this module does not know is a filter that would be
        # dropped, which is indistinguishable from a library that genuinely
        # matches everything.
        raise TypeError("get_items() got unexpected keyword arguments: %s"
                        % ", ".join(sorted(unknown)))
    query = {_QUERY_KEYS[k]: v for k, v in kwargs.items() if v is not None}
    if ids is not None:
        query["Ids"] = ",".join(str(x) for x in ids)
    # Substituted by the http layer from the authenticated session.
    query["UserId"] = "{UserId}"
    if params:
        query.update(params)
    return query


def get_items(api, ids=None, params=None, **kwargs):
    """``GET /Items`` -- the modern twin of ``api.get_user_items``."""
    return api.items(params=build_query(ids=ids, params=params, **kwargs))
