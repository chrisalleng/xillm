--[[
* nav - geometry/spatial_index.lua
*
* Fast 2D spatial lookup over collision triangles.
* Uses a uniform grid over the X/Y (horizontal) plane.
*
* Cells are identified by integer (cx, cy) and store lists of triangle indices.
* Only horizontal extents are indexed; Z (elevation) is queried lazily.
*
* Public interface:
*   spatial_index.new(triangles, bounds) -> idx
*   spatial_index.query_point(idx, x, y) -> { tri_i, ... }
*   spatial_index.query_rect(idx, x0,y0, x1,y1) -> { tri_i, ... }
--]]

local spatial_index = {}

-- Grid cell size in yalms.  Smaller = faster lookup, more memory.
local CELL_SIZE = 4.0

local function cell_xy(x, y)
    return math.floor(x / CELL_SIZE), math.floor(y / CELL_SIZE)
end

local function tri_bounds_2d(tri)
    local xs = { tri.v0[1], tri.v1[1], tri.v2[1] }
    local ys = { tri.v0[2], tri.v1[2], tri.v2[2] }
    local minx = math.min(xs[1], xs[2], xs[3])
    local maxx = math.max(xs[1], xs[2], xs[3])
    local miny = math.min(ys[1], ys[2], ys[3])
    local maxy = math.max(ys[1], ys[2], ys[3])
    return minx, miny, maxx, maxy
end

local function cell_key(cx, cy)
    -- Encode as a string key.  Lua tables handle this well for reasonable map sizes.
    return cx .. ',' .. cy
end

-------------------------------------------------------------------------------
-- Build a spatial index over a triangles array.
-- bounds: { min=[x,y,z], max=[x,y,z] } (used only for diagnostics).
-- Returns the index object.
-------------------------------------------------------------------------------
function spatial_index.new(triangles, bounds)
    local idx = {
        grid       = {},   -- [key] = { tri_idx, ... }
        triangles  = triangles,
        cell_size  = CELL_SIZE,
        bounds     = bounds,
        count      = #triangles,
    }

    for i, tri in ipairs(triangles) do
        local x0, y0, x1, y1 = tri_bounds_2d(tri)
        local cx0, cy0 = cell_xy(x0, y0)
        local cx1, cy1 = cell_xy(x1, y1)
        for cx = cx0, cx1 do
            for cy = cy0, cy1 do
                local k = cell_key(cx, cy)
                idx.grid[k] = idx.grid[k] or {}
                table.insert(idx.grid[k], i)
            end
        end
    end

    return idx
end

-------------------------------------------------------------------------------
-- Return triangle indices that overlap the cell containing (x, y).
-- Includes one ring of neighbouring cells for boundary triangles.
-------------------------------------------------------------------------------
function spatial_index.query_point(idx, x, y)
    local cx, cy = cell_xy(x, y)
    local result = {}
    local seen   = {}
    for dx = -1, 1 do
        for dy = -1, 1 do
            local k = cell_key(cx + dx, cy + dy)
            for _, ti in ipairs(idx.grid[k] or {}) do
                if not seen[ti] then
                    seen[ti] = true
                    table.insert(result, ti)
                end
            end
        end
    end
    return result
end

-------------------------------------------------------------------------------
-- Return triangle indices that overlap the 2D AABB (x0,y0)-(x1,y1).
-------------------------------------------------------------------------------
function spatial_index.query_rect(idx, x0, y0, x1, y1)
    -- Expand by one cell on each side.
    local cx0, cy0 = cell_xy(math.min(x0, x1), math.min(y0, y1))
    local cx1, cy1 = cell_xy(math.max(x0, x1), math.max(y0, y1))
    local result = {}
    local seen   = {}
    for cx = cx0 - 1, cx1 + 1 do
        for cy = cy0 - 1, cy1 + 1 do
            local k = cell_key(cx, cy)
            for _, ti in ipairs(idx.grid[k] or {}) do
                if not seen[ti] then
                    seen[ti] = true
                    table.insert(result, ti)
                end
            end
        end
    end
    return result
end

return spatial_index
