"""What a file *is*, as labelled rows — the pure half of three screens.

The same knowledge is wanted in three places and was on its way to being
written three times:

* the detail page's one-line summary (``_media_info_line``), which exists so
  you can judge direct-play before pressing Play;
* the **playback info** screen, which answers "what is happening right now"
  over a playing video;
* the **media info** dialog off the context menu, which is jellyfin-web's
  ``itemMediaInfo`` — every attribute of every stream.

Three independent renderings of one DTO drift, and the way they drift is
invisible: each is right about the item it was written against and wrong
about the codec nobody tested. So the formatting lives here once, and the
screens are layout.

**Pure, and not merely convention.** Nothing here builds a widget, reaches
the source, or knows a route — the interesting cases are all DTO shapes (a
stream with no ``Codec``, a Dolby Vision profile, an external subtitle, a
source with no ``Size``), and every one of them is a dictionary. That is
what makes them cheap to test, and it is the rule the whole package is held
to by ``tests/test_source_invariants.py``.

Faithful to ``jellyfin-web/src/components/itemMediaInfo/itemMediaInfo.js``
in *which* attributes appear, their order, and their conditions, because a
user comparing the two screens is comparing them attribute by attribute.
Two deliberate departures, both recorded in ``docs/UI_FIXES_4.md``:

* the **file path is not administrator-gated** here (web gates it on
  ``IsAdministrator``);
* boolean attributes are omitted when false rather than printed as "No",
  because web has a scrollbar and a HUD panel does not — see
  :func:`stream_attributes`.
"""

from ...i18n import _


#: Streams with nothing to say about the file itself. ``Data`` is
#: jellyfin-web's own exclusion (itemMediaInfo.js:82); it is timecode and
#: chapter tracks, which have no attributes anyone reads.
_HIDDEN_STREAM_TYPES = frozenset({"Data"})


def stream_sort_key(stream):
    """jellyfin-web's ``itemHelper.sortTracks``, as a key function.

    Embedded before external, forced before not, default before not, then
    the container's own index. Worth copying exactly rather than sorting by
    index alone: the order *is* information — it is the order a player
    considers them in — and two clients disagreeing about it makes an
    external subtitle look like a missing one.
    """
    return (bool(stream.get("IsExternal")),
            not bool(stream.get("IsForced")),
            not bool(stream.get("IsDefault")),
            stream.get("Index") if stream.get("Index") is not None else 1 << 30)


def visible_streams(source):
    """The streams of one media source, in display order."""
    streams = (source or {}).get("MediaStreams") or []
    return sorted((s for s in streams
                   if s.get("Type") not in _HIDDEN_STREAM_TYPES),
                  key=stream_sort_key)


def stream_heading(stream):
    """"Video" / "Audio" / "Subtitle" / … for a stream's block."""
    kind = (stream or {}).get("Type") or ""
    return {
        "Video": _("Video"),
        "Audio": _("Audio"),
        "Subtitle": _("Subtitle"),
        "Lyric": _("Lyric"),
        # web maps EmbeddedImage onto its plain "Image" string; a cover
        # embedded in an mka is the common case and "EmbeddedImage" is not
        # what anyone calls it.
        "EmbeddedImage": _("Image"),
    }.get(kind, kind)


def source_attributes(source):
    """``[(label, value)]`` for the file itself: container, format, path, size.

    The **path is included for everyone**, which is where this departs from
    jellyfin-web. It is not a boundary: the same string is in this app's own
    log, in a web client's devtools, and a ``static=true`` stream is
    reachable by anyone holding the item guid regardless. A control that
    stops nobody and hides a row from the owner of the machine is not worth
    having. See docs/UI_FIXES_4.md §11.
    """
    from .labels import human_size

    source = source or {}
    out = []
    if source.get("Container"):
        out.append((_("Container"), str(source["Container"])))
    if source.get("Formats"):
        out.append((_("Format"), ",".join(str(f) for f in source["Formats"])))
    if source.get("Path"):
        out.append((_("Path"), str(source["Path"])))
    if source.get("Size"):
        out.append((_("Size"), human_size(source["Size"])))
    return out


def _yes_no(value):
    return _("Yes") if value else _("No")


def stream_attributes(stream, source=None):
    """``[(label, value)]`` for one stream, in jellyfin-web's order.

    ``source`` supplies the one attribute that is a property of the file
    rather than the stream (``Timestamp``, shown against video), exactly as
    web does it.

    **Booleans are omitted when false**, where web prints "No". Anamorphic,
    Interlaced, Default, Forced and External are five rows per stream, and
    on a file with eight subtitle tracks that is forty rows saying nothing.
    Web can afford them behind a scrollbar; the playback HUD cannot, and one
    formatter serving two screens has to pick. "External: No" carries no
    information a reader was missing — the absence of the row says it.
    """
    stream = stream or {}
    kind = stream.get("Type")
    video = kind == "Video"
    out = []

    def add(label, value):
        if value is not None and value != "":
            out.append((label, str(value)))

    if stream.get("DisplayTitle"):
        add(_("Title"), stream["DisplayTitle"])
    # Language is on the DisplayTitle for video and would read twice.
    if stream.get("Language") and not video:
        add(_("Language"), stream["Language"])
    if stream.get("Codec"):
        add(_("Codec"), str(stream["Codec"]).upper())
    if stream.get("CodecTag"):
        add(_("Codec tag"), stream["CodecTag"])
    if stream.get("IsAVC") is not None:
        add("AVC", _yes_no(stream["IsAVC"]))
    if stream.get("Profile"):
        add(_("Profile"), stream["Profile"])
    if stream.get("Level"):
        add(_("Level"), stream["Level"])
    if stream.get("Width") or stream.get("Height"):
        add(_("Resolution"), "%sx%s" % (stream.get("Width") or "?",
                                        stream.get("Height") or "?"))
    if stream.get("AspectRatio") and video:
        add(_("Aspect ratio"), stream["AspectRatio"])
    if video:
        if stream.get("IsAnamorphic"):
            add(_("Anamorphic"), _yes_no(True))
        if stream.get("IsInterlaced"):
            add(_("Interlaced"), _yes_no(True))
    if stream.get("ReferenceFrameRate") and video:
        add(_("Framerate"), stream["ReferenceFrameRate"])
    if stream.get("ChannelLayout"):
        add(_("Layout"), stream["ChannelLayout"])
    if stream.get("Channels"):
        add(_("Channels"), _("%d ch") % stream["Channels"])
    if stream.get("BitRate"):
        add(_("Bitrate"), _("%d kbps") % (int(stream["BitRate"]) // 1000))
    if stream.get("SampleRate"):
        add(_("Sample rate"), _("%d Hz") % stream["SampleRate"])
    if stream.get("BitDepth"):
        add(_("Bit depth"), _("%d bit") % stream["BitDepth"])
    if video:
        add(_("Video range"), stream.get("VideoRange"))
        add(_("Video range type"), stream.get("VideoRangeType"))
    out.extend(_dolby_vision_attributes(stream))
    add(_("Color space"), stream.get("ColorSpace"))
    add(_("Color transfer"), stream.get("ColorTransfer"))
    add(_("Color primaries"), stream.get("ColorPrimaries"))
    add(_("Pixel format"), stream.get("PixelFormat"))
    add(_("Ref frames"), stream.get("RefFrames"))
    if video:
        add(_("Rotation"), stream.get("Rotation"))
    add("NAL", stream.get("NalLengthSize"))
    if kind in ("Subtitle", "Audio"):
        # Only the true ones; see the docstring.
        for flag, label in (("IsDefault", _("Default")),
                            ("IsForced", _("Forced")),
                            ("IsExternal", _("External"))):
            if stream.get(flag):
                add(label, _yes_no(True))
    if video and (source or {}).get("Timestamp"):
        add(_("Timestamp"), source["Timestamp"])
    return out


def _dolby_vision_attributes(stream):
    """The Dolby Vision block, present only on a stream that carries one.

    Kept together and kept out of the main run because it is eight
    attributes that are all absent on every file that is not Dolby Vision,
    and inlining them buries the ordinary ones in `if`s.
    """
    if not stream.get("VideoDoViTitle"):
        return []
    out = [(_("Dolby Vision title"), str(stream["VideoDoViTitle"]))]
    for key, label in (("DvVersionMajor", _("DV version major")),
                       ("DvVersionMinor", _("DV version minor")),
                       ("DvProfile", _("DV profile")),
                       ("DvLevel", _("DV level")),
                       ("RpuPresentFlag", _("RPU present")),
                       ("ElPresentFlag", _("EL present")),
                       ("BlPresentFlag", _("BL present")),
                       ("DvBlSignalCompatibilityId",
                        _("DV BL signal compatibility ID"))):
        value = stream.get(key)
        if value is not None:
            out.append((label, str(value)))
    return out


# -- the one-line summary ------------------------------------------------

def primary_stream(source, kind):
    """The stream a summary line should describe: the first of its type in
    display order, which is the default one where the file marks a default."""
    for stream in visible_streams(source):
        if stream.get("Type") == kind:
            return stream
    return None


def video_summary(stream):
    """Codec and resolution for the summary line, or the server's own
    ``DisplayTitle`` when it gave one.

    Codec *as well as* resolution: "1080p" alone drops the one thing that
    decides whether a file will direct-play.
    """
    if not stream:
        return ""
    if stream.get("DisplayTitle"):
        return stream["DisplayTitle"]
    bits = [str(stream.get("Codec") or "").upper()]
    if stream.get("Width") and stream.get("Height"):
        bits.append("%dx%d" % (stream["Width"], stream["Height"]))
    elif stream.get("Height"):
        bits.append("%dp" % stream["Height"])
    return " ".join(b for b in bits if b)


def audio_summary(stream):
    """Codec and channel layout for the summary line."""
    if not stream:
        return ""
    bits = [str(stream.get("Codec") or "").upper(),
            stream.get("ChannelLayout") or ""]
    return " ".join(b for b in bits if b)


def video_range(stream):
    """The HDR flavour, or "" for SDR.

    ``VideoRangeType`` first: ``VideoRange`` only says HDR, not which.
    """
    if not stream:
        return ""
    value = stream.get("VideoRangeType") or stream.get("VideoRange") or ""
    return "" if value == "SDR" else value


def summary_parts(source):
    """The detail page's one-line summary, as its pieces.

    The caller joins them, and adds whatever else belongs to its own screen
    (the detail page appends an "Ends at" that is a property of the *item*,
    not of the file).
    """
    from .labels import human_size

    source = source or {}
    parts = []
    video = primary_stream(source, "Video")
    if video:
        parts.append(video_summary(video))
        parts.append(video_range(video))
    audio = primary_stream(source, "Audio")
    if audio:
        parts.append(audio_summary(audio))
    if source.get("Container"):
        parts.append(str(source["Container"]).upper())
    if source.get("Size"):
        parts.append(human_size(source["Size"]))
    if source.get("Bitrate"):
        parts.append(_("%.1f Mbps") % (source["Bitrate"] / 1000000.0))
    return [p for p in parts if p]


# -- play method ---------------------------------------------------------

#: How the stream reached us. Derived, not asked for: unlike jellyfin-web —
#: which has to read its own session back off the server because the server
#: is what decided — *we* made this decision, in
#: ``media.Video._get_url_from_source``. See docs/UI_FIXES_4.md §10.
DIRECT_PLAY = "DirectPlay"
DIRECT_STREAM = "DirectStream"
REMUX = "Remux"
TRANSCODE = "Transcode"


def play_method_label(method):
    """A play method as the word jellyfin-web shows for it."""
    return {
        DIRECT_PLAY: _("Direct playing"),
        DIRECT_STREAM: _("Direct streaming"),
        REMUX: _("Remuxing"),
        TRANSCODE: _("Transcoding"),
    }.get(method, "")


#: ``TranscodeReasons`` as sentences. The server sends these as bare enum
#: names ("VideoCodecNotSupported"), which is the single most useful thing on
#: the screen and the least readable — it is the answer to "why is my server
#: pinned at 100%".
_REASONS = {
    "ContainerNotSupported": _("The container is not supported."),
    "VideoCodecNotSupported": _("The video codec is not supported."),
    "AudioCodecNotSupported": _("The audio codec is not supported."),
    "SubtitleCodecNotSupported": _("The subtitle codec is not supported."),
    "AudioIsExternal": _("The audio track is external."),
    "SecondaryAudioNotSupported": _("Secondary audio is not supported."),
    "VideoProfileNotSupported": _("The video profile is not supported."),
    "VideoLevelNotSupported": _("The video level is not supported."),
    "VideoResolutionNotSupported": _("The video resolution is not supported."),
    "VideoBitDepthNotSupported": _("The video bit depth is not supported."),
    "VideoFramerateNotSupported": _("The video framerate is not supported."),
    "RefFramesNotSupported": _("The number of reference frames is not "
                               "supported."),
    "AnamorphicVideoNotSupported": _("Anamorphic video is not supported."),
    "InterlacedVideoNotSupported": _("Interlaced video is not supported."),
    "AudioChannelsNotSupported": _("The audio channel count is not "
                                   "supported."),
    "AudioProfileNotSupported": _("The audio profile is not supported."),
    "AudioSampleRateNotSupported": _("The audio sample rate is not "
                                     "supported."),
    "AudioBitDepthNotSupported": _("The audio bit depth is not supported."),
    "ContainerBitrateExceedsLimit": _("The bitrate exceeds the limit."),
    "VideoBitrateNotSupported": _("The video bitrate is not supported."),
    "AudioBitrateNotSupported": _("The audio bitrate is not supported."),
    "UnknownVideoStreamInfo": _("The video stream could not be inspected."),
    "UnknownAudioStreamInfo": _("The audio stream could not be inspected."),
    "DirectPlayError": _("Direct play failed."),
    "VideoRangeTypeNotSupported": _("The video range is not supported."),
}


def transcode_reasons(reasons):
    """``TranscodeReasons`` as readable sentences.

    An unknown reason is passed through rather than dropped: the enum grows
    with the server, and a name we do not have a sentence for is still the
    answer to the question.
    """
    if isinstance(reasons, str):
        # The server sends this as a comma-joined flags string on some
        # endpoints and as a list on others.
        reasons = [r.strip() for r in reasons.split(",") if r.strip()]
    return [_REASONS.get(r, r) for r in (reasons or ())]
