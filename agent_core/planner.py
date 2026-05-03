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
from pathlib import Path
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


SYSTEM_PROMPT = _web_research.ERA_CONSTRAINT + '\n\n' + """You are the player AT THE KEYBOARD playing an FFXI character - NOT
the in-game character. Talk like a real person planning their game
session: practical, casual, focused on the numbers and the goal.
NOT in-character roleplay (no "I, the warrior of San d'Oria...",
no oaths, no heroic narration).

When you call `update_goals`, ALWAYS populate the `rationale` field
with ONE short first-person sentence (<=100 chars) - what the player
is thinking, why this plan. Echoed in-game.

ZONE NAME RULE - read carefully:
- The world state has a line ">>> CURRENT LOCATION: <NAME> ... <<<".
  This is the literal zone the agent is standing in right now.
- If the rationale mentions the agent's current zone, you MUST copy
  the EXACT zone name from that line. NOT a similar-sounding zone
  (West Ronfaure != East Ronfaure), NOT a zone from research notes,
  NOT a famous zone you remember, NOT an outdoor zone next door
  just because the current zone is a city - the literal current
  location.
- If you're emitting a travel goal, mention the destination zone by
  name (also exact - take it from the neighbors block).

CITY ZONE RULE:
- City zones (Bastok Markets, Bastok Mines, Northern San d'Oria,
  Southern San d'Oria, Port San d'Oria, Windurst Walls, Windurst
  Waters, Windurst Woods, Port Windurst, Lower Jeuno, Upper Jeuno,
  Port Jeuno, Ru'Lude Gardens, Tavnazian Safehold, Selbina, Mhaura,
  Norg, Kazham, etc.) have NO engageable mobs. If CURRENT LOCATION
  is a city, do NOT emit engage_nearby with no preceding travel -
  there's nothing to fight. Emit a travel goal to an adjacent
  outdoor zone first. Cities are for buying/selling/quests, not
  leveling.

Other rationale rules:
- The job and level MUST match the "Self:" line of the world state.
- DO NOT make up numbers, copy example wording verbatim, or use
  third-person.

GOOD shape (player voice; substitute REAL world-state values):
  "<job><lvl> in <current zone name>, <short reason for the plan>."
GOOD examples (use placeholders, NOT these zone names verbatim):
  "<JOB><LVL> in <CITY>, heading to <NEIGHBOR> for mobs."
  "<JOB><LVL> in <OUTDOOR ZONE>, mobs here are good, farming."
BAD examples (in-character voice):
  "I, brave warrior, shall conquer these beasts."
  "By Altana's grace, I will reach level 10."

DO NOT copy zone names from these examples. ALWAYS substitute the
actual current zone (from the CURRENT LOCATION line) or an actual
neighbor (from the Direct neighbors block).

TRAVEL DESTINATION RULE - weigh time cost against unique value:

Each zoneline away costs the agent 30s-2min of real wall-clock,
spent walking through monsters with no in-flight task. So travel
is justified only when the destination offers something the
current zone (and its direct neighbors) doesn't.

Two cases:

1. UNIQUE-VALUE travel - LONG ROUTES ARE FINE.
   Quest NPC in a specific city, a vendor with an item you can't
   buy locally, a key item that only drops in one zone, a level-
   appropriate camp that genuinely doesn't exist near the player
   - travel cost is intrinsic to the goal. Pick the actual
   destination even if it's many zonelines away. The travel layer
   handles multi-hop pathing.

2. INTERCHANGEABLE-VALUE travel - PREFER STAYING.
   For repeatable activities like "level up", "kill some mobs",
   "get XP" - the current outdoor zone almost always has mobs of
   the appropriate tier already. Don't run an hour to a famous
   leveling zone when you could fight where you stand. Example:
   from South Gustaberg, do NOT travel to West Ronfaure for XP -
   the mobs are equivalent and the trip is multiple zonelines of
   wasted time. Stay and engage_nearby.

Practical defaults:
- If CURRENT LOCATION is an outdoor zone and the user goal is
  "level up" / "get XP" / "kill mobs", emit engage_nearby only
  - NO travel leaf, regardless of how famous another zone is.
- If CURRENT LOCATION is a city, you DO need travel - but pick
  the cheapest viable destination from the "Direct neighbors of
  zone N" block (the adjacent outdoor zone), not a famous zone
  on the other side of Vana'diel.
- For unique-value goals, pick the actual destination by name
  even if it's not a direct neighbor; the travel layer pathfinds
  the rest.

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
                    stop_when:   { target_level: <int> }
                                 | { kill_count:   <int> },
                    rest_hp_pct: <int> }                # default 70
                  drive a kill loop on the named mob in the player's
                  CURRENT zone; the agent locks the mob, attacks, rests
                  when low HP, repeats until stop_when fires. The
                  player must already be near spawn points for the
                  named mob.
  engage_nearby   { stop_when:   { target_level: <int> }
                                 | { kill_count:   <int> },
                    rest_hp_pct: <int> }                # default 70
                  Same kill loop as `farm` but with NO named target -
                  the agent locks the closest visible mob, fights it,
                  picks the next one after the kill. Use this in zones with
                  empty/unknown mob catalogs (the typical case early
                  on). Each kill auto-catalogs the mob's name + spawn
                  area, so future plans CAN use named `farm` goals.
                  Fails if no enemy is in range within ~20s.

  stop_when (farm/engage_nearby) - PICK THE ONE THAT MATCHES THE GOAL:
    target_level: <int>
      Completes once the player's main-job level reaches <int>. THE
      RIGHT CHOICE FOR ANY "reach level X" / "level up to X" / "get
      to level X" user goal. Don't try to estimate kill_count from
      XP tables - kill rates and XP-per-kill vary too much by mob
      tier and check_type, and undershooting just ping-pongs the
      planner. Use the user's stated target level directly.
    kill_count: <int>
      Completes after exactly N kills. Use ONLY for narrow tasks
      where the kill count itself is the deliverable - "kill 5
      Yagudo for a quest", "farm 10 Bumblebee Wings" - not for XP.
    Omit both for indefinite farming (the planner will be re-invoked
    on zone change or other events). Don't combine; pick one.
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
      {id:"c", type:"engage_nearby", title:"...", state:"pending", stop_when:{target_level:10}},
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
  This is interchangeable-value travel (any level-appropriate zone
  serves equally), so favor the cheapest path. First, decide if you
  need to travel AT ALL:

  - If CURRENT LOCATION is already an outdoor zone with mobs near
    the player's level: NO travel leaf. Emit just the engage_nearby
    root with stop_when.target_level set to the user's target.
  - If CURRENT LOCATION is a city: emit travel to the cheapest
    appropriate outdoor zone, picked from the "Direct neighbors of
    zone N" block (one zoneline away).

  Two-leaf shape (city case only):
    goals = [
      {id:"travel_out", type:"travel", title:"Travel to <neighbor zone>",
       state:"pending", origin:"auto", target_zone_name:"<neighbor zone>"},
      {id:"farm_lvl",  type:"engage_nearby", title:"Engage low-level mobs",
       state:"pending", origin:"auto", stop_when:{target_level:10}, rest_hp_pct:70},
    ]
    roots = ["travel_out", "farm_lvl"]

  One-leaf shape (already in an outdoor zone — the common case):
    goals = [
      {id:"farm_lvl",  type:"engage_nearby", title:"Engage low-level mobs",
       state:"pending", origin:"auto", stop_when:{target_level:10}, rest_hp_pct:70},
    ]
    roots = ["farm_lvl"]

  Notes: stop_when.target_level matches the user's stated target so
  the leaf doesn't auto-complete mid-grind, looping the planner.
  Resist the urge to add a travel leaf "for a better camp" - if the
  current outdoor zone has mobs at the player's level (it almost
  always does), staying is the right call.

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

  - In a city with no observed mobs, leave by an adjacent zoneline to
    a leveling outdoor zone. The "Direct neighbors of zone N" block
    in the world state is the AUTHORITATIVE list of zones one
    transition away - pick from there. Do NOT pick a zone on the
    other side of Vana'diel because it's famous - the travel layer
    will path through every intervening zone, taking minutes.
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
  2. engage_nearby with stop_when.target_level set to the user's
     stated target level (NOT a kill_count - see stop_when section).
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
                                'properties': {
                                    'kill_count':   {'type': 'integer'},
                                    'target_level': {'type': 'integer'},
                                },
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
            f'>>> CURRENT LOCATION: {cur_zone_name} '
            f'(zone id {snap.zone_id}) <<<\n'
            f'(this is the LITERAL zone the agent is standing in right '
            f'now - not a nearby zone, not a similar-sounding one, not '
            f'a famous zone you remember; the rationale and any '
            f'"farm in place" claim MUST refer to this exact zone)\n'
            f'Position:         ({snap.x}, {snap.y}, {snap.z})\n'
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

    # Reject snapshots older than this when gating plan calls. Long
    # enough that a busy poll loop doesn't false-trip; short enough
    # that we catch post-respawn / post-zone "addons paused publishing"
    # windows where the file shows pre-event state.
    SNAPSHOT_FRESH_S = 5.0

    def _is_path_fresh(self, path: Path) -> bool:
        try:
            mtime = path.stat().st_mtime
        except (OSError, FileNotFoundError):
            return False
        return (time.time() - mtime) <= self.SNAPSHOT_FRESH_S

    def _read_self_state(self) -> dict[str, Any] | None:
        """Read combat.json's `self` block, with two staleness guards:

        1. mtime check - if the file hasn't been written within
           SNAPSHOT_FRESH_S, treat as not-published (post-zone /
           post-respawn windows where the addon hasn't caught up yet
           often have multi-second-old combat.json showing pre-event
           HP / zone, which would feed bad data into the plan).
        2. content check - main_job > 0 AND main_job_lvl > 0. The
           addon transiently publishes main_job=0 (NON) during zoning
           and character select.

        Falls back to the last-known-good cached value if the current
        read fails either guard. Returns None if we have neither a
        fresh read nor a cache."""
        path = self.cfg.paths.state_dir(self.cfg.character) / 'combat.json'
        s: dict[str, Any] | None = None
        # Don't even read the file if mtime says it's stale - the
        # data inside would be untrustworthy regardless of content.
        if path.exists() and self._is_path_fresh(path):
            try:
                with open(path) as f:
                    d = json.load(f)
                s = d.get('self') or {}
            except (OSError, json.JSONDecodeError):
                s = None
        loaded = (
            isinstance(s, dict)
            and (s.get('main_job') or 0) > 0
            and (s.get('main_job_lvl') or 0) > 0
        )
        if loaded:
            self._cached_self = s
            return s
        return getattr(self, '_cached_self', None)

    def has_valid_self(self) -> bool:
        """True when we can plan with confidence. Returns False if
        either combat.json OR nav_status.json is stale (post-zone /
        post-respawn window where addons paused publishing) - planning
        against stale data produced "in East Ronfaure, need to recover
        HP" while actually in Bastok at full HP."""
        # combat.json freshness is enforced inside _read_self_state.
        if self._read_self_state() is None:
            return False
        # nav_status.json carries the zone_id + position the rationale
        # needs. Stale = "recently zoned, addon still publishing prior
        # zone" = wrong zone in plan.
        nav_path = self.cfg.paths.ipc_base / 'nav_status.json'
        if not self._is_path_fresh(nav_path):
            return False
        return True

    def _self_text(self) -> str:
        """Render job/level/HP for the planner prompt. Falls back to
        the cached last-known-good state if combat.json's current self
        block is stale (NON0 etc.)."""
        s = self._read_self_state()
        if not s:
            return 'Self: <combat.json not yet published>\n'
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
        'interact_npc',
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

    # Zones with no engageable mobs - cities, residential areas, port
    # towns. If a plan includes a travel goal to one of these AND an
    # engage_nearby/farm leaf, the farm leaf would fail immediately on
    # arrival. We reject the whole plan and force a replan rather than
    # commit and burn a goal cycle.
    _CITY_ZONE_NAMES = frozenset(name.lower() for name in [
        'Bastok Markets', 'Bastok Mines',
        'Northern San d\'Oria', 'Southern San d\'Oria', 'Port San d\'Oria',
        'Windurst Walls', 'Windurst Waters', 'Windurst Woods', 'Port Windurst',
        'Lower Jeuno', 'Upper Jeuno', 'Port Jeuno', 'Ru\'Lude Gardens',
        'Tavnazian Safehold',
        'Selbina', 'Mhaura', 'Norg', 'Kazham',
        'Rabao', 'Aht Urhgan Whitegate',  # the latter is era-filtered
                                          # but harmless to keep listed
    ])

    def _validate_no_city_farm(self, goals_list: list[dict[str, Any]],
                                roots: list[str]) -> bool:
        """Reject plans that pair travel-to-city with farm/engage_nearby.
        Cities have no mobs - the farm leaf would activate on arrival
        and fail. Returns True if the plan is valid (or doesn't have
        the conflict), False if rejected."""
        nodes = {n.get('id'): n for n in goals_list if n.get('id')}
        # Find any farm/engage_nearby leaves in the plan.
        farm_ids = [
            n['id'] for n in goals_list
            if n.get('type') in ('farm', 'engage_nearby') and n.get('id')
        ]
        if not farm_ids:
            return True
        # Find any travel leaves whose target_zone_name is a city.
        bad_travels: list[str] = []
        for n in goals_list:
            if n.get('type') != 'travel':
                continue
            tname = (n.get('target_zone_name') or '').strip().lower()
            if tname and tname in self._CITY_ZONE_NAMES:
                bad_travels.append(f'{n.get("id")}->{n.get("target_zone_name")}')
        if not bad_travels:
            return True
        print(f'  planner: REJECTED plan - travel(s) to city zone '
              f'{bad_travels} alongside farm/engage_nearby leaves '
              f'{farm_ids}. Cities have no mobs; the farm leaf would '
              f'fail on arrival. Forcing replan.')
        return False

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
        # Hard server-side rejection: travel-to-city followed by
        # engage_nearby/farm. Cities have no engageable mobs, so the
        # farm leaf would activate after travel completes and immediately
        # fail. The system prompt has rules against this but small models
        # ignore them often enough to need a hard validator. Force a
        # replan rather than commit a broken plan.
        if not self._validate_no_city_farm(goals_list, roots):
            return False
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
        # Refuse to plan against a stale "not loaded yet" snapshot.
        # During zone change / fresh login / character select the
        # addon publishes main_job=0 + main_job_lvl=0, which would
        # have the LLM produce rationales like "NON0 in <zone>...".
        # The caller (poll_user_goal_file / poll_idle_replan / etc.)
        # will retry on the next tick once valid state lands.
        if not self.has_valid_self():
            print('  planner: snapshot stale or self-state not loaded; '
                  'deferring plan (addons probably mid-zone or post-respawn).')
            return False

        """Send the user instruction + world state to the LLM. Apply
        whichever tool calls come back (goals, gambits, or both).
        Returns True if at least one tool was applied successfully."""
        if not self.llm.available:
            print('  planner: LLM unavailable; skipping.')
            return False

        # In-game heads-up: a plan call is 60-90s on the deliberative
        # tier (research phase + plan phase). Without this echo the
        # screen sits silent and someone watching has no way to know
        # the agent isn't just stuck. Templated, instant - no LLM.
        _echo.to_chat(self.cfg, 'thinking', 'Reflecting and planning...')

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
