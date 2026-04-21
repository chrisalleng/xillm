--[[
* nav - core/zone_transitions.lua
*
* Discovered zone transition (zone line) storage and persistence.
*
* A transition record captures where a zone line was crossed:
*   from_zone / from_x / from_y  — approximate position in the source zone
*   to_zone   / to_x   / to_y   — spawn position in the destination zone
*
* Records are stored bidirectionally: crossing A→B also records B→A so that
* the return trip is known without requiring a second accidental crossing.
*
* Persisted to: <ashita>/config/addons/mapper/transitions.json
--]]

local json = require('json')
local D    = require('core.debug')

local zt = {}

-- zt.transitions[zone_id] = list of { from_zone, from_x, from_y, to_zone, to_x, to_y }
zt.transitions = {}

-- Two zone-line positions within this distance are considered the same line.
local DEDUP_DIST = 8.0

local function path(data_path)
    return data_path .. 'config/addons/mapper/transitions.json'
end

local function ensure_dir(data_path)
    pcall(function()
        if ashita and ashita.fs and ashita.fs.create_directory then
            ashita.fs.create_directory(data_path .. 'config/addons/mapper/')
        end
    end)
end

-------------------------------------------------------------------------------
-- Internal: insert one directional record, skipping duplicates.
-------------------------------------------------------------------------------
local function insert(from_zone, from_x, from_y, to_zone, to_x, to_y)
    zt.transitions[from_zone] = zt.transitions[from_zone] or {}
    for _, t in ipairs(zt.transitions[from_zone]) do
        if t.to_zone == to_zone then
            local dx = t.from_x - from_x
            local dy = t.from_y - from_y
            if dx*dx + dy*dy < DEDUP_DIST * DEDUP_DIST then
                return false  -- already known
            end
        end
    end
    table.insert(zt.transitions[from_zone], {
        from_zone = from_zone,
        from_x    = from_x,
        from_y    = from_y,
        to_zone   = to_zone,
        to_x      = to_x,
        to_y      = to_y,
    })
    return true
end

-------------------------------------------------------------------------------
-- Record a newly discovered zone transition (both directions).
-- from_zone/from_x/from_y — position in the zone we came from
-- to_zone/to_x/to_y       — spawn position in the zone we entered
-------------------------------------------------------------------------------
function zt.add(from_zone, from_x, from_y, to_zone, to_x, to_y)
    local a = insert(from_zone, from_x, from_y, to_zone, to_x, to_y)
    local b = insert(to_zone,   to_x,   to_y,   from_zone, from_x, from_y)
    if a or b then
        D.print_msg(string.format(
            'Zone line discovered: %d (%.0f,%.0f) ↔ %d (%.0f,%.0f)',
            from_zone, from_x, from_y, to_zone, to_x, to_y))
    end
end

-------------------------------------------------------------------------------
-- Return all known transitions from a zone.
-------------------------------------------------------------------------------
function zt.get(zone_id)
    return zt.transitions[zone_id] or {}
end

-------------------------------------------------------------------------------
-- Total count of known transitions across all zones.
-------------------------------------------------------------------------------
function zt.count()
    local n = 0
    for _, list in pairs(zt.transitions) do n = n + #list end
    return n
end

-------------------------------------------------------------------------------
-- Save all transitions to disk.
-------------------------------------------------------------------------------
function zt.save(data_path)
    ensure_dir(data_path)
    local all = {}
    for _, list in pairs(zt.transitions) do
        for _, t in ipairs(list) do
            table.insert(all, t)
        end
    end
    local f = io.open(path(data_path), 'w')
    if f then
        f:write(json.encode({ transitions = all }))
        f:close()
    else
        D.print_msg('Warning: could not save zone transitions.')
    end
end

-------------------------------------------------------------------------------
-- Load transitions from disk.  Safe to call when no file exists yet.
-------------------------------------------------------------------------------
function zt.load(data_path)
    zt.transitions = {}
    local f = io.open(path(data_path), 'r')
    if not f then return end
    local content = f:read('*all'); f:close()
    local ok, data = pcall(json.decode, content)
    if not ok or not data or type(data.transitions) ~= 'table' then return end
    for _, t in ipairs(data.transitions) do
        if t.from_zone and t.to_zone then
            zt.transitions[t.from_zone] = zt.transitions[t.from_zone] or {}
            table.insert(zt.transitions[t.from_zone], t)
        end
    end
    local n = zt.count()
    if n > 0 then
        D.dbg(string.format('Loaded %d zone transition records.', n))
    end
end

return zt
