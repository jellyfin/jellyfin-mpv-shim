import json
import unittest

# Importing osc_bridge must be side-effect safe: it pulls in menu.py for the
# shared option lists but must not import player.py (which needs libmpv).
from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.osc_bridge import OscBridge


class FakeJellyfinApi:
    def __init__(self):
        self.favorites = []

    def favorite(self, item_id, option=True):
        self.favorites.append((item_id, option))


class FakeClient:
    def __init__(self):
        self.jellyfin = FakeJellyfinApi()


class FakeParent:
    has_prev = False
    has_next = True


class FakeVideo:
    """Stand-in for media.Video covering what the bridge reads."""

    def __init__(self):
        self.aid = 1
        self.sid = 4
        self.client = FakeClient()
        self.item = {"Id": "item1", "UserData": {"IsFavorite": False}}
        self.parent = FakeParent()
        self.secondary_sid = None
        self.subtitle_seq = {3: 1}      # embedded: jellyfin idx 3 -> mpv sid 1
        self.subtitle_url = {4: "https://server/sub.srt"}  # external
        self.subtitle_enc = {5}         # burn-in (requires transcode)
        self.trs = None
        self.media_source = {
            "MediaStreams": [
                {"Type": "Video", "Index": 0},
                {"Type": "Audio", "Index": 1, "DisplayTitle": "English AAC",
                 "Language": "eng"},
                {"Type": "Audio", "Index": 2, "DisplayTitle": "Japanese",
                 "Language": "jpn"},
                {"Type": "Subtitle", "Index": 3, "Language": "eng",
                 "Codec": "subrip"},
                {"Type": "Subtitle", "Index": 4, "Language": "eng",
                 "Codec": "srt"},
                {"Type": "Subtitle", "Index": 5, "Language": "eng",
                 "Codec": "pgssub"},
            ]
        }

    def get_transcode_bitrate(self):
        return "none"

    def set_trs_override(self, bitrate, force):
        self.trs = (bitrate, force)


class FakeSyncplay:
    current_group = None
    client = None

    def is_enabled(self):
        return False


class FakePlayerManager:
    def __init__(self, video=None):
        self._video = video
        self._osc_script_loaded = True
        self.menu = None
        self.syncplay = FakeSyncplay()
        self.tasks = []
        self.messages = []
        self.timeline_handles = 0
        self.restarted = 0

    def get_video(self):
        return self._video

    def put_task(self, func, *args):
        self.tasks.append((func, args))

    def run_tasks(self):
        tasks, self.tasks = self.tasks, []
        for func, args in tasks:
            func(*args)

    def script_message(self, command, *args):
        self.messages.append((command, args))

    def timeline_handle(self):
        self.timeline_handles += 1

    def set_streams(self, aid, sid):
        self.streams = (aid, sid)

    def set_secondary_subtitle(self, sub_uid):
        self.secondary = sub_uid

    def restart_playback(self):
        self.restarted += 1

    def update_subtitle_visuals(self):
        pass

    def screenshot(self):
        pass

    def unwatched_quit(self):
        pass

    def play_next(self):
        pass

    def play_prev(self):
        pass

    def skip_intro(self):
        pass


class StateBuildTests(unittest.TestCase):
    def _state(self, pm):
        # The HUD pulls this blob directly on every repaint.
        return OscBridge(pm).build_state()

    def test_no_media(self):
        state = self._state(FakePlayerManager())
        self.assertFalse(state["has_media"])
        self.assertIn("strings", state)

    def test_streams_and_asides(self):
        state = self._state(FakePlayerManager(FakeVideo()))
        self.assertTrue(state["has_media"])

        subs = state["subtitles"]
        # Off entry first, not selected because sid=4 is active.
        self.assertEqual(subs[0]["id"], -1)
        self.assertFalse(subs[0]["selected"])
        by_id = {s["id"]: s for s in subs}
        # All three delivery methods are listed -- this is the point of
        # the new UI (the old OSC only saw embedded tracks).
        self.assertIn(3, by_id)
        self.assertNotIn("aside", by_id[3])
        self.assertEqual(by_id[4]["aside"], "External")
        self.assertTrue(by_id[4]["selected"])
        self.assertEqual(by_id[5]["aside"], "Transcode")

        audio = state["audio"]
        self.assertEqual([a["id"] for a in audio], [1, 2])
        self.assertTrue(audio[0]["selected"])
        self.assertFalse(audio[1]["selected"])

    def test_secondary_subtitle_streams(self):
        # sid=4 (the external track) is the primary. The secondary picker
        # offers None + only mpv-renderable tracks that aren't the primary:
        # embedded 3 stays, the primary 4 and the burn-in 5 drop out.
        state = self._state(FakePlayerManager(FakeVideo()))
        sub2 = state["secondary_subtitles"]
        self.assertEqual(sub2[0]["id"], -1)
        self.assertTrue(sub2[0]["selected"])   # nothing chosen yet
        ids = [s["id"] for s in sub2]
        self.assertEqual(ids, [-1, 3])
        self.assertNotIn(4, ids)               # can't dup the primary
        self.assertNotIn(5, ids)               # burn-in can't be secondary

    def test_secondary_subtitle_marks_selection(self):
        video = FakeVideo()
        video.secondary_sid = 3
        state = self._state(FakePlayerManager(video))
        by_id = {s["id"]: s for s in state["secondary_subtitles"]}
        self.assertTrue(by_id[3]["selected"])
        self.assertFalse(by_id[-1]["selected"])

    def test_quality_current(self):
        state = self._state(FakePlayerManager(FakeVideo()))
        quality = state["quality"]
        self.assertEqual(quality["current"], "No Transcode")
        selected = [o for o in quality["options"] if o["selected"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], "none")

    def test_favorite_in_state(self):
        video = FakeVideo()
        video.item["UserData"]["IsFavorite"] = True
        state = self._state(FakePlayerManager(video))
        self.assertTrue(state["favorite"])

    def test_sub_style_groups(self):
        state = self._state(FakePlayerManager(FakeVideo()))
        style = state["sub_style"]
        for key in ("size", "position", "color"):
            self.assertIn("current", style[key])
            self.assertTrue(style[key]["options"])

class ActionDispatchTests(unittest.TestCase):
    def test_set_sub_queues_set_streams(self):
        pm = FakePlayerManager(FakeVideo())
        bridge = OscBridge(pm)
        bridge.handle_action(["set-sub", "5"])
        funcs = [t[0] for t in pm.tasks]
        self.assertIn(pm.set_streams, funcs)
        self.assertEqual(pm.tasks[0][1], (None, 5))
        self.assertEqual(pm.timeline_handles, 1)

    def test_set_secondary_sub_queues_secondary(self):
        pm = FakePlayerManager(FakeVideo())
        OscBridge(pm).handle_action(["set-secondary-sub", "3"])
        self.assertEqual(pm.tasks[0][0], pm.set_secondary_subtitle)
        self.assertEqual(pm.tasks[0][1], (3,))

    def test_set_audio(self):
        pm = FakePlayerManager(FakeVideo())
        OscBridge(pm).handle_action(["set-audio", "2"])
        self.assertEqual(pm.tasks[0][1], (2, None))

    def test_set_quality_restarts(self):
        video = FakeVideo()
        pm = FakePlayerManager(video)
        OscBridge(pm).handle_action(["set-quality", "max"])
        pm.run_tasks()
        self.assertEqual(video.trs, (None, True))
        self.assertEqual(pm.restarted, 1)

    def test_set_quality_bitrate(self):
        video = FakeVideo()
        pm = FakePlayerManager(video)
        OscBridge(pm).handle_action(["set-quality", "4000"])
        pm.run_tasks()
        self.assertEqual(video.trs, (4000, True))

    def test_sub_style_saved(self):
        pm = FakePlayerManager(FakeVideo())
        old_size = settings.subtitle_size
        old_save = settings.save
        settings.save = lambda: None
        try:
            OscBridge(pm).handle_action(["set-sub-size", "125"])
            pm.run_tasks()
            self.assertEqual(settings.subtitle_size, 125)
        finally:
            settings.save = old_save
            settings.subtitle_size = old_size

    def test_queue_state(self):
        pm = FakePlayerManager(FakeVideo())
        state = OscBridge(pm).build_state()
        self.assertFalse(state["queue"]["has_prev"])
        self.assertTrue(state["queue"]["has_next"])

    def test_queue_nav_and_skip_actions(self):
        pm = FakePlayerManager(FakeVideo())
        bridge = OscBridge(pm)
        bridge.handle_action(["next-item"])
        self.assertEqual(pm.tasks[0][0], pm.play_next)
        pm.tasks.clear()
        bridge.handle_action(["prev-item"])
        self.assertEqual(pm.tasks[0][0], pm.play_prev)
        pm.tasks.clear()
        bridge.handle_action(["skip-segment"])
        self.assertEqual(pm.tasks[0][0], pm.skip_intro)

    def test_toggle_favorite(self):
        video = FakeVideo()
        pm = FakePlayerManager(video)
        OscBridge(pm).handle_action(["toggle-favorite"])
        pm.run_tasks()
        self.assertEqual(video.client.jellyfin.favorites, [("item1", True)])
        self.assertTrue(video.item["UserData"]["IsFavorite"])

    def test_unknown_verb_is_harmless(self):
        pm = FakePlayerManager(FakeVideo())
        OscBridge(pm).handle_action(["frobnicate", "1"])
        self.assertEqual(pm.tasks, [])

    def test_empty_args(self):
        pm = FakePlayerManager(FakeVideo())
        OscBridge(pm).handle_action([])
        self.assertEqual(pm.tasks, [])


if __name__ == "__main__":
    unittest.main()
