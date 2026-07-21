"""Home-screen section layout, compatible with jellyfin-web's.

The layout lives server-side in DisplayPreferences under the ``usersettings``
id and the ``emby`` client namespace (the legacy name jellyfin-web still
writes; using anything else gets you a different, empty preference set). Each
slot is one string in ``CustomPrefs`` under ``homesection0``..``homesection9``.

Everything here is pure: no network, no settings singleton. The I/O lives in
repository.LibrarySource, which reads/writes the DTO and hands the CustomPrefs
dict to resolve_layout().

Two rules from jellyfin-web that are easy to get wrong, and both are load
bearing for interop:

* An absent or empty slot means "use *this slot's* default", NOT "none". Only
  the literal string "none" blanks a slot. jellyfin-web's settings UI rewrites
  the default option's value to "" before saving, so a user who never touched
  the screen and a user who explicitly picked the default are indistinguishable
  on the wire — both round-trip through the per-slot default.
* A stored "folders" is a pre-10.x alias for smalllibrarytiles.
"""

from ..i18n import _

#: DisplayPreferences ids. Both are jellyfin-web's, and both must match it
#: exactly or we read/write a preference set nothing else can see.
DISPLAY_PREFS_ID = "usersettings"
DISPLAY_PREFS_CLIENT = "emby"

#: jellyfin-web grew from 7 slots to 10. Reading all 10 is harmless against an
#: older server (missing keys fall back to their slot default), so there is no
#: version sniffing here.
SLOT_COUNT = 10

# -- section types ---------------------------------------------------------

NONE = "none"
LIBRARIES = "smalllibrarytiles"
LIBRARY_BUTTONS = "librarybuttons"
RESUME = "resume"
RESUME_AUDIO = "resumeaudio"
RESUME_BOOK = "resumebook"
NEXT_UP = "nextup"
LATEST = "latestmedia"
LIVE_TV = "livetv"
ACTIVE_RECORDINGS = "activerecordings"

#: Per-slot defaults, mirroring jellyfin-web's constants/homeSectionType.ts
#: (itself synced with the server's DisplayPreferencesController). Slots we
#: cannot render still keep their real default so that resolving and then
#: re-serializing an untouched layout is a no-op on the wire.
DEFAULT_LAYOUT = (
    LIBRARIES,
    RESUME,
    RESUME_AUDIO,
    RESUME_BOOK,
    LIVE_TV,
    NEXT_UP,
    LATEST,
    NONE,
    NONE,
    NONE,
)

#: What this browser can actually draw. The rest of jellyfin-web's types are
#: recognised but render nothing: Live TV and recordings because
#: repository.EXCLUDED_COLLECTION_TYPES drops livetv outright, books likewise,
#: and librarybuttons is a redundant second styling of the Libraries row.
#: Unsupported values are *preserved* on save rather than rewritten to "none",
#: so configuring the shim never silently degrades the web client's home
#: screen for the same user.
SUPPORTED = frozenset({NONE, LIBRARIES, RESUME, RESUME_AUDIO, NEXT_UP, LATEST})

#: Fetch stage per section, matching get_home_rows' two-batch load. "local"
#: needs no request (the Libraries row is rendered from get_libraries, which
#: the loader already has); "latest" is the per-library fan-out that sits below
#: the fold. Unsupported sections are absent and contribute no work.
STAGE = {
    LIBRARIES: "local",
    RESUME: "primary",
    RESUME_AUDIO: "primary",
    NEXT_UP: "primary",
    LATEST: "latest",
}


def section_labels():
    """(value, label) for every section offered in the settings dropdowns.

    A function, not a constant: these are translated, and i18n is initialised
    after import on some paths.
    """
    return [
        (NONE, _("None")),
        (LIBRARIES, _("My Media")),
        (RESUME, _("Continue Watching")),
        (RESUME_AUDIO, _("Continue Listening")),
        (NEXT_UP, _("Next Up")),
        (LATEST, _("Recently Added")),
    ]


def default_section(slot: int) -> str:
    """The default type for one slot, or "none" past the end."""
    if 0 <= slot < len(DEFAULT_LAYOUT):
        return DEFAULT_LAYOUT[slot]
    return NONE


def resolve_layout(custom_prefs) -> list:
    """CustomPrefs dict -> the ordered list of SLOT_COUNT section types.

    Unknown/unsupported values are kept as-is; callers skip what they cannot
    draw. This is deliberate — see SUPPORTED.
    """
    prefs = custom_prefs or {}
    layout = []
    for slot in range(SLOT_COUNT):
        value = prefs.get("homesection%d" % slot)
        # Values arrive as strings, but a hand-edited or non-web-written DTO
        # can hold anything; str() rather than trusting the type.
        value = "" if value is None else str(value).strip()
        if value == "folders":
            # Pre-10.x alias. jellyfin-web remaps it to slot 0's default
            # rather than to the *current* slot's, so this is not
            # default_section(slot).
            value = DEFAULT_LAYOUT[0]
        layout.append(value or default_section(slot))
    return layout


def layout_to_prefs(layout) -> dict:
    """The ordered section list -> the CustomPrefs keys to write.

    Writes "" for a slot holding its own default, which is what jellyfin-web
    stores and what keeps a never-customised layout from being pinned to
    today's defaults if the server's change later.
    """
    prefs = {}
    for slot in range(SLOT_COUNT):
        value = layout[slot] if slot < len(layout) else default_section(slot)
        value = str(value or NONE)
        prefs["homesection%d" % slot] = (
            "" if value == default_section(slot) else value)
    return prefs


def stages_for(layout):
    """The fetch stages a layout actually needs, e.g. {"primary", "latest"}.

    Lets the loader skip a round trip entirely when no slot asks for it.
    """
    return {STAGE[s] for s in layout if s in STAGE}
