--[[
* mapper - entities.lua
* Entity scanning, cross-session identity matching, and deduplication.
* Tracks NPCs (type 2) and interactive objects/doors (type 5).
--]]

local entities = {}

-- Per-zone persistent records: entities.zone_records[zone_id] = { entity_record, ... }
entities.zone_records = {}
-- Session-local index by ServerId for O(1) in-session updates
entities.session_index = {}

local ENTITY_TYPES = { [2] = true, [5] = true }
local MATCH_RADIUS = 10.0   -- yalms; within this distance = same entity cross-session
local TIE_RADIUS   = 0.1    -- yalms; equidistant tie → keep both records

-------------------------------------------------------------------------------
-- Reset session state (but keep loaded zone records intact across resets
-- triggered by zone change, which calls load() after reset()).
-------------------------------------------------------------------------------
function entities.reset(zone_id)
    entities.session_index = {}
    if zone_id then
        entities.zone_records[zone_id] = entities.zone_records[zone_id] or {}
    end
end

-------------------------------------------------------------------------------
-- Build a stable UID from zone, entity name, and the nearest graph node id.
-- The nearest node acts as a spatial anchor that survives minor coord drift.
-------------------------------------------------------------------------------
local function make_uid(zone_id, name, nearest_node_id)
    return string.format('zone_%d:%s:%d', zone_id, name, nearest_node_id or 0)
end

-------------------------------------------------------------------------------
-- Find the best cross-session match for a candidate in zone_id's record list.
-- Returns the matching record, or nil if none found within MATCH_RADIUS.
-------------------------------------------------------------------------------
local function find_match(zone_id, candidate)
    local records = entities.zone_records[zone_id]
    if records == nil then return nil end

    local best_rec, best_dist = nil, math.huge
    for _, rec in ipairs(records) do
        if rec.name == candidate.name and rec.type == candidate.type then
            local dx = rec.x - candidate.x
            local dy = rec.y - candidate.y   -- north/south axis
            local dist = math.sqrt(dx * dx + dy * dy)
            if dist <= MATCH_RADIUS then
                if dist < best_dist - TIE_RADIUS then
                    best_rec  = rec
                    best_dist = dist
                elseif dist <= best_dist + TIE_RADIUS then
                    -- True tie: return nil so both records coexist
                    best_rec = nil
                end
            end
        end
    end
    return best_rec
end

-------------------------------------------------------------------------------
-- Create a new entity record and insert it into the zone's record list.
-------------------------------------------------------------------------------
local function create_record(zone_id, candidate, nearest_node_id)
    local uid = make_uid(zone_id, candidate.name, nearest_node_id)
    local rec = {
        uid          = uid,
        name         = candidate.name,
        type         = candidate.type,
        server_id    = candidate.server_id,
        x            = candidate.x,
        y            = candidate.y,
        z            = candidate.z,
        nearest_node = nearest_node_id,
        zone_id      = zone_id,
    }
    entities.zone_records[zone_id] = entities.zone_records[zone_id] or {}
    table.insert(entities.zone_records[zone_id], rec)
    return rec
end

-------------------------------------------------------------------------------
-- Scan all entity slots and update/create records.
-- graph_ref is the graph module (used to find nearest_node for new records).
-------------------------------------------------------------------------------
function entities.scan(zone_id, graph_ref)
    if zone_id == nil or zone_id == 0 then return end

    for index = 0, 2303 do
        local e = GetEntity(index)
        if e ~= nil and e.ActorPointer ~= 0 then
            local name = e.Name
            if name ~= nil and name ~= '' and ENTITY_TYPES[e.Type] then
                local candidate = {
                    name      = name,
                    type      = e.Type,
                    server_id = e.ServerId,
                    x         = e.Movement.LocalPosition.X,
                    y         = e.Movement.LocalPosition.Y,
                    z         = e.Movement.LocalPosition.Z,
                }

                local existing = entities.session_index[e.ServerId]
                if existing then
                    -- Fast path: same session, just update position
                    existing.x = candidate.x
                    existing.y = candidate.y
                    existing.z = candidate.z
                else
                    -- Cross-session match attempt
                    local match = find_match(zone_id, candidate)
                    if match then
                        match.server_id = candidate.server_id
                        match.x = candidate.x
                        match.y = candidate.y
                        match.z = candidate.z
                        entities.session_index[candidate.server_id] = match
                    else
                        -- New entity
                        local nearest_node = nil
                        if graph_ref then
                            nearest_node = graph_ref.nearest_node(candidate.x, candidate.z)
                        end
                        local rec = create_record(zone_id, candidate, nearest_node)
                        entities.session_index[candidate.server_id] = rec
                    end
                end
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Serialize all entity records for a zone into a plain table for JSON export
-------------------------------------------------------------------------------
function entities.serialize(zone_id)
    local out = {}
    local records = entities.zone_records[zone_id]
    if records == nil then return out end
    for _, rec in ipairs(records) do
        table.insert(out, {
            uid          = rec.uid,
            name         = rec.name,
            type         = rec.type,
            server_id    = rec.server_id,
            x            = rec.x,
            y            = rec.y,
            z            = rec.z,
            nearest_node = rec.nearest_node,
            zone_id      = rec.zone_id,
        })
    end
    return out
end

-------------------------------------------------------------------------------
-- Load entity records from a deserialized JSON data table
-------------------------------------------------------------------------------
function entities.load(data, zone_id)
    entities.zone_records[zone_id] = {}
    if data.entities == nil then return end
    for _, rec in ipairs(data.entities) do
        table.insert(entities.zone_records[zone_id], {
            uid          = rec.uid,
            name         = rec.name,
            type         = rec.type,
            server_id    = rec.server_id or 0,
            x            = rec.x,
            y            = rec.y,
            z            = rec.z,
            nearest_node = rec.nearest_node,
            zone_id      = rec.zone_id,
        })
    end
end

-------------------------------------------------------------------------------
-- Count of known entities for a zone
-------------------------------------------------------------------------------
function entities.count(zone_id)
    if zone_id == nil then
        -- Sum across all zones
        local total = 0
        for _, records in pairs(entities.zone_records) do
            total = total + #records
        end
        return total
    end
    local records = entities.zone_records[zone_id]
    return records and #records or 0
end

return entities
