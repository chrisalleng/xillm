"""Menu LLM fallback - decides which menu option to pick when the
declarative script in an interact_npc goal can't resolve the choice.

Fires when:
  1. A script entry's text doesn't substring-match any visible option.
  2. A script entry's text matches MULTIPLE options ambiguously.
  3. The script is exhausted but the menu is still open with
     actionable options (planner under-specified).

Why this exists: wiki/guide step-lists are the ground truth for
quest dialog choices, but they go stale, get reworded by the server
build, or transcribe options imprecisely. Failing fast on every
mismatch would make any fragile script unusable. Falling through to
a reactive-tier judge keeps progress moving while still surfacing
the script error in the [interact] echo so the user can see what
happened.

Design mirrors engage_judge.EngageJudge:
  - rid-based fire/poll with a worker thread per request
  - no caching; every menu state is unique
  - tool_choice='required' so the LLM can't return free-form text
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config as _config
from . import echo as _echo
from . import events as _events
from . import llm_gateway as _llm
from . import lsb_script_resolver as _lsb_scripts


SYSTEM_PROMPT = """You are the menu-decision agent for an autonomous Final Fantasy XI character.
The player's declarative script for this NPC dialog couldn't resolve
which option to pick - either the option text was misspelled in the
script, the menu's wording differs from the wiki/guide source, or the
script ran out of entries while the menu is still open.

Pick the option that best advances the agent's GOAL using the menu
prompt and the visible options. The script the planner wrote is
informational - if it conflicts with the actual menu, trust the menu.

Respond with EXACTLY ONE tool call. Two tools are available:

  pick_menu_option({index, reason})
    - index: integer, 0-based into the options list
    - reason: 1-2 sentence first-person explanation, used in the
      [interact] echo so the player at the keyboard can see WHY this
      option was chosen. NOT in-character voice ("I, brave warrior...").
      Keyboard voice ("picking 'Yes' because the wiki says start the
      quest with affirmation"). Hyphens, never em-dashes.

  abort_menu({reason})
    - Use ONLY when no option safely advances the goal AND closing
      the menu is the right call. Examples: vendor menu opened by
      mistake (we wanted dialog, got shop), confirm-purchase prompt
      for an item we don't actually want.

DO NOT invent options. Pick from the indices in the menu. If the
right option doesn't exist, abort_menu is the correct response, not
a wild guess.
"""


def _truncate(s: str, n: int) -> str:
    s = s or ''
    return s if len(s) <= n else s[:n - 1] + '...'


def _build_prompt(*,
                  npc_name: str,
                  zone_name: str | None,
                  user_goal: str,
                  leaf_title: str,
                  remaining_script: list[Any],
                  menu_prompt: str,
                  menu_options: list[str],
                  menu_cursor: int,
                  research_notes: str | None,
                  lsb_sources: dict[str, str] | None = None) -> str:
    """Assemble the user message. Kept compact - menu choices need
    sub-5s latency on the reactive tier and prompt bloat is the
    fastest way to blow that budget on a 9b model."""
    script_lines = []
    for i, entry in enumerate(remaining_script[:8]):  # cap at 8 to keep prompt tight
        if isinstance(entry, str):
            script_lines.append(f'  {i+1}. choose "{entry}"')
        elif isinstance(entry, dict):
            if 'index' in entry:
                script_lines.append(f'  {i+1}. choose index {entry["index"]}')
            elif 'text' in entry:
                exact = entry.get('exact')
                script_lines.append(
                    f'  {i+1}. choose "{entry["text"]}"'
                    + (' (exact)' if exact else '')
                )
        else:
            script_lines.append(f'  {i+1}. <unrecognized: {entry!r}>')
    script_block = '\n'.join(script_lines) if script_lines else '  (script exhausted)'

    options_block = '\n'.join(
        f'  [{i}] {opt}{"  <- cursor" if i == menu_cursor else ""}'
        for i, opt in enumerate(menu_options)
    ) if menu_options else (
        '  (no labels available - menu choices live in client memory '
        'and we do not extract them yet. READ THE LSB SCRIPT BELOW '
        'to determine option indices: each `option == N` branch in '
        'onEventUpdate / onEventFinish describes what index N does. '
        'Pick the integer index that the LSB script maps to the goal.)'
    )

    research_block = (
        f'\nResearch notes from the planner (era-correct, may apply):\n'
        f'{_truncate(research_notes, 1200)}\n'
        if research_notes else ''
    )

    # LSB source block: when the NPC has known Lua source (its own
    # script and/or any quest/mission script that references it),
    # include them. The LLM reads `if option == N then ...` branches
    # directly to figure out option semantics. Most accurate signal
    # we have for non-cataloged menus.
    lsb_block = ''
    if lsb_sources:
        parts = []
        for label, src in lsb_sources.items():
            parts.append(f'### {label}\n```lua\n{src}\n```')
        lsb_block = (
            '\nLSB server source for this NPC and any quests/missions '
            'that reference it. Read the `onEventUpdate` and `onEventFinish` '
            'handlers - the `option` parameter is the index the player '
            'picked. `mission:advance(player)` / `mission:complete(player)` / '
            '`player:setVar(...)` calls indicate which option progresses '
            'the storyline. This is the ground truth - prefer it over '
            'guessing from the menu prompt alone.\n\n' + '\n\n'.join(parts) + '\n'
        )

    return (
        f'User goal: "{user_goal or "(none set)"}"\n'
        f'Active leaf: "{leaf_title or "(untitled)"}"\n'
        f'NPC: {npc_name or "(unknown)"}\n'
        f'Zone: {zone_name or "(unknown)"}\n'
        f'\n'
        f'Planner script (first 8, then truncated):\n{script_block}\n'
        f'\n'
        f'Current menu prompt:\n  {_truncate(menu_prompt, 400)}\n'
        f'\n'
        f'Options (0-based):\n{options_block}\n'
        f'{research_block}'
        f'{lsb_block}'
        f'\n'
        f'Pick the option index that advances the goal, or abort_menu '
        f'if no safe choice exists.'
    )


PICK_TOOL = {
    'type': 'function',
    'function': {
        'name': 'pick_menu_option',
        'description': 'Pick a menu option by 0-based index.',
        'parameters': {
            'type': 'object',
            'properties': {
                'index':  {'type': 'integer'},
                'reason': {'type': 'string'},
            },
            'required': ['index', 'reason'],
        },
    },
}

ABORT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'abort_menu',
        'description': 'Close the menu without selecting any option. '
                       'Use only when no option advances the goal.',
        'parameters': {
            'type': 'object',
            'properties': {
                'reason': {'type': 'string'},
            },
            'required': ['reason'],
        },
    },
}


class MenuJudge:
    """rid-based fire/poll dispatcher for menu decisions. Mirrors
    EngageJudge - same pattern, simpler shape (no caching, single-turn
    tool call, reactive tier). Caller fires `request(...)`, polls
    `status(rid)` for None vs verdict, then `discard(rid)` after
    acting on the result."""

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway | None):
        self.cfg = cfg
        self.llm = llm
        self._results: dict[int, dict[str, Any]] = {}
        self._pending: set[int] = set()
        self._next_rid: int = 1
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.llm is not None and self.llm.available

    def status(self, rid: int) -> dict[str, Any] | None:
        with self._lock:
            if rid in self._pending:
                return None
            return self._results.get(rid)

    def discard(self, rid: int) -> None:
        with self._lock:
            self._results.pop(rid, None)

    def request(self, *,
                npc_name: str,
                zone_name: str | None,
                user_goal: str,
                leaf_title: str,
                remaining_script: list[Any],
                menu_prompt: str,
                menu_options: list[str],
                menu_cursor: int = 0,
                research_notes: str | None = None) -> int | None:
        """Fire a fresh menu decision. Returns rid for polling, or
        None if LLM is unavailable (caller should abort the leaf).

        zone_name is upper-snake-case (e.g. 'BASTOK_MARKETS'); used
        to locate the NPC's LSB script for the prompt enrichment."""
        if not self.available():
            return None
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            self._pending.add(rid)
        # Log the exact options snapshot the LLM is about to see -
        # menu.json gets overwritten every tick so historical state is
        # otherwise lost. Critical for debugging "why did the LLM pick
        # X" because the picked label alone doesn't show what the
        # alternatives were.
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='menu_judge',
            type_='request',
            rid=rid,
            npc_name=npc_name,
            menu_prompt=menu_prompt,
            menu_options=list(menu_options),
            menu_cursor=menu_cursor,
        )
        t = threading.Thread(
            target=self._worker,
            args=(rid, npc_name, zone_name, user_goal, leaf_title,
                  list(remaining_script), menu_prompt,
                  list(menu_options), menu_cursor, research_notes),
            name=f'menu-judge-{rid}', daemon=True,
        )
        t.start()
        return rid

    def _worker(self, rid: int, npc_name: str, zone_name: str | None,
                user_goal: str, leaf_title: str,
                remaining_script: list[Any],
                menu_prompt: str, menu_options: list[str],
                menu_cursor: int, research_notes: str | None) -> None:
        # Pull LSB source for the NPC + any quest/mission scripts
        # that reference it. Best-effort: failures (LSB checkout
        # missing, unusual NPC name) just return empty; the LLM
        # falls back to reasoning from the prompt + options alone.
        lsb_sources: dict[str, str] = {}
        try:
            lsb_sources = _lsb_scripts.resolve(zone_name, npc_name)
        except Exception:
            lsb_sources = {}
        prompt = _build_prompt(
            npc_name=npc_name,
            zone_name=zone_name,
            user_goal=user_goal,
            leaf_title=leaf_title,
            remaining_script=remaining_script,
            menu_prompt=menu_prompt,
            menu_options=menu_options,
            menu_cursor=menu_cursor,
            research_notes=research_notes,
            lsb_sources=lsb_sources or None,
        )
        verdict: dict[str, Any] | None = None
        err: str | None = None
        t0 = time.time()
        try:
            cr = self.llm.tool_chat(
                tier='reactive',
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user',   'content': prompt},
                ],
                tools=[PICK_TOOL, ABORT_TOOL],
                tool_choice='required',
                max_tokens=300,
            )
            verdict = self._parse_verdict(cr, len(menu_options))
            if verdict is None:
                err = (f'no usable tool call '
                       f'(finish={cr.finish_reason}, calls={len(cr.tool_calls)})')
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
        latency = time.time() - t0

        with self._lock:
            self._pending.discard(rid)
            if verdict is not None:
                self._results[rid] = verdict
            else:
                self._results[rid] = {
                    'decision': 'error',
                    'reason':   err or 'unknown',
                }

        # Echo the decision so the screen-watcher can see WHY a fallback
        # was used and which option it picked. Skipped on error - the
        # leaf will fail and the planner replan path emits its own echo.
        v = self._results[rid]
        decision = v.get('decision', 'error')
        reason = v.get('reason', '')
        if decision == 'pick':
            picked = ''
            idx = v.get('index')
            if isinstance(idx, int) and 0 <= idx < len(menu_options):
                picked = menu_options[idx]
            _echo.to_chat(self.cfg, 'interact',
                          f"picked '{picked}': {reason}")
        elif decision == 'abort':
            _echo.to_chat(self.cfg, 'interact',
                          f'aborting menu: {reason}')

        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='menu_judge',
            type_='decision',
            rid=rid,
            decision=decision,
            index=v.get('index'),
            reason=reason,
            npc_name=npc_name,
            options_count=len(menu_options),
            latency_s=round(latency, 3),
        )

    def _parse_verdict(self, cr, n_options: int) -> dict[str, Any] | None:
        """Pull pick_menu_option / abort_menu out of the tool calls
        list. Returns a normalized dict or None on no-usable-call.
        Validates the index is in range; out-of-range falls back to
        error so the leaf can fail cleanly."""
        for call in cr.tool_calls:
            name = call.name
            args = call.arguments or {}
            if name == 'pick_menu_option':
                idx = args.get('index')
                reason = (args.get('reason') or '').strip()
                if not isinstance(idx, int):
                    continue
                if idx < 0:
                    return {
                        'decision': 'error',
                        'reason': f'index {idx} is negative',
                    }
                # Bounds check only when we actually have visible
                # option labels. With no labels (n_options == 0) the
                # LLM is reasoning purely from the LSB script and we
                # have no client-side index ceiling to validate
                # against - trust the LLM's pick.
                if n_options > 0 and idx >= n_options:
                    return {
                        'decision': 'error',
                        'reason': f'index {idx} out of range (0..{n_options-1})',
                    }
                return {'decision': 'pick', 'index': idx, 'reason': reason}
            if name == 'abort_menu':
                reason = (args.get('reason') or '').strip()
                return {'decision': 'abort', 'reason': reason}
        return None
