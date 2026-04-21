--[[
* nav - core/persistence.lua
*
* Load and save per-zone discovered graph data.
*
* Data layout:
*   <data_path>/config/addons/mapper/zones/<zone_id>.graph.json
*
* Graph JSON format:
*   {
*     "zone_id": <int>,
*     "nodes":   [ { "id", "x", "y", "z" }, ... ],
*     "edges":   [ { "a", "b", "conf" }, ... ]
*   }
--]]

local json     = require('json')
local D        = require('core.debug')
local zld      = require('core.zone_line_discovery')
local entities = require('entities')

local persistence = {}

-- Build the file path for a zone's graph data.
local function graph_path(data_path, zone_id)
    return string.format('%sconfig/addons/mapper/zones/%d.graph.json',
                         data_path, zone_id)
end

-- Ensure the zones subdirectory exists.
local function ensure_dir(data_path)
    pcall(function()
        if ashita and ashita.fs and ashita.fs.create_directory then
            ashita.fs.create_directory(
                data_path .. 'config/addons/mapper/zones/')
        end
    end)
end

-------------------------------------------------------------------------------
-- Load a zone graph from disk into zone_graph module.
-- Returns true on success, false if file not found or parse error.
-------------------------------------------------------------------------------
function persistence.load_zone_graph(zone_id, data_path, zone_graph)
    if zone_id == nil or zone_id == 0 then return false end
    local path = graph_path(data_path, zone_id)
    local f    = io.open(path, 'r')
    if not f then return false end
    local content = f:read('*all'); f:close()
    local ok, data = pcall(json.decode, content)
    if not ok or not data then
        D.print_msg('Warning: could not parse zone graph file for zone ' .. zone_id)
        return false
    end
    zone_graph.load(data)
    if data.zone_lines then
        zld.load(data.zone_lines, zone_id)
    end
    if data.entities then
        entities.load(data.entities, zone_id)
    end
    D.print_msg(string.format('Loaded zone %d graph: %d nodes, %d edges',
        zone_id, zone_graph.node_count(), zone_graph.edge_count()))
    return true
end

-------------------------------------------------------------------------------
-- Save a zone graph to disk.
-- Skips write if zone_graph.dirty is false and force is not set.
-------------------------------------------------------------------------------
function persistence.save_zone_graph(zone_id, data_path, zone_graph, force)
    if zone_id == nil or zone_id == 0 then return end
    if not force and not zone_graph.dirty then return end
    ensure_dir(data_path)
    local data = {
        zone_id    = zone_id,
        nodes      = zone_graph.serialize_nodes(),
        edges      = zone_graph.serialize_edges(),
        zone_lines = zld.serialize(zone_id),
        entities   = entities.serialize(zone_id),
    }
    local path = graph_path(data_path, zone_id)
    local f = io.open(path, 'w')
    if f then
        f:write(json.encode(data))
        f:close()
        zone_graph.dirty = false
    else
        D.print_msg('Warning: could not write zone graph to ' .. path)
    end
end

return persistence
