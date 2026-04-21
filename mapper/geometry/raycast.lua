--[[
* nav - geometry/raycast.lua
*
* Collision query helpers: floor detection and movement-segment validation.
*
* Coordinate convention (Ashita LocalPosition):
*   X = east/west, Y = north/south (horizontal), Z = elevation (up)
*
* Triangles are stored as { v0=[x,y,z], v1=[x,y,z], v2=[x,y,z] }.
* In the index X→v[1], Y→v[2], Z→v[3].
*
* All functions accept an optional spatial_index; if nil they return sensible
* fallback values so callers degrade gracefully when collision is unavailable.
--]]

local raycast = {}

local EPSILON = 1e-6

-- Maximum height above/below a query point to consider as "floor".
raycast.FLOOR_SEARCH_RANGE = 20.0

-- Maximum height step between consecutive segment samples (yalms).
-- Matches Recast NavMesh max_climb of 0.3-0.5m.
raycast.MAX_STEP_HEIGHT = 0.5

-- Number of samples per yalm along a segment for blocking checks.
raycast.SAMPLES_PER_YALM = 3.0

-------------------------------------------------------------------------------
-- Möller–Trumbore ray-triangle intersection.
-- ray_o  = {ox, oy, oz}, ray_d = {dx, dy, dz} (need not be normalised)
-- tri    = { v0={x,y,z}, v1={x,y,z}, v2={x,y,z} }  (1-indexed arrays)
-- Returns t (parameter along ray) or nil if no intersection.
-------------------------------------------------------------------------------
local function ray_triangle(ray_o, ray_d, tri)
    local v0, v1, v2 = tri.v0, tri.v1, tri.v2
    local e1 = { v1[1]-v0[1], v1[2]-v0[2], v1[3]-v0[3] }
    local e2 = { v2[1]-v0[1], v2[2]-v0[2], v2[3]-v0[3] }

    -- h = D × E2
    local hx = ray_d[2]*e2[3] - ray_d[3]*e2[2]
    local hy = ray_d[3]*e2[1] - ray_d[1]*e2[3]
    local hz = ray_d[1]*e2[2] - ray_d[2]*e2[1]

    local a = e1[1]*hx + e1[2]*hy + e1[3]*hz
    if math.abs(a) < EPSILON then return nil end  -- parallel

    local f = 1.0 / a

    -- s = O - V0
    local sx = ray_o[1] - v0[1]
    local sy = ray_o[2] - v0[2]
    local sz = ray_o[3] - v0[3]

    local u = f * (sx*hx + sy*hy + sz*hz)
    if u < 0.0 or u > 1.0 then return nil end

    -- q = S × E1
    local qx = sy*e1[3] - sz*e1[2]
    local qy = sz*e1[1] - sx*e1[3]
    local qz = sx*e1[2] - sy*e1[1]

    local v = f * (ray_d[1]*qx + ray_d[2]*qy + ray_d[3]*qz)
    if v < 0.0 or u + v > 1.0 then return nil end

    local t = f * (e2[1]*qx + e2[2]*qy + e2[3]*qz)
    if t < EPSILON then return nil end

    return t
end

-------------------------------------------------------------------------------
-- Floor height at horizontal position (x, y).
-- Shoots a ray straight down from (x, y, z_start) and returns the Z of the
-- nearest hit below z_start.
-- Returns z_floor (elevation) or nil if no floor found.
--
-- idx       : spatial_index (or nil → returns nil)
-- triangles : the triangles array from collision data
-- x, y      : horizontal position (Ashita X, Y)
-- z_start   : starting elevation for the downward ray (e.g. player Z + buffer)
-------------------------------------------------------------------------------
function raycast.floor_height(idx, triangles, x, y, z_start)
    if idx == nil then return nil end

    z_start = z_start or 100.0
    local ray_o = { x, y, z_start }
    local ray_d = { 0, 0, -1 }  -- straight down

    local candidate_tris = require('geometry.spatial_index').query_point(idx, x, y)

    local best_t = math.huge
    for _, ti in ipairs(candidate_tris) do
        local tri = triangles[ti]
        if tri then
            local t = ray_triangle(ray_o, ray_d, tri)
            if t ~= nil and t < best_t then
                best_t = t
            end
        end
    end

    if best_t == math.huge then return nil end
    return z_start - best_t   -- Z coordinate of the floor hit
end

-------------------------------------------------------------------------------
-- Check whether a horizontal movement segment from (x1,y1) to (x2,y2) is
-- traversable.
-- "Traversable" means:
--   1. Floor exists at both endpoints and at intermediate samples.
--   2. No sample has a height change greater than MAX_STEP_HEIGHT vs the previous.
--   3. No near-vertical triangle blocks the segment at any sample.
--
-- Returns true if traversable, false if blocked, nil if no collision data.
-------------------------------------------------------------------------------
function raycast.segment_walkable(idx, triangles, x1, y1, x2, y2, z_hint)
    if idx == nil then return nil end

    z_hint = z_hint or 0.0
    local dx = x2 - x1
    local dy = y2 - y1
    local len = math.sqrt(dx * dx + dy * dy)
    if len < EPSILON then return true end  -- same point

    local steps = math.max(2, math.floor(len * raycast.SAMPLES_PER_YALM) + 1)
    local prev_z = nil

    local z_start = z_hint + raycast.FLOOR_SEARCH_RANGE

    for i = 0, steps do
        local t = i / steps
        local sx = x1 + t * dx
        local sy = y1 + t * dy
        local fz = raycast.floor_height(idx, triangles, sx, sy, z_start)
        if fz == nil then
            return false  -- No floor = gap / cliff = not traversable
        end
        if prev_z ~= nil and math.abs(fz - prev_z) > raycast.MAX_STEP_HEIGHT then
            return false  -- Too steep a step
        end
        prev_z  = fz
        z_start = fz + raycast.FLOOR_SEARCH_RANGE  -- follow terrain height
    end

    return true
end

-------------------------------------------------------------------------------
-- Check if a 3D ray segment from A to B is blocked by any non-walkable
-- triangle (e.g. a wall).  Used for path-smoothing validation.
-- Casts at three heights (ankle, waist, head) for robustness.
-- Returns true if blocked, false if clear, nil if no collision data.
-------------------------------------------------------------------------------
local WALL_CHECK_OFFSETS = { 0.3, 0.8, 1.5 }

function raycast.segment_blocked(idx, triangles, x1, y1, z1, x2, y2, z2)
    if idx == nil then return nil end

    local si = require('geometry.spatial_index')
    local candidate_tris = si.query_rect(idx,
        math.min(x1,x2), math.min(y1,y2),
        math.max(x1,x2), math.max(y1,y2))

    if #candidate_tris == 0 then return false end

    -- Cast at multiple heights for robustness against short/tall obstacles.
    local base_z1 = z1
    local base_z2 = z2
    for _, dz in ipairs(WALL_CHECK_OFFSETS) do
        local rz1 = base_z1 + dz
        local rz2 = base_z2 + dz
        local ray_o = { x1, y1, rz1 }
        local ray_d = { x2-x1, y2-y1, rz2-rz1 }
        local len = math.sqrt(ray_d[1]^2 + ray_d[2]^2 + ray_d[3]^2)
        if len >= EPSILON then
            for _, ti in ipairs(candidate_tris) do
                local tri = triangles[ti]
                if tri then
                    local t = ray_triangle(ray_o, ray_d, tri)
                    if t ~= nil and t >= 0 and t <= 1.0 then
                        -- Verify the hit is on a near-vertical face (wall), not floor.
                        local v0, v1, v2 = tri.v0, tri.v1, tri.v2
                        local e1x = v1[1]-v0[1]; local e1y = v1[2]-v0[2]; local e1z = v1[3]-v0[3]
                        local e2x = v2[1]-v0[1]; local e2y = v2[2]-v0[2]; local e2z = v2[3]-v0[3]
                        local nz = e1x*e2y - e1y*e2x
                        local nx = e1y*e2z - e1z*e2y
                        local ny = e1z*e2x - e1x*e2z
                        local nlen = math.sqrt(nx*nx + ny*ny + nz*nz)
                        if nlen > EPSILON then
                            local slope = math.abs(nz) / nlen
                            if slope < 0.70 then
                                return true
                            end
                        end
                    end
                end
            end
        end
    end
    return false
end

return raycast
