"""LSB menu catalog extractor.

One-shot tool: walks a LandSandBoat (LSB) checkout and produces a static
JSON catalog of every deterministic menu the agent might encounter -
home point destinations, conquest outpost warps, vendor stocks,
survival guides. The catalog is keyed by menu KIND (so the same
overseer NPC in any city resolves to the same outpost-warp catalog)
plus per-NPC entries where the menu is NPC-specific (vendor stocks).

Why static extraction at build time, not live memory reads:
  - Outpost / home-point / vendor menus are 100% determined by
    server-side data (the client only renders what the server tells
    it). Mining LSB gives us the source of truth.
  - LSB Lua tables follow consistent formatting; regex parsing is
    sufficient and avoids embedding a Lua interpreter.
  - The result is a small JSON file checked into the agent's
    persistent dir; runtime cost is one-shot load + dict lookups.

What's NOT mined here:
  - Quest dialog options. Those branch on event state and are not
    pure data. Handled via the LSB-script-resolver: when menu_judge
    fires for a non-cataloged menu, we hand the relevant Lua source
    (scripts/zones/.../<npc>.lua, scripts/quests/..., scripts/missions/...)
    to the LLM and let it read the option semantics.

Invocation:
    python -m agent_core.lsb_extract \
        --lsb /home/chris/workspace/server \
        --out persistent/menu_catalog.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Enum table parser
# ---------------------------------------------------------------------------

# LSB enum format:
#   xi.zone =
#   {
#       SOUTHERN_SAN_DORIA              = 230,
#       NORTHERN_SAN_DORIA              = 231,
#       ...
#   }
_ENUM_LINE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)\s*,?\s*(?:--.*)?$')


def parse_enum(path: Path) -> dict[str, int]:
    """Parse an LSB enum file into {NAME: value}. Tracks `{`/`}`
    nesting so the LSB style of putting the opening brace on its
    own line works. Reads the first depth-1 block whose body
    contains `NAME = number,` rows."""
    out: dict[str, int] = {}
    saw_xi = False
    depth = 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            if not saw_xi:
                if 'xi.' in line and '=' in line:
                    saw_xi = True
                continue
            # Update depth tracking on this line BEFORE attempting
            # to parse rows, so a closing `}` doesn't get matched
            # as a row.
            opens  = line.count('{')
            closes = line.count('}')
            if depth == 0 and opens > 0:
                depth += opens - closes
                continue  # the opening-brace line itself has no rows
            depth += opens - closes
            if depth <= 0 and out:
                break
            m = _ENUM_LINE.match(line)
            if m:
                out[m.group(1)] = int(m.group(2))
    return out


# ---------------------------------------------------------------------------
# Home points
# ---------------------------------------------------------------------------

# Pattern matching one row of homepointData:
#   [  0] = { group = 1, fee = 1, dest = {  -85.554,       1, -64.554,  45, xi.zone.SOUTHERN_SAN_DORIA     } }, -- Southern San d'Oria #1
_HP_ROW = re.compile(
    r'\[\s*(\d+)\s*\]\s*=\s*\{\s*'
    r'group\s*=\s*(\d+)\s*,\s*'
    r'fee\s*=\s*(-?[\d.]+)\s*,\s*'
    r'dest\s*=\s*\{\s*'
    r'(-?[\d.]+)\s*,\s*'
    r'(-?[\d.]+)\s*,\s*'
    r'(-?[\d.]+)\s*,\s*'
    r'(-?[\d.]+)\s*,\s*'
    r'xi\.zone\.([A-Z0-9_]+)'
    r'\s*\}\s*\}'
)


def parse_homepoints(homepoint_lua: Path) -> list[dict[str, Any]]:
    """Read scripts/globals/homepoint.lua's `homepointData` table.
    Returns a list of {index, zone_name, x, y, z, rot, group, fee_mult, label}.
    The label comes from the trailing `-- ` comment which IS stable
    across the LSB repo's history."""
    rows: list[dict[str, Any]] = []
    text = homepoint_lua.read_text(encoding='utf-8')
    for line in text.splitlines():
        m = _HP_ROW.search(line)
        if not m:
            continue
        idx, group, fee, x, y, z, rot, zone_name = m.groups()
        # Pull the trailing comment as a human label.
        label = ''
        c = line.find('--')
        if c >= 0:
            # Skip past the second `--` if any (header separators
            # like ----------- look like comments too); we want the
            # last comment on the line, which is the row label.
            tail = line[c + 2:].strip()
            label = tail
        rows.append({
            'index':    int(idx),
            'zone_name': zone_name,
            'x':        float(x),
            'y':        float(y),
            'z':        float(z),
            'rot':      float(rot),
            'group':    int(group),
            'fee_mult': float(fee),
            'label':    label,
        })
    return rows


# ---------------------------------------------------------------------------
# Conquest outposts (per-region warp destinations)
# ---------------------------------------------------------------------------

# The outposts table is keyed by xi.region.<NAME>, e.g.:
#   [xi.region.RONFAURE] = { zone = xi.zone.WEST_RONFAURE, ki = ..., cp = 10, lvl = 10, fee = 100 },
_OUTPOST_ROW = re.compile(
    r'\[\s*xi\.region\.([A-Z0-9_]+)\s*\]\s*=\s*\{\s*'
    r'zone\s*=\s*xi\.zone\.([A-Z0-9_]+)\s*,'
    r'(?:[^}]*?ki\s*=\s*xi\.ki\.([A-Z0-9_]+)\s*,)?'
    r'(?:[^}]*?cp\s*=\s*(-?\d+)\s*,)?'
    r'(?:[^}]*?lvl\s*=\s*(-?\d+)\s*,)?'
    r'(?:[^}]*?fee\s*=\s*(-?\d+)\s*,?)?'
    r'\s*\}'
)


def parse_outposts(conquest_lua: Path) -> list[dict[str, Any]]:
    """Pull the `local outposts =` table from conquest.lua. Each
    entry has {region_name, zone_name, key_item?, cp?, level?, fee?}."""
    text = conquest_lua.read_text(encoding='utf-8')
    rows: list[dict[str, Any]] = []
    for m in _OUTPOST_ROW.finditer(text):
        region, zone, ki, cp, lvl, fee = m.groups()
        rows.append({
            'region_name': region,
            'zone_name':   zone,
            'key_item':    ki,
            'cp':          int(cp) if cp else None,
            'level_req':   int(lvl) if lvl else None,
            'fee':         int(fee) if fee else None,
        })
    return rows


# ---------------------------------------------------------------------------
# Vendor stocks (per-NPC)
# ---------------------------------------------------------------------------

# Match a `local stock = { ... }` block, which is the canonical shape
# every vendor NPC uses (Brunhilde, Balthild, etc.). We capture the
# NPC's zone from the file path and pair stock rows with item ids.
_STOCK_ROW = re.compile(
    r'\{\s*xi\.item\.([A-Z0-9_]+)\s*,\s*(\d+)\s*(?:,\s*(\d+))?\s*\}'
)


def parse_vendor_stock(npc_lua: Path) -> list[dict[str, Any]]:
    """Extract every vendor stock row from one NPC script. Vendor
    NPCs that aren't shops will return []. Order matches the menu
    order the client renders (LSB iterates the array as-given)."""
    text = npc_lua.read_text(encoding='utf-8')
    # Quick reject if no stock / shop pattern present.
    if 'stock' not in text and 'addShopItem' not in text:
        return []
    items: list[dict[str, Any]] = []
    for m in _STOCK_ROW.finditer(text):
        item_name, price, fame_min = m.groups()
        items.append({
            'index':    len(items),
            'item_name': item_name,
            'price':    int(price),
            'fame_min': int(fame_min) if fame_min else None,
        })
    return items


def walk_vendors(zones_dir: Path,
                 zone_name_to_id: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Walk every zones/<zone>/npcs/*.lua, extract stock arrays.
    Returns {<ZONE_NAME>::<NPC_Name>: {zone_id, stock: [...]}}."""
    out: dict[str, dict[str, Any]] = {}
    for zone_path in sorted(zones_dir.iterdir()):
        if not zone_path.is_dir():
            continue
        zone_name_raw = zone_path.name  # e.g. "Bastok_Markets"
        zone_name_upper = zone_name_raw.upper().replace('-', '_')
        zone_id = zone_name_to_id.get(zone_name_upper)
        if zone_id is None:
            continue
        npcs_dir = zone_path / 'npcs'
        if not npcs_dir.is_dir():
            continue
        for npc_lua in sorted(npcs_dir.glob('*.lua')):
            stock = parse_vendor_stock(npc_lua)
            if not stock:
                continue
            key = f'{zone_name_upper}::{npc_lua.stem}'
            out[key] = {
                'zone_id':   zone_id,
                'zone_name': zone_name_upper,
                'npc_name':  npc_lua.stem,
                'stock':     stock,
            }
    return out


# ---------------------------------------------------------------------------
# Top-level extract
# ---------------------------------------------------------------------------

def extract(lsb_root: Path) -> dict[str, Any]:
    region_enum = parse_enum(lsb_root / 'scripts/enum/region.lua')
    zone_enum   = parse_enum(lsb_root / 'scripts/enum/zone.lua')

    # Resolve enum-name references in the parsed tables to numeric ids.
    homepoints = parse_homepoints(lsb_root / 'scripts/globals/homepoint.lua')
    for hp in homepoints:
        hp['zone_id'] = zone_enum.get(hp['zone_name'])

    outposts = parse_outposts(lsb_root / 'scripts/globals/conquest.lua')
    for op in outposts:
        op['region_id'] = region_enum.get(op['region_name'])
        op['zone_id']   = zone_enum.get(op['zone_name'])

    vendors = walk_vendors(lsb_root / 'scripts/zones', zone_enum)

    return {
        'lsb_source':  str(lsb_root),
        'enums': {
            'region': region_enum,
            'zone':   zone_enum,
        },
        'homepoints':         homepoints,
        'conquest_outposts':  outposts,
        'vendors':            vendors,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--lsb', default='/home/chris/workspace/server',
                   help='LSB checkout root.')
    p.add_argument('--out', required=True,
                   help='Output JSON path.')
    args = p.parse_args()

    catalog = extract(Path(args.lsb))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, indent=2, sort_keys=True),
                        encoding='utf-8')
    print(f'Wrote {out_path}')
    print(f'  zones:      {len(catalog["enums"]["zone"])}')
    print(f'  regions:    {len(catalog["enums"]["region"])}')
    print(f'  homepoints: {len(catalog["homepoints"])}')
    print(f'  outposts:   {len(catalog["conquest_outposts"])}')
    print(f'  vendors:    {len(catalog["vendors"])}')


if __name__ == '__main__':
    main()
