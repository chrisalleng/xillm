#!/usr/bin/env python3
"""One-shot converter: zone_lines.lua → zone_transitions.json"""
import json
import re
import sys

lua_text = open(sys.argv[1]).read()

names = {}
for m in re.finditer(r'\[(\d+)\]="([^"]+)"', lua_text):
    names[m.group(1)] = m.group(2)

transitions = {}
zone_block_re = re.compile(r'^\[(\d+)\]=\{$(.*?)^\}', re.MULTILINE | re.DOTALL)
trans_re = re.compile(r'\{to=(\d+),x=([-\d.]+),y=([-\d.]+)\}')

lines_section = lua_text.split('lines = {', 1)
if len(lines_section) < 2:
    lines_section = lua_text.split('lines={', 1)
zone_text = lines_section[1] if len(lines_section) > 1 else lua_text

for m in zone_block_re.finditer(zone_text):
    zone_id = m.group(1)
    block = m.group(2)
    trans = []
    for t in trans_re.finditer(block):
        trans.append({
            'to': int(t.group(1)),
            'x': float(t.group(2)),
            'y': float(t.group(3)),
        })
    if trans:
        transitions[zone_id] = trans

out = {'names': names, 'transitions': transitions}
json.dump(out, open(sys.argv[2], 'w'), indent=1)
print(f'{len(names)} zones, {sum(len(v) for v in transitions.values())} transitions')
