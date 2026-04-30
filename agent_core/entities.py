"""Entity record loader - reads the nav addon's per-zone entity files.

The nav addon tracks every mob/NPC/object it observes per zone and
writes it to `<ipc_base>/persistent/<character>/entities/<zone>.json`
- PER-CHARACTER, not shared. Each agent has its own private memory of
the world, which is what supports the planned chat-mediated knowledge-
sharing (one character literally tells another about a mob and that
fact propagates as an `import` rather than as observation). Each record:

    {
      "<server_id>": {
        "name":          "Huge Hornet",
        "type":          1,                # 1 mob, 2 npc, 16 player...
        "server_id":     17211814,
        "center_x":      ..,
        "center_y":      ..,
        "center_z":      ..,
        "min_x":         .., "max_x": ..,
        "min_y":         .., "max_y": ..,
        "wander_radius": ..,
        "tod_hours":     [..],            # spawn-time-of-day data
        "weather_ids":   [..],
        "sightings":     <int>            # how many times we've seen it
      }
    }

These are aggregated over many sightings - the agent uses them as
"this mob has been seen here, somewhere in the wander radius." For a
farm goal we only need the live position of the spawn we're heading to;
the records are good enough as a starting waypoint.

This module is read-only - the nav addon owns writes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config as _config


def _records_path(cfg: _config.Config, zone_id: int) -> Path:
    return cfg.paths.persistent_dir(cfg.character) / 'entities' / f'{zone_id}.json'


def load_records(cfg: _config.Config, zone_id: int) -> dict[str, dict[str, Any]]:
    """Load the raw record map for one zone. Returns {} on missing or
    malformed file - caller can treat absence as 'never observed.'"""
    path = _records_path(cfg, zone_id)
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def find_mobs(cfg: _config.Config, zone_id: int, name: str,
              *, exclude_types: tuple[int, ...] = (16,)) -> list[dict[str, Any]]:
    """Return every record in the zone whose `name` matches the given
    string (case-insensitive substring). By default excludes type 16
    (players) so a mob name that happens to overlap a player's nick
    doesn't get returned. Mob types vary across Ashita versions (we've
    seen 1 and 2 both used for hostile mobs) so we exclude rather than
    include - safer default."""
    records = load_records(cfg, zone_id)
    needle = name.strip().lower()
    if not needle:
        return []
    out: list[dict[str, Any]] = []
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        if rec.get('type') in exclude_types:
            continue
        n = (rec.get('name') or '').lower()
        if needle in n:
            out.append(rec)
    return out


def closest_mob(cfg: _config.Config, zone_id: int, name: str,
                player_x: float, player_y: float) -> dict[str, Any] | None:
    """Pick the mob record nearest to the player's current position. The
    director uses this to decide which spawn to head to first - closer
    means less travel, and given multiple spawns of the same name the
    geometry is the only differentiator we have."""
    candidates = find_mobs(cfg, zone_id, name)
    if not candidates:
        return None
    best = None
    best_d = float('inf')
    for r in candidates:
        cx = r.get('center_x')
        cy = r.get('center_y')
        if cx is None or cy is None:
            continue
        dx = cx - player_x
        dy = cy - player_y
        d2 = dx * dx + dy * dy
        if d2 < best_d:
            best_d = d2
            best = r
    return best
