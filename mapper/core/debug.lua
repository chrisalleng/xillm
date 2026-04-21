--[[
* nav - core/debug.lua
*
* Debug output helpers.  All user-visible messages go through print_msg().
* Debug-only messages go through dbg() and are suppressed unless S.debug.enabled.
*
* All messages are also captured into a rolling in-memory log buffer so
* debug_state.lua can include them in the state snapshot file.
--]]

local S = require('core.state')

local D = {}

-- Rolling log buffer: last LOG_CAPACITY entries, newest last.
local LOG_CAPACITY = 30
D.log_buffer = {}

local function push_log(tag, msg)
    if #D.log_buffer >= LOG_CAPACITY then
        table.remove(D.log_buffer, 1)
    end
    table.insert(D.log_buffer, { tag = tag, msg = tostring(msg) })
end

function D.print_msg(msg)
    push_log('info', msg)
    AshitaCore:GetChatManager():QueueCommand(1, '/echo [nav] ' .. tostring(msg))
end

function D.dbg(msg)
    push_log('dbg', msg)
    if S.debug.enabled then
        AshitaCore:GetChatManager():QueueCommand(1, '/echo [nav:dbg] ' .. tostring(msg))
    end
end

-- Convenience: always print regardless of debug flag.
D.print = D.print_msg

return D
