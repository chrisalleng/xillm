"""Per-character zone-coverage map reader.

The nav addon's `coverage.lua` writes one file per zone the agent has
ever stepped foot in, at:

    <ipc_base>/persistent/<character>/coverage/<zone>.json

Each file:

    {
      "zone_id": <int>,
      "cell_size": <float>,           # 30y default
      "cells": {
        "<cx>:<cy>": {
          "cx": <int>, "cy": <int>,
          "center_x": <float>, "center_y": <float>,
          "visit_count": <int>,
          "first_visited_unix": <int>,
          "last_visited_unix":  <int>,
          "vana_hours_seen": { "<hour>": <count> },
          "last_vana_hour":   <int>
        }, ...
      },
      "ts": <int>
    }

This module is read-only - it doesn't mutate anything on disk. Used
by the farming director's wander state to pick the next destination
("nearest cell I haven't visited at this Vana hour") and to decide
when the zone is genuinely explored ("every cell visited at this
Vana hour"; failure_replan can then escalate to a different zone or
strategy).

Important architecture note: coverage is PER-CHARACTER. Multiple
agents on the same install never share knowledge - they each build
their own world model. A future chat-mediated knowledge-share will
import data with a `source` tag rather than merging maps.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import config as _config


def _path(cfg: _config.Config, zone_id: int) -> Path:
    return cfg.paths.persistent_dir(cfg.character) / 'coverage' / f'{zone_id}.json'


def load(cfg: _config.Config, zone_id: int) -> dict[str, Any]:
    """Read the coverage file for one zone. Returns an empty cells dict
    on missing/malformed file - callers treat absence as 'fully
    unexplored.'"""
    path = _path(cfg, zone_id)
    if not path.exists():
        return {'zone_id': zone_id, 'cell_size': 30.0, 'cells': {}}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {'zone_id': zone_id, 'cell_size': 30.0, 'cells': {}}
    if not isinstance(data, dict):
        return {'zone_id': zone_id, 'cell_size': 30.0, 'cells': {}}
    return data


def cells_unvisited_at_hour(
    cfg: _config.Config, zone_id: int, vana_hour: int,
) -> list[dict[str, Any]]:
    """Return cells the agent HAS been to in this zone, but not at the
    given Vana'diel hour. Unvisited-at-this-hour cells are the right
    re-explore targets for ToD-gated mob discovery: we already know
    the cell is reachable, we just want to see what spawns there at
    a time of day we haven't sampled yet.

    Note: cells the agent has NEVER visited (at any hour) are NOT
    returned here - those come from `cells_never_visited`. Callers
    typically prefer never-visited first, then unvisited-at-hour as
    the fallback."""
    cov = load(cfg, zone_id)
    out: list[dict[str, Any]] = []
    for cell in (cov.get('cells') or {}).values():
        hours = cell.get('vana_hours_seen') or {}
        if str(vana_hour) not in hours:
            out.append(cell)
    return out


def all_cells(cfg: _config.Config, zone_id: int) -> list[dict[str, Any]]:
    cov = load(cfg, zone_id)
    return list((cov.get('cells') or {}).values())


def is_fully_covered(
    cfg: _config.Config, zone_id: int, vana_hour: int,
) -> bool:
    """True if every recorded cell in the zone has been visited at the
    given Vana hour. Cells never visited at all don't count - by
    definition we can't know they exist until we've been there."""
    cov = load(cfg, zone_id)
    cells = (cov.get('cells') or {}).values()
    if not cells:
        # No cells recorded yet - definitionally not "covered."
        return False
    for cell in cells:
        hours = cell.get('vana_hours_seen') or {}
        if str(vana_hour) not in hours:
            return False
    return True


def vana_hour_now() -> int:
    """Compute current Vana'diel hour from real time. Mirrors the
    formula in nav/entities.lua and nav/nav.lua. Vana time runs 25x
    faster than real time, anchored at unix epoch 1009810800."""
    VANA_EPOCH = 1009810800
    import time as _t
    vana_secs = (_t.time() - VANA_EPOCH) * 25
    return int(vana_secs // 3600) % 24


def _dist2(ax: float, ay: float, bx: float, by: float) -> float:
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


def nearest_target(
    cfg: _config.Config,
    zone_id: int,
    current_xy: tuple[float, float],
    vana_hour: int,
    *,
    prefer_unvisited_at_hour: bool = True,
    exclude_cells: set[str] | None = None,
) -> tuple[tuple[float, float], str] | None:
    """Pick the next exploration target.

    Returns ((world_x, world_y), cell_key) or None. The cell_key is
    "cx:cy" for the cell we picked - caller can stash it in a
    blacklist if the goto fails, then pass that blacklist back via
    `exclude_cells` so the next pick skips the unreachable cell.

    Strategy:
      1. **Frontier expansion** - find an unvisited cell adjacent to
         a visited one and head toward its center. This is how the
         agent grows its known area: always step just past the edge.
         If we picked a strictly unvisited far-away cell, the addon
         would have no path data for it; frontier cells are reachable
         because their visited neighbour gives a foothold.
      2. **Re-explore at new hour** - every recorded cell has been
         visited, but not at this hour. Pick the closest such cell
         so we can re-sample for ToD spawns.
      3. **None** - the zone is fully covered at this hour. Caller
         (farming director) treats this as exploration-complete and
         bubbles up to failure_replan."""
    cov = load(cfg, zone_id)
    cells = cov.get('cells') or {}
    cell_size = float(cov.get('cell_size') or 30.0)
    if not cells:
        return None  # nothing visited yet -> can't compute frontier
    cx, cy = current_xy
    blacklist = exclude_cells or set()

    # Step 1: build the frontier - any cell-neighbor key that doesn't
    # exist in `cells` AND is adjacent to a key that does. Pick the
    # one closest to the player. Frontier cells are virtual (no record
    # yet), so we synthesize their center from the grid math.
    visited_keys = set(cells.keys())
    frontier: list[tuple[float, int, int]] = []  # (dist², ncx, ncy)
    for cell in cells.values():
        cell_cx = int(cell.get('cx') or 0)
        cell_cy = int(cell.get('cy') or 0)
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                if ddx == 0 and ddy == 0:
                    continue
                ncx = cell_cx + ddx
                ncy = cell_cy + ddy
                key = f'{ncx}:{ncy}'
                if key in visited_keys or key in blacklist:
                    continue
                # Synthesised cell center from grid coords.
                cx_world = (ncx + 0.5) * cell_size
                cy_world = (ncy + 0.5) * cell_size
                frontier.append((
                    _dist2(cx, cy, cx_world, cy_world),
                    ncx, ncy,
                ))
    if frontier:
        frontier.sort(key=lambda t: t[0])
        _, ncx, ncy = frontier[0]
        return (
            ((ncx + 0.5) * cell_size, (ncy + 0.5) * cell_size),
            f'{ncx}:{ncy}',
        )

    # Step 2: zone fully discovered (no frontier). Look for cells
    # we haven't sampled at THIS Vana hour. Useful for ToD spawns.
    if prefer_unvisited_at_hour:
        candidates: list[tuple[float, float, float, str]] = []
        for key, cell in cells.items():
            if key in blacklist:
                continue
            hours = cell.get('vana_hours_seen') or {}
            if str(vana_hour) not in hours:
                tx = float(cell.get('center_x') or 0)
                ty = float(cell.get('center_y') or 0)
                candidates.append((_dist2(cx, cy, tx, ty), tx, ty, key))
        if candidates:
            candidates.sort(key=lambda t: t[0])
            return ((candidates[0][1], candidates[0][2]), candidates[0][3])

    # Step 3: zone fully covered at this hour (or every remaining
    # candidate is blacklisted as unreachable).
    return None
