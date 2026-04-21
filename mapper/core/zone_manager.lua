--[[
* nav - core/zone_manager.lua
*
* Zone lifecycle management.
* Detects zone changes, loads/saves graph data and collision geometry,
* and resets planner/explorer state when the zone changes.
--]]

local S           = require('core.state')
local D           = require('core.debug')
local persistence = require('core.persistence')
local zone_graph  = require('graph.zone_graph')
local geo         = require('geometry.geometry_provider')
local spatial_idx = require('geometry.spatial_index')
local vision      = require('exploration.vision')
local hires_mesh  = require('geometry.hires_mesh')
local zld         = require('core.zone_line_discovery')

local zone_manager = {}

-------------------------------------------------------------------------------
-- Load collision geometry for a zone.
-- Sets S.collision and S.spatial_index.
-- Safe to call when no collision file exists (sets both to nil).
-------------------------------------------------------------------------------
local function load_collision(zone_id)
    S.collision     = nil
    S.spatial_index = nil

    local col = geo.load(zone_id, S.data_path)
    if col == nil then
        D.dbg(string.format('No collision data for zone %d.', zone_id))
        return
    end

    S.collision     = col
    S.spatial_index = spatial_idx.new(col.triangles, col.bounds)
    D.print_msg(string.format(
        'Zone %d: loaded %d collision triangles.',
        zone_id, #col.triangles))
end

-------------------------------------------------------------------------------
-- Initialize state for a new zone.
-- Called on the first valid zone read and on every subsequent zone change.
-------------------------------------------------------------------------------
function zone_manager.init(zone_id)
    if zone_id == nil or zone_id == 0 then return end

    -- Save previous zone graph if we were in a valid zone.
    if S.zone_id ~= nil and S.zone_id ~= 0 and S.graph then
        persistence.save_zone_graph(S.zone_id, S.data_path, S.graph)
    end

    S.zone_id     = zone_id
    S.goal        = nil
    S.active_path = nil

    -- Reset graph and load persisted data.
    zone_graph.reset()
    vision.reset()
    hires_mesh.reset()
    zld.reset(zone_id)
    S.graph = zone_graph
    S.hires_mesh = hires_mesh
    persistence.load_zone_graph(zone_id, S.data_path, zone_graph)

    -- Load collision geometry.
    load_collision(zone_id)

    -- Reconstruct vision seen set from persisted graph nodes.
    vision.rebuild_seen(zone_graph)

    D.dbg(string.format('Zone manager: initialized zone %d.', zone_id))
end

-------------------------------------------------------------------------------
-- Save current zone data (called on unload or explicit /mapper save).
-------------------------------------------------------------------------------
function zone_manager.save(force)
    if S.zone_id == nil or S.zone_id == 0 then return end
    if S.graph then
        persistence.save_zone_graph(S.zone_id, S.data_path, S.graph, force)
    end
end

return zone_manager
