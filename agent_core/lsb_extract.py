"""LSB menu / NPC / zone catalog extractor.

One-shot tool: walks a LandSandBoat (LSB) checkout and produces JSON
catalogs the agent uses at runtime:

  - menu_catalog.json: deterministic menu KINDS (home points, conquest
    outpost warps, vendor stocks).
  - npcs.json:         every NPC the server knows about, with zone +
    world coordinates + role tags scraped from each NPC's Lua script.
    The planner's `find_npc(role=...)` and `closest_npc(...)` tools
    query this.
  - zone_meta.json:    zone_id -> {name, type (CITY/OUTDOORS/DUNGEON/
    INSTANCED/...), region}. Powers safety-weighted routing in the
    planner.

Why static extraction at build time, not live memory reads:
  - Server data (NPC positions, zone classifications, vendor stocks)
    is 100% server-driven; LSB is the source of truth.
  - LSB SQL files and Lua tables follow consistent formatting; regex
    parsing is sufficient and avoids embedding a Lua interpreter.
  - The results are small JSON files in the agent's persistent dir;
    runtime cost is one-shot load + dict lookups.

What's NOT mined here:
  - Quest dialog options. Those branch on event state and are not
    pure data. Handled via the LSB-script-resolver: when menu_judge
    fires for a non-cataloged menu, we hand the relevant Lua source
    (scripts/zones/.../<npc>.lua, scripts/quests/..., scripts/missions/...)
    to the LLM and let it read the option semantics.

Invocation:
    python -m agent_core.lsb_extract \
        --lsb /home/chris/workspace/lsb-server \
        --out-menu persistent/menu_catalog.json \
        --out-npcs persistent/npcs.json \
        --out-zones persistent/zone_meta.json
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
# NPC list (from sql/npc_list.sql)
# ---------------------------------------------------------------------------

# Each row:
#   INSERT INTO `npc_list` VALUES (
#       npcid, 'lua_name', 'polutils_name',
#       pos_rot, pos_x, pos_y, pos_z,
#       flag, speed, speedsub, animation, animationsub, namevis,
#       status, entityFlags, 0xLOOK,
#       name_prefix, content_tag, widescan
#   );
#
# zone_id encoding: (npcid >> 12) & 0xFFF. Verified empirically -
# Rabid Wolf, I.M. (Bastok Markets, zone 235): npcid=17739828=0x010EB034,
# (0x010EB034 >> 12) & 0xFFF = 0xEB = 235. ✓
_NPC_LIST_ROW = re.compile(
    r"INSERT INTO `npc_list` VALUES \("
    r"(\d+),"                                # npcid
    r"'([^']*)',"                            # lua_name (or 'NPC' / NULL-ish)
    r"(?:'([^']*)'|NULL),"                   # polutils_name (human-readable)
    r"(\d+),"                                # pos_rot
    r"(-?[\d.]+),"                           # pos_x
    r"(-?[\d.]+),"                           # pos_y
    r"(-?[\d.]+),"                           # pos_z
)


def parse_npc_list(npc_list_sql: Path) -> list[dict[str, Any]]:
    """Parse sql/npc_list.sql. Returns one dict per NPC row with
    {id, lua_name, polutils_name, zone_id, x, y, z, rot}.

    Filters out generic placeholders (lua_name == 'NPC') and rows
    with all-zero positions (those are unspawned/template entries that
    just clutter the catalog without being addressable in-world)."""
    out: list[dict[str, Any]] = []
    text = npc_list_sql.read_text(encoding='utf-8')
    for m in _NPC_LIST_ROW.finditer(text):
        npcid     = int(m.group(1))
        lua_name  = m.group(2) or ''
        polu_name = m.group(3) or ''
        rot       = int(m.group(4))
        x, y, z   = float(m.group(5)), float(m.group(6)), float(m.group(7))
        if lua_name == 'NPC' or lua_name.startswith('NPC['):
            continue  # placeholder rows (no real NPC behind them)
        if x == 0.0 and y == 0.0 and z == 0.0:
            continue  # unspawned templates
        zone_id = (npcid >> 12) & 0xFFF
        out.append({
            'id':            npcid,
            'lua_name':      lua_name,
            'name':          polu_name or lua_name,
            'zone_id':       zone_id,
            'x':             x,
            'y':             y,
            'z':             z,
            'rot':           rot,
        })
    return out


# ---------------------------------------------------------------------------
# Zone settings (from sql/zone_settings.sql)
# ---------------------------------------------------------------------------

# Each row:
#   INSERT INTO `zone_settings` VALUES (
#       zoneid, zonetype, 'zoneip', zoneport, 'name',
#       music_day, music_night, battlesolo, battlemulti, restriction,
#       tax, misc
#   );
#
# zonetype encodes xi.zoneType (see scripts/enum/zone_type.lua):
#   1=CITY, 2=OUTDOORS, 4=DUNGEON, 8=SIGNET, 16=SANCTION,
#   32=SIGIL, 64=IONIS, 128=DYNAMIS, 256=INSTANCED. (0=UNKNOWN.)
_ZONE_SETTINGS_ROW = re.compile(
    r"INSERT INTO `zone_settings` VALUES \("
    r"(\d+),"                                # zoneid
    r"(\d+),"                                # zonetype
    r"'[^']*',"                              # zoneip (skip)
    r"\d+,"                                  # zoneport (skip)
    r"'([^']*)',"                            # name
)

ZONE_TYPE_NAMES = {
    0:   'UNKNOWN',
    1:   'CITY',
    2:   'OUTDOORS',
    4:   'DUNGEON',
    8:   'SIGNET',
    16:  'SANCTION',
    32:  'SIGIL',
    64:  'IONIS',
    128: 'DYNAMIS',
    256: 'INSTANCED',
}


def parse_zone_settings(zone_settings_sql: Path) -> dict[int, dict[str, Any]]:
    """Parse sql/zone_settings.sql. Returns {zone_id: {name, type_id,
    type_name}}. type_id is the raw bitfield from the SQL; type_name
    is the human label (UNKNOWN/CITY/OUTDOORS/DUNGEON/...)."""
    out: dict[int, dict[str, Any]] = {}
    text = zone_settings_sql.read_text(encoding='utf-8')
    for m in _ZONE_SETTINGS_ROW.finditer(text):
        zone_id   = int(m.group(1))
        type_id   = int(m.group(2))
        zone_name = m.group(3)
        out[zone_id] = {
            'zone_id':   zone_id,
            'name':      zone_name,
            'type_id':   type_id,
            'type_name': ZONE_TYPE_NAMES.get(type_id, f'TYPE_{type_id}'),
        }
    return out


# ---------------------------------------------------------------------------
# NPC role tagging (from per-NPC Lua scripts)
# ---------------------------------------------------------------------------

# Patterns that identify an NPC's "role" - what kind of dialog/menus
# it offers. Each pattern is a substring search in the NPC's Lua
# script. Multiple matches stack into the role list.
#
# Role tag conventions:
#   - shorthand kebab-style (`conquest_overseer`, not `ConquestOverseer`)
#   - one tag per discrete capability the planner might query for
#   - keep the tag list small and focused on planner-relevant roles
ROLE_PATTERNS: list[tuple[str, str]] = [
    # Conquest officers (Rabid Wolf, I.M., Yoshihiro, I.M., etc.)
    ('conquest_overseer',  'xi.conquest.overseerOnTrigger'),
    # Outpost teleport NPCs (the regional overseers in starter zones)
    ('conquest_outpost',   'xi.conquest.outpostOnTrigger'),
    # Vendor with an explicit shop stock array
    ('vendor',             'xi.shop.general'),
    ('vendor',             'xi.shop.nation'),
    ('vendor',             'addShopItem'),
    # Home points
    ('homepoint',          'xi.homepoint.onTrigger'),
    # Survival guides
    ('survival_guide',     'xi.survivalGuide'),
    # Auction house
    ('auction_house',      'xi.auctionhouse'),
    # Mog house entry
    ('moghouse',           'xi.moghouse'),
    # Trust caster (alter-ego trainer)
    ('trust_master',       'xi.trust'),
    # Adventurer's coupon dispenser
    ('mentor',             'xi.mentor'),
    # Linkshell concierge
    ('linkshell',          'xi.linkshell'),
    # Storage NPCs
    ('storage',            'xi.gobbiebag'),
    ('storage',            'storageSlip'),
    ('storage',            'mogSafe'),
    # Generic dialog/quest NPCs - flagged so the planner can tell
    # them apart from pure decoration. ANY onTrigger handler counts.
    ('quest_npc',          'entity.onTrigger'),
]


def tag_npc_roles(zones_dir: Path,
                  zone_id_to_path_name: dict[int, str]) -> dict[tuple[int, str], list[str]]:
    """Walk every zones/<zone>/npcs/*.lua, return {(zone_id, lua_name):
    [role_tag, ...]} for every NPC whose script matches any role
    pattern. Roles are deduped per NPC; the order reflects the
    pattern table above."""
    out: dict[tuple[int, str], list[str]] = {}
    # Zone path names use underscores AND apostrophes get stripped:
    # e.g. "Carpenters' Landing" in npc_list.sql comments maps to
    # "Carpenters_Landing" on disk. Build a lookup by normalized name.
    path_to_zone_id: dict[str, int] = {}
    for zone_path in zones_dir.iterdir():
        if not zone_path.is_dir():
            continue
        path_to_zone_id[zone_path.name] = -1  # filled in below if matched
    # Match path-name -> zone_id from the cache the caller provides
    for zid, pname in zone_id_to_path_name.items():
        if pname in path_to_zone_id:
            path_to_zone_id[pname] = zid

    for zone_path in sorted(zones_dir.iterdir()):
        if not zone_path.is_dir():
            continue
        zone_id = path_to_zone_id.get(zone_path.name, -1)
        if zone_id < 0:
            continue  # zone path not in the SQL settings (probably a tool dir)
        npcs_dir = zone_path / 'npcs'
        if not npcs_dir.is_dir():
            continue
        for npc_lua in sorted(npcs_dir.glob('*.lua')):
            try:
                text = npc_lua.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            roles: list[str] = []
            for tag, needle in ROLE_PATTERNS:
                if needle in text and tag not in roles:
                    roles.append(tag)
            if roles:
                out[(zone_id, npc_lua.stem)] = roles
    return out


# ---------------------------------------------------------------------------
# Top-level extract
# ---------------------------------------------------------------------------

def extract_menu_catalog(lsb_root: Path,
                         zone_enum: dict[str, int],
                         region_enum: dict[str, int]) -> dict[str, Any]:
    """Build the menu catalog (homepoints, conquest outposts, vendor
    stocks) - the original output of this module."""
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


def extract_zone_meta(lsb_root: Path,
                      zone_enum: dict[str, int],
                      region_enum: dict[str, int]) -> dict[str, Any]:
    """Build the zone-meta catalog: zone_id -> {name, type_name,
    type_id, region}. Powers safety-weighted routing.

    Region is derived from scripts/globals/conquest.lua's regional
    npc table when available; for zones without a region, the field
    is null and the planner treats it as standalone."""
    settings = parse_zone_settings(lsb_root / 'sql/zone_settings.sql')
    zones: dict[str, dict[str, Any]] = {}
    # zone_enum is NAME -> id; we want the inverse to label the SQL rows
    id_to_enum_name = {zid: name for name, zid in zone_enum.items()}
    for zid, row in settings.items():
        zones[str(zid)] = {
            'zone_id':    zid,
            'name':       row['name'],         # SQL form ("Bastok_Markets")
            'enum_name':  id_to_enum_name.get(zid),  # enum form ("BASTOK_MARKETS")
            'type_id':    row['type_id'],
            'type_name':  row['type_name'],
        }
    return {
        'lsb_source': str(lsb_root),
        'zones':      zones,
    }


def extract_npcs(lsb_root: Path,
                 zone_meta: dict[str, Any]) -> dict[str, Any]:
    """Build the NPC catalog: every NPC the server knows about, with
    zone, world coords, and role tags scraped from its Lua script.

    Returns {npcs: [{id, name, lua_name, zone_id, zone_name, x, y, z,
    rot, roles}]}. Sorted by zone_id then by id for stable output."""
    npc_rows = parse_npc_list(lsb_root / 'sql/npc_list.sql')

    # Build zone_id -> SQL path name (used for the per-NPC Lua script
    # location). The SQL "name" column is the on-disk path stem
    # (e.g. "Bastok_Markets").
    zone_id_to_path_name: dict[int, str] = {}
    for zid_str, row in zone_meta['zones'].items():
        zone_id_to_path_name[int(zid_str)] = row['name']

    roles = tag_npc_roles(lsb_root / 'scripts/zones', zone_id_to_path_name)

    npcs: list[dict[str, Any]] = []
    for r in npc_rows:
        zone_id = r['zone_id']
        zone_path_name = zone_id_to_path_name.get(zone_id)
        npc_roles = roles.get((zone_id, r['lua_name']), [])
        npcs.append({
            'id':         r['id'],
            'name':       r['name'],
            'lua_name':   r['lua_name'],
            'zone_id':    zone_id,
            'zone_name':  zone_path_name,
            'x':          r['x'],
            'y':          r['y'],
            'z':          r['z'],
            'rot':        r['rot'],
            'roles':      npc_roles,
        })
    npcs.sort(key=lambda n: (n['zone_id'], n['id']))

    # Index by role for quick planner lookup. Each role -> list of
    # npc record indices.
    role_index: dict[str, list[int]] = {}
    for i, n in enumerate(npcs):
        for tag in n['roles']:
            role_index.setdefault(tag, []).append(i)

    return {
        'lsb_source': str(lsb_root),
        'npcs':       npcs,
        'role_index': role_index,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--lsb', default='/home/chris/workspace/lsb-server',
                   help='LSB checkout root.')
    p.add_argument('--out-menu',  default=None,
                   help='Output path for menu_catalog.json.')
    p.add_argument('--out-npcs',  default=None,
                   help='Output path for npcs.json.')
    p.add_argument('--out-zones', default=None,
                   help='Output path for zone_meta.json.')
    p.add_argument('--out',       default=None,
                   help='LEGACY: write menu_catalog only to this path. '
                        'Prefer --out-menu / --out-npcs / --out-zones.')
    args = p.parse_args()

    lsb_root = Path(args.lsb)

    # Shared enum tables - parsed once and reused across all extracts.
    region_enum = parse_enum(lsb_root / 'scripts/enum/region.lua')
    zone_enum   = parse_enum(lsb_root / 'scripts/enum/zone.lua')

    out_menu  = args.out_menu  or args.out
    out_npcs  = args.out_npcs
    out_zones = args.out_zones

    if not (out_menu or out_npcs or out_zones):
        p.error('at least one of --out-menu / --out-npcs / --out-zones / --out is required')

    if out_menu:
        catalog = extract_menu_catalog(lsb_root, zone_enum, region_enum)
        path = Path(out_menu)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(catalog, indent=2, sort_keys=True),
                        encoding='utf-8')
        print(f'Wrote {path}')
        print(f'  zones:      {len(catalog["enums"]["zone"])}')
        print(f'  regions:    {len(catalog["enums"]["region"])}')
        print(f'  homepoints: {len(catalog["homepoints"])}')
        print(f'  outposts:   {len(catalog["conquest_outposts"])}')
        print(f'  vendors:    {len(catalog["vendors"])}')

    # Zone meta is a dependency of NPC extract (we use it to map
    # zone_id -> on-disk path), so build it whenever NPCs are
    # requested.
    zone_meta = None
    if out_zones or out_npcs:
        zone_meta = extract_zone_meta(lsb_root, zone_enum, region_enum)
        if out_zones:
            path = Path(out_zones)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(zone_meta, indent=2, sort_keys=True),
                            encoding='utf-8')
            print(f'Wrote {path}')
            type_counts: dict[str, int] = {}
            for row in zone_meta['zones'].values():
                type_counts[row['type_name']] = type_counts.get(row['type_name'], 0) + 1
            for tn, count in sorted(type_counts.items()):
                print(f'  {tn:11s} {count}')

    if out_npcs:
        npcs = extract_npcs(lsb_root, zone_meta)
        path = Path(out_npcs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(npcs, indent=2, sort_keys=True),
                        encoding='utf-8')
        print(f'Wrote {path}')
        print(f'  npcs:       {len(npcs["npcs"])}')
        for tag, idxs in sorted(npcs['role_index'].items()):
            print(f'  role/{tag:18s} {len(idxs)}')


if __name__ == '__main__':
    main()
