"""Per-character zone safety overrides.

Stored at:
    <ipc_base>/persistent/<character>/zone_safety.json

Schema:
    {
        "overrides": {
            "<zone_id>": {
                "verdict": "safe" | "dangerous" | "default",
                "reason":  "<short text>",
                "ts":      <unix epoch float>
            },
            ...
        }
    }

Two writers:
  - The planner LLM via the `set_zone_safety` tool (deliberative
    decision: "WAR75 won't get aggroed in zone X").
  - The death reflex: on KO in an OUTDOORS / SIGNET zone, auto-mark
    `dangerous` with the killer name in `reason`. CITY / DUNGEON
    deaths are skipped because their type already drives the right
    routing weight (CITY=0 means we'd never avoid them; DUNGEON=100
    is already high). INSTANCED / DYNAMIS deaths happen inside event
    instances and don't affect cross-zone travel.

Routing reads happen via `nav_router.Router` which loads this file
fresh on construction.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


VALID_VERDICTS = {'safe', 'dangerous', 'default'}


def _safety_path(persistent_dir: Path) -> Path:
    return persistent_dir / 'zone_safety.json'


def load(persistent_dir: Path) -> dict[str, Any]:
    """Load the per-character overrides. Missing file -> empty
    overrides (the in-memory shape always has the `overrides` key
    so callers don't need to None-check)."""
    p = _safety_path(persistent_dir)
    if not p.exists():
        return {'overrides': {}}
    try:
        return json.loads(p.read_text(encoding='utf-8')) or {'overrides': {}}
    except (OSError, ValueError):
        return {'overrides': {}}


def save(persistent_dir: Path, data: dict[str, Any]) -> None:
    """Atomic write: tmp file + rename. Avoids the router seeing a
    half-written file if it reload()s mid-write."""
    p = _safety_path(persistent_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(p)


def set_verdict(persistent_dir: Path,
                zone_id: int,
                verdict: str,
                reason: str = '') -> dict[str, Any]:
    """Upsert one zone's safety verdict. Returns the new override
    record. `verdict` must be in VALID_VERDICTS; "default" removes
    the override (falls back to the type-based base cost)."""
    if verdict not in VALID_VERDICTS:
        raise ValueError(
            f"verdict must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}"
        )
    data = load(persistent_dir)
    overrides = data.setdefault('overrides', {})
    key = str(int(zone_id))
    if verdict == 'default':
        overrides.pop(key, None)
        record = {'verdict': 'default', 'reason': reason, 'ts': time.time()}
    else:
        record = {
            'verdict': verdict,
            'reason':  (reason or '')[:200],
            'ts':      time.time(),
        }
        overrides[key] = record
    save(persistent_dir, data)
    return record


def get_verdict(persistent_dir: Path, zone_id: int) -> dict[str, Any] | None:
    """Returns the override record for zone_id, or None if no
    override is set (i.e. the routing default applies)."""
    data = load(persistent_dir)
    return (data.get('overrides') or {}).get(str(int(zone_id)))


def all_overrides(persistent_dir: Path) -> dict[int, dict[str, Any]]:
    """All current overrides keyed by int zone_id."""
    data = load(persistent_dir)
    out: dict[int, dict[str, Any]] = {}
    for k, v in (data.get('overrides') or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Death reflex
# ---------------------------------------------------------------------------

# Zone types where a death is meaningful for cross-zone routing. In
# CITY zones the player shouldn't die from monsters at all (PvP /
# scripted deaths only); DUNGEON / DYNAMIS / INSTANCED zones are
# already heavily-weighted in the router or unreachable for transit
# entirely. For OUTDOORS / SIGNET (outpost outdoor) deaths, marking
# dangerous tells the planner to route around this zone next time.
DEATH_REFLEX_ZONE_TYPES = {'OUTDOORS', 'SIGNET'}


def auto_mark_on_death(persistent_dir: Path,
                       zone_id: int,
                       zone_type_name: str,
                       died_to: str | None,
                       player_level: int | None) -> dict[str, Any] | None:
    """Implements the "I died here" reflex. Returns the new override
    record if marked, None if skipped (zone type doesn't qualify or
    an override was already set).

    Idempotent-ish: if the LLM has explicitly marked this zone safe
    we DO NOT auto-overwrite. The LLM's deliberate verdict beats the
    reflex - the player may have died for an unrelated reason (DD
    pull mistake, lag, mismatched gear) that doesn't change the
    zone's level-suitability."""
    if zone_type_name not in DEATH_REFLEX_ZONE_TYPES:
        return None
    existing = get_verdict(persistent_dir, zone_id)
    if existing and existing.get('verdict') == 'safe':
        # LLM explicitly decided this zone is safe at our level; don't
        # second-guess it on a single death. The next death-marker
        # update through the planner will be deliberate.
        return None
    reason_parts = []
    if died_to:
        reason_parts.append(f'killed by {died_to}')
    if player_level is not None:
        reason_parts.append(f'lvl {player_level}')
    reason = '; '.join(reason_parts) or 'died here'
    return set_verdict(persistent_dir, zone_id, 'dangerous', reason)
