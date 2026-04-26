#!/usr/bin/env python3
"""
Convert collision JSON files to Wavefront OBJ for Recast navmesh building.

Collision JSON format (from tools/extract_collision):
  vertices: [[x, y, z], ...]  — raw MZB coordinates
  triangles: [[i0, i1, i2], ...] — 0-based indices

Coordinate transform (MZB → Recast Y-up):
  FFXI game: X=east/west, Y=south/north (horizontal), Z=elevation (up)
  MZB raw:   game.x=MZB.x, game.y=-MZB.y, game.z=MZB.z
  Recast:    X=horizontal, Y=up, Z=horizontal

  Recast = (MZB.x, MZB.z, -MZB.y) = (game.x, game.z, game.y)

Winding order: MZB triangles have reversed winding for Recast's
  upward-normal convention. Swap indices 1,2 to fix.
"""

import json
import sys
import os


def load_collision(json_path: str) -> dict:
    with open(json_path) as f:
        return json.load(f)


def write_obj(data: dict, obj_path: str):
    """Write collision data as Wavefront OBJ matching FFXI NavMesh Builder coords."""
    verts = data['vertices']
    tris = data['triangles']

    with open(obj_path, 'w') as f:
        f.write(f"# Zone {data.get('zone_id', '?')}\n")
        f.write(f"# {len(verts)} vertices, {len(tris)} triangles\n\n")

        # Transform: (MZB.x, MZB.z, -MZB.y) for Recast Y-up
        for v in verts:
            f.write(f"v {v[0]:.4f} {v[2]:.4f} {-v[1]:.4f}\n")

        f.write("\n")

        # OBJ uses 1-based indices; swap v1/v2 to fix winding order
        for tri in tris:
            f.write(f"f {tri[0]+1} {tri[2]+1} {tri[1]+1}\n")

    print(f"Wrote {obj_path}: {len(verts)} verts, {len(tris)} tris")


def convert_zone(zone_id: int, collision_dir: str, output_dir: str):
    json_path = os.path.join(collision_dir, f"{zone_id}.json")
    if not os.path.exists(json_path):
        print(f"No collision file for zone {zone_id}")
        return None

    data = load_collision(json_path)
    obj_path = os.path.join(output_dir, f"{zone_id}.obj")
    os.makedirs(output_dir, exist_ok=True)
    write_obj(data, obj_path)
    return obj_path


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: collision_to_obj.py <zone_id> [collision_dir] [output_dir]")
        sys.exit(1)

    zone_id = int(sys.argv[1])
    collision_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(__file__), '..', 'nav', 'data', 'collision')
    output_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(__file__), 'data', 'obj')

    convert_zone(zone_id, collision_dir, output_dir)
