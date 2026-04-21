--[[
* nav - movement/movement_adapter.lua
*
* Thin adapter between the planner/explorer and the preserved movement
* execution layer (movement_bridge.lua).
*
* This boundary means: if the movement_bridge interface ever needs to change,
* only this file needs updating.  The rest of the codebase talks to the adapter.
*
* All methods delegate directly to movement_bridge with no transformation
* in the initial scaffold.  As the planner matures this file is where format
* conversions (e.g. planner waypoint structs → node-ID arrays) will live.
--]]

local bridge = require('movement.movement_bridge')

local adapter = {}

-- Pass-through accessors -------------------------------------------------------

function adapter.is_active()    return bridge.is_active()    end
function adapter.mode()         return bridge.mode()         end
function adapter.get_probe()    return bridge.get_probe()    end
function adapter.get_heading()  return bridge.get_heading()  end
function adapter.get_probe_count()  return bridge.get_probe_count()  end
function adapter.get_stuck_frames() return bridge.get_stuck_frames() end
function adapter.stop()         return bridge.stop()         end

-- Per-frame tick ---------------------------------------------------------------

-- px/py = LocalPosition.X/Y (horizontal), pz = elevation, frame = counter
function adapter.tick(px, py, pz, frame)
    bridge.tick(px, py, pz, frame)
end

-- Graph-waypoint navigation ----------------------------------------------------

-- path      : array of node IDs from zone_graph A*
-- graph_ref : zone_graph module (exposes .nodes[id].{x,y})
-- on_done   : function(reason) callback  ('reached' | 'stuck' | 'error')
-- extra_x/y : optional raw target after path end (e.g. trigger position)
function adapter.navigate(path, graph_ref, on_done, extra_x, extra_y)
    bridge.start_navigate(path, graph_ref, on_done, extra_x, extra_y)
end

-- Coordinate-array navigation (from hires mesh) --------------------------------

-- waypoints : array of { x, y, z } world coordinates
-- on_done   : function(reason) callback  ('reached' | 'stuck' | 'error')
function adapter.navigate_local(waypoints, on_done)
    bridge.start_navigate_local(waypoints, on_done)
end

-- Autonomous probe-based exploration -------------------------------------------

-- heading   : initial facing in radians
-- graph_ref : zone_graph module
-- on_node   : function(x, y, z)  called when a sample position is recorded
-- on_done   : function(reason)   called when exploration ends
-- on_debug  : function(msg)      optional debug log callback
-- start_x/y : horizontal start position
function adapter.explore(heading, graph_ref, on_node, on_done, on_debug, start_x, start_y)
    bridge.start_explore(heading, graph_ref, on_node, on_done, on_debug, start_x, start_y)
end

return adapter
