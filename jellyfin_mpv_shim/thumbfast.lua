local utils = require 'mp.utils'

-- Set JMS_JF_OSC_DEBUG=1 to log every thumb/clear decision.
local debug_events = os.getenv("JMS_JF_OSC_DEBUG") ~= nil
local function dbg(text)
    if debug_events then
        mp.msg.info(text)
    end
end

img_count = 0
-- The file is a WINDOW of the video, not all of it: frames [img_first,
-- img_first + img_count) of img_total. Decoded BGRA balloons -- a two-hour
-- film is hundreds of megabytes of it -- so the shim fetches the part you
-- are looking at and swaps files as you move. Indexing the file with a
-- frame number the video's length justifies reads past its end: a failed
-- overlay-add on a current mpv, and a SIGBUS on one old enough to still
-- mmap the file. So the bounds check below is not cosmetic. A shim old
-- enough not to send these sends the whole video, which the defaults
-- describe.
img_first = 0
img_total = 0
img_asked = nil    -- video-relative frame a window was last requested for
img_multiplier = 0
img_width = 0
img_height = 0
img_file = ""
img_last_frame = -1
img_last_x = nil
img_last_y = nil
img_is_shown = false
img_enabled = false
img_is_bif = false
img_chapters = {}
img_overlay_id = 46

function send_thumbfast_message()
    local json, err = utils.format_json({
        width = img_width,
        height = img_height,
        -- Always 1: width/height are already what a consumer should draw,
        -- the same as upstream thumbfast sends. Present only because an OSC
        -- that divides by it to recover a logical size errors on nil.
        scale_factor = 1,
        disabled = not img_enabled,
        available = img_enabled,
        overlay_id = img_overlay_id
    })
    if err ~= nil
    then
        mp.log("error", "Failed to format JSON: " .. err)
    else
        mp.commandv("script-message", "thumbfast-info", json)
    end
end

function client_message_handler(event)
    local event_name = event["args"][1]
    if event_name == "shim-trickplay-clear"
    then
        mp.log("info", "Clearing trickplay.")
        img_enabled = false
        if img_is_shown
        then
            mp.commandv("overlay-remove", 46)
            img_is_shown = false
        end
        send_thumbfast_message()
    elseif event_name == "shim-trickplay-bif"
    then
        mp.log("info", "Received BIF data.")
        img_count = tonumber(event["args"][2])
        img_multiplier = tonumber(event["args"][3])
        img_width = tonumber(event["args"][4])
        img_height = tonumber(event["args"][5])
        img_file = event["args"][6]
        img_first = tonumber(event["args"][7]) or 0
        img_total = tonumber(event["args"][8]) or img_count
        img_asked = nil
        img_last_frame = -1
        img_enabled = true
        img_is_bif = true
        send_thumbfast_message()
    elseif event_name == "shim-trickplay-chapters"
    then
        mp.log("info", "Received chapter metadata.")
        img_width = tonumber(event["args"][2])
        img_height = tonumber(event["args"][3])
        img_file = event["args"][4]
        
        img_chapters = {}
        for timestamp in string.gmatch(event["args"][5], '([^,]+)') do
            table.insert(img_chapters, tonumber(timestamp))
        end

        img_last_frame = -1
        img_enabled = true
        img_is_bif = false
        send_thumbfast_message()
    elseif event_name == "thumb"
    then
        local offset_seconds = tonumber(event["args"][2])
        local x = tonumber(event["args"][3])
        local y = tonumber(event["args"][4])
        if offset_seconds == nil or x == nil or y == nil then
            return
        end

        if img_enabled then
            local frame = 0;
            if img_is_bif then
                frame = math.floor(offset_seconds / (img_multiplier / 1000))
            else
                for i = #img_chapters, 1, -1 do
                    if img_chapters[i] <= offset_seconds then
                        frame = i - 1
                        break
                    end
                end
            end
            should_render_preview = true
            if img_is_bif then
                -- Clamp against the VIDEO, then move into the file.
                if frame >= img_total then frame = img_total - 1 end
                if frame < 0 then frame = 0 end
                local want = frame
                frame = frame - img_first
                if frame < 0 or frame >= img_count then
                    -- Not loaded. Ask for it and take the stale thumbnail
                    -- down: leaving it up labels this position with a
                    -- picture from somewhere else in the film, which is
                    -- worse than an empty box.
                    --
                    -- Keyed on the FRAME, not on offset_seconds: the OSC
                    -- sends a `thumb` per pointer position, and dozens of
                    -- them land on the one frame a single window would
                    -- answer. img_asked is cleared when a window arrives.
                    if img_asked ~= want then
                        img_asked = want
                        mp.commandv("script-message", "shim-trickplay-need",
                                    tostring(offset_seconds))
                    end
                    if img_is_shown then
                        mp.commandv("overlay-remove", img_overlay_id)
                        img_is_shown = false
                        img_last_frame = -1
                    end
                    return
                end
            end
            -- Re-add only when the frame or position actually changed:
            -- overlay-add re-reads and re-uploads the whole BGRA tile, and
            -- doing that on every render tick makes the preview flicker.
            -- (img_last_frame was previously never updated, so the dedup
            -- check always passed.)
            if frame ~= img_last_frame or x ~= img_last_x or y ~= img_last_y then
                local offset = frame * img_width * img_height * 4
                img_is_shown = true
                img_last_frame = frame
                img_last_x = x
                img_last_y = y
                dbg(("overlay-add frame=%d @ %d,%d"):format(frame, x, y))
                mp.commandv("overlay-add", img_overlay_id, x, y, img_file, offset, "bgra", img_width, img_height, img_width * 4)
            else
                dbg(("thumb dedup frame=%d @ %d,%d"):format(frame, x, y))
            end
        end
    elseif event_name == "clear"
    then
        if img_is_shown
        then
            dbg("overlay-remove (clear)")
            mp.commandv("overlay-remove", img_overlay_id)
            img_is_shown = false
            img_last_frame = -1
            img_last_x = nil
            img_last_y = nil
        end
    end
end
mp.register_event("client-message", client_message_handler)

-- mpv's OSC Preview API (0.41+, DOCS/man/osc.rst).
--
-- The stock OSC publishes `user-data/osc/draw-preview` -- a table of
-- {x, y, w, h, hover-sec, ass} -- when the pointer is over the seekbar, and
-- sets it to nil to say "take it down". That is exactly the hook
-- trickplay-osc.lua was forked to work around, so on a new enough mpv the
-- player loads mpv's OWN OSC and we answer this instead.
--
-- Both paths live side by side on purpose and cannot both be live: the
-- property only ever appears when the stock OSC is running, and the
-- client-messages only ever arrive from our fork, which is loaded only when
-- the stock one cannot do this. Neither needs to know about the other.
--
-- The coordinates are OSD pixels, which is what `overlay-add` wants -- the
-- fork had to divide by the virtual scale factor to get here. `w`/`h` are
-- the box the OSC reserved, and the docs say "the actual backing thumbnail
-- size may differ", so our own frame is centred in it rather than stretched
-- to it.
--
-- `ass` (a border to draw around the preview) is deliberately ignored: it is
-- sized to the OSC's box rather than to our frame, so drawing it would put a
-- rectangle around something the wrong size. A preview without a border is
-- the lesser of the two.
local function on_draw_preview(_, req)
    dbg("draw-preview -> " .. type(req))
    if type(req) ~= "table" then
        if img_is_shown then
            mp.commandv("overlay-remove", img_overlay_id)
            img_is_shown = false
            img_last_frame = -1
            img_last_x = nil
            img_last_y = nil
        end
        return
    end
    local secs = tonumber(req["hover-sec"])
    local x, y = tonumber(req.x), tonumber(req.y)
    if secs == nil or x == nil or y == nil then
        return
    end
    local w, h = tonumber(req.w) or 0, tonumber(req.h) or 0
    if img_width > 0 and w > 0 then
        x = x + math.floor((w - img_width) / 2)
    end
    if img_height > 0 and h > 0 then
        y = y + math.floor((h - img_height) / 2)
    end
    -- Straight into the handler the fork's message lands in, so there is one
    -- implementation of "draw the frame for this timestamp here" and the two
    -- front doors cannot drift.
    client_message_handler({args = {"thumb", tostring(secs),
                                    tostring(x), tostring(y)}})
end

mp.observe_property("user-data/osc/draw-preview", "native", on_draw_preview)
