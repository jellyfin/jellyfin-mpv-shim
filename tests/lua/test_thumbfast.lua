-- Unit tests for thumbfast.lua, run against a faked mpv (see fake_mp.lua).
--
-- This is the shim's compatibility layer for thumbfast-style lua OSCs: it
-- receives the TrickPlay worker's `shim-trickplay-*` messages and answers a
-- `thumb` request by compositing one frame out of the frame file. It had no
-- tests at all, which mattered once the frame file became a WINDOW of the
-- video rather than the whole of it — the bounds check added for that is the
-- thing standing between an old (mmapping) mpv and a SIGBUS, and
-- `mpv_options.mpv_scripts` loads this script under every OSC style.
--
-- It reaches mpv through the raw `client-message` EVENT rather than through
-- `register_script_message`, so everything here is driven with
-- `fake.client_message(...)` — the same route the real script has.
--
-- Prints "ok N" / "not ok N - why" (TAP-ish); the Python wrapper asserts on
-- the exit status and shows this output on failure.

local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path

local fake = require("fake_mp")
fake.install()

local SCRIPT = arg[1]
assert(SCRIPT, "usage: test_thumbfast.lua <path to thumbfast.lua>")
assert(loadfile(SCRIPT))()

-- ------------------------------------------------------------ harness

local passed, failed = 0, 0
local n = 0

local function ok(cond, name, detail)
    n = n + 1
    if cond then
        passed = passed + 1
        print(string.format("ok %d - %s", n, name))
    else
        failed = failed + 1
        print(string.format("not ok %d - %s", n, name))
        if detail then print("    # " .. tostring(detail)) end
    end
end

local function eq(got, want, name)
    ok(got == want, name,
       string.format("got %s, want %s", tostring(got), tostring(want)))
end

-- ------------------------------------------------------------ helpers

local W, H = 32, 18
local FRAME = W * H * 4

--- The last overlay-add issued, as {x, y, file, offset}, or nil.
local function overlay()
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-add" then
            found = { x = tonumber(c[3]), y = tonumber(c[4]), file = c[5],
                      offset = tonumber(c[6]) }
        end
    end
    return found
end

local function removed()
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-remove" then return true end
    end
    return false
end

--- Positions a window was requested for, in order.
local function asks()
    local out = {}
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "script-message" and c[2] == "shim-trickplay-need" then
            out[#out + 1] = tonumber(c[3])
        end
    end
    return out
end

--- The whole video in one file, which is what a shim too old to send
--- `first`/`total` produces — and what fast mode still produces.
local function whole(count)
    fake.client_message("shim-trickplay-bif", tostring(count), "10000",
                        tostring(W), tostring(H), "/tiles.bin")
end

--- Frames [first, first+count) of a video `total` frames long.
local function window(first, count, total)
    fake.client_message("shim-trickplay-bif", tostring(count), "10000",
                        tostring(W), tostring(H), "/tiles.bin",
                        tostring(first), tostring(total))
end

local function thumb(secs, x, y)
    fake.log.commands = {}
    fake.client_message("thumb", tostring(secs), tostring(x or 0),
                        tostring(y or 0))
end

-- ------------------------------------------------- the whole-video case

-- The five-argument message is what every shim before windowing sent, and
-- what fast mode still sends. It must keep behaving exactly as it did:
-- `first` defaults to 0 and `total` to `count`, so the arithmetic collapses
-- to what it always was.
whole(60)
thumb(300, 10, 20)
local ov = overlay()
ok(ov ~= nil, "no overlay for a frame that is present")
eq(ov and ov.offset, 30 * FRAME, "the legacy 5-arg message moved the offset")
eq(#asks(), 0, "a whole-video file asked for a window")

-- Past the end clamps rather than reading off it. This is the guard that
-- predates windowing, and it has to keep working: an offset past EOF is a
-- failed overlay-add on a current mpv and a SIGBUS on an mmapping one.
thumb(9999, 10, 20)
eq(overlay().offset, 59 * FRAME, "a position past the end did not clamp")

-- ------------------------------------------------------ the window case

-- Frames 40..59 of a 100-frame video: 6:40 to 9:50 of a ten-minute film.
window(40, 20, 100)

-- 7:30 is frame 45, which the FILE holds as its frame 5. Indexing the file
-- with 45 would read 45 * w * h * 4 into a 20-frame mapping.
thumb(450, 10, 20)
ov = overlay()
ok(ov ~= nil, "a frame inside the window was not drawn")
eq(ov and ov.offset, 5 * FRAME, "the offset was not rebased onto the window")
eq(#asks(), 0, "asked for a window it already had")

-- Below the window: nothing to draw, and a request for that part.
thumb(60, 10, 20)
eq(overlay(), nil, "composited a frame the file does not hold")
eq(asks()[1], 60, "no window was requested for the gap below")

-- Above the window, likewise. 800s is frame 80; the window ends at 59, and
-- the VIDEO runs to 99, so this is outside the file and inside the film.
thumb(800, 10, 20)
eq(overlay(), nil, "composited a frame past the end of the window")
eq(asks()[1], 800, "no window was requested for the gap above")

-- Clamping happens against the VIDEO first: 9999s is frame 99, which is
-- outside this window, so it must ask rather than clamp into the file.
thumb(9999, 10, 20)
eq(overlay(), nil, "clamped into the window instead of asking")
eq(#asks(), 1, "a position past the end of the video asked for nothing")

-- The stale thumbnail comes DOWN. Leaving it up labels this position with a
-- picture from somewhere else in the film, which is worse than an empty box.
thumb(450, 10, 20)                      -- draw something first
ok(overlay() ~= nil, "nothing was drawn to go stale")
thumb(60, 10, 20)
ok(removed(), "a frame from elsewhere in the film was left on screen")

-- One ask per frame index, not one per `thumb`. The OSC sends a thumb per
-- pointer position and dozens of them land on the one frame a single window
-- would answer, so keying the guard on the seconds would ask on nearly
-- every one. 300 and 305 are both frame 30, and neither has been asked for
-- yet -- an index this run has already requested would make the assertion
-- pass for the wrong reason.
fake.log.commands = {}
fake.client_message("thumb", "300", "10", "20")
fake.client_message("thumb", "305", "10", "20")
eq(#asks(), 1, "the request repeats for every pointer position in one frame")

-- ...but a different frame index is a different question. Without this the
-- assertion above would also pass if nothing ever asked at all.
fake.log.commands = {}
fake.client_message("thumb", "200", "10", "20")   -- frame 20, still missing
eq(#asks(), 1, "moving to another missing frame asked nothing")

-- A window landing re-arms the guard, so a position that is STILL missing is
-- asked for again -- the pointer moves on while a fetch is in flight, and
-- without this the bubble would sit empty with nothing pending.
window(40, 20, 100)
thumb(60, 10, 20)
eq(asks()[1], 60, "a landing window did not re-arm the request")

-- ------------------------------------------------------- other branches

-- The chapter fallback indexes by chapter start, and carries no window: the
-- bounds check must not fire on it (there is nothing to be outside of).
fake.client_message("shim-trickplay-chapters", tostring(W), tostring(H),
                    "/tiles.bin", "0,120,480")
thumb(300, 10, 20)
ov = overlay()
ok(ov ~= nil, "the chapter fallback drew nothing")
eq(ov and ov.offset, 1 * FRAME, "chapter tiles index by start time")
eq(#asks(), 0, "the chapter fallback asked for a window")

-- Clearing takes the overlay down and stops answering.
window(40, 20, 100)
thumb(450, 10, 20)
ok(overlay() ~= nil, "nothing was shown to clear")
fake.log.commands = {}
fake.client_message("shim-trickplay-clear")
ok(removed(), "clear left the overlay pointing at bytes about to be unlinked")
thumb(450, 10, 20)
eq(overlay(), nil, "kept compositing after a clear")
eq(#asks(), 0, "asked for a window while disabled")

-- --------------------------------------------- the thumbfast-info payload

-- Third-party OSCs read this blob and nothing else to decide whether
-- previews exist and how big to reserve for them, so its shape is a
-- published interface. `scale_factor` is the one field carried purely for
-- them: width/height already arrive pre-multiplied (as upstream thumbfast
-- sends them), so nothing here needs it -- but an OSC that divides by it to
-- recover a logical size gets an arithmetic error on nil rather than a
-- thumbnail. docs/mpv-backends.md section 12.

--- The last thumbfast-info payload, as the table format_json was handed.
local function info()
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "script-message" and c[2] == "thumbfast-info" then
            found = c[3]
        end
    end
    return found
end

fake.log.commands = {}
window(0, 20, 100)
ok(info() ~= nil, "publishing a window announced no thumbfast-info")
eq(info().scale_factor, 1, "scale_factor missing from thumbfast-info")
eq(info().width, W, "announced the wrong width")
eq(info().height, H, "announced the wrong height")
eq(info().disabled, false, "announced itself disabled with a window loaded")
eq(info().available, true, "announced itself unavailable with a window loaded")

fake.log.commands = {}
fake.client_message("shim-trickplay-clear")
eq(info().scale_factor, 1, "scale_factor dropped on the clear announcement")
eq(info().disabled, true, "stayed enabled after a clear")

-- ------------------------------------------------------- display scaling

-- The frames are decoded at the server's preview width, a size in PHYSICAL
-- pixels, so on a 2x display they come out half the size of everything the
-- OSC drew around them. overlay-add scales on the GPU when handed a display
-- size (dw/dh, mpv 0.38+), so the frame file stays the size it was. The
-- message's last argument is `thumbnail_scale`, or "auto" for the display's.
local OLD_MPV = os.getenv("JMS_TEST_NO_OVERLAY_SCALE") ~= nil

--- The last overlay-add, raw: (overlay-add, id, x, y, file, offset, fmt,
--- w, h, stride[, dw, dh]).
local function overlay_cmd()
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-add" then found = c end
    end
    return found
end

local function num(c, i)
    return c and c[i] and tonumber(c[i])
end

local function scaled_window(scale)
    fake.log.commands = {}
    fake.client_message("shim-trickplay-bif", "20", "10000", tostring(W),
                        tostring(H), "/tiles.bin", "40", "100", scale)
end

-- At 1x nothing changes: no display size is passed at all, so an mpv older
-- than 0.38 -- which rejects the extra arguments -- still draws.
scaled_window("auto")
thumb(450, 10, 20)
eq(overlay_cmd() and #overlay_cmd(), 10, "a 1x preview passed a display size")

scaled_window("2")
if OLD_MPV then
    eq(info().width, W, "announced a width an old mpv cannot draw")
    eq(info().scale_factor, 1, "announced a scale an old mpv cannot apply")
else
    eq(info().width, 2 * W, "thumbfast-info announced the source width")
    eq(info().height, 2 * H, "thumbfast-info announced the source height")
    eq(info().scale_factor, 2, "thumbfast-info did not carry the scale")
end
thumb(450, 10, 20)
local cmd = overlay_cmd()
eq(num(cmd, 8), W, "the frame was not read at its own size")
eq(num(cmd, 6), 5 * FRAME, "scaling moved the frame's offset")
if OLD_MPV then
    eq(cmd and #cmd, 10, "passed a display size to an mpv that rejects one")
else
    eq(num(cmd, 11), 2 * W, "the frame was not drawn at 2x")
    eq(num(cmd, 12), 2 * H, "the frame height was not drawn at 2x")
end

-- "auto" follows the display, and a display change re-announces the size
-- (moving the window to another monitor changes it mid-video).
scaled_window("auto")
fake.log.commands = {}
fake.observe("display-hidpi-scale", 2)
eq(info() and info().width, OLD_MPV and W or 2 * W,
   "a display-scale change did not re-announce the preview size")
thumb(450, 10, 20)
eq(num(overlay_cmd(), 11), (not OLD_MPV) and 2 * W or nil,
   "auto did not follow display-hidpi-scale")
fake.observe("display-hidpi-scale", 1)

-- mpv's own OSC reserves a box and we centre the frame in it -- at the size
-- it is DRAWN, or a 2x frame hangs off the bottom right of its box.
scaled_window("2")
fake.observe("user-data/osc/draw-preview",
             { x = 100, y = 50, w = 200, h = 100, ["hover-sec"] = 450 })
cmd = overlay_cmd()
local dw, dh = OLD_MPV and W or 2 * W, OLD_MPV and H or 2 * H
eq(num(cmd, 3), 100 + math.floor((200 - dw) / 2),
   "the frame was centred at its source width, not the width drawn")
eq(num(cmd, 4), 50 + math.floor((100 - dh) / 2),
   "the frame was centred at its source height, not the height drawn")
fake.observe("user-data/osc/draw-preview", nil)

-- The chapter fallback carries the same argument, after its timestamps.
fake.log.commands = {}
fake.client_message("shim-trickplay-chapters", tostring(W), tostring(H),
                    "/tiles.bin", "0,120,480", "2")
eq(info().width, OLD_MPV and W or 2 * W, "the chapter fallback ignored the scale")

-- ------------------------------------------------ shim-thumbfast-render

-- An OSC that draws the frame itself asks the upstream way -- empty x and y,
-- its script name fourth -- and is answered with OUR message, not upstream's
-- `thumbfast-render`. Upstream's payload names a file holding one frame; ours
-- holds a window of them, so an OSC following upstream's contract would draw
-- the window's first frame for every position. The offset is the difference.

--- The last shim-thumbfast-render payload sent to `script`.
local function rendered(script)
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "script-message-to" and c[2] == script
                and c[3] == "shim-thumbfast-render" then
            found = c[4]
        end
    end
    return found
end

scaled_window("2")
fake.log.commands = {}
fake.client_message("thumb", "450", "", "", "someosc")
local r = rendered("someosc")
ok(r ~= nil, "an OSC that renders for itself was sent nothing")
eq(overlay_cmd(), nil, "drew the frame for an OSC that renders for itself")
eq(r and r.available, true, "the frame was announced as unavailable")
eq(r and r.file, "/tiles.bin", "the payload does not name the frame file")
eq(r and r.offset, 5 * FRAME, "the payload does not say where the frame is")
eq(r and r.frame_width, W, "the payload does not give the source width")
eq(r and r.frame_height, H, "the payload does not give the source height")
eq(r and r.width, dw, "the payload does not give the width to draw")
eq(r and r.height, dh, "the payload does not give the height to draw")
eq(r and r.scale_factor, OLD_MPV and 1 or 2, "the payload's scale is wrong")

-- Once per frame, like the positioned path's dedup.
fake.log.commands = {}
fake.client_message("thumb", "452", "", "", "someosc")
eq(rendered("someosc"), nil, "re-sent a frame the OSC already has")

-- Outside the window: the OSC is told to take its frame down, and the window
-- is still asked for -- the OSC owns the drawing, not the fetching.
fake.log.commands = {}
fake.client_message("thumb", "60", "", "", "someosc")
r = rendered("someosc")
eq(r and r.available, false,
   "left an OSC showing a frame from elsewhere in the film")
eq(asks()[1], 60, "an OSC that renders for itself never gets its window")

-- Upstream's empty coordinates with no script to answer: nothing to do.
fake.log.commands = {}
fake.client_message("thumb", "450", "", "")
eq(overlay_cmd(), nil, "drew a frame with no position and nobody to tell")

-- ------------------------------------------------------------ summary

print(string.format("1..%d", n))
if failed > 0 then os.exit(1) end
