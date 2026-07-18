-- Injects fake BIF trickplay data into thumbfast.lua once playback
-- starts, standing in for jellyfin_mpv_shim.trickplay.
-- Env: BIF_PATH = raw BGRA tile file (10 frames of 160x90).
local fired = false
mp.observe_property("time-pos", "number", function(_, t)
    if t and t > 0.5 and not fired then
        fired = true
        mp.commandv("script-message", "shim-trickplay-bif",
            "10", "3000", "160", "90", os.getenv("BIF_PATH"))
    end
end)
