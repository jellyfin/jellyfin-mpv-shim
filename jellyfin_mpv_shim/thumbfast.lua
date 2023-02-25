local utils = require 'mp.utils'

img_count = 0
img_multiplier = 0
img_width = 0
img_height = 0
img_file = ""
img_last_frame = -1
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
            if frame ~= img_last_frame then
                if img_is_bif and frame >= img_count then
                    frame = img_count -1
                end
                local offset = frame * img_width * img_height * 4
                img_is_shown = true
                mp.commandv("overlay-add", img_overlay_id, x, y, img_file, offset, "bgra", img_width, img_height, img_width * 4)
            end
        end
    elseif event_name == "clear"
    then
        if img_is_shown
        then
            mp.commandv("overlay-remove", img_overlay_id)
            img_is_shown = false
        end
    end
end
mp.register_event("client-message", client_message_handler)
