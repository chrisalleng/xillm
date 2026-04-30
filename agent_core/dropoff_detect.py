"""Detect one-way drop-off cliffs in a zone's navmesh and emit a connections
JSON that agent_core merges into the Detour off-mesh connection table.

Approach: for every border edge of the navmesh (edge with no neighboring
walkable polygon), probe outward in the edge's outward direction, then search
downward within `max_fall` for the nearest walkable polygon. If a landing is
found at least `min_drop` below the edge, emit a one-way connection from the
top edge (slightly inside the source polygon) to the landing point.

The output JSON shape matches `nav/data/dropoffs/<zone_id>.json`:
    {
      "zone_id": 110,
      "max_fall": 60.0,
      "connections": [
        {"start": [x,y,z], "end": [x,y,z], "radius": 0.75, "bidir": false,
         "source": "auto"},
        ...
      ],
      "overrides": { "added": [], "removed": [] }
    }

All coordinates stored in the JSON are in **runtime Ashita** space
(x=EW, y=NS, z=elev). agent_core converts to Recast at load time via
game_to_recast().

Usage:
    python -m agent_core.dropoff_detect --zone 110
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "recast_wrapper" / "build"))
import navmesh  # type: ignore

ROOT = Path(__file__).parent.parent
COLLISION_DIR = ROOT / "nav" / "data" / "collision"
DROPOFF_DIR = ROOT / "nav" / "data" / "dropoffs"

# The extractor and MZB parser live in the nav tools directory. Import
# lazily (inside the function) so the module can still be imported without
# those tools available (e.g. inside agent_core).


def load_collision(zone_id: int):
    """Load a zone's collision JSON and convert to Recast-space verts/tris.
    Mirrors NavServer.load_collision in main.py (kept standalone so this
    module runs without importing main.py)."""
    verts, tris, _, _ = load_collision_with_walls(zone_id)
    return verts, tris


def load_collision_with_walls(zone_id: int):
    """Like load_collision but additionally returns (wall_verts_rc, wall_tris)
    - the instance-wall-triangle subset, in Recast space, already winding-
    corrected. Used by the occlusion check to detect hitwalls past a cliff
    edge. Terrain triangles come first in the raw JSON; instance walls are
    the suffix after terrain_triangle_count."""
    path = COLLISION_DIR / f"{zone_id}.json"
    with open(path) as f:
        data = json.load(f)
    verts_raw = np.array(data["vertices"], dtype=np.float64)
    tris = np.array(data["triangles"], dtype=np.int32)

    valid = np.all(np.abs(verts_raw) < 1500, axis=1)
    if not np.all(valid):
        good_idx = np.where(valid)[0]
        remap = np.full(len(verts_raw), -1, dtype=np.int32)
        remap[good_idx] = np.arange(len(good_idx), dtype=np.int32)
        verts_raw = verts_raw[valid]
        tri_valid = np.all(remap[tris] >= 0, axis=1)
        tris = remap[tris[tri_valid]]

    referenced = set(tris.flatten().tolist())
    orphaned = np.array([i not in referenced for i in range(len(verts_raw))], dtype=bool)
    if orphaned.any():
        keep = ~orphaned
        remap = np.full(len(verts_raw), -1, dtype=np.int32)
        remap[np.where(keep)[0]] = np.arange(int(keep.sum()), dtype=np.int32)
        verts_raw = verts_raw[keep]
        tris = remap[tris]

    verts_raw = verts_raw.astype(np.float32)
    # JSON is in Ashita convention; convert to Recast via ashita_to_recast
    # (swap Y↔Z and negate Z). Winding is preserved by this transform.
    verts = np.column_stack([
        verts_raw[:, 0],       # Recast.x = Ashita.X
        -verts_raw[:, 2],      # Recast.y = -Ashita.Z  (physical up)
        verts_raw[:, 1],       # Recast.z = Ashita.Y
    ]).astype(np.float32)
    tris_fixed = tris

    # Partition into terrain (first N) and instance-wall (the suffix).
    # Use `emitted_terrain_count` (the post-walkability-filter count) - the
    # older `terrain_triangle_count` field is the pre-filter input count and
    # overshoots the boundary. Fall back for older JSON files without the
    # new field (conservatively treat all triangles as terrain).
    md = data.get("metadata", {})
    tcount = md.get("emitted_terrain_count")
    if tcount is None:
        tcount = len(tris_fixed)
    tcount = int(tcount)
    wall_tris = tris_fixed[tcount:] if tcount < len(tris_fixed) else np.zeros((0, 3), dtype=np.int32)
    return verts, tris_fixed, verts, wall_tris


def make_settings(zone_id=None):
    """Navmesh settings matching NavServer.get_mesh(). Applies the same
    per-zone override (ZONE_NAV_OVERRIDES) so the detector's navmesh is
    consistent with the server's - otherwise drop-off connections could
    reference polys that don't exist in the production navmesh."""
    s = navmesh.NavSettings()
    s.cell_size = 0.20
    s.cell_height = 0.06
    s.agent_radius = 0.25
    s.agent_max_slope = 45.0
    s.agent_max_climb = 0.6
    s.region_min_size = 2
    s.region_merge_size = 20
    s.tile_size = 1024
    if zone_id is not None:
        # Import lazily to avoid pulling in the full server stack at module
        # load time (dropoff_detect is also runnable standalone).
        from agent_core.main import NavServer
        overrides = NavServer.ZONE_NAV_OVERRIDES.get(zone_id, {})
        for k, v in overrides.items():
            setattr(s, k, v)
    return s


def recast_to_game(rx, ry, rz):
    # Recast (Y-up) -> runtime Ashita (x=EW, y=NS, z=elev).
    return (rx, rz, -ry)


def load_hitwall_bboxes_rc(zone_id, ffxi_path):
    """Parse MZB + MMB for the given zone, return a list of axis-aligned
    bboxes (in Recast space) for every `hitwall_*` instance - the explicit
    invisible collision walls FFXI uses to seal non-droppable cliffs.

    Why this is separate from the navmesh's instance-wall triangles:
    * Recast's navmesh is eroded by hitwall geometry identically to any other
      solid instance, but when identifying DROP-OFF edges we need to
      distinguish between "invisible wall that's stopping the player"
      (hitwall - not droppable) and "visible stone/wall object that happens
      to terminate at a cliff" (player CAN fall off the side). The
      `hitwall_*` name prefix is the precise signal; regular solid models
      like `pl_sta_coi1_m` must not block a drop-off candidate."""
    import sys
    tools = str(Path(__file__).parent.parent / "nav" / "tools" / "extract_collision")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import extract_collision as ec  # type: ignore

    rel = ec.get_rom_path(ffxi_path, zone_id)
    dat = os.path.join(ffxi_path, rel.replace("\\", "/"))
    if not os.path.exists(dat):
        return []
    with open(dat, "rb") as f:
        raw = f.read()
    mzb_block = None
    mmb_blocks = []
    for t, b in ec.iter_chunks(raw):
        if t == ec.RESOURCE_TYPE_MZB and mzb_block is None:
            mzb_block = b
        elif t == ec.RESOURCE_TYPE_MMB:
            mmb_blocks.append(b)
    if mzb_block is None:
        return []

    # Build MMB model index to recover each hitwall's model-local bbox.
    mmb_models = {}
    for mb in mmb_blocks:
        buf = bytearray(mb); ec.decode_mmb(buf)
        parsed = ec.parse_mmb(bytes(buf))
        if parsed:
            mmb_models[parsed["imgID"]] = parsed

    mzb_mut = bytearray(mzb_block); ec.decode_mzb(mzb_mut)
    instances = ec.parse_mzb_instances(mzb_mut)

    bboxes = []
    for inst in instances:
        if not inst["name"].startswith("hitwall"):
            continue
        model = mmb_models.get(inst["name"])
        if model is None or not model["models"]:
            continue
        # Take the bbox of the synthesized box submesh (MMB parser emits
        # the header bbox as a single box submesh for zero-piece models).
        verts_local = model["models"][0]["vertices"]
        m = ec.instance_matrix(inst)
        world_ashita = ec.apply_instance_transform(verts_local, m)
        # Ashita -> Recast: swap Y↔Z and negate Z (same transform as
        # load_collision uses on terrain/instance vertices).
        xs = [v[0] for v in world_ashita]          # Recast.x = Ashita.X
        ys = [-v[2] for v in world_ashita]         # Recast.y = -Ashita.Z
        zs = [v[1] for v in world_ashita]          # Recast.z = Ashita.Y
        # Inflate slightly (0.5y) to account for discretization between the
        # MZB bbox corners and the actual rasterized collision extents.
        bboxes.append((
            min(xs) - 0.5, max(xs) + 0.5,
            min(ys) - 0.5, max(ys) + 0.5,
            min(zs) - 0.5, max(zs) + 0.5,
        ))
    return bboxes


def build_terrain_grid(verts_rc, tris, cell_size=5.0):
    """Spatial index of collision triangles keyed by XZ cell (Recast space).
    Each triangle is appended to every cell its XZ AABB overlaps. Returns
    (dict[(cx,cz)]=list-of-tri-index, cell_size). Used for fast vertical-
    segment-vs-triangle blocking checks in the drop-off detector."""
    grid: dict[tuple[int, int], list[int]] = {}
    n = len(tris)
    if n == 0:
        return grid, cell_size
    # Compute per-triangle XZ bbox in one vectorized pass.
    v0 = verts_rc[tris[:, 0]]
    v1 = verts_rc[tris[:, 1]]
    v2 = verts_rc[tris[:, 2]]
    x_min = np.minimum(np.minimum(v0[:, 0], v1[:, 0]), v2[:, 0])
    x_max = np.maximum(np.maximum(v0[:, 0], v1[:, 0]), v2[:, 0])
    z_min = np.minimum(np.minimum(v0[:, 2], v1[:, 2]), v2[:, 2])
    z_max = np.maximum(np.maximum(v0[:, 2], v1[:, 2]), v2[:, 2])
    cx0 = np.floor(x_min / cell_size).astype(np.int32)
    cx1 = np.floor(x_max / cell_size).astype(np.int32)
    cz0 = np.floor(z_min / cell_size).astype(np.int32)
    cz1 = np.floor(z_max / cell_size).astype(np.int32)
    for i in range(n):
        for cx in range(int(cx0[i]), int(cx1[i]) + 1):
            for cz in range(int(cz0[i]), int(cz1[i]) + 1):
                grid.setdefault((cx, cz), []).append(i)
    return grid, cell_size


def has_overhead_rock(grid_tuple, verts_rc, tris, x, z, y_floor,
                      max_overhead=30.0, margin=0.2):
    """Return True if there is a near-horizontal collision triangle whose
    plane at (x, z) sits between `y_floor + margin` and `y_floor +
    max_overhead`. Used to reject drop-off candidates whose START poly
    is sitting under solid rock - Recast voxelizes the rock's underside
    or sides as a walkable poly at slightly the wrong height, creating
    a phantom drop that arrows through the rock to whatever's below.

    Filters to triangles within 60° of horizontal (`ny²/|n|² > 0.25`):
    walls and slopes are ignored, only floors/ceilings count."""
    grid, cell_size = grid_tuple
    cx = int(math.floor(x / cell_size))
    cz = int(math.floor(z / cell_size))
    tri_ids = grid.get((cx, cz), ())
    y_min = y_floor + margin
    y_max = y_floor + max_overhead
    for idx in tri_ids:
        a = verts_rc[tris[idx, 0]]
        b = verts_rc[tris[idx, 1]]
        c = verts_rc[tris[idx, 2]]
        ax, az = a[0], a[2]
        bx, bz = b[0], b[2]
        cx2, cz2 = c[0], c[2]
        d1 = (x - bx) * (az - bz) - (ax - bx) * (z - bz)
        d2 = (x - cx2) * (bz - cz2) - (bx - cx2) * (z - cz2)
        d3 = (x - ax) * (cz2 - az) - (cx2 - ax) * (z - az)
        neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        if neg and pos:
            continue
        e1x = b[0] - a[0]; e1y = b[1] - a[1]; e1z = b[2] - a[2]
        e2x = c[0] - a[0]; e2y = c[1] - a[1]; e2z = c[2] - a[2]
        nx = e1y * e2z - e1z * e2y
        ny = e1z * e2x - e1x * e2z
        nz = e1x * e2y - e1y * e2x
        n_len_sq = nx * nx + ny * ny + nz * nz
        if n_len_sq < 1e-12:
            continue
        if (ny * ny) / n_len_sq < 0.25:
            continue
        if abs(ny) < 1e-6:
            continue
        y_hit = a[1] - (nx * (x - ax) + nz * (z - az)) / ny
        if y_min < y_hit < y_max:
            return True
    return False


def segment_blocked(grid_tuple, verts_rc, tris, s, e, margin=0.5):
    """Return True if the 3D line segment `s`->`e` (Recast space) is
    intersected by any collision triangle strictly between the endpoints
    (a margin of `margin` yalms is excluded at each end to avoid self-
    intersection with the top-cliff and landing polys). Catches drop-off
    candidates whose straight fall line passes through solid geometry -
    cave ceilings, overhanging rock, or floors between two stacked
    levels - which means the "drop" can't actually happen.

    Implementation: walk XZ grid cells along the segment; for each
    collected triangle run Möller-Trumbore ray-triangle intersection."""
    grid, cell_size = grid_tuple
    dx = e[0] - s[0]
    dy = e[1] - s[1]
    dz = e[2] - s[2]
    seg_len = math.sqrt(dx * dx + dy * dy + dz * dz)
    if seg_len < 1e-6:
        return False
    t_lo = margin / seg_len
    t_hi = 1.0 - t_lo
    if t_hi <= t_lo:
        return False

    # Visit every XZ cell the segment crosses. Step at half cell_size so
    # we don't miss any cell for diagonal paths.
    steps = max(2, int(seg_len / (cell_size * 0.5)) + 1)
    cells_seen = set()
    inv_steps = 1.0 / steps
    for i in range(steps + 1):
        t = i * inv_steps
        cx = int(math.floor((s[0] + t * dx) / cell_size))
        cz = int(math.floor((s[2] + t * dz) / cell_size))
        cells_seen.add((cx, cz))

    tested = set()
    for key in cells_seen:
        for idx in grid.get(key, ()):
            if idx in tested:
                continue
            tested.add(idx)
            a = verts_rc[tris[idx, 0]]
            b = verts_rc[tris[idx, 1]]
            c = verts_rc[tris[idx, 2]]
            # Möller-Trumbore ray/triangle intersection (adapted for segment).
            e1x = b[0] - a[0]; e1y = b[1] - a[1]; e1z = b[2] - a[2]
            e2x = c[0] - a[0]; e2y = c[1] - a[1]; e2z = c[2] - a[2]
            px = dy * e2z - dz * e2y
            py = dz * e2x - dx * e2z
            pz = dx * e2y - dy * e2x
            det = e1x * px + e1y * py + e1z * pz
            if -1e-8 < det < 1e-8:
                continue
            inv_det = 1.0 / det
            tx = s[0] - a[0]; ty = s[1] - a[1]; tz = s[2] - a[2]
            u = (tx * px + ty * py + tz * pz) * inv_det
            if u < 0.0 or u > 1.0:
                continue
            qx = ty * e1z - tz * e1y
            qy = tz * e1x - tx * e1z
            qz = tx * e1y - ty * e1x
            v = (dx * qx + dy * qy + dz * qz) * inv_det
            if v < 0.0 or u + v > 1.0:
                continue
            t_hit = (e2x * qx + e2y * qy + e2z * qz) * inv_det
            if t_lo < t_hit < t_hi:
                return True
    return False


def build_hitwall_grid(hitwall_bboxes, cell_size=5.0):
    """Index the hitwall bbox list by the XZ grid cells each bbox covers.
    Returns (grid_dict, cell_size, bbox_array) shaped like build_wall_grid's
    output for uniform query ergonomics."""
    import numpy as _np
    if not hitwall_bboxes:
        return ({}, cell_size, _np.zeros((0, 6), dtype=_np.float32))
    arr = _np.array(hitwall_bboxes, dtype=_np.float32)
    grid: dict = {}
    for i, b in enumerate(hitwall_bboxes):
        cx0 = int(b[0] // cell_size)
        cx1 = int(b[1] // cell_size)
        cz0 = int(b[4] // cell_size)
        cz1 = int(b[5] // cell_size)
        for cx in range(cx0, cx1 + 1):
            for cz in range(cz0, cz1 + 1):
                grid.setdefault((cx, cz), []).append(i)
    return (grid, cell_size, arr)


def build_wall_grid(verts_rc, wall_tris, cell_size=5.0):
    """Bucket every instance-wall triangle into every XZ cell its bbox
    overlaps. A single huge wall (like hitwall_005 at Beaucedine, 29y x 32y)
    would be missed by centroid-bucketing but is caught by bbox-bucketing.
    Returns (grid_dict, cell_size, tri_bboxes) where tri_bboxes[i] =
    (xmin, xmax, ymin, ymax, zmin, zmax) in Recast space for wall triangle i."""
    n = len(wall_tris)
    if n == 0:
        return ({}, cell_size, np.zeros((0, 6), dtype=np.float32))
    idx = wall_tris.astype(np.int64)
    v0 = verts_rc[idx[:, 0]]
    v1 = verts_rc[idx[:, 1]]
    v2 = verts_rc[idx[:, 2]]
    xmin = np.minimum(np.minimum(v0[:, 0], v1[:, 0]), v2[:, 0])
    xmax = np.maximum(np.maximum(v0[:, 0], v1[:, 0]), v2[:, 0])
    ymin = np.minimum(np.minimum(v0[:, 1], v1[:, 1]), v2[:, 1])
    ymax = np.maximum(np.maximum(v0[:, 1], v1[:, 1]), v2[:, 1])
    zmin = np.minimum(np.minimum(v0[:, 2], v1[:, 2]), v2[:, 2])
    zmax = np.maximum(np.maximum(v0[:, 2], v1[:, 2]), v2[:, 2])
    bboxes = np.column_stack([xmin, xmax, ymin, ymax, zmin, zmax]).astype(np.float32)
    grid: dict = {}
    for i in range(n):
        cx0 = int(xmin[i] // cell_size)
        cx1 = int(xmax[i] // cell_size)
        cz0 = int(zmin[i] // cell_size)
        cz1 = int(zmax[i] // cell_size)
        for cx in range(cx0, cx1 + 1):
            for cz in range(cz0, cz1 + 1):
                grid.setdefault((cx, cz), []).append(i)
    return (grid, cell_size, bboxes)


def wall_blocks_step_off(grid_tuple, sx, sy, sz, horizontal_radius=2.5,
                          vertical_radius=3.0):
    """True if any instance-wall triangle bbox overlaps a box of extent
    (horizontal_radius, vertical_radius, horizontal_radius) around the
    step-off point (sx, sy, sz) in Recast space. The cell-neighborhood
    search expands with horizontal_radius so large radii still hit all
    relevant cells."""
    grid, cell_size, bboxes = grid_tuple
    if bboxes.shape[0] == 0:
        return False
    cx = int(sx // cell_size)
    cz = int(sz // cell_size)
    qxmin = sx - horizontal_radius; qxmax = sx + horizontal_radius
    qymin = sy - vertical_radius;   qymax = sy + vertical_radius
    qzmin = sz - horizontal_radius; qzmax = sz + horizontal_radius
    # Expand the cell-neighborhood search to cover the full horizontal
    # radius. For radius R and cell C, we need ±ceil(R/C) cells.
    import math as _math
    n = int(_math.ceil(horizontal_radius / cell_size))
    seen_any = False
    for dz in range(-n, n + 1):
        for dx in range(-n, n + 1):
            bucket = grid.get((cx + dx, cz + dz))
            if not bucket:
                continue
            for i in bucket:
                b = bboxes[i]
                if b[0] > qxmax or b[1] < qxmin:
                    continue
                if b[2] > qymax or b[3] < qymin:
                    continue
                if b[4] > qzmax or b[5] < qzmin:
                    continue
                return True
    return False


def detect_dropoffs(
    zone_id: int,
    max_fall: float = 60.0,
    horizontal_reach: float = 2.0,
    min_drop: float = 4.0,
    horizontal_match_tol: float = 15.0,
    dedup_stride: float = 5.0,
    conn_radius: float = 0.75,
    detour_ratio_min: float = 4.0,
    max_check_path: float = 80.0,
    ffxi_path: str = "/home/chris/Faugus/xillm/drive_c/Program Files (x86)/PlayOnline/SquareEnix/FINAL FANTASY XI",
):
    """Run the detector. Returns a list of connections in runtime Ashita space."""
    print(f"[dropoff_detect] zone {zone_id}: loading collision...")
    verts, tris, wall_verts_rc, wall_tris = load_collision_with_walls(zone_id)
    print(f"  verts={len(verts)}  tris={len(tris)}  instance_walls={len(wall_tris)}")

    print(f"[dropoff_detect] building navmesh...")
    t0 = time.time()
    settings = make_settings(zone_id)
    try:
        mesh = navmesh.build_navmesh(verts, tris, settings)
    except RuntimeError as e:
        # Indoor city zones, BCNM arenas, and similar have no walkable
        # ground terrain - Recast refuses to build with "No tiles built".
        # Such zones can't have drop-offs by definition; emit an empty
        # connections list and exit gracefully so a sweep over all 255
        # zones doesn't bail at the first interior.
        print(f"[dropoff_detect] zone {zone_id} has no walkable geometry "
              f"({e}); emitting empty dropoffs.")
        return []
    ground, off = navmesh.count_polys(mesh)
    print(f"  built in {time.time()-t0:.1f}s  ({ground} ground + {off} off-mesh polys)")

    print(f"[dropoff_detect] loading hitwall bboxes from MZB (invisible-wall markers)...")
    hitwall_bboxes = load_hitwall_bboxes_rc(zone_id, ffxi_path)
    print(f"  found {len(hitwall_bboxes)} hitwall instances")
    wall_grid = build_hitwall_grid(hitwall_bboxes, cell_size=5.0)

    # Terrain-intersection grid: catches drops whose straight vertical fall
    # passes through solid geometry (cave ceilings, overhangs). Zone 7 has
    # no hitwall markers but dense cave structure; the terrain check is
    # what distinguishes a real cliff from a drop-through-the-ceiling.
    print(f"[dropoff_detect] building terrain-intersection grid...")
    terrain_grid = build_terrain_grid(verts, tris, cell_size=5.0)
    print(f"  {sum(len(v) for v in terrain_grid[0].values())} tri-cell entries "
          f"across {len(terrain_grid[0])} cells")

    print(f"[dropoff_detect] enumerating border edges...")
    edges = navmesh.enumerate_border_edges(mesh)
    print(f"  {len(edges)} border edges")

    # Each edge: (p1x, p1y, p1z, p2x, p2y, p2z, nx, nz, poly_ref)
    agent_radius = settings.agent_radius

    candidates = []
    skipped = {"wall_blocked": 0,
               "same_poly": 0, "no_landing": 0, "too_shallow": 0, "too_far": 0,
               "already_reachable": 0, "terrain_between": 0, "start_overhung": 0}

    for i, edge in enumerate(edges):
        p1x, p1y, p1z, p2x, p2y, p2z, nx, nz, top_ref = edge
        mx = 0.5 * (p1x + p2x)
        my = 0.5 * (p1y + p2y)
        mz = 0.5 * (p1z + p2z)
        # Step-off point: edge midpoint pushed outward by (agent_radius + reach).
        outward = agent_radius + horizontal_reach
        sx = mx + nx * outward
        sz = mz + nz * outward

        # Occlusion check - does an invisible `hitwall_*` instance sit
        # anywhere in the candidate's drop-off zone? A hitwall positioned
        # between the navmesh border edge and the cliff's actual physical
        # edge means the game's collision stops the player before they can
        # walk off. We search a disk of radius = horizontal_match_tol around
        # the step-off point (same radius used for the landing search below);
        # if a hitwall exists within that disk at a compatible elevation the
        # fall is occluded. Regular visible geometry (pl_sta_*, _rol_*, etc.)
        # intentionally does NOT count here - those are droppable surfaces
        # that terminate at cliff edges, not invisible walls.
        if wall_blocks_step_off(wall_grid, sx, my, sz,
                                 horizontal_radius=horizontal_match_tol,
                                 vertical_radius=max(max_fall * 0.5, 5.0)):
            skipped["wall_blocked"] += 1
            continue

        # Recast.y = -Ashita.Z, i.e. physical-up-positive. A drop (landing
        # physically below the edge) has LOWER Recast.y. Search the AABB
        # extending downward in Recast.y from the edge.
        half_fall = 0.5 * max_fall
        center = (sx, my - half_fall, sz)
        extents = (horizontal_match_tol, half_fall, horizontal_match_tol)
        ref, lx, ly, lz = navmesh.find_nearest_poly(mesh, center, extents)
        if ref == 0:
            skipped["no_landing"] += 1
            continue
        if ref == top_ref:
            skipped["same_poly"] += 1
            continue
        # drop (physical yalms fallen) = edge Recast.y − landing Recast.y
        # = (Ashita.Z_landing) − (Ashita.Z_top). Positive means landing is
        # physically below the edge.
        drop = my - ly
        if drop < min_drop:
            skipped["too_shallow"] += 1
            continue
        # Horizontal distance from step-off XZ to landing XZ.
        d_xy = math.hypot(lx - sx, lz - sz)
        if d_xy > horizontal_match_tol:
            skipped["too_far"] += 1
            continue
        # Start of the connection: push inward slightly onto the TOP poly
        # (1.0y along -normal from edge midpoint) so Detour snaps it to the
        # top polygon at build time.
        start_rc = (mx - nx * 1.0, my, mz - nz * 1.0)
        end_rc = (lx, ly, lz)

        # Redundancy filter: if the existing navmesh routes between the
        # SPECIFIC top-cliff poly and SPECIFIC landing poly at reasonable cost,
        # a connection adds nothing. Use explicit-ref query to avoid the
        # findNearestPoly snap - otherwise both endpoints can snap to a
        # common connecting slope and the path comes back bogus-short.
        straight = math.hypot(end_rc[0] - start_rc[0], end_rc[2] - start_rc[2])
        if straight <= max_check_path:
            plen = navmesh.find_path_length_between_refs(
                mesh, top_ref, ref, start_rc, end_rc
            )
            if plen > 0.0 and straight > 1e-3 and plen / straight < detour_ratio_min:
                skipped["already_reachable"] += 1
                continue

        # Terrain-intersection filter: does the straight 3D drop path
        # (start->landing, diagonal through air) pass through solid
        # geometry? Covers cave ceilings, overhangs, and inter-level
        # floors that stand between the top poly and the landing poly.
        if segment_blocked(terrain_grid, verts, tris,
                           start_rc, end_rc, margin=0.5):
            skipped["terrain_between"] += 1
            continue

        # Overhead-rock filter: a real cliff has clear sky above the
        # step-off point. If there's a ceiling triangle (downward-facing
        # surface) within max_overhead yalms above the start, the start
        # poly is sitting under solid rock - a navmesh artifact, not a
        # genuine cliff edge. Drops emitted here would arrow through
        # the cave/overhang ceiling into the room below.
        if has_overhead_rock(terrain_grid, verts, tris,
                             start_rc[0], start_rc[2], start_rc[1],
                             max_overhead=30.0, margin=0.2):
            skipped["start_overhung"] += 1
            continue

        candidates.append({
            "start_rc": start_rc,
            "end_rc": end_rc,
            "drop": drop,
            "d_xy": d_xy,
            "top_ref": top_ref,
        })

    print(f"  raw candidates: {len(candidates)}")
    for reason, n in skipped.items():
        print(f"  skipped [{reason}]: {n}")

    # Deduplicate: bucket start_rc XZ into dedup_stride cells, keep deepest.
    buckets: dict[tuple[int, int], dict] = {}
    for c in candidates:
        sx, _, sz = c["start_rc"]
        key = (int(sx / dedup_stride), int(sz / dedup_stride))
        prev = buckets.get(key)
        if prev is None or c["drop"] > prev["drop"]:
            buckets[key] = c
    dedup = list(buckets.values())
    print(f"  after dedup: {len(dedup)} connections")

    # Convert to runtime Ashita + emit JSON shape.
    connections = []
    for c in dedup:
        start_game = recast_to_game(*c["start_rc"])
        end_game = recast_to_game(*c["end_rc"])
        connections.append({
            "start": [round(v, 3) for v in start_game],
            "end": [round(v, 3) for v in end_game],
            "radius": conn_radius,
            "bidir": False,
            "source": "auto",
            "_drop": round(c["drop"], 2),
        })
    return connections


def write_output(zone_id: int, connections: list, max_fall: float):
    DROPOFF_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DROPOFF_DIR / f"{zone_id}.json"

    existing = None
    if out_path.exists():
        with open(out_path) as f:
            existing = json.load(f)
    overrides = (existing or {}).get("overrides", {"added": [], "removed": []})

    payload = {
        "zone_id": zone_id,
        "max_fall": max_fall,
        "connections": connections,
        "overrides": overrides,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[dropoff_detect] wrote {out_path} ({len(connections)} connections)")

    # Auto-deploy to the Ashita addon folder so /nav dropoffs reload
    # picks up the new connections without a separate deploy step. Mirrors
    # the instances-JSON auto-deploy in extract_collision.py.
    deploy_dir = Path(
        "/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/"
        "config/addons/nav/dropoffs"
    )
    if deploy_dir.parent.is_dir():
        deploy_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(out_path, deploy_dir / f"{zone_id}.json")
        print(f"[dropoff_detect] deployed to {deploy_dir / f'{zone_id}.json'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zone", type=int, required=True)
    ap.add_argument("--max-fall", type=float, default=60.0,
                    help="Max vertical drop (yalms) to consider (default 60)")
    ap.add_argument("--horizontal-reach", type=float, default=2.0,
                    help="How far past the edge to project the step-off point (default 2y)")
    ap.add_argument("--min-drop", type=float, default=2.0,
                    help="Minimum drop to classify as a cliff (default 2y)")
    ap.add_argument("--match-tol", type=float, default=15.0,
                    help="Horizontal search tolerance for landing poly / "
                         "hitwall occlusion radius (default 15y)")
    ap.add_argument("--dedup-stride", type=float, default=3.0,
                    help="Grid stride for deduplicating nearby candidates (default 3y)")
    ap.add_argument("--conn-radius", type=float, default=0.75,
                    help="Detour off-mesh connection match radius (default 0.75)")
    args = ap.parse_args()

    connections = detect_dropoffs(
        args.zone,
        max_fall=args.max_fall,
        horizontal_reach=args.horizontal_reach,
        min_drop=args.min_drop,
        horizontal_match_tol=args.match_tol,
        dedup_stride=args.dedup_stride,
        conn_radius=args.conn_radius,
    )
    write_output(args.zone, connections, args.max_fall)


if __name__ == "__main__":
    main()
