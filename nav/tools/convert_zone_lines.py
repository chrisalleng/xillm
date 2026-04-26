#!/usr/bin/env python3
"""One-shot converter: zone_lines.lua + zonelines.sql → zone_transitions.json

Usage: python convert_zone_lines.py zone_lines.lua zonelines.sql zone_transitions.json
"""
import json
import re
import sys

lua_text = open(sys.argv[1]).read()
sql_text = open(sys.argv[2]).read()

# Parse zone names from lua
names = {}
for m in re.finditer(r'\[(\d+)\]="([^"]+)"', lua_text):
    names[m.group(1)] = m.group(2)

# Parse SQL entries keyed by (fromzone, tozone) → list of (tox, toy, toz)
sql_dests = {}
for m in re.finditer(
    r'VALUES\s*\((\d+),(\d+),(\d+),([-\d.]+),([-\d.]+),([-\d.]+),(\d+)\)', sql_text
):
    key = (int(m.group(2)), int(m.group(3)))
    sql_dests.setdefault(key, []).append(
        (float(m.group(4)), float(m.group(5)), float(m.group(6)))
    )

# Parse transitions from lua
transitions = {}
current_zone = None
lines_section = lua_text.split('lines = {', 1)
if len(lines_section) < 2:
    lines_section = lua_text.split('lines={', 1)
zone_text = lines_section[1] if len(lines_section) > 1 else lua_text

for line in zone_text.split('\n'):
    zm = re.match(r'\[(\d+)\]=\{', line.strip())
    if zm:
        current_zone = int(zm.group(1))
    if current_zone is not None:
        for tm in re.finditer(r'\{to=(\d+),x=([-\d.]+),y=([-\d.]+)\}', line):
            to_zone = int(tm.group(1))
            lx = float(tm.group(2))
            ly = float(tm.group(3))

            # Get elevation from reverse SQL entry (B→A destination ≈ A→B trigger)
            z = 0.0
            rev_entries = sql_dests.get((to_zone, current_zone), [])
            best_dist = float('inf')
            for sx, sy, sz in rev_entries:
                d = ((sx - lx) ** 2 + (sz + ly) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    z = sy

            entry = {'to': to_zone, 'x': lx, 'y': ly}
            if best_dist < 30:
                entry['z'] = round(z, 1)

            transitions.setdefault(str(current_zone), []).append(entry)

out = {'names': names, 'transitions': transitions}
json.dump(out, open(sys.argv[3], 'w'), indent=1)

total = sum(len(v) for v in transitions.values())
with_z = sum(1 for v in transitions.values() for t in v if 'z' in t)
print(f'{len(names)} zones, {total} transitions ({with_z} with elevation)')
