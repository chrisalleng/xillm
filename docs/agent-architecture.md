# Plan: Autonomous LLM Agent for Final Fantasy XI — Architecture

## Vision

Turn the existing nav-addon prototype into a fully autonomous LLM-controlled FFXI agent. The agent should hold long-running goals (e.g. "unlock a subjob"), decompose them into sub-goals, and execute the resulting plans by interacting with every relevant game subsystem: navigation, NPCs, combat, inventory, chat. The LLM owns *what* to do; the client owns *how* and *when* in real time.

This document captures the **architecture** only — no implementation yet. We iterate until the major decisions are settled, then write per-subsystem implementation plans.

## Hard constraints (drive the architecture)

1. **Real-time game loop.** FFXI runs at 60 fps. The agent must react in subsecond timeframes for combat, but LLM round-trips are 2–30s. The LLM **cannot** sit in the render loop.
2. **LLM is expensive.** Every call costs money and adds latency. Architecture must minimise calls — call only on events the LLM uniquely solves.
3. **Game state is observable but partial.** The Ashita API exposes a lot, but some things (NPC dialog text, menu options, drop notifications) only arrive as packets the addon must intercept.
4. **Crashes happen.** Ashita can crash, the network can drop, the agent should resume from where it left off when restarted.
5. **Tooling already exists for some areas.** `enternity`, `packer`, `luashitacast` cover chunks of the work — we configure them, we don't reimplement them.

## Three-tier architecture

```
┌────────────────────────────────────────────────────────────┐
│  Tier 3: Deliberative agent (LLM)                          │
│  - Goal planning / re-planning                             │
│  - Gambit tuning from combat-log analysis                  │
│  - Chat composition                                        │
│  - Dialog/menu option choice when ambiguous                │
│  Cadence: event-driven + periodic (~minutes)               │
└────────────────────────────────────────────────────────────┘
                     ▲              │
            world state             │ tool calls
            + events                │ (modify goals/gambits,
                     │              │  send chat, etc.)
                     │              ▼
┌────────────────────────────────────────────────────────────┐
│  Tier 2: Reactive agent (Python orchestrator)              │
│  - Live world model (aggregated from addon state files)    │
│  - Goal executor (translates active goal → addon configs)  │
│  - Gambit engine driver / monitor                          │
│  - Event dispatcher (decides when to call LLM)             │
│  - Persistent state (goals, gambits, knowledge base)       │
│  Cadence: 1–5 Hz                                           │
└────────────────────────────────────────────────────────────┘
                     ▲              │
            JSON state              │ JSON commands
                     │              ▼
┌────────────────────────────────────────────────────────────┐
│  Tier 1: Game client (Ashita addons)                       │
│  - One addon per game subsystem (nav, interact, combat...) │
│  - Reads game memory, intercepts packets                   │
│  - Issues game commands (/raw text, autofollow, etc.)      │
│  - Reactive Lua loops for sub-second response              │
│  Cadence: 60 fps                                           │
└────────────────────────────────────────────────────────────┘
```

**Key invariant:** Tier 1 reacts at frame rate. Tier 2 reacts in seconds. Tier 3 reacts in tens of seconds. Each tier hides its slowness from the tier below by exposing a stable contract.

## Process / deployment

- **One Lua addon per concern**, all running in the same Ashita instance.
- **One Python orchestrator process** (extending the existing navserver into a generic `agent_core`). It watches a shared IPC dir, builds a unified world model, runs goal/gambit logic, and is the only thing that talks to the LLM.
- **One LLM client** invoked by the orchestrator over Anthropic's API.

**Decided:** one process, modular internally. The current navserver is renamed `agent_core/` and `nav.py` becomes one of its modules. New subsystems land as sibling modules.

## IPC pattern

All addons share a base directory (`<ashita>/config/addons/agent/`). Every per-character file is namespaced by the character's name so multi-character is a wiring change, not a rewrite:

```
state/<char>/
  nav.json            # current zone, position, autofollow state
  combat.json         # target, hp/mp, status effects, casting state
  inventory.json      # bag contents per container, gear sets active
  party.json          # party members + their hp/mp/status
  chat.json           # recent chat lines (rolling 200)
  menu.json           # active NPC dialog or menu (or null)
  entities.json       # known NPCs/mobs/objects in current zone (already exists)
events.jsonl          # append-only cross-addon event stream (character-tagged per line)
commands/<char>/
  nav.json            # current command for the nav addon
  combat.json         # current gambit set + farming directives
  interact.json       # pending menu/trade/door actions
  inventory.json      # gear set, inventory ops queue
  chat.json           # outgoing chat queue
persistent/<char>/
  goals.json          # persistent goal tree (orchestrator-owned)
  gambits.json        # persistent gambit definitions (orchestrator-owned)
  knowledge.json      # accumulated facts (mob respawn timers, vendor prices, etc.)
```

For single-character MVP, `<char>` resolves to a configured default name (read from a top-level `agent_core/config.toml`). For multi-character later, the orchestrator either runs one instance per character or holds parallel world models keyed by name — the file layout doesn't change.

Pattern:
- **Addons publish** their slice of state on every relevant change. Atomic write (temp file + rename) so readers never see partial JSON.
- **Orchestrator polls / inotifies** the state dir at 5 Hz, builds a unified world model.
- **Orchestrator writes** to `commands/<addon>.json` when it wants the addon to change behaviour. Each command file contains a sequence number; addons ignore stale commands.
- **Both sides append** game-significant events to `events.jsonl` (level up, death, item received, dialog entered, etc.). This is the LLM trigger surface.

This extends the nav addon's existing pattern (`nav_request.json` / `nav_path.json`) — same shape, more channels.

## Addon catalog

### A. nav — built (mostly)
Current state: goto/preview/find/dropoffs/objects/record. Routes paths through the Recast navmesh.

**Future enhancements scoped here:**
1. **Entity Z coords.** Today entities.lua tracks XY; add Z so combat can verify line-of-sight and respect 3D distance for "closest mob" queries.
2. **Entity query interface.** A `commands/nav.json` action like `{action: "find_entities", filter: {name: "Huge Hornet", aggressive: false, max_dist: 200}}` returning a list. The orchestrator uses this to pick farming targets.
3. **Avoid-aggro routing.** Optional path-cost penalty inside aggro radius of known hostile mobs. Implementation: feed a list of `{x, y, radius}` into the navserver's existing obstacle system, regenerate path. Toggle per-request (`avoid_aggro: true`).
4. **Respawn-aware target picking.** This is *orchestrator* logic, not nav — nav just exposes "where is this entity" / "when was it last seen." Knowledge stored in `knowledge.json`.

Entity tracking stays inside the nav addon for now (already produces the data); revisit if other addons need it standalone.

### B. interact — new
Wraps NPC/object interaction. Uses `enternity` for auto-progress; adds the parts `enternity` doesn't do.

**Capabilities:**
- **Default action on object.** "Use closest interactable" (door, ??? in maps, conquest tally, etc.).
- **NPC menu introspection.** When a dialog menu opens, capture the prompt text + option strings + current cursor index, write to `state/menu.json`. The LLM (or a cached rule) picks the option; we set the cursor and confirm.
- **Cutscene option choice.** Same mechanism as menus — read the option list, pick one.
- **NPC trade.** Programmatically open trade window with a target NPC, drop items in, hit trade.
- **Player trade.** Same UI but with a player character.
- **Vendor.** Detect vendor windows, expose buy/sell as commands. The LLM can issue `{action: "buy", item: "Bronze Knife", qty: 1}` or `{action: "sell", item_id: 4096, qty: stack}`.

**Critical packets to hook:** menu open/close, dialog text, trade window state, vendor item list. These probably already have known offsets in the LSB / Ashita community; we'll lift definitions rather than reverse-engineer.

**Decided:** one addon. Dialog, trade, and vendor share too much packet plumbing to split.

### C. combat — new (highest complexity)
FF12-style gambit engine + farming director.

**Gambit model.** A gambit is `(priority, trigger_predicate, action)`. Each tick (~5 Hz from Lua), evaluate gambits in priority order; the first matching one whose action is currently legal fires. Action = a sequence of `/ja`, `/ma`, `/ws`, `/follow`, `/attack`, etc. A small DSL covers triggers:

```
self.hp_pct < 50
target.tp >= 1000 and ja('Sneak Attack').ready
party_member.lowest_hp_pct < 30
self.mp_pct < 25 and not engaged and not in_aggro_range
target.casting('Firaga III')
status_effect_on(self, 'Silence')
```

Compile each predicate to a Lua closure once; cache. Re-compile only when the gambit list changes.

**Decided:** Python parses, Lua evaluates. The LLM produces plain text triggers/actions. Python's parser turns them into a typed JSON AST (rejecting unknown identifiers, malformed expressions, references to abilities the agent doesn't have at its current level/job) before deploy. Lua walks the AST against live state at 5 Hz with no parse cost.

**Farming director.** Lives in the orchestrator (Tier 2), not the combat addon. Given a directive like:

```json
{
  "type": "farm",
  "stop_when": {"any_of": [{"item_received": "Orcish Axe"}, {"level_reached": 18}]},
  "targets": [{"name": "Orcish Fodder"}, {"name": "Orcish Grappler"}],
  "rest_policy": {"hp_pct_threshold": 70, "mp_pct_threshold": 50}
}
```

The director:
1. Queries nav for closest live target matching `targets`.
2. If none alive nearby (per knowledge of respawn timers), waits or relocates.
3. Sends nav goto + combat engage commands.
4. Watches state (HP/MP/death/loot) until the engagement ends.
5. Decides to rest, reposition, or re-engage based on `rest_policy` + LLM-tunable defaults.
6. Watches `stop_when` for completion.

**Respawn tracking.** Each kill writes `{entity_id, name, last_killed_at, location}` to `knowledge.json`. Director estimates per-name respawn period (start with 5 min default; refine from data). Don't path to a target whose ETA-to-arrival exceeds its expected respawn time.

**Combat log.** Append every significant combat event (damage, status, ability use, exp gained, drops, deaths) to `events.jsonl`. The orchestrator periodically (e.g. every 2 minutes during active combat) hands a summarised slice to the LLM with the prompt "evaluate gambit performance, suggest changes."

### D. inventory — thin wrapper
We do not write a packer or luashitacast competitor.

**Configuration responsibility:**
- Generate `luashitacast` profile files based on the agent's level/job/owned gear (templates from yzyii/luashitacast).
- Generate `packer` config files for routine inventory shuffles (e.g. "stash crystals to mog satchel when inventory > 80% full").
- Update both when gear/job changes.

**State reporting:**
- Read every container (inventory, satchel, sack, case, wardrobe ×4, mog locker if accessible) and publish as `state/inventory.json`. The combat director uses this to detect drops; the LLM uses it to decide gear shopping.

**Action surface (commands/inventory.json):**
- `equip_set <name>`        → forwards to luashitacast
- `move_item <id> <from> <to>` → forwards to packer
- `discard_item <id>`       → with safety guard
- `usable_check <ability>`  → "do I have the gear/level for this?"

**Decided:** diff polling for `state/<char>/inventory.json`. Add packet hooks for goal types where event-timestamp accuracy matters (e.g. distinguishing "drop from this kill" vs "drop from a previous kill that we just noticed").

### E. chat — new
Message in / message out + LLM intercept.

**Inbound:**
- Hook every chat packet (party, tell, linkshell, say, shout, yell).
- Append to `state/<char>/chat.json` (rolling buffer of last 200 lines).
- Append a `chat_received` event to `events.jsonl` with sender / channel / text / direct-mention flag.
- **All channels are LLM-routed** (private server, small population). LLM receives recent chat context + current goal; returns either `{action: "send", channel, text}` or `{action: "ignore"}`. Optionally `{action: "modify_goal", new_goal: ...}` to redirect the agent.
- **Rate limiting** is enforced in the orchestrator (not the LLM): per-channel cooldowns prevent the agent from replying to every shout (e.g. yell: max one outbound every 5 minutes; shout: every 2 minutes; party: no limit). The LLM sees pending-cooldown status in its context so it can choose to wait or skip.
- A small in-Python rule layer handles routine traffic without LLM calls (loot rolls, ready checks, follow requests from party leader). Each rule's match short-circuits the LLM dispatch.

**Outbound:** write to `commands/<char>/chat.json`, addon executes `/p`, `/t`, etc. No human-in-the-loop confirmation — autonomous mode.

### F. agent_core — Python orchestrator (new, but extends navserver)

The brain of Tier 2. Modules:

1. **State aggregator.** Polls `state/*.json`, builds an in-memory world model. Diffs against previous snapshot to fire change events.
2. **Event router.** Reads `events.jsonl`, classifies events into:
   - **Reactive** (handled in Tier 2 immediately — e.g., low-HP triggers a cure gambit).
   - **Heuristic-rule** (Tier 2 + deterministic rules — e.g., loot decisions, polite tell auto-replies).
   - **LLM-required** (escalate to Tier 3 — e.g., new menu prompt, goal completion, recurring gambit failure).
3. **Goal manager.** Owns the goal tree, picks the active leaf, writes derived directives to `commands/*.json`.
4. **Gambit manager.** Owns the gambit list per goal-context, deploys to combat addon, tracks performance for LLM tuning.
5. **Knowledge base.** Persistent facts (vendor prices, mob respawn estimates, quest prerequisites we've learned). Both queried by the orchestrator and surfaced to the LLM.
6. **LLM gateway.** Tool-use API to Anthropic. Builds compact prompts from world model + recent events; routes tool calls back into the orchestrator.

The current `navserver/server.py` becomes the `nav` module of `agent_core`. The HTTP/JSON pattern stays.

## Goal model

```json
{
  "id": "g_unlock_subjob",
  "title": "Unlock subjob",
  "origin": "user",
  "state": "active",
  "subgoals": ["g_reach_18", "g_subjob_quest"],
  "depends_on": [],
  "completion": {
    "type": "key_item",
    "key_item_id": 511
  },
  "directives": null
}
```

Goal states: `pending → active → completed | failed | abandoned`. Auto-generated subgoals are mutable; user-set goals are immutable (orchestrator can mark them failed but not delete them).

The orchestrator's planner walks the tree, finds the first ready leaf (no incomplete deps), instantiates its `directives` (nav target, gambit set, etc.), and writes them to `commands/*.json`. When a subgoal completes, the parent re-evaluates.

The LLM is invoked when:
- A goal completes (re-plan parent / pick next).
- A goal fails (re-plan or abandon).
- A user adds/removes a top-level goal.
- Periodically to review whether the plan still makes sense (every 10 min during execution).

## LLM integration

**Tool API.** The LLM only acts via structured tools:

| Tool | Purpose |
|---|---|
| `read_world_state` | Compact snapshot (zone, pos, hp/mp, target, party, current goal, recent events) |
| `read_inventory` | Bag contents + equipped + gear sets |
| `read_chat_log` | Recent chat lines |
| `update_goals` | Replace a subtree of goals |
| `update_gambits` | Replace gambit list for a context |
| `send_chat` | Channel + text |
| `pick_menu_option` | Index or option text |
| `set_directive` | Override the current directive (manual nudge) |
| `query_knowledge` | Vendor prices, mob locations, quest info |
| `update_knowledge` | Persist a fact |

**Cadence.**
- Reactive (haiku-class, <2s): chat replies, menu choices, urgent dilemmas.
- Periodic (sonnet-class, every few minutes when active): combat-log review, plan check-in.
- Deliberative (opus-class on big events): top-level goal change, level-up replanning, post-death analysis.

**Default model per tier** (configurable via `agent_core/config.toml`):
- Reactive: `claude-haiku-4-5`
- Periodic: `claude-sonnet-4-6`
- Deliberative: `claude-opus-4-7`

**Context budget.** The world-state snapshot must fit comfortably in <2k tokens for reactive calls and <10k tokens for deliberative calls. Lean on the knowledge base instead of replaying full chat logs.

## Persistence and crash recovery

Persistent (survives crashes):
- `goals.json` — goal tree
- `gambits.json` — gambit library
- `knowledge.json` — learned facts
- `entities/<zone>.json` — entity sightings (already persistent)
- `events.jsonl` — event log (rolled at 100k lines)

Ephemeral (rebuilt from current game state):
- `state/*.json` — addon-published live state
- `commands/*.json` — orchestrator-issued directives

On orchestrator restart: load persistent files, wait for first round of `state/*.json` from addons, resume executing the active goal.

On addon reload: addon re-publishes its state from current game observation. Orchestrator re-deploys current `commands/*.json` for that addon.

## Observability

Two complementary surfaces.

**Live web dashboard** (served by `agent_core` over HTTP, default port 7777):
- Live world model snapshot (zone, position, hp/mp, target, party members).
- Goal tree, with state per goal and current active leaf highlighted.
- Active gambit list with last-fired timestamp and hit count per gambit.
- Recent LLM calls — model, tier, prompt token count, response token count, latency, cost. Running session-cost meter.
- Raw events stream (filterable by character / channel / event type).
- Last navmesh path overlay (debug nav fragmentation).
- Manual override controls — pause the agent, force a goal-tree refresh, send a single chat message.

**In-game ImGui overlay** (rendered by a debug addon — likely an extension of nav, or its own `debug` addon):
- Compact HUD pinned to a screen corner: current goal title, active gambit, current target name + HP%, agent's HP/MP, last LLM-issued action and its age.
- Keybind to expand into a fuller view of the goal tree without alt-tabbing to the dashboard.

Both surfaces read from the same `state/<char>/*.json` and `events.jsonl` files the orchestrator already maintains; no new persistence layer.

## Phased roadmap (revised after MVP-ordering decision)

Each phase will get its own implementation plan once we lock the architecture.

**Phase 0 — Architecture sign-off (this document).**

**Phase 1 — `agent_core` skeleton.** Refactor `navserver/` into `agent_core/` with the new IPC layout (`state/<char>/`, `commands/<char>/`, `events.jsonl`, persistent files). State aggregator + character-namespaced channel registry + LLM gateway stub (real Anthropic API but only stub tools). nav still works exactly as today.

**Phase 2 — Goal manager + LLM planner.** Persistent goal tree, planner loop, tool surface (`read_world_state`, `update_goals`, `set_directive`, `query_knowledge`). Demo: hand the agent "travel Sandy → Selbina → Mhaura → Kazham"; agent decomposes into per-zone gotos and executes via the existing nav pipeline. No new addons — we exercise the planning brain on nav alone.

**Phase 3 — Combat addon + gambit engine + farming director.** Demo: "farm Bumblebees outside Sandy until level 10."

**Phase 4 — Interact addon + NPC menu navigation.** Demo: "talk to gate guard, accept signet."

**Phase 5 — Chat addon.** Demo: agent responds to /tells reasonably; party-chat directed at agent gets handled.

**Phase 6 — Inventory addon (luashitacast/packer config writer).** Demo: agent equips appropriate gear when leveling/changing job.

**Phase 7 — Observability dashboard.**

After Phase 4 we have the full "unlock a subjob" loop end-to-end.

## What this plan does NOT cover

- Specific Ashita API offsets and packet structures (research per-phase).
- LLM prompt engineering details (per-phase, with measurement).
- Multi-character coordination.
- Quest-data ingestion (quest prerequisites, item locations) — initially the LLM relies on its training; we add a knowledge file for things it consistently gets wrong.
- Combat outside of solo play (party leadership, alliance positioning).
- Botting safety / detection avoidance — out of scope; this is for private servers / LSB.

## Decision log

- **Server target**: Private (LSB). We can introspect packets and memory freely.
- **Process boundary**: Single orchestrator (extend navserver). Modular internally; split later only if a module grows too big.
- **Multi-character**: Single character for MVP, but **namespace all state/command/persistent files by character name** so multi-character becomes a wiring change, not a rewrite. Conventions:
  - State paths: `state/<character>/<channel>.json` (e.g. `state/Mybird/nav.json`)
  - Persistent: `goals/<character>.json`, `gambits/<character>.json`, `knowledge/<character>.json`
  - Single-character mode picks a default character name from config; multi-character mode runs one orchestrator per character (or one orchestrator with per-character world models — deferred decision).
- **MVP scope ordering** (revised): **agent_core skeleton + goal manager + LLM planner first**, before combat/interact. Rationale per user: prove the planning brain works on top of nav alone (e.g., LLM-driven multi-step navigation goals like "travel from Sandy to Selbina, then Mhaura, then Kazham") before wiring in subsystems whose results need to feed back to the planner. Combat and interact slot in once the planner has somewhere to send their output.
- **Gambit DSL home**: AST in Python, evaluator in Lua. Python parses LLM-generated gambit text into a typed JSON AST (catching errors before deploy); the combat addon walks the AST at 5 Hz against live state.
- **Source-tree layout**: Rename `navserver/` → `agent_core/`. nav becomes `agent_core/nav.py`; new subsystems get sibling modules. Existing `recast_wrapper/` stays put as a build artifact.
- **Safety / HITL**: **Fully autonomous, no human-in-the-loop.** No confirmation gates on tells, trades, party invites, or any other inter-player action. The LLM is the sole arbiter. (Implication: the addons must never block waiting for a confirmation that won't come — every action is fire-and-forget once the LLM/orchestrator decides.)
- **State observation for goal completion**: Poll-and-diff via published `state/*.json` for everything in memory; add targeted packet hooks per goal type for events that aren't (key items, exp gains, item-receipt timestamps). Catalogue packet IDs incrementally in the codebase as we encounter each goal type.
- **Chat aggressiveness**: **All channels.** Private server with a small population — every channel (tell, party, linkshell, say, shout, yell) is LLM-routed for inbound and is fair game for outbound. Implication: rate-limit outbound responses (don't reply to every shout) and add a per-channel cooldown so the agent doesn't spam.
- **Observability**: **Both a live web dashboard and an in-game ImGui overlay.**
  - **Web dashboard** (served by agent_core): full world state, goal tree, gambit list with hit-counts, LLM call log + cost totals, raw events stream, navmesh snapshot. Use this for offline iteration and post-mortem.
  - **ImGui overlay** (rendered by a debug addon, or inside `nav` for now): compact at-a-glance HUD — current goal title, active gambit, target, hp/mp, last LLM action. Use this while playing.
