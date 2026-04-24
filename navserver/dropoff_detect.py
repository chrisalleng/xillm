"""Detect one-way drop-off cliffs in a zone's navmesh and emit a connections
JSON that the navserver merges into the Detour off-mesh connection table.

Approach: for every border edge of the navmesh (edge with no neighboring
walkable polygon), probe outward in the edge's outward direction, then search
downward within `max_fall` for the nearest walkable polygon. If a landing is
found at least `min_drop` below the edge, emit a one-way connection from the
top edge (slightly inside the source polygon) to the landing point.

The output JSON shape matches `mapper/data/dropoffs/<zone_id>.json`:
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
(x=EW, y=NS, z=elev). The navserver converts to Recast at load time via
game_to_recast().

Usage:
    python -m navserver.dropoff_detect --zone 110
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
COLLISION_DIR = ROOT / "mapper" / "data" / "collision"
DROPOFF_DIR = ROOT / "mapper" / "data" / "dropoffs"

# The extractor and MZB parser live in the mapper tools directory. Import
# lazily (inside the function) so the module can still be imported without
# those tools available (e.g. in the navserver).


def load_collision(zone_id: int):
    """Load a zone's collision JSON and convert to Recast-space verts/tris.
    Mirrors NavServer.load_collision in server.py (kept standalone so this
    module runs without importing server.py)."""
    verts, tris, _, _ = load_collision_with_walls(zone_id)
    return verts, tris


def load_collision_with_walls(zone_id: int):
    """Like load_collision but additionally returns (wall_verts_rc, wall_tris)
    — the instance-wall-triangle subset, in Recast space, already winding-
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
    # Use `emitted_terrain_count` (the post-walkability-filter count) — the
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


def make_settings():
    s = navmesh.NavSettings()
    s.cell_size = 0.20
    s.cell_height = 0.12
    s.agent_radius = 1.5
    s.agent_max_slope = 45.0
    s.agent_max_climb = 0.3
    s.region_min_size = 2
    s.region_merge_size = 20
    s.tile_size = 1024
    return s


def recast_to_game(rx, ry, rz):
    # Recast (Y-up) → runtime Ashita (x=EW, y=NS, z=elev).
    return (rx, rz, -ry)


def load_hitwall_bboxes_rc(zone_id, ffxi_path):
    """Parse MZB + MMB for the given zone, return a list of axis-aligned
    bboxes (in Recast space) for every `hitwall_*` instance — the explicit
    invisible collision walls FFXI uses to seal non-droppable cliffs.

    Why this is separate from the navmesh's instance-wall triangles:
    * Recast's navmesh is eroded by hitwall geometry identically to any other
      solid instance, but when identifying DROP-OFF edges we need to
      distinguish between "invisible wall that's stopping the player"
      (hitwall — not droppable) and "visible stone/wall object that happens
      to terminate at a cliff" (player CAN fall off the side). The
      `hitwall_*` name prefix is the precise signal; regular solid models
      like `pl_sta_coi1_m` must not block a drop-off candidate."""
    import sys
    tools = str(Path(__file__).parent.parent / "mapper" / "tools" / "extract_collision")
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
        # Ashita → Recast: swap Y↔Z and negate Z (same transform as
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
    overlaps. A single huge wall (like hitwall_005 at Beaucedine, 29y × 32y)
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
    mesh = navmesh.build_navmesh(verts, tris, make_settings())
    ground, off = navmesh.count_polys(mesh)
    print(f"  built in {time.time()-t0:.1f}s  ({ground} ground + {off} off-mesh polys)")

    print(f"[dropoff_detect] loading hitwall bboxes from MZB (invisible-wall markers)...")
    hitwall_bboxes = load_hitwall_bboxes_rc(zone_id, ffxi_path)
    print(f"  found {len(hitwall_bboxes)} hitwall instances")
    wall_grid = build_hitwall_grid(hitwall_bboxes, cell_size=5.0)

    print(f"[dropoff_detect] enumerating border edges...")
    edges = navmesh.enumerate_border_edges(mesh)
    print(f"  {len(edges)} border edges")

    # Each edge: (p1x, p1y, p1z, p2x, p2y, p2z, nx, nz, poly_ref)
    agent_radius = 1.5

    candidates = []
    skipped = {"wall_blocked": 0,
               "same_poly": 0, "no_landing": 0, "too_shallow": 0, "too_far": 0,
               "already_reachable": 0}

    for i, edge in enumerate(edges):
        p1x, p1y, p1z, p2x, p2y, p2z, nx, nz, top_ref = edge
        mx = 0.5 * (p1x + p2x)
        my = 0.5 * (p1y + p2y)
        mz = 0.5 * (p1z + p2z)
        # Step-off point: edge midpoint pushed outward by (agent_radius + reach).
        outward = agent_radius + horizontal_reach
        sx = mx + nx * outward
        sz = mz + nz * outward

        # Occlusion check — does an invisible `hitwall_*` instance sit
        # anywhere in the candidate's drop-off zone? A hitwall positioned
        # between the navmesh border edge and the cliff's actual physical
        # edge means the game's collision stops the player before they can
        # walk off. We search a disk of radius = horizontal_match_tol around
        # the step-off point (same radius used for the landing search below);
        # if a hitwall exists within that disk at a compatible elevation the
        # fall is occluded. Regular visible geometry (pl_sta_*, _rol_*, etc.)
        # intentionally does NOT count here — those are droppable surfaces
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
        # findNearestPoly snap — otherwise both endpoints can snap to a
        # common connecting slope and the path comes back bogus-short.
        straight = math.hypot(end_rc[0] - start_rc[0], end_rc[2] - start_rc[2])
        if straight <= max_check_path:
            plen = navmesh.find_path_length_between_refs(
                mesh, top_ref, ref, start_rc, end_rc
            )
            if plen > 0.0 and straight > 1e-3 and plen / straight < detour_ratio_min:
                skipped["already_reachable"] += 1
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
