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

function client_message_handler(event)
    if event["args"][1] == "shim-menu-enable"
    then
        if event["args"][2] == "True"
        then
            mp.log("info", "Enabled shim menu mouse events.")
            mp.add_key_binding("MOUSE_BTN0", "shim_mouse_click_handler", mouse_click_handler)
            mp.add_key_binding("MOUSE_MOVE", "shim_mouse_move_handler", mouse_handler)
        else
            mp.log("info", "Disabled shim menu mouse events.")
            mp.remove_key_binding("shim_mouse_click_handler")
            mp.remove_key_binding("shim_mouse_move_handler")
        end
    end
end

mp.add_key_binding("MOUSE_MOVE", "shim_mouse_move_handler", mouse_handler)
mp.register_event("client-message", client_message_handler)
