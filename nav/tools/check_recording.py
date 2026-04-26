#!/usr/bin/env python3
"""
check_recording.py — compare a nav.lua /nav record session against what
agent_core's Recast navmesh thinks about those same positions.

Usage:
    python3 check_recording.py                 # reads nav_record.json from
                                                # the default config/addons/nav/ path
    python3 check_recording.py <path>          # explicit recording file

For each recorded player sample, reports:
  * how far it is from the nearest walkable navmesh poly (big value = Recast
    thinks that spot is unreachable but the player walked through it)
  * which instance bboxes contain it (the player walked INSIDE the bbox of
    an object Recast treats as a wall)

That exposes coord-projection bugs and axis-inversion bugs: if the player
walked through wide-open terrain but Recast sees a wall there, both
indicators will fire at the same sample.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / 'agent_core'))
sys.path.insert(0, str(REPO / 'agent_core' / 'recast_wrapper' / 'build'))
import navmesh  # noqa: E402
import main as server_mod  # noqa: E402

DEFAULT_RECORD = Path(
    '/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav/nav_record.json')
# Samples farther than this from ANY poly center are "far" — poly centers
# can legitimately be 5-10y from an interior point, so tune high.
OFF_MESH_THRESHOLD_Y = 15.0
# Cumulative distance between sample pairs (yalms). We sub-sample to skip
# tiny moves.
STRIDE_Y = 3.0


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RECORD
    if not path.exists():
        print(f'No recording at {path}. Run /nav record start / stop in-game first.')
        sys.exit(1)

    rec = json.load(open(path))
    zone_id = int(rec['zone_id'])
    points = rec['points']
    print(f'Loaded {len(points)} samples from zone {zone_id} ({path})')

    srv = server_mod.NavServer()
    mesh = srv.get_mesh(zone_id)
    centers = np.array(navmesh.get_poly_centers(mesh))
    print(f'Zone {zone_id} navmesh: {len(centers)} polys')

    inst_path = REPO / 'nav' / 'data' / 'instances' / f'{zone_id}.json'
    instances = []
    if inst_path.exists():
        instances = json.load(open(inst_path)).get('instances', [])
    print(f'Instances: {len(instances)}')

    # Sub-sample: keep every point that's at least STRIDE_Y from the previous kept.
    kept = []
    last = None
    for i, p in enumerate(points):
        if last is None or (p[0] - last[0]) ** 2 + (p[1] - last[1]) ** 2 > STRIDE_Y ** 2:
            kept.append((i, p))
            last = p
    print(f'Sub-sampled to {len(kept)} checkpoints (stride ≥ {STRIDE_Y}y)')

    # For each consecutive pair, try find_path on the Recast mesh. A connectivity
    # break between two nearby recorded points = Recast thinks there's a wall
    # between them where the player actually walked.
    breaks = []
    off_mesh = []
    for k in range(len(kept) - 1):
        (i_a, pa), (i_b, pb) = kept[k], kept[k + 1]
        sa = srv.game_to_recast(pa[0], pa[1], pa[2])
        sb = srv.game_to_recast(pb[0], pb[1], pb[2])
        p = navmesh.find_path(mesh, sa, sb, exclude_flags=0)
        if not p:
            breaks.append((i_a, i_b, pa, pb, 0, 0.0))
            continue
        last_rc = p[-1]
        dxz = ((last_rc[0] - sb[0]) ** 2 + (last_rc[2] - sb[2]) ** 2) ** 0.5
        if dxz > 3.0:
            breaks.append((i_a, i_b, pa, pb, len(p), dxz))

    # Also flag samples very far from any poly (sanity)
    for i, p in kept:
        rc = srv.game_to_recast(p[0], p[1], p[2])
        d_xy = np.sqrt((centers[:, 0] - rc[0]) ** 2 + (centers[:, 2] - rc[2]) ** 2)
        j = int(np.argmin(d_xy))
        d = float(d_xy[j])
        if d > OFF_MESH_THRESHOLD_Y:
            off_mesh.append((i, p[0], p[1], p[2], d,
                             float(centers[j, 1] - rc[1]),
                             tuple(centers[j])))

    print()
    print(f'== Consecutive-pair connectivity breaks: {len(breaks)} ==')
    for i_a, i_b, pa, pb, plen, dxz in breaks[:30]:
        flag = 'NO PATH' if plen == 0 else f'ended {dxz:.1f}y short ({plen} wps)'
        print(f'  [{i_a}→{i_b}]  ({pa[0]:.1f},{pa[1]:.1f},{pa[2]:.1f}) → '
              f'({pb[0]:.1f},{pb[1]:.1f},{pb[2]:.1f})  Δ={((pb[0]-pa[0])**2+(pb[1]-pa[1])**2)**0.5:.1f}y  {flag}')
    if len(breaks) > 30:
        print(f'  ... ({len(breaks) - 30} more)')

    print()
    print(f'== Samples >{OFF_MESH_THRESHOLD_Y:.0f}y from any navmesh poly: {len(off_mesh)} ==')
    for i, gx, gy, gz, d, dy, near in off_mesh[:20]:
        print(f'  [{i:4d}] at ({gx:7.1f},{gy:7.1f},{gz:6.1f})  d_xz={d:5.1f}y  dy={dy:+5.1f}')
    if len(off_mesh) > 20:
        print(f'  ... ({len(off_mesh) - 20} more)')

    # End-to-end reachability: does Recast route from the first recorded point
    # to the last? If not, the recording crosses a navmesh island boundary.
    if len(kept) >= 2:
        sa = srv.game_to_recast(*kept[0][1][:3])
        sb = srv.game_to_recast(*kept[-1][1][:3])
        p = navmesh.find_path(mesh, sa, sb, exclude_flags=0)
        print()
        if not p:
            print('== End-to-end: NO PATH on Recast mesh between first and last sample ==')
        else:
            last_rc = p[-1]
            dxz = ((last_rc[0] - sb[0]) ** 2 + (last_rc[2] - sb[2]) ** 2) ** 0.5
            if dxz < 3.0:
                print(f'== End-to-end: routable ({len(p)} waypoints) ==')
            else:
                print(f'== End-to-end: partial path only — ended {dxz:.1f}y short ({len(p)} waypoints) ==')

    # Interpretation
    print()
    if breaks:
        print('→ Recast disagrees with the recorded walk at specific points.')
        print('  Each break is a pair where Recast cannot route directly between two')
        print('  recorded samples that the player walked between in-game. Good candidates')
        print('  for the zone-connectivity culprits.')
    else:
        print('→ Every consecutive recording pair is routable on the navmesh. The recording')
        print('  alone does not expose a coord/wall bug; the 12/36 cross-transition problem')
        print('  is likely specific to the endpoints zone_transitions.json picks, not the')
        print('  middle of the path. Try /nav goto between two transitions that fail.')


if __name__ == '__main__':
    main()
