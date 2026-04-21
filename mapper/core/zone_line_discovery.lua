--[[
* nav - core/zone_line_discovery.lua
*
* Discovers zone line triggers as the player walks near them.
* Uses pre-extracted trigger positions from data/zone_lines.lua.
*
* When a zone line is discovered:
*   1. Announce in chat with zone names and position
*   2. Compute shortest paths between all discovered zone lines in this zone
*   3. Persist discovery state with the zone graph
--]]

local S         = require('core.state')
local D         = require('core.debug')
local astar     = require('planning.astar')
local zone_data = require('data.zone_lines')

local zld = {}

local DISCOVER_RADIUS = 50.0
local REPATH_INTERVAL = 18000  -- frames between route recalcs (~5 min)

-- Per-zone discovery state
-- zld.discovered[zone_id] = { {to_zone, x, y, node_id, paths={...}}, ... }
zld.discovered = {}

-- Frame of last path recalculation per zone
local _last_repath = {}

local function zone_name(zone_id)
    return zone_data.names[zone_id] or ('Zone ' .. tostring(zone_id))
end

function zld.get_zone_lines(zone_id)
    return zone_data.lines[zone_id]
end

function zld.get_discovered(zone_id)
    return zld.discovered[zone_id] or {}
end

local function is_discovered(zone_id, to_zone, x, y)
    local disc = zld.discovered[zone_id]
    if not disc then return false end
    for _, d in ipairs(disc) do
        if d.to_zone == to_zone then
            local dx = d.x - x
            local dy = d.y - y
            if dx*dx + dy*dy < DISCOVER_RADIUS * DISCOVER_RADIUS then
                return true
            end
        end
    end
    return false
end

local function find_nearest_node(zone_graph, x, y)
    if zone_graph and zone_graph.nearest_node then
        return zone_graph.nearest_node(x, y)
    end
    return nil
end

local function compute_paths(zone_id, zone_graph)
    local disc = zld.discovered[zone_id]
    if not disc or #disc < 2 then return end

    for i = 1, #disc do
        disc[i].paths = disc[i].paths or {}
        if disc[i].node_id == nil then
            disc[i].node_id = find_nearest_node(zone_graph, disc[i].x, disc[i].y)
        end
    end

    local changed = false
    for i = 1, #disc do
        for j = i + 1, #disc do
            local a = disc[i]
            local b = disc[j]
            if a.node_id and b.node_id then
                local key_ab = tostring(b.to_zone) .. ':' .. tostring(b.x) .. ',' .. tostring(b.y)
                local key_ba = tostring(a.to_zone) .. ':' .. tostring(a.x) .. ',' .. tostring(a.y)
                local path = astar.search(zone_graph, a.node_id, b.node_id)
                if path then
                    local dist = 0
                    for k = 1, #path - 1 do
                        local na = zone_graph.nodes[path[k]]
                        local nb = zone_graph.nodes[path[k + 1]]
                        if na and nb then
                            local dx = nb.x - na.x
                            local dy = nb.y - na.y
                            dist = dist + math.sqrt(dx*dx + dy*dy)
                        end
                    end
                    a.paths[key_ab] = { to_zone = b.to_zone, distance = math.floor(dist), hops = #path }
                    b.paths[key_ba] = { to_zone = a.to_zone, distance = math.floor(dist), hops = #path }
                    changed = true
                end
            end
        end
    end
    if changed then
        zone_graph.dirty = true
    end
end

function zld.tick(zone_id, zone_graph, px, py)
    if zone_id == nil or zone_id == 0 then return end
    local known_lines = zone_data.lines[zone_id]
    if not known_lines then return end

    local r2 = DISCOVER_RADIUS * DISCOVER_RADIUS
    for _, zl in ipairs(known_lines) do
        if not is_discovered(zone_id, zl.to, zl.x, zl.y) then
            local dx = px - zl.x
            local dy = py - zl.y
            if dx*dx + dy*dy < r2 then
                zld.discovered[zone_id] = zld.discovered[zone_id] or {}
                local nid = find_nearest_node(zone_graph, zl.x, zl.y)
                local entry = {
                    to_zone = zl.to,
                    x = zl.x,
                    y = zl.y,
                    node_id = nid,
                    paths = {},
                }
                table.insert(zld.discovered[zone_id], entry)
                D.print_msg(string.format('Discovered %s → %s zone line at (%.0f, %.0f)',
                    zone_name(zone_id), zone_name(zl.to), zl.x, zl.y))
                compute_paths(zone_id, zone_graph)
            end
        end
    end

    -- Periodic path recalculation
    local last = _last_repath[zone_id] or 0
    if S.frame - last >= REPATH_INTERVAL then
        _last_repath[zone_id] = S.frame
        local disc = zld.discovered[zone_id]
        if disc and #disc >= 2 then
            compute_paths(zone_id, zone_graph)
        end
    end
end

function zld.serialize(zone_id)
    local disc = zld.discovered[zone_id]
    if not disc then return nil end
    local out = {}
    for _, d in ipairs(disc) do
        out[#out + 1] = {
            to_zone = d.to_zone,
            x = d.x,
            y = d.y,
            node_id = d.node_id,
            paths = d.paths,
        }
    end
    return out
end

function zld.load(data, zone_id)
    if data == nil then return end
    zld.discovered[zone_id] = {}
    for _, d in ipairs(data) do
        zld.discovered[zone_id][#zld.discovered[zone_id] + 1] = {
            to_zone = d.to_zone,
            x = d.x,
            y = d.y,
            node_id = d.node_id,
            paths = d.paths or {},
        }
    end
    local n = #zld.discovered[zone_id]
    if n > 0 then
        D.dbg(string.format('Loaded %d discovered zone lines for zone %d.', n, zone_id))
    end
end

function zld.on_zone_change(from_zone, to_zone, last_px, last_py, zone_graph)
    if from_zone == nil or from_zone == 0 then return end
    if to_zone == nil or to_zone == 0 then return end
    local known_lines = zone_data.lines[from_zone]
    if not known_lines then return end

    for _, zl in ipairs(known_lines) do
        if zl.to == to_zone and not is_discovered(from_zone, zl.to, zl.x, zl.y) then
            zld.discovered[from_zone] = zld.discovered[from_zone] or {}
            local nid = nil
            if zone_graph then nid = find_nearest_node(zone_graph, last_px, last_py) end
            local entry = {
                to_zone = zl.to,
                x = last_px,
                y = last_py,
                node_id = nid,
                paths = {},
            }
            table.insert(zld.discovered[from_zone], entry)
            D.print_msg(string.format('Discovered %s → %s zone line at (%.0f, %.0f)',
                zone_name(from_zone), zone_name(zl.to), last_px, last_py))
            compute_paths(from_zone, zone_graph)
            return
        end
    end
end

function zld.reset(zone_id)
    if zone_id then
        zld.discovered[zone_id] = nil
    else
        zld.discovered = {}
    end
    _last_repath = {}
end

function zld.count(zone_id)
    if zone_id then
        local d = zld.discovered[zone_id]
        return d and #d or 0
    end
    local total = 0
    for _, d in pairs(zld.discovered) do total = total + #d end
    return total
end

return zld
