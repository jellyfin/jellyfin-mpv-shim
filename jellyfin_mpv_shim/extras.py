"""What makes an item a Jellyfin extra (a Special Feature, a local trailer).

Its own module because both ``sync/`` and ``mpvtk_browser/`` ask: the one
definition both use. ``sync/`` must not import the browser.
"""


def is_extra(item):
    """Whether ``item`` is an extra belonging to some other item.

    ``ExtraType`` is the only signal the DTO carries. Not ``ParentId`` (null
    on extras, and not only on them) and not ``Type`` (an extra is a
    ``Video``, exactly like a home video). docs/jellyfin-api-notes.md §15.
    """
    return (item or {}).get("ExtraType") is not None
