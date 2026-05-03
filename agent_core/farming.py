"""Farming director - drives the kill loop for a `farm` goal.

A `farm` goal is a leaf in the goal tree:

    {
      "id": "...",
      "type": "farm",
      "target_name": "Huge Hornet",                  # mob to engage
      "stop_when":   {"kill_count": 5},              # MVP stop condition
      "rest_hp_pct": 70                              # rest below this
    }

The director runs as a per-tick state machine while the goal_manager has
a `farm` leaf active. It coordinates three things:

    1. State changes (locate -> approach -> acquire -> engage -> killed ->
       rest -> loop) gated on what the combat addon publishes to
       state/<char>/combat.json AND what the nav addon publishes to
       nav_status.json (player position).
    2. Movement requests (cross_zone_goto with target_pos) via the
       same dispatch path the goal_manager uses for travel/goto.
    3. Side effects (/ta, /follow, /attack, /heal) via the cmd_inbox
       channel that cmdrelay drains in-game.

Phase 3c-min scope: single zone, single target name, kill-count stop
condition, HP-only rest. Respawn-aware target picking, item drops,
cross-zone relocation land in later passes.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config as _config
from . import coverage as _coverage
from . import echo as _echo
from . import entities as _entities
from . import events as _events
from . import fight_history as _fight_history


# How long /ta has to bring the requested mob into the combat addon's
# `target` slot before we give up and retry. Most mobs respond instantly;
# 3s is a generous bound that survives a laggy frame.
TARGET_LOCK_TIMEOUT_S = 3.0

# After issuing /heal, how long to sit before we check HP again. Sitting
# regenerates HP roughly every tick, so even 5s is plenty between checks.
REST_CHECK_INTERVAL_S = 5.0
# Min gap between /heal toggles. /heal is a TOGGLE in FFXI — sending it
# twice in quick succession sits the player down then immediately stands
# them back up. We rate-limit at this addon to make /heal idempotent
# from the orchestrator's perspective: if multiple decision paths all
# fire /heal at once (engage_judge rest + rest_judge rest + resting
# state re-arm), only the first lands and the rest are silently dropped.
HEAL_TOGGLE_GAP_S = 6.0

# Hard ceiling on how long we'll spend in any single state. If something
# pathological happens (target unkillable, HP not regenerating, etc.) we
# bail to LOCATE rather than stay stuck forever.
STATE_TIMEOUT_S = 120.0

# How close the player has to be to the spawn position before we
# consider ourselves "near enough to /ta" - the named entity has to be
# within client detection range, which is roughly 20 yalms in FFXI.
APPROACH_RADIUS_Y = 18.0
# Single-phase engage: nav-walk to melee range, THEN /attack on.
# Why not engage earlier:
#   - nav.lua moves the player by setting IAutoFollow:SetIsAutoRunning(1)
#     plus delta vectors. nav and autorun are the SAME mechanism.
#   - /attack on triggers FFXI's auto-pursuit-toward-target while engaged.
#     This overrides nav's delta vectors - player walks straight at the
#     target instead of along the planned path, getting wedged on
#     fences / walls / geometry nav was trying to route around.
# So: stay un-engaged while nav is doing the approach. Only fire
# /attack on once we're already at melee range and nav has stopped.
MELEE_DISTANCE_Y = 3.0
# Retry /attack on every N seconds until snap.engaged flips true.
# FFXI's server rejects /attack on with "You must wait longer to
# perform that action" if a prior auto-attack swing's weapon delay
# hasn't elapsed. Most weapons are 2-5s; can be longer with slow.
# Re-firing every 2s lets us land the engage as soon as the delay
# clears, without spamming the chat or thrashing the lock.
ATTACK_RETRY_S = 2.0
# Re-dispatch the approach goto at most this often. Mobs in FFXI wander
# and flee - locking-and-walking once won't track a moving target. 2s
# strikes a balance between responsiveness and goto-spam (the nav
# addon's path build is cheap but not free).
APPROACH_REDISPATCH_S = 2.0
# Min distance the target must have moved since the last approach goto
# before we re-dispatch. Below this, the existing path is "good enough"
# - re-dispatching for sub-3y wobble just thrashes the path-following.
APPROACH_REDISPATCH_DELTA_Y = 3.0

# Hand the engage tick a margin before re-issuing /attack on. Only used
# when we've never seen engaged=True for this fight - once we observe it,
# we stop retrying entirely (autoattack runs to kill on its own). 5s is
# slow enough to avoid a visible "agent spamming commands" pattern in
# the chat log when /attack on is rejected (mid-action, recovering, etc.).
ATTACK_RETRY_INTERVAL_S = 5.0

# Acquire-specific timeout. If /ta hasn't locked the named target after
# this many seconds, the spawn record we headed to is stale (mob despawned,
# wandered out of range, line-of-sight blocked, etc.). Bounce to locate
# WITHOUT crediting a kill, and blacklist that record for a while so we
# pick a different spawn next time.
ACQUIRE_TIMEOUT_S = 20.0
SPAWN_BLACKLIST_S = 90.0

# engage_nearby wander parameters. When /ta finds nothing, the director
# walks to the next exploration target picked by the coverage map (an
# unexplored cell adjacent to ground we've already visited - frontier
# expansion). engage_nearby fails only when the coverage map says the
# zone is fully covered at the current Vana'diel hour.
WANDER_TIMEOUT_S = 30.0  # max time spent walking to a single target cell
# Fallback random-wander radius for the case where coverage data isn't
# available yet (zone has zero recorded cells - first visit, agent just
# zoned in). Used until at least one cell is on disk, then frontier
# expansion takes over.
WANDER_RADIUS_Y = 50.0

# /check response codes the combat addon stores in nearby_enemies
# entries (`check_type`). Mapping mirrors the third-party `checker`
# addon's `types` table.
#   0x40 too weak (skip - no XP)
#   0x41 incredibly easy prey (engage)
#   0x42 easy prey                (engage)
#   0x43 decent challenge         (engage)
#   0x44 even match               (engage)
#   0x45 tough                    (skip - risky for solo)
#   0x46 very tough               (skip)
#   0x47 incredibly tough         (skip)
#   0xF9 impossible to gauge      (skip - content way out of band)
CHECK_TYPE_ENGAGEABLE = {0x41, 0x42, 0x43, 0x44}
CHECK_TYPE_TOO_TOUGH  = {0x45, 0x46, 0x47, 0xF9}
# Too-weak (0x40) is not in either set: we don't engage (no XP) but
# we also don't blacklist - could be useful for testing or low-level
# quests later. Treat as "skip but not unreachable."

# Max time to wait for a /check response after issuing /ta + /check
# before assuming the check failed (target despawned, etc) and
# blacklisting the candidate.
CHECK_RESPONSE_TIMEOUT_S = 5.0
# How close we need to be to the cell center to count as "arrived" and
# transition back to acquire. 5y is small relative to the 30y cell so
# we genuinely walk into the cell rather than peek at its edge.
WANDER_ARRIVAL_RADIUS_Y = 5.0
# Hard ceiling on consecutive wander attempts when coverage termination
# isn't triggering - prevents an infinite loop if e.g. the addon stops
# updating the coverage file. Real termination should always come from
# coverage.is_fully_covered before this fires.
MAX_WANDERS_SAFETY_CAP = 200
# Hardcoded safety floor: never engage below this HP%, regardless of
# the LLM judge's verdict. Bad JSON parses from a wobbly local model
# drop us into the hardcoded fallback, which previously engaged
# anything check_type 0x41-0x44 with no HP check - saw the agent fight
# at 1% HP because of this. The floor forces 'rest' instead.
MIN_ENGAGE_HP_PCT = 25.0


@dataclass
class _Snapshot:
    """The combat + nav bits the director consults each tick. All fields
    are nullable; the director treats nil as 'no info, retry'."""
    self_hp_pct:   float | None
    self_hp:       int   | None     # raw - death check (hp_pct unreliable when hp_max=0)
    self_status:   int   | None     # 0 idle, 1 engaged, 2 dead, 3 dead/zoning, 33 healing
    self_lvl:      int   | None     # main-job level - used to invalidate stale check_type
    target_name:   str   | None
    target_alive:  bool  | None
    target_hp_pct: float | None
    engaged:       bool
    # Defaulted fields - additions here don't break existing _Snapshot
    # construction sites that don't pass them. Keep all new optional
    # state below this line to avoid TypeError on partial constructors
    # (the planner/goal_manager build minimal _Snapshots from nav data
    # and don't know about combat-only fields).
    self_mp_pct:   float | None = None
    self_main_job: int   | None = None  # FFXI job ID; resolve via gambits.JOB_NAMES
    self_sub_job:  int   | None = None
    self_sub_lvl:  int   | None = None
    target_distance: float | None = None
    target_x:      float | None = None
    target_y:      float | None = None
    target_z:      float | None = None
    # True when the mob's ClaimServerId matches our own player's
    # ServerId - this is the authoritative "I own this fight" flag,
    # not `engaged`. /attack on can fire and the engaged status flip
    # before the server actually claims the mob for us; navigating
    # until claim is ours handles the gap.
    target_claimed_by_us: bool   = False
    menu_open:     bool          = False
    # Nav-side fields used to decide arrival at the spawn position.
    zone_id:       int   | None = None
    x:             float | None = None
    y:             float | None = None
    z:             float | None = None
    moving:        bool         = False
    # List of {name, distance, hp_pct, server_id, status, level?,
    # check_type?, conditions?} dicts for unclaimed live mobs in /ta
    # range. engage_nearby acquires by name from this list (FFXI has
    # no `/ta <enemy>` placeholder; the prior bogus command silently
    # no-op'd).
    nearby_enemies: list[dict] = field(default_factory=list)
    # ServerId of currently locked target (for cross-ref into the
    # nearby_enemies entry -> look up its check_type by exact spawn,
    # not just by name which can collide between adjacent same-name
    # mobs). None when nothing is targeted.
    target_server_id: int | None = None


class FarmingDirector:
    """One director per orchestrator. start() arms it for a directive;
    tick() advances the state machine."""

    STATES = ('idle', 'locate', 'approach', 'acquire', 'engage',
              'killed', 'judging_rest', 'resting', 'wander',
              'completed', 'failed')

    def __init__(
        self,
        cfg: _config.Config,
        snapshot_provider: Callable[[], _Snapshot],
        issue_command: Callable[[str], None],
        nav_snapshot_provider: Callable[[], _Snapshot] | None = None,
        dispatch_goto: Callable[[dict[str, Any]], None] | None = None,
        nearest_reachable: Callable[[int, tuple[float, float]], tuple[float, float] | None] | None = None,
        engage_judge: Any = None,
        rest_judge: Any = None,
    ):
        self.cfg = cfg
        self._combat_snapshot = snapshot_provider
        self._issue = issue_command
        # LLM judge for engage/skip/rest decisions. None falls back to
        # the hardcoded CHECK_TYPE_* sets - same behaviour as before.
        self._engage_judge = engage_judge
        # LLM judge for post-kill rest/continue decisions. None falls
        # back to the directive's hardcoded rest_hp_pct threshold.
        self._rest_judge = rest_judge
        # Active rest-judgment request id, set on entry to judging_rest
        # state and cleared on exit. None means no in-flight request.
        self._rest_judge_rid: int | None = None
        # True once _tick_killed has done its one-shot bookkeeping
        # (kill_count++, /attack off, /follow off, fight_history record).
        # Reset on every state entry so re-entering killed re-records.
        self._kill_recorded: bool = False
        # (target_x, target_y) the last approach goto was dispatched to.
        # Compared against current target position to decide whether to
        # re-dispatch (mob wandered too far) - without this, the engage
        # state would re-route every tick at melee speed.
        self._last_approach_target: tuple[float, float] | None = None
        # True once we've issued /nav stop because we're in melee range.
        # Resets when the mob moves out of melee, so the engage state
        # re-dispatches a goto to chase. Without this, /nav stop would
        # spam every tick while standing in melee.
        self._nav_stopped: bool = False
        # Player HP% at the moment we entered the engage state. Used to
        # compute damage_taken_pct on kill/death - a much more stable
        # signal than hp_remaining_pct because it isolates damage from
        # this specific fight rather than rolling resting HP into it.
        self._engage_start_hp_pct: float | None = None
        # check_type the locked mob had at engage entry (relative to
        # player level at that moment). Recorded into fight_history on
        # kill/death so we can aggregate "deaths to Even-Match mobs"
        # at this level via tier_summary().
        self._engage_check_type: int | None = None
        # Wall-clock of the last /heal we issued. Used by _start_resting
        # to drop redundant toggle attempts within HEAL_TOGGLE_GAP_S.
        self._last_heal_at: float = 0.0
        # Active engage_judge request id (None when no call is in
        # flight). Cleared in _enter('acquire') so each acquire cycle
        # fires a fresh judgment, and on verdict consume so a 'skip'
        # bouncing back to acquire fires a fresh call for the next
        # candidate. No cross-tick caching - "calls are free" per the
        # local LLM tier.
        self._engage_judge_rid: int | None = None
        # Optional injectables - without them, locate/approach degrade
        # gracefully into the old "assume we're near the spawn" behaviour.
        # Keeps tests + tools that drive the director directly viable.
        self._nav_snapshot = nav_snapshot_provider
        # Navmesh reachability check (zone_id, target_xy) -> nearest
        # walkable point or None. Used by wander to filter unreachable
        # cells (walls, water, off-mesh nooks) before dispatching the
        # goto. None disables the check, falling back to dispatching
        # blind and learning unreachability via the 30s timeout.
        self._nearest_reachable = nearest_reachable
        self._dispatch = dispatch_goto
        self.state: str = 'idle'
        self.directive: dict[str, Any] | None = None
        self.kills: int = 0
        self._state_entered_at: float = 0.0
        self._last_action_ts: float = 0.0
        # Spawn we're heading to right now. Refreshed on every locate so
        # we don't keep walking to a stale (or already-killed) record.
        self._target_spawn: dict[str, Any] | None = None
        # server_id -> wall-clock ts when /ta last failed for that spawn.
        # locate() skips entries newer than SPAWN_BLACKLIST_S. Without
        # this, a stale record at the closest position would be picked
        # forever, /ta would fail forever, and the agent would never
        # try a different spawn. Reset on directive change (start()).
        self._spawn_blacklist: dict[Any, float] = {}
        # True once we observe `engaged=True` during the current engage
        # state. Without this guard, target_name flipping to None (from
        # /ta dropping, mob despawning, the player zoning) gets counted
        # as a kill, because the engage tick can't tell "we never hit
        # it" from "the mob is dead." Reset on every state entry.
        self._observed_engaged: bool = False
        # True once we've issued /follow for the current target. /follow
        # is sticky in FFXI - once on, it persists until cancelled, the
        # target dies, or you zone. Re-issuing pollutes the chat log
        # without changing anything. Reset on every state entry.
        self._follow_issued: bool = False
        # Wall-clock of the last /attack on we issued during this
        # engage. Compared to ATTACK_RETRY_S to know when to retry -
        # FFXI rejects /attack on if a prior weapon delay is ticking,
        # so we fire repeatedly until snap.engaged flips true. 0.0
        # means "never issued in this engage cycle". Reset on engage
        # state entry.
        self._last_attack_attempt: float = 0.0
        # Last main-job level we observed. When this changes, mob
        # check_type assessments become stale (FFXI's /check is
        # relative to your level - what was "tough" at lvl 2 might
        # be "easy prey" at lvl 5). Used to wipe spawn_blacklist on
        # level-up so previously-skipped mobs become engageable again.
        self._last_known_level: int | None = None
        # engage_nearby mode: the directive doesn't name a target, so
        # we latch whatever name we acquired when /ta <enemy> succeeded.
        # Used by engage state's gone/dead detection. Reset on entry to
        # acquire so a fresh /ta cycle starts cleanly.
        self._acquired_name: str | None = None
        # engage_nearby wander counter. Each /ta-found-nothing cycle
        # bumps it; once it hits MAX_WANDERS_BEFORE_FAIL we give up.
        # Reset on directive change in start().
        self._wander_count: int = 0
        # Last server_id we issued /ta for in engage_nearby acquire.
        # Used to dedupe /ta spam - only re-issue when picking a
        # different mob. Tracked by sid (not name) because multiple
        # same-name mobs (e.g. 3 "Wild Rabbit"s) need to be /ta'd
        # individually as the blacklist rotates them out.
        self._last_acquire_sid: Any = None
        # sid -> wall-clock when we issued /check on it. Used to detect
        # /check timeouts (no ct response within CHECK_RESPONSE_TIMEOUT_S
        # means the entity isn't a real mob - we tell combat.lua to
        # blacklist the sid). Cleaned on zone change via clear in the
        # tick's level-up branch and on directive change in start().
        self._check_issued_at: dict[int, float] = {}
        # Set of coverage cell-keys ("cx:cy") that this directive's
        # wander has tried to reach but failed (timeout / partial
        # path). Excluded from subsequent nearest_target picks so the
        # agent doesn't keep retrying an unreachable corner. Reset
        # on directive change in start().
        self._wander_cell_blacklist: set[str] = set()
        # Cell-key the current wander attempt targets. Saved when we
        # dispatch so we can blacklist it on timeout.
        self._wander_cell_key: str | None = None

    # ---- public lifecycle --------------------------------------------

    def start(self, directive: dict[str, Any]) -> None:
        """Arm the director for a new farm directive. Idempotent if the
        same directive is already active (e.g. goal_manager re-dispatches)."""
        if self.state not in ('idle', 'completed', 'failed'):
            if self.directive == directive:
                return
        self.directive = directive
        self.kills = 0
        self._target_spawn = None
        # Each new directive gets a fresh blacklist - old failures are
        # not necessarily relevant to a new mob name / kill count.
        self._spawn_blacklist = {}
        self._wander_count = 0
        self._wander_cell_blacklist = set()
        self._wander_cell_key = None
        # Enter locate first - pick a known spawn position from the nav
        # addon's entity records before doing anything else. If we have
        # no nav snapshot or no entity records, locate falls through to
        # acquire so a manually-positioned agent still works.
        self._enter('locate')
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='farming',
            type_='farm_started',
            directive=directive,
        )

    def stop(self) -> None:
        """Tell the agent to stop attacking and return to idle. Called
        when the leaf's state changes externally (e.g. user clears goals)."""
        if self.state in ('engage', 'killed'):
            self._issue('/attack off')
        self._enter('idle')

    def is_done(self) -> bool:
        return self.state in ('completed', 'failed')

    def is_active(self) -> bool:
        return self.state not in ('idle', 'completed', 'failed')

    def _snapshot(self) -> _Snapshot:
        """Compose the combat snapshot with nav fields. The combat
        provider is mandatory; nav is optional - without it we can't
        pick a spawn or detect arrival, so locate/approach short-circuit
        to acquire and the director behaves like the pre-Phase-3c-loc
        version."""
        snap = self._combat_snapshot()
        if self._nav_snapshot is None:
            return snap
        nav = self._nav_snapshot()
        return _Snapshot(
            self_hp_pct=snap.self_hp_pct,
            self_hp=snap.self_hp,
            self_status=snap.self_status,
            self_lvl=snap.self_lvl,
            target_name=snap.target_name,
            target_alive=snap.target_alive,
            target_hp_pct=snap.target_hp_pct,
            target_distance=snap.target_distance,
            target_x=snap.target_x,
            target_y=snap.target_y,
            target_z=snap.target_z,
            target_server_id=snap.target_server_id,
            target_claimed_by_us=snap.target_claimed_by_us,
            engaged=snap.engaged,
            self_mp_pct=snap.self_mp_pct,
            self_main_job=snap.self_main_job,
            self_sub_job=snap.self_sub_job,
            self_sub_lvl=snap.self_sub_lvl,
            menu_open=snap.menu_open,
            nearby_enemies=snap.nearby_enemies,
            zone_id=getattr(nav, 'zone_id', None),
            x=getattr(nav, 'x', None),
            y=getattr(nav, 'y', None),
            z=getattr(nav, 'z', None),
            moving=getattr(nav, 'moving', False),
        )

    # ---- state machine -----------------------------------------------

    def _enter(self, new_state: str) -> None:
        if new_state == self.state:
            return
        # Leaving engage - cancel any in-flight nav approach goto. We
        # don't issue /combat stop here (or anywhere): with the nav-
        # based approach we never enable autorun, so there's nothing
        # to clear. /nav stop is enough.
        if self.state == 'engage' and new_state != 'engage':
            self._issue('/nav stop')
        old_state = self.state
        self.state = new_state
        self._state_entered_at = time.time()
        # Reset action throttle on state entry so the first tick of the
        # new state fires its first command immediately, not after
        # whatever cooldown the previous state was halfway through.
        self._last_action_ts = 0.0
        # Engagement observation is per-engage-state: each new engage
        # has to actually witness combat before a kill is creditable.
        self._observed_engaged = False
        # /follow is sticky per-target. New state = new target = need
        # to re-issue /follow once on entry (not every tick).
        self._follow_issued = False
        # Reset attack-attempt timestamp so the engage state's first
        # tick can fire /attack on immediately.
        self._last_attack_attempt = 0.0
        # Same for the /nav stop latch - fresh engage starts with us
        # not yet in melee range, so nav should be free to dispatch
        # an approach goto.
        self._nav_stopped = False
        # Acquired-name latch is per-acquire-cycle: clearing on entry to
        # acquire ensures the next /ta cycle starts fresh and we don't
        # mistakenly compare against the previous mob's name.
        if new_state == 'acquire':
            self._acquired_name = None
            self._last_acquire_sid = None
            # Drop any in-flight engage-judge rid - the next acquire's
            # locked target deserves a fresh judgment.
            if (self._engage_judge_rid is not None
                    and self._engage_judge is not None):
                self._engage_judge.discard(self._engage_judge_rid)
            self._engage_judge_rid = None
        # killed bookkeeping is one-shot per kill - clear the latch on
        # entry so re-entering 'killed' (e.g. after an aborted rest)
        # would record again. In practice we only enter killed once per
        # kill cycle, so this is defensive.
        if new_state == 'killed':
            self._kill_recorded = False
        # Reset engage-start HP latch only when entering engage - so the
        # next engage tick captures a fresh starting HP. _tick_killed
        # reads the latch before clear, so we don't lose it on the
        # engage->killed transition.
        if new_state == 'engage':
            self._engage_start_hp_pct = None
            self._engage_check_type = None
        # Discard any leftover rest-judgment rid when leaving the
        # judging_rest state - without this, a stale rid could resolve
        # later and confuse a subsequent post-kill cycle.
        if new_state != 'judging_rest' and self._rest_judge_rid is not None:
            if self._rest_judge is not None:
                self._rest_judge.discard(self._rest_judge_rid)
            self._rest_judge_rid = None
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='farming',
            type_='farm_state',
            state=new_state,
            kills=self.kills,
        )

    def _stop_when_satisfied(self) -> bool:
        """Did the leaf's stop condition fire? Supports either:

          stop_when.target_level - completes once snap.self_lvl >= N.
            The right choice for "reach level X" user goals - a fixed
            kill_count almost always under- or over-shoots the actual
            XP needed, leading to either hand-offs back to the planner
            mid-level or wasted kills past the target.

          stop_when.kill_count - completes after N kills, regardless
            of XP gained. Use for narrow tasks like "kill 5 Yagudo
            for the quest" where XP is incidental.

        If both are present, EITHER one firing completes the leaf
        (target_level is the primary; kill_count is a safety cap so
        a stalled XP curve doesn't loop forever). If neither is
        present, the leaf never auto-completes - the planner can do
        that on purpose for indefinite engage_nearby goals.
        """
        d = self.directive or {}
        sw = d.get('stop_when') or {}
        tl = sw.get('target_level')
        if isinstance(tl, (int, float)) and self._last_known_level is not None \
                and self._last_known_level >= int(tl):
            return True
        kc = sw.get('kill_count')
        if isinstance(kc, (int, float)) and self.kills >= kc:
            return True
        return False

    def tick(self) -> None:
        if self.directive is None or not self.is_active():
            return
        snap = self._snapshot()
        now = time.time()

        # Level-change handling: /check is relative to current level,
        # so anything we previously skipped as "too tough" might now be
        # within reach. Wipe spawn_blacklist + cell-blacklist so the
        # acquire picker re-evaluates everything we previously rejected.
        # (combat.lua wipes its own entity_checks cache on the same
        # signal, so the next acquire's check_type lookups come back
        # fresh from /check rather than stale-cached.)
        if snap.self_lvl is not None:
            if self._last_known_level is None:
                self._last_known_level = snap.self_lvl
            elif snap.self_lvl != self._last_known_level:
                old = self._last_known_level
                self._last_known_level = snap.self_lvl
                if self._spawn_blacklist or self._wander_cell_blacklist:
                    print(f'  farming: level {old} -> {snap.self_lvl}; '
                          f'clearing {len(self._spawn_blacklist)} spawn + '
                          f'{len(self._wander_cell_blacklist)} cell blacklist '
                          f'(checks may now be different)')
                self._spawn_blacklist = {}
                self._wander_cell_blacklist = set()
                # Engage-judge cache is keyed by (name, player_lvl); its
                # entries from the prior level are now stale. Clear so
                # the judge re-evaluates on the next encounter.
                if self._engage_judge is not None:
                    self._engage_judge.invalidate()

        # Death check - runs first, beats every other transition. If the
        # player is dead we MUST stop firing combat commands. Without
        # this, /attack on retries spam the chat log forever, and the
        # acquire/engage timeouts try to re-engage a dead character.
        #
        # Status alone is the authoritative death signal:
        #   2 = dead-pending-menu, 3 = dead/lying down post-menu.
        # The old `(hp==0 AND status NOT in {None,1})` backup was meant
        # to catch hp=0 cases where hp_max=0 made hp_pct unreliable, but
        # it also fired during the post-zone load-in window where the
        # addon momentarily publishes (hp=0, status=0) while the player
        # entity hasn't fully spawned yet. That false-positive marked
        # the farm goal as failed the instant we zoned in. Restricting
        # the backup to "hp=0 AND status is also a death code" keeps
        # the hp_max=0 case covered without the load-in false positive.
        is_dead = (
            (snap.self_status in (2, 3))
            or (snap.self_hp == 0 and snap.self_status in (2, 3))
        )
        if is_dead:
            if self.state != 'failed':
                print('  farming: player is dead - aborting farm')
                self._issue('/attack off')
                # Attribute the death to whoever we were last engaging.
                # In engage_nearby that's _acquired_name; in named farm
                # it's directive.target_name. Either is the right key
                # for the per-mob death prior the LLM judge consults.
                died_to = ((self.directive or {}).get('target_name')
                           or self._acquired_name)
                if died_to and snap.zone_id is not None:
                    # Death damage = engage_start_hp (we lost all of it).
                    # If we don't have the latch, the record_death
                    # default of 100% kicks in.
                    dmg = self._engage_start_hp_pct
                    _fight_history.record_death(
                        self.cfg, snap.zone_id, died_to,
                        player_level=snap.self_lvl,
                        damage_taken_pct=dmg,
                        check_type=self._engage_check_type,
                    )
                _events.append(
                    self.cfg.paths.events_file(),
                    character=self.cfg.character,
                    source='farming',
                    type_='farm_death',
                    state_at_death=self.state,
                    kills_at_death=self.kills,
                    died_to=died_to,
                )
                # First-person reflection echo. Templated rather than
                # LLM-generated because death is a moment for fast
                # observability, not a 10s deliberative call. Uses the
                # mob name + (if known) prior death history to give the
                # echo some weight.
                if died_to:
                    prior_deaths = 0
                    try:
                        if snap.zone_id is not None and snap.self_lvl is not None:
                            h = _fight_history.summary(
                                self.cfg, snap.zone_id, died_to, snap.self_lvl,
                            )
                            prior_deaths = int(h.get('death_count') or 0)
                    except Exception:
                        pass
                    if prior_deaths > 1:  # this death is now >=2 incl current
                        msg = (f"Killed by {died_to} again. "
                               f"They keep wrecking me at this level.")
                    else:
                        msg = (f"Killed by {died_to}. "
                               f"Going to be more careful with these.")
                else:
                    msg = "Died. Not sure exactly what got me."
                _echo.to_chat(self.cfg, 'died', msg)
                self._enter('failed')
            return

        # Per-state timeout - never wedged for more than STATE_TIMEOUT_S.
        # Resting is exempt because /heal up time is itself bounded by
        # the rest tick's HP check; killed is exempt because it's a
        # one-shot bookkeeping state.
        if now - self._state_entered_at > STATE_TIMEOUT_S \
                and self.state not in ('resting', 'killed'):
            print(f'  farming: state {self.state} timed out - bouncing to locate')
            self._enter('locate')

        if self.state == 'locate':
            self._tick_locate(snap, now)
        elif self.state == 'approach':
            self._tick_approach(snap, now)
        elif self.state == 'acquire':
            self._tick_acquire(snap, now)
        elif self.state == 'engage':
            self._tick_engage(snap, now)
        elif self.state == 'killed':
            self._tick_killed(snap, now)
        elif self.state == 'judging_rest':
            self._tick_judging_rest(snap, now)
        elif self.state == 'resting':
            self._tick_resting(snap, now)
        elif self.state == 'wander':
            self._tick_wander(snap, now)

    def _tick_locate(self, snap: _Snapshot, now: float) -> None:
        """Pick a spawn position to head toward. We prefer the closest
        known record in the player's current zone - the agent is already
        IN the zone (the planner emits a travel leaf before farm), so
        the only legwork left is closing the in-zone distance."""
        target_name = (self.directive or {}).get('target_name')
        # engage_nearby mode: no named target, just whatever's around.
        # Skip the records lookup and let acquire issue /ta <enemy> to
        # grab the closest visible mob. The cataloging side-effect of
        # entities.lua scanning will fill in the per-zone records as
        # we kill things, so subsequent farm goals can name them.
        if not target_name:
            self._enter('acquire')
            return
        # No nav snapshot - fall back to the bare-/ta loop. Useful for
        # tests or when the agent has been hand-positioned next to the
        # spawn.
        if snap.x is None or snap.zone_id is None:
            self._enter('acquire')
            return
        # Pull every candidate, sort by distance, skip any whose
        # server_id is on a fresh blacklist entry. The first survivor
        # is our spawn. If everything's blacklisted we fall back to the
        # globally closest record (better than nothing - worst case the
        # acquire timeout will re-fail it and the blacklist will roll
        # over once SPAWN_BLACKLIST_S elapses).
        all_candidates = _entities.find_mobs(self.cfg, snap.zone_id, target_name)
        if not all_candidates:
            print(f'  farming: no recorded "{target_name}" in zone {snap.zone_id}; failing')
            self._enter('failed')
            return
        px, py = snap.x, snap.y or 0.0
        all_candidates.sort(
            key=lambda r: (r.get('center_x', 0) - px) ** 2 + (r.get('center_y', 0) - py) ** 2
        )
        cutoff = now - SPAWN_BLACKLIST_S
        spawn = None
        for r in all_candidates:
            sid = r.get('server_id')
            if self._spawn_blacklist.get(sid, 0.0) > cutoff:
                continue
            spawn = r
            break
        if spawn is None:
            # All known spawns are blacklisted - pick the closest as a
            # last-resort retry. Better to walk back to a place we know
            # mobs spawn than spin in idle.
            spawn = all_candidates[0]
            print(f'  farming: all {len(all_candidates)} spawns blacklisted; '
                  f'retrying closest at ({spawn["center_x"]:.0f},{spawn["center_y"]:.0f})')
        self._target_spawn = spawn
        # If we're already within the named entity's client-detection
        # range, skip approach - /ta will work right now.
        dx = (spawn['center_x']) - (snap.x or 0.0)
        dy = (spawn['center_y']) - (snap.y or 0.0)
        dist = (dx * dx + dy * dy) ** 0.5
        print(f'  farming: heading to {spawn["name"]} at '
              f'({spawn["center_x"]:.0f},{spawn["center_y"]:.0f}) '
              f'- {dist:.0f}y away')
        if dist <= APPROACH_RADIUS_Y:
            self._enter('acquire')
            return
        self._enter('approach')

    def _tick_approach(self, snap: _Snapshot, now: float) -> None:
        """Walk to the spawn position. Issues a goto once on entry; the
        nav addon owns the path-following retry/wider-radius/random-walk
        chain and we just watch for arrival."""
        spawn = self._target_spawn
        if spawn is None or snap.x is None:
            # Lost the spawn record (started without one) - re-locate.
            self._enter('locate')
            return
        sx, sy = spawn['center_x'], spawn['center_y']
        dx = sx - snap.x
        dy = sy - (snap.y or 0.0)
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= APPROACH_RADIUS_Y:
            # Arrived (or close enough). Stop the nav addon if it's
            # still threading the last waypoint, then try to /ta.
            self._issue('/nav stop')
            self._enter('acquire')
            return
        # Issue the goto exactly once on entry. Re-issuing every tick
        # would keep wp_idx=1 and make the agent walk only the first
        # waypoint of an N-waypoint path forever. nav already has its
        # own re-dispatch ladder for stuck/no-path cases.
        if now - self._last_action_ts < 1.0:
            return
        if self._dispatch is None:
            # No goto channel; can't move. Fall back to /ta and hope
            # the player is near enough. Same compatibility path as
            # the bare-snapshot case above.
            self._enter('acquire')
            return
        if self._last_action_ts == 0.0:  # first time entering approach
            self._dispatch({
                'action':    'goto',
                'zone_id':   snap.zone_id,
                'player':    [snap.x, snap.y or 0.0, snap.z or 0.0],
                'target':    [sx, sy, spawn.get('center_z', 0.0)],
                'seq':       int(now * 1000),
            })
            self._last_action_ts = now

    # ------------------------------------------------------------------
    # Engage-judge helpers
    #
    # The judge is consulted with a locked candidate's check_type filled
    # in. Callers get back one of:
    #   'engage'  - proceed to engage state
    #   'skip'    - blacklist sid, pick next candidate
    #   'pending' - request fired, hold acquire until cache populates
    #   'fallback'- judge unavailable; caller applies CHECK_TYPE_* sets
    # ------------------------------------------------------------------
    def _read_user_goal_text(self) -> str:
        try:
            p = self.cfg.paths.user_goal_file()
            if p.exists():
                return p.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            pass
        return ''

    def _consult_engage_judge(self, snap: _Snapshot, locked: dict[str, Any],
                              now: float) -> str:
        # Hardcoded safety floor: HP critically low -> force rest, no
        # matter what the LLM (or fallback) says. Cheap pre-check ahead
        # of any cache/queue logic.
        ct = locked.get('check_type')
        if (isinstance(snap.self_hp_pct, (int, float))
                and snap.self_hp_pct < MIN_ENGAGE_HP_PCT):
            self._echo_engage_decision(
                'rest', locked.get('name'),
                f"I'm at {snap.self_hp_pct:.0f}% HP - too low to fight, "
                f"I have to rest now",
                check_type=ct, hp_pct=snap.self_hp_pct,
            )
            return 'rest'
        ej = self._engage_judge
        name = locked.get('name')
        plvl = snap.self_lvl
        if ej is None or not name or plvl is None:
            v = self._fallback_verdict(ct)
            self._echo_engage_decision(
                v, name,
                f'(fallback - LLM unavailable, applying hardcoded rules)',
                check_type=ct, hp_pct=snap.self_hp_pct,
            )
            return v

        # Per-call rid lifecycle (no cache). On the first tick we fire
        # a fresh request and store the rid; subsequent ticks poll
        # status. Once a verdict lands we consume + clear the rid;
        # the next acquire cycle for a different sid fires fresh.
        rid = self._engage_judge_rid
        if rid is None:
            history = {}
            tier_history = {}
            if snap.zone_id is not None:
                history = _fight_history.summary(
                    self.cfg, snap.zone_id, name, plvl,
                )
                # Aggregate by check_type at this level so the judge
                # generalizes "I died to Even-Match mobs at level 6"
                # across all mobs of that con tier - even ones we
                # haven't personally fought yet.
                tier_history = _fight_history.tier_summary(
                    self.cfg, snap.zone_id, locked.get('check_type'), plvl,
                )
            mob = {
                'name':       name,
                'level':      locked.get('level'),
                'check_type': locked.get('check_type'),
                'conditions': locked.get('conditions'),
                'distance':   locked.get('distance'),
            }
            from . import gambits as _gambits
            main_job_s = _gambits.JOB_NAMES.get(int(snap.self_main_job)) \
                if isinstance(snap.self_main_job, int) else None
            sub_job_s = _gambits.JOB_NAMES.get(int(snap.self_sub_job)) \
                if isinstance(snap.self_sub_job, int) else None
            player = {
                'level':    plvl,
                'hp_pct':   snap.self_hp_pct,
                'mp_pct':   snap.self_mp_pct,
                'main_job': main_job_s,
                'sub_job':  sub_job_s,
                'sub_lvl':  snap.self_sub_lvl,
            }
            new_rid = ej.request(
                mob, player, self._read_user_goal_text(), history,
                tier_history=tier_history,
            )
            if new_rid is None:
                v = self._fallback_verdict(ct)
                self._echo_engage_decision(
                    v, name,
                    '(fallback - LLM unavailable, applying hardcoded rules)',
                    check_type=ct, hp_pct=snap.self_hp_pct,
                )
                return v
            self._engage_judge_rid = new_rid
            return 'pending'

        verdict = ej.status(rid)
        if verdict is None:
            return 'pending'
        # Verdict landed - consume and clear the rid before acting,
        # so a 'skip' that bounces back to acquire fires a fresh call
        # for the next candidate.
        ej.discard(rid)
        self._engage_judge_rid = None
        decision = verdict.get('decision')
        reason = verdict.get('reason') or ''
        if decision in ('engage', 'skip', 'rest'):
            self._echo_engage_decision(
                decision, name, reason,
                check_type=ct, hp_pct=snap.self_hp_pct,
            )
            return decision
        # 'error' or unknown - fallback.
        v = self._fallback_verdict(ct)
        self._echo_engage_decision(
            v, name, '(fallback - LLM error, applying hardcoded rules)',
            check_type=ct, hp_pct=snap.self_hp_pct,
        )
        return v

    def _echo_engage_decision(self, verdict: str, mob_name: str | None,
                              reason: str,
                              check_type: Any = None,
                              hp_pct: float | int | None = None) -> None:
        """Surface an engage/skip/rest decision in-game. Now that the
        check_type labels in the LLM prompt are correct, we trust the
        LLM's reason text and skip the factual prefix that previously
        guarded against mislabeled cons. Falls back to the mob name
        if the reason is empty.

        check_type / hp_pct kept on the signature so future callers
        can re-introduce a fallback prefix if a model misbehaves."""
        body = (reason or '').strip()
        if not body:
            body = str(mob_name or '(no reason given)')
        _echo.to_chat(self.cfg, verdict, body)

    def _start_resting(self, snap: _Snapshot) -> None:
        """Issue /heal, but only if we're not already resting and we
        haven't toggled recently. /heal in FFXI is a TOGGLE — sending
        it twice in quick succession sits the player down and then
        immediately stands them back up, defeating the rest. Multiple
        decision paths can fire this (engage_judge rest verdict, the
        old post-kill rest, _tick_resting re-arming after disturbance);
        the gap-and-status guard makes them idempotent."""
        if snap.self_status == 33:  # already sitting/resting
            return
        now = time.time()
        if now - self._last_heal_at < HEAL_TOGGLE_GAP_S:
            return
        self._issue('/heal')
        self._last_heal_at = now

    def _fallback_verdict(self, ct: Any) -> str:
        """Hardcoded check_type bucketing - used only when the LLM judge
        is unavailable. Mirrors the pre-judge behavior so the agent
        keeps farming on local-LLM downtime."""
        if isinstance(ct, int):
            if ct in CHECK_TYPE_ENGAGEABLE:
                return 'engage'
            if ct in CHECK_TYPE_TOO_TOUGH or ct == 0x40:
                return 'skip'
        return 'fallback'

    def _tick_acquire(self, snap: _Snapshot, now: float) -> None:
        target_name = (self.directive or {}).get('target_name')
        # engage_nearby mode: target_name is None on the directive, so
        # we pick a target from the live nearby_enemies feed and lock
        # by server_id via combat addon's /combat target - disambiguates
        # the multiple-same-name case that FFXI's /ta "<name>" can't
        # handle. Each kill auto-catalogs the mob's name via entities.lua,
        # so subsequent plans can refine to named `farm` goals if desired.
        if not target_name:
            # The lock has landed - but DO NOT transition to engage
            # until we know the locked mob is level-appropriate. The old
            # logic transitioned the moment target_name appeared, which
            # bypassed the check_type filter entirely (we'd lock + /check
            # but engage before the response landed). Cross-reference the
            # locked mob against nearby_enemies by server_id so we get
            # exactly THIS spawn's check_type, not a same-name neighbor's.
            cutoff = now - SPAWN_BLACKLIST_S
            if snap.target_name and snap.target_alive:
                locked_sid = snap.target_server_id
                # Skip locked-target processing entirely when the lock is
                # on a sid we already blacklisted. Without this, the
                # too-tough branch re-fires every tick (combat.json keeps
                # publishing the stale lock until our next /ta replaces
                # it). Falling through lets the picker pick a different
                # candidate; the picker's `/ta "<name>"` replaces the
                # current lock without needing a separate de-target.
                # (FFXI has no slash command to drop a non-engaged lock -
                # `/ta <bt>` only works while engaged.)
                if (locked_sid is None
                        or self._spawn_blacklist.get(locked_sid, 0.0) <= cutoff):
                    locked_entry = None
                    if locked_sid is not None:
                        for cand in (snap.nearby_enemies or []):
                            if cand.get('server_id') == locked_sid:
                                locked_entry = cand
                                break
                    ct = (locked_entry or {}).get('check_type')
                    # ct is None: /check response hasn't landed yet.
                    # Fall through to the picker; it will re-pick the
                    # same locked mob, hit the sid-dedup, and quietly
                    # wait without re-issuing the lock. ACQUIRE_TIMEOUT_S
                    # bails us out if /check never returns.
                    if ct is not None:
                        verdict = self._consult_engage_judge(
                            snap, locked_entry or {}, now,
                        )
                        if verdict == 'engage':
                            self._acquired_name = snap.target_name
                            self._enter('engage')
                            return
                        if verdict == 'skip':
                            print(f'  farming: judge says skip {snap.target_name!r} '
                                  f'(check 0x{ct:02X}); blacklisting sid '
                                  f'and trying next candidate')
                            if locked_sid is not None:
                                self._spawn_blacklist[locked_sid] = now
                            self._state_entered_at = now
                            # fall through to picker
                        elif verdict == 'rest':
                            # Judge decided HP/MP is too low to safely
                            # engage. Sit and recover before considering
                            # another candidate. The resting state's
                            # post-rest path bounces back into acquire
                            # and we re-evaluate from a healthy state.
                            print(f'  farming: judge says rest before engaging '
                                  f'{snap.target_name!r}')
                            self._start_resting(snap)
                            self._enter('resting')
                            return
                        # verdict == 'pending': hold acquire, do not
                        # re-pick. Returning here keeps the lock and
                        # waits for the worker thread to populate the
                        # cache. ACQUIRE_TIMEOUT_S still bails if the
                        # judge never resolves.
                        elif verdict == 'pending':
                            return
            # Pick the closest unclaimed live mob from nearby_enemies.
            # Skip:
            #   - server_ids on the spawn blacklist (judge said skip,
            #     or lock timed out)
            #   - mobs whose (name, player_lvl) judge cache already says
            #     "skip" - saves an unnecessary lock + /check round trip
            #   - ct == 0x40 (too weak, zero XP) - silent skip, no blacklist
            # Anything else falls through; lock + /check + judge will
            # decide once /check returns.
            nearby = snap.nearby_enemies or []
            pick = None
            for cand in nearby:
                sid = cand.get('server_id')
                if sid is not None and self._spawn_blacklist.get(sid, 0.0) > cutoff:
                    continue
                if cand.get('check_type') == 0x40:
                    continue
                pick = cand
                break
            if pick is not None:
                pname = pick.get('name', '')
                sid   = pick.get('server_id')
                ct    = pick.get('check_type')
                # Dedup by sid, not name: multiple "Wild Rabbit"s are
                # distinct mobs and each one needs its own /ta as the
                # blacklist rotates them out. Name-based dedup would
                # silently skip the second Rabbit after blacklisting
                # the first, leaving the agent stuck on a stale lock.
                if sid is not None and sid != self._last_acquire_sid:
                    self._issue(f'/combat target {sid}')
                    # If we don't have a check_type yet, queue a /check
                    # right after the lock. The lock and /check both
                    # operate on Target slot 0; the response packet
                    # populates combat.lua's entity_checks cache and
                    # the next acquire tick will see check_type filled.
                    if ct is None:
                        self._issue('/check')
                        self._check_issued_at[sid] = now
                    self._last_acquire_sid = sid
                    self._last_action_ts = now
                # /check response timeout: real mobs respond within ~2s.
                # If we /check'd this sid and ct is still None after
                # CHECK_RESPONSE_TIMEOUT_S, it's likely a non-mob entity
                # sharing the mob entity type (Treasure Casket, Footprint).
                # Skip blacklisting if a menu is/was open during the
                # /check window - menus suppress /check responses, so
                # the timeout doesn't actually mean the entity is a
                # non-mob. The addon-side TTL also fades any mistaken
                # blacklist after 5 minutes.
                check_at = self._check_issued_at.get(sid) if sid is not None else None
                if (sid is not None and ct is None and check_at is not None
                        and now - check_at >= CHECK_RESPONSE_TIMEOUT_S):
                    if snap.menu_open:
                        # Reset the check timer so we'll retry once the
                        # menu closes; do not blacklist.
                        self._check_issued_at[sid] = now
                        return
                    self._issue(f'/combat blacklist {sid}')
                    self._spawn_blacklist[sid] = now
                    self._check_issued_at.pop(sid, None)
                    print(f'  farming: /check on sid {sid} ({pname!r}) '
                          f'timed out; non-mob, blacklisting in addon')
                    self._last_acquire_sid = None
                    return
                # Generic acquire timeout: lock didn't take in
                # ACQUIRE_TIMEOUT_S (probably unreachable), local
                # blacklist + try next.
                if now - self._state_entered_at > ACQUIRE_TIMEOUT_S:
                    if sid is not None:
                        self._spawn_blacklist[sid] = now
                    print(f'  farming: /combat target {sid} ({pname!r}) '
                          f'failed in {ACQUIRE_TIMEOUT_S:.0f}s; blacklisting '
                          f'and retrying with next candidate')
                    self._state_entered_at = now
                    self._last_acquire_sid = None
                return
            # nearby_enemies was empty. For engage_nearby this isn't
            # a hard failure - coverage-driven exploration: walk to
            # the next unexplored cell. Termination happens in wander
            # when coverage.is_fully_covered says the zone is mapped
            # at the current Vana'diel hour.
            if self._wander_count >= MAX_WANDERS_SAFETY_CAP:
                print(f'  farming: hit safety cap of {MAX_WANDERS_SAFETY_CAP} '
                      f'wander attempts; failing (coverage termination '
                      f'should have fired earlier)')
                self._enter('failed')
            else:
                print(f'  farming: nearby_enemies empty; '
                      f'wander attempt {self._wander_count + 1}')
                self._enter('wander')
            return
        # Already locked onto the right mob? Engage.
        if snap.target_name == target_name and snap.target_alive:
            self._enter('engage')
            return
        # Acquire-state timeout: if the lock didn't take in this many
        # seconds, the spawn we headed to is stale (mob despawned, ran
        # out of detection range, line-of-sight blocked by terrain).
        # Blacklist this spawn and re-locate so we try a different one.
        # Without this, a stuck position would spin for the full
        # STATE_TIMEOUT_S = 120s, picking the same spawn every retry.
        if now - self._state_entered_at > ACQUIRE_TIMEOUT_S:
            spawn = self._target_spawn
            if spawn is not None:
                sid = spawn.get('server_id')
                self._spawn_blacklist[sid] = now
                print(f'  farming: lock failed for "{target_name}" at '
                      f'({spawn.get("center_x", 0):.0f},{spawn.get("center_y", 0):.0f}) '
                      f'after {ACQUIRE_TIMEOUT_S:.0f}s - blacklisting, re-locating')
            self._target_spawn = None
            self._enter('locate')
            return
        # Pick the closest unclaimed live mob in nearby_enemies whose
        # name matches target_name and lock by its server_id. The
        # cataloged _target_spawn.server_id is unreliable (mobs respawn
        # with new sids), so we resolve to a live sid from the current
        # snapshot. Skip sids on the spawn blacklist so we don't re-lock
        # the despawn we just gave up on.
        cutoff = now - SPAWN_BLACKLIST_S
        pick = None
        for cand in (snap.nearby_enemies or []):
            if cand.get('name') != target_name:
                continue
            sid = cand.get('server_id')
            if sid is not None and self._spawn_blacklist.get(sid, 0.0) > cutoff:
                continue
            pick = cand
            break
        if pick is None:
            # No matching mob visible yet - keep waiting. The spawn
            # may not have rendered into the entity table; the
            # ACQUIRE_TIMEOUT_S branch above bails us out if it never
            # appears.
            return
        sid = pick.get('server_id')
        if sid is not None and sid != self._last_acquire_sid:
            self._issue(f'/combat target {sid}')
            self._last_acquire_sid = sid
            self._last_action_ts = now

    def _tick_engage(self, snap: _Snapshot, now: float) -> None:
        # Latch engaged-observed once we ever see the engaged flag flip
        # true during this engage state. Without this latch, target
        # deselect / mob despawn / a brief /ta drop would count as a
        # kill - see _observed_engaged docstring for the underlying bug.
        if snap.engaged:
            self._observed_engaged = True

        # In engage_nearby mode, target_name is None on the directive;
        # we use the name we latched in acquire so the gone/dead checks
        # below stay meaningful for *this specific* mob. Without this,
        # target_gone would always be True and we'd bounce immediately.
        target_name = (self.directive or {}).get('target_name')
        if target_name is None:
            target_name = self._acquired_name
        target_gone = snap.target_name != target_name
        target_dead = (snap.target_alive is False
                       or (snap.target_hp_pct is not None and snap.target_hp_pct == 0))

        # Real kill: the engaged flag was true at some point AND the
        # target is now gone or dead. Bookkeep as a kill.
        if (target_dead or target_gone) and self._observed_engaged:
            self._enter('killed')
            return

        # Target gone but we never engaged. Common cause during the
        # nav-walk approach: the mob wandered just past /ta range and
        # FFXI auto-dropped the lock. If our last-acquired sid is
        # still in nearby_enemies, the mob is around - just re-lock
        # and stay in engage so nav keeps walking us closer. Bouncing
        # to acquire would fire /nav stop in _enter() and produce
        # an engage<->acquire flap loop spamming /nav stop every tick.
        if target_gone:
            sid = self._last_acquire_sid
            still_near = False
            if sid is not None:
                for cand in (snap.nearby_enemies or []):
                    if cand.get('server_id') == sid:
                        still_near = True
                        break
            if still_near:
                # Re-issue the lock at most once per second to avoid
                # spam if the mob is right at the edge of /ta range.
                if now - self._last_attack_attempt >= 1.0:
                    self._issue(f'/combat target {sid}')
                    self._last_attack_attempt = now
                return
            # Mob is genuinely gone (out of nearby scan). Bounce.
            self._enter('acquire')
            return

        # Capture HP at engage entry for damage-taken tracking. First
        # tick of the engage state populates this; clears on every
        # _enter() since re-entering engage is a new fight.
        if self._engage_start_hp_pct is None and snap.self_hp_pct is not None:
            self._engage_start_hp_pct = float(snap.self_hp_pct)
        # Capture the locked mob's check_type at engage entry so
        # fight_history can aggregate by difficulty tier on kill/death.
        if self._engage_check_type is None:
            for cand in (snap.nearby_enemies or []):
                if cand.get('server_id') == snap.target_server_id:
                    ct = cand.get('check_type')
                    if isinstance(ct, int):
                        self._engage_check_type = ct
                    break

        d = snap.target_distance
        if d is None:
            return  # snapshot stale; wait for fresh data

        # The authoritative "combat has actually started" signal is
        # claim, not engaged. /attack on can fire and engaged flip
        # while the server hasn't actually awarded the mob to us yet
        # (mob still unclaimed or claimed by someone else). Until
        # claim is ours, keep nav tracking the mob's live position so
        # a lunge during weapon-draw or claim contention doesn't
        # strand us. Once claimed_by_us flips true, /nav stop and
        # FFXI's auto-pursuit handles the rest.
        if snap.target_claimed_by_us:
            if not self._nav_stopped:
                self._issue('/nav stop')
                self._nav_stopped = True
            return

        # Not yet claimed by us - keep nav active.
        self._nav_stopped = False

        # In attack range: fire /attack on (retried every
        # ATTACK_RETRY_S to handle the weapon-delay "must wait longer"
        # rejection). Once snap.engaged flips true the server has
        # accepted the attack and we're swinging - re-issuing only
        # produces "must wait longer" spam, even if claim hasn't
        # propagated yet. Engaged is the right signal here; claim
        # gates the OUTER /nav stop, not the /attack on retry.
        if d <= MELEE_DISTANCE_Y and not snap.engaged:
            if snap.self_status == 33:
                # Still sitting from a prior rest - stand up first.
                if now - self._last_heal_at >= HEAL_TOGGLE_GAP_S:
                    self._issue('/heal off')
                    self._last_heal_at = now
            elif now - self._last_attack_attempt >= ATTACK_RETRY_S:
                self._issue('/attack on')
                self._last_attack_attempt = now

        # Re-dispatch nav goto to the mob's current position when
        # throttle elapsed and the mob has moved enough to matter.
        if (snap.target_x is None or snap.zone_id is None
                or snap.x is None or self._dispatch is None):
            return
        if now - self._last_action_ts < APPROACH_REDISPATCH_S:
            return
        last = getattr(self, '_last_approach_target', None)
        if last is not None:
            dx = snap.target_x - last[0]
            dy = snap.target_y - last[1] if snap.target_y is not None else 0
            if (dx * dx + dy * dy) ** 0.5 < APPROACH_REDISPATCH_DELTA_Y:
                return
        self._dispatch({
            'action':  'goto',
            'zone_id': snap.zone_id,
            'player':  [snap.x, snap.y or 0.0, snap.z or 0.0],
            'target':  [snap.target_x,
                        snap.target_y or 0.0,
                        snap.target_z or 0.0],
            'seq':     int(now * 1000),
        })
        self._last_action_ts = now
        self._last_approach_target = (snap.target_x, snap.target_y or 0.0)

    def _tick_killed(self, snap: _Snapshot, now: float) -> None:
        # First tick in this state: do the one-shot kill bookkeeping and
        # fire the rest judgment. Subsequent ticks (rare - we usually
        # transition out on this same tick) just transition to the
        # judging_rest state. The split lets the judgment-fire happen
        # AFTER kill_count++ so stop_when can short-circuit before
        # we waste an LLM call.
        if not self._kill_recorded:
            if snap.engaged:
                self._issue('/attack off')
            # /follow off was already issued by _enter() on the engage->
            # killed transition; no need to repeat here.
            self.kills += 1
            killed_name = ((self.directive or {}).get('target_name')
                           or self._acquired_name)
            if killed_name and snap.zone_id is not None:
                # damage_taken_pct = HP at engage start - HP now. Falls
                # back to None (skipped) if we don't have an engage-
                # start latch (e.g. the engage state never ticked).
                dmg = None
                if (self._engage_start_hp_pct is not None
                        and snap.self_hp_pct is not None):
                    dmg = max(0.0, self._engage_start_hp_pct - float(snap.self_hp_pct))
                _fight_history.record_kill(
                    self.cfg, snap.zone_id, killed_name,
                    player_level=snap.self_lvl,
                    hp_remaining_pct=snap.self_hp_pct,
                    damage_taken_pct=dmg,
                    check_type=self._engage_check_type,
                )
            _events.append(
                self.cfg.paths.events_file(),
                character=self.cfg.character,
                source='farming',
                type_='farm_kill',
                kills=self.kills,
                target_name=killed_name,
            )
            self._kill_recorded = True
            if self._stop_when_satisfied():
                self._enter('completed')
                return
            # TODO(rest-after-kill): the post-kill rest decision is
            # disabled for now. We previously consulted rest_judge here
            # to choose between rest/continue, but the LLM lacks the
            # context (mob aggression, link behavior, who's nearby and
            # what they'll do if we sit) to make a good call. When we
            # have a richer aggression model — chat-listener picks up
            # mob link/aggro events, fight_history records who linked,
            # the engage tier learns "sit-safe" vs "sit-risky" zones —
            # bring the rest_judge call back here. The instance lives
            # on (`self._rest_judge`) so the wiring is intact, just
            # not invoked. Engage-time rest (HP-too-low-to-fight) is
            # still active in _consult_engage_judge.
            self._target_spawn = None
            self._last_action_ts = 0.0
            self._enter('locate')
            return
        # Defensive: re-entering killed without re-recording (rare).
        self._target_spawn = None
        self._last_action_ts = 0.0
        self._enter('locate')

    def _fallback_rest_decision(self, snap: _Snapshot) -> None:
        """Hardcoded rest_hp_pct threshold - used when the LLM judge is
        unavailable or returned an error. Mirrors pre-judge behaviour
        so the agent keeps farming on local-LLM downtime."""
        rest_threshold = (self.directive or {}).get('rest_hp_pct', 70)
        if snap.self_hp_pct is not None and snap.self_hp_pct < rest_threshold:
            self._start_resting(snap)
            self._enter('resting')
            return
        self._target_spawn = None
        self._last_action_ts = 0.0
        self._enter('locate')

    def _tick_judging_rest(self, snap: _Snapshot, now: float) -> None:
        """Poll the rest_judge until a verdict lands, then transition.
        STATE_TIMEOUT_S bails us out if the LLM hangs - the parent
        timeout in tick() routes us back to locate."""
        rid = self._rest_judge_rid
        if rid is None or self._rest_judge is None:
            # Defensive: judge gone or rid never set - fall back.
            self._fallback_rest_decision(snap)
            return
        verdict = self._rest_judge.status(rid)
        if verdict is None:
            # Still pending - wait.
            return
        decision = verdict.get('decision')
        reason = (verdict.get('reason') or '')[:120]
        self._rest_judge.discard(rid)
        self._rest_judge_rid = None
        if decision == 'rest':
            print(f'  farming: rest judge -> rest ({reason})')
            _echo.to_chat(self.cfg, 'rest', reason or '(no reason given)')
            self._start_resting(snap)
            self._enter('resting')
            return
        if decision == 'continue':
            print(f'  farming: rest judge -> continue ({reason})')
            _echo.to_chat(self.cfg, 'continue', reason or '(no reason given)')
            self._target_spawn = None
            self._last_action_ts = 0.0
            self._enter('locate')
            return
        # 'error' or anything unexpected -> fall back.
        print(f'  farming: rest judge -> error ({reason}); falling back')
        _echo.to_chat(self.cfg, 'rest?',
                      '(rest judge errored - applying hardcoded threshold)')
        self._fallback_rest_decision(snap)

    def _tick_resting(self, snap: _Snapshot, now: float) -> None:
        # Periodically check HP. Once high enough, /heal off (stand up)
        # and resume hunting.
        if now - self._last_action_ts < REST_CHECK_INTERVAL_S:
            return
        self._last_action_ts = now
        # 95% is a common cutoff - getting from 95->100 is much slower than
        # the regen rate while sitting, and we'd rather get back to the kill.
        if snap.self_hp_pct is not None and snap.self_hp_pct >= 95:
            self._issue('/heal off')
            self._target_spawn = None
            self._last_action_ts = 0.0
            self._enter('locate')
        # If we're not actually sitting (combat or movement broke the
        # rest), try to re-engage the toggle. _start_resting drops the
        # call if we toggled recently, so we don't oscillate.
        elif snap.self_status != 33:  # 33 = resting per Ashita
            self._start_resting(snap)

    def _tick_wander(self, snap: _Snapshot, now: float) -> None:
        """engage_nearby helper: walk to the next exploration target
        (frontier cell from the coverage map) and then re-enter acquire
        to scan for mobs there. Coverage data is updated continuously
        by the nav addon as the agent moves, so each new wander pick
        sees a slightly larger explored area.

        Termination: when the coverage map says every recorded cell
        in this zone has been visited at the current Vana'diel hour,
        we declare the zone fully explored at this ToD and fail -
        the failure replan can then escalate (different zone, party
        up, etc). Random fallback only kicks in for the bootstrap
        case (zero cells recorded yet) so the very first wander has
        somewhere to go.

        Reactivity: re-checks `nearby_enemies` on every tick. If a mob
        appears mid-wander (we walked into detection range, or it just
        spawned, or the combat addon's sample lagged), abort and bounce
        to acquire to /ta it immediately."""
        # Reactivity: enemies showed up since we entered wander.
        # Don't waste the wander attempt - interrupt and engage.
        if snap.nearby_enemies:
            self._target_spawn = None
            self._issue('/nav stop')
            self._enter('acquire')
            return
        # No nav snapshot or no dispatch channel -> can't move; just
        # cycle back to acquire and let it succeed-or-fail again.
        if snap.x is None or snap.zone_id is None or self._dispatch is None:
            self._wander_count += 1
            self._enter('acquire')
            return
        # Pick the next exploration target via the coverage map.
        # Frontier expansion finds cells adjacent to visited ones,
        # which keeps the agent on reachable terrain. Fallback to
        # random offset for the bootstrap case where no cells are
        # recorded yet (very first wander after entering the zone,
        # before nav.lua has written any coverage data to disk).
        #
        # Termination signal: when nearest_target returns None AND we
        # have at least one recorded cell, the frontier is exhausted
        # - every cell adjacent to explored ground has itself been
        # explored. That's the real "zone fully mapped" signal, much
        # tighter than "every recorded cell visited at this hour"
        # (which fires the moment you step into any zone).
        vana_hour = _coverage.vana_hour_now()
        if self._target_spawn is None:
            cells_known = len(_coverage.load(self.cfg, snap.zone_id).get('cells') or {})
            tx = ty = None
            pick_kind = ''
            # Exploitation first: if we already know about mobs in this
            # zone (entities.json records from prior sightings), head
            # toward the closest one. Frontier expansion is for
            # discovering new ground, but for a "level up" goal we
            # care more about reaching mobs we've already seen than
            # mapping uncharted terrain. Skip records whose center
            # we recently failed to reach (cell-key blacklist already
            # excludes those).
            try:
                records = _entities.load_records(self.cfg, snap.zone_id)
            except Exception:
                records = {}
            if records and self._nearest_reachable is not None:
                best_d2 = float('inf')
                best_xy: tuple[float, float] | None = None
                for rec in records.values():
                    cx_rec = rec.get('center_x')
                    cy_rec = rec.get('center_y')
                    if cx_rec is None or cy_rec is None:
                        continue
                    # Exclude records we already tried as wander targets
                    # this directive (failed cells get cell_key-blacklisted).
                    cell_key = f'{int(cx_rec // 30)}:{int(cy_rec // 30)}'
                    if cell_key in self._wander_cell_blacklist:
                        continue
                    d2 = (snap.x - cx_rec) ** 2 + ((snap.y or 0.0) - cy_rec) ** 2
                    # Filter "already here" - if we're standing on top
                    # of the spawn point and there's nothing in
                    # nearby_enemies, the mob's not up; move on.
                    if d2 < 100.0:  # within ~10y, basically "here"
                        continue
                    if d2 < best_d2:
                        snapped = self._nearest_reachable(
                            snap.zone_id, (cx_rec, cy_rec),
                        )
                        if snapped is not None:
                            best_d2 = d2
                            best_xy = snapped
                if best_xy is not None:
                    tx, ty = best_xy
                    self._wander_cell_key = (
                        f'{int(best_xy[0] // 30)}:{int(best_xy[1] // 30)}'
                    )
                    pick_kind = 'known-mob-spawn'
            # Fall back to coverage-frontier exploration if we have no
            # known-mob target (either no records yet, or all known
            # spawns blacklisted/unreachable). Loop the picker so
            # unreachable cells get blacklisted immediately rather
            # than wasting a 30s wander timeout finding out the same
            # thing. Capped at MAX_PICK_TRIES so a degenerate state
            # (every candidate unreachable) still terminates.
            MAX_PICK_TRIES = 16
            for _ in range(MAX_PICK_TRIES if tx is None else 0):
                pick = _coverage.nearest_target(
                    self.cfg, snap.zone_id,
                    (snap.x, snap.y or 0.0),
                    vana_hour,
                    exclude_cells=self._wander_cell_blacklist,
                )
                if pick is None:
                    break
                (cand_x, cand_y), cell_key = pick
                # Validate against the navmesh. nearest_reachable
                # snaps the target to the closest walkable poly within
                # max_snap_y; if that's too far, the cell is genuinely
                # off-mesh (wall, water, void) and we blacklist now.
                if self._nearest_reachable is not None:
                    snapped = self._nearest_reachable(
                        snap.zone_id, (cand_x, cand_y),
                    )
                    if snapped is None:
                        # Cell center isn't near walkable terrain -
                        # blacklist and try the next candidate.
                        self._wander_cell_blacklist.add(cell_key)
                        continue
                    cand_x, cand_y = snapped
                tx, ty = cand_x, cand_y
                self._wander_cell_key = cell_key
                pick_kind = 'coverage-frontier'
                break
            if tx is None and cells_known > 0:
                # Loop exhausted without finding a reachable cell, AND
                # we have real coverage data. Either the zone is
                # genuinely mapped at this Vana hour, or every remaining
                # frontier cell turned out to be unreachable. Either
                # way: stop spinning, escalate to failure_replan.
                print(f'  farming: zone {snap.zone_id} no reachable frontier '
                      f'({cells_known} cells, vana hour {vana_hour}, '
                      f'{len(self._wander_cell_blacklist)} unreachable); '
                      f'failing (escalate to replan)')
                self._enter('failed')
                return
            if tx is None:
                # Coverage is empty (first sample hasn't been written
                # to disk yet). Use a random offset for the very first
                # step so the agent moves at all - once it does, the
                # coverage tracker will start populating cells. Validate
                # the random pick too if we have the navmesh API.
                for _ in range(MAX_PICK_TRIES):
                    angle = random.uniform(0, 2 * math.pi)
                    radius = random.uniform(WANDER_RADIUS_Y * 0.5, WANDER_RADIUS_Y)
                    rx = snap.x + math.cos(angle) * radius
                    ry = (snap.y or 0.0) + math.sin(angle) * radius
                    if self._nearest_reachable is not None:
                        snapped = self._nearest_reachable(
                            snap.zone_id, (rx, ry),
                        )
                        if snapped is None:
                            continue
                        rx, ry = snapped
                    tx, ty = rx, ry
                    break
                if tx is None:
                    # Navmesh returned no walkable points anywhere
                    # near our random samples. Bail.
                    print(f'  farming: bootstrap wander found no '
                          f'reachable points; failing')
                    self._enter('failed')
                    return
                self._wander_cell_key = None  # bootstrap targets aren't grid cells
                pick_kind = 'random-bootstrap'
            self._target_spawn = {
                'center_x': tx, 'center_y': ty, 'center_z': snap.z or 0.0,
                'name': '<wander>',
                'server_id': None,
            }
            self._dispatch({
                'action':   'goto',
                'zone_id':  snap.zone_id,
                'player':   [snap.x, snap.y or 0.0, snap.z or 0.0],
                'target':   [tx, ty, snap.z or 0.0],
                'seq':      int(now * 1000),
                # Each new wander overrides the addon's previous
                # in-flight state - without this, leftover retry
                # targets from prior travel/farm goals can wake up
                # mid-wander and walk the agent to the wrong place.
                'reset_state': True,
            })
            self._last_action_ts = now
            print(f'  farming: wandering toward ({tx:.0f},{ty:.0f}) '
                  f'[{pick_kind}, attempt {self._wander_count + 1}]')
            return
        # Arrived? Bump counter, clear target, head back to acquire.
        sx = self._target_spawn['center_x']
        sy = self._target_spawn['center_y']
        dx = sx - snap.x
        dy = sy - (snap.y or 0.0)
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= WANDER_ARRIVAL_RADIUS_Y:
            self._wander_count += 1
            # Add the just-arrived cell to the in-memory blacklist so
            # the next nearest_target pick doesn't re-select it before
            # the addon's coverage save (~30s) propagates the new
            # "visited" status to disk. Without this we churn on the
            # same cell every tick, dispatching tiny 3-wp paths and
            # never actually exploring outward.
            if self._wander_cell_key is not None:
                self._wander_cell_blacklist.add(self._wander_cell_key)
            self._target_spawn = None
            self._wander_cell_key = None
            self._issue('/nav stop')
            self._enter('acquire')
            return
        # Timeout fallback: position is unreachable (off-mesh, blocked
        # by terrain, agent stuck). Blacklist the cell so the next
        # nearest_target pick skips it - without this, frontier
        # expansion would deterministically pick the same cell again
        # next tick and we'd infinitely loop on an unreachable corner.
        if now - self._state_entered_at > WANDER_TIMEOUT_S:
            if self._wander_cell_key is not None:
                self._wander_cell_blacklist.add(self._wander_cell_key)
                print(f'  farming: wander timeout ({dist:.0f}y short of '
                      f'cell {self._wander_cell_key}); blacklisting and '
                      f'retrying acquire')
            else:
                print(f'  farming: wander timeout ({dist:.0f}y short of '
                      f'random-bootstrap target); retrying acquire')
            self._wander_count += 1
            self._target_spawn = None
            self._wander_cell_key = None
            self._issue('/nav stop')
            self._enter('acquire')
