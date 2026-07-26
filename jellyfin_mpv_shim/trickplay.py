import threading
import os
import logging
import math

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


def _img_path(seq):
    return conffile.get(APP_NAME, "%s.%d%s" % (IMG_PREFIX, seq, IMG_SUFFIX))


def _unlink(path):
    """Best-effort removal. On Windows this fails while mpv holds the file
    mapped; that is harmless — the name is never reused, and the next run's
    cleanup_stale_files() collects it."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        log.debug("Could not remove %s", path, exc_info=True)


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
        # Generation counter for frame-file names, and the file the renderer
        # is currently pointed at. Only the worker thread advances _seq; both
        # it and stop()/clear() touch _current, hence the lock.
        self._seq = 0
        self._current = None
        self._file_lock = threading.Lock()

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
        self.player.trickplay_meta = None
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

    def fetch_thumbnails(self):
        self.trigger.set()

    def clear(self):
        self.player.trickplay_meta = None
        # Renderer first, file second: overlay-remove has to land before the
        # bytes behind it go away.
        self.player.script_message("shim-trickplay-clear")
        self._retire_current()

    # -- frame-file lifecycle ---------------------------------------------

    def _next_file(self):
        """A fresh path for the next set of frames. Never an existing one."""
        with self._file_lock:
            self._seq += 1
            return _img_path(self._seq)

    def _publish(self, path):
        """Adopt `path` as the live frame file and drop the previous one.

        Called only AFTER the renderer has been pointed at `path`, so the
        file being unlinked is one nothing is being told to read any more.
        """
        with self._file_lock:
            old, self._current = self._current, path
        _unlink(old)

    def _retire_current(self):
        with self._file_lock:
            old, self._current = self._current, None
        _unlink(old)

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
                try:
                    data = video.get_bif(settings.thumbnail_preferred_size)
                    if not self.player.has_video() or video != self.player.get_video():
                        # Video changed while we were getting the bif file
                        continue

                    if data:
                        path = self._next_file()
                        with open(path, "wb") as fh:
                            img_count = math.ceil(
                                data["ThumbnailCount"]
                                / data["TileWidth"]
                                / data["TileHeight"]
                            )
                            written = bifdecode.decompress_tiles(
                                data["Width"],
                                data["Height"],
                                data["TileWidth"],
                                data["TileHeight"],
                                data["ThumbnailCount"],
                                video.get_hls_tile_images(data["Width"], img_count),
                                fh,
                            )

                        if not written:
                            log.warning("Trickplay produced no frames.")
                            _unlink(path)
                            continue
                        if written < data["ThumbnailCount"]:
                            # The tile source ran short. Report what is
                            # actually in the file — mpv seeks to
                            # frame * w * h * 4 in a mapping of it, so the
                            # manifest's count would send it past EOF.
                            log.warning(
                                "Trickplay short: %d of %d frames.",
                                written, data["ThumbnailCount"],
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

                        # Same data both ways: the lua OSCs get a script
                        # message; the mpvtk HUD reads the raw frames via
                        # this metadata (see player.trickplay_meta).
                        self.player.trickplay_meta = dict(
                            bif_meta, file=path
                        )
                        self.player.script_message(
                            "shim-trickplay-bif",
                            str(bif_meta["count"]),
                            str(bif_meta["multiplier"]),
                            str(bif_meta["width"]),
                            str(bif_meta["height"]),
                            path,
                        )
                        # Both consumers now point at `path`; the previous
                        # generation is safe to drop.
                        self._publish(path)
                        log.info(
                            f"Collected {bif_meta['count']} trickplay preview images"
                        )
                        continue
                    else:
                        log.warning("No trickplay data available")
                except:
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
                    # Video changed while we were getting the thumbnails
                    _unlink(path)
                    break

                self.player.script_message(
                    "shim-trickplay-chapters",
                    str(bif_meta["width"]),
                    str(bif_meta["height"]),
                    str(path),
                    ",".join(str(x["start"]) for x in chapter_data),
                )
                self._publish(path)
                log.info(f"Collected {len(chapter_data)} chapter preview images")

            except:
                log.error("Could not get trickplay images", exc_info=True)
