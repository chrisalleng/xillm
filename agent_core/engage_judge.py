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
    'You are the inner monologue of an autonomous FFXI player agent. '
    'Given the enemy, player state, current goal, and per-mob fight '
    'history, decide what to do with this specific enemy now. Reply '
    'with ONLY a JSON object - no prose, no code fences - of the shape: '
    '{"decision": "engage" | "skip" | "rest", '
    '"reason": "<one first-person sentence in the agent\'s voice>"}.\n\n'
    'DECISION PRIORITY (apply in order):\n'
    '1. CURRENT HP is the dominant signal, not history. If HP >= 80%% '
    '   and check is Easy Prey / Decent Challenge / Even Match, ENGAGE '
    '   regardless of past deaths. "Still recovering" is FALSE at 80%%+ '
    '   HP - you ARE recovered. One past death amid many kills is '
    '   normal variance, not a reason to skip.\n'
    '2. Skip only when the mob is genuinely too dangerous NOW: check '
    '   is Tough/Very Tough/Incredibly Tough at current level, OR '
    '   avg_damage_taken_pct on this mob is high enough that current '
    '   HP could not absorb it (e.g. avg dmg 60%% with current HP 50%%).\n'
    '3. Rest only when HP/MP are low enough that ANY fight is unsafe '
    '   (HP < ~50%%) - not as a way to be cautious before a routine fight.\n\n'
    'The reason field MUST be ONE short sentence under 90 characters '
    '- sounds like a player muttering to themselves about the fight.\n\n'
    'FORMATTING RULES:\n'
    '- The AGENT speaks in first-person ("My HP is...", "I\'ll skip"). '
    '  The MOB is referenced by name + con label, e.g. "the Wild Rabbit '
    '  is Easy Prey" - that is correct usage, not third-person preamble.\n'
    '- ONE sentence, no narrative or flavor. Just the reasoning.\n'
    '- DO NOT reference past fights or "still recovering" / "still '
    '  healing" - state the CURRENT decision only.\n'
    '- DO mention HP%%, level, or con label - they are the deciding '
    '  factors. The HP/MP values in the prompt are bucketed to 10%% '
    '  and prefixed with "around" (e.g. "around 90%%"). Use that exact '
    '  phrasing - do NOT invent a more precise number, and do NOT drop '
    '  the "around" qualifier. So "my HP is around 90%%" is correct; '
    '  "my HP is at 95%%" or "my HP is 90%%" is wrong.\n'
    '- If you mention the mob\'s con label, use the EXACT label from '
    '  the prompt (Easy Prey, Decent Challenge, Even Match, Tough, etc.).\n\n'
    'GOOD examples (mention agent, mob name, mob con, decision):\n'
    '  "My HP is around 100%%, the Wild Rabbit is Easy Prey, going in."\n'
    '  "I\'m around 30%% HP and the Goblin Smithy is a Decent Challenge '
    '  - too risky, skipping."\n'
    '  "The Tunnel Worm is Tough at this level, I\'ll skip it."\n'
    '  "MP\'s around 0 and a Wild Rabbit linker is next to me, sitting."\n'
    'BAD examples:\n'
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
                  goal: str, history: dict[str, Any]) -> str:
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
    # Bucket HP/MP to the same 10% granularity the cache key uses. The
    # cache is (name, level, hp_bucket); echoing a cached reason that
    # cites an exact percent is misleading once HP shifts within the
    # same bucket. Round to nearest 10% (clipped to 100) and tell the
    # LLM to phrase it with "around X%" so it stays accurate for any
    # HP in the bucket.
    def _bucket_pct(p: Any) -> int | None:
        if not isinstance(p, (int, float)):
            return None
        return min(int(p) // HP_BUCKET_PCT * HP_BUCKET_PCT, 100)
    hp_b = _bucket_pct(hp)
    mp_b = _bucket_pct(mp)
    hp_s = f'around {hp_b}%' if hp_b is not None else '?'
    mp_s = f'around {mp_b}%' if mp_b is not None else '?'
    sub_s = f'{sub_job}{sub_lvl}' if isinstance(sub_lvl, int) and sub_lvl > 0 else sub_job
    job_s = f'{main_job}{plvl_s}/{sub_s}'

    kc = int(history.get('kill_count')  or 0)
    dc = int(history.get('death_count') or 0)
    avg_hp = history.get('avg_hp_remaining_pct')
    avg_hp_s = f'{avg_hp:.0f}%' if isinstance(avg_hp, (int, float)) else 'n/a'
    avg_dmg = history.get('avg_damage_taken_pct')
    avg_dmg_s = f'{avg_dmg:.0f}%' if isinstance(avg_dmg, (int, float)) else 'n/a'
    last_killed = history.get('last_killed_at')
    last_died   = history.get('last_died_at')
    now = time.time()
    def _ago(ts):
        if not isinstance(ts, (int, float)): return 'never'
        d = max(0.0, now - ts)
        if d < 60:    return f'{d:.0f}s ago'
        if d < 3600:  return f'{d/60:.0f}m ago'
        return f'{d/3600:.1f}h ago'

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
        f'Fight history (this character vs. "{name}" in this zone):\n'
        f'  kills:               {kc}\n'
        f'  deaths:              {dc}\n'
        f'  avg HP remaining:    {avg_hp_s}\n'
        f'  avg damage taken:    {avg_dmg_s}    <- typical fight cost\n'
        f'  last killed:         {_ago(last_killed)}\n'
        f'  last died:           {_ago(last_died)}\n'
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
    """Owns the (name, lvl) -> judgment cache and worker dispatch. One
    instance per orchestrator; passed into FarmingDirector by main.py."""

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway | None):
        self.cfg = cfg
        self.llm = llm
        # Cache key is (name, level, hp_bucket). Crossing a bucket
        # boundary (default every 25% HP) forces a fresh LLM call so
        # the reason text reflects current HP, not whatever HP was
        # captured on the original judgment.
        self._cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._pending: set[tuple[str, int, int]] = set()
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def lookup(self, name: str, player_lvl: int,
               hp_pct: float | int | None) -> dict[str, Any] | None:
        """Synchronous cache read. Returns the cached judgment or None.
        Error entries auto-expire after ERROR_CACHE_TTL_S so we re-try
        the LLM once it's recovered."""
        key = (name, player_lvl, _hp_bucket(hp_pct))
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.get('decision') == 'error':
                if time.time() - float(entry.get('ts') or 0.0) > ERROR_CACHE_TTL_S:
                    self._cache.pop(key, None)
                    return None
            return entry

    def is_pending(self, name: str, player_lvl: int,
                   hp_pct: float | int | None) -> bool:
        with self._lock:
            return (name, player_lvl, _hp_bucket(hp_pct)) in self._pending

    def request(self, mob: dict[str, Any], player: dict[str, Any],
                goal: str, history: dict[str, Any]) -> None:
        """Fire-and-forget judgment. Dedup'd by (name, level, hp_bucket)
        so repeated calls while a worker is in flight don't pile up."""
        name = mob.get('name')
        plvl = player.get('level')
        if not name or plvl is None or self.llm is None or not self.llm.available:
            return
        key = (name, int(plvl), _hp_bucket(player.get('hp_pct')))
        with self._lock:
            if key in self._cache or key in self._pending:
                return
            self._pending.add(key)
        t = threading.Thread(
            target=self._worker, args=(key, mob, player, goal, history),
            name=f'engage-judge-{name}', daemon=True,
        )
        t.start()

    def _worker(self, key: tuple[str, int], mob: dict[str, Any],
                player: dict[str, Any], goal: str,
                history: dict[str, Any]) -> None:
        prompt = _build_prompt(mob, player, goal, history)
        # Echo the key fields we're sending to the LLM (job/level/HP +
        # mob name/con/level) BEFORE the call so a watcher can verify
        # the values were correct as the decision is forming. Pairs
        # with the reply echo fired after the call returns.
        try:
            hp = player.get('hp_pct')
            mp = player.get('mp_pct')
            mj = player.get('main_job') or '?'
            plvl = player.get('level')
            ct = mob.get('check_type')
            ct_label = CHECK_TYPE_LABEL.get(ct, '?') if isinstance(ct, int) else '?'
            mob_name = mob.get('name') or '?'
            mob_lvl = mob.get('level')
            hp_s = f'HP {hp:.0f}%' if isinstance(hp, (int, float)) else 'HP?'
            mp_s = f'MP {mp:.0f}%' if isinstance(mp, (int, float)) else 'MP?'
            plvl_s = f'{plvl}' if plvl is not None else '?'
            mlvl_s = f'lvl{mob_lvl}' if mob_lvl is not None else 'lvl?'
            _echo.to_chat(self.cfg, 'query',
                          f'{mj}{plvl_s} {hp_s} {mp_s} vs {mob_name} '
                          f'{ct_label} {mlvl_s}')
        except Exception:
            pass
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
                max_iters=4,
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
            self._pending.discard(key)
            if decision is not None:
                # 'rest' is a response to transient state (current HP/MP)
                # and isn't a property of the mob at this level - caching
                # it would loop forever (rest -> cache hit -> rest -> ...).
                # 'engage' and 'skip' are stable per (name, lvl) so they
                # cache cleanly.
                if decision.get('decision') == 'rest':
                    pass  # don't cache; re-fire next time
                else:
                    decision['ts'] = time.time()
                    self._cache[key] = decision
            else:
                # Cache the error so callers fall back to hardcoded
                # buckets without re-firing the request every tick.
                # The TTL in lookup() will evict it after 30s.
                self._cache[key] = {
                    'decision': 'error',
                    'reason':   err or 'unknown',
                    'ts':       time.time(),
                }

        # Echo the LLM's raw text response verbatim so a watcher can
        # see exactly what the model returned (verifying for
        # hallucinated numbers etc.). Errors / empty replies surface
        # as "(no reply)" so the absence is visible in chat.
        try:
            _echo.to_chat(self.cfg, 'reply', raw_text.strip() or '(no reply)')
        except Exception:
            pass
        verdict = (decision or {}).get('decision') or 'error'
        reason  = (decision or {}).get('reason') or err
        # Log the verbatim prompt and raw response on every judgment.
        # Lets us verify the HP%% / level / con label we sent the LLM
        # before assuming it hallucinated. Truncated to keep
        # events.jsonl bounded but generous enough to see the full
        # player + history blocks.
        event_kwargs: dict[str, Any] = {
            'character':   self.cfg.character,
            'source':      'engage_judge',
            'type_':       'judgment',
            'mob_name':    key[0],
            'player_lvl':  key[1],
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
