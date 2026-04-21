--[[
* nav - planning/routing_coordinator.lua
*
* Routes all navigation through the high-resolution heightfield mesh.
* Returns nil only when the mesh hasn't finished building yet.
--]]

local hires = require('geometry.hires_mesh')
local D     = require('core.debug')

local router = {}

-------------------------------------------------------------------------------
-- Route from (sx,sy) to (gx,gy) using hires mesh A*.
-- Returns { mode='local', path={x,y,z}[] } or nil if mesh can't route yet.
-------------------------------------------------------------------------------
function router.plan(zone_graph, sx, sy, gx, gy)
    local local_path = hires.find_path(sx, sy, gx, gy)
    if local_path and #local_path > 0 then
        return { mode = 'local', path = local_path }
    end

    return nil
end

return router
