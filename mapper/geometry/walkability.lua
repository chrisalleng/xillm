--[[
* nav - geometry/walkability.lua
*
* Classify collision triangles as walkable or non-walkable.
*
* Classification criteria:
*   1. Surface normal must be "mostly upward" (angle from vertical < max_slope).
*      Default max slope: 45 degrees.
*   2. Triangle area must be above a minimum threshold.
*      Tiny triangles are likely seams or artifacts.
*
* walkability.classify(triangles) annotates each triangle in-place with a
* .walkable boolean field and a .normal table {nx,ny,nz}.
*
* Coordinate convention:
*   Triangle vertices: v0/v1/v2 = [X, Y, Z] (X=east, Y=north/south, Z=up).
*   A horizontal floor has normal pointing in the +Z direction: {0, 0, 1}.
--]]

local walkability = {}

-- Maximum slope angle from vertical (degrees).  Steeper → non-walkable.
walkability.MAX_SLOPE_DEG = 45.0

-- Minimum triangle area in square yalms.
walkability.MIN_AREA = 0.01

-------------------------------------------------------------------------------
-- Compute the normal vector of a triangle (unnormalised cross product).
-- Returns nx, ny, nz.
-------------------------------------------------------------------------------
local function triangle_normal(v0, v1, v2)
    -- Edge vectors: E1 = V1-V0, E2 = V2-V0
    local e1x, e1y, e1z = v1[1]-v0[1], v1[2]-v0[2], v1[3]-v0[3]
    local e2x, e2y, e2z = v2[1]-v0[1], v2[2]-v0[2], v2[3]-v0[3]
    -- Cross product E1 × E2
    local nx = e1y*e2z - e1z*e2y
    local ny = e1z*e2x - e1x*e2z
    local nz = e1x*e2y - e1y*e2x
    return nx, ny, nz
end

-------------------------------------------------------------------------------
-- Compute triangle area (half the magnitude of the cross product).
-------------------------------------------------------------------------------
local function triangle_area(nx, ny, nz)
    return 0.5 * math.sqrt(nx*nx + ny*ny + nz*nz)
end

-------------------------------------------------------------------------------
-- Classify all triangles in-place.
-- Adds .walkable = bool and .normal = {nx,ny,nz} (unit vector) to each triangle.
-- Also adds .area = float.
-------------------------------------------------------------------------------
function walkability.classify(triangles, max_slope_deg, min_area)
    max_slope_deg = max_slope_deg or walkability.MAX_SLOPE_DEG
    min_area      = min_area      or walkability.MIN_AREA

    -- cos(max_slope) is the minimum Z component of the unit normal for walkable.
    local cos_max_slope = math.cos(math.rad(max_slope_deg))

    for _, tri in ipairs(triangles) do
        local nx, ny, nz = triangle_normal(tri.v0, tri.v1, tri.v2)
        local area = triangle_area(nx, ny, nz)
        local len  = math.sqrt(nx*nx + ny*ny + nz*nz)

        tri.area = area

        if len < 1e-9 then
            -- Degenerate triangle (zero area).
            tri.normal   = { 0, 0, 1 }
            tri.walkable = false
        else
            local unx, uny, unz = nx/len, ny/len, nz/len
            tri.normal = { unx, uny, unz }
            -- Z-up convention: floor normals have large positive unz.
            tri.walkable = (unz >= cos_max_slope) and (area >= min_area)
        end
    end
end

-------------------------------------------------------------------------------
-- Return the count of walkable triangles.
-------------------------------------------------------------------------------
function walkability.count_walkable(triangles)
    local n = 0
    for _, tri in ipairs(triangles) do
        if tri.walkable then n = n + 1 end
    end
    return n
end

return walkability
