#!/usr/bin/env python3
"""Generate a synthetic movie library: N one-second clips, each with a poster.

    python3 tools/jf_disposable/make_library.py "$W/media/Movies" 120

Each movie gets its own folder ("Synthetic Movie 001 (1981)") holding a 320x180
one-second MKV and a 1000x1500 `poster.jpg` that Jellyfin picks up as the
primary image, so no metadata provider is needed. The posters are gradients with
circles, noise and a number -- detailed enough that the JPEGs are a realistic
size (~300 KB), so scrolling a grid of them exercises image loading as well as
layout. The seed is fixed: the same arguments always give the same library.
Needs Pillow and ffmpeg. Nothing here touches the network.
"""
import argparse
import os
import random
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="directory to create the movie folders in")
    ap.add_argument("count", type=int, help="how many movies")
    args = ap.parse_args(argv)
    random.seed(7)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    except OSError:
        font = ImageFont.load_default()
    w, h = 1000, 1500
    for i in range(1, args.count + 1):
        name = "Synthetic Movie %03d (%d)" % (i, 1980 + i % 40)
        folder = os.path.join(args.root, name)
        os.makedirs(folder, exist_ok=True)
        c1 = tuple(random.randint(20, 235) for _ in range(3))
        c2 = tuple(random.randint(20, 235) for _ in range(3))
        gradient = Image.linear_gradient("L").resize((w, h))
        img = Image.composite(Image.new("RGB", (w, h), c1),
                              Image.new("RGB", (w, h), c2), gradient)
        draw = ImageDraw.Draw(img)
        for _ in range(40):
            x, y, r = random.randint(0, w), random.randint(0, h), random.randint(30, 300)
            draw.ellipse([x - r, y - r, x + r, y + r],
                         outline=tuple(random.randint(0, 255) for _ in range(3)),
                         width=random.randint(2, 14))
        img = img.filter(ImageFilter.GaussianBlur(1.2))
        img = Image.blend(img, Image.effect_noise((w, h), 30).convert("RGB"), 0.12)
        ImageDraw.Draw(img).text((60, 560), "MOVIE\n%03d" % i, font=font, fill=(255, 255, 255))
        img.save(os.path.join(folder, "poster.jpg"), quality=88)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x%02x%02x%02x:s=320x180:r=10" % c1,
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
             "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             os.path.join(folder, name + ".mkv")], check=True)
    print("made %d movies in %s" % (args.count, args.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
