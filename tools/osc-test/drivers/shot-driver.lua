-- Screenshot driver: pushes representative fake shim state into the OSC,
-- optionally opens an action sheet and/or the skip button, positions the
-- pointer, then screenshots the window (OSD included) and quits.
--
-- Env: SHOT_PATH (output png), SHOT_SHEET (sub|audio|settings),
--      SHOT_SKIP=1 (show the skip button), SHOT_MOUSE="x,y" (hover).
local utils = require 'mp.utils'
local out = os.getenv("SHOT_PATH") or "/tmp/osc-shot.png"
local sheet = os.getenv("SHOT_SHEET")
local fired = false

local fake_state = {
    strings = {},
    has_media = true,
    allow_screenshot = true,
    favorite = true,
    queue = {has_prev = true, has_next = true},
    subtitles = {
        {id = -1, label = "None", selected = false},
        {id = 2, label = "English (SRT)", selected = true},
        {id = 3, label = "English (Full) (ASS)", selected = false},
        {id = 4, label = "German (External)", aside = "External", selected = false},
        {id = 5, label = "Signs PGS", aside = "Transcode", selected = false},
    },
    audio = {
        {id = 1, label = "English AAC 5.1", selected = true},
        {id = 6, label = "Japanese FLAC 2.0", selected = false},
    },
    quality = {
        current = "No Transcode",
        options = {
            {id = "none", label = "No Transcode", selected = true},
            {id = "10000", label = "1080p 10 Mbps", selected = false},
            {id = "4000", label = "720p 4 Mbps", selected = false},
        },
    },
    sub_style = {
        size = {current = "Normal", options = {
            {id = "50", label = "Tiny", selected = false},
            {id = "100", label = "Normal", selected = true},
            {id = "200", label = "Huge", selected = false}}},
        position = {current = "Bottom", options = {
            {id = "bottom", label = "Bottom", selected = true},
            {id = "top", label = "Top", selected = false}}},
        color = {current = "White", options = {
            {id = "#FFFFFFFF", label = "White", selected = true},
            {id = "#FFFFEE00", label = "Yellow", selected = false}}},
    },
    profiles = {
        current = "None",
        options = {{id = "none", label = "None (Disabled)", selected = true},
                   {id = "anime4k", label = "Anime4K x4", selected = false}},
    },
    syncplay = {current = "Off", enabled = false, groups = {}},
}

mp.observe_property("time-pos", "number", function(_, t)
    if t and t > 0.8 and not fired then
        fired = true
        mp.set_property_bool("pause", true)
        mp.commandv("script-message", "shim-jf-osc-state",
                    utils.format_json(fake_state))
        if os.getenv("SHOT_SKIP") == "1" then
            mp.commandv("script-message", "shim-jf-osc-skip", "Skip Intro")
        end
        if sheet and sheet ~= "" then
            mp.add_timeout(0.3, function()
                mp.commandv("script-message", "shim-jf-osc-menu", sheet)
            end)
        end
        local mouse = os.getenv("SHOT_MOUSE")
        if mouse and mouse ~= "" then
            local mx, my = mouse:match("(%d+),(%d+)")
            mp.add_timeout(0.5, function()
                mp.commandv("mouse", tonumber(mx), tonumber(my))
            end)
        end
        mp.add_timeout(1.2, function()
            mp.commandv("screenshot-to-file", out, "window")
            mp.add_timeout(0.5, function() mp.commandv("quit") end)
        end)
    end
end)
