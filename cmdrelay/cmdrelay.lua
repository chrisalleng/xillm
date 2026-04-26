--[[
* cmdrelay - Ashita v4 addon
*
* Polls cmd_inbox.txt every 30 frames and executes any command found.
* Runs independently of nav so it can reload nav after a crash.
*
* Inbox file: <ashita>/config/addons/nav/cmd_inbox.txt
--]]

addon.name    = 'cmdrelay'
addon.author  = 'xillm'
addon.version = '1.0'
addon.desc    = 'Polls cmd_inbox.txt to relay commands (survives nav crashes)'

require('common')

local frame = 0

local function get_install_path()
    local ok, path = pcall(function()
        return AshitaCore:GetInstallPath()
    end)
    if ok and path then return path end
    return ''
end

local function inbox_path()
    local base = get_install_path()
    if base ~= '' and base:sub(-1) ~= '/' and base:sub(-1) ~= '\\' then
        base = base .. '/'
    end
    return base .. 'config/addons/nav/cmd_inbox.txt'
end

local function poll()
    local path = inbox_path()
    local f = io.open(path, 'r')
    if not f then return end
    local cmd = f:read('*l')
    f:close()
    -- Clear before executing so a reload doesn't re-trigger.
    local wf = io.open(path, 'w')
    if wf then wf:close() end
    if cmd and cmd ~= '' then
        print(string.format('[cmdrelay] exec: %s', cmd))
        AshitaCore:GetChatManager():QueueCommand(1, cmd)
    end
end

ashita.events.register('load', 'cmdrelay_load', function()
    print('[cmdrelay] Loaded. Polling cmd_inbox.txt every 30 frames.')
end)

ashita.events.register('d3d_present', 'cmdrelay_render', function()
    frame = frame + 1
    if frame % 30 == 0 then
        poll()
    end
end)
