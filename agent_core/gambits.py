"""Gambit schema, validator, and command-file writer.

A gambit list is the AST the orchestrator deploys to the combat addon
via `commands/<character>/combat.json`. The combat addon (Lua) walks
the AST against live world state at ~5 Hz and fires the action of
the first matching gambit whose cooldown has elapsed.

The schema is deliberately structural JSON (no DSL string parsing)
so the LLM can produce it directly via tool-calling and we can
validate without a parser. See docs/agent-architecture.md "combat".

File shape on disk:

    {
      "seq": 1234567890,                    # monotonic; addon ignores stale seqs
      "version": 1,
      "gambits": [
        {
          "id":       "g_low_hp",           # stable id; cooldowns key off this
          "priority": 1,                     # lower fires first; ties broken by list order
          "cooldown": 5.0,                   # seconds; 0 = no cooldown
          "trigger":  <expr>,                # see _validate_expr
          "action":   <action>               # see _validate_action
        },
        ...
      ]
    }

Expression nodes:

    {"op": "lit",  "value": <number|string|bool>}
    {"op": "ref",  "path": "self.hp_pct"}              # dotted path into world state
    {"op": "and",  "args": [<expr>, ...]}              # at least one arg
    {"op": "or",   "args": [<expr>, ...]}
    {"op": "not",  "a":   <expr>}
    {"op": "lt"|"lte"|"gt"|"gte"|"eq"|"ne",
                   "a":   <expr>, "b": <expr>}
    {"op": "in",   "needle": <expr>, "haystack": <expr>}  # value in list

Action nodes:

    {"kind": "ability",     "name": "Sneak Attack",  "target": "<t>"}
    {"kind": "magic",       "name": "Cure III",      "target": "<p1>"}
    {"kind": "weaponskill", "name": "Spirits Within","target": "<t>"}
    {"kind": "engage"}                                 # /attack on
    {"kind": "disengage"}                              # /attack off
    {"kind": "raw",         "command": "/echo hello"}  # literal command line

Targets follow Ashita's <...> placeholder convention; we pass them
through verbatim so any standard target token (<me>, <t>, <p0>..<p5>,
<bt>, <ft>, <stnpc>, <stpc>) is supported.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config as _config
from . import events as _events
from . import persistence as _persistence


SCHEMA_VERSION = 1


# Standard FFXI job IDs ↔ 3-letter codes used in context keys. We pin
# these here (rather than reading from a Lua-side table) because the
# context key has to be stable across processes and characters.
JOB_NAMES: dict[int, str] = {
    0: 'NON',  1: 'WAR',  2: 'MNK',  3: 'WHM',  4: 'BLM',
    5: 'RDM',  6: 'THF',  7: 'PLD',  8: 'DRK',  9: 'BST',
    10: 'BRD', 11: 'RNG', 12: 'SAM', 13: 'NIN', 14: 'DRG',
    15: 'SMN', 16: 'BLU', 17: 'COR', 18: 'PUP', 19: 'DNC',
    20: 'SCH', 21: 'GEO', 22: 'RUN',
}
_JOB_NAME_TO_ID: dict[str, int] = {v: k for k, v in JOB_NAMES.items()}
WILDCARD = '*'
_PARTY_VALUES = ('solo', 'party')

# Allowed expression operators. Comparison ops take {a, b}; logical
# binary ops take {args}; not takes {a}; in takes {needle, haystack}.
_BINARY_CMP = {'lt', 'lte', 'gt', 'gte', 'eq', 'ne'}
_NARY_LOGICAL = {'and', 'or'}
_LEAF_OPS = {'lit', 'ref'}
_ACTION_KINDS = {'ability', 'magic', 'weaponskill', 'engage', 'disengage', 'raw'}


class GambitValidationError(ValueError):
    """Raised when a gambit list fails structural validation. The
    message lists every error found (we collect, not fail-fast, so the
    LLM gets one round of feedback instead of N retries)."""


def _validate_expr(expr: Any, errors: list[str], path: str) -> None:
    if not isinstance(expr, dict):
        errors.append(f'{path}: expression must be an object, got {type(expr).__name__}')
        return
    op = expr.get('op')
    if op == 'lit':
        v = expr.get('value')
        if not isinstance(v, (int, float, str, bool)):
            errors.append(f'{path}: lit.value must be number/string/bool')
        return
    if op == 'ref':
        p = expr.get('path')
        if not isinstance(p, str) or not p:
            errors.append(f'{path}: ref.path must be a non-empty string')
        return
    if op in _BINARY_CMP:
        if 'a' not in expr or 'b' not in expr:
            errors.append(f'{path}: {op} requires fields a, b')
            return
        _validate_expr(expr['a'], errors, f'{path}.a')
        _validate_expr(expr['b'], errors, f'{path}.b')
        return
    if op in _NARY_LOGICAL:
        args = expr.get('args')
        if not isinstance(args, list) or not args:
            errors.append(f'{path}: {op}.args must be a non-empty list')
            return
        for i, a in enumerate(args):
            _validate_expr(a, errors, f'{path}.args[{i}]')
        return
    if op == 'not':
        if 'a' not in expr:
            errors.append(f'{path}: not requires field a')
            return
        _validate_expr(expr['a'], errors, f'{path}.a')
        return
    if op == 'in':
        if 'needle' not in expr or 'haystack' not in expr:
            errors.append(f'{path}: in requires needle, haystack')
            return
        _validate_expr(expr['needle'], errors, f'{path}.needle')
        _validate_expr(expr['haystack'], errors, f'{path}.haystack')
        return
    errors.append(f'{path}: unknown op {op!r}')


def _validate_action(action: Any, errors: list[str], path: str) -> None:
    if not isinstance(action, dict):
        errors.append(f'{path}: action must be an object')
        return
    kind = action.get('kind')
    if kind not in _ACTION_KINDS:
        errors.append(f'{path}: unknown kind {kind!r} (allowed: {sorted(_ACTION_KINDS)})')
        return
    if kind in ('ability', 'magic', 'weaponskill'):
        if not isinstance(action.get('name'), str):
            errors.append(f'{path}: {kind} requires string `name`')
        # target is optional for magic/ws (default <t>); ability often
        # implies <me>. The Lua side fills in defaults.
        if 'target' in action and not isinstance(action['target'], str):
            errors.append(f'{path}: target must be a string token like "<t>"')
    elif kind == 'raw':
        if not isinstance(action.get('command'), str):
            errors.append(f'{path}: raw requires a string `command`')
    # engage / disengage take no parameters.


def _validate_gambit(g: Any, errors: list[str], idx: int) -> None:
    base = f'gambits[{idx}]'
    if not isinstance(g, dict):
        errors.append(f'{base}: must be an object')
        return
    if not isinstance(g.get('id'), str) or not g['id']:
        errors.append(f'{base}.id must be a non-empty string')
    if 'priority' in g and not isinstance(g['priority'], (int, float)):
        errors.append(f'{base}.priority must be a number')
    if 'cooldown' in g and not isinstance(g['cooldown'], (int, float)):
        errors.append(f'{base}.cooldown must be a number')
    # Floor the cooldown at 0.5s. A zero-cooldown gambit fires every
    # 100ms tick; if the action is something with no in-game cooldown
    # (e.g., /attack off, /echo) the result is hundreds of duplicate
    # commands per second and chat is unusable. Real abilities have
    # their own recast timers, but the gambit cooldown is our backstop
    # against runaway loops.
    cd = g.get('cooldown')
    if isinstance(cd, (int, float)) and cd < 0.5:
        g['cooldown'] = 0.5
    if 'trigger' not in g:
        errors.append(f'{base}.trigger is required')
    else:
        _validate_expr(g['trigger'], errors, f'{base}.trigger')
    if 'action' not in g:
        errors.append(f'{base}.action is required')
    else:
        _validate_action(g['action'], errors, f'{base}.action')


def validate(gambits: list[dict[str, Any]]) -> None:
    """Validate a flat gambit list. Raises GambitValidationError with
    every error found if the list is malformed."""
    errors: list[str] = []
    if not isinstance(gambits, list):
        raise GambitValidationError('gambits must be a list')
    seen_ids: set[str] = set()
    for i, g in enumerate(gambits):
        _validate_gambit(g, errors, i)
        gid = g.get('id') if isinstance(g, dict) else None
        if isinstance(gid, str):
            if gid in seen_ids:
                errors.append(f'gambits[{i}].id duplicate: {gid!r}')
            seen_ids.add(gid)
    if errors:
        raise GambitValidationError('\n'.join(errors))


# -----------------------------------------------------------------------
# Context keys + resolver
# -----------------------------------------------------------------------

def _normalise_job(value: Any) -> str:
    """Coerce a job field (int id, 3-letter code, '*' or None) to a
    canonical 3-letter code or WILDCARD. Unknown values fall back to
    WILDCARD so a malformed planner call broadens rather than mismatches."""
    if value is None:
        return WILDCARD
    if isinstance(value, str):
        s = value.strip().upper()
        if not s or s == WILDCARD:
            return WILDCARD
        if s in _JOB_NAME_TO_ID:
            return s
        return WILDCARD
    if isinstance(value, (int, float)):
        return JOB_NAMES.get(int(value), WILDCARD)
    return WILDCARD


def _normalise_party(value: Any) -> str:
    """Coerce party field to 'solo' / 'party' / WILDCARD."""
    if value is None:
        return WILDCARD
    if isinstance(value, bool):
        return 'party' if value else 'solo'
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _PARTY_VALUES:
            return s
        if s in (WILDCARD, ''):
            return WILDCARD
    return WILDCARD


def context_key_from_dict(ctx: dict[str, Any] | None) -> str:
    """Build the canonical "main/sub/party" string for a context dict.
    Missing or unknown fields become WILDCARD ('*'), so passing `{}`
    yields the universal fallback "*/*/*"."""
    ctx = ctx or {}
    main = _normalise_job(ctx.get('main_job'))
    sub  = _normalise_job(ctx.get('sub_job'))
    party = _normalise_party(ctx.get('in_party') if 'in_party' in ctx else ctx.get('party'))
    return f'{main}/{sub}/{party}'


def context_from_combat(combat_state: dict[str, Any] | None) -> dict[str, str]:
    """Read the live context (main_job, sub_job, in_party) from the addon's
    combat.json payload. `party` includes self in slot 0, so any second
    member counts as "in party." Returns a dict of canonical strings."""
    s = (combat_state or {}).get('self') or {}
    party_list = (combat_state or {}).get('party') or []
    return {
        'main_job': _normalise_job(s.get('main_job')),
        'sub_job':  _normalise_job(s.get('sub_job')),
        'in_party': 'party' if len(party_list) > 1 else 'solo',
    }


def _key_matches(set_key: str, ctx: dict[str, str]) -> bool:
    """A stored key matches if every concrete (non-WILDCARD) segment
    equals the current ctx value. Wildcard segments always match."""
    parts = set_key.split('/')
    if len(parts) != 3:
        return False
    cur = (ctx['main_job'], ctx['sub_job'], ctx['in_party'])
    for sk, cv in zip(parts, cur):
        if sk == WILDCARD:
            continue
        if sk != cv:
            return False
    return True


def _specificity(set_key: str) -> int:
    """0..3 - count of concrete (non-wildcard) segments. Used to order
    matching sets so most-specific wins on id collisions."""
    return sum(1 for p in set_key.split('/') if p != WILDCARD)


def resolve(sets: dict[str, list[dict[str, Any]]],
            ctx: dict[str, str]) -> list[dict[str, Any]]:
    """Pick every set whose key matches `ctx`, merge them (more-specific
    overrides less-specific by gambit id), and return the merged list
    sorted by priority (lower fires first; ties by insertion order).

    Merge semantics: a gambit `id` defined in a more-specific set
    completely replaces any same-id gambit from a broader one. This
    lets a `*/*/*` baseline ("cure self when low") coexist with a
    job-specific addition without the LLM having to re-emit the
    baseline gambits every time it tweaks a job."""
    matching = sorted(
        ((k, v) for k, v in sets.items() if _key_matches(k, ctx)),
        key=lambda kv: _specificity(kv[0]),
    )
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for _, gambits in matching:
        for g in gambits:
            gid = g.get('id')
            if not isinstance(gid, str):
                continue
            if gid not in by_id:
                order.append(gid)
            by_id[gid] = g  # later (more-specific) wins
    merged = [by_id[gid] for gid in order]
    merged.sort(key=lambda g: (g.get('priority', 0), order.index(g['id'])))
    return merged


# -----------------------------------------------------------------------
# Store mutation + active deployment
# -----------------------------------------------------------------------

def store_path(cfg: _config.Config) -> Path:
    return cfg.paths.persistent_dir(cfg.character) / 'gambits.json'


def update_set(cfg: _config.Config, store: _persistence.Gambits,
               ctx: dict[str, Any] | None,
               gambits: list[dict[str, Any]]) -> str:
    """Validate + replace one set keyed by `ctx`. Persists the store.
    Does NOT redeploy - caller does that with the *current* live ctx.
    Returns the canonical key written."""
    validate(gambits)
    key = context_key_from_dict(ctx)
    store.sets[key] = list(gambits)
    store.save(store_path(cfg))
    return key


def clear_set(cfg: _config.Config, store: _persistence.Gambits,
              ctx: dict[str, Any] | None) -> str | None:
    """Remove one set by `ctx`. Returns the key removed, or None if
    the set didn't exist."""
    key = context_key_from_dict(ctx)
    if key not in store.sets:
        return None
    del store.sets[key]
    store.save(store_path(cfg))
    return key


_MUTABLE_GAMBIT_FIELDS = {'priority', 'cooldown', 'trigger', 'action'}


def add_gambit(cfg: _config.Config, store: _persistence.Gambits,
               ctx: dict[str, Any] | None,
               gambit: dict[str, Any]) -> str:
    """Append one gambit to the set keyed by `ctx`. Validates the new
    gambit; rejects if its id is already present in the same set.
    Persists. Returns the canonical key written."""
    if not isinstance(gambit, dict):
        raise GambitValidationError('gambit must be an object')
    gid = gambit.get('id')
    if not isinstance(gid, str) or not gid:
        raise GambitValidationError('gambit.id must be a non-empty string')
    key = context_key_from_dict(ctx)
    existing = list(store.sets.get(key, []))
    if any(g.get('id') == gid for g in existing):
        raise GambitValidationError(
            f'gambit id {gid!r} already exists in {key!r}; '
            f'use modify_gambit to change it'
        )
    candidate = existing + [gambit]
    validate(candidate)
    store.sets[key] = candidate
    store.save(store_path(cfg))
    return key


def modify_gambit(cfg: _config.Config, store: _persistence.Gambits,
                  ctx: dict[str, Any] | None,
                  gambit_id: str,
                  patch: dict[str, Any]) -> str:
    """Merge top-level fields from `patch` into the gambit identified by
    `gambit_id` within the set keyed by `ctx`. Patch may contain any of
    {priority, cooldown, trigger, action}; each replaces its top-level
    field wholesale (no deep merge - partial trigger/action ASTs are
    not validatable). Persists. Returns the canonical key."""
    if not isinstance(gambit_id, str) or not gambit_id:
        raise GambitValidationError('gambit_id must be a non-empty string')
    if not isinstance(patch, dict) or not patch:
        raise GambitValidationError('patch must be a non-empty object')
    bad = set(patch) - _MUTABLE_GAMBIT_FIELDS
    if bad:
        raise GambitValidationError(
            f'patch may only contain {sorted(_MUTABLE_GAMBIT_FIELDS)}; '
            f'got unexpected fields: {sorted(bad)}'
        )
    key = context_key_from_dict(ctx)
    existing = store.sets.get(key)
    if not existing:
        raise GambitValidationError(f'no gambit set at {key!r}')
    new_list: list[dict[str, Any]] = []
    found = False
    for g in existing:
        if g.get('id') == gambit_id:
            merged = {**g, **patch}
            new_list.append(merged)
            found = True
        else:
            new_list.append(g)
    if not found:
        raise GambitValidationError(
            f'gambit id {gambit_id!r} not found in {key!r}'
        )
    validate(new_list)
    store.sets[key] = new_list
    store.save(store_path(cfg))
    return key


def remove_gambit(cfg: _config.Config, store: _persistence.Gambits,
                  ctx: dict[str, Any] | None,
                  gambit_id: str) -> str | None:
    """Remove one gambit from the set keyed by `ctx`. If that empties
    the set, drop the set entirely (so the store doesn't accumulate
    empty contexts). Persists. Returns the key removed-from, or None
    if no matching gambit existed."""
    key = context_key_from_dict(ctx)
    existing = store.sets.get(key)
    if not existing:
        return None
    new_list = [g for g in existing if g.get('id') != gambit_id]
    if len(new_list) == len(existing):
        return None
    if new_list:
        store.sets[key] = new_list
    else:
        del store.sets[key]
    store.save(store_path(cfg))
    return key


def deploy_active(cfg: _config.Config, store: _persistence.Gambits,
                  current_ctx: dict[str, str]) -> list[dict[str, Any]]:
    """Resolve the gambit list for `current_ctx`, write it to the
    addon's command file. Returns the resolved list."""
    merged = resolve(store.sets, current_ctx)
    deploy(cfg, merged)
    return merged


def deploy(cfg: _config.Config, gambits: list[dict[str, Any]]) -> Path:
    """Validate the list, write `commands/<character>/combat.json` atomically,
    log a `gambits_deployed` event. Returns the file path written."""
    validate(gambits)
    path = cfg.paths.commands_dir(cfg.character) / 'combat.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'seq': int(time.time() * 1000),
        'version': SCHEMA_VERSION,
        'gambits': gambits,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        # mkstemp creates 0600; the Lua addon may need 0644 under Wine.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _events.append(
        cfg.paths.events_file(),
        character=cfg.character,
        source='gambits',
        type_='gambits_deployed',
        seq=payload['seq'],
        count=len(gambits),
    )
    return path
