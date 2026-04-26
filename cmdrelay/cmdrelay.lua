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
addon.version = '1.1'
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
    -- Consume every command in the inbox per poll, not just the first.
    -- Producers (e.g. agent_core/deploy.sh) often need to issue several
    -- commands together — `/addon reload nav` + `/addon load combat`
    -- + `/addon reload combat` for instance — and the previous one-line
    -- behaviour silently dropped all but the first.
    local path = inbox_path()
    local f = io.open(path, 'r')
    if not f then return end
    local body = f:read('*a') or ''
    f:close()
    -- Clear immediately so a reload-during-execute doesn't re-trigger.
    local wf = io.open(path, 'w')
    if wf then wf:close() end
    if body == '' then return end
    for line in body:gmatch('[^\r\n]+') do
        if line ~= '' then
            print(string.format('[cmdrelay] exec: %s', line))
            AshitaCore:GetChatManager():QueueCommand(1, line)
        end
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
