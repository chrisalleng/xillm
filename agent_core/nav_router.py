"""Cross-zone routing with safety weighting.

Loads three catalogs:
  - nav/data/zone_transitions.json (zone_id -> [{to, x, y, z}, ...])
  - nav/data/zone_meta.json        (zone_id -> {name, type_name, ...})
  - persistent/<char>/zone_safety.json (zone_id -> {verdict, reason})
    [optional - missing file means no per-character overrides yet]

Exposes:

  Router.shortest_route(from_zone, to_zone) -> RoutePlan | None
    Dijkstra over the zone graph with node weights driven by
    zone-type + character overrides. Returns the cheapest path
    (list of zone_ids) plus its total cost, or None if unreachable.

  Router.closest(from_zone, candidates) -> [RankedCandidate]
    Sort `candidates` (list of (npc_record_or_zone_id)) by
    shortest_route cost from `from_zone`. Stable secondary sort on
    zone_id for deterministic output.

  Router.zone_cost(zone_id) -> int
    The weight assigned to *entering* zone_id under the current
    character's safety overrides. Useful for the planner to explain
    its routing choice in echo lines.

Default zone-type weights (no character data):
  CITY        0      Always free; resting + safe.
  OUTDOORS    1      Normal travel; mob aggro is level-based, so the
                     planner relies on per-character overrides to
                     mark specific outdoor zones safe-at-level.
  SIGNET      1      Outpost outdoor zones; same as OUTDOORS.
  DUNGEON     100    Avoid by default; only enter when destination
                     is in a dungeon. The 100x multiplier lets
                     Dijkstra prefer a longer outdoor route over a
                     shorter dungeon shortcut.
  DYNAMIS     INF    Event-only; cannot be a transit zone.
  INSTANCED   INF    Event-only.
  UNKNOWN     5      Conservative middle ground - if LSB doesn't
                     classify it, neither do we.

Per-character `zone_safety.json` schema:
  {
      "overrides": {
          "<zone_id>": {
              "verdict": "safe" | "dangerous" | "default",
              "reason":  "WAR75 - mobs cap at 30, no aggro",
              "ts":      1777920000
          },
          ...
      }
  }

  - "safe":      cost = 1 (treat like an outdoors zone the agent has
                 cleared, regardless of its base type)
  - "dangerous": cost = max(100, base) (treat like a dungeon)
  - "default":   ignore the override and use the type-based base
                 (useful for un-marking a zone)
"""
from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# Cost the router uses when nothing else applies. INF marks "do not
# traverse"; downstream Dijkstra treats it as an unreachable edge.
COST_INF = math.inf

# Map zone-type names from zone_meta.json to base weights.
ZONE_TYPE_BASE_COST: dict[str, float] = {
    'CITY':      0,
    'OUTDOORS':  1,
    'SIGNET':    1,    # outposts; treat as outdoors for routing
    'DUNGEON':   100,
    'DYNAMIS':   COST_INF,
    'INSTANCED': COST_INF,
    'SANCTION':  COST_INF,  # TOAU instance areas
    'SIGIL':     COST_INF,  # WOTG instance areas
    'IONIS':     COST_INF,  # SOA instance areas
    'UNKNOWN':   5,
}

# Verdict-driven costs for explicit per-character overrides. These
# override the base type cost entirely.
SAFETY_OVERRIDE_COST: dict[str, float] = {
    'safe':      1,
    'dangerous': 100,
    # 'default' falls through to the type-based base
}


@dataclass
class RoutePlan:
    """One cross-zone path. `zones` includes both endpoints. `cost`
    is the sum of zone weights for entering each non-source zone -
    the source zone's weight is excluded since you're already there."""
    zones: list[int]
    cost:  float

    def __bool__(self) -> bool:
        return len(self.zones) > 0


@dataclass
class RankedCandidate:
    """A candidate ranked by route cost from the player's location."""
    candidate: Any                 # original record (NPC dict, zone_id, etc.)
    zone_id:   int
    cost:      float
    route:     list[int] = field(default_factory=list)


class Router:
    def __init__(self, transitions_path: Path,
                 zone_meta_path: Path,
                 safety_path: Path | None = None):
        """`safety_path` is the per-character zone_safety.json. Pass
        None when you want default routing only (e.g. for the planner's
        catalog-only queries before a character is selected)."""
        self.transitions_path = transitions_path
        self.zone_meta_path   = zone_meta_path
        self.safety_path      = safety_path

        # Lazy-loaded; reload() rebuilds them.
        self._adj:    dict[int, list[int]] = {}
        self._meta:   dict[int, dict[str, Any]] = {}
        self._safety: dict[int, dict[str, Any]] = {}

        self.reload()

    # ---- catalog loading ----------------------------------------------

    def reload(self) -> None:
        """Re-read all three input files. Cheap enough to call between
        every routing query if the planner edits zone_safety.json
        mid-session."""
        self._adj = self._load_adjacency(self.transitions_path)
        self._meta = self._load_zone_meta(self.zone_meta_path)
        self._safety = (
            self._load_safety(self.safety_path)
            if self.safety_path else {}
        )

    @staticmethod
    def _load_adjacency(path: Path) -> dict[int, list[int]]:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        adj: dict[int, list[int]] = {}
        for src_str, edges in (data.get('transitions') or {}).items():
            try:
                src = int(src_str)
            except ValueError:
                continue
            if not isinstance(edges, list):
                continue
            tos = []
            seen = set()
            for e in edges:
                to = e.get('to') if isinstance(e, dict) else None
                if isinstance(to, int) and to not in seen:
                    seen.add(to)
                    tos.append(to)
            adj[src] = tos
        return adj

    @staticmethod
    def _load_zone_meta(path: Path) -> dict[int, dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        meta: dict[int, dict[str, Any]] = {}
        for zid_str, row in (data.get('zones') or {}).items():
            try:
                zid = int(zid_str)
            except ValueError:
                continue
            meta[zid] = row
        return meta

    @staticmethod
    def _load_safety(path: Path | None) -> dict[int, dict[str, Any]]:
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        out: dict[int, dict[str, Any]] = {}
        for zid_str, row in (data.get('overrides') or {}).items():
            try:
                zid = int(zid_str)
            except ValueError:
                continue
            if isinstance(row, dict):
                out[zid] = row
        return out

    # ---- zone weighting -----------------------------------------------

    def zone_cost(self, zone_id: int) -> float:
        """Cost to ENTER zone_id. Per-character override beats type
        base; an unknown zone (no meta) is treated as UNKNOWN."""
        override = self._safety.get(zone_id, {}).get('verdict')
        if override in SAFETY_OVERRIDE_COST:
            return SAFETY_OVERRIDE_COST[override]
        type_name = (self._meta.get(zone_id) or {}).get('type_name', 'UNKNOWN')
        return ZONE_TYPE_BASE_COST.get(type_name, ZONE_TYPE_BASE_COST['UNKNOWN'])

    # ---- routing ------------------------------------------------------

    def shortest_route(self, from_zone: int, to_zone: int) -> RoutePlan | None:
        """Dijkstra with node weights. Cost of a path = sum of
        zone_cost(z) for each non-source zone on the path. Returns
        the minimum-cost path or None if no path exists.

        The destination's own cost IS included - this matters when
        comparing destinations (a dungeon-located NPC will be more
        expensive than an outdoors-located equivalent, which is the
        right tiebreaker for closest_npc)."""
        if from_zone == to_zone:
            return RoutePlan(zones=[from_zone], cost=0.0)
        if from_zone not in self._adj:
            return None

        # (cost_so_far, zone_id, path_list)
        heap: list[tuple[float, int, list[int]]] = [(0.0, from_zone, [from_zone])]
        best: dict[int, float] = {from_zone: 0.0}

        while heap:
            cost, zid, path = heapq.heappop(heap)
            if zid == to_zone:
                return RoutePlan(zones=path, cost=cost)
            if cost > best.get(zid, COST_INF):
                continue
            for nbr in self._adj.get(zid, ()):
                step = self.zone_cost(nbr)
                if step == COST_INF:
                    continue
                ncost = cost + step
                if ncost < best.get(nbr, COST_INF):
                    best[nbr] = ncost
                    heapq.heappush(heap, (ncost, nbr, path + [nbr]))
        return None

    def closest(self,
                from_zone: int,
                candidates: Iterable[Any],
                zone_of: Any = None) -> list[RankedCandidate]:
        """Rank candidates by shortest_route cost from `from_zone`.

        `candidates` can be:
          - a list of zone_ids (int)
          - a list of dicts with a 'zone_id' field (e.g. NPC records)
          - a list of arbitrary records when `zone_of(record) -> int`
            is provided

        Unreachable candidates are dropped from the output. Stable
        secondary sort by candidate's zone_id for determinism."""
        if zone_of is None:
            def zone_of(c: Any) -> int:
                if isinstance(c, int):
                    return c
                if isinstance(c, dict) and 'zone_id' in c:
                    return int(c['zone_id'])
                raise ValueError(
                    f'closest(): cannot extract zone_id from {type(c).__name__}; '
                    f'pass zone_of=lambda r: r.zone_id'
                )

        ranked: list[RankedCandidate] = []
        for c in candidates:
            zid = zone_of(c)
            route = self.shortest_route(from_zone, zid)
            if route is None:
                continue
            ranked.append(RankedCandidate(
                candidate=c, zone_id=zid, cost=route.cost, route=route.zones,
            ))
        ranked.sort(key=lambda rc: (rc.cost, rc.zone_id))
        return ranked


# ---------------------------------------------------------------------------
# NPC catalog facade
# ---------------------------------------------------------------------------

class NPCCatalog:
    """In-memory wrapper around nav/data/npcs.json. Provides find_npc
    queries that the planner exposes as a tool."""

    def __init__(self, npcs_path: Path):
        self.path = npcs_path
        self._npcs: list[dict[str, Any]] = []
        self._role_index: dict[str, list[int]] = {}
        self.reload()

    def reload(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self._npcs = []
            self._role_index = {}
            return
        self._npcs = data.get('npcs') or []
        self._role_index = data.get('role_index') or {}

    def by_id(self, npc_id: int) -> dict[str, Any] | None:
        for n in self._npcs:
            if n.get('id') == npc_id:
                return n
        return None

    def find(self, *,
             name_contains: str | None = None,
             role:          str | None = None,
             zone_id:       int | None = None,
             zone_name:     str | None = None,
             limit:         int | None = None) -> list[dict[str, Any]]:
        """Filtered query. All non-None filters are AND-ed.

        - name_contains: case-insensitive substring against both
          `name` (polutils form) and `lua_name`. Use '' to skip.
        - role: exact role tag (e.g. 'conquest_overseer'). When set,
          we use the precomputed role_index for fast lookup.
        - zone_id / zone_name: scope to one zone.
        - limit: cap result count. None = unlimited.
        """
        # Start from role_index when role is specified - O(matches)
        # instead of O(all_npcs).
        if role is not None:
            indices = self._role_index.get(role, [])
            pool = (self._npcs[i] for i in indices)
        else:
            pool = iter(self._npcs)

        nc = name_contains.lower() if name_contains else None
        out: list[dict[str, Any]] = []
        for n in pool:
            if zone_id is not None and n.get('zone_id') != zone_id:
                continue
            if zone_name is not None:
                zn = (n.get('zone_name') or '').lower()
                if zn != zone_name.lower():
                    continue
            if nc:
                hit = (
                    nc in (n.get('name') or '').lower() or
                    nc in (n.get('lua_name') or '').lower()
                )
                if not hit:
                    continue
            out.append(n)
            if limit is not None and len(out) >= limit:
                break
        return out

    @property
    def role_tags(self) -> list[str]:
        return sorted(self._role_index.keys())
