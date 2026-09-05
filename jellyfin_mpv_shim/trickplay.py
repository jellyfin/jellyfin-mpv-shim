import threading
import os
import logging

from .conf import settings
from . import conffile
from .constants import APP_NAME

try:
    from . import bifdecode

    BIFDECODE_AVAILABLE = bifdecode.PIL_AVAILABLE
except ImportError:
    BIFDECODE_AVAILABLE = False
    bifdecode = None

log = logging.getLogger("trickplay")

# Frame files are per-generation, never a single reused path.
#
# mpv MMAPS whatever file it is handed for overlay-add, so rewriting one in
# place is unsafe: `open(path, "wb")` truncates the existing inode, and the
# mapping mpv still holds then extends past EOF — a SIGBUS in the mpv process
# (renderer.lua says as much: "mpv mmaps the file and reading past EOF is a
# SIGBUS crash"). It also let the Lua side keep the PREVIOUS video's
# width/height/count while pointing at the new video's bytes.
#
# A fresh path per generation makes both impossible: the old inode is never
# written again, and the old file is only unlinked once the renderer has been
# pointed somewhere else.
IMG_PREFIX = "raw_images"
IMG_SUFFIX = ".bin"

# Module-level, not per-instance. A new TrickPlay is built on every mpv
# re-creation, so a per-instance counter restarts at 0 and the SECOND
# generation of the second worker reuses the first's path -- which is the
# one thing the comment above says cannot happen. stop(join=False) lets a
# straggler finish and publish, and script_message resolves the live mpv at
# call time, so the new renderer can be pointed at a name the new worker is
# about to open("wb"). mpv mmaps what it is handed and reading past a
# truncated EOF is a SIGBUS in the mpv process; the mpvtk HUD reads these
# frames through mpv now rather than in Python, so that stopped being a
# missing thumbnail and became a crash.
_seq_lock = threading.Lock()
_seq = 0


def _next_seq():
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _img_path(seq):
    return conffile.get(APP_NAME, "%s.%d%s" % (IMG_PREFIX, seq, IMG_SUFFIX))


#: Decoded BGRA one window may occupy.
#:
#: A frame is ``Width * Height * 4`` bytes. **[iw]**: "if it's in ram I would
#: say stay below 25 MB if possible as this is for one feature, unless the
#: user requests the trickplay fast mode where it just hands the entire file
#: to mpv where it can seek faster."
#:
#: This bounds the FILE and the work done before a preview appears -- plus
#: resident memory on an mpv old enough to still mmap what it is handed. The
#: peak it does **not** bound is Python's, which is per TILE and belongs to
#: `bifdecode`. Both measured: docs/artwork-pipeline.md section 11.0.
#:
#: Held constant in BYTES rather than in minutes deliberately: what is
#: scarce scales with the frame, so a library with big thumbnails should get
#: a shorter window rather than a bigger file. At 320x134 that is about 24
#: minutes of video, at 640x268 about six.
WINDOW_BUDGET_BYTES = 25 * 1024 * 1024

# Frame files waiting to be removed, for either of two reasons: a removal
# Windows refused (it will not unlink a file mpv still has open), or a file
# `_publish` deliberately held over one cycle so a render that has not yet
# processed the message pointing it elsewhere still finds its bytes. Both
# mean "remove on a later pass", so they share one list.
#
# Windowed fetching is what makes this worth having: it turns both cases
# from once-per-video into once-per-window-swap, and a long session would
# otherwise strand a window's worth of frames for every place the user
# looked.
_pending_unlink = []
_pending_lock = threading.Lock()


def _unlink(path):
    """Best-effort removal. On Windows this fails while mpv holds the file
    mapped; the name is never reused, so the file is remembered and retried
    rather than left for the next run's cleanup_stale_files()."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        log.debug("Could not remove %s", path, exc_info=True)
        with _pending_lock:
            if path not in _pending_unlink:
                _pending_unlink.append(path)


def _sweep_unlinks():
    """Retry the removals that were refused earlier."""
    with _pending_lock:
        if not _pending_unlink:
            return
        paths, _pending_unlink[:] = list(_pending_unlink), []
    for path in paths:
        _unlink(path)


def cleanup_stale_files():
    """Remove frame files left behind by a previous run.

    They are unlinked on the way out normally, but a crash or a kill leaves
    them, and nothing else ever will — the names are unique per generation.
    """
    try:
        directory = os.path.dirname(_img_path(0))
        for name in os.listdir(directory):
            if not (name.startswith(IMG_PREFIX + ".")
                    and name.endswith(IMG_SUFFIX)):
                continue
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                log.debug("Could not remove stale %s", name, exc_info=True)
        # The pre-per-generation name, from an older version.
        legacy = conffile.get(APP_NAME, IMG_PREFIX + IMG_SUFFIX)
        if os.path.isfile(legacy):
            os.remove(legacy)
    except Exception:
        log.debug("Could not clean up stale trickplay files.", exc_info=True)


class TrickPlay(threading.Thread):
    def __init__(self, player):
        self.trigger = threading.Event()
        self.halt = False
        self.player = player
        # The file the renderer is currently pointed at. The generation
        # counter behind the names is module-level (see _next_seq); this is
        # touched by the worker thread and by stop()/clear(), hence the lock.
        self._current = None
        self._file_lock = threading.Lock()
        # Where the next fetch should centre its window, in seconds. A hint,
        # not a queue: every request overwrites it, so a drag across the
        # whole seek bar coalesces into one fetch of wherever the pointer
        # ended up instead of a fetch per frame boundary it crossed.
        self._want = 0.0
        # The manifest for the video the window below belongs to, cached
        # because get_bif() re-fetches the ITEM when Trickplay was not in
        # the fields it was loaded with -- a round trip per scrub otherwise.
        self._bif = None            # (video, manifest) or None
        # (video, first, count, total) for the published window, so a scrub
        # into frames already on disk asks the server for nothing.
        self._window = None

        threading.Thread.__init__(self)
        # Daemon so a stop that can't join (see below) never blocks process
        # exit, and a lingering worker from a re-open can't either.
        self.daemon = True

    def stop(self, join=True):
        # join=False is required when stopping from a context that holds the
        # player lock: this worker's run loop calls player.script_message
        # (which takes that same lock), so joining under it would deadlock.
        # The worker still exits promptly on its next loop turn via `halt`.
        self.halt = True
        self.trigger.set()
        # No shim-trickplay-clear here, unlike clear(): stop() runs while mpv
        # is being torn down (and may run under the player lock, which
        # script_message also takes), so talking to that instance is both
        # pointless and a place to block. Safe because the path is never
        # reused — a stale overlay in a dying mpv refers to an inode nothing
        # will write again, rather than to a file the next video truncates
        # under it.
        self._retire_current()
        if join:
            self.join()

    def fetch_thumbnails(self, position=0.0):
        """Load the window around ``position`` (seconds).

        The player passes where playback actually starts, which for a resumed
        item is not zero -- the first place the user is going to scrub is
        where they already are.
        """
        self._want = max(0.0, position or 0.0)
        self.trigger.set()

    def request_at(self, seconds):
        """A preview was asked for at ``seconds`` and the window does not
        cover it. Sent by the renderer (and by thumbfast.lua for the lua
        OSCs), which is the only side that knows where the pointer is.

        Runs on mpv's event thread -- see ``player._on_client_message`` --
        so it must not do anything but record and wake.
        """
        self._want = max(0.0, seconds or 0.0)
        self.trigger.set()

    def clear(self):
        # Renderer first, file second: overlay-remove has to land before the
        # bytes behind it go away.
        self.player.script_message("shim-trickplay-clear")
        self._retire_current()
        self._bif = None
        self._window = None

    # -- frame-file lifecycle ---------------------------------------------

    def _next_file(self):
        """A fresh path for the next set of frames. Never an existing one,
        for the life of the PROCESS -- see _next_seq."""
        return _img_path(_next_seq())

    def _publish(self, path):
        """Adopt `path` as the live frame file and retire the previous one.

        The previous file is dropped **one publish later**, not now:
        `script_message` returns when the message is queued, not when lua has
        run it, so a render timer firing in between still issues `overlay-add`
        against the OLD path (docs/artwork-pipeline.md section 11.4). One
        cycle of grace costs at most a second window on disk.
        """
        with self._file_lock:
            old, self._current = self._current, path
        # Sweep BEFORE queueing, so `old` survives exactly one more publish.
        _sweep_unlinks()
        if old:
            with _pending_lock:
                if old not in _pending_unlink:
                    _pending_unlink.append(old)

    def _retire_current(self):
        with self._file_lock:
            old, self._current = self._current, None
        _unlink(old)
        _sweep_unlinks()

    # -- windowing ---------------------------------------------------------

    def _manifest(self, video):
        """This video's trickplay manifest, fetched once.

        ``get_bif`` re-requests the whole ITEM when ``Trickplay`` was not in
        the fields it was loaded with, so asking per window would put a round
        trip in front of every scrub.
        """
        cached = self._bif
        if cached is not None and cached[0] is video:
            return cached[1]
        data = video.get_bif(settings.thumbnail_preferred_size)
        self._bif = (video, data)
        return data

    @staticmethod
    def _frame_index(data, seconds):
        """Which frame of the WHOLE video ``seconds`` falls on.

        The cadence is the video's, not the window's -- ``Interval`` is
        milliseconds between thumbnails from the start of the file -- so
        this is the number every consumer computes and then rebases onto
        whatever part of it is loaded.
        """
        total = int(data["ThumbnailCount"])
        interval = max(1, int(data["Interval"]))
        return max(0, min(int(seconds * 1000 // interval), total - 1))

    @staticmethod
    def _window_for(data, seconds):
        """``(first, count)`` -- the frames to put on disk for a preview at
        ``seconds``.

        The whole video when the user asked for it (fast mode) or when it
        fits under WINDOW_BUDGET_BYTES anyway, which is most short content
        and is why an ordinary episode never pays for windowing. Otherwise a
        budget's worth centred on the position, clamped to the video so the
        first and last few minutes are reachable without a window that runs
        off either end.
        """
        total = int(data["ThumbnailCount"])
        frame = int(data["Width"]) * int(data["Height"]) * 4
        if total <= 0 or frame <= 0:
            return 0, total
        budget = max(1, WINDOW_BUDGET_BYTES // frame)
        if settings.trickplay_fast_mode or budget >= total:
            return 0, total
        centre = TrickPlay._frame_index(data, seconds)
        first = max(0, min(centre - budget // 2, total - budget))
        return first, budget

    def _covers(self, video, frame):
        """Whether the last fetch already answered for ``frame`` of this video.

        Asked about the POSITION, not about the range a fresh window would
        have: the window is recentred on wherever it was asked for, so
        comparing ranges answers "no" for every request that is not dead
        centre and re-fetches the neighbouring frames of a file already on
        disk. This is what makes the window hysteretic -- one fetch buys
        half a window of scrubbing in either direction.

        The span recorded is the one that was ASKED for, not the number of
        frames that arrived. Those differ when the tile source ends early --
        in practice a partial download, since an online tile failure aborts
        the window rather than truncating it -- and the difference is a
        loop: the frames in the shortfall are inside the
        window every request for them produces, so answering "not covered"
        re-fetches, re-publishes, clears the consumer's one-ask-per-window
        marker, and is asked again on the very next frame -- at render
        cadence, for as long as the pointer sits there. Recording the
        attempt is what makes a second fetch pointless *and* silent. Nothing
        is lost by it: a re-fetch would come back equally short, and a new
        item or a clear() drops this outright.
        """
        cur = self._window
        if cur is None or cur[0] is not video:
            return False
        _v, first, asked, _total = cur
        return first <= frame < first + asked

    def run(self):
        if not BIFDECODE_AVAILABLE:
            log.warning(
                "Trickplay thumbnails disabled: Pillow (PIL) not available. Install with: pip install pillow"
            )
            return

        while not self.halt:
            self.trigger.wait()
            self.trigger.clear()

            if self.halt:
                break

            try:
                log.info("Collecting trickplay images...")

                if not self.player.has_video():
                    continue

                video = self.player.get_video()
                want = self._want
                # Set once the bif branch owns this pass. A failure AFTER
                # that point must not fall through to the chapter images
                # below -- see the handler at the bottom of the block.
                windowing = False
                path = None
                try:
                    data = self._manifest(video)
                    if not self.player.has_video() or video != self.player.get_video():
                        # Video changed while we were getting the bif file
                        continue

                    # A manifest that promises no frames falls through to
                    # the chapter images below rather than short-circuiting
                    # here: it is the same "no trickplay" the else branch
                    # answers, and the fallback is the point of that branch.
                    if data and int(data.get("ThumbnailCount") or 0) > 0:
                        windowing = True
                        if self._covers(video,
                                        self._frame_index(data, want)):
                            log.debug("Trickplay window already loaded.")
                            continue
                        first, count = self._window_for(data, want)

                        # Tiles are the DOWNLOAD unit and frames are the
                        # storage unit, and they do not divide: the window
                        # starts wherever it was centred, so the first tile
                        # is fetched whole and `skip` throws away the frames
                        # of it that fall before the window. Fetching from
                        # the tile boundary instead would be the same bytes
                        # off the network and a bigger file.
                        per_tile = (int(data["TileWidth"])
                                    * int(data["TileHeight"]))
                        tile_first = first // per_tile
                        tile_count = (
                            (first + count - 1) // per_tile - tile_first + 1
                        )
                        path = self._next_file()
                        with open(path, "wb") as fh:
                            written = bifdecode.decompress_tiles(
                                data["Width"],
                                data["Height"],
                                data["TileWidth"],
                                data["TileHeight"],
                                count,
                                video.get_hls_tile_images(
                                    data["Width"], tile_count, start=tile_first
                                ),
                                fh,
                                skip=first - tile_first * per_tile,
                            )

                        if not written:
                            log.warning("Trickplay produced no frames.")
                            _unlink(path)
                            continue
                        if written < count:
                            # The tile source ran short. Report what is
                            # actually in the file — a consumer seeks to
                            # frame * w * h * 4 inside it, so the count the
                            # window ASKED for would send it past the end.
                            log.warning(
                                "Trickplay short: %d of %d frames.",
                                written, count,
                            )
                        bif_meta = {
                            "count": written,
                            "multiplier": data["Interval"],
                            "width": data["Width"],
                            "height": data["Height"],
                        }

                        if (
                            not self.player.has_video()
                            or video != self.player.get_video()
                        ):
                            # Video changed while we were decompressing the bif file
                            _unlink(path)
                            continue

                        # One message, every consumer: thumbfast.lua for
                        # the lua OSCs, and mpvtk's renderer.lua, which
                        # reads the frames out of `path` itself for the
                        # playback HUD's scrub preview.
                        #
                        # `first` and `total` are what make the file a
                        # WINDOW rather than the video; without them a
                        # consumer indexes a 30-frame file with a frame
                        # number the video's length justifies
                        # (docs/artwork-pipeline.md section 11.2).
                        total = int(data["ThumbnailCount"])
                        self.player.script_message(
                            "shim-trickplay-bif",
                            str(bif_meta["count"]),
                            str(bif_meta["multiplier"]),
                            str(bif_meta["width"]),
                            str(bif_meta["height"]),
                            path,
                            str(first),
                            str(total),
                        )
                        # Both consumers now point at `path`; the previous
                        # generation is safe to drop. The consumers are told
                        # `written` (what is in the file); `_covers` records
                        # `count` (what was asked for) -- see its docstring
                        # for why those must be the different numbers.
                        self._window = (video, first, count, total)
                        self._publish(path)
                        log.info(
                            "Collected %d trickplay preview images "
                            "(frames %d-%d of %d)",
                            bif_meta["count"], first,
                            first + written - 1, total,
                        )
                        continue
                    else:
                        log.warning("No trickplay data available")
                except:
                    # Only if it never became the live file: `path` is the
                    # partial write to clean up, and testing that rather
                    # than the statement order is what keeps a future edit
                    # between _publish and `continue` from unlinking the
                    # window it just published.
                    if path and path != self._current:
                        _unlink(path)
                    if windowing:
                        # One window failed on an item that HAS trickplay:
                        # retry-by-scrubbing, not a downgrade. The published
                        # window is still correct, and the next position the
                        # consumer cannot draw asks again.
                        #
                        # This `continue` prevents falling through to the
                        # chapter images, which is a **one-way door**: once
                        # `shim-trickplay-chapters` is published neither
                        # consumer can ask for a window again, so one dropped
                        # tile would mean chapter stills for the rest of the
                        # item (docs/artwork-pipeline.md section 11.5).
                        log.error("Could not load a trickplay window.",
                                  exc_info=True)
                        continue
                    log.error(
                        "Could not get trickplay data.",
                        exc_info=True,
                    )

                chapter_data = video.get_chapters()

                if chapter_data is None or len(chapter_data) == 0:
                    log.info("No chapters available")
                    continue

                path = self._next_file()
                with open(path, "wb") as fh:
                    bif_meta = bifdecode.decompress_bif(
                        video.get_chapter_images(settings.thumbnail_preferred_size), fh
                    )

                if not self.player.has_video() or video != self.player.get_video():
                    # Video changed while we were getting the thumbnails.
                    # `continue`, like every sibling guard: this cancels the
                    # fetch, not the worker. Breaking left the loop for good,
                    # and nothing restarts it or even notices — the thread is
                    # created once per mpv instance and fetch_thumbnails() is
                    # a bare trigger.set() — so one skip during a chapter-image
                    # download silently ended scrub previews until mpv was
                    # re-created.
                    _unlink(path)
                    continue

                self.player.script_message(
                    "shim-trickplay-chapters",
                    str(bif_meta["width"]),
                    str(bif_meta["height"]),
                    str(path),
                    ",".join(str(x["start"]) for x in chapter_data),
                )
                # Not windowed, and it does not need to be: one frame per
                # chapter is a few dozen at most. Clearing `_window` is what
                # keeps that honest -- it describes the bif file, and the
                # live file is now this one.
                self._window = None
                self._publish(path)
                log.info(f"Collected {len(chapter_data)} chapter preview images")

            except:
                log.error("Could not get trickplay images", exc_info=True)
