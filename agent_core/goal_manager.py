"""Goal manager - picks the active leaf goal and dispatches it.

The persistent goal tree lives in `persistent/<char>/goals.json` (loaded
via `persistence.Goals`). Each tick the manager:
    1. scans the tree DFS for the first leaf in `pending` or `active` state,
    2. if that leaf has no in-flight directive or its directive has been
       satisfied, dispatches the next directive (or marks it complete),
    3. updates the on-disk file when state changes.

Phase 2a scope: deterministic execution - no LLM in this loop. The
LLM planner (Phase 2b) writes / mutates the goal tree; the manager
just walks it. Goals supported in 2a:

    type=travel         { target_zone: int, target_pos: [x, y, z]? }
    type=goto           { target_pos: [x, y, z] }                       (same-zone)
    type=composite      no directive; container for subgoals
    type=wait           { seconds: float }                              (debug)

Each goal also carries an optional `completion` block that defines how
to detect completion (`type=in_zone, zone_id`, `type=near_pos, ...`).
If `completion` is omitted we infer it from `type` (travel->arrival in
target_zone, goto->within 5y of target_pos, etc.).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config as _config
from . import events as _events
from . import persistence as _persistence


# How close to a target position counts as "arrived" in same-zone gotos.
ARRIVAL_RADIUS_Y = 8.0


@dataclass
class _Snapshot:
    """The bits of world state the goal manager actually needs each tick.

    Pulled from the existing nav_status.json the addon publishes - when
    the IPC migration lands (Phase 1b) this will read state/<char>/nav.json
    instead, but the shape of the data we care about is the same.
    """
    zone_id: int | None
    x: float | None
    y: float | None
    z: float | None
    moving: bool
    # slot_name -> equipped item name; None when inventory.json hasn't
    # published yet. Slots without an equipped item map to None.
    # Used by `equip` goal completion detection.
    equipped: dict[str, str | None] | None = None


class GoalManager:
    """Walks the persistent goal tree and dispatches directives via a
    caller-provided dispatch function (so the manager doesn't depend on
    NavServer directly - easier to test, easier to swap the dispatch
    target if/when we move off nav_request.json)."""

    # Hard cap between two redispatches of the same leaf, even when
    # we believe a redispatch is justified (zone change, no nav status,
    # etc.). Stops tight loops if some condition flickers.
    REDISPATCH_COOLDOWN_S = 5.0

    def __init__(
        self,
        cfg: _config.Config,
        dispatch_goto: Callable[[dict[str, Any]], None],
        snapshot_provider: Callable[[], _Snapshot],
        farming_director: Any | None = None,
        issue_command: Callable[[str], None] | None = None,
        interact_director: Any | None = None,
    ):
        self.cfg = cfg
        self._dispatch = dispatch_goto
        self._snapshot = snapshot_provider
        # `equip` leaves issue raw FFXI /commands via cmdrelay rather
        # than going through nav_request.json. None disables `equip`
        # (those leaves immediately complete as no-ops).
        self._issue_command = issue_command
        # Interact director: state machine for interact_npc / vendor_*
        # / home_point leaves. Optional for the same reason farming
        # is optional - older configs without the addon shouldn't wedge.
        self.interact_director = interact_director
        self._goals_path = cfg.paths.persistent_dir(cfg.character) / 'goals.json'
        self.goals = _persistence.Goals.load(self._goals_path)
        # Per-leaf bookkeeping for redispatch decisions:
        #   _last_dispatch[gid]      -> wall-clock of last dispatch
        #   _dispatched_zone[gid]    -> zone_id we dispatched FROM
        # We only redispatch a leaf if the player has crossed a zone
        # boundary since the previous dispatch (route-segment changed)
        # or if the cooldown has elapsed and we still have no path
        # (recovery from a missed response).
        self._last_dispatch: dict[str, float] = {}
        self._dispatched_zone: dict[str, int | None] = {}
        # Cache of the active leaf id so external callers can ask without
        # re-walking the tree.
        self._active_leaf_id: str | None = None
        # One-shot signal: set to the goal id of any leaf that just
        # transitioned to `failed`. Consumed by main's failure-replan
        # poll, which calls `consume_failure_signal()` to read+clear.
        # Distinct from completion - failure is the trigger because the
        # agent's plan visibly stopped working and needs LLM re-evaluation.
        self._failed_leaf_signal: str | None = None
        # Farming director (Phase 3c). Optional; if absent, `farm` leaves
        # complete immediately as a no-op so older configs don't wedge.
        self.farming = farming_director

    def consume_failure_signal(self) -> str | None:
        """Return the id of the most recently failed leaf, clearing the
        latch in the process. None if no failure pending. Caller (main)
        decides what to do - typically a failure-triggered replan."""
        sig = self._failed_leaf_signal
        self._failed_leaf_signal = None
        return sig

    # ---- tree helpers --------------------------------------------------

    def _node(self, gid: str) -> dict[str, Any] | None:
        return self.goals.nodes.get(gid)

    def _children(self, gid: str) -> list[str]:
        n = self._node(gid)
        return list(n.get('subgoals', [])) if n else []

    def _is_leaf(self, gid: str) -> bool:
        return not self._children(gid)

    def _find_active_leaf(self) -> str | None:
        """DFS through `roots`, return the first leaf that is pending or
        active (skipping completed/failed/abandoned). Composite parents
        whose children are all done auto-advance to `completed` here."""
        def visit(gid: str) -> str | None:
            node = self._node(gid)
            if node is None:
                return None
            state = node.get('state', 'pending')
            if state in ('completed', 'failed', 'abandoned'):
                return None
            if self._is_leaf(gid):
                return gid
            unresolved_children = []
            for cid in self._children(gid):
                cnode = self._node(cid)
                if cnode is None:
                    continue
                if cnode.get('state') in ('completed', 'failed', 'abandoned'):
                    continue
                unresolved_children.append(cid)
            if not unresolved_children:
                # All children terminal - propagate failure if any child
                # failed (a composite isn't "completed" if its subgoal
                # died). Otherwise the parent is completed.
                child_states = [
                    (self._node(cid) or {}).get('state', 'pending')
                    for cid in self._children(gid)
                ]
                terminal = 'failed' if 'failed' in child_states else 'completed'
                node['state'] = terminal
                self._save()
                _events.append(
                    self.cfg.paths.events_file(),
                    character=self.cfg.character,
                    source='goal_manager',
                    type_=f'goal_{terminal}',
                    goal_id=gid,
                    title=node.get('title', ''),
                )
                return None
            for cid in unresolved_children:
                hit = visit(cid)
                if hit is not None:
                    return hit
            return None

        for rid in self.goals.roots:
            hit = visit(rid)
            if hit is not None:
                return hit
        return None

    def _save(self) -> None:
        self.goals.save(self._goals_path)

    # ---- completion detection -----------------------------------------

    def _is_complete(self, leaf: dict[str, Any], snap: _Snapshot) -> bool:
        completion = leaf.get('completion')
        if completion is None:
            # Infer from type.
            t = leaf.get('type', '')
            if t == 'travel':
                tz = leaf.get('target_zone')
                tp = leaf.get('target_pos')
                if snap.zone_id != tz:
                    return False
                if tp is None:
                    return True
                if snap.x is None or snap.y is None:
                    return False
                dx = snap.x - tp[0]
                dy = snap.y - tp[1]
                return (dx * dx + dy * dy) ** 0.5 < ARRIVAL_RADIUS_Y
            if t == 'goto':
                tp = leaf.get('target_pos')
                if tp is None or snap.x is None or snap.y is None:
                    return False
                if leaf.get('target_zone') is not None and snap.zone_id != leaf['target_zone']:
                    return False
                dx = snap.x - tp[0]
                dy = snap.y - tp[1]
                return (dx * dx + dy * dy) ** 0.5 < ARRIVAL_RADIUS_Y
            if t == 'wait':
                started = leaf.get('_started_at')
                if started is None:
                    return False
                return time.time() - started >= leaf.get('seconds', 0)
            if t in ('farm', 'engage_nearby'):
                # The farming director marks itself completed when the
                # `stop_when` directive fires (e.g. kill_count reached).
                return self.farming is not None and self.farming.is_done()
            if t == 'interact_npc':
                # The interact director runs the dialog state machine
                # and marks itself completed/failed when the menu
                # closes (or the completion clause matches). Same
                # contract as farming.is_done().
                return (self.interact_director is not None
                        and self.interact_director.is_done())
            if t == 'equip':
                # Done when the next inventory snapshot reflects the
                # /equip we issued. equipped[slot] either (a) hasn't
                # published yet -> wait, or (b) doesn't match -> wait,
                # (c) matches -> done. We also bail with success after
                # the dispatch timeout to avoid wedging on items that
                # silently failed to equip (lvl_req mismatch, etc.) -
                # the LLM can re-plan after observing the unchanged state.
                eq = (snap.equipped or {}).get(leaf.get('slot'))
                if eq == leaf.get('item'):
                    return True
                started = leaf.get('_started_at')
                if started is not None and time.time() - started >= 5.0:
                    # Timeout: mark complete-with-warning. The leaf
                    # records the effective outcome on disk so a
                    # re-plan can see what didn't take.
                    leaf['_equip_outcome'] = 'timeout'
                    return True
                return False
            return False
        # Explicit completion clauses.
        ctype = completion.get('type')
        if ctype == 'in_zone':
            return snap.zone_id == completion.get('zone_id')
        if ctype == 'near_pos':
            tp = completion.get('pos')
            if tp is None or snap.x is None or snap.y is None:
                return False
            r = completion.get('radius', ARRIVAL_RADIUS_Y)
            dx = snap.x - tp[0]
            dy = snap.y - tp[1]
            return (dx * dx + dy * dy) ** 0.5 < r
        return False

    # ---- dispatch -----------------------------------------------------

    def _should_dispatch(self, gid: str, snap: _Snapshot, now: float) -> bool:
        """Decide whether to (re)dispatch this leaf's directive on this
        tick. Dispatch once per leaf-activation, period.

        The addon already handles all in-flight concerns: cross-zone
        segment progression, stuck-detection, wider-radius retry, even
        random walks on persistent failure (see nav.lua's retry chain).
        Once we hand it a cross_zone_goto with a route, our job is to
        watch for completion and advance to the next leaf - not to
        keep poking the addon."""
        return self._last_dispatch.get(gid) is None

    def _dispatch_leaf(self, gid: str, leaf: dict[str, Any], snap: _Snapshot) -> None:
        t = leaf.get('type', '')
        now = time.time()
        if t == 'wait':
            if '_started_at' not in leaf:
                leaf['_started_at'] = now
                self._save()
            return
        if t == 'composite':
            return  # nothing to dispatch directly
        if t == 'equip':
            # One-shot: fire `/equip <slot> "<item>"` once, stamp _started_at
            # for completion's timeout fallback, and let the inventory
            # snapshot drive completion.
            if not self._should_dispatch(gid, snap, now):
                return
            slot = leaf.get('slot')
            item = leaf.get('item')
            if slot and item and self._issue_command is not None:
                # Quote the name - FFXI item names can contain spaces and
                # apostrophes; the client's parser handles "..." fine.
                self._issue_command(f'/equip {slot} "{item}"')
                leaf['_started_at'] = now
                self._last_dispatch[gid] = now
                self._dispatched_zone[gid] = snap.zone_id
                self._save()
            return
        if t in ('farm', 'engage_nearby'):
            # Farm + engage_nearby both run through the farming director
            # - same start/tick contract, just different acquire behavior
            # (named target vs /ta <enemy>). Single hand-off; director
            # drives the kill loop tick-by-tick from there.
            if not self._should_dispatch(gid, snap, now):
                return
            if self.farming is not None:
                self.farming.start(leaf)
                self._last_dispatch[gid] = now
                self._dispatched_zone[gid] = snap.zone_id
            return
        if t == 'interact_npc':
            # Single hand-off to the interact director; same contract
            # as farming. The director walks the dialog script and
            # marks done when the menu closes / completion fires.
            if not self._should_dispatch(gid, snap, now):
                return
            if self.interact_director is not None:
                self.interact_director.start(leaf)
                self._last_dispatch[gid] = now
                self._dispatched_zone[gid] = snap.zone_id
            return
        if not self._should_dispatch(gid, snap, now):
            return
        if t == 'travel':
            target_zone = leaf.get('target_zone')
            if target_zone is None or snap.x is None:
                return
            req: dict[str, Any] = {
                'action': 'cross_zone_goto',
                'zone_id': snap.zone_id,
                'target_zone': target_zone,
                'player': [snap.x, snap.y, snap.z],
                'seq': int(now * 1000),
                # Orchestrator-pushed travel - addon must drop any
                # in-flight retry/route state from prior goals before
                # following this one.
                'reset_state': True,
            }
            if leaf.get('target_pos') is not None:
                req['target'] = leaf['target_pos']
            self._dispatch(req)
        elif t == 'goto':
            target_pos = leaf.get('target_pos')
            if target_pos is None or snap.x is None:
                return
            req = {
                'action': 'goto',
                'zone_id': snap.zone_id,
                'player': [snap.x, snap.y, snap.z],
                'target': target_pos,
                'seq': int(now * 1000),
                'reset_state': True,
            }
            self._dispatch(req)
        self._last_dispatch[gid] = now
        self._dispatched_zone[gid] = snap.zone_id

    # ---- the loop -----------------------------------------------------

    def tick(self) -> None:
        """One tick of the goal loop. Cheap; safe to call every poll."""
        if not self.goals.roots:
            self._active_leaf_id = None
            return
        snap = self._snapshot()
        leaf_id = self._find_active_leaf()
        # Detect transition into a new leaf.
        if leaf_id != self._active_leaf_id:
            if leaf_id is not None:
                _events.append(
                    self.cfg.paths.events_file(),
                    character=self.cfg.character,
                    source='goal_manager',
                    type_='goal_active',
                    goal_id=leaf_id,
                    title=(self._node(leaf_id) or {}).get('title', ''),
                )
            self._active_leaf_id = leaf_id
        if leaf_id is None:
            # No active leaf: if the farming director was running for a
            # previous leaf, tell it to stop so /attack doesn't keep
            # firing after the goal tree was cleared.
            if self.farming is not None and self.farming.is_active():
                self.farming.stop()
            # Same for interact - leaving a half-walked menu open in
            # the client blocks all future input, so close it on
            # leaf-clear.
            if self.interact_director is not None \
                    and self.interact_director.is_active():
                self.interact_director.stop()
            return
        leaf = self._node(leaf_id)
        if leaf is None:
            return
        # Lazily mark the leaf as active so the dashboard / event log
        # reflect what we're currently working on.
        if leaf.get('state', 'pending') == 'pending':
            leaf['state'] = 'active'
            self._save()
        # The farming director is a long-running state machine. Tick it
        # before the completion check so a kill that just happened gets
        # counted before we evaluate stop_when.
        if leaf.get('type') in ('farm', 'engage_nearby') \
                and self.farming is not None and self.farming.is_active():
            self.farming.tick()
        # Same pattern for the interact director - tick before checking
        # completion so a freshly-closed menu transitions to completed
        # this same tick rather than next.
        if leaf.get('type') == 'interact_npc' \
                and self.interact_director is not None \
                and self.interact_director.is_active():
            self.interact_director.tick()
        if self._is_complete(leaf, snap):
            # Distinguish completed vs failed for farm leaves so death
            # (FarmingDirector -> 'failed') doesn't masquerade as success.
            # `is_done()` covers both terminal states; `state == 'failed'`
            # is the disambiguator.
            terminal = 'completed'
            if leaf.get('type') in ('farm', 'engage_nearby') and self.farming is not None \
                    and getattr(self.farming, 'state', None) == 'failed':
                terminal = 'failed'
            if leaf.get('type') == 'interact_npc' and self.interact_director is not None \
                    and getattr(self.interact_director, 'state', None) == 'failed':
                terminal = 'failed'
            leaf['state'] = terminal
            self._last_dispatch.pop(leaf_id, None)
            self._dispatched_zone.pop(leaf_id, None)
            self._save()
            if terminal == 'failed':
                # Latch the failure id so main's failure-replan poll
                # can pick it up. Overwriting an unconsumed signal is
                # fine - what matters is "something failed since you
                # last replanned," not which specific leaf.
                self._failed_leaf_signal = leaf_id
            _events.append(
                self.cfg.paths.events_file(),
                character=self.cfg.character,
                source='goal_manager',
                type_=f'goal_{terminal}',
                goal_id=leaf_id,
                title=leaf.get('title', ''),
            )
            return
        self._dispatch_leaf(leaf_id, leaf, snap)
