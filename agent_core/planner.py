"""LLM planner - turns a free-text user goal into a structured goal tree,
plus optional gambit edits and player-relationship updates.

Invoked when:
    1. The user edits `<repo>/user_goal.txt` and saves; the orchestrator's
       file watcher hands the contents to `Planner.plan()`.
    2. (Later phases) a leaf fails or a periodic re-plan triggers.

Tool surface (single-turn - every call applies in one round):
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
from . import echo as _echo
from . import gambits as _gambits
from . import goal_manager as _gm
from . import llm_gateway as _llm
from . import persistence as _persistence
from . import relationships as _relationships
from . import web_research as _web_research


SYSTEM_PROMPT = _web_research.ERA_CONSTRAINT + '\n\n' + """You are the inner monologue of an autonomous Final Fantasy XI agent.
When you call `update_goals`, ALWAYS populate the `rationale` field
with ONE first-person sentence (<=100 chars) - what the agent is
thinking, why this plan. Echoed in-game. The rationale MUST use the
actual current job, level, zone, and HP from the world state shown
above - DO NOT make up numbers or copy any specific values from this
prompt's examples.
GOOD shape (substitute REAL current values): "<job><lvl> in <zone>,
<short reason for the plan>."
BAD: third-person, multi-sentence, citing field names, made-up
levels or zones, copying example wording verbatim.

DO NOT recommend Valkurm Dunes below level ~17. It is a party-only
camp, not a solo zone - sending a low-level player there gets them
killed regardless of how famous it is for "leveling". Use web_search
to verify zone level ranges and mob composition; do NOT rely on
training-data priors about famous leveling spots.

If the player is ALREADY in an outdoor zone whose mobs match their
level, DO NOT emit a travel leaf - emit engage_nearby only. Travel
is for leaving a zone whose mobs don't fit, not for chasing a
prettier camp.
""" + """You are the planning brain for an autonomous Final Fantasy XI agent.
Decompose the user's free-text instruction into structured directives the
client can execute. The client owns real-time decisions (combat reactions,
nav, retries); you own *what* the agent should do next.

Respond by calling tools. Do not chat - every meaningful response is a tool
call.

# Default: only touch goals

When the user gives you a goal (a destination, a quest, a "farm X"
instruction), call `update_goals`. **Do not** touch gambits unless the
user explicitly asks about combat behavior, healing, ability use, or
party support. Gambits are long-lived combat tuning that evolves
incrementally over many sessions - changing them on every goal flip
destroys accumulated tuning. If the user's instruction is purely about
where to go or what to accomplish, leave the gambit store alone.

When you do touch gambits, prefer the smallest possible change:
  • `add_gambit(context, gambit)`        - one new reaction
  • `modify_gambit(context, id, patch)`  - tweak threshold/cooldown/action
  • `remove_gambit(context, id)`         - drop an obsolete reaction

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
you about - the chat addon will accrete real interactions on its own
in a later phase.

# update_goals - the goal tree

Goal types you can emit:

  composite       container; has `subgoals`; completes when all children do
  travel          { target_zone_name: <str> }
                  cross-zone goto; completes on arrival in that zone.
                  `target_zone_name` is the human-readable zone name
                  (e.g. "North Gustaberg", "Selbina"). The planner
                  resolves it to the canonical zone id at apply time -
                  always pass names, NOT numeric ids. Names that can't
                  be resolved cause the goal to be dropped with a log.
                  Optional: target_pos: [x, y, z]
  goto            { target_pos: [x, y, z], target_zone: <int>? }
                  same-zone goto; completes within ~8y of target_pos
  farm            { target_name: <str>,
                    stop_when:   { kill_count: <int> },
                    rest_hp_pct: <int> }                # default 70
                  drive a kill loop on the named mob in the player's
                  CURRENT zone; the agent locks the mob, attacks, rests
                  when low HP, repeats until stop_when fires.
                  MVP supports kill_count stop only; the player must
                  already be near spawn points for the named mob.
  engage_nearby   { stop_when:   { kill_count: <int> },
                    rest_hp_pct: <int> }                # default 70
                  Same kill loop as `farm` but with NO named target -
                  the agent locks the closest visible mob, fights it,
                  picks the next one after the kill. Use this in zones with
                  empty/unknown mob catalogs (the typical case early
                  on). Each kill auto-catalogs the mob's name + spawn
                  area, so future plans CAN use named `farm` goals.
                  Fails if no enemy is in range within ~20s.
  equip           { slot: <str>, item: <str> }
                  put `item` into equipment `slot`. Slot is one of:
                  main, sub, range, ammo, head, body, hands, legs, feet,
                  neck, waist, ear1, ear2, ring1, ring2, back.
                  `item` is the exact item name from the inventory
                  payload's `name` field. Completes when the next
                  inventory snapshot shows the item equipped in `slot`.
                  Use only when (a) the slot is empty or holds something
                  inferior, and (b) you can see the item in the
                  Unequipped section of the world state. Don't try to
                  equip an item whose `lvl_req` exceeds the player's
                  current main-job level - the command will silently
                  fail and the goal will hang.
  wait            { seconds: <float> }

Each goal: id (short string), title, origin ("user" / "auto"),
state ("pending"), type, subgoals (composite only), and type-specific fields.
The `roots` list names top-level goal ids in priority order.

**The goal tree is FLAT.** `goals` is a single flat array of all
goal nodes (composites + leaves, in any order). Tree structure comes
from `subgoals` being an **array of STRING IDS** that reference other
goals in the same flat array - NEVER nested objects. Likewise `roots`
is an array of string IDs at the top level of `update_goals`, NOT a
field that appears inside individual composite goals.

  Correct shape:
    goals = [
      {id:"a", type:"composite", title:"...", state:"pending", subgoals:["b","c"]},
      {id:"b", type:"travel", title:"...", state:"pending", target_zone_name:"..."},
      {id:"c", type:"engage_nearby", title:"...", state:"pending", stop_when:{kill_count:10}},
    ]
    roots = ["a"]

  Wrong shape (will be rejected):
    goals = [
      {id:"a", type:"composite", subgoals:[
        {id:"b", type:"travel", ...}, <- nested object, must be a string
        {id:"c", type:"engage_nearby", ...},
      ], roots:["b","c"]},          <- roots only belongs at the top level
    ]

**EVERY goal MUST include the `type` field** - it picks the dispatcher
on the agent side. Goals without a `type` are dropped and the user's
plan silently breaks. If you emit `update_goals` with N goals, every
single one of those N goals MUST have `type` set to one of: composite,
travel, goto, farm, engage_nearby, equip, wait.

Most plans don't need composites at all - sequential top-level roots
work for the common case ("travel, then farm" = `roots=["travel_x","farm_y"]`,
flat). Only reach for composite when you genuinely need to group
sub-tasks under a shared parent (e.g. a quest with multiple steps).

**For multi-step user goals like "1. Reach level 4 / 2. Reach level 7
/ 3. Reach level 10", DO NOT wrap each step in a composite.** Emit the
leaves needed to make ACTUAL PROGRESS on the first user-stated goal as
top-level roots - usually that means BOTH a travel leaf AND a follow-up
engage_nearby/farm leaf, not just one. The agent will be called again
once those complete, so far-future stages (level 7 -> 10) can be deferred,
but never emit a plan that lands the agent somewhere with nothing to do.
A travel leaf without a follow-up engage_nearby for a "reach level X"
goal is a broken plan - the agent will arrive in the zone and idle.

Keep trees small. If the user names a single zone, emit a single travel
leaf - no composite wrapper.

Worked example - user asks "Get to level 4" or "Reach level 10":
  Roots in priority order: get to a level-appropriate area, then engage.
  Pick `target_zone_name` from the "Direct neighbors of zone N" block in
  the world state - those are the zones one zoneline from where the
  player IS RIGHT NOW. NEVER copy the example zone name verbatim; always
  resolve against the neighbors block. (E.g. if the player is currently
  in West Ronfaure, the right neighbor leveling zone is East Ronfaure;
  if in Bastok Markets, it's North Gustaberg or South Gustaberg.)
  goals = [
    {id:"travel_out", type:"travel", title:"Travel to <neighbor zone>",
     state:"pending", origin:"auto", target_zone_name:"<neighbor zone>"},
    {id:"farm_lvl",  type:"engage_nearby", title:"Engage low-level mobs",
     state:"pending", origin:"auto", stop_when:{kill_count:25}, rest_hp_pct:70},
  ]
  roots = ["travel_out", "farm_lvl"]
  Notes: 2 sequential roots, travel BEFORE engage_nearby (need to be in
  the outdoor zone first), high-ish kill_count for headroom. If the
  player is ALREADY in a leveling zone (an outdoor zone with low-level
  mobs), drop the travel leaf and emit just the engage_nearby root.

# Replanning - preserve in-flight work

The world state includes a "Current goal tree" section showing every
goal already on disk. You may be called mid-plan (zone change,
periodic re-evaluation) - NOT just on a fresh user instruction.
**Read that section FIRST.**

  - If a goal is already `completed` (e.g. an equip you previously
    emitted), DO NOT re-emit it. The state on disk reflects what
    actually happened in-game.
  - If a goal is `pending` or `active` and is still the right thing to
    be doing, KEEP IT in your `update_goals` call (same id, same
    fields). update_goals replaces the whole tree, so leaving a pending
    goal out of your call deletes it.
  - Only emit a brand-new goal if the existing tree doesn't already
    cover what needs to happen next. The standing concerns below tell
    you what should always be present; if the current tree already has
    them, leave them be.
  - Only DROP an existing goal if it's no longer applicable (e.g. user
    changed direction, the goal's premise is now wrong).

# Standing concerns - apply on EVERY plan call, even if the user didn't ask

These are background priorities you should always weave into the goal
tree. They don't replace the user's primary goal; they sit alongside it.

## 1. Operate from zero knowledge - use general FFXI knowledge to make progress

The agent's mob/zone catalog accumulates from observation. Early on,
the world-state will show empty entity lists for most zones. That is
expected, NOT a blocker. Use your training-data FFXI knowledge to fill
the gap:

  - In a starter city with no observed mobs, leave by the appropriate
    gate to the surrounding outdoor zone. Bastok -> North/South Gustaberg
    or Zeruhn Mines. Sandy -> East/West Ronfaure. Windy -> East/West
    Sarutabaruta. Jeuno -> any of the surrounding fields. Selbina ->
    Valkurm Dunes. The travel layer handles cross-zone routing - you
    just emit `travel { target_zone: <id> }`.
  - You pass zone NAMES in travel goals (`target_zone_name`), not ids.
    The planner resolves the name against the catalog. The "Direct
    neighbors of zone N" block lists names that are one zoneline away;
    the full catalog at the bottom lists every name the agent knows.
    Use names exactly as they appear in either block (case-insensitive
    is fine, but the spelling has to match a catalog entry). Names not
    in the catalog cause the goal to be dropped.
  - Once outside, observed entities will start populating in subsequent
    plan calls. Until then, trust that low-level mobs (Worms, Crabs,
    Rabbits, Bees, Mandragoras, etc) exist near the zone-line and the
    agent will encounter them by walking.

## 2. Mob knowledge grows over time

`farm` goals require a known recorded mob `target_name` for that zone.
If you don't see specific mob names in the world state, do NOT invent
one - the farming director will reject unknown names and the goal will
fail. Use `engage_nearby` instead: it locks the closest visible mob
and fights it, with no name needed. Each kill the
addon observes catalogs the mob, so subsequent plans can switch to
named `farm` goals once the catalog has built up.

Default pattern for "level up in an unknown zone":
  1. travel to an appropriate outdoor zone (general FFXI knowledge)
  2. engage_nearby with stop_when.kill_count high enough to make real
     XP progress (15-30 typical for early levels)
  3. Once a future plan call shows specific mob names you've cataloged,
     you can refine to named `farm` goals if you want to focus on a
     particular spawn area or mob type.

# update_gambits - the FF12-style combat reaction list

Each gambit is `{id, priority, cooldown, trigger, action}`. The combat
addon walks the list every ~100ms; the FIRST gambit whose trigger
expression evaluates true (and whose cooldown has elapsed) fires its
action. Lower `priority` numbers fire first; ties broken by list order.

Gambits are stored per CONTEXT - a `(main_job, sub_job, party)` triple
where any field can be `null` to mean "any value." Common contexts:

  {} or {"main_job": null, "sub_job": null, "in_party": null}
        -> universal fallback ("*/*/*"), applies regardless of job/party
  {"main_job": "WAR"}
        -> all WAR play (any subjob, solo or party)
  {"main_job": "WAR", "sub_job": "NIN", "in_party": false}
        -> WAR/NIN soloing specifically

When the agent's live context changes (job change, party invite/leave),
the orchestrator recomputes the active list by MERGING every matching
context into one - more-specific gambits override less-specific ones
that share the same `id`. So a `*/*/*` "cure self below 30%" baseline
plus a `WAR/NIN/*` "Sneak Attack on full TP" addition both fire when
playing WAR/NIN. Use stable ids and reuse them across contexts when you
want to override a baseline.

Each `update_gambits` call REPLACES exactly one context's set - it does
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
Default targets: ability -> <me>, magic/ws -> <t>.

Cooldown is in seconds and keys off `id`, so reuse stable ids across calls
to keep cooldowns honest.

# Examples (illustrative, do not echo)

User: "go to selbina"
  update_goals: one travel leaf with target_zone=248.

User: "if my HP drops below 30%, cast Cure on myself"
  update_gambits (only - no goal): one gambit, trigger lt(self.hp_pct, 30),
  action magic Cure <me>, context = {} (universal fallback).

User: "farm bumblebees in the dunes"
  update_goals only - combat reactions are NOT this user's concern; the
  agent's existing gambits handle combat. Do not touch gambits.

User: "go to selbina, and bump the cure threshold to 40%"
  update_goals: travel->248. PLUS exactly one minimal gambit edit (the
  cure threshold) - this is the rare case where the user asked for both.
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
                                'enum': ['composite', 'travel', 'goto', 'farm',
                                         'engage_nearby', 'equip', 'wait'],
                            },
                            'subgoals': {
                                'type': 'array',
                                # Permissive: accept either string IDs
                                # (preferred) or nested goal objects.
                                # _apply_goals flattens any nested
                                # objects into the flat goals[] list.
                                # Schemes like Groq's strict tool-arg
                                # validation reject mixed array types
                                # if we constrain items to "string"
                                # only, even though many models prefer
                                # to nest. Empty items spec = anything.
                                'items': {},
                                'description': (
                                    'Subgoal references. Either string '
                                    'IDs of other goals in the flat '
                                    'goals[] list, OR full nested goal '
                                    'objects which will be flattened '
                                    'into goals[] at apply time.'
                                ),
                            },
                            'target_zone_name': {
                                'type': 'string',
                                'description': (
                                    'Travel destination by name (preferred). '
                                    'Resolved against the zone catalog at apply '
                                    'time; goal dropped if name unknown.'
                                ),
                            },
                            'target_zone': {
                                'type': 'integer',
                                'description': (
                                    'Travel destination by numeric id (legacy). '
                                    'Use target_zone_name instead - it avoids '
                                    'LLM-side id-table guessing.'
                                ),
                            },
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
                            'slot': {
                                'type': 'string',
                                'enum': [
                                    'main', 'sub', 'range', 'ammo',
                                    'head', 'body', 'hands', 'legs', 'feet',
                                    'neck', 'waist', 'ear1', 'ear2',
                                    'ring1', 'ring2', 'back',
                                ],
                                'description': 'Equipment slot (equip goal type).',
                            },
                            'item': {
                                'type': 'string',
                                'description': 'Item name to equip (equip goal type).',
                            },
                        },
                        'required': ['id', 'title', 'type', 'state'],
                    },
                },
                'roots': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Top-level goal ids in priority order.',
                },
                'rationale': {
                    'type': 'string',
                    'description': (
                        'One short first-person sentence (<=100 chars) in '
                        'the agent\'s voice explaining WHY this plan. '
                        'e.g. "Lvl 4 in W. Ronfaure, mobs here are good, '
                        'I\'ll just farm in place." Echoed in-game; keep '
                        'it tight, no preamble. Required.'
                    ),
                    'maxLength': 120,
                },
            },
            'required': ['goals', 'roots', 'rationale'],
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
            "condition->action rules; on every ~100ms tick the first one whose "
            "trigger evaluates true and whose cooldown has elapsed fires. "
            "Multiple matching contexts merge - more-specific overrides "
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
            "ids - use modify_gambit to change an existing one."
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
            "cooldown, trigger, action - each replaces its top-level field "
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
            "Don't invent interactions - the chat addon accretes real "
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
                 current_ctx_provider,
                 neighbors_provider=None):
        self.cfg = cfg
        self.llm = llm
        self.goal_manager = goal_manager
        # Shared with main.NavServer - the planner mutates the store
        # (via update_set / clear_set) and the orchestrator's context
        # watcher reads it on the next tick to redeploy the active list.
        self.gambits_store = gambits_store
        # Callable returning the current {main_job, sub_job, in_party}
        # context as canonical strings. Used to redeploy immediately
        # after a tool call, so the addon sees the new gambits this tick
        # rather than waiting on the watcher to notice the file change.
        self._current_ctx = current_ctx_provider
        # Callable: zone_id -> list[int] of zones directly reachable via
        # a single zoneline crossing. None falls back to "no direct-
        # neighbor info" - planner still gets the full zone catalog and
        # has to guess. Wiring this is what stops the LLM from picking
        # zone IDs that mismatch its intended destination name.
        self._neighbors_provider = neighbors_provider

    # ---- world-state snapshot ----------------------------------------

    def _world_state_text(self, zone_names: dict[int, str]) -> str:
        """Compact prompt-friendly description of where the player is and
        which zones are reachable to plan toward."""
        snap = self.goal_manager._snapshot()
        cur_zone_name = zone_names.get(snap.zone_id, '?') if snap.zone_id else '?'
        # Direct-neighbor block: zones reachable via a single zoneline
        # from where the player is right now. This is the canonical
        # source for picking a `target_zone` - the LLM doesn't have
        # FFXI's zone-ID table memorized, so without this it guesses
        # (e.g. "North Gustaberg -> 108" when 108 is actually Konschtat).
        neighbors_block = ''
        if self._neighbors_provider is not None and snap.zone_id is not None:
            try:
                ns = self._neighbors_provider(snap.zone_id)
            except Exception:
                ns = []
            if ns:
                neighbors_block = (
                    f'\nDirect neighbors of zone {snap.zone_id} '
                    f'(one zoneline away - use these IDs for travel):\n'
                    + '\n'.join(
                        f'  {zid:>3}  {zone_names.get(zid, "?")}'
                        for zid in ns
                    ) + '\n'
                )
        # The full zone catalog is ~200 entries - tiny in token terms,
        # and the LLM knows zone IDs only via what we tell it. Send all
        # so multi-hop travel goals (auto-routed by cross_zone_goto)
        # still have valid IDs to reference.
        zones_block = '\n'.join(
            f'  {zid:>3}  {name}' for zid, name in sorted(zone_names.items())
        )
        return (
            f'Current zone: {snap.zone_id} ({cur_zone_name})\n'
            f'Position:     ({snap.x}, {snap.y}, {snap.z})\n'
            f'Moving:       {snap.moving}\n'
            f'\n{self._self_text()}'
            f'\n{self._inventory_text()}'
            f'\n{self._current_goals_text()}'
            f'{neighbors_block}'
            f'\nAll known zones (id  name):\n{zones_block}\n'
        )

    def _current_goals_text(self) -> str:
        """Render the persistent goal tree so the LLM (re)plans with
        knowledge of what's already in flight. Critical on replan: a
        plan that doesn't see existing pending/active goals re-invents
        work the agent has already done.

        Format is intentionally compact - root id, type, state, title,
        then key type-specific fields on a second line. Composite
        children render under their parent indented."""
        goals = self.goal_manager.goals
        if not goals.roots:
            return 'Current goal tree: (empty)\n'
        nodes = goals.nodes
        lines = ['Current goal tree (preserve in-flight goals where possible):']

        def render(gid: str, depth: int) -> None:
            n = nodes.get(gid)
            if n is None:
                lines.append(f'{"  " * depth}- [{gid}] <missing node>')
                return
            t = n.get('type', '?')
            st = n.get('state', '?')
            title = n.get('title', '')
            lines.append(f'{"  " * depth}- [{gid}] {t}/{st}: {title}')
            # Type-specific summary on a second line - just the bits
            # the LLM needs to recognise this goal vs make a new one.
            extras: list[str] = []
            if t == 'travel':
                tn = n.get('target_zone_name') or n.get('target_zone')
                if tn is not None: extras.append(f'target={tn}')
            elif t == 'goto':
                tp = n.get('target_pos')
                if tp: extras.append(f'pos={tp}')
            elif t == 'equip':
                extras.append(f'{n.get("slot")}={n.get("item")}')
            elif t in ('farm', 'engage_nearby'):
                tn = n.get('target_name')
                if tn: extras.append(f'mob={tn}')
                sw = n.get('stop_when') or {}
                if sw.get('kill_count') is not None:
                    extras.append(f'kill_count={sw["kill_count"]}')
            if extras:
                lines.append(f'{"  " * (depth + 1)}{" ".join(extras)}')
            for cid in n.get('subgoals') or []:
                render(cid, depth + 1)

        for rid in goals.roots:
            render(rid, 0)
        return '\n'.join(lines) + '\n'

    # FFXI job ids -> 3-letter codes. Matches combat.lua's GetMainJob() output.
    _JOB_CODES = {
        0:  'NON', 1: 'WAR', 2: 'MNK', 3: 'WHM', 4: 'BLM', 5:  'RDM',
        6:  'THF', 7: 'PLD', 8: 'DRK', 9: 'BST', 10: 'BRD', 11: 'RNG',
        12: 'SAM', 13: 'NIN', 14: 'DRG', 15: 'SMN', 16: 'BLU', 17: 'COR',
        18: 'PUP', 19: 'DNC', 20: 'SCH', 21: 'GEO', 22: 'RUN',
    }

    def _self_text(self) -> str:
        """Render job/level/HP from combat.json. Best-effort - empty
        string if the channel hasn't published yet."""
        path = self.cfg.paths.state_dir(self.cfg.character) / 'combat.json'
        if not path.exists():
            return 'Self: <combat.json missing>\n'
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return 'Self: <combat.json unreadable>\n'
        s = d.get('self') or {}
        mj = self._JOB_CODES.get(s.get('main_job') or 0, '?')
        sj = self._JOB_CODES.get(s.get('sub_job')  or 0, '?')
        return (
            f'Self: {self.cfg.character} {mj}{s.get("main_job_lvl", "?")}'
            f'/{sj}{s.get("sub_job_lvl", 0)}  '
            f'HP {s.get("hp", "?")}/{s.get("hp_max", "?")}  '
            f'MP {s.get("mp", "?")}/{s.get("mp_max", "?")}\n'
        )

    # Equipment-slot enum (fine-grained, what /equip and goal `slot`
    # field use) -> bitmask category name (what the inventory addon
    # reports in `equip_slots`). ear1/ear2 collapse to "ear", etc.
    _SLOT_TO_CATEGORY = {
        'main':  'main',  'sub':   'sub',   'range': 'range', 'ammo':  'ammo',
        'head':  'head',  'body':  'body',  'hands': 'hands', 'legs':  'legs',
        'feet':  'feet',  'neck':  'neck',  'waist': 'waist',
        'ear1':  'ear',   'ear2':  'ear',
        'ring1': 'ring',  'ring2': 'ring',
        'back':  'back',
    }

    def _read_inventory_state(self) -> dict[str, Any] | None:
        """Parse inventory.json once. Returns the raw payload (with
        equipped dict + containers dict). None on missing/malformed -
        caller treats absence as 'no inventory data available'."""
        path = self.cfg.paths.state_dir(self.cfg.character) / 'inventory.json'
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _inventory_text(self) -> str:
        """Render equipped slots + unequipped equippable items so the LLM
        can spot upgrade opportunities. Skips bag items that aren't
        equippable (key items, consumables) - they don't inform planning."""
        d = self._read_inventory_state()
        if d is None:
            return 'Inventory: <inventory.json missing or unreadable>\n'
        equipped = d.get('equipped') or {}
        # Render all 16 slots in canonical order so empty slots are
        # visible - the agent needs to know "main is empty" to realize
        # equipping a sword is an option.
        slot_order = (
            'main', 'sub', 'range', 'ammo',
            'head', 'body', 'hands', 'legs', 'feet',
            'neck', 'waist', 'ear1', 'ear2', 'ring1', 'ring2', 'back',
        )
        eq_lines = []
        for slot in slot_order:
            it = equipped.get(slot)
            if it:
                eq_lines.append(f'  {slot:>5}: {it.get("name", "?")}')
            else:
                eq_lines.append(f'  {slot:>5}: <empty>')
        # Equipped items physically stay in the bag - FFXI doesn't move
        # them. Track which (container, slot) tuples are currently
        # equipped so we don't double-list them as "unequipped."
        equipped_tuples = {
            (it.get('container'), it.get('slot'))
            for it in equipped.values() if it
        }
        # Unequipped equippable items from the main inventory bag only.
        # Other containers (safe, storage) aren't in reach without a
        # mog house visit, so they don't inform on-the-fly equip choices.
        bag = (d.get('containers') or {}).get('inventory') or {}
        unequipped = []
        for it in bag.get('items') or []:
            slots = it.get('equip_slots') or []
            if not slots:
                continue
            # Container 0 = inventory bag.
            if (0, it.get('slot')) in equipped_tuples:
                continue
            unequipped.append(
                f'  bag-slot {it.get("slot"):>2}: {it.get("name", "?"):<22}'
                f' fits={",".join(slots)}  lvl_req={it.get("lvl_req", 0)}'
            )
        eq_block = '\n'.join(eq_lines)
        un_block = '\n'.join(unequipped) if unequipped else '  (none)'
        return f'Equipped:\n{eq_block}\n\nUnequipped equippable (main bag):\n{un_block}\n'

    # ---- tool-call appliers -----------------------------------------

    _VALID_GOAL_TYPES = {
        'composite', 'travel', 'goto', 'farm', 'engage_nearby', 'equip', 'wait',
    }

    def _flatten_nested_subgoals(self, goals_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Walk every goal and recursively pull nested subgoal objects
        up into a single flat list. After this pass, every `subgoals`
        field is a list of string IDs that reference other entries in
        the returned list. Goals appear once each; duplicate IDs are
        dropped (first occurrence wins)."""
        flat: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        def visit(g: Any) -> str | None:
            if not isinstance(g, dict):
                return None
            gid = g.get('id')
            if not isinstance(gid, str) or not gid:
                return None
            if gid in seen_ids:
                return gid  # dedupe but still return id for parent's subgoal list
            seen_ids.add(gid)
            sgs = g.get('subgoals')
            if isinstance(sgs, list):
                normalized: list[str] = []
                for sg in sgs:
                    if isinstance(sg, str):
                        normalized.append(sg)
                    elif isinstance(sg, dict):
                        sg_id = visit(sg)
                        if sg_id is not None:
                            normalized.append(sg_id)
                g['subgoals'] = normalized
            flat.append(g)
            return gid

        for g in goals_in:
            visit(g)
        return flat

    def _apply_goals(self, args: dict[str, Any], zone_names: dict[int, str]) -> bool:
        goals_list = args.get('goals') or []
        roots = args.get('roots') or []
        if not goals_list or not roots:
            print('  planner: update_goals missing goals/roots')
            return False
        # Flatten any nested-object subgoals into the flat goals list.
        # Llama-4-Scout (and probably others) prefers to emit composites
        # with full nested goal definitions inside `subgoals` rather than
        # ID references. We accept either shape via the permissive schema
        # and normalize here so the goal manager only ever sees the flat
        # form it expects (subgoals = [string, string, ...]).
        goals_list = self._flatten_nested_subgoals(goals_list)
        # Build a name -> id lookup once. Case-insensitive match; falls
        # back to substring match (so the LLM can write "Gustaberg"
        # and we resolve to the unique full name if there is one).
        name_to_id = {n.lower(): zid for zid, n in zone_names.items()}
        def resolve_zone(name: str) -> int | None:
            n = name.strip().lower()
            if n in name_to_id:
                return name_to_id[n]
            # Substring fallback - only commit if exactly one zone name
            # contains the substring, otherwise it's ambiguous.
            hits = [(zid, fn) for fn, zid in name_to_id.items() if n in fn]
            if len(hits) == 1:
                return hits[0][0]
            return None

        # Snapshot equipped + bag items once for equip-goal validation.
        # Layout: equipped_categories = set of canonical slot names
        # currently filled (so we can reject "equip X to slot already
        # holding something"); bag_items = {name -> list[equip_slots]}.
        # When inventory hasn't published yet we skip equip validation
        # rather than dropping all equip goals (which would block the
        # standing-concerns flow before inventory.json exists).
        inv_state = self._read_inventory_state()
        equipped_slots: set[str] = set()
        bag_items_by_name: dict[str, list[str]] = {}
        if inv_state is not None:
            for slot_name, item in (inv_state.get('equipped') or {}).items():
                if item is not None:
                    equipped_slots.add(slot_name)
            bag = (inv_state.get('containers') or {}).get('inventory') or {}
            for it in bag.get('items') or []:
                name = it.get('name')
                slots = it.get('equip_slots') or []
                if name and slots:
                    bag_items_by_name.setdefault(name, slots)

        nodes_dict: dict[str, dict[str, Any]] = {}
        dropped: list[str] = []
        for g in goals_list:
            gid = g.get('id')
            if not gid:
                dropped.append('<no id>')
                continue
            # Force pending state on every node - the manager owns state
            # transitions, the planner doesn't.
            g['state'] = 'pending'
            # Defensive validation: an LLM-produced goal missing `type`
            # or with an unknown type wedges the goal manager (it can't
            # dispatch what it doesn't recognise). Drop such nodes here
            # so the user sees the failure in the log instead of silently
            # bricking the agent.
            gtype = g.get('type')
            if gtype not in self._VALID_GOAL_TYPES:
                dropped.append(f'{gid} (bad type={gtype!r})')
                continue
            # Equip-goal sanity checks. Two failure modes the LLM hits:
            #   (a) wrong slot for the item (ring -> sub, sword -> head)
            #   (b) target slot already holds an item (would no-op or,
            #       worse, swap with the equipped piece)
            # Both produce silent in-game failures; drop here so the
            # log shows the LLM's mistake instead of the agent wedging.
            # Skip checks if inventory.json hasn't published yet - the
            # goal manager will fail the equip later if the item really
            # isn't there.
            if gtype == 'equip' and inv_state is not None:
                slot = g.get('slot')
                item = g.get('item')
                if not slot or not item:
                    dropped.append(f'{gid} (equip missing slot/item)')
                    continue
                category = self._SLOT_TO_CATEGORY.get(slot)
                if category is None:
                    dropped.append(f'{gid} (equip bad slot {slot!r})')
                    continue
                if slot in equipped_slots:
                    dropped.append(f'{gid} (slot {slot!r} already occupied)')
                    continue
                fits = bag_items_by_name.get(item)
                if fits is None:
                    dropped.append(f'{gid} (item {item!r} not in inventory)')
                    continue
                if category not in fits:
                    dropped.append(
                        f'{gid} (item {item!r} fits {fits}, not {category!r})'
                    )
                    continue
            # Resolve travel goals' name -> numeric id. Goal manager and
            # the addon both work in ids; the name-based input only
            # exists so the LLM doesn't have to guess id mappings.
            if gtype == 'travel':
                tname = g.get('target_zone_name')
                if tname:
                    resolved = resolve_zone(tname)
                    if resolved is None:
                        dropped.append(
                            f'{gid} (unknown zone name {tname!r})'
                        )
                        continue
                    g['target_zone'] = resolved
                    # Keep target_zone_name on the node so dashboards /
                    # debug logs show what the LLM intended, not just
                    # the resolved id.
                elif g.get('target_zone') is None:
                    dropped.append(f'{gid} (travel missing target_zone_name)')
                    continue
            nodes_dict[gid] = g
        # Composite-specific validation: a composite with no subgoals
        # is dead weight (the goal manager treats it as a leaf and never
        # makes progress). Subgoal IDs that don't exist in the flat
        # nodes_dict are dangling pointers; trim them. If trimming
        # leaves the composite empty, drop the composite too.
        for gid in list(nodes_dict.keys()):
            g = nodes_dict[gid]
            if g.get('type') != 'composite':
                continue
            sgs = list(g.get('subgoals') or [])
            valid_sgs = [sgid for sgid in sgs if sgid in nodes_dict]
            if len(valid_sgs) != len(sgs):
                missing = [s for s in sgs if s not in nodes_dict]
                print(f'  planner: composite {gid!r} subgoals trimmed '
                      f'(dropped missing refs: {missing})')
                g['subgoals'] = valid_sgs
            if not valid_sgs:
                dropped.append(f'{gid} (composite with no valid subgoals)')
                del nodes_dict[gid]
        if dropped:
            print(f'  planner: dropped malformed goals: {dropped}')
        # If a root references a dropped node, drop the root too -
        # otherwise the goal manager walks into a dangling pointer.
        roots = [r for r in roots if r in nodes_dict]
        # Recovery for the common LLM mistake: if validation dropped
        # all roots BUT there are still meaningful leaf goals in the
        # flat list (they were defined as composite children that got
        # orphaned), promote them to roots in original list order.
        # Without this we'd toss a usable plan because of a wrapping
        # mistake. Skip composites - only promote actionable leaves.
        if not roots and nodes_dict:
            promoted = [
                gid for gid in nodes_dict
                if nodes_dict[gid].get('type') != 'composite'
            ]
            if promoted:
                print(f'  planner: no valid roots; promoting orphan leaves to roots: {promoted}')
                roots = promoted
        if not roots:
            print('  planner: no valid roots after validation')
            return False
        new_goals = _persistence.Goals(nodes=nodes_dict, roots=list(roots))
        new_goals.save(self.goal_manager._goals_path)
        self.goal_manager.goals = new_goals
        self.goal_manager._last_dispatch.clear()
        self.goal_manager._active_leaf_id = None
        print(f'  planner: applied {len(nodes_dict)} goal node(s), '
              f'{len(roots)} root(s)')
        return True

    # Gambit-mutation handlers - store changes only, no redeploy here.
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

    def _do_research(self, user_text: str, ws: str) -> str:
        """Pre-plan research phase. Lets the LLM use web_search /
        web_fetch to verify era-correct FFXI facts (zone level ranges,
        quest prerequisites, job unlock steps) before committing to a
        plan. Returns a text summary that gets prepended to the plan
        prompt; on any error returns empty string and the plan call
        proceeds with training-data knowledge only.

        Cached: identical (user_text, zone) pairs hit the disk cache,
        so the second plan in a session adds ~0s. First plan adds
        ~5-30s depending on how many lookups the LLM does."""
        research_system = (
            _web_research.ERA_CONSTRAINT
            + '\n\nYou are doing pre-plan research, NOT yet committing '
              'to a plan. Use web_search and web_fetch to gather facts '
              'relevant to the user instruction (zone level ranges, '
              'mob types, quest prereqs, job-unlock steps). Prefer '
              'queries that target classicffxi.fandom.com or the '
              'LandSandBoat repo. End with a concise 3-6 sentence '
              'summary of the facts that will inform the plan. Do NOT '
              'propose a plan or list goals - that is the next step.'
        )
        user_prompt = (
            f'User instruction:\n  "{user_text}"\n\n'
            f'World state (current):\n{ws}\n\n'
            f'Research what era-correct facts you need to make a good '
            f'plan. Be thorough but concise - 1-3 searches and 0-2 '
            f'fetches is usually enough.'
        )
        try:
            result = self.llm.run_tool_loop(
                tier='deliberative',
                system_prompt=research_system,
                user_prompt=user_prompt,
                tools=[_web_research.WEB_SEARCH_TOOL,
                       _web_research.WEB_FETCH_TOOL],
                tool_handlers=_web_research.make_handlers(self.cfg),
                max_iters=6,
                max_tokens=1024,
                source='planner_research',
            )
            return (result.final_text or '').strip()
        except Exception as e:
            print(f'  planner: research phase failed: {e}')
            return ''

    def plan(self, user_text: str, zone_names: dict[int, str]) -> bool:
        """Send the user instruction + world state to the LLM. Apply
        whichever tool calls come back (goals, gambits, or both).
        Returns True if at least one tool was applied successfully."""
        if not self.llm.available:
            print('  planner: LLM unavailable; skipping.')
            return False

        ws = self._world_state_text(zone_names)
        # Research first - gives the LLM era-correct facts to plan
        # against. Empty string on error / unavailable; the plan still
        # runs from training-data knowledge in that case.
        research_summary = self._do_research(user_text, ws)
        if research_summary:
            print(f'  planner: research notes:\n{research_summary[:500]}')
        research_block = (
            f'\nResearch notes (era-correct, use these over your '
            f'training data):\n{research_summary}\n'
            if research_summary else ''
        )
        user_msg = (
            f'User instruction:\n  "{user_text}"\n\n'
            f'World state:\n{ws}\n'
            f'{research_block}'
            f'Plan the agent\'s response. Use `update_goals` for what to '
            f'do (where to go, what to accomplish). Touch gambits ONLY if '
            f'the user mentioned combat behavior - and prefer add_gambit / '
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

        try:
            cr = self.llm.tool_chat(
                tier='deliberative',
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user',   'content': user_msg},
                ],
                tools=[
                    UPDATE_GOALS_TOOL,
                    ADD_GAMBIT_TOOL,
                    MODIFY_GAMBIT_TOOL,
                    REMOVE_GAMBIT_TOOL,
                    UPDATE_GAMBITS_TOOL,
                    CLEAR_GAMBIT_SET_TOOL,
                    UPDATE_PLAYER_TOOL,
                ],
                tool_choice='required',
                max_tokens=4096,
            )
        except Exception as e:
            print(f'  planner: LLM call failed: {e}')
            return False
        latency = cr.latency_s

        if not cr.tool_calls:
            print('  planner: model did not return a tool call '
                  f'(finish={cr.finish_reason}, '
                  f'in={cr.input_tokens} out={cr.output_tokens}, '
                  f'content={cr.text[:300]!r})')
            return False

        # Walk every tool call in order. Gambit mutations accumulate; we
        # redeploy the resolved active list once at the end so the addon
        # sees the final merged state, not intermediate ones. Goals
        # collapse to the last call (replacing the tree twice is moot).
        applied: list[str] = []
        last_goals_args: dict[str, Any] | None = None
        gambit_mutations = 0
        player_updates = 0
        for call in cr.tool_calls:
            name = call.name
            args = call.arguments
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
                print(f'  planner: redeployed gambits -> {len(merged)} active')
                applied.append(f'gambitsx{gambit_mutations}')
            except Exception as e:
                print(f'  planner: gambit redeploy failed: {e}')
        if last_goals_args is not None:
            if self._apply_goals(last_goals_args, zone_names):
                applied.append('goals')
        if player_updates:
            applied.append(f'playersx{player_updates}')

        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='planner',
            type_='plan_generated',
            user_text=user_text,
            tier='deliberative',
            model=self.cfg.llm.deliberative,
            input_tokens=cr.input_tokens,
            output_tokens=cr.output_tokens,
            latency_s=round(latency, 3),
            applied=applied,
        )
        if not applied:
            print('  planner: no tools applied')
            _echo.to_chat(self.cfg, 'plan',
                          f'no plan produced for {user_text!r}')
            return False
        print(f'  planner: applied {", ".join(applied)} in {latency:.2f}s '
              f'({cr.input_tokens}->{cr.output_tokens} tokens)')
        # First-person echo: prefer the LLM's `rationale` field (added
        # to UPDATE_GOALS_TOOL specifically so plan summaries read in
        # the agent's voice). Fall back to root titles if the LLM
        # omitted rationale, then to the applied-tools list.
        rationale = ''
        if last_goals_args is not None:
            rationale = str(last_goals_args.get('rationale') or '').strip()
        if rationale:
            _echo.to_chat(self.cfg, 'plan', rationale)
        else:
            try:
                roots = self.goal_manager.goals.roots
                titles = []
                for rid in roots[:4]:
                    node = self.goal_manager.goals.nodes.get(rid) or {}
                    t = node.get('title') or node.get('type') or rid
                    titles.append(t)
                if titles:
                    _echo.to_chat(self.cfg, 'plan', ' -> '.join(titles))
                else:
                    _echo.to_chat(self.cfg, 'plan', ', '.join(applied))
            except Exception:
                _echo.to_chat(self.cfg, 'plan', ', '.join(applied))
        return True
