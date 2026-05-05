"""Planner-callable tools for NPC discovery and safety-weighted routing.

Exposed alongside web_search/web_fetch in the planner's research phase
so the LLM can answer "which NPC sells X?" by querying the LSB-derived
catalog instead of guessing from training data or scraping wikis.

Tools:

  find_npc(role=, name_contains=, zone_id=, zone_name=, limit=)
    Filter NPCs by role tag (e.g. 'conquest_overseer', 'vendor'),
    name substring, or zone. Returns concise records the LLM can
    pass directly to closest_npc.

  closest_npc(role=, name_contains=, candidate_ids=, from_zone=)
    Rank matching NPCs by safety-weighted route cost from `from_zone`.
    Either filters live (role/name) and ranks the result, or accepts
    a precomputed list of npc ids. Returns the top N with route info.

  list_zones(type=, region=)
    Inspect zone metadata. Useful when the LLM wants to know what
    zones connect or whether a zone is a dungeon vs outdoors.

  find_item(name_contains=, limit=)
    Resolve a human-readable item name to its FFXI item id (and
    stack/sell metadata). Required before any auction-house buy or
    sell goal because the AH packets address items by numeric id,
    not name.

The handlers close over a Router + NPCCatalog so they always reflect
the current per-character zone_safety overrides (the planner reload()s
between sessions).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config as _config
from . import nav_router as _nr
from . import zone_safety as _zs


# ---------------------------------------------------------------------------
# Tool definitions (JSON schemas the LLM sees)
# ---------------------------------------------------------------------------

FIND_NPC_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'find_npc',
        'description': (
            'Look up NPCs in the LSB-derived catalog by role, name, or '
            'zone. Prefer this over web_search when you need an NPC '
            "name or position - the catalog has world coordinates and "
            'role tags scraped from the server source. Useful role '
            "tags: 'conquest_overseer' (sells conquest items including "
            "signet staffs and rank gear), 'vendor' (general shops), "
            "'homepoint', 'survival_guide', 'moghouse', 'auction_house', "
            "'trust_master', 'storage', 'quest_npc' (any NPC with an "
            'onTrigger handler - a wide net). Returns at most `limit` '
            'records (default 20).'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'role': {
                    'type': 'string',
                    'description': (
                        'Exact role tag from the catalog. Use list_zones '
                        "with role='?' to inspect available tags first if "
                        "unsure; the planner system prompt lists the "
                        'common ones.'
                    ),
                },
                'name_contains': {
                    'type': 'string',
                    'description': (
                        'Case-insensitive substring matched against both '
                        'the polutils-style name ("Rabid Wolf, I.M.") and '
                        "the lua name (\"Rabid_Wolf_IM\"). Empty / omitted "
                        'means no name filter.'
                    ),
                },
                'zone_id': {
                    'type':        'integer',
                    'description': 'Restrict to one zone by id.',
                },
                'zone_name': {
                    'type':        'string',
                    'description': 'Restrict to one zone by SQL name (e.g. "Bastok_Markets").',
                },
                'limit': {
                    'type':        'integer',
                    'description': 'Max results to return (default 20, hard cap 200).',
                },
            },
        },
    },
}


CLOSEST_NPC_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'closest_npc',
        'description': (
            'Rank NPCs by safety-weighted travel cost from `from_zone`. '
            'Pass the same role / name_contains filters as find_npc, OR '
            'a precomputed list of npc ids. Cost weights penalize '
            'transit through dungeons (default cost 100 per dungeon '
            'zone vs 1 per outdoors), unless the character has marked '
            'them safe via set_zone_safety. Use this when a goal can '
            'be satisfied at multiple NPCs (e.g. any nation\'s conquest '
            'overseer) and you want the easiest one to reach. Returns '
            'top N records with their route zone_id list and total cost.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'from_zone': {
                    'type':        'integer',
                    'description': "The character's current zone_id.",
                },
                'role':           {'type': 'string'},
                'name_contains':  {'type': 'string'},
                'candidate_ids':  {
                    'type':        'array',
                    'items':       {'type': 'integer'},
                    'description': 'Specific npc ids to rank (alternative to role/name).',
                },
                'limit': {
                    'type':        'integer',
                    'description': 'Max ranked results (default 5).',
                },
            },
            'required': ['from_zone'],
        },
    },
}


SET_ZONE_SAFETY_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'set_zone_safety',
        'description': (
            "Mark one zone safe or dangerous for THIS character based "
            "on level/job suitability. The router uses these overrides "
            "when ranking routes via closest_npc - 'safe' makes a zone "
            "cost 1 (treated like a cleared outdoors zone), 'dangerous' "
            "makes it cost 100 (avoid for transit). Use 'safe' when the "
            "character is overlevel for the zone's mob aggro range "
            "(e.g. WAR75 in a level-30 outdoors zone) so future "
            "find/closest queries can route through it. Use 'dangerous' "
            "if you observed a death or near-death there; the death "
            "reflex auto-marks dangerous already, so manual dangerous "
            "marks are usually for pre-emptive caution. Pass "
            "verdict='default' to clear an override."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'zone_id': {'type': 'integer'},
                'verdict': {
                    'type': 'string',
                    'enum': ['safe', 'dangerous', 'default'],
                },
                'reason': {
                    'type':        'string',
                    'description': 'Short rationale (<= 200 chars). Stored alongside the verdict.',
                },
            },
            'required': ['zone_id', 'verdict'],
        },
    },
}


FIND_ITEM_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'find_item',
        'description': (
            'Resolve a human-readable item name (e.g. "Rabbit Hide", '
            '"Legionnaire\'s Axe") to its numeric FFXI item id, stack '
            'size, and base vendor sell price from the LSB item catalog. '
            'Required before any auction-house goal: the ah_bid / '
            'ah_sell_ask actions take an integer item_id, NOT a name. '
            'The match is a case-insensitive substring scan against both '
            'the snake_case lua name ("rabbit_hide") and the display '
            'name ("Rabbit Hide"). Returns at most `limit` records '
            '(default 10, hard cap 50). Use the `id` and `stack_size` '
            'from the result to build the AH action; `base_sell` is a '
            'rough floor for sane listing prices.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'name_contains': {
                    'type': 'string',
                    'description': (
                        'Case-insensitive substring of the item name. '
                        'Required.'
                    ),
                },
                'limit': {
                    'type':        'integer',
                    'description': 'Max results (default 10, hard cap 50).',
                },
            },
            'required': ['name_contains'],
        },
    },
}


LIST_ZONES_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'list_zones',
        'description': (
            'Inspect zone metadata (name, type, neighbors). Use this to '
            "answer 'is zone X a dungeon?' or 'what zones border zone Y?' "
            "before committing to a travel plan. Pass type='?' alone to "
            'list available type tags.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'zone_id': {
                    'type':        'integer',
                    'description': 'One zone id - returns its full record + neighbors.',
                },
                'name_contains': {
                    'type':        'string',
                    'description': 'Substring filter on zone name.',
                },
                'type': {
                    'type':        'string',
                    'description': "CITY / OUTDOORS / DUNGEON / etc. (or '?' to list).",
                },
                'limit': {
                    'type':        'integer',
                    'description': 'Max records (default 20).',
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------

def _safety_path_for(cfg: _config.Config) -> Path | None:
    char = cfg.character
    if not char:
        return None
    return cfg.paths.persistent_dir(char) / 'zone_safety.json'


class _ItemCatalog:
    """Lazy loader for items.json. Keeps the parsed items list on
    first access; the file is ~5 MB so we load once per Planner
    instance rather than rescanning per tool call."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._items is None:
            try:
                blob = json.loads(self.path.read_text(encoding='utf-8'))
                self._items = blob.get('items') or []
            except (OSError, json.JSONDecodeError):
                self._items = []
        return self._items

    def find(self, name_contains: str, limit: int) -> list[dict[str, Any]]:
        needle = (name_contains or '').lower().strip()
        if not needle:
            return []
        out: list[dict[str, Any]] = []
        for r in self._load():
            if (needle in r.get('name', '').lower()
                    or needle in r.get('display_name', '').lower()):
                out.append(r)
                if len(out) >= limit:
                    break
        return out


def _npc_summary(n: dict[str, Any]) -> dict[str, Any]:
    """Compact NPC record we return to the LLM. Drops the raw lua_name
    + look fields that don't help planning, keeps name + zone + coords
    + roles."""
    return {
        'id':         n.get('id'),
        'name':       n.get('name'),
        'zone_id':    n.get('zone_id'),
        'zone_name':  n.get('zone_name'),
        'pos':        [n.get('x'), n.get('y'), n.get('z')],
        'roles':      n.get('roles') or [],
    }


def make_handlers(cfg: _config.Config) -> dict[str, Any]:
    """Build the {tool_name: handler} dict the planner passes to
    run_tool_loop. Re-creates a Router on each call to pick up
    zone_safety.json edits the agent itself made earlier in the
    session."""
    npc_path  = cfg.paths.npcs_file
    zone_path = cfg.paths.zone_meta_file
    trans_path = cfg.paths.transitions_file
    items_path = cfg.paths.items_file

    catalog = _nr.NPCCatalog(npc_path)
    items   = _ItemCatalog(items_path)

    def _router() -> _nr.Router:
        return _nr.Router(trans_path, zone_path, _safety_path_for(cfg))

    def _handle_find_npc(args: dict[str, Any]) -> dict[str, Any]:
        catalog.reload()
        limit = int(args.get('limit') or 20)
        limit = max(1, min(200, limit))
        results = catalog.find(
            role          = args.get('role') or None,
            name_contains = args.get('name_contains') or None,
            zone_id       = args.get('zone_id'),
            zone_name     = args.get('zone_name') or None,
            limit         = limit,
        )
        return {
            'count':   len(results),
            'role_tags_available': catalog.role_tags,
            'results': [_npc_summary(n) for n in results],
        }

    def _handle_closest_npc(args: dict[str, Any]) -> dict[str, Any]:
        catalog.reload()
        from_zone = args.get('from_zone')
        if not isinstance(from_zone, int):
            return {'error': "from_zone (int) is required"}

        if args.get('candidate_ids'):
            ids = [int(i) for i in args['candidate_ids']]
            pool = [n for i in ids if (n := catalog.by_id(i))]
        else:
            pool = catalog.find(
                role          = args.get('role') or None,
                name_contains = args.get('name_contains') or None,
                limit         = 200,
            )
        if not pool:
            return {'count': 0, 'results': [], 'note': 'no candidates matched'}

        router = _router()
        ranked = router.closest(from_zone, pool)
        limit = int(args.get('limit') or 5)
        limit = max(1, min(50, limit))

        return {
            'count':   len(ranked),
            'results': [
                {
                    **_npc_summary(rc.candidate),
                    'route_cost':  rc.cost,
                    'route_zones': rc.route,
                }
                for rc in ranked[:limit]
            ],
        }

    def _handle_list_zones(args: dict[str, Any]) -> dict[str, Any]:
        router = _router()
        meta = router._meta  # internal but stable; deliberately exposed here

        if args.get('type') == '?':
            tags = sorted({row.get('type_name', 'UNKNOWN') for row in meta.values()})
            return {'available_type_tags': tags}

        zone_id = args.get('zone_id')
        if isinstance(zone_id, int):
            row = meta.get(zone_id)
            if not row:
                return {'error': f'unknown zone_id {zone_id}'}
            neighbors = router._adj.get(zone_id, [])
            return {
                'zone':      row,
                'cost':      router.zone_cost(zone_id),
                'neighbors': [
                    {
                        'zone_id':   n,
                        'name':      (meta.get(n) or {}).get('name'),
                        'type_name': (meta.get(n) or {}).get('type_name'),
                        'cost':      router.zone_cost(n),
                    }
                    for n in neighbors
                ],
            }

        nc = (args.get('name_contains') or '').lower()
        type_filter = args.get('type') or None
        limit = int(args.get('limit') or 20)
        limit = max(1, min(200, limit))
        out = []
        for row in meta.values():
            if nc and nc not in row.get('name', '').lower():
                continue
            if type_filter and row.get('type_name') != type_filter:
                continue
            out.append({
                'zone_id':   row['zone_id'],
                'name':      row.get('name'),
                'type_name': row.get('type_name'),
                'cost':      router.zone_cost(row['zone_id']),
            })
            if len(out) >= limit:
                break
        return {'count': len(out), 'results': out}

    def _handle_set_zone_safety(args: dict[str, Any]) -> dict[str, Any]:
        zone_id = args.get('zone_id')
        verdict = args.get('verdict')
        reason  = args.get('reason') or ''
        if not isinstance(zone_id, int):
            return {'error': 'zone_id (int) is required'}
        if verdict not in _zs.VALID_VERDICTS:
            return {'error': f'verdict must be one of {sorted(_zs.VALID_VERDICTS)}'}
        char = cfg.character
        if not char:
            return {'error': 'no character set; cannot persist override'}
        rec = _zs.set_verdict(cfg.paths.persistent_dir(char),
                              zone_id, verdict, reason)
        return {'zone_id': zone_id, 'override': rec}

    def _handle_find_item(args: dict[str, Any]) -> dict[str, Any]:
        nc = args.get('name_contains') or ''
        if not nc:
            return {'error': 'name_contains is required'}
        limit = int(args.get('limit') or 10)
        limit = max(1, min(50, limit))
        results = items.find(nc, limit)
        return {
            'count':   len(results),
            'results': results,
        }

    return {
        'find_npc':        _handle_find_npc,
        'closest_npc':     _handle_closest_npc,
        'list_zones':      _handle_list_zones,
        'set_zone_safety': _handle_set_zone_safety,
        'find_item':       _handle_find_item,
    }
