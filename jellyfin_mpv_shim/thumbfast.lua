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
img_last_k = nil
img_is_shown = false
img_enabled = false
img_is_bif = false
img_chapters = {}
img_overlay_id = 46
-- `thumbnail_scale` off the last publish, or nil for "auto" -- which here is
-- the display's own factor, since that is what an mpv OSC is drawn at.
img_scale = nil
display_scale = 1
-- Whether overlay-add takes a display size (dw/dh, mpv 0.38+). nil until
-- first needed: an older mpv rejects the extra arguments outright, so a
-- scaled frame there would be no frame at all.
overlay_scaling = nil
-- What the last shim-thumbfast-render told each script, so a frame is sent
-- once rather than once per pointer position.
img_rendered = {}

local function parse_scale(arg)
    local k = tonumber(arg)
    if k and k > 0 then return k end
    return nil
end

-- The factor frames are drawn at: 1 unless something asks for more AND this
-- mpv can draw it.
local function draw_scale()
    local k = img_scale or display_scale or 1
    if k == 1 then return 1 end
    if overlay_scaling == nil then
        overlay_scaling = false
        for _, c in ipairs(mp.get_property_native("command-list") or {}) do
            if c.name == "overlay-add" then
                for _, a in ipairs(c.args or {}) do
                    if a.name == "dw" then overlay_scaling = true end
                end
            end
        end
    end
    return overlay_scaling and k or 1
end

local function drawn_size()
    local k = draw_scale()
    return math.floor(img_width * k + 0.5), math.floor(img_height * k + 0.5), k
end

function send_thumbfast_message()
    local w, h, k = drawn_size()
    local json, err = utils.format_json({
        width = w,
        height = h,
        -- The factor the frame is drawn at, with width/height already
        -- multiplied by it -- upstream's meaning. Sent at 1 as well, because
        -- an OSC that divides by it errors on nil (docs/mpv-backends.md
        -- section 12).
        scale_factor = k,
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

local function send_render(script, payload)
    local json, err = utils.format_json(payload)
    if err ~= nil then
        mp.log("error", "Failed to format JSON: " .. err)
        return
    end
    mp.commandv("script-message-to", script, "shim-thumbfast-render", json)
end

function client_message_handler(event)
    local event_name = event["args"][1]
    if event_name == "shim-trickplay-clear"
    then
        mp.log("info", "Clearing trickplay.")
        img_enabled = false
        img_rendered = {}
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
        img_scale = parse_scale(event["args"][9])
        img_asked = nil
        img_last_frame = -1
        img_rendered = {}
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
        img_scale = parse_scale(event["args"][6])

        img_last_frame = -1
        img_rendered = {}
        img_enabled = true
        img_is_bif = false
        send_thumbfast_message()
    elseif event_name == "thumb"
    then
        local offset_seconds = tonumber(event["args"][2])
        local x = tonumber(event["args"][3])
        local y = tonumber(event["args"][4])
        -- Empty x and y plus a script name is upstream's way of asking to
        -- draw the frame yourself. It is answered with shim-thumbfast-render
        -- rather than upstream's thumbfast-render, whose payload has no
        -- offset into a file of several frames (docs/mpv-backends.md
        -- section 12).
        local script = event["args"][5]
        local render_to = (x == nil or y == nil) and script ~= nil
                          and script ~= "" and script or nil
        if offset_seconds == nil or ((x == nil or y == nil) and not render_to) then
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
                    if render_to then
                        -- The OSC owns its overlay, so it is TOLD.
                        if img_rendered[render_to] ~= -1 then
                            img_rendered[render_to] = -1
                            send_render(render_to, { available = false })
                        end
                    elseif img_is_shown then
                        mp.commandv("overlay-remove", img_overlay_id)
                        img_is_shown = false
                        img_last_frame = -1
                    end
                    return
                end
            end
            local offset = frame * img_width * img_height * 4
            local w, h, k = drawn_size()
            if render_to then
                local key = frame .. "@" .. k
                if img_rendered[render_to] ~= key then
                    img_rendered[render_to] = key
                    send_render(render_to, {
                        available = true,
                        file = img_file,
                        offset = offset,
                        frame_width = img_width,
                        frame_height = img_height,
                        width = w,
                        height = h,
                        scale_factor = k,
                        overlay_id = img_overlay_id,
                    })
                end
                return
            end
            -- Re-add only when the frame or position actually changed:
            -- overlay-add re-reads and re-uploads the whole BGRA tile, and
            -- doing that on every render tick makes the preview flicker.
            -- (img_last_frame was previously never updated, so the dedup
            -- check always passed.)
            if frame ~= img_last_frame or x ~= img_last_x or y ~= img_last_y
                    or k ~= img_last_k then
                img_is_shown = true
                img_last_frame = frame
                img_last_x = x
                img_last_y = y
                img_last_k = k
                dbg(("overlay-add frame=%d @ %d,%d x%s"):format(frame, x, y, k))
                if k ~= 1 then
                    mp.commandv("overlay-add", img_overlay_id, x, y, img_file, offset, "bgra", img_width, img_height, img_width * 4, w, h)
                else
                    -- No display size at 1x, so an mpv without dw/dh draws.
                    mp.commandv("overlay-add", img_overlay_id, x, y, img_file, offset, "bgra", img_width, img_height, img_width * 4)
                end
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
-- size may differ", so our own frame is centred in it, at the size it is
-- drawn, rather than stretched to it.
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
    local fw, fh = drawn_size()
    if img_width > 0 and w > 0 then
        x = x + math.floor((w - fw) / 2)
    end
    if img_height > 0 and h > 0 then
        y = y + math.floor((h - fh) / 2)
    end
    -- Straight into the handler the fork's message lands in, so there is one
    -- implementation of "draw the frame for this timestamp here" and the two
    -- front doors cannot drift.
    client_message_handler({args = {"thumb", tostring(secs),
                                    tostring(x), tostring(y)}})
end

mp.observe_property("user-data/osc/draw-preview", "native", on_draw_preview)

-- "auto" follows this, and it changes mid-video when the window moves to
-- another monitor, so the size an OSC reserves is re-announced with it.
mp.observe_property("display-hidpi-scale", "number", function(_, v)
    display_scale = tonumber(v) or 1
    if display_scale <= 0 then display_scale = 1 end
    if img_enabled then send_thumbfast_message() end
end)
