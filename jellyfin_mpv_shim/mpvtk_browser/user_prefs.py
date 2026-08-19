"""Per-user display preferences shared with jellyfin-web.

Pure logic, like :mod:`home_sections` and the preference half of
:mod:`live_tv`, and for the same reason: these live in the *server's*
DisplayPreferences document, so a user who sets one in jellyfin-web gets it
here and vice versa. The document, its id and its client namespace are
``docs/jellyfin-api-notes.md`` section 7; the I/O is
``LibrarySource.get_user_prefs`` / ``save_user_prefs``.

**Only settings jellyfin-web stores on the server belong here**, and the test
is whether it called ``userSettings.set(key, value, enableOnServer)`` with
``enableOnServer !== false`` (section 7.2). Its localStorage-only settings do
not sync between clients even in web, so mirroring one here would be a lie --
if we want it, it is an ordinary ``conf.py`` key.
"""

#: jellyfin-web's key. Its accessor is ``useEpisodeImagesInNextUpAndResume``
#: (``userSettings.js:360-366``) and it defaults to **false**, which is the
#: same value spelled two ways: false means "do not use the episode's own
#: image", i.e. *do* inherit the series' artwork. See EPISODE_IMAGES_DEFAULT.
EPISODE_IMAGES_KEY = "useEpisodeImagesInNextUpAndResume"

#: Off, matching web. The reason is spoilers: a Next Up card for the episode
#: you have not watched shows a still *from* that episode, and enough users
#: complained that web made series artwork the default.
EPISODE_IMAGES_DEFAULT = False


def _as_bool(value, default):
    """A CustomPrefs value as a bool.

    Shares :func:`live_tv._as_bool`'s reasoning rather than importing it --
    that module is the Live TV screens and this one has no business
    depending on it. Absent or empty means the default; otherwise
    jellyfin-web's string comparison, plus a stored JSON ``true`` because
    some clients write one.
    """
    if value is None or value == "":
        return default
    return str(value).strip().lower() == "true"


def resolve_prefs(custom_prefs):
    """CustomPrefs dict -> the preference dict the screens read.

    Never raises and never returns a partial dict: a server that has never
    seen one of these answers exactly jellyfin-web's defaults.
    """
    prefs = custom_prefs or {}
    return {
        "episode_images": _as_bool(prefs.get(EPISODE_IMAGES_KEY),
                                   EPISODE_IMAGES_DEFAULT),
    }


def prefs_to_custom(prefs):
    """The preference dict -> the CustomPrefs keys to write.

    Booleans go out as ``"true"``/``"false"`` **strings**: jellyfin-web reads
    them with ``toBoolean(this.get(...))`` over a value it wrote with
    ``val.toString()``, so a JSON boolean would read as false there and the
    setting would appear to revert every time the web client was opened.
    """
    prefs = prefs or {}
    return {
        EPISODE_IMAGES_KEY: ("true" if prefs.get("episode_images")
                             else "false"),
    }


def inherit_artwork(prefs):
    """Whether a Next Up / Continue Watching tile may borrow series artwork.

    The double negative is jellyfin-web's, and worth stating once here so no
    call site has to hold it: the setting is *use episode images*, and its
    consumers all express it as ``inheritThumb: !useEpisodeImages``
    (``homesections/sections/nextUp.ts:119``, ``resume.ts:111``). Setting
    off (the default) => inherit on => the series' thumb wins over the
    episode still.
    """
    return not (prefs or {}).get("episode_images",
                                 EPISODE_IMAGES_DEFAULT)
