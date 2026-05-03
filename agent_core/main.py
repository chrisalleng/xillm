#!/usr/bin/env python3
"""
agent_core entrypoint - currently the navmesh / pathfinding service.

Phase 1 of the agent architecture (docs/agent-architecture.md): the
existing navserver code lives here unchanged in behaviour; the package
gains stub modules (config, state, events, persistence, llm_gateway)
that future phases will fill in. Existing nav IPC files
(`nav_request.json`, `nav_path.json`, `nav_status.json`) continue to
work as-is until Phase 1b migrates them to `state/<char>/nav.json`
and `commands/<char>/nav.json`.

Communicates with the Lua addon via JSON files:
  - nav_request.json: Lua writes goto requests
  - nav_path.json: server writes waypoint paths
"""

import json
import os
import sys
import time
import numpy as np
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / 'recast_wrapper' / 'build'))
import navmesh

sys.stdout.reconfigure(line_buffering=True)

COLLISION_DIR = SCRIPT_DIR.parent / 'nav' / 'data' / 'collision'
OBSTACLE_DIR = SCRIPT_DIR.parent / 'nav' / 'data' / 'obstacles'
DROPOFF_DIR = SCRIPT_DIR.parent / 'nav' / 'data' / 'dropoffs'
TRANSITIONS_FILE = SCRIPT_DIR.parent / 'nav' / 'data' / 'zone_transitions.json'
IPC_DIR = Path('/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/xillm')
REQUEST_FILE = IPC_DIR / 'nav_request.json'
PATH_FILE = IPC_DIR / 'nav_path.json'
POLL_INTERVAL = 0.1
OBSTACLE_BLOCK_RADIUS = 2.0


from collections import deque
import heapq


class NavServer:
    # Fallback agent radius used when a request sets `wider_radius=true`.
    # The default mesh is built with agent_radius=0.75 (the global default
    # in get_mesh) - when the addon's first attempt at a goto fails
    # because the path threads a too-narrow gap, it retries with this
    # much larger value to force a clearly-conservative route.
    FALLBACK_AGENT_RADIUS = 1.5

    def __init__(self):
        # meshes are keyed by (zone_id, radius) so agent_core can keep
        # both the default-radius mesh and the wider-radius fallback in
        # cache without rebuilding on every retry.
        self.meshes: dict[tuple[int, float], object] = {}
        self.obstacles: dict[int, list] = {}
        self.last_request_mtime = 0
        self.zone_names: dict[int, str] = {}
        self.name_to_zone: dict[str, int] = {}
        self.transitions: dict[int, list] = {}
        self.reachability: dict[int, dict] = {}
        self._load_transitions()
        # Phase 2 + 3c scaffolding: load config + goal manager +
        # farming director + LLM planner. The goal manager runs
        # alongside the request handler and dispatches its directives
        # by calling handle_request directly (no round-trip through
        # nav_request.json). The farming director handles long-running
        # `farm` leaves via the combat addon's state file. The planner
        # consumes free-text user goals from <repo>/user_goal.txt
        # (mtime-watched) and writes a decomposed tree (and optional
        # gambit list) to the goal manager's persistent file.
        from . import chat_handler as _chat_handler
        from . import config as _config
        from . import dashboard as _dashboard
        from . import engage_judge as _engage_judge
        from . import farming as _farming
        from . import interact as _interact
        from . import interact_director as _interact_director
        from . import menu_judge as _menu_judge
        from . import rest_judge as _rest_judge
        from . import gambits as _gambits
        from . import goal_manager as _gm
        from . import llm_gateway as _llm
        from . import persistence as _persistence
        from . import planner as _planner
        self.cfg = _config.load()
        self.llm = _llm.LLMGateway(self.cfg)
        # Persistent gambit library - keyed by (job/subjob/party) context.
        # Loaded once at startup; the planner mutates it via tool calls,
        # the context watcher resolves the right set for the current
        # job/party state and redeploys when context changes.
        self.gambits_store = _persistence.Gambits.load(_gambits.store_path(self.cfg))
        self._gambits_mod = _gambits  # cached reference for the watcher
        self._last_gambit_ctx: dict[str, str] | None = None
        # Dashboard runs on a daemon thread so it doesn't add to the
        # poll-loop critical path. Best-effort; if the port is in use
        # we just log and continue (server is read-only, not required).
        try:
            _dashboard.start(self.cfg)
        except OSError as e:
            print(f'  dashboard: not started: {e}')
        self.engage_judge = _engage_judge.EngageJudge(self.cfg, self.llm)
        self.rest_judge = _rest_judge.RestJudge(self.cfg, self.llm)
        # Interact driver: read state/<char>/menu.json, write
        # commands/<char>/interact.json. The orchestrator threads this
        # into the goal manager (interact_npc / vendor_* / home_point
        # dispatchers) and the death_recovery watcher. Stateless beyond
        # a seq counter so it's safe to share across consumers.
        self.interact = _interact.InteractDriver(self.cfg)
        # Reactive-tier LLM fallback for menu choices the script
        # can't resolve. Same rid-based fire/poll surface as
        # EngageJudge - the InteractDirector consumes via status/
        # discard.
        self.menu_judge = _menu_judge.MenuJudge(self.cfg, self.llm)
        # Interact director - state machine for interact_npc leaves.
        # Reads the user goal from disk so the menu_judge has full
        # context for fallback decisions; reuses the goal manager's
        # snapshot provider so we don't double-read nav state.
        self.interact_director = _interact_director.InteractDirector(
            cfg=self.cfg,
            interact=self.interact,
            menu_judge=self.menu_judge,
            player_snapshot_provider=self._read_player_snapshot,
            dispatch_goto=self.handle_request,
            issue_command=self._issue_command,
            user_goal_provider=self._read_user_goal,
        )
        self.farming = _farming.FarmingDirector(
            cfg=self.cfg,
            snapshot_provider=self._read_combat_snapshot,
            issue_command=self._issue_command,
            # Nav snapshot drives the locate/approach states. The goal
            # manager's snapshot has the same fields (zone_id/x/y/z/moving)
            # so we reuse it - duck-typed inside FarmingDirector via getattr.
            nav_snapshot_provider=self._read_player_snapshot,
            # Same dispatch_goto callable the goal manager uses, so the
            # approach state's request goes through handle_request and
            # rides the existing nav addon's path-following retry chain.
            dispatch_goto=self.handle_request,
            # Navmesh reachability check used by engage_nearby's wander
            # to validate exploration targets before dispatching the
            # goto. Stops the agent from dispatching partial-path
            # requests to cells that fall in walls / water / off-mesh.
            nearest_reachable=self.nearest_reachable,
            # LLM judge that decides engage/skip per-candidate from mob
            # info, player state, goal, and per-mob fight history.
            engage_judge=self.engage_judge,
            # LLM judge for post-kill rest/continue decisions. Same
            # context shape; consulted once per kill before moving on.
            rest_judge=self.rest_judge,
        )
        self.goal_manager = _gm.GoalManager(
            cfg=self.cfg,
            dispatch_goto=self.handle_request,
            snapshot_provider=self._read_player_snapshot,
            farming_director=self.farming,
            issue_command=self._issue_command,
            interact_director=self.interact_director,
        )
        self.planner = _planner.Planner(
            self.cfg, self.llm, self.goal_manager,
            gambits_store=self.gambits_store,
            current_ctx_provider=self._current_gambit_ctx,
            neighbors_provider=self._zone_neighbors,
        )
        # Reactive chat layer - polls events.jsonl for chat_received,
        # auto-accretes interactions to the relationship store, and
        # LLM-dispatches lines worth a reply. Outbound replies ride
        # the same cmd_inbox.txt path as nav commands. Initialised
        # last so it can borrow _issue_command from the server.
        self.chat = _chat_handler.ChatHandler(
            self.cfg, self.llm,
            issue_command=self._issue_command,
        )
        # User-edited goal file: the user types a free-text instruction
        # into <repo>/user_goal.txt and saves. We watch its mtime; on
        # change we hand the contents to the planner (or clear, if empty).
        # The current mtime at startup is treated as "already applied"
        # so an existing file doesn't replay on every restart - the
        # persistent goal tree on disk is the source of truth for what
        # the agent is currently doing.
        self._user_goal_mtime = 0.0
        ugf = self.cfg.paths.user_goal_file()
        if ugf.exists():
            try:
                self._user_goal_mtime = ugf.stat().st_mtime
            except OSError:
                pass

    def _read_user_goal(self) -> str:
        """Return the current user_goal.txt contents (stripped). Empty
        string when the file is missing/blank/unreadable. Used by the
        menu_judge so it has the player's stated intent as context for
        falling-through dialog choices."""
        try:
            return self.cfg.paths.user_goal_file().read_text(
                encoding='utf-8', errors='replace').strip()
        except (FileNotFoundError, OSError):
            return ''

    # Maximum age (seconds) before a state file is treated as ghost
    # data. Addons publish at 5-10 Hz when the client is running; if
    # nothing has been written for several seconds, the client is
    # closed (or the addon crashed). Ticking the goal/farm state
    # machines against frozen snapshots churns through false moves
    # against the orchestrator's view of a world that doesn't exist.
    SNAPSHOT_STALE_S = 5.0

    def _is_state_fresh(self, path: Path) -> bool:
        """True iff `path` exists and was written within SNAPSHOT_STALE_S.
        Stale or missing -> False; callers should treat as 'addon not
        publishing, idle the state machines.'"""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) <= self.SNAPSHOT_STALE_S

    def _read_player_snapshot(self):
        """Build the goal-manager snapshot from the addon's nav_status.json.

        Returns an all-None snapshot if the file is missing OR stale -
        the goal manager treats absent zone_id as 'don't dispatch.'"""
        from . import goal_manager as _gm
        status_file = IPC_DIR / 'nav_status.json'
        if not self._is_state_fresh(status_file):
            return _gm._Snapshot(zone_id=None, x=None, y=None, z=None,
                                 moving=False, equipped=self._read_equipped_map())
        try:
            with open(status_file) as f:
                s = json.load(f)
        except (json.JSONDecodeError, OSError):
            return _gm._Snapshot(zone_id=None, x=None, y=None, z=None,
                                 moving=False, equipped=self._read_equipped_map())
        return _gm._Snapshot(
            zone_id=s.get('zone_id'),
            x=s.get('x'),
            y=s.get('y'),
            z=s.get('z'),
            moving=bool(s.get('moving', False)),
            equipped=self._read_equipped_map(),
        )

    def _zone_neighbors(self, zone_id: int) -> list[int]:
        """Direct one-zoneline neighbors of `zone_id`. De-duped and sorted
        so the planner's prompt is stable across calls. Returns [] for
        unknown zones (e.g. instances we have no transitions for)."""
        trans = self.transitions.get(zone_id, [])
        seen: set[int] = set()
        for t in trans:
            tz = t.get('to')
            if isinstance(tz, int) and tz != zone_id:
                seen.add(tz)
        return sorted(seen)

    def _read_equipped_map(self) -> dict[str, str | None] | None:
        """Read inventory.json and return slot_name -> equipped item name.
        None if the channel hasn't published yet; equip-goal completion
        treats None as 'wait for next snapshot'."""
        path = self.cfg.paths.state_dir(self.cfg.character) / 'inventory.json'
        if not path.exists():
            return None
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        equipped = d.get('equipped') or {}
        return {
            slot: (item.get('name') if item else None)
            for slot, item in equipped.items()
        }

    def _read_combat_snapshot(self):
        """Build the farming director's snapshot from combat.json.
        Returns a _Snapshot with all fields possibly None - the director
        treats nil as "no info yet, retry". Same staleness gate as the
        nav snapshot: if combat.json hasn't been touched in
        SNAPSHOT_STALE_S, treat it as missing rather than authoritative."""
        from . import farming as _farming
        path = self.cfg.paths.state_dir(self.cfg.character) / 'combat.json'
        empty = _farming._Snapshot(
            self_hp_pct=None, self_hp=None, self_mp_pct=None,
            self_status=None, self_lvl=None,
            self_main_job=None, self_sub_job=None, self_sub_lvl=None,
            target_name=None, target_alive=None, target_hp_pct=None,
            engaged=False,
        )
        if not self._is_state_fresh(path):
            return empty
        try:
            with open(path) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            return empty
        s = d.get('self') or {}
        t = d.get('target') or {}
        return _farming._Snapshot(
            self_hp_pct=s.get('hp_pct'),
            self_hp=s.get('hp'),
            self_mp_pct=s.get('mp_pct'),
            self_status=s.get('status'),
            self_lvl=s.get('main_job_lvl'),
            self_main_job=s.get('main_job'),
            self_sub_job=s.get('sub_job'),
            self_sub_lvl=s.get('sub_job_lvl'),
            target_name=t.get('name') if t else None,
            target_alive=t.get('alive') if t else None,
            target_hp_pct=t.get('hp_pct') if t else None,
            target_distance=t.get('distance') if t else None,
            target_x=t.get('x') if t else None,
            target_y=t.get('y') if t else None,
            target_z=t.get('z') if t else None,
            target_server_id=t.get('server_id') if t else None,
            target_claimed_by_us=bool(t.get('claimed_by_us', False)) if t else False,
            engaged=bool(d.get('engaged', False)),
            nearby_enemies=d.get('nearby_enemies') or [],
            menu_open=bool(s.get('menu_open', False)),
        )

    def _read_combat_state(self) -> dict | None:
        """Best-effort full-payload read of combat.json. Used for context
        derivation (job/sub/party); returns None on missing/malformed."""
        path = self.cfg.paths.state_dir(self.cfg.character) / 'combat.json'
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _current_gambit_ctx(self) -> dict[str, str]:
        """Build the live (main_job, sub_job, in_party) context from
        combat.json. Falls back to all-wildcard if combat.json is
        missing - that selects the universal `*/*/*` set, if any."""
        state = self._read_combat_state()
        return self._gambits_mod.context_from_combat(state)

    def poll_gambit_context(self):
        """Recompute the current context from combat.json; if it
        changed since last tick, redeploy the resolved gambit list.
        Always cheap - usually a single dict-eq check after one JSON
        read; the actual resolve+write only fires on real changes
        (zoning, /sj <job>, party invite/leave)."""
        ctx = self._current_gambit_ctx()
        if ctx == self._last_gambit_ctx:
            return
        self._last_gambit_ctx = ctx
        try:
            merged = self._gambits_mod.deploy_active(
                self.cfg, self.gambits_store, ctx,
            )
        except Exception as e:
            print(f'  gambits: deploy_active failed: {e}')
            return
        print(f'  gambits: ctx={ctx["main_job"]}/{ctx["sub_job"]}/{ctx["in_party"]}'
              f'  active={len(merged)}')

    # Zones that are NOT present on this LandSandBoat server. The
    # bundled zone_transitions.json was generated from retail FFXI data
    # (~200 zones) but our server runs RoZ + CoP era only. Loading the
    # missing zones means the planner sometimes picks them ("East
    # Ronfaure S") and the agent paths off across non-existent geometry.
    # Filter at load time so the rest of the system never sees them.
    _ERA_EXCLUDED_NAME_SUFFIXES = (' S',)              # WotG past-Vana
    _ERA_EXCLUDED_NAME_KEYWORDS = (                    # ToAU
        'Aht Urhgan', 'Wajaom', 'Bhaflau', 'Arrapago',
        'Talacca', 'Mount Zhayolm', 'Halvung', 'Mamook',
        'Aydeewa', 'Alzadaal', 'Caedarva',
    )

    def _is_era_excluded(self, name: str) -> bool:
        """True if the zone name is from a post-CoP expansion that the
        LSB server does not implement."""
        if not name:
            return False
        for suf in self._ERA_EXCLUDED_NAME_SUFFIXES:
            if name.endswith(suf):
                return True
        nl = name.lower()
        for kw in self._ERA_EXCLUDED_NAME_KEYWORDS:
            if kw.lower() in nl:
                return True
        return False

    def _load_transitions(self):
        if not TRANSITIONS_FILE.exists():
            print(f'Warning: {TRANSITIONS_FILE} not found')
            return
        with open(TRANSITIONS_FILE) as f:
            data = json.load(f)
        excluded_zids: set[int] = set()
        for zid_str, name in data.get('names', {}).items():
            zid = int(zid_str)
            if self._is_era_excluded(name):
                excluded_zids.add(zid)
                continue
            self.zone_names[zid] = name
            self.name_to_zone[name.lower()] = zid
        for zid_str, trans_list in data.get('transitions', {}).items():
            zid = int(zid_str)
            if zid in excluded_zids:
                continue
            # Also drop transitions whose destination is an excluded zone.
            filtered = [t for t in trans_list if t.get('zone') not in excluded_zids]
            for t in filtered:
                if 'z' not in t:
                    t['z'] = 0.0
            self.transitions[zid] = filtered
        print(f'Loaded {len(self.zone_names)} zone names, '
              f'{sum(len(v) for v in self.transitions.values())} transitions '
              f'(excluded {len(excluded_zids)} post-CoP zones)')

    def resolve_zone_name(self, name: str):
        name_lower = name.lower()
        if name_lower in self.name_to_zone:
            return self.name_to_zone[name_lower]
        for full_name, zid in self.name_to_zone.items():
            if name_lower in full_name:
                return zid
        return None

    def _find_arrival_transition(self, from_zone: int, from_trans: dict, to_zone: int):
        """Find the transition in to_zone that leads back to from_zone, closest to from_trans position."""
        to_trans = self.transitions.get(to_zone, [])
        best = None
        best_dist = float('inf')
        for i, t in enumerate(to_trans):
            if t['to'] == from_zone:
                dx = t['x'] - from_trans['x']
                dy = t['y'] - from_trans['y']
                d = dx*dx + dy*dy
                if d < best_dist:
                    best_dist = d
                    best = i
        return best

    @staticmethod
    def _path_length(path):
        total = 0.0
        for k in range(1, len(path)):
            dx = path[k][0] - path[k-1][0]
            dy = path[k][1] - path[k-1][1]
            dz = path[k][2] - path[k-1][2]
            total += (dx*dx + dy*dy + dz*dz) ** 0.5
        return total

    def _build_reachability(self, zone_id: int):
        """Build reachability map: (i,j) -> distance or None if unreachable."""
        if zone_id in self.reachability:
            return self.reachability[zone_id]

        trans = self.transitions.get(zone_id, [])
        if not trans:
            self.reachability[zone_id] = {}
            return self.reachability[zone_id]

        try:
            mesh = self.get_mesh(zone_id)
        except FileNotFoundError:
            self.reachability[zone_id] = {}
            return self.reachability[zone_id]

        reach = {}
        for i, t_from in enumerate(trans):
            src = self.game_to_recast(t_from['x'], t_from['y'], t_from['z'])
            for j, t_to in enumerate(trans):
                if i == j:
                    reach[(i, j)] = 0.0
                    continue
                dst = self.game_to_recast(t_to['x'], t_to['y'], t_to['z'])
                path = navmesh.find_path(mesh, src, dst, exclude_flags=0)
                if path:
                    last = self.recast_to_game(path[-1][0], path[-1][1], path[-1][2])
                    dx = last[0] - t_to['x']
                    dy = last[1] - t_to['y']
                    if (dx*dx + dy*dy) ** 0.5 < 30.0:
                        reach[(i, j)] = self._path_length(path)
                    else:
                        reach[(i, j)] = None
                else:
                    reach[(i, j)] = None

        self.reachability[zone_id] = reach
        reachable = sum(1 for v in reach.values() if v is not None)
        print(f'  Zone {zone_id} reachability: {reachable}/{len(reach)} pairs connected')
        return reach

    def _player_reachable_transitions(self, zone_id: int, player_pos: tuple):
        """Find which transitions the player can reach. Returns [(trans_idx, distance), ...]."""
        trans = self.transitions.get(zone_id, [])
        if not trans:
            return []

        try:
            mesh = self.get_mesh(zone_id)
        except FileNotFoundError:
            return []

        start_rc = self.game_to_recast(*player_pos)
        reachable = []
        for i, t in enumerate(trans):
            end_rc = self.game_to_recast(t['x'], t['y'], t['z'])
            path = navmesh.find_path(mesh, start_rc, end_rc, exclude_flags=0)
            if path:
                last = self.recast_to_game(path[-1][0], path[-1][1], path[-1][2])
                dx = last[0] - t['x']
                dy = last[1] - t['y']
                if (dx*dx + dy*dy) ** 0.5 < 30.0:
                    reachable.append((i, self._path_length(path)))
        return reachable

    def plan_cross_zone(self, from_zone: int, player_pos: tuple, to_zone: int, target_pos=None, avoid_zones=None):
        if from_zone == to_zone:
            return None

        player_reachable = self._player_reachable_transitions(from_zone, player_pos)
        if not player_reachable:
            return None

        if avoid_zones:
            trans = self.transitions.get(from_zone, [])
            player_reachable = [
                (i, d) for i, d in player_reachable
                if trans[i]['to'] not in avoid_zones
            ]
            if not player_reachable:
                return None

        # Dijkstra on transition-level graph, weighted by walking distance
        # Nodes are (zone_id, trans_idx) tuples
        dist = {}
        prev = {}
        heap = []
        seq = 0

        for i, path_dist in player_reachable:
            node = (from_zone, i)
            dist[node] = path_dist
            prev[node] = None
            heapq.heappush(heap, (path_dist, seq, from_zone, i))
            seq += 1

        goal_node = None
        while heap:
            cost, _, zone, tidx = heapq.heappop(heap)
            node = (zone, tidx)
            if cost > dist.get(node, float('inf')):
                continue

            trans_list = self.transitions.get(zone, [])
            if not trans_list or tidx >= len(trans_list):
                continue
            t = trans_list[tidx]

            if t['to'] == to_zone:
                goal_node = node
                break

            next_zone = t['to']
            arrival_idx = self._find_arrival_transition(zone, t, next_zone)
            if arrival_idx is None:
                continue

            arrival_node = (next_zone, arrival_idx)
            if cost < dist.get(arrival_node, float('inf')):
                dist[arrival_node] = cost
                prev[arrival_node] = node
                heapq.heappush(heap, (cost, seq, next_zone, arrival_idx))
                seq += 1

            reach = self._build_reachability(next_zone)
            next_trans = self.transitions.get(next_zone, [])
            for j in range(len(next_trans)):
                if j == arrival_idx:
                    continue
                edge_dist = reach.get((arrival_idx, j))
                if edge_dist is not None:
                    neighbor = (next_zone, j)
                    new_cost = cost + edge_dist
                    if new_cost < dist.get(neighbor, float('inf')):
                        dist[neighbor] = new_cost
                        prev[neighbor] = arrival_node
                        heapq.heappush(heap, (new_cost, seq, next_zone, j))
                        seq += 1

        if goal_node is None:
            return None

        path = []
        node = goal_node
        while node is not None:
            path.append(node)
            node = prev.get(node)
        path.reverse()

        total_cost = dist.get(goal_node, 0)
        print(f'  Route cost: {total_cost:.0f}y walking distance')

        route = []
        prev_zone = from_zone
        for i, (zone, tidx) in enumerate(path):
            t = self.transitions[zone][tidx]
            if t['to'] == prev_zone and i > 0:
                prev_zone = zone
                continue
            route.append({
                'zone_id': zone,
                'target': [round(t['x'], 1), round(t['y'], 1), 0],
                'is_transition': True,
                'next_zone': t['to'],
            })
            prev_zone = zone

        if target_pos:
            route.append({
                'zone_id': to_zone,
                'target': [round(target_pos[0], 1), round(target_pos[1], 1), round(target_pos[2], 1)],
                'is_transition': False,
            })

        return route

    def _avoid_transitions(self, waypoints, zone_id, target_pos=None):
        """Push waypoints away from zone transition points to prevent accidental zoning.
        If target_pos given, skip the transition closest to the target."""
        trans = self.transitions.get(zone_id, [])
        if not trans:
            return waypoints

        skip_idx = None
        if target_pos:
            best_d = float('inf')
            for i, t in enumerate(trans):
                d = (t['x'] - target_pos[0])**2 + (t['y'] - target_pos[1])**2
                if d < best_d:
                    best_d = d
                    skip_idx = i

        AVOID_RADIUS = 10.0
        PUSH_DIST = 12.0

        for i, t in enumerate(trans):
            if i == skip_idx:
                continue
            tx, ty = t['x'], t['y']
            new_wps = []
            for j, wp in enumerate(waypoints):
                dx = wp[0] - tx
                dy = wp[1] - ty
                d = (dx*dx + dy*dy) ** 0.5
                if d < AVOID_RADIUS and d > 0.01:
                    moving_away = False
                    if j + 1 < len(waypoints):
                        nxt = waypoints[j + 1]
                        d_next = ((nxt[0] - tx)**2 + (nxt[1] - ty)**2) ** 0.5
                        if d_next > d:
                            moving_away = True
                    if moving_away:
                        new_wps.append(wp)
                    else:
                        nx, ny = dx / d, dy / d
                        new_wps.append([round(tx + nx * PUSH_DIST, 2), round(ty + ny * PUSH_DIST, 2), wp[2]])
                else:
                    new_wps.append(wp)
            waypoints = new_wps

        return waypoints

    def load_collision(self, zone_id: int):
        path = COLLISION_DIR / f'{zone_id}.json'
        if not path.exists():
            raise FileNotFoundError(f'No collision data for zone {zone_id}')

        with open(path) as f:
            data = json.load(f)

        verts_raw = np.array(data['vertices'], dtype=np.float64)
        tris = np.array(data['triangles'], dtype=np.int32)

        valid = np.all(np.abs(verts_raw) < 1500, axis=1)
        if not np.all(valid):
            bad = int(np.sum(~valid))
            print(f'  Warning: filtering {bad}/{len(verts_raw)} corrupt vertices in zone {zone_id}')
            good_idx = np.where(valid)[0]
            remap = np.full(len(verts_raw), -1, dtype=np.int32)
            remap[good_idx] = np.arange(len(good_idx), dtype=np.int32)
            verts_raw = verts_raw[valid]
            tri_valid = np.all(remap[tris] >= 0, axis=1)
            tris = remap[tris[tri_valid]]

        # Strip orphaned vertices that bloat the bounding box
        referenced = set(tris.flatten().tolist())
        orphaned = np.array([i not in referenced for i in range(len(verts_raw))], dtype=bool)
        if orphaned.any():
            keep = ~orphaned
            n_orphaned = int(orphaned.sum())
            print(f'  Stripping {n_orphaned} orphaned vertices in zone {zone_id}')
            remap = np.full(len(verts_raw), -1, dtype=np.int32)
            remap[np.where(keep)[0]] = np.arange(int(keep.sum()), dtype=np.int32)
            verts_raw = verts_raw[keep]
            tris = remap[tris]

        verts_raw = verts_raw.astype(np.float32)

        # JSON vertices are in Ashita convention (X=EW, Y=NS, Z=elev, with
        # Z down-positive). Recast wants its Y axis to be "physical up" for
        # walkability filtering, so we swap Y↔Z and negate Z: the sole
        # coordinate transformation in the entire pipeline.
        # See ashita_to_recast() below - this is the vectorised form.
        verts = np.column_stack([
            verts_raw[:, 0],       # Recast.x = Ashita.X
            -verts_raw[:, 2],      # Recast.y = -Ashita.Z  (physical up)
            verts_raw[:, 1],       # Recast.z = Ashita.Y
        ]).astype(np.float32)

        # The (x, y, z) -> (x, -z, y) transform has positive determinant, so
        # triangle winding is preserved - no index swap needed.
        return verts, tris


    # Per-zone NavSettings overrides. Keys match navmesh.NavSettings field
    # names (cell_size, agent_radius, agent_max_slope, agent_max_climb, ...).
    # Defaults (set in get_mesh) apply to every zone not listed here. Add
    # entries like `106: {'agent_radius': 1.8}` to override specific zones.
    ZONE_NAV_OVERRIDES: dict = {}

    def get_mesh(self, zone_id: int, wider_radius: bool = False):
        # Defaults derived from end-to-end tuning in Attohwa Chasm
        # (zone 7 - narrowest trails + densest instance collision):
        #   - agent_radius=0.25 opens 1y-wide ledges and cliff trails;
        #     larger values fragment the navmesh.
        #   - agent_max_slope=45° matches FFXI's actual walkable angle.
        #   - agent_max_climb=0.6 absorbs low rocks and small steps
        #     without merging cliff terraces.
        #   - cell_height=0.06 keeps stacked floors (cave levels,
        #     bridges, multi-tier zones) as separate polys.
        radius = self.FALLBACK_AGENT_RADIUS if wider_radius else 0.75
        overrides = self.ZONE_NAV_OVERRIDES.get(zone_id, {})
        if 'agent_radius' in overrides and not wider_radius:
            radius = overrides['agent_radius']
        key = (zone_id, radius)
        if key not in self.meshes:
            print(f'Building navmesh for zone {zone_id} (radius={radius})...')
            t0 = time.time()
            verts, tris = self.load_collision(zone_id)
            settings = navmesh.NavSettings()
            settings.cell_size = 0.20
            settings.cell_height = 0.06
            settings.agent_radius = radius
            settings.agent_max_slope = 45.0
            settings.agent_max_climb = 0.6
            settings.region_min_size = 2
            settings.region_merge_size = 20
            settings.tile_size = 1024
            for k, v in overrides.items():
                if k == 'agent_radius' and wider_radius:
                    continue  # honor the fallback radius, not the override
                setattr(settings, k, v)
                print(f'  Override {k}={v}')
            dropoffs = self._load_dropoffs(zone_id)
            self.meshes[key] = navmesh.build_navmesh(
                verts, tris, settings,
                off_mesh_connections=dropoffs,
            )
            print(f'  Built in {time.time()-t0:.1f}s')
            self._apply_obstacle_blocking(zone_id, self.meshes[key])
        return self.meshes[key]

    # When True, _load_dropoffs ignores the on-disk JSON entirely and
    # returns an empty list. We're parking the auto-detected drop-offs
    # for now (too many false positives in tight zones, and wide-radius
    # routing already handles most legitimate cliff routes); known-good
    # drops will be hand-added back later via the overrides.added block
    # once we re-enable loading.
    USE_AUTO_DROPOFFS = False

    def _load_dropoffs(self, zone_id: int):
        """Read nav/data/dropoffs/<zone_id>.json and return a list of
        off-mesh connections in Recast space, suitable for
        navmesh.build_navmesh(). Applies the overrides block: 'added' entries
        are appended and 'removed' entries (matched by approximate start XY)
        are filtered from the auto-detected set."""
        if not self.USE_AUTO_DROPOFFS:
            return []
        path = DROPOFF_DIR / f'{zone_id}.json'
        if not path.exists():
            return []
        with open(path) as f:
            data = json.load(f)
        auto = data.get('connections', []) or []
        overrides = data.get('overrides', {}) or {}
        removed = overrides.get('removed', []) or []
        added = overrides.get('added', []) or []

        def matches_removed(c):
            sx, sy = c['start'][0], c['start'][1]
            for r in removed:
                rsx, rsy = r['start'][0], r['start'][1]
                if abs(rsx - sx) < 1.0 and abs(rsy - sy) < 1.0:
                    return True
            return False

        merged = [c for c in auto if not matches_removed(c)] + added

        # Convert from runtime Ashita to Recast space.
        out = []
        for c in merged:
            s_game = c['start']; e_game = c['end']
            s_rc = self.game_to_recast(s_game[0], s_game[1], s_game[2])
            e_rc = self.game_to_recast(e_game[0], e_game[1], e_game[2])
            out.append({
                'start': s_rc,
                'end': e_rc,
                'radius': float(c.get('radius', 0.75)),
                'bidir': bool(c.get('bidir', False)),
                'area': int(c.get('area', 2)),
                'flags': int(c.get('flags', 1)),
            })
        if out:
            print(f'  Loaded {len(out)} drop-off connection(s)')
        return out

    def load_obstacles(self, zone_id: int):
        if zone_id not in self.obstacles:
            path = OBSTACLE_DIR / f'{zone_id}.json'
            if path.exists():
                with open(path) as f:
                    self.obstacles[zone_id] = json.load(f)
            else:
                self.obstacles[zone_id] = []
        return self.obstacles[zone_id]

    def save_obstacles(self, zone_id: int):
        OBSTACLE_DIR.mkdir(parents=True, exist_ok=True)
        with open(OBSTACLE_DIR / f'{zone_id}.json', 'w') as f:
            json.dump(self.obstacles[zone_id], f)

    def add_obstacle(self, zone_id: int, x, y, z):
        obstacles = self.load_obstacles(zone_id)
        for obs in obstacles:
            if ((obs[0] - x)**2 + (obs[1] - y)**2) ** 0.5 < 2.0:
                return
        obstacles.append([round(x, 1), round(y, 1), round(z, 1)])
        self.save_obstacles(zone_id)
        print(f'  Stored obstacle at ({x:.1f}, {y:.1f}) for zone {zone_id} ({len(obstacles)} total)')
        center_rc = self.game_to_recast(x, y, z)
        for key, mesh in self.meshes.items():
            if key[0] != zone_id:
                continue
            n = navmesh.mark_polys_blocked(mesh, center_rc, OBSTACLE_BLOCK_RADIUS)
            if n > 0:
                print(f'  Blocked {n} polys near new obstacle (radius={key[1]})')

    def _apply_obstacle_blocking(self, zone_id: int, mesh):
        obstacles = self.load_obstacles(zone_id)
        if not obstacles:
            return
        total = 0
        for obs in obstacles:
            center_rc = self.game_to_recast(obs[0], obs[1], obs[2])
            total += navmesh.mark_polys_blocked(mesh, center_rc, OBSTACLE_BLOCK_RADIUS)
        if total > 0:
            print(f'  Blocked {total} polys for {len(obstacles)} obstacles in zone {zone_id}')

    def game_to_recast(self, x, y, z):
        """Ashita LocalPosition (X=EW, Y=NS, Z=elev down-positive) -> Recast
        (Y-up with floor normals pointing +Y). Swap Y↔Z and negate Z. This
        is the ONLY coordinate transformation in the server - everything
        else uses Ashita directly."""
        return (x, -z, y)

    def recast_to_game(self, rx, ry, rz):
        """Inverse of game_to_recast. Recast (x, y, z) -> Ashita (x, z, -y)."""
        return (rx, rz, -ry)

    def _path_to_waypoints(self, path_rc, max_segment=1.0):
        raw = []
        for p in path_rc:
            gx, gy, gz = self.recast_to_game(p[0], p[1], p[2])
            raw.append([gx, gy, gz])

        if len(raw) < 2:
            return [[round(v, 2) for v in p] for p in raw]

        waypoints = [raw[0]]
        for i in range(1, len(raw)):
            prev = raw[i - 1]
            curr = raw[i]
            dx, dy, dz = curr[0]-prev[0], curr[1]-prev[1], curr[2]-prev[2]
            dist = (dx*dx + dy*dy) ** 0.5
            if dist > max_segment:
                steps = int(dist / max_segment)
                for s in range(1, steps + 1):
                    t = s / (steps + 1)
                    waypoints.append([
                        prev[0] + dx*t,
                        prev[1] + dy*t,
                        prev[2] + dz*t,
                    ])
            waypoints.append(curr)

        return [[round(v, 2) for v in p] for p in waypoints]

    def _end_dist_2d(self, waypoints, tx, ty):
        if not waypoints:
            return float('inf')
        last = waypoints[-1]
        return ((last[0] - tx)**2 + (last[1] - ty)**2) ** 0.5

    def _avoid_obstacles(self, waypoints, zone_id):
        obstacles = self.load_obstacles(zone_id)
        if not obstacles:
            return waypoints

        AVOID_RADIUS = 3.0
        PUSH_DIST = 4.0

        for obs in obstacles:
            ox, oy = obs[0], obs[1]
            new_wps = []
            for wp in waypoints:
                dx = wp[0] - ox
                dy = wp[1] - oy
                d = (dx*dx + dy*dy) ** 0.5
                if d < AVOID_RADIUS and d > 0.01:
                    nx, ny = dx / d, dy / d
                    new_wps.append([round(ox + nx * PUSH_DIST, 2), round(oy + ny * PUSH_DIST, 2), wp[2]])
                else:
                    new_wps.append(wp)
            waypoints = new_wps

        return waypoints

    def nearest_reachable(self, zone_id: int, target_xy: tuple[float, float],
                          *, max_snap_y: float = 8.0) -> tuple[float, float] | None:
        """Return the navmesh point closest to `target_xy` in this zone,
        or None if no walkable poly exists within `max_snap_y` yalms.

        Used by the farming director's wander state to validate that an
        exploration target sits on (or very near) walkable terrain
        before dispatching the goto. Without this we keep dispatching
        partial-path requests to cells that fall in water / walls /
        off-mesh nooks, then waiting WANDER_TIMEOUT_S to give up.

        Cheap: navmesh poly centers are cached per-zone, lookup is a
        sweep through them. No path-finding involved."""
        try:
            mesh = self.get_mesh(zone_id)
        except Exception:
            return None
        try:
            import numpy as _np
            centers = _np.array(navmesh.get_poly_centers(mesh))
        except Exception:
            return None
        if len(centers) == 0:
            return None
        tx, ty = target_xy
        # Recast x/z = game x/y; recast y is elevation (we ignore it
        # for snap-distance - the target's z is unknown anyway).
        d2 = (centers[:, 0] - tx) ** 2 + (centers[:, 2] - ty) ** 2
        idx = int(d2.argmin())
        dist = float(d2[idx]) ** 0.5
        if dist > max_snap_y:
            return None
        return (float(centers[idx, 0]), float(centers[idx, 2]))

    def find_path(self, zone_id, start_game, end_game, avoid_zone_exits=True,
                  wider_radius: bool = False):
        mesh = self.get_mesh(zone_id, wider_radius=wider_radius)
        start_rc = self.game_to_recast(*start_game)
        end_rc = self.game_to_recast(*end_game)
        tx, ty = end_game[0], end_game[1]

        path_rc = navmesh.find_path(mesh, start_rc, end_rc)
        best = self._path_to_waypoints(path_rc)
        best_dist = self._end_dist_2d(best, tx, ty)
        initial_raw_len = len(path_rc)

        if best_dist > 5.0:
            centers = navmesh.get_poly_centers(mesh)
            candidate_elevs = set()
            for c in centers:
                gx, gy = c[0], c[2]  # recast x,z -> game x,y
                if (gx - tx)**2 + (gy - ty)**2 < 10**2:
                    candidate_elevs.add(round(-c[1], 1))  # recast y -> game z = -recast_y

            for game_z in sorted(candidate_elevs):
                end_try = self.game_to_recast(tx, ty, game_z)
                path_try = navmesh.find_path(mesh, start_rc, end_try)
                wps = self._path_to_waypoints(path_try)
                d = self._end_dist_2d(wps, tx, ty)
                if d < best_dist:
                    best = wps
                    best_dist = d
                if best_dist < 5.0:
                    break

        best = self._avoid_obstacles(best, zone_id)
        if avoid_zone_exits:
            best = self._avoid_transitions(best, zone_id, target_pos=end_game)

        if best:
            last = best[-1]
            dx = last[0] - end_game[0]
            dy = last[1] - end_game[1]
            if (dx*dx + dy*dy) ** 0.5 < 15.0:
                best.append([round(end_game[0], 2), round(end_game[1], 2), last[2]])

        # Diagnostics when a path call produces nothing or stops far short.
        # Exposes whether Recast returned empty, whether the target poly
        # exists, how many polys live near the endpoints, and whether
        # exclude_flags was the culprit.
        if not best or self._end_dist_2d(best, tx, ty) > 10.0:
            try:
                path_nofilter = navmesh.find_path(mesh, start_rc, end_rc, exclude_flags=0)
            except Exception:
                path_nofilter = []
            import numpy as _np
            centers = _np.array(navmesh.get_poly_centers(mesh))
            near_start = 0
            near_end = 0
            if len(centers) > 0:
                near_start = int(((centers[:,0]-start_rc[0])**2 + (centers[:,2]-start_rc[2])**2 < 25).sum())
                near_end = int(((centers[:,0]-end_rc[0])**2 + (centers[:,2]-end_rc[2])**2 < 25).sum())
            print(f'  [find_path diag] raw_len={initial_raw_len}  with_flags0_len={len(path_nofilter)}  '
                  f'polys_within_5y_of_start={near_start}  polys_within_5y_of_end={near_end}  '
                  f'best_dist_to_target={self._end_dist_2d(best, tx, ty):.1f}y')

        return best

    def generate_search_points(self, zone_id: int, player_game: list, entity_positions: list = None) -> list:
        mesh = self.get_mesh(zone_id)
        centers = navmesh.get_poly_centers(mesh)

        game_points = []
        for c in centers:
            gx, gy, gz = self.recast_to_game(c[0], c[1], c[2])
            game_points.append([gx, gy, gz])

        if not game_points:
            return []

        SPACING = 80.0
        SPACING_SQ = SPACING * SPACING
        covered = [False] * len(game_points)
        search_points = []

        px, py = player_game[0], player_game[1]
        indices = sorted(range(len(game_points)),
                         key=lambda i: (game_points[i][0]-px)**2 + (game_points[i][1]-py)**2)

        for i in indices:
            if covered[i]:
                continue
            pt = game_points[i]
            search_points.append([round(pt[0], 1), round(pt[1], 1), round(pt[2], 1)])
            for j in range(len(game_points)):
                if not covered[j]:
                    dx = game_points[j][0] - pt[0]
                    dy = game_points[j][1] - pt[1]
                    if dx*dx + dy*dy < SPACING_SQ:
                        covered[j] = True

        entity_near = []
        entity_far = []
        ENTITY_RADIUS_SQ = 150.0 ** 2
        ent_pos = entity_positions or []

        for sp in search_points:
            near = False
            for ep in ent_pos:
                dx = sp[0] - ep[0]
                dy = sp[1] - ep[1]
                if dx*dx + dy*dy < ENTITY_RADIUS_SQ:
                    near = True
                    break
            if near:
                entity_near.append(sp)
            else:
                entity_far.append(sp)

        ordered_unsearched = self._order_search_points(entity_far, px, py)
        ordered_searched = self._order_search_points(entity_near, px, py)
        print(f'  Search: {len(ordered_unsearched)} unsearched, {len(ordered_searched)} already covered')
        return ordered_unsearched + ordered_searched

    def _order_search_points(self, points: list, start_x: float, start_y: float) -> list:
        if len(points) <= 1:
            return points

        remaining = list(range(len(points)))
        ordered = []
        cx, cy = start_x, start_y

        while remaining:
            best_idx = None
            best_dist = float('inf')
            for i, pi in enumerate(remaining):
                dx = points[pi][0] - cx
                dy = points[pi][1] - cy
                d = dx*dx + dy*dy
                if d < best_dist:
                    best_dist = d
                    best_idx = i
            pi = remaining.pop(best_idx)
            ordered.append(points[pi])
            cx, cy = points[pi][0], points[pi][1]

        return ordered

    def handle_request(self, req):
        action = req.get('action')
        zone_id = req.get('zone_id')
        seq = req.get('seq')

        if action == 'goto':
            new_obs = req.get('new_obstacle')
            if new_obs and zone_id:
                self.add_obstacle(zone_id, new_obs[0], new_obs[1], new_obs[2])

            player = req['player']
            target = req['target']
            wider = bool(req.get('wider_radius'))
            tag = ' [wider_radius]' if wider else ''
            print(f'[#{seq}] goto zone={zone_id} from=({player[0]:.1f}, {player[1]:.1f}) to=({target[0]:.1f}, {target[1]:.1f}){tag}')

            try:
                waypoints = self.find_path(
                    zone_id,
                    (player[0], player[1], player[2]),
                    (target[0], target[1], target[2]),
                    wider_radius=wider,
                )

                if not waypoints:
                    self.write_response({'status': 'no_path', 'zone_id': zone_id, 'seq': seq})
                    print(f'  No path found')
                    return

                last = waypoints[-1]
                dx = last[0] - target[0]
                dy = last[1] - target[1]
                end_dist = (dx*dx + dy*dy) ** 0.5
                partial = end_dist > 10.0

                if partial:
                    print(f'  Partial: {len(waypoints)} wps, {end_dist:.0f}y short')
                else:
                    print(f'  OK: {len(waypoints)} wps')

                self.write_response({
                    'status': 'partial' if partial else 'ok',
                    'zone_id': zone_id,
                    'waypoints': waypoints,
                    'end_dist': round(end_dist, 1),
                    'seq': seq,
                    # Pass through the orchestrator's intent: when set,
                    # the addon will drop any in-flight retry / route /
                    # search state before accepting this new path.
                    # Stops stale leftovers (old segment targets, retry
                    # walks toward an old destination) from firing
                    # request_path's that wander the agent off-course.
                    'reset_state': bool(req.get('reset_state')),
                })
            except Exception as e:
                print(f'  Error: {e}')
                self.write_response({'status': 'error', 'message': str(e), 'zone_id': zone_id, 'seq': seq})

        elif action == 'search_points':
            print(f'[#{seq}] search_points zone={zone_id}')
            try:
                player = req.get('player', [0, 0, 0])
                ent_pos = req.get('entity_positions', [])
                waypoints = self.generate_search_points(zone_id, player, ent_pos)
                self.write_response({
                    'action': 'search_points',
                    'status': 'ok',
                    'zone_id': zone_id,
                    'waypoints': waypoints,
                    'seq': seq,
                })
                print(f'  Generated {len(waypoints)} search points')
            except Exception as e:
                print(f'  Error: {e}')
                self.write_response({'action': 'search_points', 'status': 'error', 'message': str(e), 'seq': seq})

        elif action == 'cross_zone_goto':
            target_zone = req.get('target_zone')
            player = req['player']
            target = req.get('target')
            avoid_zones = req.get('avoid_zones')
            print(f'[#{seq}] cross_zone_goto zone={zone_id} -> {target_zone}'
                  f' ({self.zone_names.get(target_zone, "?")})'
                  f'{" avoid=" + str(avoid_zones) if avoid_zones else ""}')

            try:
                if zone_id == target_zone:
                    if target:
                        self.handle_request({
                            'action': 'goto', 'zone_id': zone_id,
                            'player': player, 'target': target, 'seq': seq,
                        })
                    else:
                        self.write_response({
                            'action': 'cross_zone_goto', 'status': 'already_there',
                            'zone_id': zone_id, 'seq': seq,
                        })
                        print(f'  Already in target zone')
                    return

                route = self.plan_cross_zone(
                    zone_id, tuple(player), target_zone,
                    tuple(target) if target else None,
                    avoid_zones=set(avoid_zones) if avoid_zones else None)

                if not route:
                    self.write_response({
                        'action': 'cross_zone_goto', 'status': 'no_route',
                        'zone_id': zone_id, 'seq': seq,
                    })
                    print(f'  No route found')
                    return

                zone_path = ' -> '.join(
                    self.zone_names.get(s['zone_id'], str(s['zone_id']))
                    for s in route)
                print(f'  Route: {zone_path} ({len(route)} segments)')

                self.write_response({
                    'action': 'cross_zone_goto',
                    'status': 'ok',
                    'route': route,
                    'zone_id': zone_id,
                    'seq': seq,
                    # Same reset semantics as the goto branch - when
                    # set, the addon drops any leftover retry state /
                    # stale waypoints before adopting this route.
                    # Without this, an addon-side wider-radius retry
                    # from a previous wander can fire its own goto
                    # mid-cross-zone and wedge the agent on stale
                    # waypoints from the wrong zone.
                    'reset_state': bool(req.get('reset_state')),
                })
            except Exception as e:
                print(f'  Error: {e}')
                import traceback; traceback.print_exc()
                self.write_response({
                    'action': 'cross_zone_goto', 'status': 'error',
                    'message': str(e), 'seq': seq,
                })

        elif action == 'report_obstacle':
            pos = req.get('position', [0, 0, 0])
            if zone_id:
                self.add_obstacle(zone_id, pos[0], pos[1], pos[2])

        elif action == 'clear_cache':
            zone = req.get('zone_id')
            if zone:
                evicted = [k for k in self.meshes if k[0] == zone]
                for k in evicted:
                    del self.meshes[k]
                self.reachability.pop(zone, None)
                if evicted:
                    print(f'Cleared cache for zone {zone} ({len(evicted)} mesh variant(s))')
            else:
                self.meshes.clear()
                self.reachability.clear()
                print('Cleared all caches')
            self.write_response({'status': 'ok', 'action': 'clear_cache',
                                 'zone_id': zone, 'seq': req.get('seq')})

        elif action == 'clear_blocks':
            zone = req.get('zone_id')
            if zone and any(k[0] == zone for k in self.meshes):
                n = sum(navmesh.clear_blocked(mesh)
                        for k, mesh in self.meshes.items() if k[0] == zone)
                print(f'Cleared {n} blocked polys in zone {zone}')
                self.write_response({'status': 'ok', 'action': 'clear_blocks',
                                     'zone_id': zone, 'cleared': n,
                                     'seq': req.get('seq')})
            else:
                self.write_response({'status': 'no_mesh', 'action': 'clear_blocks',
                                     'zone_id': zone, 'seq': req.get('seq')})

    def _issue_command(self, command: str) -> None:
        """Append a single Ashita /command to cmd_inbox.txt for cmdrelay
        to relay. Multi-line - cmdrelay v1.1+ consumes every line per
        poll, so we can queue several without losing any. Best-effort:
        a missing cmdrelay just means the line sits unread, no error."""
        inbox = IPC_DIR / 'cmd_inbox.txt'
        try:
            with open(inbox, 'a') as f:
                f.write(command + '\n')
        except OSError as e:
            print(f'  cmd_inbox write failed: {e}')

    def write_response(self, data):
        # Unique temp filename per call. A shared "<file>.tmp" raced
        # whenever two write_responses fired in quick succession (addon
        # goto + goal_manager dispatch on the same tick): both opened
        # the same .tmp, the first os.replace consumed it, and the
        # second failed with [Errno 2]. tempfile.mkstemp gives each
        # caller its own .tmp.<random> in the same directory; os.replace
        # is still atomic.
        import tempfile
        out = PATH_FILE
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=out.parent, prefix=out.name + '.', suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, out)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def poll(self):
        if not REQUEST_FILE.exists():
            return

        try:
            with open(REQUEST_FILE) as f:
                req = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        req_seq = req.get('seq')
        if req_seq is None or req_seq == self.last_request_mtime:
            return

        self.last_request_mtime = req_seq
        try:
            self.handle_request(req)
        except Exception as e:
            print(f'Error handling request: {e}')
            self.write_response({'status': 'error', 'message': str(e), 'timestamp': time.time()})
        finally:
            # nav_request.json is a one-shot trigger, NOT a state file.
            # Without this delete, an addon-issued goto persists on disk
            # forever - and after an agent_core restart (which resets
            # last_request_mtime to 0) the same stale request gets
            # treated as fresh and replayed.
            try:
                REQUEST_FILE.unlink()
            except OSError:
                pass

    # Minimum gap between failure-triggered replans, to avoid storms
    # if a freshly-planned goal also fails immediately. The next
    # failure inside this window is silently dropped.
    FAILURE_REPLAN_COOLDOWN_S = 30.0

    # Same cooldown but for idle-replans - fired when the goal tree's
    # roots have all completed (success) but the user goal is still
    # active. Fills the gap when the LLM plans only the first leg
    # (e.g. travel) and didn't include the follow-up (engage_nearby).
    IDLE_REPLAN_COOLDOWN_S = 60.0

    def poll_idle_replan(self):
        """If every root in the goal tree is in a terminal state but
        the user goal is non-empty, fire a fresh plan call. The current
        world state (now including completed prior leaves) gives the
        LLM the context to add the next stage.

        Distinct from failure-replan: that one fires on a single failed
        leaf even with other leaves still active. This one only fires
        when there's nothing left to do AND the user still wants more."""
        gm = self.goal_manager
        if gm._active_leaf_id is not None:
            return  # something is still running
        # Empty goals tree: only skip if user_goal is ALSO empty
        # (legitimate "stop everything"). If user_goal has text but
        # the tree is empty, we got stranded - probably a /goals
        # refresh that cleared goals then had its plan call die
        # (process restart, LLM error, etc). Fire a fresh plan so
        # the agent recovers instead of sitting idle forever.
        if not gm.goals.roots:
            ugf_path = self.cfg.paths.user_goal_file()
            try:
                if not ugf_path.exists() or not ugf_path.read_text(
                        encoding='utf-8', errors='replace').strip():
                    return
            except OSError:
                return
            # Fall through to replan with non-empty user_goal.
        else:
            # Tree exists - all roots must be terminal for "idle".
            terminal = ('completed', 'failed', 'abandoned')
            for rid in gm.goals.roots:
                node = gm._node(rid)
                if node is None or node.get('state', 'pending') not in terminal:
                    return
        now = time.time()
        if now - getattr(self, '_last_idle_replan_ts', 0.0) < self.IDLE_REPLAN_COOLDOWN_S:
            return
        ugf = self.cfg.paths.user_goal_file()
        if not ugf.exists():
            return
        try:
            text = ugf.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return
        if not text:
            return
        print(f'[idle_replan] all roots done; replanning against current state')
        self._last_idle_replan_ts = now
        try:
            self.planner.plan(text, self.zone_names)
        except Exception as e:
            print(f'  idle_replan failed: {e}')

    def poll_replan_request(self):
        """Watch <ipc_base>/replan_request.txt - written by `/goals refresh`
        in the goals addon. Presence of the file means "throw away the
        current plan and replan from current state." Also clears any
        idle/failure replan cooldowns so a manual refresh always lands."""
        path = self.cfg.paths.ipc_base / 'replan_request.txt'
        if not path.exists():
            return
        try:
            path.unlink()
        except OSError as e:
            print(f'  replan_request: could not consume {path}: {e}')
            return
        ugf = self.cfg.paths.user_goal_file()
        if not ugf.exists():
            return
        try:
            text = ugf.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return
        if not text:
            print('[replan_request] user_goal empty; nothing to plan')
            return
        # Bypass cooldowns - this came from a deliberate user action.
        self._last_failure_replan_ts = 0.0
        self._last_idle_replan_ts = 0.0
        # Clear the existing tree so the planner produces a fresh plan
        # rather than reasoning around stale active/completed leaves.
        from . import persistence as _persistence
        empty = _persistence.Goals()
        empty.save(self.goal_manager._goals_path)
        self.goal_manager.goals = empty
        self.goal_manager._last_dispatch.clear()
        self.goal_manager._active_leaf_id = None
        # Stop any in-flight farming director so it doesn't keep firing
        # combat commands against the cleared goal.
        if self.farming.is_active():
            self.farming.stop()
        print(f'[replan_request] /goals refresh - replanning')
        try:
            self.planner.plan(text, self.zone_names)
        except Exception as e:
            print(f'  replan_request failed: {e}')

    def poll_failure_replan(self):
        """If the goal manager latched a goal-failed signal since the
        last poll, fire ONE replan with the current user_goal text.
        Triggered by a real "plan stopped working" event - not by mid-
        plan zone changes (which were causing thrash and got removed)."""
        sig = self.goal_manager.consume_failure_signal()
        if sig is None:
            return
        now = time.time()
        if now - getattr(self, '_last_failure_replan_ts', 0.0) < self.FAILURE_REPLAN_COOLDOWN_S:
            print(f'[failure_replan] cooldown active; skipping replan for {sig!r}')
            return
        ugf = self.cfg.paths.user_goal_file()
        if not ugf.exists():
            return
        try:
            text = ugf.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return
        if not text:
            return
        print(f'[failure_replan] leaf {sig!r} failed; replanning')
        self._last_failure_replan_ts = now
        try:
            self.planner.plan(text, self.zone_names)
        except Exception as e:
            print(f'  failure_replan failed: {e}')

    def poll_user_goal_file(self):
        """Watch <repo>/user_goal.txt for changes. Whenever the user saves
        a new instruction we re-plan; if the file is empty (or whitespace
        only) we treat that as "stop everything" - clear the goal tree,
        wipe gambits, and tell the nav addon to stop.

        Mtime tracking is in-memory only. The persistent goal tree on
        disk is the source of truth for what the agent is doing across
        restarts; the file just triggers re-planning when the user
        wants to change direction."""
        ugf = self.cfg.paths.user_goal_file()
        if not ugf.exists():
            return
        try:
            mtime = ugf.stat().st_mtime
        except OSError:
            return
        if mtime <= self._user_goal_mtime:
            return
        self._user_goal_mtime = mtime
        try:
            text = ugf.read_text(encoding='utf-8', errors='replace').strip()
        except OSError as e:
            print(f'  user_goal: read failed: {e}')
            return
        if text:
            print(f'[user_goal] {text!r}')
            self.planner.plan(text, self.zone_names)
        else:
            print('[user_goal] empty - clearing goals (gambits untouched)')
            from . import persistence as _persistence
            empty = _persistence.Goals()
            empty.save(self.goal_manager._goals_path)
            self.goal_manager.goals = empty
            self.goal_manager._last_dispatch.clear()
            self.goal_manager._active_leaf_id = None
            # Gambits intentionally NOT wiped - they are context-bound
            # combat reactions (per job/subjob/party state) that should
            # outlive any single user goal. The agent itself has to ask
            # for a clear if it wants one.
            self._issue_command('/nav stop')

    VERSION = '.9'

    def run(self):
        print(f'Nav server v{self.VERSION} started. Watching {REQUEST_FILE}')
        print(f'Collision data: {COLLISION_DIR}')
        print(f'User goal file: {self.cfg.paths.user_goal_file()}')
        print(f'Character: {self.cfg.character}  goals: {len(self.goal_manager.goals.nodes)} nodes / '
              f'{len(self.goal_manager.goals.roots)} root(s)')
        # Goal-tick cadence: every Nth poll, not every poll. Polls are
        # 100ms; 5 polls = 0.5s tick rate, which matches the architecture
        # doc's Tier-2 budget.
        ticks_per_goal_run = 5
        tick_counter = 0
        # Liveness latch - log once on transitions so the operator can
        # see "client closed" / "client back" without scrolling through
        # silent gaps.
        _addons_were_live: bool | None = None
        try:
            while True:
                self.poll()
                self.poll_user_goal_file()
                tick_counter += 1
                if tick_counter >= ticks_per_goal_run:
                    tick_counter = 0
                    # Liveness gate. If the addons aren't publishing
                    # state (combat.json mtime > SNAPSHOT_STALE_S),
                    # skip every tick that consumes/acts on snapshots.
                    # Without this gate, goal manager + farming director
                    # spin forever against frozen snapshots, dispatching
                    # gotos that no addon reads. user_goal poll runs
                    # regardless - text edits are safe, and any plan it
                    # produces just sits on disk until the client comes
                    # back. nav_request.json poll (in self.poll above)
                    # also runs regardless, since that's the addon-side
                    # arrival channel.
                    combat_path = self.cfg.paths.state_dir(self.cfg.character) / 'combat.json'
                    addons_live = self._is_state_fresh(combat_path)
                    if _addons_were_live is None:
                        _addons_were_live = addons_live
                    elif addons_live != _addons_were_live:
                        if addons_live:
                            print('[liveness] addons publishing again; resuming ticks')
                        else:
                            print('[liveness] addon snapshots stale; idling state machines '
                                  '(client likely closed)')
                        _addons_were_live = addons_live
                    if not addons_live:
                        time.sleep(POLL_INTERVAL)
                        continue
                    try:
                        self.goal_manager.tick()
                    except Exception as e:
                        print(f'goal_manager tick error: {e}')
                    try:
                        self.poll_gambit_context()
                    except Exception as e:
                        print(f'gambit_context error: {e}')
                    try:
                        self.poll_replan_request()
                    except Exception as e:
                        print(f'replan_request error: {e}')
                    try:
                        self.poll_failure_replan()
                    except Exception as e:
                        print(f'failure_replan error: {e}')
                    try:
                        self.poll_idle_replan()
                    except Exception as e:
                        print(f'idle_replan error: {e}')
                    try:
                        self.chat.tick()
                    except Exception as e:
                        print(f'chat tick error: {e}')
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print('\nShutting down.')


if __name__ == '__main__':
    server = NavServer()

    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        zone = int(sys.argv[2]) if len(sys.argv) > 2 else 107
        sx, sy, sz = 221.0, -434.0, 59.8
        tx, ty, tz = 350.0, -400.0, 20.0
        if len(sys.argv) > 4:
            tx, ty = float(sys.argv[3]), float(sys.argv[4])
            tz = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

        waypoints = server.find_path(zone, (sx, sy, sz), (tx, ty, tz))
        print(f'{len(waypoints)} waypoints:')
        for i, w in enumerate(waypoints):
            print(f'  [{i}] ({w[0]}, {w[1]}, {w[2]})')
    else:
        server.run()
