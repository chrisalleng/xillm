"""Interact driver - the Python side of the `interact` Ashita addon.

Reads menu state from `state/<char>/menu.json` (published by
interact.lua on every dialog/menu change) and writes action requests
to `commands/<char>/interact.json` (drained by the addon at ~10Hz).

The orchestrator owns one InteractDriver instance, instantiated in
main.py and threaded into anything that needs to drive an NPC dialog
- the goal manager's interact_npc / vendor_buy / vendor_sell / home_
point dispatchers, and the death_recovery watcher.

State shape (matches interact.lua's publisher):

    {
      "ts":           <unix>,
      "open":         bool,
      "kind":         "dialog" | "vendor" | "trade" | "death" | "unknown",
      "npc_name":     str,
      "npc_sid":      int,
      "prompt":       str,           # NPC's last spoken line
      "options":      [str, ...],    # menu choices (may be empty)
      "cursor":       int,           # current cursor index, 0-based
      "vendor_items": [...]          # populated only for kind=="vendor"
    }

Action shape (one JSON object per file; we write fresh on each call):

    {"seq": int, "action": "advance_text"}
    {"seq": int, "action": "select_option", "index": int}
    {"seq": int, "action": "buy",  "item": str, "qty": int}
    {"seq": int, "action": "sell", "item": str, "qty": int}
    {"seq": int, "action": "close_menu"}
    {"seq": int, "action": "open_dialog", "target_sid": int}

Phase A scope: state read + advance_text action work end-to-end via
the addon's TkEventMsg2 key injection (stepdialog pattern). The
select_option / buy / sell actions are wired through but the ADDON-
side handlers stub them out until Phase C lands real packet sends.
The driver still records the requests so callers can see what would
have been done.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config as _config


class InteractDriver:
    """Read menu state, write interact actions. Stateless beyond the
    seq counter (used to make every written file content-different so
    the addon's drain-on-change logic doesn't hit any "same content,
    skipped" edge case)."""

    def __init__(self, cfg: _config.Config):
        self.cfg = cfg
        # Cache the last successfully-read menu snapshot + its mtime
        # so callers polling at 5+ Hz don't repeatedly hit the disk
        # for an unchanged file. The orchestrator's existing
        # snapshot-mtime pattern (combat.snapshot, nav.snapshot) does
        # the same thing - we mirror it here.
        self._cached: dict[str, Any] | None = None
        self._cached_mtime: float = 0.0
        # Monotonically-increasing per-action sequence id. The addon
        # doesn't currently inspect this but having distinct contents
        # protects against any future "skip if file unchanged" logic
        # on the consumer side.
        self._seq = int(time.time() * 1000)

    # ---- state ---------------------------------------------------------

    def _menu_path(self) -> Path:
        return self.cfg.paths.state_dir(self.cfg.character) / 'menu.json'

    def current_menu(self) -> dict[str, Any] | None:
        """Read the latest menu snapshot, returning None if the file
        doesn't exist yet (addon hasn't started or hasn't seen a menu
        this session) or if the JSON is malformed."""
        p = self._menu_path()
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return None
        if mtime == self._cached_mtime and self._cached is not None:
            return self._cached
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return self._cached  # last good wins on read failure
        if not isinstance(data, dict):
            return None
        self._cached = data
        self._cached_mtime = mtime
        return data

    def is_menu_open(self) -> bool:
        m = self.current_menu()
        return bool(m and m.get('open'))

    def has_actionable_options(self) -> bool:
        """True when the current menu has 'real' choices that need a
        decision - >1 option OR a single non-OK option. Used by the
        dialog walker to decide between consuming a script entry vs
        sending a plain Enter to advance text."""
        m = self.current_menu()
        if not m or not m.get('open'):
            return False
        opts = m.get('options') or []
        if len(opts) > 1:
            return True
        if len(opts) == 1:
            txt = (opts[0] or '').strip().lower()
            # Treat trivial passive prompts as not-actionable so the
            # walker auto-Enters past them. Add other passive labels
            # here as we encounter them in the wild.
            return txt not in ('ok', 'okay', 'continue', 'next')
        return False

    def pick_option_by_text(self, target_text: str, *, exact: bool = False) -> int | None:
        """Resolve an option-text match against the current menu's
        option list. Returns the matched index on unique hit, or None
        on no-match / ambiguous-match. Caller (the leaf state machine
        or the menu_judge fallback) decides what to do on None."""
        m = self.current_menu()
        if not m:
            return None
        opts = m.get('options') or []
        if not opts:
            return None
        target = (target_text or '').strip().lower()
        if not target:
            return None
        if exact:
            hits = [i for i, o in enumerate(opts)
                    if (o or '').strip().lower() == target]
        else:
            hits = [i for i, o in enumerate(opts)
                    if target in (o or '').strip().lower()]
        if len(hits) == 1:
            return hits[0]
        return None

    # ---- actions -------------------------------------------------------

    def _action_path(self) -> Path:
        return self.cfg.paths.commands_dir(self.cfg.character) / 'interact.json'

    def _next_seq(self) -> int:
        # Wall-clock millis is fine - we just need monotonic for this
        # session. If two actions land in the same millisecond, bump.
        s = int(time.time() * 1000)
        if s <= self._seq:
            s = self._seq + 1
        self._seq = s
        return s

    def _send(self, payload: dict[str, Any]) -> bool:
        """Atomic temp+rename write of the action JSON. The addon
        polls, reads, and DELETES the file - so we always overwrite
        regardless of whether a previous action is still in flight.
        That's by design: if the orchestrator changes its mind about
        what to do mid-frame (e.g. menu_judge picked a different
        option than the script writer), the latest write wins."""
        path = self._action_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(payload)
        payload['seq'] = self._next_seq()
        try:
            fd, tmp = tempfile.mkstemp(
                dir=path.parent, prefix=path.name + '.', suffix='.tmp',
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.replace(tmp, path)
            return True
        except OSError:
            return False

    def advance_text(self) -> bool:
        """Press Enter to advance non-menu dialog text. The addon
        injects via TkEventMsg2::OnKeyDown - same trick `stepdialog`
        uses, no packet send needed. Always works for the simple
        'text scrolls past' case; for menus with multiple options
        this just confirms the current cursor selection."""
        return self._send({'action': 'advance_text'})

    def pick_option_by_index(self, index: int, *, mode: int | None = None) -> bool:
        """Select option N in the current menu by sending an outgoing
        0x05B (CLI_COMMAND_EVENTEND) packet. `mode` is optional:

          mode=1 (default, UpdatePending): event continues - the
                 server will send another menu after we pick. This
                 is what most quest dialog steps want.
          mode=0 (End): closes the event entirely. Use when picking
                 the final / "Cancel" option in a top-level menu.

        The addon ignores mode if not provided and uses 1.
        """
        payload: dict[str, Any] = {
            'action': 'select_option',
            'index':  int(index),
        }
        if mode is not None:
            payload['mode'] = int(mode)
        return self._send(payload)

    def buy(self, item_index: int, qty: int = 1) -> bool:
        """Purchase item at vendor stock position `item_index`. The
        addon emits outgoing 0x083 (SHOP_BUY). The orchestrator
        resolves item-name -> index via the LSB-mined vendor catalog
        before calling this; the addon doesn't see the name."""
        return self._send({
            'action': 'buy',
            'index':  int(item_index),
            'qty':    int(qty),
        })

    def sell(self, item_index: int, qty: int = 1) -> bool:
        """Sell inventory item at slot `item_index`. Two-step protocol
        in FFXI (0x084 sell-req + 0x085 sell-set); current addon
        implementation stubs this until the orchestrator-driven /sell
        chat command flow is wired up."""
        return self._send({
            'action': 'sell',
            'index':  int(item_index),
            'qty':    int(qty),
        })

    def close_menu(self) -> bool:
        return self._send({'action': 'close_menu'})

    def set_cursor(self, index: int) -> bool:
        """Pick option `index` (0-indexed) the way a player would.
        The addon writes (index+1) to cursor_struct + 0x548; the
        FFXI client's per-frame submit loop reads that flag, looks
        up whatever option_index it needs (including opaque ones
        like 0x80A1 for conquest items), sends 0x05B itself, and
        dismisses / transitions the UI cleanly. Verified 2026-05-02
        for both single-pick (Signet) and multi-step (Rabid Wolf
        conquest -> 12-item sub-menu).

        Name is a misnomer kept for backwards compat - the action
        actually picks, it doesn't just position the cursor.
        Prefer this over `pick_option_by_index` (which builds the
        0x05B packet manually and has trouble with opaque option
        indexes and dialog dismissal)."""
        return self._send({'action': 'set_cursor', 'index': int(index)})

    def force_escape(self) -> bool:
        """Inject FFXI's cancel/escape key via TkEventMsg2. Use this
        when close_menu can't help because we have no captured event
        context (e.g. dialog stuck from a prior session). Best-effort:
        the keycode is empirically chosen and may not dismiss every
        overlay, but it's harmless when nothing is open."""
        return self._send({'action': 'escape'})

    def open_dialog(self, target_sid: int) -> bool:
        """Trigger the talk-to-target Enter press. Caller should have
        already ensured the NPC is /target'd (the combat addon's
        flow). The addon translates this to an Enter injection."""
        return self._send({'action': 'open_dialog',
                           'target_sid': int(target_sid)})

    # ---- auction house --------------------------------------------------
    #
    # Three primitives for AH interaction. Player must be standing within
    # interaction range of an auction house counter NPC; no /target or
    # dialog open required - the WORK_CHECK packet establishes the AH
    # session directly.
    #
    # Sell flow (two-step):
    #   1. ah_open()           - one-time per session
    #   2. ah_sell_ask(...)    - server replies with listing fee
    #   3. ah_sell_lot(...)    - commits the listing
    #
    # Buy flow:
    #   1. ah_open()           - one-time per session
    #   2. ah_bid(...)         - server matches against cheapest listing

    def ah_open(self) -> bool:
        """Open an AH session with the nearest counter NPC. Send once
        per session; subsequent ah_sell_ask / ah_bid calls reuse it.
        Equivalent to walking up to an AH counter and clicking the
        'Auction' option in the dialog menu (the LSB server doesn't
        require the dialog flow - WORK_CHECK alone establishes the
        session)."""
        return self._send({'action': 'ah_open'})

    def ah_sell_ask(self, slot: int, item_id: int, price: int,
                    *, single: bool = False) -> bool:
        """Phase 1 of selling: ASK_COMMIT. Server validates the
        listing and replies with the listing fee in a 0x4C packet.

        Args:
            slot: inventory slot (1..30) of the item to list.
            item_id: FFXI item ID. Must match what's at `slot`.
            price: asking price in gil.
            single: True to list as a single item, False (default) to
                list as a stack. A stack listing requires a full stack
                in the slot (12 for most items, 99 for some).
        """
        return self._send({
            'action':  'ah_sell_ask',
            'slot':    int(slot),
            'item_id': int(item_id),
            'price':   int(price),
            'single':  1 if single else 0,
        })

    def ah_sell_lot(self, slot: int, price: int,
                    *, single: bool = False, auc_idx: int = 0) -> bool:
        """Phase 2 of selling: LOT_IN. Commits the listing the player
        just previewed via ah_sell_ask. `price` and `single` MUST match
        what was passed to ah_sell_ask or the server rejects the commit.

        Args:
            slot: inventory slot (must match the ASK_COMMIT slot).
            price: must match the ASK_COMMIT price.
            single: must match the ASK_COMMIT single flag.
            auc_idx: which player sales-history slot to occupy (0..6).
                Defaults to 0; server will reject if that slot is
                already in use.
        """
        return self._send({
            'action':  'ah_sell_lot',
            'slot':    int(slot),
            'price':   int(price),
            'single':  1 if single else 0,
            'auc_idx': int(auc_idx),
        })

    def ah_bid(self, item_id: int, max_price: int,
               *, single: bool = False, auc_idx: int = 0) -> bool:
        """Bid on a listing. Server matches against the cheapest
        listing of `item_id` at or below `max_price`. If a match is
        found, gil is deducted and the item lands in the player's
        delivery box. If no match, server replies with an error in
        the 0x4C response.

        Args:
            item_id: FFXI item ID being bid on.
            max_price: maximum gil willing to pay.
            single: True to bid on a single, False (default) for a
                stack listing.
            auc_idx: which player bid-history slot (0..6).
        """
        return self._send({
            'action':  'ah_bid',
            'item_id': int(item_id),
            'price':   int(max_price),
            'single':  1 if single else 0,
            'auc_idx': int(auc_idx),
        })
