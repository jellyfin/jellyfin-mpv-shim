#!/usr/bin/env python3
from io import BytesIO

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None


def decompress_tiles(width, height, tile_width, tile_height, count, tiles, fh):
    """Write `count` BGRA frames from `tiles` into `fh`.

    Returns the number of frames ACTUALLY written, which can be fewer than
    `count` when the tile source runs short (a 404 on a late tile, a
    truncated body, a server still generating trickplay). The caller must
    report this rather than the manifest's count: mpv is handed the file to
    mmap and seeks to `frame * width * height * 4`, so an over-reported count
    produces an offset past EOF, which is a SIGBUS in the mpv process.
    """
    if not PIL_AVAILABLE:
        raise ImportError(
            "Pillow (PIL) is required for trickplay thumbnails. Install with: pip install pillow"
        )

    image_count = 0

    for image in tiles:
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
