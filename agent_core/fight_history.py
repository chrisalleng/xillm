"""Per-mob, per-player-level fight outcomes - written by farming.py,
read by the engage judge. Lives at
`persistent/<character>/fight_history/<zone_id>.json`.

Schema (top-level keyed by mob name; per-name keyed by player level
the fight happened at, as a string for JSON safety):

    {
      "Wild Rabbit": {
        "6": {                          # we were level 6 when this happened
          "check_type":            66,    # 0x42 = Easy Prey at lvl 6
          "kill_count":            int,
          "death_count":           int,
          "hp_remaining_sum":      float, # self HP% at kill
          "hp_remaining_n":        int,
          "damage_taken_sum":      float, # engage_start_hp - hp_at_end
          "damage_taken_n":        int,
          "max_damage_taken_pct":  float, # worst single fight
          "last_engaged_at":       float,
          "last_killed_at":        float | None,
          "last_died_at":          float | None
        }
      }
    }

Why per-level: a mob that one-shot us at level 4 may be trivial at
level 8. The judge needs damage stats that apply to the agent's
CURRENT level, not aggregated across the whole career.

Why we also track check_type: it's the same difficulty tier across
mobs. A death to one Even-Match mob is signal that all Even-Match
mobs at this level are risky, even ones we've never fought. The
`tier_summary()` helper aggregates kill/death/damage across all mobs
of a given check_type at a given level so the judge can see "I've
died 3 times to Even-Match mobs at level 6, regardless of which
specific mob it was". As the character gets stronger, what counts as
Even-Match shifts and the per-level aggregate self-corrects.

Old (level-agnostic) records are migrated on read into a synthetic
'unknown' level bucket - they still exist but don't count toward
the current-level summary.
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


def _migrate_record(rec: Any) -> dict[str, dict[str, Any]]:
    """Old shape (pre-level-keyed) had stats directly under the mob
    name. Wrap that into a synthetic 'unknown' level bucket so the
    new code can still read it without losing data outright."""
    if not isinstance(rec, dict):
        return {}
    if 'kill_count' in rec:
        return {'unknown': rec}
    return rec


def load(cfg: _config.Config, zone_id: int) -> dict[str, dict[str, dict[str, Any]]]:
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
    return {name: _migrate_record(rec) for name, rec in data.items()}


def _save(cfg: _config.Config, zone_id: int, data: dict[str, dict[str, dict[str, Any]]]) -> None:
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
        'check_type':           None,
        'kill_count':           0,
        'death_count':          0,
        'hp_remaining_sum':     0.0,
        'hp_remaining_n':       0,
        # Rolling window of damage_taken_pct values from recent fights
        # (kills + deaths). Bounded to RECENT_DAMAGE_WINDOW entries so
        # old fights age out naturally as the player improves at the
        # same level (better gear, sub-job leveled, etc.). All damage
        # stats - avg, max - are computed from THIS list, never from
        # all-time accumulators. A historical 50%% from early in the
        # session won't haunt the agent forever.
        'recent_damage_taken':  [],
        'last_engaged_at':      None,
        'last_killed_at':       None,
        'last_died_at':         None,
    }


# How many recent damage values to keep per (mob, level) record.
# Large enough to smooth out variance, small enough that a steady
# improvement curve replaces old high values within a session of
# normal farming (~20 fights at this size).
RECENT_DAMAGE_WINDOW = 20


def _level_key(player_level: int | None) -> str:
    if player_level is None:
        return 'unknown'
    return str(int(player_level))


def _get_level_record(data: dict[str, dict[str, dict[str, Any]]],
                      name: str,
                      player_level: int | None) -> dict[str, Any]:
    by_level = data.get(name)
    if not isinstance(by_level, dict):
        by_level = {}
        data[name] = by_level
    key = _level_key(player_level)
    rec = by_level.get(key)
    if not isinstance(rec, dict):
        rec = _empty_record()
        by_level[key] = rec
    # Backfill missing fields on legacy records so callers don't need
    # to defend against missing keys.
    for k, v in _empty_record().items():
        rec.setdefault(k, v)
    return rec


def _push_damage(rec: dict[str, Any], dmg: float) -> None:
    """Append a damage_taken_pct value to the rolling window, evicting
    the oldest entry if at capacity. This is the only place that
    grows the window; readers compute avg/max from it directly."""
    lst = rec.get('recent_damage_taken')
    if not isinstance(lst, list):
        lst = []
    lst.append(float(dmg))
    if len(lst) > RECENT_DAMAGE_WINDOW:
        # Drop oldest. We slice rather than pop(0) so the underlying
        # storage is always a fresh list - JSON-safe.
        lst = lst[-RECENT_DAMAGE_WINDOW:]
    rec['recent_damage_taken'] = lst


def record_kill(cfg: _config.Config, zone_id: int, name: str,
                player_level: int | None,
                hp_remaining_pct: float | None,
                damage_taken_pct: float | None = None,
                check_type: int | None = None,
                ts: float | None = None) -> None:
    if not name:
        return
    if ts is None:
        ts = time.time()
    data = load(cfg, zone_id)
    rec = _get_level_record(data, name, player_level)
    if check_type is not None:
        rec['check_type'] = int(check_type)
    rec['kill_count'] = int(rec.get('kill_count') or 0) + 1
    rec['last_engaged_at'] = ts
    rec['last_killed_at'] = ts
    if hp_remaining_pct is not None:
        rec['hp_remaining_sum'] = float(rec.get('hp_remaining_sum') or 0.0) + float(hp_remaining_pct)
        rec['hp_remaining_n']   = int(rec.get('hp_remaining_n') or 0) + 1
    if damage_taken_pct is not None:
        _push_damage(rec, float(damage_taken_pct))
    _save(cfg, zone_id, data)


def record_death(cfg: _config.Config, zone_id: int, name: str,
                 player_level: int | None,
                 damage_taken_pct: float | None = None,
                 check_type: int | None = None,
                 ts: float | None = None) -> None:
    if not name:
        return
    if ts is None:
        ts = time.time()
    data = load(cfg, zone_id)
    rec = _get_level_record(data, name, player_level)
    if check_type is not None:
        rec['check_type'] = int(check_type)
    rec['death_count'] = int(rec.get('death_count') or 0) + 1
    rec['last_engaged_at'] = ts
    rec['last_died_at'] = ts
    dmg = float(damage_taken_pct) if damage_taken_pct is not None else 100.0
    _push_damage(rec, dmg)
    _save(cfg, zone_id, data)


def summary(cfg: _config.Config, zone_id: int, name: str,
            player_level: int | None) -> dict[str, Any]:
    """Stats for THIS character vs `name` AT THE GIVEN LEVEL. The
    level filter is critical: a fight at level 4 against Ding Bat
    tells you nothing about the same mob at level 8. Returns zeros
    for unseen-at-this-level so the caller can distinguish 'never
    fought at this level' from 'fought and survived' vs 'fought
    and died'."""
    by_level = load(cfg, zone_id).get(name) or {}
    if not isinstance(by_level, dict):
        by_level = {}
    rec = by_level.get(_level_key(player_level)) or _empty_record()
    n = int(rec.get('hp_remaining_n') or 0)
    avg_hp = (float(rec.get('hp_remaining_sum') or 0.0) / n) if n > 0 else None
    # Damage stats come from the rolling window only - intentional, so
    # old high-water marks fade as the player improves at this level.
    recent = rec.get('recent_damage_taken')
    if isinstance(recent, list) and recent:
        avg_dmg = sum(recent) / len(recent)
        max_dmg = max(recent)
    else:
        avg_dmg = None
        max_dmg = None
    return {
        'kill_count':           int(rec.get('kill_count') or 0),
        'death_count':          int(rec.get('death_count') or 0),
        'avg_hp_remaining_pct': avg_hp,
        'avg_damage_taken_pct': avg_dmg,
        'max_damage_taken_pct': max_dmg,
        'last_engaged_at':      rec.get('last_engaged_at'),
        'last_killed_at':       rec.get('last_killed_at'),
        'last_died_at':         rec.get('last_died_at'),
    }


# How fast tier-history records decay as the player levels away from
# them. weight = max(0, 1 - LEVEL_DECAY_PER_LEVEL * abs(distance)).
# At 0.25, level 5 data carries weight 0.75 at level 6, 0.5 at level 7,
# 0.25 at level 8, gone by level 9. So a level-5 even-match death still
# influences level-6 risk assessment (less so), and naturally fades as
# the character outgrows that tier without ever being "forgotten" abruptly.
LEVEL_DECAY_PER_LEVEL = 0.25


def tier_summary(cfg: _config.Config, zone_id: int,
                 check_type: int | None,
                 player_level: int | None) -> dict[str, Any]:
    """Aggregate stats across ALL mobs of the given check_type, weighted
    by how close each record's level is to the player's current level.

    The point: a death to an Even-Match mob at level 5 should still
    influence the level-6 engage decision (we're not THAT much
    stronger), but should weight less than a level-6 death. As the
    character levels up, old-level data decays smoothly toward zero
    instead of being abruptly forgotten on each ding. Past-level
    deaths to a tier never fully disappear unless we've leveled
    several tiers above them.

    Returns weighted (float) kill/death/damage counts plus an
    unweighted `max_damage_taken_pct` (max is hardline - if we
    actually took 100% damage from this tier once, that fact persists
    even at a higher level)."""
    if check_type is None or player_level is None:
        return {
            'check_type':           check_type,
            'kill_count':           0.0,
            'death_count':          0.0,
            'avg_damage_taken_pct': None,
            'max_damage_taken_pct': None,
            'distinct_mobs':        0,
        }
    data = load(cfg, zone_id)
    kills = 0.0
    deaths = 0.0
    dmg_weighted_sum = 0.0
    dmg_weighted_n = 0.0
    dmg_max = 0.0
    distinct = 0
    have_max = False
    for name, by_level in data.items():
        if not isinstance(by_level, dict):
            continue
        for level_key, rec in by_level.items():
            if not isinstance(rec, dict):
                continue
            if rec.get('check_type') != int(check_type):
                continue
            try:
                rec_level = int(level_key)
            except (TypeError, ValueError):
                continue
            distance = abs(int(player_level) - rec_level)
            weight = max(0.0, 1.0 - LEVEL_DECAY_PER_LEVEL * distance)
            if weight <= 0.0:
                continue
            distinct += 1
            kills += weight * float(rec.get('kill_count') or 0)
            deaths += weight * float(rec.get('death_count') or 0)
            # Damage stats come from the rolling window only.
            recent = rec.get('recent_damage_taken')
            if isinstance(recent, list) and recent:
                for v in recent:
                    dmg_weighted_sum += weight * float(v)
                    dmg_weighted_n += weight
                m = max(recent)
                if m > dmg_max:
                    dmg_max = m
                    have_max = True
    avg_dmg = (dmg_weighted_sum / dmg_weighted_n) if dmg_weighted_n > 0 else None
    return {
        'check_type':           int(check_type),
        'kill_count':           round(kills, 1),
        'death_count':          round(deaths, 1),
        'avg_damage_taken_pct': avg_dmg,
        'max_damage_taken_pct': dmg_max if have_max else None,
        'distinct_mobs':        distinct,
    }
