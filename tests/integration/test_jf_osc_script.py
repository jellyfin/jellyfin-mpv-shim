"""Tier 2: OSC lua scripts load and run cleanly in a REAL mpv binary.

Drives the external ``mpv`` executable directly (subprocess), NOT libmpv:
the point is to catch lua syntax/runtime errors in the shipped OSC scripts
on a real player, and an external process sidesteps the libmpv-teardown
races that kept these scripts out of test_realmpv_smoke.

For the jellyfin OSC this also exercises the Python-facing protocol from
the lua side: a state blob is pushed over ``shim-jf-osc-state`` and each
action sheet is opened via ``shim-jf-osc-menu``, with the OSC forced
visible so the render path (layout, vector icons, sheet drawing) actually
executes. Any lua error mpv reports fails the test.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

RESOURCE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "jellyfin_mpv_shim"
)

FAKE_STATE = {
    "strings": {},
    "has_media": True,
    "allow_screenshot": True,
    "subtitles": [
        {"id": -1, "label": "None", "selected": False},
        {"id": 2, "label": "English (SRT)", "selected": True},
        {"id": 4, "label": "German", "aside": "External", "selected": False},
        {"id": 5, "label": "Signs PGS", "aside": "Transcode", "selected": False},
    ],
    "audio": [
        {"id": 1, "label": "English AAC 5.1", "selected": True},
        {"id": 6, "label": "Japanese FLAC 2.0", "selected": False},
    ],
    "quality": {
        "current": "No Transcode",
        "options": [
            {"id": "none", "label": "No Transcode", "selected": True},
            {"id": "4000", "label": "720p 4 Mbps", "selected": False},
        ],
    },
    "sub_style": {
        "size": {"current": "Normal", "options": [
            {"id": "100", "label": "Normal", "selected": True}]},
        "position": {"current": "Bottom", "options": [
            {"id": "bottom", "label": "Bottom", "selected": True}]},
        "color": {"current": "White", "options": [
            {"id": "#FFFFFFFF", "label": "White", "selected": True}]},
    },
    "profiles": {"current": "None", "options": [
        {"id": "none", "label": "None (Disabled)", "selected": True}]},
    "syncplay": {"current": "Off", "enabled": False, "groups": []},
}

# Driver script: waits for playback, pushes the fake state, walks through
# every action sheet, then quits. Written to a temp file at test time.
DRIVER_LUA = """
local utils = require 'mp.utils'
local fired = false
local state_json = os.getenv("JMS_TEST_STATE")
mp.observe_property("time-pos", "number", function(_, t)
    if t and t > 0.3 and not fired then
        fired = true
        if state_json and state_json ~= "" then
            mp.commandv("script-message", "shim-jf-osc-state", state_json)
        end
        local sheets = {"sub", "audio", "settings"}
        for i, sheet in ipairs(sheets) do
            mp.add_timeout(0.2 * i, function()
                mp.commandv("script-message", "shim-jf-osc-menu", sheet)
            end)
        end
        mp.add_timeout(1.0, function() mp.commandv("quit") end)
    end
end)
"""


def _run_mpv_with_script(script_name, push_state):
    driver = tempfile.NamedTemporaryFile(
        "w", suffix=".lua", prefix="jms-osc-driver-", delete=False
    )
    try:
        driver.write(DRIVER_LUA)
        driver.close()
        env = dict(os.environ)
        env["JMS_TEST_STATE"] = json.dumps(FAKE_STATE) if push_state else ""
        proc = subprocess.run(
            [
                "mpv",
                "--no-config",
                "--osc=no",
                "--ao=null",
                "--script-opts=osc-visibility=always",
                "--script=%s" % os.path.join(RESOURCE_DIR, script_name),
                "--script=%s" % driver.name,
                "--end=8",
                "av://lavfi:testsrc2=duration=10:size=640x360:rate=30",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc
    finally:
        os.unlink(driver.name)


@unittest.skipUnless(
    h.HAVE_MPV_BIN and (h.HAVE_DISPLAY or h.HAVE_XVFB),
    "OSC script smoke needs the mpv binary + a display (xvfb)",
)
class OscScriptSmokeTest(unittest.TestCase):
    def _assert_clean(self, proc, script_name):
        output = proc.stdout + proc.stderr
        for line in output.splitlines():
            lowered = line.lower()
            if "lua error" in lowered or "error running lua" in lowered:
                self.fail("%s produced a lua error:\n%s" % (script_name, output))
        self.assertIn(
            proc.returncode, (0,),
            "%s: mpv exited %s\n%s" % (script_name, proc.returncode, output),
        )

    def test_jf_osc_loads_and_renders_sheets(self):
        proc = _run_mpv_with_script("trickplay-jf-osc.lua", push_state=True)
        self._assert_clean(proc, "trickplay-jf-osc.lua")

    def test_jf_osc_without_shim_state(self):
        # No state push: menus must fall back to mpv's own track list
        # without erroring (plain-mpv graceful degradation).
        proc = _run_mpv_with_script("trickplay-jf-osc.lua", push_state=False)
        self._assert_clean(proc, "trickplay-jf-osc.lua")

    def test_stock_trickplay_osc_still_loads(self):
        proc = _run_mpv_with_script("trickplay-osc.lua", push_state=False)
        self._assert_clean(proc, "trickplay-osc.lua")


if __name__ == "__main__":
    unittest.main()
