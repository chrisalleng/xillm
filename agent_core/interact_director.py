"""Interact director - state machine that drives an `interact_npc`
goal from start to completion.

The flow:
  1. LOCATE      look up the NPC in the per-zone entities catalog
                 (entities.py type=2 NPCs); if not found, fail.
  2. APPROACH    if more than INTERACT_DISTANCE_Y from the NPC,
                 dispatch a goto and wait for arrival. If we're
                 already close, skip straight to OPEN.
  3. OPEN        issue /target <sid>, then send open_dialog (an
                 Enter press to start the conversation). Wait for
                 menu.json to flip to open=true.
  4. SCRIPT      walk the script. Each tick:
                 - if menu has actionable options, pop the next
                   script entry and resolve to an option index;
                   on unique match send pick_option_by_index, on
                   no/ambiguous match fire menu_judge.
                 - if no actionable options (passive "OK", text
                   line, cutscene), send advance_text.
                 - if script exhausted but menu still open with
                   actionable options, fire menu_judge to walk
                   the rest.
  5. JUDGE       waiting on a menu_judge rid. On verdict:
                 pick -> send pick_option_by_index, back to SCRIPT.
                 abort -> send close_menu, fail.
                 error -> fail.
  6. COMPLETED   completion clause met (default: menu closed and
                 script exhausted; or zone_changed; or
                 key_item_received - latter two land later).
  7. FAILED      timeout / NPC missing / judge unavailable / etc.

Mirrors FarmingDirector's start/stop/tick/is_done/is_active surface
so the goal manager can drive it the same way (`farming` directive
type bridges to FarmingDirector; `interact_npc` to this).
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import config as _config
from . import echo as _echo
from . import entities as _entities
from . import events as _events
from . import input_driver as _input
from . import interact as _interact
from . import menu_judge as _menu_judge


# How close (yalms) to the NPC we need to be before /target + open
# will land. FFXI's interaction range is around 6y for most NPCs; we
# add a small buffer for the path's stop precision.
INTERACT_DISTANCE_Y = 6.0

# Hard ceiling per state. Stuck states (NPC walking pattern, dialog
# server-side delay, judge call timeout) shouldn't wedge a leaf.
STATE_TIMEOUT_S = 60.0

# How long to wait for menu.json to flip open=true after we send
# open_dialog. Generous - first-touch NPC dialogs sometimes take
# 1-2s of cutscene fade-in before the menu materializes.
DIALOG_OPEN_TIMEOUT_S = 8.0

# Min gap between script-step actions. Menus have generous server
# tolerance but firing actions every tick (~5Hz) overflows the input
# buffer on some shop interfaces. 0.5s feels human and safe.
# Min gap between script-step actions. With the memory-only submit
# (interact.lua's submit_menu_pick writing +0x548), each action
# triggers FFXI's per-frame menu update which sends the 0x05B
# packet, gets server response, allocates new menu_base struct, and
# repopulates state. Round-trip time on a local LSB server is
# typically 0.5-1.5 seconds; remote/lagged servers can be longer.
# 2.0s is a safe default that still lets a 7-step purchase complete
# in ~14s. Per-step `{sleep: secs}` entries can override locally if
# a particular menu transition needs more time.
ACTION_GAP_S = 2.0

# Minimum delay between dialog open (event begin received) and the
# FIRST script pick. The FFXI server used to silently drop event-
# response packets that arrived too soon after the event begin
# (captured a manual signet pick where the user took 6 seconds to
# read the dialog before clicking). With the +0x548 submit path
# (cursor_struct memory write) the client's per-frame loop handles
# the timing for us, so a tighter delay is safe. 2 seconds keeps
# things responsive while still letting the addon publish a fresh
# menu.json after the open packet round-trip.
POST_OPEN_READ_S = 2.0

# After a script-resolved pick fires, wait this long for the server
# to respond (close the event via 0x052) before falling back to the
# LLM judge. Without this, the director sees `menu still open AND
# script empty` on the very next tick and fires the judge - which
# then aborts because options[] is empty (we don't capture option
# labels client-side). Tunable: 4s is a comfortable round-trip even
# on a busy server; menu picks that fail to close after 4s really
# DO need the judge.
POST_PICK_GRACE_S = 4.0


class InteractDirector:
    """One instance shared across all interact_npc leaves. The active
    leaf's directive replaces any previous one (you can only be in
    one dialog at a time)."""

    def __init__(self, cfg: _config.Config,
                 interact: _interact.InteractDriver,
                 menu_judge: _menu_judge.MenuJudge,
                 player_snapshot_provider: Callable[[], Any],
                 dispatch_goto: Callable[[dict[str, Any]], None],
                 issue_command: Callable[[str], None],
                 user_goal_provider: Callable[[], str] | None = None,
                 research_notes_provider: Callable[[], str | None] | None = None,
                 input_driver: _input.InputDriver | None = None):
        self.cfg = cfg
        self.interact = interact
        self.menu_judge = menu_judge
        # Client-input driver. Memory-write driver: writes cursor
        # state into FFXI's client memory then sends the pick packet
        # via interact.lua. The action handler reads cursor from
        # memory mirrors (verified 2026-05-01), so the server-side
        # action resolves to the right option even though the visible
        # cursor highlight may lag. With mode=End on the final pick
        # the server sends event_end and the client closes the dialog.
        self.input_driver = input_driver or _input.InputDriver(interact)
        self._player_snapshot = player_snapshot_provider
        self._dispatch_goto = dispatch_goto
        self._issue_command = issue_command
        self._user_goal = user_goal_provider or (lambda: '')
        self._research_notes = research_notes_provider or (lambda: None)

        self.state: str = 'idle'
        self.directive: dict[str, Any] | None = None
        self._npc_record: dict[str, Any] | None = None
        # Remaining script entries (pop from front as we consume them).
        self._script: list[Any] = []
        # menu_judge rid we're polling on, if any.
        self._judge_rid: int | None = None
        self._judge_pick_index: int | None = None  # cached after pick
        # Timestamps for state-timeout and action-gap enforcement.
        self._state_entered_at: float = 0.0
        self._last_action_at: float = 0.0
        self._dialog_opened_at: float | None = None
        # When the most recent SCRIPT-RESOLVED pick was sent. While
        # this is recent (within POST_PICK_GRACE_S), we don't fire
        # menu_judge - we give the server a chance to close the event
        # in response to the pick.
        self._last_script_pick_at: float = 0.0
        # Already-dispatched goto for this leaf - don't re-dispatch on
        # every tick while waiting for arrival.
        self._goto_dispatched: bool = False

    # ---- public ------------------------------------------------------

    def start(self, directive: dict[str, Any]) -> None:
        if self.state not in ('idle', 'completed', 'failed'):
            if self.directive == directive:
                return
        self.directive = directive
        self._npc_record = None
        self._script = list(directive.get('script') or [])
        self._judge_rid = None
        self._judge_pick_index = None
        self._goto_dispatched = False
        self._dialog_opened_at = None
        # Wall-clock when this leaf began. Used by _tick_open to
        # distinguish a freshly-opened menu from a stale menu.json
        # entry left over from a previous (failed/aborted) session.
        # Without this guard, a leftover open=true on disk would
        # cause _tick_open to skip the actual /target+A press and
        # the script would run blind against a non-existent menu.
        self._directive_started_at: float = time.time()
        self._enter('locate')
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='interact_director',
            type_='interact_started',
            directive=directive,
        )

    def stop(self) -> None:
        # Best-effort close of any open menu - if we got pulled mid-
        # dialog, leaving the menu open in-game blocks all future input.
        if self.interact.is_menu_open():
            self.interact.close_menu()
        self._enter('idle')

    def is_done(self) -> bool:
        return self.state in ('completed', 'failed')

    def is_active(self) -> bool:
        return self.state not in ('idle', 'completed', 'failed')

    # ---- state transitions ------------------------------------------

    def _enter(self, new_state: str) -> None:
        old = self.state
        self.state = new_state
        self._state_entered_at = time.time()
        # Reset the "have we acted this state" timer when transitioning
        # to a state whose tick uses _last_action_at as a "fire once"
        # gate. Without this reset the gate would carry value from a
        # previous state and the new state's first tick would skip
        # its action.
        if new_state in ('open',):
            self._last_action_at = 0.0
        if old != new_state:
            _events.append(
                self.cfg.paths.events_file(),
                character=self.cfg.character,
                source='interact_director',
                type_='state',
                state=new_state,
            )

    def _state_age(self) -> float:
        return time.time() - self._state_entered_at

    def _fail(self, reason: str) -> None:
        _echo.to_chat(self.cfg, 'interact', f'leaf failed: {reason}')
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='interact_director',
            type_='failed',
            reason=reason,
            state_at_fail=self.state,
        )
        # Best-effort menu cleanup. Memory-write driver routes
        # press_escape through interact.close_menu (0x05B Mode=End
        # for the captured event_id), which closes both server-side
        # and client-side when cursor state has been kept in sync.
        try:
            self.input_driver.press_escape()
        except Exception:
            pass
        self._enter('failed')

    # ---- helpers ----------------------------------------------------

    def _player(self) -> Any:
        return self._player_snapshot()

    def _distance_to(self, x: float, y: float) -> float | None:
        snap = self._player()
        if snap is None or snap.x is None or snap.y is None:
            return None
        dx = snap.x - x
        dy = snap.y - y
        return (dx * dx + dy * dy) ** 0.5

    def _find_inventory_slot(self, name: str) -> tuple[int | None, int]:
        """Look up `name` in the player's main inventory and return
        (slot, item_id). Slot is the server-side inventory slot
        (1..30); item_id is the FFXI item id needed for the 0x084
        body. Match is case-insensitive on the inventory addon's
        name field. Returns (None, 0) if not found."""
        path = self.cfg.paths.state_dir(self.cfg.character) / 'inventory.json'
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None, 0
        items = (d.get('containers', {})
                 .get('inventory', {})
                 .get('items') or [])
        target = name.strip().lower()
        for item in items:
            if (item.get('name') or '').strip().lower() == target:
                return int(item.get('slot') or 0), int(item.get('id') or 0)
        return None, 0

    # ---- tick -------------------------------------------------------

    def tick(self) -> None:
        if self.directive is None or not self.is_active():
            return
        # Hard timeout per state. Long enough that legitimate slow
        # cutscenes don't trip it; short enough that a stuck dialog
        # surfaces as a failed leaf for replan rather than wedging.
        if self._state_age() > STATE_TIMEOUT_S:
            self._fail(f'timeout in state {self.state!r}')
            return

        if self.state == 'locate':
            self._tick_locate()
        elif self.state == 'approach':
            self._tick_approach()
        elif self.state == 'open':
            self._tick_open()
        elif self.state == 'script':
            self._tick_script()
        elif self.state == 'judge':
            self._tick_judge()

    # ---- per-state --------------------------------------------------

    def _tick_locate(self) -> None:
        d = self.directive or {}
        # Accept the canonical key first, then planner-LLM aliases.
        # The deliberative model frequently emits target_name /
        # target_npc instead of npc_name; rejecting on those forced a
        # failure_replan loop. Aliasing them here is cheaper than
        # teaching the prompt every variant.
        npc_name = (d.get('npc_name') or d.get('target_name')
                    or d.get('target_npc') or '').strip()
        if not npc_name:
            self._fail('directive missing npc_name')
            return
        snap = self._player()
        if snap is None or snap.zone_id is None:
            return  # wait for nav snapshot
        # npc_zone is canonical (an int zone id). Aliases:
        #   target_zone (int) - sometimes emitted by the LLM
        #   target_zone_name / npc_zone_name (str) - resolved via
        #     menu_catalog lookup. Falls back to current zone if the
        #     name doesn't match any known zone.
        target_zone = d.get('npc_zone') or d.get('target_zone')
        if target_zone is None:
            zone_name_alias = (d.get('target_zone_name')
                               or d.get('npc_zone_name'))
            if zone_name_alias:
                try:
                    from . import menu_catalog as _mc
                    target_zone = _mc.zone_id_by_name(self.cfg, str(zone_name_alias))
                except Exception:
                    target_zone = None
        if target_zone is None:
            target_zone = snap.zone_id
        if target_zone != snap.zone_id:
            # The NPC lives in a different zone - this directive
            # should have been preceded by a travel goal. Fail; the
            # planner will re-emit with travel first.
            self._fail(
                f'NPC {npc_name!r} expected in zone {target_zone}, '
                f'agent is in zone {snap.zone_id}')
            return
        # Pick the closest record matching the name. find_mobs is the
        # generic non-player entity finder - returns NPCs (type=2)
        # along with mobs, but the LLM should not be naming mobs in
        # interact_npc directives so substring match is fine.
        candidates = _entities.find_mobs(self.cfg, snap.zone_id, npc_name)
        if candidates:
            rec = _entities.closest_mob(
                self.cfg, snap.zone_id, npc_name,
                snap.x or 0.0, snap.y or 0.0,
            )
            self._npc_record = rec or candidates[0]
            self._enter('approach')
            return

        # Fallback to the LSB-derived NPC catalog. The live entities
        # catalog only contains NPCs the agent has personally observed
        # (populated via 0x05B widescan results), which fails for
        # planner-driven first visits. The LSB catalog has every
        # server-known NPC with stable npcids (= server_id) and world
        # coordinates baked in, so we can synthesize a usable record
        # without prior in-game observation.
        #
        # Two lookup strategies, in order:
        #  1. Substring name match: handles the common case where the
        #     planner correctly emits an NPC's polutils-form name from
        #     find_npc results ("Rabid Wolf, I.M.").
        #  2. Role-tag interpretation: if the planner emits a role
        #     description instead ("Conquest Overseer", "Vendor"),
        #     translate to the role index keys (lowercase, underscored)
        #     and pick the closest matching NPC in this zone. This is
        #     a fallback for when the LLM's output drifts from the
        #     prompt requirement to use specific names.
        try:
            from . import nav_router as _nr
            cat = _nr.NPCCatalog(self.cfg.paths.npcs_file)
            matches = cat.find(name_contains=npc_name, zone_id=snap.zone_id, limit=10)
            if not matches:
                role_key = (
                    npc_name.lower()
                            .replace("'", '')
                            .replace(',', '')
                            .replace('.', '')
                            .strip()
                            .replace(' ', '_')
                )
                if role_key in cat.role_tags:
                    matches = cat.find(role=role_key, zone_id=snap.zone_id, limit=20)
                    if matches:
                        print(f'  interact_director: directive npc_name={npc_name!r} '
                              f'matched role tag {role_key!r}; resolving to '
                              f'{matches[0].get("name")!r} (closest in zone)')
        except Exception as e:
            print(f'  interact_director: LSB catalog lookup failed: {e}')
            matches = []
        if matches:
            # Closest by 2D distance from current position.
            cx = snap.x or 0.0
            cz = snap.z or 0.0
            def dist2(n: dict[str, Any]) -> float:
                dx = (n.get('x') or 0.0) - cx
                dz = (n.get('z') or 0.0) - cz
                return dx*dx + dz*dz
            best = sorted(matches, key=dist2)[0]
            # Synthesize a record matching the entities-catalog shape
            # (center_x/y/z, server_id). `server_id` from npc_list.sql
            # IS the same value the client's 0x01A Talk packet targets
            # (verified: Rabid Wolf, I.M. = 17739828 = npcid in SQL =
            # target in live packet capture).
            #
            # Coordinate convention swap: LSB SQL uses (pos_x, pos_y,
            # pos_z) where pos_y = elevation and pos_z = north-south.
            # Ashita / nav addon / entities catalog uses (x, y, z)
            # where y = north-south and z = elevation. So we map
            # LSB(x, y, z) -> Ashita(x, z, y). Verified against the
            # live entities catalog record for Rabid Wolf (LSB SQL
            # = (-346.354, -10.002, -184.252); live catalog center_y
            # = -184.252, center_z = -10.002).
            lsb_x = best.get('x') or 0.0
            lsb_y = best.get('y') or 0.0
            lsb_z = best.get('z') or 0.0
            ashita_x = lsb_x
            ashita_y = lsb_z
            ashita_z = lsb_y
            self._npc_record = {
                'name':      best.get('name') or best.get('lua_name'),
                'server_id': best.get('id'),
                'center_x':  ashita_x,
                'center_y':  ashita_y,
                'center_z':  ashita_z,
                'min_x':     ashita_x, 'max_x': ashita_x,
                'min_y':     ashita_y, 'max_y': ashita_y,
                'zone_id':   best.get('zone_id'),
                'type':      2,  # NPC
                '_source':   'lsb_catalog',
            }
            self._enter('approach')
            return

        self._fail(f'no entity matching {npc_name!r} in zone {snap.zone_id} '
                   f'(neither live catalog nor LSB-derived catalog has it)')

    def _tick_approach(self) -> None:
        rec = self._npc_record
        if rec is None:
            self._fail('lost NPC record')
            return
        cx = rec.get('center_x')
        cy = rec.get('center_y')
        cz = rec.get('center_z') or 0.0
        if cx is None or cy is None:
            self._fail('NPC record has no position')
            return
        dist = self._distance_to(cx, cy)
        if dist is None:
            return  # wait for nav snapshot
        if dist <= INTERACT_DISTANCE_Y:
            self._enter('open')
            return
        if not self._goto_dispatched:
            snap = self._player()
            if snap is None or snap.x is None:
                return
            self._dispatch_goto({
                'action':   'goto',
                'zone_id':  snap.zone_id,
                'player':   [snap.x, snap.y, snap.z or 0.0],
                'target':   [float(cx), float(cy), float(cz)],
                'seq':      int(time.time() * 1000),
                'reset_state': True,
            })
            self._goto_dispatched = True

    def _tick_open(self) -> None:
        # ALWAYS fire /combat target + 0x01A Talk on the first tick
        # of the open state, regardless of what the addon's menu.json
        # says. The addon's publish loop refreshes menu.json's ts
        # every frame even when the menu state is stale (known bug
        # in the addon's menu-close detection), so the freshness
        # check we tried earlier was unreliable. Without firing the
        # open action, the script would run against a non-existent
        # client menu (every submit_menu_pick fails with "menu
        # pointer chain broken").
        #
        # If the menu was somehow already open (e.g. user manually
        # opened the dialog before the agent ran, an edge case),
        # firing Talk again is mostly harmless - the server will
        # close the previous event and start a new one. The new
        # 0x034 will refresh client state cleanly.
        rec = self._npc_record
        if rec is None:
            self._fail('lost NPC record')
            return
        sid = int(rec.get('server_id') or 0)
        # First tick of open state: fire /combat target + 0x01A Talk.
        # /combat target is async via cmdrelay (combat addon polls
        # cmd_inbox.txt at ~10Hz). 0x01A Talk via the interact addon's
        # QueuePacket - server then sends 0x032/0x034 event begin
        # which interact.lua captures into menu.json (open=true).
        if self._last_action_at == 0.0:
            self._issue_command(f'/combat target {sid}')
            self.interact.open_dialog(sid)
            self.input_driver.reset_cursor()
            self._last_action_at = time.time()
            return  # let next tick check for menu_open

        # After our open action fired, wait for the addon's menu.json
        # to publish a FRESH open=true (ts >= when we fired). Without
        # the freshness anchor, we'd false-positive on stale state
        # left over from a previous interact_npc leaf.
        menu = self.interact.current_menu() or {}
        menu_ts = float(menu.get('ts') or 0)
        if menu.get('open') and menu_ts >= self._last_action_at:
            if self._dialog_opened_at is None:
                self._dialog_opened_at = time.time()
            self._enter('script')
            return

        if self._state_age() > DIALOG_OPEN_TIMEOUT_S:
            self._fail(
                f'menu did not open within {DIALOG_OPEN_TIMEOUT_S}s '
                f'(NPC sid={sid}, target_idx={rec.get("server_id")} - '
                f'check that the NPC is actually targeted in-game and '
                f'within interact range)')

    def _tick_script(self) -> None:
        # Menu closed - we're done with the dialog phase.
        if not self.interact.is_menu_open():
            self._complete_or_continue()
            return

        # Vendor sell short-circuit. When the directive carries a
        # `sell_item`, we don't need the LLM judge - the planner has
        # already decided what to sell. Find the inventory slot, fire
        # the two-step 0x084+0x085 via interact.sell, mark complete.
        # Gated on kind=='vendor' so a directive that lands at a non-
        # vendor NPC fails loudly rather than silently sending sell
        # packets at the wrong server state.
        directive = self.directive or {}
        sell_item = directive.get('sell_item')
        if sell_item:
            menu = self.interact.current_menu() or {}
            if menu.get('kind') != 'vendor':
                self._fail(
                    f'sell_item directive but menu kind is '
                    f'{menu.get("kind")!r} (need vendor)')
                return
            slot, item_id = self._find_inventory_slot(str(sell_item))
            if slot is None:
                self._fail(
                    f'sell_item "{sell_item}" not found in inventory')
                return
            qty = int(directive.get('sell_qty') or 1)
            self.interact.sell(slot, item_id, qty)
            _echo.to_chat(
                self.cfg, 'interact',
                f'sell {sell_item} (slot={slot}, qty={qty})')
            self._last_action_at = time.time()
            self._enter('completed')
            return

        # Throttle: don't fire actions faster than ACTION_GAP_S. The
        # menu state on disk lags the in-game UI by up to one tick of
        # the addon's publish loop, so over-firing leads to acting on
        # stale state.
        now = time.time()
        if (now - self._last_action_at) < ACTION_GAP_S:
            return

        # Read time: server silently discards picks that arrive too
        # soon after event begin. Wait long enough that our packet
        # cadence resembles a human reading the dialog before clicking.
        if self._dialog_opened_at is not None and \
                (now - self._dialog_opened_at) < POST_OPEN_READ_S:
            return

        menu = self.interact.current_menu() or {}
        opts = menu.get('options') or []

        # Text-frame auto-advance: only when we're SURE it's a text
        # frame (memory-derived kind via cursor_struct vtable match).
        # The packet-history kind="dialog" is too noisy to gate on -
        # menu transitions briefly publish empty options[] with kind
        # =dialog and we'd spam Enter mid-transition. Vtable match
        # is the authoritative signal: if the cursor_struct's class
        # is the confirmation/text-frame type, no options will ever
        # appear and Enter is the only valid action.
        kind = menu.get('kind') or 'unknown'
        no_real_options = (not opts) or all(not (s or '').strip() for s in opts)
        if kind == 'text_frame' and no_real_options:
            self.interact.advance_text()
            self._last_action_at = now
            self._last_script_pick_at = now
            _echo.to_chat(self.cfg, 'interact', 'advance text frame')
            return

        # Script-first: consume entries via virtual gamepad button
        # presses to FFXI's input handler. Schema for client-input
        # scripts:
        #   {"position": N}      - move cursor to option N (Down N times)
        #                          then press Enter. N=0 confirms the
        #                          default-selected option.
        #   "enter" | {"key": "enter"}
        #                        - just press Enter (advance dialog
        #                          text or confirm default option).
        #   {"key": "down" | "up" | "escape", "n": N}
        #                        - send N presses of the named key.
        #   {"sleep": secs}      - wait between actions (useful when
        #                          a multi-stage menu transitions
        #                          slower than the director's tick).
        #
        # Backwards compat: legacy {"index": N, "mode": M} entries
        # fall through to the packet-injection path that landed
        # earlier. New tests should prefer position-based entries.
        if self._script:
            entry = self._script.pop(0)
            handled = self._handle_script_entry(entry, menu, opts, now)
            if handled:
                return
            # Couldn't resolve - push back and try the LLM fallback.
            self._script.insert(0, entry)
        # Script empty or didn't resolve. Before firing the judge,
        # honor the post-pick grace window: a script entry sent with
        # mode=End triggers a server-side close, but the close packet
        # takes some time to arrive. Firing the judge before the
        # close lands would needlessly abort an otherwise-successful
        # pick.
        if (now - self._last_script_pick_at) < POST_PICK_GRACE_S:
            return
        # Don't fire the judge against an empty options list. After a
        # pick, there's a window where the new menu is opening but
        # widgets haven't populated yet (cursor_struct + 0x24 reads 0
        # for a few frames). The judge would receive zero options,
        # the LLM would refuse to pick, and the leaf would fail.
        # Just wait - either widgets show up (judge fires next tick)
        # or STATE_TIMEOUT_S kicks in.
        if no_real_options:
            return
        self._fire_menu_judge(menu)

    def _is_last_pick(self) -> bool:
        """True if this is the final pick of the script AND the
        leaf's completion is `menu_closed` - meaning we want the
        server to close the event and the client to dismiss the
        dialog. self._script has ALREADY been popped at this point,
        so an empty list means the entry we're handling was the
        last one. The completion-type check guards against multi-
        stage interactions where menu_closed isn't the success
        criterion (e.g. zone_changed for home points).

        In pure-LLM mode (the directive's original script was empty
        or absent), there's no script to count against, so every pick
        could be "last" by this naive check. That breaks multi-step
        navigation - we'd mark complete after the first pick, ignoring
        the sub-menu that just opened. Instead, return False so the
        director loops through the judge for each new menu and only
        completes when `_tick_script` sees `is_menu_open() == False`."""
        original_script = (self.directive or {}).get('script') or []
        if not original_script:
            return False
        if self._script:
            return False
        completion = (self.directive or {}).get('completion') or {'type': 'menu_closed'}
        return completion.get('type') == 'menu_closed'

    def _handle_script_entry(self, entry: Any, menu: dict[str, Any],
                             opts: list[str], now: float) -> bool:
        """Try to dispatch a single script entry. Returns True if
        handled (action sent, last_action_at updated), False if the
        entry shape isn't recognized and the caller should fall back
        to the LLM judge."""
        # Bare integer or {position: N, end?: bool, mode?: int} -
        # absolute pick. Memory-write driver writes cursor state and
        # sends the pick packet atomically. mode/end controls
        # whether the server keeps the event open (mode=1, default)
        # or closes it (mode=0, sets `end: true` on the script entry
        # of the FINAL pick of a multi-step interaction so the
        # client receives event_end and dismisses the dialog).
        position = None
        end = False
        if isinstance(entry, int):
            position = entry
        elif isinstance(entry, dict) and 'position' in entry:
            position = int(entry['position'])
            end = bool(entry.get('end'))
            # Explicit `mode` overrides `end`.
            if 'mode' in entry:
                end = int(entry.get('mode')) == 0
        if position is not None:
            mode = _input.MODE_END if end or self._is_last_pick() else _input.MODE_UPDATE
            ok = self.input_driver.select_menu_option(position, mode=mode)
            # Reset local cursor: the menu is about to transition
            # (server will respond with the next event or close).
            self.input_driver.reset_cursor()
            self._last_action_at = now
            self._last_script_pick_at = now
            mode_tag = ' [End]' if mode == _input.MODE_END else ''
            _echo.to_chat(
                self.cfg, 'interact',
                f'pick {position}{mode_tag} ({"sent" if ok else "failed"})',
            )
            # When this was the final pick (mode=End on a script-
            # exhausted menu_closed leaf), trust the server's event_end
            # response and mark the leaf complete. Without this we'd
            # wait for the addon's menu.json to flip open=false, which
            # has a known bug where it doesn't notice client-side
            # dismissals reliably.
            #
            # We deliberately do NOT call _cleanup_ui_overlay here -
            # the End-mode pick already triggers the server to close
            # the event and the client to dismiss the dialog; sending
            # an extra close_menu would clobber the pick action
            # in interact.json (atomic-write means latest wins) and
            # the actual packet to go out would be close_menu's, not
            # our pick. That bit us once (signet test 2026-05-01).
            if mode == _input.MODE_END and not self._script and \
                    ((self.directive or {}).get('completion') or {'type': 'menu_closed'}).get('type') == 'menu_closed':
                self._enter('completed')
            return True
        # {key: "enter"|"down"|"up"|"escape", n: N, end?: bool}
        if isinstance(entry, dict) and 'key' in entry:
            key = (entry.get('key') or '').lower()
            n = int(entry.get('n') or 1)
            end = bool(entry.get('end')) or self._is_last_pick()
            ok = False
            if key in ('enter', 'return'):
                ok = self.input_driver.press_enter(repeats=n, end=end)
            elif key == 'down':
                ok = self.input_driver.press_down(repeats=n)
            elif key == 'up':
                ok = self.input_driver.press_up(repeats=n)
            elif key in ('esc', 'escape'):
                ok = self.input_driver.press_escape(repeats=n)
            self._last_action_at = now
            self._last_script_pick_at = now
            mode_tag = ' [End]' if end and key in ('enter', 'return') else ''
            _echo.to_chat(self.cfg, 'interact',
                          f'key={key} x{n}{mode_tag} ({"sent" if ok else "failed"})')
            # End-mode confirm on script-exhausted menu_closed leaf:
            # trust the server's event_end response (see position
            # branch above for rationale, including why we don't
            # call _cleanup_ui_overlay here).
            if end and key in ('enter', 'return') and not self._script and \
                    ((self.directive or {}).get('completion') or {'type': 'menu_closed'}).get('type') == 'menu_closed':
                self._enter('completed')
            return True
        # Bare string shorthand: "enter".
        if isinstance(entry, str) and entry.lower() in ('enter', 'return'):
            ok = self.input_driver.press_enter()
            self._last_action_at = now
            self._last_script_pick_at = now
            _echo.to_chat(self.cfg, 'interact',
                          f'enter ({"sent" if ok else "failed"})')
            return True
        # {sleep: secs} - bookkeeping; the director just delays the
        # next action by stamping last_action_at into the future.
        if isinstance(entry, dict) and 'sleep' in entry:
            secs = float(entry['sleep'])
            self._last_action_at = now + secs
            return True
        # Legacy {index: N, mode: M} - packet-injection path. Kept
        # working for backward compat but prefer the cursor-position
        # path above for new tests.
        picked = self._resolve_script_entry(entry, opts)
        if picked is not None:
            mode = None
            if isinstance(entry, dict) and 'mode' in entry:
                mode = int(entry['mode'])
            self.interact.pick_option_by_index(picked, mode=mode)
            self._last_action_at = now
            self._last_script_pick_at = now
            opt_label = opts[picked] if picked < len(opts) else f'index={picked}'
            mode_tag = f' mode={mode}' if mode is not None else ''
            _echo.to_chat(self.cfg, 'interact',
                          f"picked '{opt_label}'{mode_tag} (legacy packet)")
            return True
        return False

    def _resolve_script_entry(self, entry: Any, options: list[str]) -> int | None:
        """Apply matching rules to one script entry. Returns the option
        index to pick, or None when no/ambiguous match. Caller decides
        what to do with None (retry via menu_judge in our case)."""
        # Explicit index always wins, AND skips the bounds check
        # against the visible options list. Many FFXI menus (conquest
        # overseers, vendor menus, home points) populate option
        # LABELS from client-side string tables that text_in never
        # captures, so options[] is empty even though the menu has
        # picks. Trust the explicit index from the script - it came
        # from LSB-mined data or a wiki guide that knows what it's
        # doing.
        if isinstance(entry, dict) and isinstance(entry.get('index'), int):
            return int(entry['index'])
        # Text matcher (string entry or {text, exact}).
        text = None
        exact = False
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict) and 'text' in entry:
            text = str(entry['text'])
            exact = bool(entry.get('exact'))
        if not text:
            return None
        return self.interact.pick_option_by_text(text, exact=exact)

    def _fire_menu_judge(self, menu: dict[str, Any]) -> None:
        if self._judge_rid is not None:
            # Already pending - judge state will pick this up.
            self._enter('judge')
            return
        opts = menu.get('options') or []
        prompt = menu.get('prompt') or ''
        npc_name = menu.get('npc_name') or (self.directive or {}).get('npc_name', '')
        leaf_title = (self.directive or {}).get('title', '')
        # Resolve zone name for the LSB script lookup. Falls back to
        # None when the snapshot or zone enum can't supply it; the
        # judge degrades to non-LSB reasoning in that case.
        zone_name: str | None = None
        snap = self._player()
        if snap is not None and snap.zone_id is not None:
            try:
                from . import menu_catalog as _mc
                zone_name = _mc.zone_name_by_id(self.cfg, int(snap.zone_id))
            except Exception:
                zone_name = None
        # Goal-side hints (free text in directive.hints) get folded
        # into research_notes for the judge. This lets a goal author
        # encode high-level navigation like "buy chariot band: spend
        # conquest points -> 1000-pt items -> chariot band -> Yes"
        # without committing to a brittle script. The judge still
        # validates against memory-read labels.
        notes = self._research_notes() or ''
        hints = (self.directive or {}).get('hints') or ''
        if hints:
            sep = '\n\n' if notes else ''
            notes = f'{notes}{sep}Goal hints:\n{hints}'
        rid = self.menu_judge.request(
            npc_name=npc_name,
            zone_name=zone_name,
            user_goal=self._user_goal() or '',
            leaf_title=leaf_title,
            remaining_script=self._script,
            menu_prompt=prompt,
            menu_options=list(opts),
            menu_cursor=int(menu.get('cursor') or 0),
            research_notes=notes or None,
        )
        if rid is None:
            self._fail('menu_judge unavailable (LLM offline?)')
            return
        self._judge_rid = rid
        self._enter('judge')

    def _tick_judge(self) -> None:
        if self._judge_rid is None:
            self._enter('script')
            return
        verdict = self.menu_judge.status(self._judge_rid)
        if verdict is None:
            return  # still pending
        # Take the verdict (echo and event already fired by the judge).
        decision = verdict.get('decision')
        self.menu_judge.discard(self._judge_rid)
        self._judge_rid = None
        if decision == 'pick':
            idx = verdict.get('index')
            if isinstance(idx, int):
                # Vendor menus don't go through the dialog widget pick
                # mechanism; the labels we showed the LLM were
                # synthesized from the 0x03C shop list. Map the picked
                # option back to a shop_index and fire 0x083 (SHOP_BUY)
                # directly. The shop UI doesn't auto-close after a buy
                # so we send escape to dismiss before completing.
                menu = self.interact.current_menu() or {}
                if menu.get('kind') == 'vendor':
                    vendor_items = menu.get('vendor_items') or []
                    if 0 <= idx < len(vendor_items):
                        row = vendor_items[idx]
                        shop_index = int(row.get('shop_index', idx))
                        item_name  = row.get('name', '?')
                        self.interact.buy(shop_index, qty=1)
                        self._last_action_at = time.time()
                        self._last_script_pick_at = time.time()
                        _echo.to_chat(
                            self.cfg, 'interact',
                            f"buy {item_name} (shop_index={shop_index})")
                        # Give the server a moment to process the buy
                        # before we dismiss the UI; otherwise escape
                        # races the 0x083 handler and the UI looks
                        # frozen.
                        self._enter('completed')
                        return
                    # Unparseable index -> fail rather than wedge.
                    self._fail(f"vendor pick index {idx} out of range "
                               f"({len(vendor_items)} items)")
                    return
                # Memory-write driver: addon writes cursor state then
                # sends 0x05B. With mode=End the server closes the
                # event and the client dismisses the dialog; mode=
                # Update keeps the event open for further picks.
                # _is_last_pick() returns True when script is empty
                # AND completion-type is menu_closed - the only case
                # where we want the dialog to close after this pick.
                last = self._is_last_pick()
                mode = _input.MODE_END if last else _input.MODE_UPDATE
                self.input_driver.select_menu_option(idx, mode=mode)
                self.input_driver.reset_cursor()
                self._last_action_at = time.time()
                self._last_script_pick_at = time.time()
                if last:
                    # Trust the server's event_end response; menu.json
                    # has a known bug detecting client-side close.
                    # No cleanup_ui_overlay - it would clobber our
                    # pick action in interact.json.
                    self._enter('completed')
                    return
            self._enter('script')
            return
        if decision == 'abort':
            # Memory-write driver's press_escape closes the menu via
            # the addon's 0x05B Mode=End packet for the captured
            # event. With cursor state set correctly (via prior
            # picks or a fresh menu) the client cleanly dismisses.
            self.input_driver.press_escape()
            self._fail(f"menu_judge aborted: {verdict.get('reason', '')}")
            return
        # error
        self._fail(f"menu_judge error: {verdict.get('reason', '')}")

    def _complete_or_continue(self) -> None:
        """Menu just closed. Decide whether the leaf is done or whether
        a follow-up menu is still expected (multi-stage dialogs that
        close and re-open as the script unfolds)."""
        d = self.directive or {}
        completion = d.get('completion') or {'type': 'menu_closed'}
        ctype = completion.get('type')
        if ctype == 'menu_closed':
            # Default success criterion: script exhausted (or near-
            # exhausted) AND menu is closed. If script entries remain
            # AND there's still meaningful work, return to open and
            # let the player open the menu again. But for typical
            # one-shot dialogs the menu closes and we're done.
            if not self._script:
                self._cleanup_ui_overlay()
                self._enter('completed')
                return
            # Script entries still pending but menu closed. Two cases:
            #
            #  (a) Multi-stage event in flight: we just sent Mode=Update
            #      and the addon briefly saw menu_open=false because
            #      the server's eventucoff hit BEFORE the next event-
            #      begin (eventnum) lands on the wire. Re-Talking from
            #      scratch would lose the localVar context the server
            #      set during Update. Stay in 'script' and wait for
            #      menu_open to flip back to true on the same event_id.
            #
            #  (b) Genuine new dialog needed: the previous event ended
            #      and the script wants us to re-engage the NPC. After
            #      a grace window with no menu re-open, fall back to
            #      'open' for a fresh Talk.
            #
            # We pick (a) by default - it's right for vendor / CP-shop
            # multi-step flows where each Update transitions the same
            # event to the next sub-state. Case (b) is rare and the
            # planner can spell it out by emitting two separate
            # interact_npc leaves if needed.
            return
        if ctype == 'zone_changed':
            # Home points / cutscene-warp NPCs - the cross-zone fire
            # is observed by the goal_manager via snap.zone_id; here
            # we just mark complete and let the goal manager read the
            # zone change as success.
            self._cleanup_ui_overlay()
            self._enter('completed')
            return
        if ctype == 'key_item_received':
            # Phase D+ - inventory diff watcher will set this. For now
            # treat as menu_closed semantics.
            self._cleanup_ui_overlay()
            self._enter('completed')
            return
        # Unknown completion type - safe default is to mark complete.
        self._cleanup_ui_overlay()
        self._enter('completed')

    def _cleanup_ui_overlay(self) -> None:
        """Send a best-effort UI dismissal after a leaf completes.
        Even when the server closes the event (0x052), the FFXI client
        sometimes keeps a residual dialog overlay on screen. The user
        observed this after a successful signet purchase: menu_open
        stayed True and required a manual cancel. Pre-emptively firing
        gamepad B here keeps the screen clean for the next test."""
        try:
            self.input_driver.press_escape()
        except Exception:
            pass
