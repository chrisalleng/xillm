# FFXI Collision-First Navigation Rewrite Plan

## Objective

Replace the addon's existing LandSandBoat navmesh-based navigation stack with a collision-first navigation system while **keeping the current in-game character movement execution layer** intact.

The current addon already has working logic for how to physically move the character in-game. That movement implementation should remain in place unless a small interface adjustment is needed to connect it to the new planner.

What should be removed and replaced:

- all dependency on LandSandBoat navmeshes for area and zone navigation
- all navmesh-driven route planning
- all assumptions that the world is pre-modeled as navmesh data

What should be kept:

- the existing strategy and implementation for issuing movement to the character in-game
- any stable movement timing, heading, keypress, or action execution code that already works well

What should be added:

- a preprocessing pipeline to extract collision geometry for **all zones** from the FFXI DATs
- repository-stored extracted collision data
- a new collision-driven per-zone exploration and planning system
- persistent learned graph data generated from collision and traversal

---

## Key Design Decision

This project is an **Ashita Lua addon**.

The addon runtime should stay Lua-based and Ashita-native.

However, **collision extraction from DATs should be handled in Step 0 by an external preprocessing tool**, and the extracted results should be checked into the repository so the addon can consume them directly.

This is strongly preferred over trying to parse and preprocess large amounts of DAT collision data inside the Lua addon at runtime.

### Why

- Lua runtime cost should stay low
- zone load times should stay predictable
- geometry processing is easier to debug offline
- extracted collision data can be versioned in the repository
- addon logic can focus on navigation, exploration, planning, and movement integration

---

## Repository Structure

```text
addons/nav/
  nav.lua
  addon.json
  CLAUDE.md

  config/
    defaults.lua

  libs/
    math3d.lua
    aabb.lua
    priority_queue.lua
    serializer.lua
    profiler.lua

  core/
    state.lua
    commands.lua
    settings.lua
    debug.lua
    zone_manager.lua
    persistence.lua
    integration.lua

  geometry/
    geometry_provider.lua
    geometry_cache.lua
    spatial_index.lua
    raycast.lua
    walkability.lua

  graph/
    zone_graph.lua
    graph_builder.lua
    graph_query.lua
    frontier.lua

  planning/
    astar.lua
    planner.lua
    path_smoother.lua

  movement/
    movement_adapter.lua
    movement_bridge.lua

  exploration/
    sampler.lua
    explorer.lua
    confidence.lua

  tools/
    extract_collision/
      README.md
      extract_collision.py
      formats.md

  data/
    collision/
      <zone_id>.json
    zones/
      <zone_id>.graph.json
```

Notes:

- `movement/` in the new design is intentionally small because the existing movement execution should be reused, not rewritten.
- `movement_adapter.lua` and `movement_bridge.lua` should adapt the new planner output into the current movement system.
- `data/collision/` is repository-owned extracted geometry generated in Step 0.
- `data/zones/` is runtime-generated learned graph state.

---

## Scope Change from Previous Plan

The previous architecture assumed building a full movement stack. That is no longer the goal.

The revised scope is:

1. **Preserve the current movement execution layer**
2. Remove old navmesh/world-routing logic
3. Replace it with collision-driven planning and discovered graph logic
4. Add an offline extraction step that exports collision data for all zones into repository data files

The movement layer should be treated as an existing dependency with a clean adapter boundary.

---

## Step 0: Extract Collision Data for All Zones

## Goal

Create an offline extraction pipeline that reads the game's DAT-based collision data for all zones and exports normalized per-zone collision files into the repository.

This step happens before the addon rewrite is considered complete.

## Requirements

- extract collision geometry for every supported zone
- include submodels where relevant
- normalize coordinates into a consistent world-space format
- store output in a repository-owned format the Lua addon can load directly
- make the extraction repeatable
- document how to rerun extraction when source DATs change or extraction logic improves

## Output

Recommended output format for first implementation:

- one file per zone
- JSON for readability first, with ability to switch to binary later if needed

Suggested structure:

```json
{
  "zone_id": 0,
  "triangles": [
    {
      "v0": [0, 0, 0],
      "v1": [0, 0, 0],
      "v2": [0, 0, 0]
    }
  ],
  "bounds": {
    "min": [0, 0, 0],
    "max": [0, 0, 0]
  },
  "metadata": {
    "source": "...",
    "submodels": []
  }
}
```

## Step 0 Deliverables

- `tools/extract_collision/README.md`
- `tools/extract_collision/formats.md`
- extraction script or scripts
- generated `data/collision/<zone_id>.json` files for all target zones
- manifest file if useful
- validation notes for spot-checking geometry alignment

## Step 0 Acceptance Criteria

- all intended zones have exported collision files
- output files load consistently from Lua
- spot-checks confirm collision aligns plausibly with in-game world coordinates
- extraction process is documented and repeatable

## Step 0 Implementation Notes

- Prefer extracting from an existing known-good collision source/tooling path rather than reinventing DAT parsing if possible.
- The addon must not depend on external extraction at runtime.
- The repository should contain the extracted results needed by the addon.

---

## Runtime Architecture

The addon runtime should consume extracted collision data and build navigation behavior around it.

### Collision is ground truth
Use extracted collision for:

- floor detection
- wall detection
- line-of-movement validation
- local obstacle awareness
- path smoothing validation

### Learned graph is runtime traversal knowledge
The addon should not assume a complete world graph from the start.

Instead, per zone it should:

- begin with empty or minimal traversal knowledge
- add nodes as the player explores
- add edges when local traversal is validated or actually traveled
- persist learned graph data for reuse

### Existing movement stays in place
The planner should output waypoints or local goals. The current movement logic should continue to handle:

- how to rotate / face
- how to issue movement
- how to keep the player moving
- timing and behavioral details that already work reliably

---

## Major Modules

## 1. `core/state.lua`
Central runtime state.

Tracks:

- current zone
- loaded collision data
- spatial index
- discovered graph
- current goal
- active planner result
- movement integration state
- debug flags

## 2. `core/zone_manager.lua`
Responsible for:

- zone detection
- loading collision data for current zone
- loading zone graph
- saving dirty graph on zone exit
- resetting planner/explorer state when zone changes

## 3. `geometry/geometry_provider.lua`
Loads repository-stored extracted collision files.

Responsibilities:

- load `data/collision/<zone_id>.json`
- validate file contents
- expose normalized geometry tables to runtime

This module should not parse DATs directly.

## 4. `geometry/spatial_index.lua`
Build a fast query structure over collision triangles.

Recommended first version:

- coarse 2D X/Z grid
- per-cell triangle membership

Responsibilities:

- nearby triangle lookup
- support raycasts and segment tests efficiently

## 5. `geometry/raycast.lua`
Pure collision query helpers.

Responsibilities:

- downward floor raycast
- segment-vs-triangle blocking tests
- nearest collision hit queries

## 6. `geometry/walkability.lua`
Classify triangles into likely walkable surfaces.

Responsibilities:

- slope checks
- area threshold checks
- walkable flag assignment

## 7. `graph/zone_graph.lua`
Persistent discovered graph per zone.

Responsibilities:

- node storage
- edge storage
- node dedupe
- edge confidence
- frontier marking

## 8. `graph/frontier.lua`
Select frontier nodes when destination is not fully known.

Responsibilities:

- identify frontiers
- score frontiers by goal direction and estimated usefulness

## 9. `planning/astar.lua`
Pure graph search over discovered nodes.

## 10. `planning/planner.lua`
High-level planning policy.

Responsibilities:

- if a known path exists, use it
- otherwise route toward the best frontier
- periodically replan as graph expands
- smooth path only when collision validation passes

## 11. `movement/movement_adapter.lua`
Adapter from planner output to the existing movement implementation.

Responsibilities:

- convert path nodes / waypoints into the format expected by current movement code
- keep movement layer decoupled from graph logic

## 12. `movement/movement_bridge.lua`
Thin integration layer that calls the existing movement system.

Responsibilities:

- send current waypoint to existing movement code
- observe movement success/failure state if available
- report movement progress back to planner/explorer

This is the boundary that preserves the current movement implementation.

## 13. `exploration/sampler.lua`
Generate candidate nearby sample positions.

Responsibilities:

- radial sampling
- directional bias toward current goal
- candidate filtering

## 14. `exploration/explorer.lua`
Grow the discovered graph around the player.

Responsibilities:

- sample around the player/current route
- validate candidate nodes using collision
- add graph nodes and edges
- mark frontiers

## 15. `exploration/confidence.lua`
Track trust in edges and routes.

Responsibilities:

- increase confidence after successful traversal
- reduce confidence after repeated failure
- temporarily suppress bad edges

## 16. `core/persistence.lua`
Load/save discovered graph state.

Responsibilities:

- load `data/zones/<zone_id>.graph.json`
- save dirty zone graph periodically

## 17. `core/debug.lua`
Essential visualization.

Should support toggles for:

- collision display
- walkable surface display
- graph nodes
- graph edges
- frontier nodes
- active path
- current target waypoint

---

## Removal / Replacement Plan

The addon already contains rough navigation between areas and zones using LandSandBoat navmeshes.

That logic should be removed in a controlled way.

## Remove or replace

- navmesh loading
- navmesh path lookup
- area-to-area navmesh route logic
- zone-to-zone navmesh route logic
- navmesh-specific assumptions in planner state

## Keep

- character movement execution
- whatever currently translates route intentions into real movement
- any successful stuck handling that is part of the movement layer rather than the navmesh planner

## Introduce compatibility boundary

Before deleting too much code, create a clean interface boundary:

- old planner produces movement goals
- new planner will produce movement goals through the same or similar interface

This makes the swap safer.

---

## Phase Plan

## Phase 0: Collision extraction pipeline

Build the offline extraction system and add extracted data to the repository.

Deliverables:

- extraction tooling
- documented format
- repository collision dataset

Acceptance:

- collision data exists for all target zones
- Lua runtime can load collision files

---

## Phase 1: Addon scaffold audit and integration boundary

Goal:

Understand the current addon structure and isolate the existing movement system from old navmesh planning.

Tasks:

- identify all navmesh-dependent modules
- identify movement execution modules that should remain
- define a planner-to-movement boundary
- document current flow

Deliverables:

- architecture notes
- thin movement adapter/bridge interfaces

Acceptance:

- it is clear what code will be deleted, what code will remain, and what interface will connect the new planner to the old movement logic

---

## Phase 2: Remove navmesh-driven planning dependencies

Goal:

Begin retiring LandSandBoat navmesh world knowledge while preserving movement execution.

Tasks:

- disable navmesh planner entry points
- isolate navmesh-dependent route generation behind stubs or adapters
- keep addon stable while no-op or temporary local planning is used

Acceptance:

- addon can run without hard dependency on old navmesh planning path generation
- movement layer still loads and functions when fed test waypoints

---

## Phase 3: Collision loading and spatial query layer

Goal:

Load extracted collision data and support fast spatial queries.

Tasks:

- implement geometry provider
- implement spatial index
- implement raycasts and segment tests

Acceptance:

- current zone collision loads correctly
- debug overlay can visualize geometry
- floor and blocking checks work on extracted collision

---

## Phase 4: Walkability classification

Goal:

Mark likely walkable surfaces from extracted collision.

Tasks:

- compute normals
- apply slope threshold
- apply area threshold
- annotate walkable triangles

Acceptance:

- overlay differentiates walkable vs non-walkable surfaces plausibly

---

## Phase 5: Planner-to-movement integration using existing movement code

Goal:

Feed simple collision-validated local goals into the current movement system.

Tasks:

- build `movement_adapter.lua`
- build `movement_bridge.lua`
- send nearby goal points into existing movement logic
- observe completion/progress/failure if available

Acceptance:

- existing movement implementation can move toward planner-selected local goals without needing navmesh routing

---

## Phase 6: Per-zone discovered graph

Goal:

Introduce persistent traversal memory per zone.

Tasks:

- implement graph storage
- node dedupe
- edge storage
- graph persistence

Acceptance:

- walking around creates reusable zone graph data
- graph reloads on revisit

---

## Phase 7: Exploration growth from collision

Goal:

Expand graph based on nearby collision-derived reachability.

Tasks:

- sample nearby candidate points
- snap to floor
- validate candidate paths using collision
- add nodes and edges
- mark frontiers

Acceptance:

- graph expands around traversed space
- frontiers represent unexplored reachable boundaries

---

## Phase 8: Graph pathfinding and frontier planning

Goal:

Route through known graph or toward best frontier when destination is not fully known.

Tasks:

- implement A*
- implement frontier selection
- implement periodic replanning
- implement path smoothing with collision validation

Acceptance:

- known areas can be routed directly
- unknown goals can be approached via exploration

---

## Phase 9: Confidence and recovery

Goal:

Improve reliability over repeated travel.

Tasks:

- edge confidence scoring
- failure tracking
- temporary suppression of bad edges
- retry and alternate frontier logic

Acceptance:

- graph quality improves over time
- repeated failures do not loop endlessly

---

## Phase 10: Inter-zone learned routing

Goal:

Replace old zone-to-zone navmesh strategy with learned zone exit knowledge.

Tasks:

- detect zone transitions
- record zone exits/entries
- build simple inter-zone graph
- use learned exits for long-distance planning

Acceptance:

- addon can eventually route between zones using learned exit knowledge rather than navmesh routing

---

## Data Flow

## On zone load

1. zone manager detects zone change
2. collision file for zone is loaded from `data/collision/`
3. spatial index is built or loaded
4. zone graph is loaded from `data/zones/`
5. planner and explorer reset for new zone

## On update tick

1. read player position
2. update exploration around player within budget
3. if a goal exists, planner chooses next waypoint/frontier
4. movement adapter feeds waypoint to existing movement implementation
5. movement results update graph confidence and planning state
6. save dirty graph periodically

---

## Constraints for Claude Code

- This is an Ashita Lua addon, not a C++ plugin.
- Reuse existing movement execution rather than rewriting it.
- Remove LandSandBoat navmesh planning dependencies completely.
- Use extracted collision files stored in the repository.
- Keep geometry loading abstracted behind a provider.
- Prefer incremental work budgets per frame.
- Add strong debug visualization and inspection commands.

---

## Suggested First Tasks for Claude Code

1. audit current addon structure
2. identify movement code to preserve
3. identify navmesh-specific code to remove
4. create planner-to-movement adapter boundary
5. scaffold Step 0 extraction tool directory and format docs
6. scaffold Lua modules for geometry provider, spatial index, and planner
7. wire current addon to run without old planner assumptions

---

## Claude Code Prompt

```text
Rewrite this Ashita Lua addon to remove its current LandSandBoat navmesh-based navigation stack and replace it with a collision-first navigation system.

Important constraints:
- Keep the existing in-game character movement execution logic. It already works well and should be reused.
- Remove old navmesh-based area and zone navigation logic.
- Add a Step 0 preprocessing pipeline that extracts collision data for all zones from the FFXI DATs and stores that extracted data in the repository under data/collision/.
- The Lua addon runtime should load extracted collision data from repository files; it should not parse DATs at runtime.
- Build a per-zone discovered graph from collision and traversal over time.
- Use collision for floor checks, segment validation, local obstacle checks, and path smoothing validation.
- Use the discovered graph for A* pathfinding.
- When destination knowledge is incomplete, choose frontier nodes and continue exploration.
- Persist discovered per-zone graph data separately from extracted collision data.

Use a Lua module structure like:
- nav.lua
- core/state.lua
- core/zone_manager.lua
- core/commands.lua
- core/debug.lua
- core/persistence.lua
- geometry/geometry_provider.lua
- geometry/spatial_index.lua
- geometry/raycast.lua
- geometry/walkability.lua
- graph/zone_graph.lua
- graph/frontier.lua
- planning/astar.lua
- planning/planner.lua
- movement/movement_adapter.lua
- movement/movement_bridge.lua
- exploration/sampler.lua
- exploration/explorer.lua
- exploration/confidence.lua
- tools/extract_collision/

Implementation order:
1. audit existing addon and document what movement code is preserved vs what navmesh code is removed
2. create planner-to-movement adapter boundary
3. scaffold Step 0 extraction pipeline and output format docs
4. build geometry provider to load extracted collision files
5. build spatial query and raycast modules
6. build walkability classification
7. connect planner-selected local goals to the existing movement code
8. add per-zone graph persistence
9. add exploration-based graph growth
10. add A* over discovered graph
11. add frontier-driven planning
12. add confidence and recovery
13. later add learned inter-zone routing

Begin by auditing the current codebase and scaffolding the new module structure without rewriting the preserved movement layer.
```

