#!/usr/bin/env python3
"""
Navigation server for FFXI mapper addon.

Builds Recast navmeshes from collision data and provides pathfinding.
Communicates with the Lua addon via JSON files:
  - nav_request.json: Lua writes goto requests
  - nav_path.json: server writes waypoint paths
"""

import json
import os
import sys
import time
import numpy as np
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / 'recast_wrapper' / 'build'))
import navmesh

sys.stdout.reconfigure(line_buffering=True)

COLLISION_DIR = SCRIPT_DIR.parent / 'mapper' / 'data' / 'collision'
OBSTACLE_DIR = SCRIPT_DIR.parent / 'mapper' / 'data' / 'obstacles'
IPC_DIR = Path('/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/mapper')
REQUEST_FILE = IPC_DIR / 'nav_request.json'
PATH_FILE = IPC_DIR / 'nav_path.json'
POLL_INTERVAL = 0.1


class NavServer:
    def __init__(self):
        self.meshes: dict[int, object] = {}
        self.obstacles: dict[int, list] = {}
        self.last_request_mtime = 0

    def load_collision(self, zone_id: int):
        path = COLLISION_DIR / f'{zone_id}.json'
        if not path.exists():
            raise FileNotFoundError(f'No collision data for zone {zone_id}')

        with open(path) as f:
            data = json.load(f)

        verts_raw = np.array(data['vertices'], dtype=np.float32)
        tris = np.array(data['triangles'], dtype=np.int32)

        # MZB → Recast (Y-up): (MZB.x, MZB.z, -MZB.y)
        verts = np.column_stack([
            verts_raw[:, 0],
            verts_raw[:, 2],
            -verts_raw[:, 1]
        ]).astype(np.float32)

        # Fix winding order
        tris_fixed = tris.copy()
        tris_fixed[:, 1], tris_fixed[:, 2] = tris[:, 2].copy(), tris[:, 1].copy()

        return verts, tris_fixed

    def get_mesh(self, zone_id: int):
        if zone_id not in self.meshes:
            print(f'Building navmesh for zone {zone_id}...')
            t0 = time.time()
            verts, tris = self.load_collision(zone_id)
            settings = navmesh.NavSettings()
            settings.cell_size = 0.20
            settings.cell_height = 0.12
            settings.agent_radius = 1.5
            settings.agent_max_slope = 40.0
            settings.agent_max_climb = 1.0
            settings.region_min_size = 2
            settings.region_merge_size = 20
            self.meshes[zone_id] = navmesh.build_navmesh(verts, tris, settings)
            print(f'  Built in {time.time()-t0:.1f}s')
        return self.meshes[zone_id]

    def load_obstacles(self, zone_id: int):
        if zone_id not in self.obstacles:
            path = OBSTACLE_DIR / f'{zone_id}.json'
            if path.exists():
                with open(path) as f:
                    self.obstacles[zone_id] = json.load(f)
            else:
                self.obstacles[zone_id] = []
        return self.obstacles[zone_id]

    def save_obstacles(self, zone_id: int):
        OBSTACLE_DIR.mkdir(parents=True, exist_ok=True)
        with open(OBSTACLE_DIR / f'{zone_id}.json', 'w') as f:
            json.dump(self.obstacles[zone_id], f)

    def add_obstacle(self, zone_id: int, x, y, z):
        obstacles = self.load_obstacles(zone_id)
        for obs in obstacles:
            if ((obs[0] - x)**2 + (obs[1] - y)**2) ** 0.5 < 2.0:
                return
        obstacles.append([round(x, 1), round(y, 1), round(z, 1)])
        self.save_obstacles(zone_id)
        print(f'  Stored obstacle at ({x:.1f}, {y:.1f}) for zone {zone_id} ({len(obstacles)} total)')

    def game_to_recast(self, x, y, z):
        """Game coords → Recast coords. Game: (x, y, z) where z=elevation. Recast: Y-up."""
        # game.x = MZB.x, game.y = -MZB.y, game.z = -MZB.z
        # Recast = (MZB.x, MZB.z, -MZB.y) = (game.x, -game.z, game.y)
        return (x, -z, y)

    def recast_to_game(self, rx, ry, rz):
        """Recast coords → Game coords."""
        # Inverse: game.x = recast.x, game.y = recast.z, game.z = -recast.y
        return (rx, rz, -ry)

    def _path_to_waypoints(self, path_rc, max_segment=1.0):
        raw = []
        for p in path_rc:
            gx, gy, gz = self.recast_to_game(p[0], p[1], p[2])
            raw.append([gx, gy, gz])

        if len(raw) < 2:
            return [[round(v, 2) for v in p] for p in raw]

        waypoints = [raw[0]]
        for i in range(1, len(raw)):
            prev = raw[i - 1]
            curr = raw[i]
            dx, dy, dz = curr[0]-prev[0], curr[1]-prev[1], curr[2]-prev[2]
            dist = (dx*dx + dy*dy) ** 0.5
            if dist > max_segment:
                steps = int(dist / max_segment)
                for s in range(1, steps + 1):
                    t = s / (steps + 1)
                    waypoints.append([
                        prev[0] + dx*t,
                        prev[1] + dy*t,
                        prev[2] + dz*t,
                    ])
            waypoints.append(curr)

        return [[round(v, 2) for v in p] for p in waypoints]

    def _end_dist_2d(self, waypoints, tx, ty):
        if not waypoints:
            return float('inf')
        last = waypoints[-1]
        return ((last[0] - tx)**2 + (last[1] - ty)**2) ** 0.5

    def _avoid_obstacles(self, waypoints, zone_id):
        obstacles = self.load_obstacles(zone_id)
        if not obstacles:
            return waypoints

        AVOID_RADIUS = 3.0
        PUSH_DIST = 4.0

        for obs in obstacles:
            ox, oy = obs[0], obs[1]
            new_wps = []
            for wp in waypoints:
                dx = wp[0] - ox
                dy = wp[1] - oy
                d = (dx*dx + dy*dy) ** 0.5
                if d < AVOID_RADIUS and d > 0.01:
                    nx, ny = dx / d, dy / d
                    new_wps.append([round(ox + nx * PUSH_DIST, 2), round(oy + ny * PUSH_DIST, 2), wp[2]])
                else:
                    new_wps.append(wp)
            waypoints = new_wps

        return waypoints

    def find_path(self, zone_id, start_game, end_game):
        mesh = self.get_mesh(zone_id)
        start_rc = self.game_to_recast(*start_game)
        end_rc = self.game_to_recast(*end_game)
        tx, ty = end_game[0], end_game[1]

        path_rc = navmesh.find_path(mesh, start_rc, end_rc)
        best = self._path_to_waypoints(path_rc)
        best_dist = self._end_dist_2d(best, tx, ty)

        if best_dist > 5.0:
            centers = navmesh.get_poly_centers(mesh)
            candidate_elevs = set()
            for c in centers:
                gx, gy = c[0], c[2]  # recast x,z -> game x,y
                if (gx - tx)**2 + (gy - ty)**2 < 10**2:
                    candidate_elevs.add(round(-c[1], 1))  # recast y → game z = -recast_y

            for game_z in sorted(candidate_elevs):
                end_try = self.game_to_recast(tx, ty, game_z)
                path_try = navmesh.find_path(mesh, start_rc, end_try)
                wps = self._path_to_waypoints(path_try)
                d = self._end_dist_2d(wps, tx, ty)
                if d < best_dist:
                    best = wps
                    best_dist = d
                if best_dist < 5.0:
                    break

        return self._avoid_obstacles(best, zone_id)

    def handle_request(self, req):
        action = req.get('action')
        zone_id = req.get('zone_id')
        seq = req.get('seq')

        if action == 'goto':
            new_obs = req.get('new_obstacle')
            if new_obs and zone_id:
                self.add_obstacle(zone_id, new_obs[0], new_obs[1], new_obs[2])

            player = req['player']
            target = req['target']
            print(f'[#{seq}] goto zone={zone_id} from=({player[0]:.1f}, {player[1]:.1f}) to=({target[0]:.1f}, {target[1]:.1f})')

            try:
                waypoints = self.find_path(
                    zone_id,
                    (player[0], player[1], player[2]),
                    (target[0], target[1], target[2])
                )

                if not waypoints:
                    self.write_response({'status': 'no_path', 'zone_id': zone_id, 'seq': seq})
                    print(f'  No path found')
                    return

                last = waypoints[-1]
                dx = last[0] - target[0]
                dy = last[1] - target[1]
                end_dist = (dx*dx + dy*dy) ** 0.5
                partial = end_dist > 10.0

                if partial:
                    print(f'  Partial: {len(waypoints)} wps, {end_dist:.0f}y short')
                else:
                    print(f'  OK: {len(waypoints)} wps')

                self.write_response({
                    'status': 'partial' if partial else 'ok',
                    'zone_id': zone_id,
                    'waypoints': waypoints,
                    'end_dist': round(end_dist, 1),
                    'seq': seq,
                })
            except Exception as e:
                print(f'  Error: {e}')
                self.write_response({'status': 'error', 'message': str(e), 'zone_id': zone_id, 'seq': seq})

        elif action == 'report_obstacle':
            pos = req.get('position', [0, 0, 0])
            if zone_id:
                self.add_obstacle(zone_id, pos[0], pos[1], pos[2])

        elif action == 'clear_cache':
            zone = req.get('zone_id')
            if zone and zone in self.meshes:
                del self.meshes[zone]
                print(f'Cleared cache for zone {zone}')
            elif not zone:
                self.meshes.clear()
                print('Cleared all caches')

    def write_response(self, data):
        tmp = str(PATH_FILE) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, PATH_FILE)

    def poll(self):
        if not REQUEST_FILE.exists():
            return

        try:
            with open(REQUEST_FILE) as f:
                req = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        req_seq = req.get('seq')
        if req_seq is None or req_seq == self.last_request_mtime:
            return

        self.last_request_mtime = req_seq
        try:
            self.handle_request(req)
        except Exception as e:
            print(f'Error handling request: {e}')
            self.write_response({'status': 'error', 'message': str(e), 'timestamp': time.time()})

    VERSION = '.1'

    def run(self):
        print(f'Nav server v{self.VERSION} started. Watching {REQUEST_FILE}')
        print(f'Collision data: {COLLISION_DIR}')
        try:
            while True:
                self.poll()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print('\nShutting down.')


if __name__ == '__main__':
    server = NavServer()

    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        zone = int(sys.argv[2]) if len(sys.argv) > 2 else 107
        sx, sy, sz = 221.0, -434.0, 59.8
        tx, ty, tz = 350.0, -400.0, 20.0
        if len(sys.argv) > 4:
            tx, ty = float(sys.argv[3]), float(sys.argv[4])
            tz = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

        waypoints = server.find_path(zone, (sx, sy, sz), (tx, ty, tz))
        print(f'{len(waypoints)} waypoints:')
        for i, w in enumerate(waypoints):
            print(f'  [{i}] ({w[0]}, {w[1]}, {w[2]})')
    else:
        server.run()
