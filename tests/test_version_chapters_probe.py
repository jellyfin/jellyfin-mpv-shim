"""tools/jf_disposable/version_chapters_probe.py -- its pure parts.

Nothing here starts a server or ffmpeg or reads the real cgroup: the specification, the ffmpeg
command, the chapter metadata and the checks are exercised on fixtures, and the scope guard and
the disposable-only paths are pinned. The real run needs a throw-away server (see the README).
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools", "jf_disposable")
sys.path.insert(0, TOOLS)
import version_chapters_probe as P  # noqa: E402

IMG, PLAIN = P.LIBRARIES
VERSIONS = {v[0]: v for lib in P.LIBRARIES for v in lib[4]}


def good(ext, chapters, **over):
    img = P.expect_image(ext, chapters)
    rows = [{"name": t, "start": s, "tag": "tag" if img else None,
             **({"status": 200, "type": "image/jpeg", "bytes": 5000} if img else {})}
            for s, t in chapters]
    return {"chapters": rows, **over}


class Spec(unittest.TestCase):
    def test_the_counts_cover_many_few_one_none_and_no_extraction(self):
        counts = {lb: len(ch) for lb, ch, _s in IMG[4]}
        self.assertEqual(counts, {"Theatrical": 5, "Directors Cut": 3, "Single": 1, "Bare": 0})
        self.assertTrue(IMG[3])
        self.assertFalse(PLAIN[3])
        self.assertTrue(all(len(ch) > 1 for _l, ch, _s in PLAIN[4]))

    def test_chapter_starts_are_ordered_and_inside_the_video(self):
        for lib in P.LIBRARIES:
            for _label, chapters, _src in lib[4]:
                starts = [s for s, _t in chapters]
                self.assertEqual(starts, sorted(set(starts)))
                self.assertTrue(all(0 <= s < P.SECONDS for s in starts))

    def test_versions_look_different_so_pictures_can_be_told_apart(self):
        for lib in P.LIBRARIES:
            sources = [src for _l, _c, src in lib[4]]
            self.assertEqual(len(sources), len(set(sources)), lib[0])

    def test_images_are_expected_only_with_extraction_and_more_than_one_chapter(self):
        many, one = IMG[4][0][1], IMG[4][2][1]
        self.assertTrue(P.expect_image(True, many))
        self.assertFalse(P.expect_image(True, one))
        self.assertFalse(P.expect_image(True, []))
        self.assertFalse(P.expect_image(False, many))


class Media(unittest.TestCase):
    def test_ffmetadata_has_one_block_per_chapter_ending_at_the_next_start(self):
        text = P.ffmetadata([(0, "A"), (10, "B")])
        self.assertTrue(text.startswith(";FFMETADATA1\n"))
        self.assertEqual(text.count("[CHAPTER]"), 2)
        self.assertIn("START=0\nEND=10000\ntitle=A", text)
        self.assertIn("START=10000\nEND=%d\ntitle=B" % (P.SECONDS * 1000), text)

    def test_ffmpeg_writes_chapters_only_when_given_and_strips_them_otherwise(self):
        with_meta = P.ffmpeg_command("o.mkv", "testsrc", "m.ffmeta")
        self.assertEqual(with_meta[with_meta.index("-map_chapters") + 1], "2")
        bare = P.ffmpeg_command("o.mkv", "testsrc")
        self.assertEqual(bare[bare.index("-map_chapters") + 1], "-1")
        self.assertEqual(bare[-1], "o.mkv")

    def test_files_are_named_so_jellyfin_merges_them_and_live_under_the_work_dir(self):
        paths = [P.media_path("/w", lib, folder, film, label) for lib, folder, film, _e, vs in P.LIBRARIES
                 for label, _c, _s in vs]
        for path in paths:
            self.assertTrue(path.startswith("/w/media_versions/"), path)
        self.assertEqual(os.path.dirname(paths[0]), os.path.dirname(paths[3]))
        self.assertIn("Test Film (2001) - Theatrical.mkv", paths[0])

    def test_existing_files_are_not_regenerated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as w:
            for lib, folder, film, _e, vs in P.LIBRARIES:
                for label, _c, _s in vs:
                    path = P.media_path(w, lib, folder, film, label)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    open(path, "w").close()
            with mock.patch.object(P.subprocess, "run") as run:
                P.make_media(w)
            run.assert_not_called()

    def test_each_missing_file_gets_one_ffmpeg_call_and_no_temp_file_is_left(self):
        import tempfile
        made = []
        with tempfile.TemporaryDirectory() as w, \
                mock.patch.object(P.subprocess, "run", lambda cmd, check: made.append(cmd)):
            P.make_media(w)
        self.assertEqual(len(made), 6)
        metas = [c[c.index("-i", c.index("-i") + 2) + 1] for c in made if "-map_metadata" in c]
        self.assertEqual(len(metas), 5, "the chapterless version has no metadata file")
        self.assertFalse([m for m in metas if os.path.exists(m)])


class ServerChecks(unittest.TestCase):
    def test_correct_facts_pass_for_every_version(self):
        for lib in P.LIBRARIES:
            for _label, chapters, _s in lib[4]:
                self.assertEqual(P.check_server(lib[3], chapters, good(lib[3], chapters)), [])

    def test_wrong_count_name_start_or_tag_is_reported(self):
        ext, chapters = IMG[3], IMG[4][0][1]
        g = good(ext, chapters)
        self.assertIn("4 chapters", P.check_server(ext, chapters, {"chapters": g["chapters"][:4]})[0])
        for key, val, word in (("name", "x", "named"), ("start", 99, "starts at"), ("tag", None, "no image tag")):
            g = good(ext, chapters)
            g["chapters"][1][key] = val
            self.assertIn(word, "\n".join(P.check_server(ext, chapters, g)), key)

    def test_an_unexpected_tag_is_reported_for_a_lone_chapter_and_for_no_extraction(self):
        for ext, chapters in ((True, IMG[4][2][1]), (False, PLAIN[4][0][1])):
            g = good(ext, chapters)
            g["chapters"][0]["tag"] = "oops"
            self.assertIn("expected none", "\n".join(P.check_server(ext, chapters, g)))

    def test_an_image_that_is_not_served_is_reported(self):
        ext, chapters = IMG[3], IMG[4][1][1]
        for key, val in (("status", 404), ("type", "text/html"), ("bytes", 3)):
            g = good(ext, chapters)
            g["chapters"][2][key] = val
            self.assertIn("image:", "\n".join(P.check_server(ext, chapters, g)), key)

    def test_an_image_served_under_another_versions_id_is_reported(self):
        ext, chapters = IMG[3], IMG[4][0][1]
        g = good(ext, chapters)
        g["chapters"][4]["foreign"] = 200
        self.assertIn("another version", "\n".join(P.check_server(ext, chapters, g)))
        g["chapters"][4]["foreign"] = 404
        self.assertEqual(P.check_server(ext, chapters, g), [])

    def test_a_version_with_no_chapters_must_report_none(self):
        self.assertEqual(P.check_server(True, [], {"chapters": []}), [])
        self.assertTrue(P.check_server(True, [], good(True, IMG[4][0][1])))

    def test_identical_pictures_in_two_versions_are_reported(self):
        self.assertEqual(P.check_distinct({"A": ["h1", "h2"], "B": ["h3"]}), [])
        self.assertEqual(len(P.check_distinct({"A": ["h1"], "B": ["h1"]})), 1)
        self.assertEqual(P.check_distinct({"A": ["h1", "h1"]}), [], "one version may repeat itself")


class ConsumerChecks(unittest.TestCase):
    def ok(self, ext, chapters):
        want = P.expected(ext, chapters)
        img = [w for w in want if w["image"]]
        return ({"times": [float(w["start"]) for w in img], "n_images": len(img)},
                [(w["name"], w["start"], w["image"]) for w in want])

    def test_matching_consumers_pass_for_every_version(self):
        for lib in P.LIBRARIES:
            for _label, chapters, _s in lib[4]:
                player, scenes = self.ok(lib[3], chapters)
                self.assertEqual(P.check_consumers(lib[3], chapters, player, scenes), [])

    def test_a_player_reading_another_versions_chapters_is_reported(self):
        ext, chapters = IMG[3], IMG[4][1][1]
        player, scenes = self.ok(ext, chapters)
        player["times"] = [0.0, 6.0, 12.0, 18.0, 24.0]        # the theatrical cut's
        self.assertIn("player previews", "\n".join(P.check_consumers(ext, chapters, player, scenes)))
        player, scenes = self.ok(ext, chapters)
        player["n_images"] = 5
        self.assertIn("images", "\n".join(P.check_consumers(ext, chapters, player, scenes)))

    def test_a_scenes_source_reading_another_versions_chapters_is_reported(self):
        ext, chapters = IMG[3], IMG[4][1][1]
        player, _scenes = self.ok(ext, chapters)
        self.assertIn("Scenes row", "\n".join(P.check_consumers(ext, chapters, player, [("T1", 0, True)])))


class Safety(unittest.TestCase):
    def test_a_real_run_is_refused_outside_a_limited_scope(self):
        with mock.patch.object(P, "in_limited_scope", lambda: False), \
                mock.patch.object(P, "make_media") as mk, mock.patch.object(P, "run") as run, \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main([]), 2)
        mk.assert_not_called()
        run.assert_not_called()
        self.assertIn("limited_run.sh", err.getvalue())

    def test_inside_a_scope_it_runs_and_its_status_is_the_runs(self):
        for rc in (0, 1):
            with mock.patch.object(P, "in_limited_scope", lambda: True), \
                    mock.patch.object(P, "make_media"), mock.patch.object(P, "run", lambda w: rc):
                self.assertEqual(P.main([]), rc)

    def test_media_only_starts_no_server(self):
        with mock.patch.object(P, "make_media") as mk, mock.patch.object(P, "run") as run:
            self.assertEqual(P.main(["--make-media-only"]), 0)
        mk.assert_called_once()
        run.assert_not_called()

    def test_scope_detection_reads_the_cgroup_name(self):
        for cg, want in (("/sys/fs/cgroup/x/jh-limited-9.scope", True), ("/sys/fs/cgroup/x/app.service", False),
                         (None, False)):
            with mock.patch("ab_metrics.cgroup_dir", lambda *a, **k: cg):
                self.assertEqual(P.in_limited_scope(), want, cg)

    def test_only_the_disposable_server_and_work_dir_are_used(self):
        with open(os.path.join(TOOLS, "version_chapters_probe.py"), encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ("auth.server", "~/.config", "expanduser", "jellyfin_mpv_shim.conf", "settings.json"):
            self.assertNotIn(forbidden, src)
        self.assertIn("J.teardown(", src)
        self.assertTrue(P.media_dir("/w").startswith("/w/"))


if __name__ == "__main__":
    unittest.main()
