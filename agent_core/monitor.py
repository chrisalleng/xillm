#!/usr/bin/env python3
"""Monitor nav client position and path progress in real-time."""
import json, time, os, sys

IPC_DIR = '/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/xillm'
STATUS = os.path.join(IPC_DIR, 'nav_status.json')
PATH = os.path.join(IPC_DIR, 'nav_path.json')
REQUEST = os.path.join(IPC_DIR, 'nav_request.json')

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None

def dist2d(ax, ay, bx, by):
    return ((ax-bx)**2 + (ay-by)**2)**0.5

last_status = None
last_x, last_y = None, None

print("Monitoring nav client... (Ctrl+C to stop)")
while True:
    status = read_json(STATUS)
    path_data = read_json(PATH)

    if status and status != last_status:
        x, y, z = status.get('x', 0), status.get('y', 0), status.get('z', 0)
        moving = status.get('moving', False)
        wp_idx = status.get('wp_idx', 0)
        wp_total = status.get('wp_total', 0)

        # Distance moved since last update
        moved = ""
        if last_x is not None:
            d = dist2d(x, y, last_x, last_y)
            moved = f" moved={d:.1f}"

        # Distance to current waypoint
        wp_info = ""
        if moving and path_data and path_data.get('waypoints'):
            wps = path_data['waypoints']
            if 0 < wp_idx <= len(wps):
                wp = wps[wp_idx - 1]
                d_wp = dist2d(x, y, wp[0], wp[1])
                wp_info = f" -> wp[{wp_idx}/{wp_total}]=({wp[0]:.0f},{wp[1]:.0f}) dist={d_wp:.1f}"

        state = "MOVING" if moving else "idle"
        print(f"[{state}] pos=({x:.1f}, {y:.1f}, {z:.1f}){moved}{wp_info}")

        last_status = status
        last_x, last_y = x, y

    time.sleep(0.5)
