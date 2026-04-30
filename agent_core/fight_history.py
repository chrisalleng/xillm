"""Per-mob fight outcomes - written by farming.py, read by the engage
judge. Lives at `persistent/<character>/fight_history/<zone_id>.json`,
keyed by mob name (not server_id - the LLM judge wants priors per mob
*type*, not per spawn instance).

Schema per mob name:
    {
      "kill_count":         int,
      "death_count":        int,
      "hp_remaining_sum":   float,   # for averaging - self HP% at kill
      "hp_remaining_n":     int,
      "last_engaged_at":    float,   # unix ts of most recent kill OR death
      "last_killed_at":     float | None,
      "last_died_at":       float | None
    }

The agent_core writes are atomic (tempfile + os.replace) so a crash
mid-write can't corrupt the file. Read-on-demand - the file is small
enough (one zone x tens of mob names) that we don't bother with an
in-process cache.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config as _config


def _path(cfg: _config.Config, zone_id: int) -> Path:
    return cfg.paths.persistent_dir(cfg.character) / 'fight_history' / f'{zone_id}.json'


def load(cfg: _config.Config, zone_id: int) -> dict[str, dict[str, Any]]:
    p = _path(cfg, zone_id)
    if not p.exists():
        return {}
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(cfg: _config.Config, zone_id: int, data: dict[str, dict[str, Any]]) -> None:
    p = _path(cfg, zone_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _empty_record() -> dict[str, Any]:
    return {
        'kill_count':       0,
        'death_count':      0,
        'hp_remaining_sum': 0.0,
        'hp_remaining_n':   0,
        # Damage_taken_pct = engage_start_hp_pct - hp_at_kill (or 100
        # for deaths). More stable than hp_remaining for assessing
        # fight risk, because it isolates damage from THIS fight rather
        # than rolling resting/healing HP into the average.
        'damage_taken_sum': 0.0,
        'damage_taken_n':   0,
        'last_engaged_at':  None,
        'last_killed_at':   None,
        'last_died_at':     None,
    }


def record_kill(cfg: _config.Config, zone_id: int, name: str,
                hp_remaining_pct: float | None,
                damage_taken_pct: float | None = None,
                ts: float | None = None) -> None:
    if not name:
        return
    if ts is None:
        ts = time.time()
    data = load(cfg, zone_id)
    rec = data.get(name) or _empty_record()
    rec['kill_count'] = int(rec.get('kill_count') or 0) + 1
    rec['last_engaged_at'] = ts
    rec['last_killed_at'] = ts
    if hp_remaining_pct is not None:
        rec['hp_remaining_sum'] = float(rec.get('hp_remaining_sum') or 0.0) + float(hp_remaining_pct)
        rec['hp_remaining_n']   = int(rec.get('hp_remaining_n') or 0) + 1
    if damage_taken_pct is not None:
        rec['damage_taken_sum'] = float(rec.get('damage_taken_sum') or 0.0) + float(damage_taken_pct)
        rec['damage_taken_n']   = int(rec.get('damage_taken_n') or 0) + 1
    data[name] = rec
    _save(cfg, zone_id, data)


def record_death(cfg: _config.Config, zone_id: int, name: str,
                 damage_taken_pct: float | None = None,
                 ts: float | None = None) -> None:
    if not name:
        return
    if ts is None:
        ts = time.time()
    data = load(cfg, zone_id)
    rec = data.get(name) or _empty_record()
    rec['death_count'] = int(rec.get('death_count') or 0) + 1
    rec['last_engaged_at'] = ts
    rec['last_died_at'] = ts
    # A death is full damage dealt to the player - record 100% if no
    # explicit measurement, or the actual delta if we know engage_start.
    if damage_taken_pct is not None:
        rec['damage_taken_sum'] = float(rec.get('damage_taken_sum') or 0.0) + float(damage_taken_pct)
        rec['damage_taken_n']   = int(rec.get('damage_taken_n') or 0) + 1
    else:
        rec['damage_taken_sum'] = float(rec.get('damage_taken_sum') or 0.0) + 100.0
        rec['damage_taken_n']   = int(rec.get('damage_taken_n') or 0) + 1
    data[name] = rec
    _save(cfg, zone_id, data)


def summary(cfg: _config.Config, zone_id: int, name: str) -> dict[str, Any]:
    """Compact view for the LLM judge prompt. Returns zeros for unknown
    mobs so the judge can distinguish 'never fought' (k=0, d=0) from
    'fought and survived' vs 'fought and died.'"""
    rec = load(cfg, zone_id).get(name) or _empty_record()
    n = int(rec.get('hp_remaining_n') or 0)
    avg_hp = (float(rec.get('hp_remaining_sum') or 0.0) / n) if n > 0 else None
    dn = int(rec.get('damage_taken_n') or 0)
    avg_dmg = (float(rec.get('damage_taken_sum') or 0.0) / dn) if dn > 0 else None
    return {
        'kill_count':           int(rec.get('kill_count') or 0),
        'death_count':          int(rec.get('death_count') or 0),
        'avg_hp_remaining_pct': avg_hp,
        'avg_damage_taken_pct': avg_dmg,
        'last_engaged_at':      rec.get('last_engaged_at'),
        'last_killed_at':       rec.get('last_killed_at'),
        'last_died_at':         rec.get('last_died_at'),
    }
