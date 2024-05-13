#!/usr/bin/env python3
from io import BytesIO
from PIL import Image


def decompress_tiles(width, height, tile_width, tile_height, count, tiles, fh):
    image_count = 0

    for image in tiles:
        image = Image.open(BytesIO(image)).convert("RGBA")

        if height * tile_height != image.height or width * tile_width != image.width:
            raise ValueError("Tile size mismatch.")

        r, g, b, a = image.split()
        image_data = Image.merge("RGBA", (b, g, r, a)).tobytes()

        for y in range(tile_height):
            for x in range(tile_width):
                if image_count >= count:
                    return
                image_count += 1

                for y_local in range(height):
                    position = (
                        y * height * width * tile_width * 4  # seek to correct row
                        + x * width * 4  # seek to correct column
                        + y_local * width * tile_width * 4  # seek to correct subrow
                    )
                    fh.write(image_data[position : position + width * 4])


def decompress_bif(images, fh):
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
