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

Targets follow Ashita's <…> placeholder convention; we pass them
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


SCHEMA_VERSION = 1

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
