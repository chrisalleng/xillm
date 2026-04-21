--[[
* nav - core/debug_state.lua
*
* Writes a JSON snapshot of addon runtime state to disk every N frames so
* external tools (e.g. Claude Code) can read it for live debugging.
*
* Output file:
*   <ashita>/config/addons/mapper/debug_state.json
*
* Snapshot fields:
*   frame          — monotonic frame counter
*   zone_id        — current zone integer id
*   player         — { x, y, z }  LocalPosition
*   adapter_mode   — 'idle' | 'navigate' | 'explore'
*   exploring      — bool
*   goal           — { x, y } or null
*   active_path_len — length of S.active_path (0 if nil)
*   graph          — { nodes, edges, frontiers }
*   collision      — { loaded, triangles } or { loaded=false }
*   zone_return    — { to_zone, from_zone, phase, attempts } or null
*   log            — last 30 messages (newest last)
--]]

local json       = require('json')
local S          = require('core.state')
local D          = require('core.debug')
local vision     = require('exploration.vision')
local hires_mesh = require('geometry.hires_mesh')

local debug_state = {}

local function state_path()
    local base = S.data_path or ''
    if base ~= '' and base:sub(-1) ~= '/' and base:sub(-1) ~= '\\' then
        base = base .. '/'
    end
    return base .. 'config/addons/mapper/debug_state.json'
end

local function count_frontiers(zone_graph)
    if zone_graph == nil then return 0 end
    local n = 0
    for _, neighbors in pairs(zone_graph.adjacency or {}) do
        if #neighbors < 4 then n = n + 1 end
    end
    return n
end

-------------------------------------------------------------------------------
-- Write the current state snapshot to disk.
-- zone_graph : the zone_graph module (passed in to avoid a circular require)
-- px, py, pz : current player position
-- adapter    : movement_adapter module
-------------------------------------------------------------------------------
function debug_state.write(zone_graph, px, py, pz, adapter)
    local snap = {
        frame        = S.frame,
        zone_id      = S.zone_id,
        player       = { x = px, y = py, z = pz },
        adapter_mode = adapter.mode(),
        exploring    = S.exploring,
        goal         = S.goal and { x = S.goal.x, y = S.goal.y } or nil,
        active_path_len = S.active_path and #S.active_path or 0,
        graph = {
            nodes      = zone_graph and zone_graph.node_count() or 0,
            edges      = zone_graph and zone_graph.edge_count()  or 0,
            seen_cells = vision.seen_count(),
            queue      = vision.queue_depth(),
        },
        hires = hires_mesh.stats(),
        collision = S.collision and {
            loaded    = true,
            triangles = #S.collision.triangles,
        } or { loaded = false },
        zone_return = S.zone_return and {
            to_zone   = S.zone_return.to_zone,
            from_zone = S.zone_return.from_zone,
            phase     = S.zone_return.phase,
            attempts  = S.zone_return.attempts,
        } or nil,
        log = D.log_buffer,
    }

    local path = state_path()
    local ok, encoded = pcall(json.encode, snap)
    if not ok then return end

    local f = io.open(path, 'w')
    if f then
        f:write(encoded)
        f:close()
    end
end

return debug_state
