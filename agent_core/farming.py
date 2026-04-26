"""Farming director — drives the kill loop for a `farm` goal.

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

    1. State changes (locate → approach → acquire → engage → killed →
       rest → loop) gated on what the combat addon publishes to
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

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config as _config
from . import entities as _entities
from . import events as _events


# How long /ta has to bring the requested mob into the combat addon's
# `target` slot before we give up and retry. Most mobs respond instantly;
# 3s is a generous bound that survives a laggy frame.
TARGET_LOCK_TIMEOUT_S = 3.0

# After issuing /heal, how long to sit before we check HP again. Sitting
# regenerates HP roughly every tick, so even 5s is plenty between checks.
REST_CHECK_INTERVAL_S = 5.0

# Hard ceiling on how long we'll spend in any single state. If something
# pathological happens (target unkillable, HP not regenerating, etc.) we
# bail to LOCATE rather than stay stuck forever.
STATE_TIMEOUT_S = 120.0

# How close the player has to be to the spawn position before we
# consider ourselves "near enough to /ta" — the named entity has to be
# within client detection range, which is roughly 20 yalms in FFXI.
APPROACH_RADIUS_Y = 18.0

# Hand the engage tick a margin before re-issuing /attack on. Only used
# when we've never seen engaged=True for this fight — once we observe it,
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


@dataclass
class _Snapshot:
    """The combat + nav bits the director consults each tick. All fields
    are nullable; the director treats nil as 'no info, retry'."""
    self_hp_pct:   float | None
    self_hp:       int   | None     # raw — death check (hp_pct unreliable when hp_max=0)
    self_status:   int   | None     # 0 idle, 1 engaged, 2 dead, 3 dead/zoning, 33 healing
    target_name:   str   | None
    target_alive:  bool  | None
    target_hp_pct: float | None
    engaged:       bool
    # Nav-side fields used to decide arrival at the spawn position.
    zone_id:       int   | None = None
    x:             float | None = None
    y:             float | None = None
    z:             float | None = None
    moving:        bool         = False


class FarmingDirector:
    """One director per orchestrator. start() arms it for a directive;
    tick() advances the state machine."""

    STATES = ('idle', 'locate', 'approach', 'acquire', 'engage',
              'killed', 'resting', 'completed', 'failed')

    def __init__(
        self,
        cfg: _config.Config,
        snapshot_provider: Callable[[], _Snapshot],
        issue_command: Callable[[str], None],
        nav_snapshot_provider: Callable[[], _Snapshot] | None = None,
        dispatch_goto: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.cfg = cfg
        self._combat_snapshot = snapshot_provider
        self._issue = issue_command
        # Optional injectables — without them, locate/approach degrade
        # gracefully into the old "assume we're near the spawn" behaviour.
        # Keeps tests + tools that drive the director directly viable.
        self._nav_snapshot = nav_snapshot_provider
        self._dispatch = dispatch_goto
        self.state: str = 'idle'
        self.directive: dict[str, Any] | None = None
        self.kills: int = 0
        self._state_entered_at: float = 0.0
        self._last_action_ts: float = 0.0
        # Spawn we're heading to right now. Refreshed on every locate so
        # we don't keep walking to a stale (or already-killed) record.
        self._target_spawn: dict[str, Any] | None = None
        # server_id → wall-clock ts when /ta last failed for that spawn.
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
        # is sticky in FFXI — once on, it persists until cancelled, the
        # target dies, or you zone. Re-issuing pollutes the chat log
        # without changing anything. Reset on every state entry.
        self._follow_issued: bool = False

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
        # Each new directive gets a fresh blacklist — old failures are
        # not necessarily relevant to a new mob name / kill count.
        self._spawn_blacklist = {}
        # Enter locate first — pick a known spawn position from the nav
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
        provider is mandatory; nav is optional — without it we can't
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
            target_name=snap.target_name,
            target_alive=snap.target_alive,
            target_hp_pct=snap.target_hp_pct,
            engaged=snap.engaged,
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
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='farming',
            type_='farm_state',
            state=new_state,
            kills=self.kills,
        )

    def _stop_when_satisfied(self) -> bool:
        d = self.directive or {}
        sw = d.get('stop_when') or {}
        kc = sw.get('kill_count')
        if isinstance(kc, (int, float)) and self.kills >= kc:
            return True
        return False

    def tick(self) -> None:
        if self.directive is None or not self.is_active():
            return
        snap = self._snapshot()
        now = time.time()

        # Death check — runs first, beats every other transition. If the
        # player is dead we MUST stop firing combat commands. Without
        # this, /attack on retries spam the chat log forever, and the
        # acquire/engage timeouts try to re-engage a dead character.
        # Status 2/3 = dead in standard Ashita; raw HP=0 is the backup
        # signal because hp_pct is unreliable when hp_max reads 0.
        is_dead = (
            (snap.self_status in (2, 3))
            or (snap.self_hp == 0 and snap.self_status not in (None, 1))
        )
        if is_dead:
            if self.state != 'failed':
                print('  farming: player is dead — aborting farm')
                self._issue('/attack off')
                _events.append(
                    self.cfg.paths.events_file(),
                    character=self.cfg.character,
                    source='farming',
                    type_='farm_death',
                    state_at_death=self.state,
                    kills_at_death=self.kills,
                )
                self._enter('failed')
            return

        # Per-state timeout — never wedged for more than STATE_TIMEOUT_S.
        # Resting is exempt because /heal up time is itself bounded by
        # the rest tick's HP check; killed is exempt because it's a
        # one-shot bookkeeping state.
        if now - self._state_entered_at > STATE_TIMEOUT_S \
                and self.state not in ('resting', 'killed'):
            print(f'  farming: state {self.state} timed out — bouncing to locate')
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
        elif self.state == 'resting':
            self._tick_resting(snap, now)

    def _tick_locate(self, snap: _Snapshot, now: float) -> None:
        """Pick a spawn position to head toward. We prefer the closest
        known record in the player's current zone — the agent is already
        IN the zone (the planner emits a travel leaf before farm), so
        the only legwork left is closing the in-zone distance."""
        target_name = (self.directive or {}).get('target_name')
        if not target_name:
            self._enter('failed')
            return
        # No nav snapshot — fall back to the bare-/ta loop. Useful for
        # tests or when the agent has been hand-positioned next to the
        # spawn.
        if snap.x is None or snap.zone_id is None:
            self._enter('acquire')
            return
        # Pull every candidate, sort by distance, skip any whose
        # server_id is on a fresh blacklist entry. The first survivor
        # is our spawn. If everything's blacklisted we fall back to the
        # globally closest record (better than nothing — worst case the
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
            # All known spawns are blacklisted — pick the closest as a
            # last-resort retry. Better to walk back to a place we know
            # mobs spawn than spin in idle.
            spawn = all_candidates[0]
            print(f'  farming: all {len(all_candidates)} spawns blacklisted; '
                  f'retrying closest at ({spawn["center_x"]:.0f},{spawn["center_y"]:.0f})')
        self._target_spawn = spawn
        # If we're already within the named entity's client-detection
        # range, skip approach — /ta will work right now.
        dx = (spawn['center_x']) - (snap.x or 0.0)
        dy = (spawn['center_y']) - (snap.y or 0.0)
        dist = (dx * dx + dy * dy) ** 0.5
        print(f'  farming: heading to {spawn["name"]} at '
              f'({spawn["center_x"]:.0f},{spawn["center_y"]:.0f}) '
              f'— {dist:.0f}y away')
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
            # Lost the spawn record (started without one) — re-locate.
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

    def _tick_acquire(self, snap: _Snapshot, now: float) -> None:
        target_name = (self.directive or {}).get('target_name')
        if not target_name:
            self._enter('failed')
            return
        # Already locked onto the right mob? Engage.
        if snap.target_name == target_name and snap.target_alive:
            self._enter('engage')
            return
        # Acquire-state timeout: if /ta hasn't locked the named target
        # in this many seconds, the spawn we headed to is stale (mob
        # despawned, ran out of detection range, line-of-sight blocked
        # by terrain). Blacklist this spawn and re-locate so we try a
        # different one. Without this, a stuck position would spin /ta
        # for the full STATE_TIMEOUT_S = 120s, picking the same spawn
        # every retry.
        if now - self._state_entered_at > ACQUIRE_TIMEOUT_S:
            spawn = self._target_spawn
            if spawn is not None:
                sid = spawn.get('server_id')
                self._spawn_blacklist[sid] = now
                print(f'  farming: /ta failed for "{target_name}" at '
                      f'({spawn.get("center_x", 0):.0f},{spawn.get("center_y", 0):.0f}) '
                      f'after {ACQUIRE_TIMEOUT_S:.0f}s — blacklisting, re-locating')
            self._target_spawn = None
            self._enter('locate')
            return
        # Issue /ta no more than once a second to avoid spamming chat.
        if now - self._last_action_ts >= 1.0:
            self._issue(f'/ta "{target_name}"')
            self._last_action_ts = now

    def _tick_engage(self, snap: _Snapshot, now: float) -> None:
        # Latch engaged-observed once we ever see the engaged flag flip
        # true during this engage state. Without this latch, target
        # deselect / mob despawn / a brief /ta drop would count as a
        # kill — see _observed_engaged docstring for the underlying bug.
        if snap.engaged:
            self._observed_engaged = True

        target_name = (self.directive or {}).get('target_name')
        target_gone = snap.target_name != target_name
        target_dead = (snap.target_alive is False
                       or (snap.target_hp_pct is not None and snap.target_hp_pct == 0))

        # Real kill: the engaged flag was true at some point AND the
        # target is now gone or dead. Bookkeep as a kill.
        if (target_dead or target_gone) and self._observed_engaged:
            self._enter('killed')
            return

        # Target gone but we never engaged — /ta dropped, mob ran out
        # of detection range, we zoned, etc. Don't count it. Bounce back
        # to acquire so we re-/ta the next live one in range.
        if target_gone:
            self._enter('acquire')
            return

        # Issue /follow exactly once on entering engage. /follow is
        # sticky — once active it persists until cancelled, the target
        # dies, or we zone. Re-issuing every retry tick spams the chat
        # log with no behaviour change.
        if not self._follow_issued:
            self._issue('/follow <t>')
            self._follow_issued = True

        # Once we've observed engaged=True, autoattack continues to the
        # kill on its own — no further /attack on commands needed.
        # Re-issuing while engaged would also fail (game rejects
        # "you are already engaged") and clutter the chat.
        if self._observed_engaged:
            return

        # Pre-engagement: re-issue /attack on at a slow cadence. /attack
        # silently rejects when out of range or mid-action; /follow is
        # already closing the distance. 5s between retries is plenty
        # and keeps the chat log quiet.
        if now - self._last_action_ts >= ATTACK_RETRY_INTERVAL_S:
            self._issue('/attack on')
            self._last_action_ts = now

    def _tick_killed(self, snap: _Snapshot, now: float) -> None:
        # Drop attack flag (no-op if already off) and tally the kill.
        if snap.engaged:
            self._issue('/attack off')
        self.kills += 1
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='farming',
            type_='farm_kill',
            kills=self.kills,
            target_name=(self.directive or {}).get('target_name'),
        )
        if self._stop_when_satisfied():
            self._enter('completed')
            return
        # Decide rest vs continue based on HP.
        rest_threshold = (self.directive or {}).get('rest_hp_pct', 70)
        if snap.self_hp_pct is not None and snap.self_hp_pct < rest_threshold:
            self._issue('/heal')
            self._enter('resting')
            return
        # Pick a fresh spawn — the one we just killed is gone, so we
        # need to relocate. Re-entering locate also resets _last_action_ts
        # for approach, which lets it dispatch a new goto cleanly.
        self._target_spawn = None
        self._last_action_ts = 0.0
        self._enter('locate')

    def _tick_resting(self, snap: _Snapshot, now: float) -> None:
        # Periodically check HP. Once high enough, /heal off (stand up)
        # and resume hunting.
        if now - self._last_action_ts < REST_CHECK_INTERVAL_S:
            return
        self._last_action_ts = now
        # 95% is a common cutoff — getting from 95→100 is much slower than
        # the regen rate while sitting, and we'd rather get back to the kill.
        if snap.self_hp_pct is not None and snap.self_hp_pct >= 95:
            self._issue('/heal off')
            self._target_spawn = None
            self._last_action_ts = 0.0
            self._enter('locate')
        # Re-issue /heal occasionally in case combat or movement broke it.
        elif snap.self_status != 33:  # 33 = resting per Ashita
            self._issue('/heal')
