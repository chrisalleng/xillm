"""LLM planner — turns a free-text user goal into a structured goal tree.

Invoked when:
    1. A new top-level user goal arrives (via the addon's `/nav goal <text>`
       slash command, which writes to `agent_request.json`).
    2. (Later phases) a leaf fails or a periodic re-plan triggers.

The planner has one tool surface entry today: `update_goals(goals, roots)`.
We give the LLM a compact world-state snapshot, the user's goal text,
and the schema for the goal types the manager understands. The LLM
returns a tool call that we apply to the persistent goal tree.

Phase 2b scope: deliberative tier only, single tool, single round
trip (no multi-turn planning loops). Multi-step replanning, knowledge
queries, and gambit tuning come later.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import config as _config
from . import events as _events
from . import goal_manager as _gm
from . import llm_gateway as _llm
from . import persistence as _persistence


SYSTEM_PROMPT = """You are the planning brain for an autonomous Final Fantasy XI agent.
You decompose the user's free-text goal into a structured goal tree the
client can execute. The client owns real-time decisions (combat reactions,
nav, retries); you own *what* the agent should do next.

You MUST respond by calling the `update_goals` tool with a complete goal
tree. Do not chat — every meaningful response is a tool call.

Goal types you can emit:

  composite       container goal; has subgoals; completes when all children do
  travel          { target_zone: <int> }
                  cross-zone goto; completes when player arrives in that zone
                  Optional: target_pos: [x, y, z] for a precise landing point
  goto            { target_pos: [x, y, z], target_zone: <int>? }
                  same-zone goto; completes within ~8y of target_pos
  wait            { seconds: <float> }
                  pause this many seconds; mostly for testing

Each goal is a JSON object with:
  id          unique short string ("g1", "g_selbina")
  title       human-readable description
  origin      "user" for top-level user goals, "auto" for LLM-decomposed
  state       always "pending" — the manager flips this as it executes
  type        one of the types above
  subgoals    list of child ids (for composite goals; in execution order)
  ...         type-specific fields (target_zone, target_pos, seconds)

The `roots` list names the top-level goal ids in priority order.

Keep the tree small. Prefer 1–5 leaves per composite. If the user's
goal already names a single zone, emit a single travel leaf — no
composite wrapper needed.
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
                                'enum': ['composite', 'travel', 'goto', 'wait'],
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


class Planner:
    """LLM-backed planner. Single-call decomposition for now."""

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

    # ---- LLM round trip ----------------------------------------------

    def plan(self, user_goal_text: str, zone_names: dict[int, str]) -> bool:
        """Send the user goal + world state to the LLM. If it returns
        a valid update_goals tool call, replace the persistent goal
        tree and return True. Returns False on any error."""
        if not self.llm.available:
            print('  planner: LLM unavailable; skipping.')
            return False

        ws = self._world_state_text(zone_names)
        user_msg = (
            f'User goal:\n  "{user_goal_text}"\n\n'
            f'World state:\n{ws}\n'
            f'Decompose this goal and emit the resulting goal tree '
            f'via the `update_goals` tool. Use zone ids from the table '
            f'above; never invent ids.'
        )

        t0 = time.time()
        try:
            resp = self.llm.client.chat.completions.create(
                model=self.cfg.llm.deliberative,
                max_tokens=2048,
                tools=[UPDATE_GOALS_TOOL],
                tool_choice={
                    'type': 'function',
                    'function': {'name': 'update_goals'},
                },
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user',   'content': user_msg},
                ],
            )
        except Exception as e:
            print(f'  planner: LLM call failed: {e}')
            return False
        latency = time.time() - t0

        # Pull the tool-call payload.
        choice = resp.choices[0] if resp.choices else None
        if choice is None or not choice.message.tool_calls:
            print('  planner: model did not return a tool call')
            return False
        call = choice.message.tool_calls[0]
        try:
            args = json.loads(call.function.arguments)
        except json.JSONDecodeError as e:
            print(f'  planner: malformed tool args: {e}')
            return False

        goals_list = args.get('goals') or []
        roots = args.get('roots') or []
        if not goals_list or not roots:
            print('  planner: tool call missing goals/roots')
            return False

        # Apply: persist the new tree.
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
        # Rebind the manager's in-memory copy.
        self.goal_manager.goals = new_goals
        self.goal_manager._last_dispatch.clear()
        self.goal_manager._active_leaf_id = None

        usage = getattr(resp, 'usage', None)
        in_tok = getattr(usage, 'prompt_tokens', 0) if usage else 0
        out_tok = getattr(usage, 'completion_tokens', 0) if usage else 0
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='planner',
            type_='plan_generated',
            user_goal=user_goal_text,
            tier='deliberative',
            model=self.cfg.llm.deliberative,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_s=round(latency, 3),
            num_nodes=len(nodes_dict),
            num_roots=len(roots),
        )
        print(f'  planner: produced {len(nodes_dict)} node(s), {len(roots)} root(s) '
              f'in {latency:.2f}s')
        return True
