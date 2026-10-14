"""Chapter-image previews when some chapters have no image.

A chapter whose image extraction failed has no ``ImageTag``. The preview
times skipped it and the image fetch did not, so the fetch raised KeyError on
it (no chapter previews for the item), or -- with a null tag -- every frame
after it would be drawn under the next chapter's time.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.media import Video  # noqa: E402


class _Jellyfin:
    def __init__(self):
        self.asked = []

    def get_chapter_image(self, dest, item_id, index, tag=None, **_kw):
        self.asked.append((index, tag))
        dest.write(("image %d" % index).encode())


class _Client:
    def __init__(self):
        self.jellyfin = _Jellyfin()


def video(chapters):
    v = Video.__new__(Video)        # the real class; its constructor plays
    v.item_id = "item-1"
    v.item = {"Chapters": chapters}
    v.client = _Client()
    return v


def chapter(n, tag="missing"):
    c = {"StartPositionTicks": n * 600_000_000, "Name": "Chapter %d" % n}
    if tag != "missing":
        c["ImageTag"] = tag
    return c


class ChapterImagesTest(unittest.TestCase):
    def test_times_and_images_line_up_around_a_missing_image(self):
        for gap in ("missing", None):           # key absent, or null
            v = video([chapter(0, "t0"), chapter(1, gap), chapter(2, "t2")])
            times = [c["start"] for c in v.get_chapters()]
            images = list(v.get_chapter_images())
            self.assertEqual(times, [0.0, 120.0], gap)
            self.assertEqual(images, [b"image 0", b"image 2"], gap)
            self.assertEqual(v.client.jellyfin.asked,
                             [(0, "t0"), (2, "t2")],
                             "the server indexes chapter images by chapter")

    def test_no_chapters_at_all(self):
        v = video(None)
        self.assertEqual(v.get_chapters(), [])
        self.assertEqual(list(v.get_chapter_images()), [])


if __name__ == "__main__":
    unittest.main()
