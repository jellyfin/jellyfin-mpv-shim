#!/usr/bin/env python3
from io import BytesIO

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None


def decompress_tiles(width, height, tile_width, tile_height, count, tiles, fh,
                     skip=0):
    """Write up to `count` BGRA frames from `tiles` into `fh`.

    `skip` drops that many frames off the FRONT of the stream without
    writing them. The server hands trickplay out one mosaic per
    `tile_width * tile_height` thumbnails, so a caller that wants a window
    of the video has to fetch whole tiles either way -- but a decoded frame
    is `width * height * 4` bytes and a tile of them is tens of megabytes,
    which is the half worth not keeping. `skip` is what lets the file hold
    the frames the window asked for rather than everything the tiles it
    came in happened to carry.

    Returns the number of frames ACTUALLY written, which can be fewer than
    `count` when the tile source ends early. Which sources do that is worth
    being precise about: `OfflineVideo.get_hls_tile_images` returns on a
    missing file, so a partial download runs short here, but `media.Video`'s
    RAISES -- an online tile failure aborts the whole window in
    `TrickPlay.run` and never reaches this return.

    The caller must report this number rather than the one it asked for. A
    consumer seeks to `frame * width * height * 4` inside the file, so an
    over-reported count is a read past the end of it: a failed `overlay-add`
    on a current mpv, and a SIGBUS in the mpv process on one old enough to
    still mmap what it is handed (docs/artwork-pipeline.md section 11).
    """
    if not PIL_AVAILABLE:
        raise ImportError(
            "Pillow (PIL) is required for trickplay thumbnails. Install with: pip install pillow"
        )

    seen = 0
    image_count = 0

    # Explicit iterator, so the `count` bound is checked BEFORE the next
    # tile is pulled. `tiles` is a generator that fetches over the network
    # one tile per step, so a `for` loop here downloads a whole mosaic to
    # discover it has already written everything it was asked for.
    tiles = iter(tiles)
    while image_count < count:
        try:
            image = next(tiles)
        except StopIteration:
            break
        # Opened, NOT converted. A mosaic is 3200x1340 at the server's
        # default preview width and twice that per axis at 640px, so every
        # WHOLE-TILE operation costs tens of megabytes -- and converting to
        # RGBA, splitting into four channel images, merging them back and
        # calling tobytes() on the result is four of them live at once.
        # Cropping each frame out first and converting only the crop leaves
        # the decoded tile as the only large buffer. Measured peak RSS for
        # one tile, byte-identical output: 86 -> 18 MB at 320x134,
        # 207 -> 71 MB at 640x268, and about 30% faster at the larger size
        # because it also replaces `height` Python slice-and-write calls per
        # frame with one.
        image = Image.open(BytesIO(image))

        if height * tile_height != image.height or width * tile_width != image.width:
            raise ValueError("Tile size mismatch.")

        for y in range(tile_height):
            for x in range(tile_width):
                if image_count >= count:
                    return image_count
                if seen < skip:
                    seen += 1
                    continue
                seen += 1
                image_count += 1

                frame = image.crop((x * width, y * height,
                                    (x + 1) * width, (y + 1) * height))
                # mpv is handed this file as BGRA. `raw`/`BGRA` is Pillow's
                # own channel-swapping packer and is byte-for-byte what the
                # split/merge/tobytes dance produced.
                fh.write(frame.convert("RGBA").tobytes("raw", "BGRA"))

    return image_count


def decompress_bif(images, fh):
    if not PIL_AVAILABLE:
        raise ImportError(
            "Pillow (PIL) is required for trickplay thumbnails. Install with: pip install pillow"
        )

    height = None
    width = None
    image_count = 0

    for image in images:
        image_count += 1
        image = Image.open(BytesIO(image)).convert("RGBA")
        if height is None:
            height = image.height
            width = image.width
        else:
            if height != image.height or width != image.width:
                raise ValueError("BIF image sizes mismatch.")

        r, g, b, a = image.split()
        image = Image.merge("RGBA", (b, g, r, a))

        fh.write(image.tobytes())

    return {"count": image_count, "height": height, "width": width}
