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
