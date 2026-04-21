--[[
* nav - core/state.lua
*
* Central runtime state singleton.
* Every module that needs shared state imports this module.
* Nothing in state.lua imports other nav modules to avoid circular dependencies.
--]]

local S = {}

-- ---- Addon runtime ----
S.zone_id   = nil     -- current zone integer ID (nil until first valid read)
S.data_path = ''      -- Ashita install path prefix, set on load
S.frame     = 0       -- monotonically-increasing frame counter

-- ---- Collision geometry for current zone ----
-- Set by zone_manager when a collision file is loaded.
-- nil if no collision data is available for this zone.
S.collision = nil     -- raw table: { zone_id, triangles, bounds, metadata }
S.spatial_index = nil -- built by spatial_index.new() from S.collision.triangles

-- ---- Per-zone discovered graph ----
-- The zone_graph module is loaded on startup; this is a reference to it.
-- Reset on zone change.
S.graph = nil         -- set to require('graph.zone_graph') by zone_manager

-- ---- High-resolution mesh ----
-- Reference to require('geometry.hires_mesh'); managed by zone_manager.
S.hires_mesh = nil

-- ---- Active planning ----
S.goal        = nil   -- { x, y } destination in current zone, or nil
S.active_path = nil   -- array of node IDs from last A* result
S.exploring   = false -- true while autonomous exploration is running

-- ---- Zone transition return state ----
-- Set when the player zones out while S.exploring is true.
-- Cleared once we successfully return to the original zone.
S.zone_return = nil
-- {
--   to_zone    = zone_id to return to (where exploration was happening)
--   from_zone  = zone_id we accidentally entered
--   from_x/y   = last known position in to_zone (approximate zone line coords)
--   phase      = 'wait' | 'navigate'
--   wait_until = frame number to wait until before acting
--   spawn_x/y  = spawn position in from_zone (recorded when phase→navigate)
--   heading    = player facing in from_zone (used to find zone line direction)
--   attempts   = number of navigation attempts made
-- }

-- ---- Stuck blacklist ----
-- Positions where the character got stuck during navigation.
-- Array of { x, y, frame } entries; recent entries repel pathfinding.
S.stuck_blacklist = {}

-- ---- Last known player position (previous frame) ----
-- Used to record from_x/y when a zone change is detected.
S.last_px = nil
S.last_py = nil

-- ---- Debug flags ----
S.debug = {
    enabled          = false,
    show_graph       = false,
    show_collision   = false,
    show_walkable    = false,
    show_frontier    = false,
    show_active_path = false,
    show_target      = false,
    verbose_ticks    = false,
}

return S
