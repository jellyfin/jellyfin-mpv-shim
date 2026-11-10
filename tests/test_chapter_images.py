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


# -- alternate versions ------------------------------------------------------
#
# A multi-version movie is several items. ``Video.item`` is the primary, and
# its Chapters (and chapter images) are the primary file's, so while another
# version plays the previews must come from that version's own item.

PRIMARY, ALT, ALT2 = "primary", "alt-1", "alt-2"


class _Versions:
    """The server: every item answers with its own chapters only."""

    def __init__(self, items, fail=()):
        self.items = items
        self.fail = set(fail)
        self.fetched = []       # get_item(owner, fields=...)
        self.images = []        # (item_id, index, tag)

    def get_item(self, item_id, fields=None):
        self.fetched.append((item_id, fields))
        if item_id in self.fail:
            raise OSError("down")
        return self.items[item_id]

    def get_chapter_image(self, dest, item_id, index, tag=None, **_kw):
        self.images.append((item_id, index, tag))
        dest.write(("%s/%d" % (item_id, index)).encode())


def chap(n, tag="t", step=10):
    c = {"StartPositionTicks": n * step * 10_000_000, "Name": "c%d" % n}
    if tag is not None:
        c["ImageTag"] = "%s%d" % (tag, n)
    return c


def versions(source=ALT, items=None, **kw):
    items = items if items is not None else {
        PRIMARY: {"Chapters": [chap(i, "p") for i in range(5)]},
        ALT: {"Chapters": [chap(i, "a", 7) for i in range(3)]},
        ALT2: {"Chapters": [chap(i, "b", 4) for i in range(2)]},
    }
    v = Video.__new__(Video)
    v.item_id = PRIMARY
    v.item = dict(items[PRIMARY])
    v.media_source = {"Id": source} if source else None
    v.client = type("C", (), {})()
    v.client.jellyfin = _Versions(items, **kw)
    return v


class AlternateVersionTest(unittest.TestCase):
    def setUp(self):
        import logging
        log = logging.getLogger("jellyfin_mpv_shim.media")
        old = log.level
        log.setLevel(logging.CRITICAL)
        self.addCleanup(log.setLevel, old)

    def test_primary_and_single_version_never_fetch(self):
        for source in (PRIMARY, None):
            v = versions(source)
            self.assertEqual([c["start"] for c in v.get_chapters()],
                             [0, 10, 20, 30, 40])
            list(v.get_chapter_images())
            self.assertEqual(v.client.jellyfin.fetched, [], source)
            self.assertEqual({i for i, _x, _t in v.client.jellyfin.images},
                             {PRIMARY})

    def test_an_alternate_uses_its_own_chapters_times_and_images(self):
        v = versions(ALT)
        self.assertEqual([c["start"] for c in v.get_chapters()], [0, 7, 14])
        self.assertEqual(list(v.get_chapter_images()),
                         [b"alt-1/0", b"alt-1/1", b"alt-1/2"])
        self.assertEqual(v.client.jellyfin.images,
                         [(ALT, 0, "a0"), (ALT, 1, "a1"), (ALT, 2, "a2")])
        self.assertEqual(v.client.jellyfin.fetched, [(ALT, "Chapters")])

    def test_chapter_counts_differ_and_the_primary_may_have_none(self):
        v = versions(ALT2)
        self.assertEqual(len(v.get_chapters()), 2)
        v = versions(ALT, items={PRIMARY: {}, ALT: {"Chapters": [
            chap(0, "a"), chap(1, "a")]}})
        self.assertEqual(len(v.get_chapters()), 2)
        v = versions(ALT, items={
            PRIMARY: {"Chapters": [chap(0, "p")]}, ALT: {"Chapters": []}})
        self.assertEqual(v.get_chapters(), [])
        self.assertEqual(list(v.get_chapter_images()), [])

    def test_an_alternate_chapter_without_an_image_keeps_alignment(self):
        items = {PRIMARY: {}, ALT: {"Chapters": [
            chap(0, "a"), chap(1, None), chap(2, "a")]}}
        v = versions(ALT, items=items)
        self.assertEqual([c["start"] for c in v.get_chapters()], [0, 20])
        list(v.get_chapter_images())
        self.assertEqual(v.client.jellyfin.images,
                         [(ALT, 0, "a0"), (ALT, 2, "a2")])

    def test_malformed_answers_are_no_chapters_not_the_primarys(self):
        for dto in (None, [], "x", {}, {"Chapters": None},
                    {"Chapters": "x"}, {"Chapters": {"a": 1}},
                    {"Chapters": [None, 3, "x", []]}):
            v = versions(ALT, items={PRIMARY: {"Chapters": [chap(0, "p")]},
                                     ALT: dto})
            self.assertEqual(v.get_chapters(), [], repr(dto))
            self.assertEqual(list(v.get_chapter_images()), [], repr(dto))
            self.assertEqual(v.client.jellyfin.images, [])

    def test_malformed_entries_are_cleaned(self):
        bad = {"Chapters": [
            {"StartPositionTicks": -5, "Name": 7, "ImageTag": "a"},
            {"StartPositionTicks": True, "ImageTag": ""},
            {"StartPositionTicks": "9", "Name": "n", "ImageTag": 3},
            {"StartPositionTicks": 20_000_000.0, "Name": "ok",
             "ImageTag": "z"}]}
        v = versions(ALT, items={PRIMARY: {}, ALT: bad})
        self.assertEqual(v.get_chapters(),
                         [{"start": 0.0, "name": ""},
                          {"start": 2.0, "name": "ok"}])
        list(v.get_chapter_images())
        self.assertEqual(v.client.jellyfin.images, [(ALT, 0, "a"), (ALT, 3, "z")])

    def test_a_failed_request_is_no_chapters_and_is_not_retried(self):
        v = versions(ALT, fail=[ALT])
        self.assertEqual(v.get_chapters(), [])
        self.assertEqual(list(v.get_chapter_images()), [])
        self.assertEqual(v.get_chapters(), [])
        self.assertEqual(len(v.client.jellyfin.fetched), 1)
        self.assertEqual(v.client.jellyfin.images, [])

    def test_the_same_source_is_fetched_once_across_restarts(self):
        v = versions(ALT)
        for _ in range(3):
            v.get_chapters()
            list(v.get_chapter_images())
        self.assertEqual(v.client.jellyfin.fetched, [(ALT, "Chapters")])

    def test_a_changed_source_gets_its_own_chapters(self):
        v = versions(ALT)
        self.assertEqual(len(v.get_chapters()), 3)
        v.media_source = {"Id": ALT2}
        self.assertEqual([c["start"] for c in v.get_chapters()], [0, 4])
        list(v.get_chapter_images())
        self.assertEqual({i for i, _x, _t in v.client.jellyfin.images[-2:]},
                         {ALT2})
        v.media_source = {"Id": PRIMARY}
        self.assertEqual(len(v.get_chapters()), 5)
        v.media_source = {"Id": ALT}
        self.assertEqual(len(v.get_chapters()), 3)
        self.assertEqual([f[0] for f in v.client.jellyfin.fetched],
                         [ALT, ALT2], "each alternate is fetched once")

    def test_times_and_frames_stay_paired_if_the_source_changes_between(self):
        v = versions(ALT)
        times = v.get_chapters()
        v.media_source = {"Id": ALT2}           # a restart lands mid-worker
        list(v.get_chapter_images())
        self.assertEqual(len(times), 3)
        self.assertEqual({i for i, _x, _t in v.client.jellyfin.images}, {ALT})

    def test_a_late_result_for_an_old_source_is_not_used_for_the_new(self):
        v = versions(ALT)
        real = v.client.jellyfin.get_item

        def switch(item_id, fields=None):
            v.media_source = {"Id": ALT2}       # changes while ALT is in flight
            return real(item_id, fields)
        v.client.jellyfin.get_item = switch
        self.assertEqual(len(v.get_chapters()), 3, "ALT's own, as asked")
        v.client.jellyfin.get_item = real
        self.assertEqual([c["start"] for c in v.get_chapters()], [0, 4])

    def test_the_cache_is_bounded_by_the_sources(self):
        v = versions(ALT)
        for src in (ALT, ALT2, ALT, ALT2, PRIMARY):
            v.media_source = {"Id": src}
            v.get_chapters()
        self.assertEqual(set(v._alt_chapters), {ALT, ALT2})

    def test_offline_video_is_untouched(self):
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo
        self.assertIsNone(OfflineVideo.get_chapters(object.__new__(OfflineVideo)))


if __name__ == "__main__":
    unittest.main()
