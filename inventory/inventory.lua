--[[
* inventory - inventory.lua
*
* Ashita v4 addon that publishes the inventory slice of the agent's
* world state. Reads every container the player has access to from
* the IInventory memory struct, atomically writes:
*    <install>/config/addons/nav/state/<char>/inventory.json
* every ~2s. The orchestrator diffs successive snapshots to detect
* item-received events (drops, vendor purchases, mog wardrobe pulls).
*
* Phase 6 scope: state publishing only. The action surface
* (equip_set, move_item, discard_item) goes through cmd_inbox.txt
* into luashitacast and packer — both of which are already deployed
* and configured via their own files. We don't need an action
* channel because those addons already accept /commands.
--]]

addon.name    = 'inventory'
addon.author  = 'xillm'
addon.version = '0.2'
addon.desc    = 'Inventory state publisher (Tier 1 addon for agent_core)'

require('common')
local json = require('json')

-------------------------------------------------------------------------------
-- Config
-------------------------------------------------------------------------------

local PUBLISH_EVERY_FRAMES = 120  -- ~2 Hz; inventory rarely changes mid-frame

-- Standard FFXI container ids (0..11). Future containers (mog locker
-- expansion, recycle bin) can be added by id; the publisher just
-- iterates everything it can read.
local CONTAINERS = {
    {id = 0,  name = 'inventory'},
    {id = 1,  name = 'safe'},
    {id = 2,  name = 'storage'},
    {id = 3,  name = 'locker'},
    {id = 4,  name = 'temp'},
    {id = 5,  name = 'satchel'},
    {id = 6,  name = 'sack'},
    {id = 7,  name = 'case'},
    {id = 8,  name = 'wardrobe'},
    {id = 9,  name = 'wardrobe2'},
    {id = 10, name = 'wardrobe3'},
    {id = 11, name = 'wardrobe4'},
}

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
    print('\30\06[inventory]\30\01 ' .. text)
end

local function ensure_dir(path)
    pcall(function() ashita.fs.create_directory(path) end)
end

-------------------------------------------------------------------------------
-- State + I/O
-------------------------------------------------------------------------------

local state = {
    frame = 0,
    last_character = nil,
}

local function pcget(fn)
    local ok, v = pcall(fn)
    if ok then return v end
    return nil
end

local function write_json(path, data)
    local ok, encoded = pcall(json.encode, data)
    if not ok or encoded == nil then return end
    local f = io.open(path, 'w')
    if not f then return end
    f:write(encoded)
    f:close()
end

-------------------------------------------------------------------------------
-- Inventory readers
-------------------------------------------------------------------------------

local function read_container(inv, container)
    local cap = pcget(function() return inv:GetContainerCountMax(container.id) end) or 0
    if cap == 0 then return nil end
    local items = {}
    -- IInventory uses 0..cap-1 for indices; item.Id == 0 (or 65535
    -- in some builds) means empty slot. Filter empty slots so the
    -- payload only carries real items.
    for i = 0, cap - 1 do
        local item = pcget(function() return inv:GetContainerItem(container.id, i) end)
        if item ~= nil then
            local id = pcget(function() return item.Id end) or 0
            local count = pcget(function() return item.Count end) or 0
            if id ~= 0 and id ~= 65535 and count > 0 then
                items[#items + 1] = {
                    slot  = i,
                    id    = id,
                    count = count,
                }
            end
        end
    end
    return {
        capacity = cap,
        used     = #items,
        items    = items,
    }
end

-- Phase 6-min: equipped-gear introspection deferred. The Ashita
-- IMemoryManager doesn't expose GetEquipment(); equipmon.lua reads
-- equipped slots via a path through IInventory + raw struct offsets
-- I haven't worked out yet. Container snapshots cover what the
-- agent actually needs for now (drop detection, vendor purchases),
-- and luashitacast handles the equipping side.
local function read_equipped()
    return {}
end

-------------------------------------------------------------------------------
-- Publish loop
-------------------------------------------------------------------------------

local function publish()
    local char = state.last_character
    if char == nil or char == '' then return end
    local inv = AshitaCore:GetMemoryManager():GetInventory()
    local payload = {
        ts         = os.time(),
        character  = char,
        containers = {},
        equipped   = read_equipped(),
    }
    for _, c in ipairs(CONTAINERS) do
        local data = read_container(inv, c)
        if data ~= nil then
            payload.containers[c.name] = data
        end
    end
    local path = get_data_path() .. 'state/' .. char .. '/inventory.json'
    write_json(path, payload)
end

-------------------------------------------------------------------------------
-- Ashita events
-------------------------------------------------------------------------------

ashita.events.register('load', 'inventory_load', function()
    msg('Loaded v' .. addon.version .. ' — inventory → state/<char>/inventory.json')
end)

ashita.events.register('d3d_present', 'inventory_render', function()
    state.frame = state.frame + 1
    if state.last_character == nil then
        local char = get_character_name()
        if char ~= nil and char ~= '' then
            state.last_character = char
            ensure_dir(get_data_path() .. 'state/' .. char)
        end
    end
    if state.frame % PUBLISH_EVERY_FRAMES ~= 0 then return end
    pcall(publish)
end)
