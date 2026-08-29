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

-- ------------------------------------------------------------ summary

print(string.format("1..%d", n))
if failed > 0 then os.exit(1) end
