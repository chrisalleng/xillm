# Plan: FFXI Mapper Addon (Navmesh-Based Navigation)

## Overview

Navigation graphs are built offline from publicly available navmeshes
(https://github.com/LandSandBoat/xiNavmeshes) rather than through in-game
autonomous exploration. A Python converter reads Recast/Detour `.nav` binary
files, extracts the walkable polygon mesh, and writes `zone_<id>.json` files
in the same format the Lua addon already loads. In-game, the addon loads the
pre-built graph at zone entry and navigates via A* + IAutoFollow exactly as
before. Exploration mode is retained as a manual fallback for zones without a
navmesh.

---

## Coordinate System

Two coordinate spaces are involved.

**Ashita LocalPosition** (used throughout Lua code):
| Axis | Meaning |
|------|---------|
| X    | east/west |
| Y    | north/south (horizontal depth) |
| Z    | elevation |

**LandSandBoat `position_t` / Recast navmesh space:**
| Axis | Meaning |
|------|---------|
| [0] x | east/west |
| [1] y | elevation |
| [2] z | north/south |

The LandSandBoat server applies `ToDetourPos` / `ToFFXIPos` (both identical —
negating Y and Z):

```cpp
out[0] = pos->x;
out[1] = pos->y * -1;
out[2] = pos->z * -1;
```

Combining the Y/Z axis swap between `position_t` and `LocalPosition`, the
full conversions are:

**Navmesh → Ashita LocalPosition** (needed at load / query time in Lua):
```
LocalPosition.X  =  navmesh[0]   (east/west, unchanged)
LocalPosition.Y  = -navmesh[2]   (north/south  = -navmesh_z)
LocalPosition.Z  = -navmesh[1]   (elevation    = -navmesh_y)
```

**Ashita LocalPosition → navmesh** (needed in the converter):
```python
navmesh = (local_x, -local_z, -local_y)
```

---

## Navmesh Source

**Repository**: https://github.com/LandSandBoat/xiNavmeshes  
**Format**: Recast/Detour binary `.nav` (one file per zone)  
**Naming**: zone name — `Bastok_Markets.nav`, `Windurst_Waters.nav`, etc.

Zone ID → filename is resolved via a static table in the converter covering
all ~300 FFXI zones (IDs are fixed in the game binary and unchanged across
private servers).

---

## Navmesh Binary Format (Recast/Detour)

```
NavMeshSetHeader
  magic:    int32   (0x4d534554  'MSET')
  version:  int32   (1)
  numTiles: int32
  params:   dtNavMeshParams
              orig[3]:    float[3]   world-space tile grid origin
              tileWidth:  float
              tileHeight: float
              maxTiles:   int32
              maxPolys:   int32

Per tile (repeated numTiles times):
  tileRef:   uint32   (or uint64 on 64-bit builds — see Key Unknowns)
  dataSize:  int32
  <raw tile blob of dataSize bytes>

Tile blob layout (dtMeshHeader then tightly packed arrays):
  dtMeshHeader (sizeof = 100 bytes):
    magic, version, x, y, layer, userId:  int32 × 6
    polyCount, vertCount, maxLinkCount:   int32 × 3
    detailMeshCount, detailVertCount, detailTriCount: int32 × 3
    bvNodeCount, offMeshConCount, offMeshBase: int32 × 3
    walkableHeight, walkableRadius, walkableClimb: float × 3
    bmin[3], bmax[3]:   float × 6
    bvQuantFactor:      float

  vertices:          float[vertCount × 3]   (x, y, z in navmesh space)
  polygons:          dtPoly[polyCount]
  links:             dtLink[maxLinkCount]
  detail meshes:     dtPolyDetail[detailMeshCount]
  detail vertices:   float[detailVertCount × 3]
  detail triangles:  uint8[detailTriCount × 4]
  BV tree nodes:     dtBVNode[bvNodeCount]
  off-mesh conns:    dtOffMeshConnection[offMeshConCount]

dtPoly (sizeof = 32 bytes for DT_VERTS_PER_POLY=6):
  firstLink:   uint32
  verts[6]:    uint16[6]   indices into tile vertex array
  neis[6]:     uint16[6]   neighbour polygon refs (0 = no neighbour;
                             >= 0x8000 = cross-tile link via dtLink chain)
  flags:       uint16
  vertCount:   uint8
  areaAndtype: uint8
```

---

## Offline Converter

### File: `tools/nav_to_graph.py`

**Input**: a directory of `.nav` files + the zone-name → zone-ID table  
**Output**: `zone_<id>.json` files ready to drop into
`<Ashita>/config/addons/mapper/`

### Algorithm

```
for each .nav file:
  1. Parse NavMeshSetHeader → dtNavMeshParams
  2. For each tile:
       a. Read tileRef + dataSize, read raw tile blob
       b. Parse dtMeshHeader from blob
       c. Read vertex array: float[vertCount × 3]
       d. Read polygon array: dtPoly[polyCount]
  3. Build node list:
       for each polygon p in tile:
         centroid = mean of p.verts[0..vertCount-1] mapped through vertex array
         (cx, cy, cz) = navmesh_to_local(centroid)   # coordinate conversion
         node_id = global running counter
         add node {id, x=cx, y=cy, z=cz}
  4. Build edge list from polygon adjacency:
       for each polygon p, each nei in p.neis[]:
         if nei == 0: skip (no neighbour)
         if nei < 0x8000: add edge (p_global_id, nei_global_id)
         if nei >= 0x8000: resolve via dtLink chain → cross-tile edge
  5. Deduplicate edges (store as sorted pairs)
  6. Prune graph density:
       spatial pass — remove nodes closer than MIN_NODE_DIST (2.5 yalms)
       to any already-kept node; redirect their edges to the kept node
  7. Output JSON:
       { "zone_id": N, "nodes": [...], "edges": [...] }
```

### Coordinate conversion function
```python
def navmesh_to_local(nx, ny, nz):
    """Navmesh space → Ashita LocalPosition (X=east, Y=north, Z=elev)."""
    lx =  nx    # east/west unchanged
    ly = -nz    # north/south = -navmesh_z
    lz = -ny    # elevation   = -navmesh_y
    return lx, ly, lz
```

### Zone name → ID table (excerpt)
```python
ZONE_IDS = {
    "Bastok_Markets":       234,
    "Bastok_Markets[S]":    456,
    "Windurst_Waters":      230,
    "South_Gustaberg":      107,
    "Jugner_Forest":        114,
    # ... all ~300 zones
}
```

### Graph density control
A navmesh tile can produce thousands of polygon centroids. The Lua A*
implementation uses `table.sort` on the open set, which is fast enough for
~500–2000 nodes but slow for 10,000+. After building the full centroid graph,
apply a greedy min-distance filter:

```python
MIN_NODE_DIST = 2.5  # yalms
kept = []
for node in all_nodes:
    if all(dist3d(node, k) >= MIN_NODE_DIST for k in kept):
        kept.append(node)
```

This typically reduces a raw centroid graph from 5,000–15,000 nodes to
500–2,000, well within Lua A* performance budget.

---

## Changes to Lua Addon

### mapper.lua
- `zone_init` already loads `zone_<id>.json` — no change needed
- Pre-built navmesh graphs load transparently alongside explored graphs
- `/mapper explore` kept as manual fallback for zones without a `.nav`
- All other commands (`goto`, `save`, `status`, `pos`) unchanged

### graph.lua, navigator.lua, entities.lua
- No changes required — these are format-agnostic

### Exploration mode
- Retained verbatim; runs when the zone JSON is absent
- Explored data saved as `zone_<id>.json` and is compatible with converter output

---

## Revised File Structure

```
<AshitaPath>/addons/mapper/
├── mapper.lua        -- unchanged
├── graph.lua         -- unchanged
├── navigator.lua     -- unchanged
└── entities.lua      -- unchanged

<AshitaPath>/config/addons/mapper/
├── zone_230.json     -- pre-built from Windurst_Waters.nav
├── zone_234.json     -- pre-built from Bastok_Markets.nav
└── ...               -- one per zone covered by xiNavmeshes

<project>/tools/
└── nav_to_graph.py   -- offline converter
```

---

## Implementation Sequence

1. **Confirm binary layout**: hex-inspect one `.nav` file; verify magic bytes,
   read first tile header, check dtPoly size vs. `DT_VERTS_PER_POLY`
2. **Write `tools/nav_to_graph.py`**:
   - Header + tile parsing
   - Vertex + polygon extraction
   - Centroid computation + coordinate conversion
   - Edge building from `neis[]` and cross-tile dtLinks
   - Min-distance density filter
   - JSON output
3. **Single-zone test**: convert one zone (e.g. South Gustaberg), load in Ashita,
   walk to a known landmark and compare `/mapper pos` against a nearby node coord
4. **Batch convert** all zones from the xiNavmeshes repo
5. **In-game navigation test**: `/mapper goto` using navmesh-derived graph;
   verify path quality and A* performance (check frame rate during computation)
6. **Tune** `MIN_NODE_DIST` and `DENSITY_REDIRECT` if paths are too sparse or too
   chatty
7. **Optional**: update `/mapper status` to display whether the loaded graph came
   from a navmesh or from exploration

---

## Key Unknowns to Resolve

| Unknown | How to resolve |
|---------|---------------|
| `dtTileRef` size (uint32 vs uint64) | Hex-inspect the file: if bytes 12–15 after the tile count are a plausible data size and 8–11 look like a tile ref, it's uint32; otherwise uint64 |
| `DT_VERTS_PER_POLY` value | Standard Recast uses 6; if dtPoly struct doesn't align at 32 bytes, try 3 or other values |
| Cross-tile dtLink chain structure | `dtLink { ref uint32, next uint32, edge uint8, side uint8, bmin uint8, bmax uint8 }` — 12 bytes; verify against tile blob arithmetic |
| Off-mesh connections (ramps, jumps) | Parse `dtOffMeshConnection`; include as normal edges initially, remove if they cause invalid paths |
| Scale factor | Expected to be 1:1 (yalms); verify by checking distance between two known landmarks in converter output |

---

## Ashita v4 API Reference (unchanged)

```lua
local player = GetPlayerEntity()
local px = player.Movement.LocalPosition.X   -- east/west
local py = player.Movement.LocalPosition.Y   -- north/south
local pz = player.Movement.LocalPosition.Z   -- elevation

local zone_id = AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)

local follow = AshitaCore:GetMemoryManager():GetAutoFollow()
follow:SetFollowDeltaX(tx - px)
follow:SetFollowDeltaY(ty - py)
follow:SetFollowDeltaZ(0)
follow:SetIsAutoRunning(1)   -- must refresh every frame
```
