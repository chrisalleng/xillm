"""Farming director — drives the kill loop for a `farm` goal.

A `farm` goal is a leaf in the goal tree:

    {
      "id": "...",
      "type": "farm",
      "target_name": "Bumblebee",                     # mob to engage
      "stop_when":   {"kill_count": 5},               # MVP stop condition
      "rest_hp_pct": 70                               # rest below this
    }

The director runs as a per-tick state machine while the goal_manager has
a `farm` leaf active. It coordinates two things:

    1. State changes (acquire → engage → killed → rest → loop) gated on
       what the combat addon publishes to state/<char>/combat.json.
    2. Side effects (issuing /ta, /attack, /heal commands) via the
       cmd_inbox channel that cmdrelay drains in-game.

Phase 3c-min scope: single zone, single target name, kill-count stop
condition, HP-only rest. Multi-target lists, item drops, respawn-aware
target picking, cross-zone relocation all land in later passes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config as _config
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
# bail to ACQUIRE rather than stay stuck forever.
STATE_TIMEOUT_S = 120.0


@dataclass
class _Snapshot:
    """The combat-side and self-side bits the director consults each tick.
    All fields are nullable; the director treats nil as "no info, retry"."""
    self_hp_pct:   float | None
    self_status:   int   | None     # 1=engaged, 33=resting, etc.
    target_name:   str   | None
    target_alive:  bool  | None
    target_hp_pct: float | None
    engaged:       bool


class FarmingDirector:
    """One director per orchestrator. start() arms it for a directive;
    tick() advances the state machine."""

    STATES = ('idle', 'acquire', 'engage', 'killed', 'resting', 'completed', 'failed')

    def __init__(
        self,
        cfg: _config.Config,
        snapshot_provider: Callable[[], _Snapshot],
        issue_command: Callable[[str], None],
    ):
        self.cfg = cfg
        self._snapshot = snapshot_provider
        self._issue = issue_command
        self.state: str = 'idle'
        self.directive: dict[str, Any] | None = None
        self.kills: int = 0
        self._state_entered_at: float = 0.0
        self._last_action_ts: float = 0.0

    # ---- public lifecycle --------------------------------------------

    def start(self, directive: dict[str, Any]) -> None:
        """Arm the director for a new farm directive. Idempotent if the
        same directive is already active (e.g. goal_manager re-dispatches)."""
        if self.state not in ('idle', 'completed', 'failed'):
            if self.directive == directive:
                return
        self.directive = directive
        self.kills = 0
        self._enter('acquire')
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

    # ---- state machine -----------------------------------------------

    def _enter(self, new_state: str) -> None:
        if new_state == self.state:
            return
        self.state = new_state
        self._state_entered_at = time.time()
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
        # Per-state timeout — never wedged for more than STATE_TIMEOUT_S.
        if now - self._state_entered_at > STATE_TIMEOUT_S and self.state != 'resting':
            print(f'  farming: state {self.state} timed out — bouncing to acquire')
            self._enter('acquire')

        if self.state == 'acquire':
            self._tick_acquire(snap, now)
        elif self.state == 'engage':
            self._tick_engage(snap, now)
        elif self.state == 'killed':
            self._tick_killed(snap, now)
        elif self.state == 'resting':
            self._tick_resting(snap, now)

    def _tick_acquire(self, snap: _Snapshot, now: float) -> None:
        target_name = (self.directive or {}).get('target_name')
        if not target_name:
            self._enter('failed')
            return
        # Already locked onto the right mob? Engage.
        if snap.target_name == target_name and snap.target_alive:
            self._enter('engage')
            return
        # Issue /ta no more than once a second to avoid spamming chat.
        if now - self._last_action_ts >= 1.0:
            self._issue(f'/ta "{target_name}"')
            self._last_action_ts = now
        # If /ta hasn't surfaced our mob within TARGET_LOCK_TIMEOUT_S,
        # don't escalate yet — just keep retrying. The mob may be respawning
        # or may need physical proximity. The state-level timeout above
        # eventually breaks any wedge.

    def _tick_engage(self, snap: _Snapshot, now: float) -> None:
        # If the target is gone or dead, transition to killed. Note: when
        # a mob dies its addon record disappears, so target_name flipping
        # to None is a kill signal too.
        target_name = (self.directive or {}).get('target_name')
        if snap.target_name != target_name:
            self._enter('killed')
            return
        if snap.target_alive is False or (snap.target_hp_pct is not None and snap.target_hp_pct == 0):
            self._enter('killed')
            return
        # Make sure /attack is on. The combat addon publishes engaged
        # status so we know whether the auto-attack flag is set.
        if not snap.engaged and now - self._last_action_ts >= 1.0:
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
        self._enter('acquire')

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
            self._enter('acquire')
        # Re-issue /heal occasionally in case combat or movement broke it.
        elif snap.self_status != 33:  # 33 = resting per Ashita
            self._issue('/heal')
