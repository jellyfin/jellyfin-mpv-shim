import threading
import os
import logging

from .conf import settings
from . import bifdecode
from . import conffile
from .constants import APP_NAME

log = logging.getLogger("trickplay")
img_file = conffile.get(APP_NAME, "raw_images.bin")


class TrickPlay(threading.Thread):
    def __init__(self, player):
        self.trigger = threading.Event()
        self.halt = False
        self.player = player

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        if os.path.isfile(img_file):
            os.remove(img_file)
        self.join()

    def fetch_thumbnails(self):
        self.trigger.set()

    def clear(self):
        self.player.script_message("shim-trickplay-clear")
        if os.path.isfile(img_file):
            os.remove(img_file)

    def run(self):
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
                if settings.thumbnail_jellyscrub:
                    try:
                        data = video.get_bif(settings.thumbnail_preferred_size)
                        if data:
                            bif = bifdecode.decode(data)

                            if (
                                not self.player.has_video()
                                or video != self.player.get_video()
                            ):
                                # Video changed while we were getting the bif file
                                continue

                            with open(img_file, "wb") as fh:
                                bif_meta = bifdecode.decompress_bif(bif["images"], fh)

                            if (
                                not self.player.has_video()
                                or video != self.player.get_video()
                            ):
                                # Video changed while we were decompressing the bif file
                                continue

                            self.player.script_message(
                                "shim-trickplay-bif",
                                bif_meta["count"],
                                bif["multiplier"],
                                bif_meta["width"],
                                bif_meta["height"],
                                img_file,
                            )
                            log.info(
                                f"Collected {len(bif['images'])} bif preview images"
                            )
                            continue
                        else:
                            log.warning("No bif file available")
                    except:
                        log.error(
                            "Could not get bif file. Do you have the plugin installed?",
                            exc_info=True,
                        )

                chapter_data = video.get_chapters()

                if chapter_data is None or len(chapter_data) == 0:
                    log.info("No chapters available")
                    continue

                with open(img_file, "wb") as fh:
                    bif_meta = bifdecode.decompress_bif(
                        video.get_chapter_images(settings.thumbnail_preferred_size), fh
                    )

                if not self.player.has_video() or video != self.player.get_video():
                    # Video changed while we were getting the thumbnails
                    break

                self.player.script_message(
                    "shim-trickplay-chapters",
                    bif_meta["width"],
                    bif_meta["height"],
                    img_file,
                    ",".join(str(x["start"]) for x in chapter_data),
                )
                log.info(f"Collected {len(chapter_data)} chapter preview images")

            except:
                log.error("Could not get trickplay images", exc_info=True)
