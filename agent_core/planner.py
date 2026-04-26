"""LLM planner — turns a free-text user goal into a structured goal tree,
plus optional gambit edits and player-relationship updates.

Invoked when:
    1. The user edits `<repo>/user_goal.txt` and saves; the orchestrator's
       file watcher hands the contents to `Planner.plan()`.
    2. (Later phases) a leaf fails or a periodic re-plan triggers.

Tool surface (single-turn — every call applies in one round):
    update_goals          replace the persistent goal tree
    add_gambit            append one gambit to a (job/sub/party) context
    modify_gambit         tweak fields on an existing gambit by id
    remove_gambit         drop one gambit by id
    update_gambits        whole-set replace (bootstrapping only)
    clear_gambit_set      remove an entire context's set
    update_player         apply a patch dict to a player relationship record

All gambit-mutation calls are collected, applied in order, then the
resolved active list is redeployed once. update_goals collapses to the
last call (replacing the tree more than once is meaningless).

Phase 3b-LLM scope: deliberative tier only, single round trip (no
multi-turn loops). Read tools (query_player, query_knowledge) belong to
the future chat-handling LLM that needs a tool-use loop; this planner
only writes.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import config as _config
from . import events as _events
from . import gambits as _gambits
from . import goal_manager as _gm
from . import llm_gateway as _llm
from . import persistence as _persistence
from . import relationships as _relationships


SYSTEM_PROMPT = """You are the planning brain for an autonomous Final Fantasy XI agent.
Decompose the user's free-text instruction into structured directives the
client can execute. The client owns real-time decisions (combat reactions,
nav, retries); you own *what* the agent should do next.

Respond by calling tools. Do not chat — every meaningful response is a tool
call.

# Default: only touch goals

When the user gives you a goal (a destination, a quest, a "farm X"
instruction), call `update_goals`. **Do not** touch gambits unless the
user explicitly asks about combat behavior, healing, ability use, or
party support. Gambits are long-lived combat tuning that evolves
incrementally over many sessions — changing them on every goal flip
destroys accumulated tuning. If the user's instruction is purely about
where to go or what to accomplish, leave the gambit store alone.

When you do touch gambits, prefer the smallest possible change:
  • `add_gambit(context, gambit)`        — one new reaction
  • `modify_gambit(context, id, patch)`  — tweak threshold/cooldown/action
  • `remove_gambit(context, id)`         — drop an obsolete reaction

Reach for `update_gambits` (whole-set replace) only when bootstrapping
a context that has no prior set; reach for `clear_gambit_set` only
when explicitly asked to disarm a (job/sub/party) configuration.

# Player relationships

Some user instructions are about specific named players: "I owe Friend
5k gil," "Mybird helped me with the NM, remember that." Use
`update_player` to record those facts in the persistent relationship
store. Patch fields available:

  tone_delta              add to a running -1..+1 score (warm/cool)
  append_interaction      {channel, direction, text, summary?}
  append_note             newline-append to free-text notes
  set_notes               replace notes wholesale
  add_favor_owed_by_us    "we promised X to <player>"
  add_favor_owed_to_us    "<player> did X for us"
  mark_favor_done_by_us   substring of an open favor we owed; resolves it
  mark_favor_done_to_us   same, for favors they owed us

Don't fabricate interactions. Only record facts the user actually told
you about — the chat addon will accrete real interactions on its own
in a later phase.

# update_goals — the goal tree

Goal types you can emit:

  composite       container; has `subgoals`; completes when all children do
  travel          { target_zone: <int> }
                  cross-zone goto; completes on arrival in that zone
                  Optional: target_pos: [x, y, z]
  goto            { target_pos: [x, y, z], target_zone: <int>? }
                  same-zone goto; completes within ~8y of target_pos
  farm            { target_name: <str>,
                    stop_when:   { kill_count: <int> },
                    rest_hp_pct: <int> }                # default 70
                  drive a kill loop on the named mob in the player's
                  CURRENT zone; the agent /ta's the mob, /attacks, rests
                  when low HP, repeats until stop_when fires.
                  MVP supports kill_count stop only; the player must
                  already be near spawn points for the named mob.
  wait            { seconds: <float> }

Each goal: id (short string), title, origin ("user" / "auto"),
state ("pending"), type, subgoals (composite only), and type-specific fields.
The `roots` list names top-level goal ids in priority order.

Keep trees small. If the user names a single zone, emit a single travel
leaf — no composite wrapper.

# update_gambits — the FF12-style combat reaction list

Each gambit is `{id, priority, cooldown, trigger, action}`. The combat
addon walks the list every ~100ms; the FIRST gambit whose trigger
expression evaluates true (and whose cooldown has elapsed) fires its
action. Lower `priority` numbers fire first; ties broken by list order.

Gambits are stored per CONTEXT — a `(main_job, sub_job, party)` triple
where any field can be `null` to mean "any value." Common contexts:

  {} or {"main_job": null, "sub_job": null, "in_party": null}
        → universal fallback ("*/*/*"), applies regardless of job/party
  {"main_job": "WAR"}
        → all WAR play (any subjob, solo or party)
  {"main_job": "WAR", "sub_job": "NIN", "in_party": false}
        → WAR/NIN soloing specifically

When the agent's live context changes (job change, party invite/leave),
the orchestrator recomputes the active list by MERGING every matching
context into one — more-specific gambits override less-specific ones
that share the same `id`. So a `*/*/*` "cure self below 30%" baseline
plus a `WAR/NIN/*` "Sneak Attack on full TP" addition both fire when
playing WAR/NIN. Use stable ids and reuse them across contexts when you
want to override a baseline.

Each `update_gambits` call REPLACES exactly one context's set — it does
NOT touch other contexts. Never wipe a context unless explicitly asked;
use `clear_gambit_set` for that.

Trigger expression nodes:
  {"op": "lit", "value": <number|string|bool>}
  {"op": "ref", "path": "self.hp_pct"}      # dotted path into world state
  {"op": "and", "args": [<expr>, ...]}
  {"op": "or",  "args": [<expr>, ...]}
  {"op": "not", "a": <expr>}
  {"op": "lt"|"lte"|"gt"|"gte"|"eq"|"ne", "a": <expr>, "b": <expr>}
  {"op": "in", "needle": <expr>, "haystack": <expr>}

Live world-state paths you can `ref`:
  self.hp_pct, self.mp_pct, self.tp, self.main_job_lvl
  self.buffs                                 # array of buff ids
  target.hp_pct, target.distance, target.alive, target.claimed_by_us
  engaged                                    # bool
  party.<n>.hp_pct, party.<n>.mp_pct, party.<n>.name

Action kinds:
  {"kind": "ability",     "name": "Provoke",     "target": "<t>"}    # /ja
  {"kind": "magic",       "name": "Cure III",    "target": "<p1>"}   # /ma
  {"kind": "weaponskill", "name": "Spirits Within", "target": "<t>"} # /ws
  {"kind": "engage"}                         # /attack on
  {"kind": "disengage"}                      # /attack off
  {"kind": "raw",         "command": "/echo hi"} # any literal /command

Targets are passthrough Ashita tokens (<me>, <t>, <p0>..<p5>, <bt>, <ft>).
Default targets: ability → <me>, magic/ws → <t>.

Cooldown is in seconds and keys off `id`, so reuse stable ids across calls
to keep cooldowns honest.

# Examples (illustrative, do not echo)

User: "go to selbina"
  update_goals: one travel leaf with target_zone=248.

User: "if my HP drops below 30%, cast Cure on myself"
  update_gambits (only — no goal): one gambit, trigger lt(self.hp_pct, 30),
  action magic Cure <me>, context = {} (universal fallback).

User: "farm bumblebees in the dunes"
  update_goals only — combat reactions are NOT this user's concern; the
  agent's existing gambits handle combat. Do not touch gambits.

User: "go to selbina, and bump the cure threshold to 40%"
  update_goals: travel→248. PLUS exactly one minimal gambit edit (the
  cure threshold) — this is the rare case where the user asked for both.
"""


UPDATE_GOALS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'update_goals',
        'description': (
            'Replace the entire goal tree with a new set of goals. '
            'The manager picks the first pending leaf in DFS root-order '
            'and executes it via the nav / combat / etc. subsystems.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'goals': {
                    'type': 'array',
                    'description': 'Flat list of goal nodes (composites + leaves).',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'id': {'type': 'string'},
                            'title': {'type': 'string'},
                            'origin': {'type': 'string', 'enum': ['user', 'auto']},
                            'state': {'type': 'string', 'enum': ['pending']},
                            'type': {
                                'type': 'string',
                                'enum': ['composite', 'travel', 'goto', 'farm', 'wait'],
                            },
                            'subgoals': {
                                'type': 'array',
                                'items': {'type': 'string'},
                            },
                            'target_zone': {'type': 'integer'},
                            'target_pos': {
                                'type': 'array',
                                'items': {'type': 'number'},
                                'minItems': 3,
                                'maxItems': 3,
                            },
                            'seconds': {'type': 'number'},
                            'target_name': {'type': 'string'},
                            'stop_when': {
                                'type': 'object',
                                'properties': {'kill_count': {'type': 'integer'}},
                            },
                            'rest_hp_pct': {'type': 'integer'},
                        },
                        'required': ['id', 'title', 'type', 'state'],
                    },
                },
                'roots': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Top-level goal ids in priority order.',
                },
            },
            'required': ['goals', 'roots'],
        },
    },
}


_CONTEXT_SCHEMA = {
    'type': 'object',
    'description': (
        'Execution context this gambit set applies to. Any field omitted '
        'or null means "any value." All fields omitted = universal fallback.'
    ),
    'properties': {
        'main_job': {
            'type': ['string', 'null'],
            'description': '3-letter job code (WAR, NIN, BLM, ...) or null.',
        },
        'sub_job': {
            'type': ['string', 'null'],
            'description': '3-letter subjob code or null. Use "NON" if you mean "no subjob."',
        },
        'in_party': {
            'type': ['boolean', 'null'],
            'description': 'true = in a party; false = solo; null = either.',
        },
    },
}


UPDATE_GAMBITS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'update_gambits',
        'description': (
            "Replace the gambit set for ONE context. Gambits are FF12-style "
            "condition→action rules; on every ~100ms tick the first one whose "
            "trigger evaluates true and whose cooldown has elapsed fires. "
            "Multiple matching contexts merge — more-specific overrides "
            "less-specific by gambit `id`. This call does not touch any "
            "other context's set. Use clear_gambit_set to remove one."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'context': _CONTEXT_SCHEMA,
                'gambits': {
                    'type': 'array',
                    'description': 'Ordered list; lower priority fires first.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'id':       {'type': 'string'},
                            'priority': {'type': 'number'},
                            'cooldown': {'type': 'number'},
                            'trigger':  {'type': 'object',
                                         'description': 'Expression AST: lit/ref/and/or/not/lt..ne/in'},
                            'action':   {'type': 'object',
                                         'description': 'kind: ability|magic|weaponskill|engage|disengage|raw'},
                        },
                        'required': ['id', 'trigger', 'action'],
                    },
                },
            },
            'required': ['gambits'],
        },
    },
}


CLEAR_GAMBIT_SET_TOOL = {
    'type': 'function',
    'function': {
        'name': 'clear_gambit_set',
        'description': (
            "Remove one stored gambit set, identified by context. Use ONLY "
            "when explicitly asked to disarm a specific (job/sub/party) "
            "configuration; gambits are otherwise persistent across goals."
        ),
        'parameters': {
            'type': 'object',
            'properties': {'context': _CONTEXT_SCHEMA},
            'required': ['context'],
        },
    },
}


# Single-gambit shape used by add_gambit + the items in update_gambits.
_GAMBIT_SCHEMA = {
    'type': 'object',
    'properties': {
        'id':       {'type': 'string',
                     'description': 'stable id; modify_gambit/remove_gambit reference it'},
        'priority': {'type': 'number',
                     'description': 'lower fires first; ties by list order'},
        'cooldown': {'type': 'number',
                     'description': 'seconds; floored at 0.5 by the validator'},
        'trigger':  {'type': 'object',
                     'description': 'expression AST: lit/ref/and/or/not/lt..ne/in'},
        'action':   {'type': 'object',
                     'description': 'kind: ability|magic|weaponskill|engage|disengage|raw'},
    },
    'required': ['id', 'trigger', 'action'],
}


ADD_GAMBIT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'add_gambit',
        'description': (
            "Append ONE new gambit to the set keyed by context. Preferred "
            "over update_gambits for incremental tuning. Rejects duplicate "
            "ids — use modify_gambit to change an existing one."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'context': _CONTEXT_SCHEMA,
                'gambit':  _GAMBIT_SCHEMA,
            },
            'required': ['gambit'],
        },
    },
}


MODIFY_GAMBIT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'modify_gambit',
        'description': (
            "Tweak fields of an existing gambit identified by id within "
            "the set keyed by context. Patch may contain priority, "
            "cooldown, trigger, action — each replaces its top-level field "
            "wholesale (no deep merge of trigger/action ASTs)."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'context': _CONTEXT_SCHEMA,
                'id':      {'type': 'string'},
                'patch':   {
                    'type': 'object',
                    'properties': {
                        'priority': {'type': 'number'},
                        'cooldown': {'type': 'number'},
                        'trigger':  {'type': 'object'},
                        'action':   {'type': 'object'},
                    },
                },
            },
            'required': ['id', 'patch'],
        },
    },
}


REMOVE_GAMBIT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'remove_gambit',
        'description': (
            "Drop one gambit by id from the set keyed by context. If that "
            "empties the set, the set itself is dropped. No-op if the id "
            "isn't present."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'context': _CONTEXT_SCHEMA,
                'id':      {'type': 'string'},
            },
            'required': ['id'],
        },
    },
}


UPDATE_PLAYER_TOOL = {
    'type': 'function',
    'function': {
        'name': 'update_player',
        'description': (
            "Apply a patch dict to a named player's relationship record. "
            "Use to record favors owed/done, append notes, adjust tone "
            "score (-1..+1), or log interactions the user told you about. "
            "Don't invent interactions — the chat addon accretes real "
            "ones automatically in a later phase."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {
                    'type': 'string',
                    'description': "FFXI character name (alphanumeric, ' _ -; max 30 chars)",
                },
                'patch': {
                    'type': 'object',
                    'description': 'Patch fields. All optional; at least one required.',
                    'properties': {
                        'tone_delta':            {'type': 'number'},
                        'append_interaction':    {
                            'type': 'object',
                            'properties': {
                                'channel':   {'type': 'string'},
                                'direction': {'type': 'string', 'enum': ['in', 'out']},
                                'text':      {'type': 'string'},
                                'summary':   {'type': 'string'},
                            },
                        },
                        'append_note':           {'type': 'string'},
                        'set_notes':             {'type': 'string'},
                        'add_favor_owed_by_us':  {'type': 'string'},
                        'add_favor_owed_to_us':  {'type': 'string'},
                        'mark_favor_done_by_us': {'type': 'string'},
                        'mark_favor_done_to_us': {'type': 'string'},
                    },
                },
            },
            'required': ['name', 'patch'],
        },
    },
}


class Planner:
    """LLM-backed planner. Single-call decomposition for now; emits
    goals and/or gambits depending on what the user asked for."""

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway,
                 goal_manager: _gm.GoalManager,
                 gambits_store: _persistence.Gambits,
                 current_ctx_provider):
        self.cfg = cfg
        self.llm = llm
        self.goal_manager = goal_manager
        # Shared with main.NavServer — the planner mutates the store
        # (via update_set / clear_set) and the orchestrator's context
        # watcher reads it on the next tick to redeploy the active list.
        self.gambits_store = gambits_store
        # Callable returning the current {main_job, sub_job, in_party}
        # context as canonical strings. Used to redeploy immediately
        # after a tool call, so the addon sees the new gambits this tick
        # rather than waiting on the watcher to notice the file change.
        self._current_ctx = current_ctx_provider

    # ---- world-state snapshot ----------------------------------------

    def _world_state_text(self, zone_names: dict[int, str]) -> str:
        """Compact prompt-friendly description of where the player is and
        which zones are reachable to plan toward."""
        snap = self.goal_manager._snapshot()
        cur_zone_name = zone_names.get(snap.zone_id, '?') if snap.zone_id else '?'
        # The full zone catalog is ~200 entries — tiny in token terms,
        # and the LLM knows zone IDs only via what we tell it. Send all.
        zones_block = '\n'.join(
            f'  {zid:>3}  {name}' for zid, name in sorted(zone_names.items())
        )
        return (
            f'Current zone: {snap.zone_id} ({cur_zone_name})\n'
            f'Position:     ({snap.x}, {snap.y}, {snap.z})\n'
            f'Moving:       {snap.moving}\n'
            f'\nAll known zones (id  name):\n{zones_block}\n'
        )

    # ---- tool-call appliers -----------------------------------------

    def _apply_goals(self, args: dict[str, Any]) -> bool:
        goals_list = args.get('goals') or []
        roots = args.get('roots') or []
        if not goals_list or not roots:
            print('  planner: update_goals missing goals/roots')
            return False
        nodes_dict: dict[str, dict[str, Any]] = {}
        for g in goals_list:
            gid = g.get('id')
            if not gid:
                continue
            # Force pending state on every node — the manager owns state
            # transitions, the planner doesn't.
            g['state'] = 'pending'
            nodes_dict[gid] = g
        new_goals = _persistence.Goals(nodes=nodes_dict, roots=list(roots))
        new_goals.save(self.goal_manager._goals_path)
        self.goal_manager.goals = new_goals
        self.goal_manager._last_dispatch.clear()
        self.goal_manager._active_leaf_id = None
        print(f'  planner: applied {len(nodes_dict)} goal node(s), '
              f'{len(roots)} root(s)')
        return True

    # Gambit-mutation handlers — store changes only, no redeploy here.
    # The plan() dispatcher batches all gambit calls and redeploys the
    # resolved active list ONCE at the end of the response.

    def _apply_update_gambits(self, args: dict[str, Any]) -> bool:
        glist = args.get('gambits')
        if not isinstance(glist, list):
            print('  planner: update_gambits missing gambits list')
            return False
        ctx = args.get('context') if isinstance(args.get('context'), dict) else {}
        try:
            key = _gambits.update_set(self.cfg, self.gambits_store, ctx, glist)
        except _gambits.GambitValidationError as e:
            print(f'  planner: update_gambits validation failed:\n{e}')
            return False
        print(f'  planner: replaced "{key}" with {len(glist)} gambit(s)')
        return True

    def _apply_clear_gambit_set(self, args: dict[str, Any]) -> bool:
        ctx = args.get('context') if isinstance(args.get('context'), dict) else None
        if ctx is None:
            print('  planner: clear_gambit_set missing context')
            return False
        removed = _gambits.clear_set(self.cfg, self.gambits_store, ctx)
        if removed is None:
            print('  planner: clear_gambit_set: no matching set')
            return False
        print(f'  planner: cleared "{removed}"')
        return True

    def _apply_add_gambit(self, args: dict[str, Any]) -> bool:
        gambit = args.get('gambit')
        if not isinstance(gambit, dict):
            print('  planner: add_gambit missing gambit object')
            return False
        ctx = args.get('context') if isinstance(args.get('context'), dict) else {}
        try:
            key = _gambits.add_gambit(self.cfg, self.gambits_store, ctx, gambit)
        except _gambits.GambitValidationError as e:
            print(f'  planner: add_gambit failed:\n{e}')
            return False
        print(f'  planner: added "{gambit.get("id")}" to "{key}"')
        return True

    def _apply_modify_gambit(self, args: dict[str, Any]) -> bool:
        gid = args.get('id')
        patch = args.get('patch')
        if not isinstance(gid, str) or not isinstance(patch, dict):
            print('  planner: modify_gambit missing id or patch')
            return False
        ctx = args.get('context') if isinstance(args.get('context'), dict) else {}
        try:
            key = _gambits.modify_gambit(self.cfg, self.gambits_store, ctx, gid, patch)
        except _gambits.GambitValidationError as e:
            print(f'  planner: modify_gambit failed:\n{e}')
            return False
        print(f'  planner: modified "{gid}" in "{key}" '
              f'(fields: {sorted(patch)})')
        return True

    def _apply_remove_gambit(self, args: dict[str, Any]) -> bool:
        gid = args.get('id')
        if not isinstance(gid, str):
            print('  planner: remove_gambit missing id')
            return False
        ctx = args.get('context') if isinstance(args.get('context'), dict) else {}
        removed_key = _gambits.remove_gambit(self.cfg, self.gambits_store, ctx, gid)
        if removed_key is None:
            print(f'  planner: remove_gambit: no gambit "{gid}" in that context')
            return False
        print(f'  planner: removed "{gid}" from "{removed_key}"')
        return True

    def _apply_update_player(self, args: dict[str, Any]) -> bool:
        name = args.get('name')
        patch = args.get('patch')
        if not isinstance(name, str) or not isinstance(patch, dict):
            print('  planner: update_player missing name or patch')
            return False
        try:
            _relationships.update(self.cfg, name, patch)
        except _relationships.InvalidPlayerName as e:
            print(f'  planner: update_player rejected name: {e}')
            return False
        except _relationships.InvalidRelationshipPatch as e:
            print(f'  planner: update_player rejected patch: {e}')
            return False
        applied_keys = sorted(patch.keys())
        print(f'  planner: updated relationship for {name!r} '
              f'(fields: {applied_keys})')
        return True

    # ---- LLM round trip ----------------------------------------------

    def plan(self, user_text: str, zone_names: dict[int, str]) -> bool:
        """Send the user instruction + world state to the LLM. Apply
        whichever tool calls come back (goals, gambits, or both).
        Returns True if at least one tool was applied successfully."""
        if not self.llm.available:
            print('  planner: LLM unavailable; skipping.')
            return False

        ws = self._world_state_text(zone_names)
        user_msg = (
            f'User instruction:\n  "{user_text}"\n\n'
            f'World state:\n{ws}\n'
            f'Plan the agent\'s response. Use `update_goals` for what to '
            f'do (where to go, what to accomplish). Touch gambits ONLY if '
            f'the user mentioned combat behavior — and prefer add_gambit / '
            f'modify_gambit / remove_gambit over update_gambits. Use '
            f'update_player when the user told you something about a '
            f'specific named player. Use zone ids from the table; never '
            f'invent ids.'
        )

        # Tool names that mutate the gambit store. The dispatcher routes
        # them to per-tool handlers and redeploys the resolved active list
        # ONCE at the end of the response (instead of per call).
        gambit_tools = {
            'update_gambits':   self._apply_update_gambits,
            'clear_gambit_set': self._apply_clear_gambit_set,
            'add_gambit':       self._apply_add_gambit,
            'modify_gambit':    self._apply_modify_gambit,
            'remove_gambit':    self._apply_remove_gambit,
        }

        t0 = time.time()
        try:
            resp = self.llm.client.chat.completions.create(
                model=self.cfg.llm.deliberative,
                max_tokens=4096,
                tools=[
                    UPDATE_GOALS_TOOL,
                    ADD_GAMBIT_TOOL,
                    MODIFY_GAMBIT_TOOL,
                    REMOVE_GAMBIT_TOOL,
                    UPDATE_GAMBITS_TOOL,
                    CLEAR_GAMBIT_SET_TOOL,
                    UPDATE_PLAYER_TOOL,
                ],
                tool_choice='required',  # must call at least one tool
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user',   'content': user_msg},
                ],
            )
        except Exception as e:
            print(f'  planner: LLM call failed: {e}')
            return False
        latency = time.time() - t0

        choice = resp.choices[0] if resp.choices else None
        if choice is None or not choice.message.tool_calls:
            print('  planner: model did not return a tool call')
            return False

        # Walk every tool call in order. Gambit mutations accumulate; we
        # redeploy the resolved active list once at the end so the addon
        # sees the final merged state, not intermediate ones. Goals
        # collapse to the last call (replacing the tree twice is moot).
        applied: list[str] = []
        last_goals_args: dict[str, Any] | None = None
        gambit_mutations = 0
        player_updates = 0
        for call in choice.message.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                print(f'  planner: malformed args for {name}: {e}')
                continue
            if name in gambit_tools:
                if gambit_tools[name](args):
                    gambit_mutations += 1
            elif name == 'update_goals':
                last_goals_args = args
            elif name == 'update_player':
                if self._apply_update_player(args):
                    player_updates += 1
            else:
                print(f'  planner: unknown tool call {name!r}, ignoring')

        if gambit_mutations:
            try:
                merged = _gambits.deploy_active(
                    self.cfg, self.gambits_store, self._current_ctx(),
                )
                print(f'  planner: redeployed gambits → {len(merged)} active')
                applied.append(f'gambits×{gambit_mutations}')
            except Exception as e:
                print(f'  planner: gambit redeploy failed: {e}')
        if last_goals_args is not None:
            if self._apply_goals(last_goals_args):
                applied.append('goals')
        if player_updates:
            applied.append(f'players×{player_updates}')

        usage = getattr(resp, 'usage', None)
        in_tok = getattr(usage, 'prompt_tokens', 0) if usage else 0
        out_tok = getattr(usage, 'completion_tokens', 0) if usage else 0
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='planner',
            type_='plan_generated',
            user_text=user_text,
            tier='deliberative',
            model=self.cfg.llm.deliberative,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_s=round(latency, 3),
            applied=applied,
        )
        if not applied:
            print('  planner: no tools applied')
            return False
        print(f'  planner: applied {", ".join(applied)} in {latency:.2f}s '
              f'({in_tok}→{out_tok} tokens)')
        return True
