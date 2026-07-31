last_idx = -1
function mouse_handler()
    local x, y = mp.get_mouse_pos()
    local hy = mp.get_property_native("osd-height")
    if hy == nil 
    then
        return
    end
    idx = math.floor((y * 1000 / hy - 33) / 55)
    if idx ~= last_idx
    then
        last_idx = idx
        mp.commandv("script-message", "shim-menu-select", idx)
    end
end

function mouse_click_handler()
    last_idx = -1  -- Force refresh.
    mouse_handler()
    mp.commandv("script-message", "shim-menu-click")
end

function mouse_back_handler()
    mp.commandv("script-message", "shim-menu-back")
end

function client_message_handler(event)
    if event["args"][1] == "shim-menu-enable"
    then
        if event["args"][2] == "True"
        then
            mp.log("info", "Enabled shim menu mouse events.")
            mp.add_key_binding("MOUSE_BTN0", "shim_mouse_click_handler", mouse_click_handler)
            mp.add_key_binding("MOUSE_MOVE", "shim_mouse_move_handler", mouse_handler)
            -- The thumb button leaves the current submenu, the same as the
            -- Back row the pointer gets by hovering above the list. Bound
            -- and unbound with the rest of them, so mpv's own MBTN_BACK
            -- (playlist-prev) is untouched whenever the menu is closed.
            mp.add_key_binding("MBTN_BACK", "shim_mouse_back_handler", mouse_back_handler)
        else
            mp.log("info", "Disabled shim menu mouse events.")
            mp.remove_key_binding("shim_mouse_click_handler")
            mp.remove_key_binding("shim_mouse_move_handler")
            mp.remove_key_binding("shim_mouse_back_handler")
        end
    end
end

-- No binding is installed at load time: the handler fires a script-message on
-- every mouse move, which is pure noise (and wakes the Python side) whenever
-- the OSD menu is closed -- which is almost always. The bindings are added on
-- "shim-menu-enable True" and removed again on False.
mp.register_event("client-message", client_message_handler)
