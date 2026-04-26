"""LLM planner — turns a free-text user goal into a structured goal tree
and/or a gambit list.

Invoked when:
    1. A new top-level user goal arrives (via the addon's `/nav goal <text>`
       slash command, which writes to `agent_request.json`).
    2. (Later phases) a leaf fails or a periodic re-plan triggers.

Tool surface today:
    update_goals(goals, roots)            — replace the persistent goal tree
    update_gambits(gambits)               — replace the combat addon's gambit list

The model can call either tool, or both in a single response. We accept
the first call of each kind and apply them in order: gambits first
(combat reactions kick in immediately), then goals (long-running
directive). Validation happens before persisting; on failure we log the
error and skip just that tool's effect, leaving any other tool's
output applied.

Phase 3b-LLM scope: deliberative tier only, single round trip (no
multi-turn loops). Multi-step replanning, knowledge queries, and
gambit tuning from combat-log analysis come in later phases.
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


SYSTEM_PROMPT = """You are the planning brain for an autonomous Final Fantasy XI agent.
Decompose the user's free-text instruction into structured directives the
client can execute. The client owns real-time decisions (combat reactions,
nav, retries); you own *what* the agent should do next.

Respond by calling tools. Do not chat — every meaningful response is a tool
call. If the user mentions both navigation/objectives and combat behaviour,
call BOTH `update_goals` and `update_gambits` in the same response.

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
  update_gambits: one gambit, trigger lt(self.hp_pct, 30), action magic Cure <me>.

User: "farm bumblebees in the dunes — heal yourself when low"
  update_goals: travel→103 (Valkurm). update_gambits: low-HP cure gambit.
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


UPDATE_GAMBITS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'update_gambits',
        'description': (
            'Replace the combat addon\'s gambit list. Gambits are FF12-style '
            'condition→action rules; the first one whose trigger evaluates '
            'true and whose cooldown has elapsed fires per ~100ms tick.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
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


class Planner:
    """LLM-backed planner. Single-call decomposition for now; emits
    goals and/or gambits depending on what the user asked for."""

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway,
                 goal_manager: _gm.GoalManager):
        self.cfg = cfg
        self.llm = llm
        self.goal_manager = goal_manager

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

    def _apply_gambits(self, args: dict[str, Any]) -> bool:
        glist = args.get('gambits')
        if not isinstance(glist, list):
            print('  planner: update_gambits missing gambits list')
            return False
        try:
            _gambits.validate(glist)
        except _gambits.GambitValidationError as e:
            print(f'  planner: gambit validation failed:\n{e}')
            return False
        _gambits.deploy(self.cfg, glist)
        print(f'  planner: deployed {len(glist)} gambit(s)')
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
            f'do (where to go, what to accomplish), `update_gambits` for '
            f'how to react in combat. Call both if both apply. Use zone '
            f'ids from the table; never invent ids.'
        )

        t0 = time.time()
        try:
            resp = self.llm.client.chat.completions.create(
                model=self.cfg.llm.deliberative,
                max_tokens=4096,
                tools=[UPDATE_GOALS_TOOL, UPDATE_GAMBITS_TOOL],
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

        # Apply gambits first so combat reactions are armed BEFORE the
        # nav directive starts moving the agent into harm's way.
        applied: list[str] = []
        # Group calls by name; only the first of each kind is honoured.
        by_name: dict[str, dict[str, Any]] = {}
        for call in choice.message.tool_calls:
            name = call.function.name
            if name in by_name:
                continue
            try:
                by_name[name] = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                print(f'  planner: malformed args for {name}: {e}')

        if 'update_gambits' in by_name:
            if self._apply_gambits(by_name['update_gambits']):
                applied.append('gambits')
        if 'update_goals' in by_name:
            if self._apply_goals(by_name['update_goals']):
                applied.append('goals')

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
