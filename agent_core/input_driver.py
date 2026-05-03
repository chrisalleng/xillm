"""Memory-write input driver for FFXI client menu navigation.

Replaces the abandoned virtual-gamepad approach. Instead of
injecting button events through uinput, we write the menu cursor
state directly into FFXI's client memory and send the action
packet through `interact.lua`'s existing path.

Why this works (verified 2026-05-01):
The FFXI client maintains a cursor index in several mirror
locations under Ashita's `'menu'` pointer chain. When the player
presses Enter (or whatever maps to "confirm" on their controller),
the action handler reads the cursor from these mirrors to decide
which option was selected - it does NOT read the visible cursor
highlight position. So we can:

  1. Write cursor state to memory (target option N)
  2. Send the pick packet (carries N explicitly)
  3. The server processes the action correctly
  4. With mode=0 (End), the server sends event_end which the
     client honors by closing the dialog UI cleanly

The visible cursor highlight may lag during this (the renderer
uses a separate state path we never located), but it's purely
cosmetic - actions resolve correctly because the action handler
reads from memory.

The driver preserves the public API the existing
`interact_director` calls (press_enter, press_escape, press_down,
select_menu_option, ...) so the director doesn't need to know
the implementation changed under it.

Local cursor tracking
---------------------
Memory writes are absolute (set cursor = N), but the script entry
format supports both absolute (`{position: N}`) and relative
(`{key: down, n: M}`) navigation. To handle relative moves, the
driver maintains a local `_cursor` counter that mirrors what we
last wrote. `press_down(N)` increments the counter and sets the
new value via memory write.

The local counter is reset to 0 by callers (`reset_cursor`) when
a new menu opens. FFXI menus default to cursor=0 on first display,
so this matches the game's own initial state.
"""
from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import interact as _interact


# Default mode for select_menu_option. 1 = UpdatePending (event
# continues; server expects more picks). 0 = End (event terminates;
# server sends event_end so client closes the dialog UI). Most
# script steps use mode=1; the FINAL pick of a multi-step menu
# (the one that should close the dialog) uses mode=0.
MODE_UPDATE = 1
MODE_END    = 0


class InputDriver:
    """Memory-write menu driver. Constructed with an InteractDriver
    instance which provides the file-based action channel into the
    `interact.lua` Ashita addon. The addon walks Ashita's `'menu'`
    pointer chain and writes the cursor mirrors before issuing the
    pick packet."""

    def __init__(self, interact: '_interact.InteractDriver') -> None:
        self._interact = interact
        self._lock = Lock()
        # Locally-tracked cursor index. Reflects what we last wrote
        # to memory. Reset to 0 by reset_cursor() when a new menu
        # opens (FFXI menus default to cursor=0 on first display).
        self._cursor: int = 0

    # ---- cursor tracking --------------------------------------------

    def reset_cursor(self) -> None:
        """Reset the local cursor to 0. Call when a new menu opens
        so subsequent relative moves (press_down/press_up) start
        from the right baseline. Does not write memory - FFXI
        already initialises cursor=0 on menu open."""
        with self._lock:
            self._cursor = 0

    def get_cursor(self) -> int:
        return self._cursor

    def invalidate_cache(self) -> None:
        """Compatibility shim with the prior xdotool driver. No-op."""
        pass

    # ---- public input verbs -----------------------------------------

    def press_enter(self, repeats: int = 1, *, end: bool = False) -> bool:
        """Confirm the current menu selection. Sends the pick packet
        for the cursor's current position. mode is Update (event
        continues) by default; pass `end=True` for the final pick
        of a multi-step interaction so the server sends event_end
        and the client closes the dialog UI.

        For text-only dialog frames (NPC narration with no choices),
        callers should use interact.advance_text() instead - this
        method assumes a real menu is open with a meaningful cursor
        position. `repeats` is ignored: pressing Enter twice rapidly
        in a real menu is the same as pressing it once for our
        action semantics. After the pick, local cursor resets to 0
        - the menu either closed or transitioned to a new state
        with a fresh cursor."""
        mode = MODE_END if end else MODE_UPDATE
        with self._lock:
            cur = self._cursor
            ok = self._interact.pick_option_by_index(cur, mode=mode)
            self._cursor = 0
            return ok

    def press_escape(self, repeats: int = 1) -> bool:
        """Cancel the current menu / close a dialog. Issues the
        addon's close_menu action which sends 0x05B Mode=End for
        the captured event. `repeats` ignored. Local cursor resets
        to 0 since the menu is closing."""
        with self._lock:
            self._cursor = 0
            return self._interact.close_menu()

    def press_down(self, repeats: int = 1) -> bool:
        """Move cursor down by `repeats` positions. Translates to a
        memory write of cursor + repeats."""
        with self._lock:
            self._cursor = max(0, self._cursor + int(repeats))
            return self._interact.set_cursor(self._cursor)

    def press_up(self, repeats: int = 1) -> bool:
        with self._lock:
            self._cursor = max(0, self._cursor - int(repeats))
            return self._interact.set_cursor(self._cursor)

    def press_left(self, repeats: int = 1) -> bool:
        """Some FFXI menus (vendor inventories, conquest item lists)
        treat Left like a "previous category" hop. We approximate as
        cursor up since vertical navigation is the dominant case."""
        return self.press_up(repeats)

    def press_right(self, repeats: int = 1) -> bool:
        return self.press_down(repeats)

    def press_menu(self, repeats: int = 1) -> bool:
        """Open the system menu. No memory-write equivalent; the
        agent doesn't currently need to drive this. Returns False
        so callers see the no-op explicitly."""
        return False

    # ---- composite verbs --------------------------------------------

    def select_menu_option(self, position: int, *,
                           mode: int = MODE_UPDATE,
                           gap_ms: int = 0) -> bool:
        """Pick option `position` (0-indexed) by writing the +0x548
        submit flag in cursor_struct. The client's per-frame submit
        loop reads the flag, computes the proper option_index (works
        for opaque encodings like 0x80A1 conquest items), sends 0x05B
        itself, and dismisses/transitions the UI cleanly.

        `mode` and `gap_ms` are preserved in the signature for
        compatibility with the prior packet-build driver. They are
        ignored here - the +0x548 path handles event-end semantics
        automatically based on the current menu state, and there's
        no sequence of presses to pace."""
        if position < 0:
            return False
        with self._lock:
            self._cursor = int(position)
            return self._interact.set_cursor(position)
