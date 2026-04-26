--[[
* chat - chat.lua
*
* Ashita v4 addon that publishes the chat slice of the agent's
* world state. Hooks `text_in` for every incoming chat line,
* maintains a rolling buffer of the last 200 lines, atomically
* writes:
*    <install>/config/addons/nav/state/<char>/chat.json
* every ~1s, and appends a `chat_received` event line to
* events.jsonl whenever a new line arrives so agent_core can
* dispatch the LLM on /tells, party-chat mentions, etc.
*
* Phase 5 scope: state publishing + event emission only. Outbound
* chat (LLM-issued replies) goes through cmd_inbox.txt → cmdrelay,
* same path as every other agent → addon command.
--]]

addon.name    = 'chat'
addon.author  = 'xillm'
addon.version = '0.1'
addon.desc    = 'Chat publisher (Tier 1 addon for the agent_core orchestrator)'

require('common')
local json = require('json')

-------------------------------------------------------------------------------
-- Config
-------------------------------------------------------------------------------

local PUBLISH_EVERY_FRAMES = 60   -- ~1 Hz; chat is bursty, no need for 10 Hz
local BUFFER_MAX = 200            -- rolling line cap
local EVENT_PATH_REL = 'events.jsonl'

local function get_data_path()
    local ok, p = pcall(function() return AshitaCore:GetInstallPath() end)
    if ok and p then
        if p:sub(-1) ~= '/' and p:sub(-1) ~= '\\' then p = p .. '/' end
        return p .. 'config/addons/nav/'
    end
    return ''
end

local function get_character_name()
    local ok, p = pcall(function()
        return AshitaCore:GetMemoryManager():GetParty():GetMemberName(0)
    end)
    if ok and p ~= nil and p ~= '' then return p end
    return nil
end

local function msg(text)
    print('\30\06[chat]\30\01 ' .. text)
end

local function ensure_dir(path)
    pcall(function() ashita.fs.create_directory(path) end)
end

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------

local state = {
    frame = 0,
    last_character = nil,
    -- Rolling list of {ts, mode, text}. Append on new line, drop the
    -- head when over BUFFER_MAX. Cheaper than re-allocating each tick.
    lines = {},
    dirty = false,        -- true when we have new lines since last publish
    last_publish_frame = 0,
}

-------------------------------------------------------------------------------
-- I/O helpers
-------------------------------------------------------------------------------

-- Strip Ashita's chat-control bytes (color codes etc.) so the JSON
-- payload is plain readable text. The bytes are 0x1E + a 1-byte arg
-- (Ashita-internal) or 0x1F + a 1-byte arg (Final Fantasy XI palette).
local function clean(s)
    if s == nil then return '' end
    local out = s
    -- Drop the two known control-byte pairs.
    out = out:gsub('\30.', '')
    out = out:gsub('\31.', '')
    return out
end

local function write_json(path, data)
    local ok, encoded = pcall(json.encode, data)
    if not ok or encoded == nil then return end
    local f = io.open(path, 'w')
    if not f then return end
    f:write(encoded)
    f:close()
end

local function append_event(record)
    local ok, encoded = pcall(json.encode, record)
    if not ok or encoded == nil then return end
    local path = get_data_path() .. EVENT_PATH_REL
    local f = io.open(path, 'a')
    if not f then return end
    f:write(encoded .. '\n')
    f:close()
end

-------------------------------------------------------------------------------
-- Publish
-------------------------------------------------------------------------------

local function publish(force)
    if not state.dirty and not force then return end
    local char = state.last_character
    if char == nil or char == '' then return end
    local payload = {
        ts        = os.time(),
        character = char,
        lines     = state.lines,
    }
    write_json(get_data_path() .. 'state/' .. char .. '/chat.json', payload)
    state.dirty = false
    state.last_publish_frame = state.frame
end

-- Called for every incoming text_in event. Records the line and emits
-- a chat_received event to events.jsonl so agent_core can react in
-- real time (e.g., LLM auto-reply on /tells).
local function on_text_in(e)
    local mode = e.mode or 0
    local text = clean(e.message_modified or '')
    if text == '' then return end
    local rec = { ts = os.time(), mode = mode, text = text }
    state.lines[#state.lines + 1] = rec
    if #state.lines > BUFFER_MAX then
        -- Drop oldest. We do this in chunks to avoid churning the table
        -- on every line; keep a small buffer of slack above BUFFER_MAX.
        if #state.lines >= BUFFER_MAX + 32 then
            local trimmed = {}
            for i = #state.lines - BUFFER_MAX + 1, #state.lines do
                trimmed[#trimmed + 1] = state.lines[i]
            end
            state.lines = trimmed
        end
    end
    state.dirty = true
    -- Emit a structured event so the orchestrator can react without
    -- waiting on the next state publish.
    local char = state.last_character
    if char ~= nil and char ~= '' then
        append_event({
            ts        = os.time(),
            character = char,
            source    = 'chat',
            type      = 'chat_received',
            mode      = mode,
            text      = text,
        })
    end
end

-------------------------------------------------------------------------------
-- Ashita events
-------------------------------------------------------------------------------

ashita.events.register('load', 'chat_load', function()
    msg('Loaded v' .. addon.version .. ' — chat → state/<char>/chat.json + events.jsonl')
end)

ashita.events.register('text_in', 'chat_text_in', function(e)
    pcall(on_text_in, e)
end)

ashita.events.register('d3d_present', 'chat_render', function()
    state.frame = state.frame + 1
    -- Resolve the character name lazily; the player may not be logged in
    -- when the addon loads.
    if state.last_character == nil then
        local char = get_character_name()
        if char ~= nil and char ~= '' then
            state.last_character = char
            ensure_dir(get_data_path() .. 'state/' .. char)
        end
    end
    if state.frame % PUBLISH_EVERY_FRAMES == 0 then
        pcall(publish, false)
    end
end)
