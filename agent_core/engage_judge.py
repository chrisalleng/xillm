"""LLM-driven engage/skip decisions for the farming director.

Replaces the hardcoded `CHECK_TYPE_TOO_TOUGH` / `CHECK_TYPE_ENGAGEABLE`
sets with a deliberative-tier judgment that consumes mob info, player
state, current goal, and per-mob fight history. Cached by
(mob_name, player_level) so the second encounter with the same mob at
the same level is instant.

Async by design: the deliberative tier is ~1.5s, and the farming tick
runs at 5-10Hz. `request()` fires a worker thread, `status()` polls.
While a request is in flight, the acquire state holds - neither engages
nor blacklists - until the verdict lands or the caller's per-state
timeout expires.

Failure modes:
  - LLM unavailable / errors: status returns 'error'; the caller falls
    back to hardcoded check_type bucketing.
  - Malformed JSON in response: same - error fallback.
  - Network timeout: same.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from . import config as _config
from . import echo as _echo
from . import events as _events
from . import llm_gateway as _llm
from . import web_research as _web_research


# /check response message-type bytes (verified against the real
# `checker` Ashita addon - this map was previously shifted by one,
# making the agent label easy prey as "decent challenge" etc.).
CHECK_TYPE_LABEL = {
    0x40: 'Too Weak (no XP)',
    0x41: 'Incredibly Easy Prey',
    0x42: 'Easy Prey',
    0x43: 'Decent Challenge',
    0x44: 'Even Match',
    0x45: 'Tough',
    0x46: 'Very Tough',
    0x47: 'Incredibly Tough',
    0xF9: 'Impossible to gauge',
}


def _check_label(ct: Any) -> str:
    if not isinstance(ct, int):
        return 'unknown'
    return CHECK_TYPE_LABEL.get(ct, f'unknown (0x{ct:02X})')


# How long an error verdict stays cached. Long enough that we don't
# hammer a downed LLM endpoint, short enough that recovery is picked
# up without manual intervention. The caller treats a cached error as
# "fall back to hardcoded check_type buckets" - the agent keeps farming
# during LLM downtime, just without per-mob LLM judgment.
ERROR_CACHE_TTL_S = 30.0

# Cache by (name, level, hp_bucket) instead of just (name, level). A
# verdict made at full HP shouldn't apply at 30% HP - the situation is
# materially different and the LLM's reason ("Full HP, let's go") would
# be wrong if echoed at the new HP. 10% buckets give a fine-grained
# re-evaluation cadence as HP changes - we get a fresh judgment after
# every meaningful HP swing without thrashing the LLM.
HP_BUCKET_PCT = 10


def _hp_bucket(hp_pct: float | int | None) -> int:
    if not isinstance(hp_pct, (int, float)):
        return 0
    return min(int(hp_pct) // HP_BUCKET_PCT, (100 // HP_BUCKET_PCT) - 1)


SYSTEM_PROMPT = (
    _web_research.ERA_CONSTRAINT + '\n\n'
    'You are the player AT THE KEYBOARD playing this FFXI character - '
    'NOT the in-game character. Think like a real person making '
    'decisions about their game: practical, casual, focused on the '
    'numbers and the goal. NOT in-character roleplay (no "by the '
    'Goddess", no "I shall vanquish", no heroic-voice narration).\n\n'
    'Given the enemy, player state, current goal, and per-mob fight '
    'history, decide what to do with this specific enemy now. Reply '
    'with ONLY a JSON object - no prose, no code fences - of the shape: '
    '{"decision": "engage" | "skip" | "rest", '
    '"reason": "<one short first-person sentence>"}.\n\n'
    'DECISION PRIORITY (apply in order):\n'
    '1. CURRENT HP vs. damage taken on this mob/tier is the dominant '
    '   signal. If `max damage taken` (per-mob OR per-tier from the '
    '   "Difficulty-tier history" block) exceeds current HP, the next '
    '   fight has a high chance of being fatal - SKIP.\n'
    '2. Generalize from the tier history: if the Difficulty-tier block '
    '   shows weighted deaths >= 1.0 to mobs of THIS check label, '
    '   treat ALL mobs of that label as too risky for now - even ones '
    '   with no per-mob history. The counts are decay-weighted by '
    '   level distance, so as the character levels up old-level '
    '   deaths fade out naturally and the agent gets willing to retry '
    '   the tier. Use the WEIGHTED counts as shown.\n'
    '3. If HP >= 80%% AND the check is Easy Prey / Decent Challenge '
    '   AND no tier-deaths at this level, ENGAGE. Past deaths from a '
    '   different level do NOT apply at higher levels.\n'
    '4. Rest only when HP/MP are low enough that ANY fight is unsafe '
    '   (HP < ~50%%), not as routine pre-fight caution.\n\n'
    'The reason field MUST be ONE short sentence under 90 characters '
    '- sounds like a real person at their keyboard talking about the '
    'game, not the character speaking from inside the world.\n\n'
    'FORMATTING RULES:\n'
    '- Player perspective ("my HP", "I\'ll skip", "going in"). The mob '
    '  is referenced by its game name + con label, e.g. "the Wild '
    '  Rabbit is Easy Prey" - that is fine.\n'
    '- NO in-character voice, NO heroic/dramatic phrasing, NO lore '
    '  references ("Altana", "Shadow Lord", oaths, vows). Just gamer '
    '  speak: practical, casual, slightly terse.\n'
    '- ONE sentence, no narrative. Just the reasoning.\n'
    '- DO NOT reference past fights or "still recovering" / "still '
    '  healing" - state the CURRENT decision only.\n'
    '- DO mention HP%%, level, or con label - they are the deciding '
    '  factors. Use the EXACT integer percent shown in the prompt '
    '  (e.g. if "Player HP" says 63%%, say "my HP is at 63%%", not '
    '  "around 60%%" or "above 50%%"). Do not round, do not invent.\n'
    '- Compare current HP vs. avg/max damage taken from history when '
    '  weighing risk. If max damage taken on this mob exceeds your '
    '  current HP, the fight is potentially fatal - skip or rest. If '
    '  avg damage taken is well below current HP, the fight is safe.\n'
    '- If you mention the mob\'s con label, use the EXACT label from '
    '  the prompt (Easy Prey, Decent Challenge, Even Match, Tough, etc.).\n\n'
    'GOOD examples (player-at-keyboard voice; HP/con/decision):\n'
    '  "100%% HP, Wild Rabbit is Easy Prey, going in."\n'
    '  "Only 30%% HP and Ding Bats average 40%%, skipping this one."\n'
    '  "Max dmg from Tunnel Worm was 95%% last time, at 60%% - nope."\n'
    '  "Out of MP and there\'s a linker next to me, sitting first."\n'
    'BAD examples:\n'
    '  "I shall vanquish this beast" (in-character voice; we want '
    '  player voice).\n'
    '  "By Altana, this rabbit shall fall" (lore/oaths).\n'
    '  "I\'m still recovering from the last fight" (flavor; not '
    '  literally resting right now).\n'
    '  "I\'m above 50%% HP" when the prompt says HP 30%% (made-up '
    '  number).\n'
    '  "Engaging." (too terse - missing why).\n\n'
    'If unfamiliar with the mob, web_search BEFORE deciding - '
    'aggressive/linking/draining mobs are dangerous in ways check_type '
    'does not capture. Keep searches minimal (cache is shared).'
)


def _build_prompt(mob: dict[str, Any], player: dict[str, Any],
                  goal: str, history: dict[str, Any],
                  tier_history: dict[str, Any] | None = None) -> str:
    name  = mob.get('name') or 'unknown'
    lvl   = mob.get('level')
    ct    = mob.get('check_type')
    cond  = mob.get('conditions')
    dist  = mob.get('distance')
    lvl_s = f'{lvl}' if lvl is not None else 'unknown'
    ct_s  = _check_label(ct)
    dist_s = f'{dist:.1f}y' if isinstance(dist, (int, float)) else 'unknown'

    main_job = player.get('main_job') or '?'
    sub_job  = player.get('sub_job')  or '-'
    sub_lvl  = player.get('sub_lvl')
    plvl  = player.get('level')
    hp    = player.get('hp_pct')
    mp    = player.get('mp_pct')
    plvl_s = f'{plvl}' if plvl is not None else '?'
    # Send exact HP/MP - the LLM needs the real number to weigh against
    # avg/max damage taken. Cache key remains bucketed (10% granularity)
    # so we don't re-query on every 1%% change, but the prompt itself
    # carries the precise current state.
    hp_s = f'{hp:.0f}%' if isinstance(hp, (int, float)) else '?'
    mp_s = f'{mp:.0f}%' if isinstance(mp, (int, float)) else '?'
    sub_s = f'{sub_job}{sub_lvl}' if isinstance(sub_lvl, int) and sub_lvl > 0 else sub_job
    job_s = f'{main_job}{plvl_s}/{sub_s}'

    kc = int(history.get('kill_count')  or 0)
    dc = int(history.get('death_count') or 0)
    avg_hp = history.get('avg_hp_remaining_pct')
    avg_hp_s = f'{avg_hp:.0f}%' if isinstance(avg_hp, (int, float)) else 'n/a'
    avg_dmg = history.get('avg_damage_taken_pct')
    avg_dmg_s = f'{avg_dmg:.0f}%' if isinstance(avg_dmg, (int, float)) else 'n/a'
    max_dmg = history.get('max_damage_taken_pct')
    max_dmg_s = f'{max_dmg:.0f}%' if isinstance(max_dmg, (int, float)) else 'n/a'
    last_killed = history.get('last_killed_at')
    last_died   = history.get('last_died_at')
    now = time.time()
    def _ago(ts):
        if not isinstance(ts, (int, float)): return 'never'
        d = max(0.0, now - ts)
        if d < 60:    return f'{d:.0f}s ago'
        if d < 3600:  return f'{d/60:.0f}m ago'
        return f'{d/3600:.1f}h ago'

    # Tier-history block: weighted aggregate across ALL mobs of THIS
    # check_type the character has ever fought. Records from past
    # levels are decay-weighted by level distance (~0.25 per level
    # away) so old-level deaths still influence the decision but
    # fade as the character outgrows them. At level 6, a level-5
    # Even-Match death contributes 0.75x; a level-3 one contributes
    # 0.25x. Counts are floats; an entry like "deaths: 1.5" means
    # "two distant-past deaths weighted to 1.5 effective".
    tier_block = ''
    if isinstance(tier_history, dict) and tier_history:
        t_kc  = float(tier_history.get('kill_count')  or 0)
        t_dc  = float(tier_history.get('death_count') or 0)
        t_n   = int(tier_history.get('distinct_mobs') or 0)
        t_avg = tier_history.get('avg_damage_taken_pct')
        t_max = tier_history.get('max_damage_taken_pct')
        t_avg_s = f'{t_avg:.0f}%' if isinstance(t_avg, (int, float)) else 'n/a'
        t_max_s = f'{t_max:.0f}%' if isinstance(t_max, (int, float)) else 'n/a'
        if t_kc + t_dc > 0:
            tier_block = (
                f'\nDifficulty-tier history '
                f'(weighted by level distance; all "{ct_s}" mobs across '
                f'{t_n} distinct mob name{"s" if t_n != 1 else ""}):\n'
                f'  kills (weighted):    {t_kc:.1f}\n'
                f'  deaths (weighted):   {t_dc:.1f}\n'
                f'  avg damage taken:    {t_avg_s}\n'
                f'  max damage taken:    {t_max_s}    <- max is unweighted\n'
            )

    return (
        f'Enemy:\n'
        f'  name:       {name}\n'
        f'  level:      {lvl_s}\n'
        f'  check:      {ct_s}\n'
        f'  conditions: {cond}\n'
        f'  distance:   {dist_s}\n'
        f'\n'
        f'Player:\n'
        f'  job:   {job_s}\n'
        f'  HP:    {hp_s}\n'
        f'  MP:    {mp_s}\n'
        f'\n'
        f'Goal: {goal or "(no explicit user goal)"}\n'
        f'\n'
        f'Fight history (this character vs. "{name}" at this level):\n'
        f'  kills:               {kc}\n'
        f'  deaths:              {dc}\n'
        f'  avg HP remaining:    {avg_hp_s}\n'
        f'  avg damage taken:    {avg_dmg_s}    <- typical fight cost\n'
        f'  max damage taken:    {max_dmg_s}    <- worst single fight\n'
        f'  last killed:         {_ago(last_killed)}\n'
        f'  last died:           {_ago(last_died)}\n'
        f'{tier_block}'
    )


def _parse_decision(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of the model's reply. Tolerant of
    a leading/trailing prose preamble that some local models emit
    despite the instruction."""
    if not text:
        return None
    m = re.search(r'\{.*?\}', text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    decision = obj.get('decision')
    if decision not in ('engage', 'skip', 'rest'):
        return None
    return {
        'decision': decision,
        'reason':   str(obj.get('reason') or '').strip()[:200],
    }


class EngageJudge:
    """Per-call judgment dispatcher. No cache - every request fires a
    fresh LLM call. Caller holds an rid (request id) to poll for the
    verdict, then discards. Pattern matches RestJudge."""

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway | None):
        self.cfg = cfg
        self.llm = llm
        # rid -> verdict dict once the worker finishes. Caller polls
        # via status(rid) and consumes via discard(rid). No cross-call
        # caching - every judgment is fresh against current state.
        self._results: dict[int, dict[str, Any]] = {}
        self._pending: set[int] = set()
        self._next_rid: int = 1
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.llm is not None and self.llm.available

    def invalidate(self) -> None:
        """No-op kept for backward compatibility - the level-up path
        in farming.py calls this. Without a cache there's nothing
        to clear, but we leave the entry point so callers don't
        need to know about the refactor."""
        pass

    def status(self, rid: int) -> dict[str, Any] | None:
        """Returns the verdict dict ('decision': 'engage'|'skip'|'rest'
        |'error', 'reason': ...) once the worker has resolved, or None
        while still pending. Caller should `discard(rid)` after acting
        on the verdict."""
        with self._lock:
            if rid in self._pending:
                return None
            return self._results.get(rid)

    def discard(self, rid: int) -> None:
        with self._lock:
            self._results.pop(rid, None)

    def request(self, mob: dict[str, Any], player: dict[str, Any],
                goal: str, history: dict[str, Any],
                tier_history: dict[str, Any] | None = None) -> int | None:
        """Fire a fresh judgment. Returns the rid for polling, or None
        if the LLM is unavailable (caller should fall back immediately).

        `tier_history` is the aggregate kill/death/damage stats across
        all mobs of THIS check_type at the player's current level -
        lets the judge generalize "Even-Match mobs at this level keep
        killing me" across mobs we haven't personally fought."""
        if not self.available():
            return None
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            self._pending.add(rid)
        t = threading.Thread(
            target=self._worker,
            args=(rid, mob, player, goal, history, tier_history or {}),
            name=f'engage-judge-{rid}', daemon=True,
        )
        t.start()
        return rid

    def _worker(self, rid: int, mob: dict[str, Any],
                player: dict[str, Any], goal: str,
                history: dict[str, Any],
                tier_history: dict[str, Any]) -> None:
        prompt = _build_prompt(mob, player, goal, history, tier_history)
        decision: dict[str, Any] | None = None
        err: str | None = None
        raw_text: str = ''
        t0 = time.time()
        try:
            result = self.llm.run_tool_loop(
                tier='deliberative',
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt,
                tools=[_web_research.WEB_SEARCH_TOOL,
                       _web_research.WEB_FETCH_TOOL],
                tool_handlers=_web_research.make_handlers(self.cfg),
                max_iters=6,
                max_tokens=256,
                source='engage_judge',
            )
            raw_text = result.final_text or ''
            decision = _parse_decision(raw_text)
            if decision is None:
                err = f'unparseable: {raw_text[:160]!r}'
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
        latency = time.time() - t0

        with self._lock:
            self._pending.discard(rid)
            if decision is not None:
                self._results[rid] = decision
            else:
                self._results[rid] = {
                    'decision': 'error',
                    'reason':   err or 'unknown',
                }

        verdict = (decision or {}).get('decision') or 'error'
        reason  = (decision or {}).get('reason') or err
        event_kwargs: dict[str, Any] = {
            'character':   self.cfg.character,
            'source':      'engage_judge',
            'type_':       'judgment',
            'rid':         rid,
            'mob_name':    mob.get('name'),
            'player_lvl':  player.get('level'),
            'decision':    verdict,
            'reason':      reason,
            'latency_s':   round(latency, 3),
            'prompt':      prompt[:2000],
            'raw_response': raw_text[:500],
        }
        _events.append(self.cfg.paths.events_file(), **event_kwargs)
        # /echo is fired by the consumer (farming._consult_engage_judge)
        # when the verdict is APPLIED, so cache hits get the same
        # narration as the original LLM call. Don't double-echo here.
