"""Audio output configuration: what mpv is told about channels, passthrough
and filters.

Moved out of ``player.py`` as ``AudioMixin``. It is the most self-contained
thing in that file: nine methods that call nothing else on the player, and a
lock of their own (``_audio_lock``) that has nothing to do with the big
``_lock`` playback runs under.

**A mixin, not a collaborator.** ``PlayerManager`` keeps one identity and one
set of locks. Making this an object the player *owns* would mean handing it a
back-reference to reach ``_player``, ``_audio_lock``, ``_audio_configured``
and ``_audio_snapshot`` -- which is the same coupling with an indirection in
front of it, and it would put the audio work under a second lock whose
ordering against ``_lock`` nobody had reasoned about. Inheritance keeps
``self`` meaning exactly what it meant before.

**Two locks, deliberately.** ``_audio_lock`` covers the settings read and the
mpv writes it implies. It is *not* ``_lock``: that one is held for the whole
of a playback start, so borrowing it here would make an audio toggle wait out
a load.

**Why the backend globals are imported inside the methods.** ``is_using_ext_mpv``
and ``_mpv_errors`` are decided at ``player`` import time. Importing them here
at module scope cannot work -- ``player`` imports this module, so it would be
mid-import -- and hoisting them into a third module made things worse rather
than better: the backend identity would then be captured by *three* modules,
and the integration harness swaps a fake mpv in and out by evicting modules
from ``sys.modules``, so all three would have to be evicted in lockstep (with
another added per future extraction). Reading them per call keeps ``player``
the single owner, and has the side benefit that a test patching
``player.is_using_ext_mpv`` is honoured here too. This is the same pattern the
gateway modules and ``mpvtk_browser/ui.py`` already use.

The logger is ``player`` rather than ``player.audio`` on purpose -- the log
format prints the logger name, and these messages have been landing in users'
log.txt under "player" for as long as the feature has existed.

Before editing this file, read ``docs/mpv-backends.md``.
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from .conf import settings

log = logging.getLogger("player")

# --- Audio output modes ---------------------------------------------------
#
# "auto" is the default and is defined by doing *nothing*: no audio-channels,
# no audio-spdif, no filters. mpv's own defaults (and anything in the user's
# mpv.conf) are left entirely alone. The other modes each describe a physical
# connection to a receiver.
#
# Which codecs a mode can pass through is a property of the cable. S/PDIF
# (optical/coax) carries ~1.5 Mbps, which fits AC3 and DTS core and nothing
# else; HDMI has the bandwidth for the high-bitrate and lossless formats too.
AUDIO_PASSTHROUGH_CODECS = {
    "optical": ("ac3", "dts"),
    "hdmi": ("ac3", "dts", "eac3", "dts-hd", "truehd"),
}

# What each mode sets audio-channels to. "auto" is absent on purpose.
AUDIO_MODE_CHANNELS = {
    "stereo": "2.0",
    "optical": "5.1,2.0",
    "hdmi": "7.1,5.1,2.0",
}

# Filter labels. Labelled so they can be removed again -- jellyfin-media-player
# added its AC3 encoder unlabelled but removed "@ac3", which never matched, so
# once switched on the filter stayed for the rest of the session.
AF_NIGHT_MODE = "jfnight"
AF_AC3_ENCODE = "jfac3"

# Night mode. dynaudnorm rather than loudnorm or acompressor: it is the one
# designed for real-time use (loudnorm's single-pass mode buffers and drifts),
# and it lifts quiet dialogue as well as taming loud effects. These are the
# widely-used mpv night-mode values.
NIGHT_MODE_FILTER = "dynaudnorm=g=5:f=250:r=0.9:p=0.5"

# Encoding to AC3 is the only way surround crosses an optical cable when the
# track is not already AC3 or DTS. minch=3 makes the filter detach itself for
# stereo content, which should just go out as PCM.
AC3_ENCODE_FILTER = "lavcac3enc=minch=3"


def audio_passthrough_enabled(codec: str) -> bool:
    """Whether the user has left ``codec`` (mpv's spelling) ticked."""
    return bool(getattr(settings, "audio_passthrough_" + codec.replace("-", "_"), False))


def audio_spdif_codecs(mode: str, night_mode: bool, enabled=audio_passthrough_enabled):
    """The codec list for mpv's ``audio-spdif``, for ``mode``.

    Empty whenever night mode is on. Passthrough hands the receiver an
    undecoded compressed stream, and a PCM filter cannot run downstream of
    one -- mpv does not arbitrate between the two, the chain fails to build
    ("unsupported conversion: spdif-ac3 -> floatp") and mpv recovers by
    disabling the filter. So asking for both does not break playback; it
    makes night mode silently do nothing, which is worse than it sounds
    because there is no user-visible sign of it.
    """
    if night_mode:
        return []
    codecs = [c for c in AUDIO_PASSTHROUGH_CODECS.get(mode, ()) if enabled(c)]
    # Per mpv's manual, specifying both dts and dts-hd "behaves equivalent to
    # specifying dts-hd only". Drop the redundant entry so the value we set
    # reads the way it actually behaves.
    if "dts-hd" in codecs:
        codecs = [c for c in codecs if c != "dts"]
    return codecs


def audio_wants_ac3_encode(
    mode: str,
    track_codec: Optional[str],
    spdif_codecs,
    encode_others: bool = True,
    ac3_ok: bool = True,
) -> bool:
    """Whether ``lavcac3enc`` belongs in the chain for the current track.

    Only optical: HDMI carries multichannel PCM natively, so re-encoding
    there would throw away quality for nothing.

    The decision is per-track and cannot be made once at startup, which is
    the trap jellyfin-media-player fell into. Handing mpv both audio-spdif
    and lavcac3enc for the same track builds a chain it cannot satisfy
    ("unsupported conversion: spdif-ac3 -> floatp"); mpv recovers by
    disabling the filter, so the cost is that the filter silently does not
    apply -- the encoder, or night mode, quietly stops working. Pass the
    track through if we can, and reach for the encoder only for the ones we
    can't.

    ``encode_others`` off declines the encoder entirely; those tracks go out
    as stereo PCM, since S/PDIF cannot carry multichannel PCM either. That
    loses surround, but the encoder adds latency on some receivers.

    ``ac3_ok`` is the AC3 passthrough toggle, and it gates this too: the
    encoder emits an IEC61937 AC3 *bitstream*, not PCM, so a user who
    unticked AC3 because their receiver cannot decode it must not be sent
    AC3 by the back door.

    A ``track_codec`` of None means *unreadable*, and the answer stays yes:
    a needless re-encode is a better failure than losing surround. It does
    not mean "nothing is playing" -- callers must not ask about a track that
    does not exist, and ``_apply_audio_filters_locked`` is where that is
    enforced.
    """
    if mode != "optical" or not encode_others or not ac3_ok:
        return False
    if track_codec and track_codec.lower() in spdif_codecs:
        return False
    return True


class AudioMixin:
    """Audio output configuration for :class:`~jellyfin_mpv_shim.player.PlayerManager`.

    Reads ``self._player``, ``self._audio_lock``, ``self._audio_configured``,
    ``self._audio_snapshot`` and ``self._device_snapshot``, all of which
    ``PlayerManager.__init__`` owns. Nothing here calls back into the rest of the player.
    """

    if TYPE_CHECKING:
        # State owned by PlayerManager.__init__, not by this mixin. Declared
        # rather than baselined, for the reason gateway/base.py gives: a
        # single class hid this coupling, and the split is what makes it
        # visible. Four is the whole of it, and the list is short enough to
        # notice if it grows.
        _player: Any
        _audio_lock: Any
        _audio_configured: bool
        _audio_snapshot: Optional[dict]
        _device_snapshot: Optional[dict]

    def _mpv_property(self, prop):
        """Read an mpv property by its full (path) name on either backend."""
        from .player import is_using_ext_mpv

        if self._player is None:
            return None
        try:
            if is_using_ext_mpv:
                return self._player.command("get_property", prop)
            return self._player._get_property(prop)
        except Exception:
            return None

    def _attached_af_labels(self):
        """Labels currently in mpv's audio filter chain.

        None if the chain could not be read, which callers treat as "don't
        know" rather than "empty".
        """
        chain = self._mpv_property("af")
        if not isinstance(chain, (list, tuple)):
            return None
        return {
            entry.get("label")
            for entry in chain
            if isinstance(entry, dict) and entry.get("label")
        }

    def _set_af(self, label: str, filter_spec: Optional[str]):
        """Add or remove a labelled audio filter, idempotently.

        ``af remove`` on a label that isn't attached still succeeds, but mpv
        logs "Option af-remove: item label @x not found" at warn level for it
        -- which the shim surfaces, so an unconditional remove meant two
        warnings on every night-mode toggle and one per file in optical mode.
        Ask what is attached first, and skip the removal when there is
        nothing to remove. Reading the chain rather than tracking it in
        Python is deliberate: mpv drops a filter that fails to initialize, so
        our idea of what is attached can otherwise drift from the truth.
        """
        from .player import _mpv_errors

        if self._player is None:
            return
        try:
            attached = self._attached_af_labels()
            # None => unreadable; fall back to the unconditional remove, which
            # is correct, just noisy.
            if attached is None or label in attached:
                self._player.command("af", "remove", "@" + label)
            if filter_spec:
                self._player.command("af", "add", "@%s:%s" % (label, filter_spec))
        except _mpv_errors:
            raise
        except Exception:
            log.error("Could not update audio filter %s.", label, exc_info=True)

    # The properties apply_audio_settings writes, and therefore the ones it
    # has to be able to put back. Keyed by mpv name, valued by the attribute
    # name the backends expose (both accept underscores).
    _AUDIO_PROPS = {
        "audio-channels": "audio_channels",
        "audio-normalize-downmix": "audio_normalize_downmix",
        "audio-spdif": "audio_spdif",
    }

    # Device selection, kept OUT of _AUDIO_PROPS on purpose. Those are
    # restored whenever the mode returns to "auto", and the device is not a
    # property of the mode -- "leave my audio alone" and "use the S/PDIF card"
    # are an entirely reasonable pair, and entangling them would silently undo
    # the device the moment someone picked Default.
    _DEVICE_PROPS = {
        "audio-device": "audio_device",
        "audio-exclusive": "audio_exclusive",
    }

    def _apply_audio_device_locked(self):
        """Point mpv at the configured output device; caller holds the lock.

        Unset means *untouched*, not "auto": mpv's own default and anything in
        the user's mpv.conf stay in force. Going back to unset restores what
        was there before we first wrote it, for the same reason
        ``_restore_audio_state`` exists -- otherwise the only way back from a
        device chosen once would be to edit the config by hand.
        """
        want = getattr(settings, "audio_device", None) or None
        exclusive = bool(getattr(settings, "audio_exclusive", False))
        if self._device_snapshot is None:
            if want is None and not exclusive:
                return          # never touched it, nothing asked for: leave it
            self._device_snapshot = {
                prop: self._mpv_property(prop) for prop in self._DEVICE_PROPS
            }
        if want is None:
            original = (self._device_snapshot or {}).get("audio-device")
            if original is not None:
                self._player.audio_device = original
        else:
            self._player.audio_device = want
        self._player.audio_exclusive = exclusive

    def _snapshot_audio_state(self):
        """Record mpv's audio config before we first overwrite it.

        Whatever is in place at this point came from the user's own mpv.conf
        (or mpv's defaults), and returning to "Default (auto)" has to give it
        back. Restoring hardcoded defaults instead would silently discard an
        `audio-spdif=ac3,dts` the user had configured themselves -- and there
        would be no way to get it back short of restarting.
        """
        if self._audio_snapshot is not None:
            return
        self._audio_snapshot = {
            prop: self._mpv_property(prop) for prop in self._AUDIO_PROPS
        }

    def _restore_audio_state(self):
        snapshot = self._audio_snapshot or {}
        for prop, attr in self._AUDIO_PROPS.items():
            value = snapshot.get(prop)
            if value is not None:
                setattr(self._player, attr, value)

    def apply_audio_settings(self):
        """Push the audio output mode to mpv.

        Called once per mpv instance and again whenever the settings change.
        In "auto" mode this sets nothing: the point of that mode is that a
        user who configured audio in their own mpv.conf is left alone.

        The per-track half of the job (whether to engage the AC3 encoder)
        happens in apply_audio_filters, because it depends on what is
        playing.
        """
        from .player import _mpv_errors

        if self._player is None:
            return
        mode = settings.audio_mode or "auto"
        night = bool(settings.audio_night_mode)
        # One lock around the settings read and the writes it implies. Without
        # it a file loading on the action thread and a toggle on the browser
        # thread can interleave into a config neither of them asked for, and
        # nothing re-runs to correct it. Not _lock, which is held across a
        # whole playback start.
        with self._audio_lock:
            try:
                # Before the mode: unconditional, because the device is not
                # part of the mode and "auto" returns early below.
                self._apply_audio_device_locked()
            except _mpv_errors:
                raise
            except Exception:
                log.error("Could not apply the audio device.", exc_info=True)
            try:
                if mode == "auto" and not night and not self._audio_configured:
                    # Nothing applied to this mpv instance and nothing asked
                    # for: leave it completely alone. Once we *have* touched
                    # it the branch below runs instead, so returning to
                    # Default undoes our changes rather than stranding them.
                    return
                self._snapshot_audio_state()
                self._audio_configured = True
                channels = AUDIO_MODE_CHANNELS.get(mode)
                codecs = audio_spdif_codecs(mode, night)
                if mode == "auto":
                    # Hand back whatever the user had, then re-apply only what
                    # night mode genuinely requires (it cannot run on a
                    # passthrough stream, so passthrough has to go).
                    self._restore_audio_state()
                    if night:
                        self._player.audio_spdif = ""
                else:
                    self._player.audio_channels = channels
                    # Downmix normalization only matters when we are the ones
                    # downmixing, which is exactly the stereo case.
                    self._player.audio_normalize_downmix = mode == "stereo"
                    self._player.audio_spdif = ",".join(codecs)
                self._set_af(AF_NIGHT_MODE, NIGHT_MODE_FILTER if night else None)
                # The AC3 encoder is re-decided per track; drop any stale one
                # so a mode change out of optical takes effect without a
                # reload.
                if mode != "optical":
                    self._set_af(AF_AC3_ENCODE, None)
                else:
                    self._apply_audio_filters_locked()
                log.info(
                    "Audio config - mode: %s, channels: %s, passthrough: %s, "
                    "night mode: %s",
                    mode,
                    channels or "restored",
                    ",".join(codecs) or "none",
                    "on" if night else "off",
                )
            except _mpv_errors:
                raise
            except Exception:
                log.error("Could not apply audio settings.", exc_info=True)

    def apply_audio_filters(self):
        """Decide the AC3 encoder for the track that is playing now.

        Runs on every file load *and* every audio-track change: the choice
        depends on the selected track's codec, so switching from an AC3 track
        to a 5.1 AAC one has to re-decide or the surround is lost.
        """
        from .player import _mpv_errors

        if self._player is None:
            return
        with self._audio_lock:
            try:
                self._apply_audio_filters_locked()
            except _mpv_errors:
                raise
            except Exception:
                log.error("Could not apply audio filters.", exc_info=True)

    def _apply_audio_filters_locked(self):
        """apply_audio_filters' body; caller holds ``_audio_lock``.

        Optical only, and only for tracks we are *not* passing through --
        handing mpv audio-spdif and lavcac3enc for one track builds a chain
        it cannot satisfy, and it recovers by disabling the filter, so the
        encoder would simply stop working with nothing to show for it.

        **With no audio track selected there is nothing to decide**, and the
        encoder comes off. This is a real path, not a defensive one:
        ``apply_audio_settings`` runs from ``_init_mpv`` before anything has
        loaded, and from the menu with nothing playing. Deciding then meant
        asking about a track that did not exist, getting the unreadable-codec
        answer (yes, encode -- correct for its own case), and attaching the
        encoder to an idle player. The next AC3 file to load then built
        exactly the chain the paragraph above says never to build:

            ad: Failed to parse codec profile.
            swresample: unsupported conversion: spdif-ac3 -> floatp
            af: Disabling filter jfac3 because it has failed.

        mpv drops the filter and recovers, so the cost was three error lines
        per session and a decision made once at startup -- the trap
        ``audio_wants_ac3_encode`` exists to avoid. Nothing is lost by
        waiting: every file load and every audio-track change re-decides.
        """
        mode = settings.audio_mode or "auto"
        if mode != "optical":
            return
        if not self._mpv_property("current-tracks/audio"):
            self._set_af(AF_AC3_ENCODE, None)
            return
        codecs = audio_spdif_codecs(mode, bool(settings.audio_night_mode))
        track_codec = self._mpv_property("current-tracks/audio/codec")
        want = audio_wants_ac3_encode(
            mode, track_codec, codecs,
            bool(settings.audio_optical_encode_ac3),
            bool(settings.audio_passthrough_ac3))
        self._set_af(AF_AC3_ENCODE, AC3_ENCODE_FILTER if want else None)

    def set_night_mode(self, enabled: bool):
        """Toggle night mode and apply it live (no reload needed)."""
        settings.audio_night_mode = bool(enabled)
        settings.save()
        self.apply_audio_settings()
