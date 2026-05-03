--[[
* inventory - inventory.lua
*
* Ashita v4 addon that publishes the inventory slice of the agent's
* world state. Reads every container the player has access to from
* the IInventory memory struct, atomically writes:
*    <install>/config/xillm/state/<char>/inventory.json
* every ~2s. The orchestrator diffs successive snapshots to detect
* item-received events (drops, vendor purchases, mog wardrobe pulls).
*
* Phase 6 scope: state publishing only. The action surface
* (equip_set, move_item, discard_item) goes through cmd_inbox.txt
* into luashitacast and packer - both of which are already deployed
* and configured via their own files. We don't need an action
* channel because those addons already accept /commands.
--]]

addon.name    = 'inventory'
addon.author  = 'xillm'
addon.version = '0.4'
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

-- Equipment slot indices per the FFXI client (matches equipmon.lua).
-- Order matters: SLOT_ORDER[i] is the slot id for the i-th /equip arg.
local EQUIP_SLOTS = {
    [0]  = 'main',  [1]  = 'sub',   [2]  = 'range', [3]  = 'ammo',
    [4]  = 'head',  [5]  = 'body',  [6]  = 'hands', [7]  = 'legs',
    [8]  = 'feet',  [9]  = 'neck',  [10] = 'waist', [11] = 'ear1',
    [12] = 'ear2',  [13] = 'ring1', [14] = 'ring2', [15] = 'back',
}

-- FFXI item slot bitmask -> slot-name list. The bitmask uses one bit
-- per character-slot (16 total, ear1/ear2/ring1/ring2 are separate),
-- so a symmetric item like a ring sets two bits. We collapse the
-- ear1/ear2 and ring1/ring2 pairs to a single 'ear'/'ring' entry so
-- the LLM doesn't have to reason about left-vs-right.
local SINGLE_BITS = {
    {bit = 0x0001, name = 'main'},
    {bit = 0x0002, name = 'sub'},
    {bit = 0x0004, name = 'range'},
    {bit = 0x0008, name = 'ammo'},
    {bit = 0x0010, name = 'head'},
    {bit = 0x0020, name = 'body'},
    {bit = 0x0040, name = 'hands'},
    {bit = 0x0080, name = 'legs'},
    {bit = 0x0100, name = 'feet'},
    {bit = 0x0200, name = 'neck'},
    {bit = 0x0400, name = 'waist'},
    {bit = 0x0800, name = 'ear'},   -- ear1
    {bit = 0x1000, name = 'ear'},   -- ear2 (collapsed)
    {bit = 0x2000, name = 'ring'},  -- ring1
    {bit = 0x4000, name = 'ring'},  -- ring2 (collapsed)
    {bit = 0x8000, name = 'back'},
}

local function get_data_path()
    local ok, p = pcall(function() return AshitaCore:GetInstallPath() end)
    if ok and p then
        if p:sub(-1) ~= '/' and p:sub(-1) ~= '\\' then p = p .. '/' end
        return p .. 'config/xillm/'
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

-- Resolve item metadata via Ashita's bundled resource manager.
-- Returns name + flattened slot list + level requirement, or nil on
-- unknown id. Cached because a 9-item inventory triggers 9 lookups
-- per publish and the resource hash is small enough to memoize fully.
local _resource_cache = {}
local function resolve_item(id)
    if id == nil or id == 0 or id == 65535 then return nil end
    local cached = _resource_cache[id]
    if cached ~= nil then return cached end
    local res = pcget(function()
        return AshitaCore:GetResourceManager():GetItemById(id)
    end)
    if res == nil then
        _resource_cache[id] = false  -- negative cache; don't re-query
        return nil
    end
    local name = pcget(function() return res.Name[1] end) or '?'
    local slot_mask = pcget(function() return res.Slots end) or 0
    local lvl_req = pcget(function() return res.Level end) or 0
    local slots = {}
    local seen = {}
    for _, sb in ipairs(SINGLE_BITS) do
        if (slot_mask % (sb.bit * 2)) >= sb.bit and not seen[sb.name] then
            slots[#slots + 1] = sb.name
            seen[sb.name] = true
        end
    end
    local meta = {name = name, slots = slots, lvl_req = lvl_req}
    _resource_cache[id] = meta
    return meta
end

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
                local entry = {
                    slot  = i,
                    id    = id,
                    count = count,
                }
                local meta = resolve_item(id)
                if meta ~= nil then
                    entry.name           = meta.name
                    entry.equip_slots    = meta.slots
                    entry.lvl_req        = meta.lvl_req
                end
                items[#items + 1] = entry
            end
        end
    end
    return {
        capacity = cap,
        used     = #items,
        items    = items,
    }
end

-- Read currently-equipped items via the equipmon pattern: GetEquippedItem
-- returns a small struct whose Index field packs (container << 8 | slot).
-- We fetch the underlying item from that container/slot to get the real
-- Id/Count, then enrich with resource-manager metadata.
local function read_equipped(inv)
    local equipped = {}
    for slot_id = 0, 15 do
        local slot_name = EQUIP_SLOTS[slot_id]
        local eq = pcget(function() return inv:GetEquippedItem(slot_id) end)
        local entry = nil
        if eq ~= nil then
            local idx = pcget(function() return eq.Index end) or 0
            if idx ~= 0 then
                local container = math.floor(idx / 256)
                local cslot = idx % 256
                local item = pcget(function()
                    return inv:GetContainerItem(container, cslot)
                end)
                if item ~= nil then
                    local id = pcget(function() return item.Id end) or 0
                    if id ~= 0 and id ~= 65535 then
                        entry = {
                            container = container,
                            slot      = cslot,
                            id        = id,
                        }
                        local meta = resolve_item(id)
                        if meta ~= nil then
                            entry.name = meta.name
                        end
                    end
                end
            end
        end
        equipped[slot_name] = entry  -- nil on empty slot; json.encode -> null
    end
    return equipped
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
        equipped   = read_equipped(inv),
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
    msg('Loaded v' .. addon.version .. ' - inventory -> state/<char>/inventory.json')
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
