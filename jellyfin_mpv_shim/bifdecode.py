#!/usr/bin/env python3
import struct
from io import BytesIO
from PIL import Image

BIF_MAGIC = b"\x89BIF\r\n\x1a\n"
BIF_SUPPORTED = 0


def decode_file(filename):
    with open(filename, "rb") as fh:
        return decode(fh)


def _read_i32(fh):
    return struct.unpack("<I", fh.read(4))[0]


def decode(fh):
    if fh.read(8) != BIF_MAGIC:
        raise ValueError("Data provided is not a BIF file.")

    bif_version = _read_i32(fh)
    if bif_version != BIF_SUPPORTED:
        raise ValueError(f"BIF version {bif_version} is not supported.")

    image_count = _read_i32(fh)
    multiplier = _read_i32(fh)

    fh.read(44)  # unused data

    index = []  # timestamp, offset
    for _ in range(image_count):
        index.append((_read_i32(fh), _read_i32(fh)))

    images = []
    for i in range(len(index)):
        timestamp, offset = index[i]

        if i != timestamp:
            raise ValueError("BIF file is not contiguous.")

        fh.seek(offset)
        if i + 1 == len(index):
            images.append(fh.read())
        else:
            images.append(fh.read(index[i + 1][1] - offset))

    return {"multiplier": multiplier, "images": images}


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


if __name__ == "__main__":
    import sys

    bif_data = decode_file(sys.argv[1])
    print(f"Images: {len(bif_data['images'])}")
    print(f"Multiplier: {bif_data['multiplier']}")

    # for timestamp, image in enumerate(bif_data["images"]):
    #    with open(f"{timestamp}.jpg", "wb") as fh:
    #        fh.write(image)

    with open("raw_images.bin", "wb") as fh:
        bif_size_info = decompress_bif(bif_data["images"], fh)

    print(f"Width: {bif_size_info['width']}")
    print(f"Height: {bif_size_info['height']}")
