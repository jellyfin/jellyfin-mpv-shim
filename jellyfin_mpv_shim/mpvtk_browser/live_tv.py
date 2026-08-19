"""Live TV: guide preferences, time maths, and the labels the screens draw.

Everything here is pure — no network, no settings singleton, no widgets — so
the parts that are easy to get subtly wrong (timezones, cell widths, the
"which recording state is this in" ladder) are testable without a server.
The I/O lives in ``repository.LibrarySource`` and ``gateway/livetv.py``; the
widgets live in ``pages/livetv.py`` and ``guide_view.py``.

**The preferences are jellyfin-web's, in jellyfin-web's storage.** Guide
indicators, channel order and favourites-first are keys in the same
DisplayPreferences ``CustomPrefs`` document the home layout uses (id
``usersettings``, client ``emby`` — see ``home_sections``). That is not
incidental compatibility: a user who sorts their guide by channel number in
the web client expects the same order here, and writing these anywhere else
would give them two settings with one name. Values are stored as the
*strings* jellyfin-web writes ("true"/"false"), because that client compares
them as strings and a real JSON boolean reads as neither.
"""

import datetime
import re

from ..i18n import _

# -- preferences -----------------------------------------------------------

#: How channels are ordered in the guide and the channel list.
CHANNEL_ORDER_KEY = "livetv-channelorder"
ORDER_NUMBER = "Number"
ORDER_DATE_PLAYED = "DatePlayed"

#: Float favourited channels to the top. Default ON (jellyfin-web tests
#: ``!== 'false'``).
FAVORITES_FIRST_KEY = "livetv-favoritechannelsattop"

#: Tint guide cells by category.
COLOR_CODED_KEY = "guide-colorcodedbackgrounds"

INDICATOR_PREFIX = "guide-indicator-"

#: Indicator -> default. These follow what ``guide.js`` actually *draws*
#: rather than what its settings dialog shows: the dialog gives "new" no
#: ``data-default``, so it renders unchecked, while the guide itself tests
#: ``!== 'false'`` and draws it. Rather than reproduce a disagreement between
#: a checkbox and the screen it controls, both sides here read this table.
INDICATOR_DEFAULTS = {
    "hd": False,
    "live": True,
    "new": True,
    "premiere": True,
    "repeat": False,
}

#: Guide category filters. Order is the order the settings dialog lists them.
#: "series" is deliberately absent: jellyfin-web has no checkbox for it and
#: infers it from the other four (see ``program_displayed``).
CATEGORIES = ("movies", "sports", "kids", "news")

#: Category -> the query flag that selects it. Spelled out rather than
#: derived as ``"is_" + name``, which is how "movies" became ``is_movies``
#: and every category-filtered fetch raised ``TypeError`` before reaching
#: the server. The server's parameter is ``IsMovie``, singular, and it is
#: the only one of the four whose flag is not its own name.
CATEGORY_FLAGS = {"movies": "is_movie", "sports": "is_sports",
                  "kids": "is_kids", "news": "is_news"}


def indicator_labels():
    """``(key, label)`` for the guide-indicator checkboxes. A function, not a
    constant: these are translated and i18n is initialised after import."""
    return [
        ("hd", _("HD Programs")),
        ("live", _("Live Broadcasts")),
        ("new", _("New Episodes")),
        ("premiere", _("Premieres")),
        ("repeat", _("Repeat Episodes")),
    ]


def category_labels():
    return [
        ("movies", _("Movies")),
        ("sports", _("Sports")),
        ("kids", _("Kids")),
        ("news", _("News")),
    ]


def _as_bool(value, default):
    """A CustomPrefs value as a bool. Absent means the default; anything
    else is jellyfin-web's string comparison, which is why a stored JSON
    ``true`` (some clients write one) has to be accepted too."""
    if value is None or value == "":
        return default
    return str(value).strip().lower() == "true"


def resolve_prefs(custom_prefs):
    """CustomPrefs dict -> the guide preference dict the screens read.

    Never raises and never returns a partial dict: a server that has never
    seen a Live TV setting answers exactly the jellyfin-web defaults.
    """
    prefs = custom_prefs or {}
    order = str(prefs.get(CHANNEL_ORDER_KEY) or "").strip()
    return {
        "order": (ORDER_DATE_PLAYED if order == ORDER_DATE_PLAYED
                  else ORDER_NUMBER),
        "favorites_first": _as_bool(prefs.get(FAVORITES_FIRST_KEY), True),
        "color_coded": _as_bool(prefs.get(COLOR_CODED_KEY), False),
        "indicators": {
            key: _as_bool(prefs.get(INDICATOR_PREFIX + key), default)
            for key, default in INDICATOR_DEFAULTS.items()
        },
    }


def prefs_to_custom(prefs):
    """The guide preference dict -> the CustomPrefs keys to write.

    Booleans go out as "true"/"false" strings: jellyfin-web reads them with
    ``=== 'true'``, so a JSON boolean would silently read as false there and
    the setting would appear to revert whenever the web client was opened.
    """
    out = {
        CHANNEL_ORDER_KEY: str(prefs.get("order") or ORDER_NUMBER),
        FAVORITES_FIRST_KEY: "true" if prefs.get("favorites_first") else "false",
        COLOR_CODED_KEY: "true" if prefs.get("color_coded") else "false",
    }
    indicators = prefs.get("indicators") or {}
    for key, default in INDICATOR_DEFAULTS.items():
        value = indicators.get(key, default)
        out[INDICATOR_PREFIX + key] = "true" if value else "false"
    return out


def category_kwargs(categories):
    """``get_channels`` flags for a category selection.

    Empty (or all four) means "no filter at all", which is what jellyfin-web
    sends and is not the same as passing all four: the server's ``IsMovie``
    is a column predicate while ``IsSports``/``IsNews``/``IsKids`` become a
    tag filter, so the four together read as "a movie that is also tagged
    sports and news and kids" and match nothing.

    **Channels only.** The guide's programmes are filtered by drawing (see
    :func:`program_displayed`), because that is what jellyfin-web does and
    because sending these to ``LiveTv/Programs`` would AND them the same
    way — a two-category guide came back empty.
    """
    picked = {c for c in (categories or ()) if c in CATEGORIES}
    if not picked or picked == set(CATEGORIES):
        return {}
    return {CATEGORY_FLAGS[name]: True for name in sorted(picked)}


def program_displayed(item, categories):
    """Whether a guide cell draws its contents under the category filter.

    jellyfin-web's ``getChannelProgramsHtml`` ladder. The cell itself is
    always drawn — only its *contents* are suppressed — which is what keeps
    a filtered guide a grid rather than a field of gaps, and what lets the
    programme still be opened.

    A programme in none of the four categories (and a plain series) is
    hidden whenever any filter is on. That looks arbitrary but is exactly
    jellyfin-web: its "series" pseudo-category is only ever added when all
    four boxes are ticked, i.e. when there is no filter at all.

    **One deliberate divergence.** Zero boxes ticked reads as "no filter"
    here and as "hide everything" in jellyfin-web, which always appends an
    "all" sentinel so the empty selection is still a selection. Its version
    draws a grid of blank cells and offers no way back except the settings
    dialog; a filter that hides its own escape hatch is not worth
    reproducing.
    """
    picked = {c for c in (categories or ()) if c in CATEGORIES}
    if not picked or picked == set(CATEGORIES):
        return True
    for field, _colour, key in CATEGORY_ORDER:
        if item.get(field):
            return key in picked
    return False


def channel_sort_kwargs(prefs):
    """``get_channels`` ordering arguments for the resolved prefs."""
    kwargs = {"enable_favorite_sorting": bool(prefs.get("favorites_first"))}
    if prefs.get("order") == ORDER_DATE_PLAYED:
        kwargs["sort_by"] = "DatePlayed"
        kwargs["sort_order"] = "Descending"
    return kwargs


# -- times -----------------------------------------------------------------

#: One guide cell. jellyfin-web's grid is 30-minute columns and every guide
#: provider aligns to the half hour, so this is the natural quantum for both
#: the time header and the window's start.
CELL_MINUTES = 30

_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_time(value):
    """A Jellyfin timestamp as a **local, aware** datetime; None if unusable.

    Written out rather than handed to ``datetime.fromisoformat`` for two
    reasons, both of which produce wrong times rather than errors:

    * Jellyfin serialises .NET ticks, i.e. **seven** fractional digits, which
      ``fromisoformat`` rejects outright before 3.11 and still will not take
      in that width;
    * these timestamps are UTC. Parsing one and forgetting the ``Z`` (which
      is what dropping the fraction with ``partition('.')`` does) yields a
      naive datetime that the guide then lays out as if it were local — the
      whole grid slides by the UTC offset, which reads as "my guide is five
      hours out" rather than as a parse failure.
    """
    if not value:
        return None
    match = _ISO.match(str(value).strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, frac, zone = match.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        naive = datetime.datetime(int(year), int(month), int(day), int(hour),
                                  int(minute), int(second), micro)
    except ValueError:
        return None
    if zone in (None, "Z", "z"):
        tz = datetime.timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        zone = zone[1:].replace(":", "")
        tz = datetime.timezone(sign * datetime.timedelta(
            hours=int(zone[:2]), minutes=int(zone[2:4])))
    return naive.replace(tzinfo=tz).astimezone()


def now():
    """Local aware "now". One place, so tests can compare against the same
    clock the layout uses."""
    return datetime.datetime.now().astimezone()


def floor_to_cell(when):
    """``when`` rounded DOWN to the previous :00 or :30.

    Down, not to-nearest: the window has to contain what is on *now*, and
    rounding a 10:29 up to 10:30 would open the guide on the next programme
    while the current one is still airing.
    """
    return when.replace(minute=(when.minute // CELL_MINUTES) * CELL_MINUTES,
                        second=0, microsecond=0)


def fmt_time(when):
    """Clock time for a guide header or an air-time label.

    24-hour, matching the rest of the app (the detail page's "Ends at" and
    the HUD both use it) rather than jellyfin-web's locale-driven format.
    """
    return when.strftime("%H:%M") if when else ""


def fmt_day(when):
    """The date-tab / timer-group label: "Tue, Jul 28"."""
    return when.strftime("%a, %b %d") if when else ""


def air_time_label(item, end=True):
    """``"20:00 - 20:30"`` for anything carrying StartDate/EndDate.

    Returns "" when the item has no start, which is what a recording that has
    already been written to disk looks like — it is an ordinary item by then
    and its air time is no longer interesting.
    """
    start = parse_time(item.get("StartDate"))
    if start is None:
        return ""
    if not end:
        return fmt_time(start)
    stop = parse_time(item.get("EndDate"))
    if stop is None:
        return fmt_time(start)
    return "%s - %s" % (fmt_time(start), fmt_time(stop))


def is_airing(item, when=None):
    """Whether this program is on the air at ``when`` (default: now).

    The one question that decides whether a guide cell is "watch this" or
    "read about it", so it is here rather than inlined at each of the four
    call sites.
    """
    start = parse_time(item.get("StartDate"))
    stop = parse_time(item.get("EndDate"))
    if start is None or stop is None:
        return False
    when = when or now()
    return start <= when < stop


def program_progress(item, when=None):
    """How far through a program is, 0..1 — the sliver of progress bar under
    an On Now tile. 0.0 when it is not airing."""
    start = parse_time(item.get("StartDate"))
    stop = parse_time(item.get("EndDate"))
    if start is None or stop is None:
        return 0.0
    when = when or now()
    span = (stop - start).total_seconds()
    if span <= 0 or when < start:
        return 0.0
    return min(1.0, (when - start).total_seconds() / span)


# -- the guide grid --------------------------------------------------------

#: Narrowest a 30-minute cell may be drawn before the window shows fewer
#: hours instead. Below this a programme name has no room at all.
MIN_CELL_W = 120

#: Window sizes offered, in 30-minute cells. Two hours by default; the
#: window shrinks on a narrow surface rather than squashing the cells.
MIN_CELLS = 2
MAX_CELLS = 8


def cells_for_width(width, min_cell_w=MIN_CELL_W):
    """How many 30-minute columns fit in ``width`` logical px.

    Clamped both ways: never fewer than two columns (one hour — a guide
    showing half an hour is not a guide) and never more than eight (four
    hours), past which every cell is narrow enough that a programme name
    has no room.
    """
    return max(MIN_CELLS, min(MAX_CELLS, int(width // max(1, min_cell_w))))


def row_segments(programs, start, end, width):
    """Lay one channel's programmes out across a window.

    Returns ``[(program_or_None, px_width), ...]`` spanning exactly
    ``width`` px, left to right, where a ``None`` entry is dead air (a gap in
    the guide data). Programmes are clipped to the window at both ends, which
    is what makes a three-hour film occupy the whole row rather than
    overflowing it.

    Widths are computed by differencing *cumulative* pixel positions rather
    than by rounding each duration independently: the latter accumulates
    error and leaves the last cell one or two px short of the edge, which on
    a Row of fixed-width boxes reads as a ragged right margin.
    """
    span = (end - start).total_seconds()
    if span <= 0 or width <= 0:
        return []

    def to_px(when):
        frac = (when - start).total_seconds() / span
        return int(round(max(0.0, min(1.0, frac)) * width))

    # Programmes overlapping the window, in start order. The server sorts by
    # StartDate, but the guide fetch is one request for every channel and a
    # caller filtering by ChannelId does not necessarily preserve that.
    entries = []
    for program in programs or ():
        p_start = parse_time(program.get("StartDate"))
        p_end = parse_time(program.get("EndDate"))
        if p_start is None or p_end is None or p_end <= p_start:
            continue
        if p_end <= start or p_start >= end:
            continue
        entries.append((p_start, p_end, program))
    entries.sort(key=lambda e: e[0])

    segments = []
    cursor = 0          # px consumed so far
    for p_start, p_end, program in entries:
        left = to_px(p_start)
        right = to_px(p_end)
        if left > cursor:
            segments.append((None, left - cursor))
            cursor = left
        if right <= cursor:
            # Fully swallowed by the previous programme — overlapping guide
            # data is common enough (a provider's rounding) that dropping the
            # loser silently beats drawing a zero-width cell.
            continue
        segments.append((program, right - cursor))
        cursor = right
    if cursor < width:
        segments.append((None, width - cursor))
    return segments


def timeslots(start, end):
    """The window's column boundaries, for the time header."""
    out = []
    step = datetime.timedelta(minutes=CELL_MINUTES)
    when = start
    while when < end:
        out.append(when)
        when += step
    return out


def clamp_window(start, guide_info, cells):
    """Keep a window start inside what the provider actually has data for.

    ``guide_info`` is the ``LiveTv/GuideInfo`` response (``StartDate`` /
    ``EndDate``); an empty or unparsable one imposes no limit, because a
    guide the shim refuses to page through is worse than one that pages into
    an empty day.
    """
    info = guide_info or {}
    lower = parse_time(info.get("StartDate"))
    upper = parse_time(info.get("EndDate"))
    if lower is not None:
        lower = floor_to_cell(lower)
        if start < lower:
            start = lower
    if upper is not None:
        # The last window that still shows something: one whose *start* is
        # before the end of the data.
        last = floor_to_cell(upper) - datetime.timedelta(
            minutes=CELL_MINUTES * (cells - 1))
        if start > last:
            start = max(last, lower or last)
    return start


# -- recording state -------------------------------------------------------

def timer_state(item):
    """Which recording state an item is in.

    One of ``None``, ``"timer"`` (scheduled), ``"recording"`` (in progress),
    ``"series"`` (covered by an active series rule) or ``"series_inactive"``
    (a series rule exists but this showing is not scheduled — already in the
    library, or cancelled individually).

    This answers **which symbol to draw**, and nothing else. What a Record
    button should do is :func:`single_timer_state`, which is a genuinely
    different question — a programme covered by a series rule answers
    ``"series"`` here while still having a single timer of its own.

    jellyfin-web's ``getTimerIndicator``, with one addition: ``InProgress``
    is split out from ``"timer"``, because the tile paints it differently.

    A cancelled single timer reads as no timer at all. On a *program* DTO
    the server never emits one anyway — ``AddRecordingInfo`` sets ``TimerId``
    only when the status is neither Cancelled nor Error — so this branch is
    reached only by a ``Type == "Timer"`` DTO, where jellyfin-web would draw
    the dot and we would rather not claim a cancelled timer is recording.
    """
    if item.get("Type") == "SeriesTimer":
        return "series"
    if item.get("TimerId") or item.get("SeriesTimerId"):
        # An absent Status alongside a timer id means the timer is not
        # scheduled — jellyfin-web spells this "|| 'Cancelled'".
        status = item.get("Status") or "Cancelled"
    elif item.get("Type") == "Timer":
        status = item.get("Status")
    else:
        return None
    if item.get("SeriesTimerId"):
        return "series" if status != "Cancelled" else "series_inactive"
    if status == "Cancelled":
        return None
    return "recording" if status == "InProgress" else "timer"


def single_timer_state(item):
    """Whether this showing has a **single recording of its own**, and which
    kind: ``None``, ``"timer"`` (scheduled) or ``"recording"`` (in progress).

    Deliberately not :func:`timer_state`, and the difference is the whole
    point. That one answers "which symbol", and a showing covered by a
    series rule answers ``"series"`` *even when it also has its own live
    timer* — which is right for an icon and wrong for a button. Driving the
    Record button off it left every episode of a series you are recording
    offering "Record" (a second timer for a programme that already has one),
    with no way to skip one showing and no way to stop one in progress.

    This is jellyfin-web's ``recordingfields`` test — ``program.TimerId &&
    program.Status !== 'Cancelled'`` — which does not consult
    ``SeriesTimerId`` at all. The two questions are independent there, and
    a programme can legitimately answer yes to both.
    """
    if not item.get("TimerId"):
        return None
    status = item.get("Status")
    if status == "Cancelled":
        return None
    return "recording" if status == "InProgress" else "timer"


def is_channel_artwork(item):
    """Whether this item's artwork is (or falls back to) a CHANNEL LOGO.

    Which matters because transparent artwork comes in two conventions that
    want opposite treatment, and the item type is what tells them apart. A
    broadcaster's channel logo is usually *dark* ink drawn for the white page
    every other client puts it on, so on this UI it needs a light plate to be
    visible at all. A film's or series' Logo artwork is white by convention
    and reads on a dark surface perfectly well -- plating that is how you end
    up needing a drop shadow to rescue white ink from a white plate, which is
    what #637 was about.

    So the two get separate settings (``logo_legibility_live_tv`` and
    ``logo_legibility_library``) and this is the line between them.

    All four Live TV DTO types, not just the two in ``LIVE_TYPES``:

    * ``TvChannel`` wears the logo itself.
    * ``Program`` mostly has no artwork of its own, so the channel logo is
      the whole fallback -- see ``repository.PROGRAM_FIELDS``.
    * ``Timer`` has *neither* ``ImageTags`` nor ``ParentPrimaryImage*``, so
      it always falls through to the channel-logo branch of
      ``image_spec`` -- which is also what jellyfin-web's schedule shows,
      via ``showChannelLogo``. Leaving it out made the Schedule tab the one
      Live TV screen that drew its channel logos unplated.
    * ``SeriesTimer`` reaches the same branch whenever the series the rule
      was made from has no poster.

    A ``SeriesTimer`` that *does* have one resolves to the series poster
    instead, and gets asked the Live TV question about it. That imprecision
    is deliberate: a poster is opaque, ``plate_for`` returns ``None`` for it,
    and no plate rule applies either way -- so paying for a resolved-artwork
    check here would buy nothing over a type test the reader can verify at a
    glance.

    A finished recording is NOT included, and does not need to be: the
    server hands it back as a Movie/Episode/Video wearing its own artwork,
    and ``recordings_page`` never asks for ``ChannelImage``, so it cannot
    reach the channel-logo branch at all.
    """
    # Imported here: repository imports THIS module at import time, so the
    # dependency can only run the other way at call time.
    from .repository import LIVE_TYPES

    itype = (item or {}).get("Type")
    return itype in LIVE_TYPES or itype in ("Timer", "SeriesTimer")


def is_recording_now(item):
    """Whether this is being written to disk *right now*.

    Deliberately a separate question from :func:`timer_state`, which answers
    "which symbol" (a single timer or a series rule). This one answers "what
    colour", and the two do not line up: a programme covered by a series
    rule and airing at this moment is being recorded, but its state is
    ``"series"`` — so keying the colour off the state left every
    series-recorded programme with an ordinary blue progress bar.

    ``_recording`` is the stamp the Recordings/Schedule screens put on
    results from an ``is_in_progress`` query — a recording DTO carries no
    timer state of its own.
    """
    if item.get("_recording"):
        return True
    return bool(item.get("TimerId")) and item.get("Status") == "InProgress"


#: Timer state -> the Material icon the guide and tiles draw for it.
STATE_ICONS = {
    "timer": "fiber_manual_record",
    "recording": "fiber_manual_record",
    "series": "fiber_smart_record",
    "series_inactive": "fiber_smart_record",
}


def program_indicators(item, indicators):
    """The one badge a guide cell shows ("Live"/"Premiere"/"New"), or "".

    One, not several: jellyfin-web picks the first that applies in this
    order, and a cell is 120px wide — two badges would leave no room for the
    programme's name.
    """
    indicators = indicators or {}
    if item.get("IsLive") and indicators.get("live"):
        return _("Live")
    if item.get("IsPremiere") and indicators.get("premiere"):
        return _("Premiere")
    if (item.get("IsSeries") and not item.get("IsRepeat")
            and indicators.get("new")):
        return _("New")
    # Both halves test IsSeries, as jellyfin-web does: "Repeat" is a
    # statement about an episode, and a film shown twice is not one.
    if (item.get("IsSeries") and item.get("IsRepeat")
            and indicators.get("repeat")):
        return _("Repeat")
    return ""


#: ``(DTO flag, colour key, category key)``. The one ladder jellyfin-web
#: checks a programme against: the first match decides both which accent
#: colour a colour-coded cell gets and which category checkbox governs it.
#: Checked in this order, so a kids' sports programme reads as kids.
#:
#: The last two columns differ for exactly one entry — the checkbox is
#: "movies" and the colour class is "movie" — which is why both are here
#: rather than one being derived from the other.
CATEGORY_ORDER = (("IsKids", "kids", "kids"),
                  ("IsSports", "sports", "sports"),
                  ("IsNews", "news", "news"),
                  ("IsMovie", "movie", "movies"))


def program_category(item):
    """The colour-coding category of a program, or None."""
    for field, name, _key in CATEGORY_ORDER:
        if item.get(field):
            return name
    return None


def program_title(item):
    """A program's display title -- the SERIES name, on its own.

    Guide data puts the series in ``Name`` and the episode in
    ``EpisodeTitle``, the opposite of a library Episode, so the ordinary tile
    labelling would show every showing of a series as the same string.
    ``program_subtitle`` supplies the episode line.
    """
    return item.get("Name") or ""


def program_subtitle(item):
    """The second line for a program: its episode title, else its channel."""
    return item.get("EpisodeTitle") or item.get("ChannelName") or ""


def group_by_day(items, fallback=None):
    """``[(day-label, [item, ...])]`` in start order.

    jellyfin-web's ``getTimersHtml`` grouping, which its channel listing
    (``renderProgramsForChannel``) repeats verbatim: a flat list of upcoming
    entries is unreadable past about a day — the same programme name appears
    three times and nothing says which showing is which.

    Consecutive runs, not a dict: the caller's list is already in start
    order and that order is the grouping. Bucketing by label would put a
    channel's Thursday showings under Wednesday's heading if the list ever
    came back unsorted, which is the failure that looks like data loss.
    """
    fallback = fallback or _("Scheduled")
    groups: list = []
    for item in items:
        start = parse_time(item.get("StartDate"))
        label = fmt_day(start) if start else fallback
        if groups and groups[-1][0] == label:
            groups[-1][1].append(item)
        else:
            groups.append((label, [item]))
    return groups


def channel_number(channel):
    """A channel's number as a display string, or "".

    Two spellings: ``LiveTv/Channels`` answers with ``Number`` and the
    ordinary item endpoint with ``ChannelNumber``, so a page that reaches a
    channel by id sees the other one from the tile that linked to it.
    """
    for key in ("Number", "ChannelNumber"):
        value = channel.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""
