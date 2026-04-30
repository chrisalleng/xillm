"""LLM-driven post-kill rest/continue decision.

After every kill the FarmingDirector fires a deliberative-tier judgment
asking whether to rest before the next mob or push on. The decision
depends on HP/MP, recent fight outcomes for the same mob name (the
fight_history `avg_hp_remaining_pct` is a strong signal - "I usually
finish this fight at 80% HP" -> push on; "I usually finish at 30%" ->
rest), and the user's goal text (push hard for an XP target vs.
conservative play).

No cache: HP/MP change every tick, so each post-kill is a fresh
decision. Per-request state keyed by an incrementing rid the caller
holds onto until consumed.

Failure modes mirror engage_judge: parse errors / LLM down land as
{'decision': 'error'}; the caller falls back to the directive's
hardcoded rest_hp_pct threshold so the agent keeps moving.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from . import config as _config
from . import events as _events
from . import llm_gateway as _llm
from . import web_research as _web_research


SYSTEM_PROMPT = (
    _web_research.ERA_CONSTRAINT + '\n\n'
    'You are the inner monologue of an autonomous FFXI player agent. '
    'After killing a mob, decide whether to rest (sit / /heal until '
    'HP+MP recover) or continue immediately to the next mob. Reply with '
    'ONLY a JSON object - no prose, no code fences - of the shape: '
    '{"decision": "rest" | "continue", '
    '"reason": "<one first-person sentence in the agent\'s voice>"}. '
    'The reason field MUST read like the agent thinking out loud - e.g. '
    '"I\'m at 45% HP and there\'s a goblin nearby that linked on me '
    'last time, I should rest first" or "I\'m still at 88% HP, no need '
    'to sit, let me find the next one". DO NOT use third-person, DO NOT '
    'use bullet points, DO NOT cite stats by their field names. Rest '
    'when the player needs HP/MP to safely engage the next fight, OR '
    'when nearby mobs include known aggressors / linkers / drainers '
    'the player is not at safe HP for. Continue when the player is '
    'healthy enough that resting would just waste time the goal could '
    'be using to gain XP. If a nearby mob name is unfamiliar, use '
    'web_search to check whether it is aggressive or has special '
    'mechanics before deciding.'
)


def _build_prompt(player: dict[str, Any], session: dict[str, Any],
                  history: dict[str, Any], goal: str,
                  nearby: list[dict[str, Any]] | None = None) -> str:
    main_job = player.get('main_job') or '?'
    sub_job  = player.get('sub_job')  or '-'
    sub_lvl  = player.get('sub_lvl')
    plvl  = player.get('level')
    hp    = player.get('hp_pct')
    mp    = player.get('mp_pct')
    plvl_s = f'{plvl}' if plvl is not None else '?'
    hp_s = f'{hp:.0f}%' if isinstance(hp, (int, float)) else '?'
    mp_s = f'{mp:.0f}%' if isinstance(mp, (int, float)) else '?'
    sub_s = f'{sub_job}{sub_lvl}' if isinstance(sub_lvl, int) and sub_lvl > 0 else sub_job
    job_s = f'{main_job}{plvl_s}/{sub_s}'

    last_mob = session.get('last_killed_name') or '?'
    kills_total = int(session.get('kills_total') or 0)

    kc = int(history.get('kill_count')  or 0)
    dc = int(history.get('death_count') or 0)
    avg_hp = history.get('avg_hp_remaining_pct')
    avg_hp_s = f'{avg_hp:.0f}%' if isinstance(avg_hp, (int, float)) else 'n/a'
    avg_dmg = history.get('avg_damage_taken_pct')
    avg_dmg_s = f'{avg_dmg:.0f}%' if isinstance(avg_dmg, (int, float)) else 'n/a'

    # Nearby mobs block - how the agent learns about its surroundings
    # over time. Each entry shows the mob's name, distance, and the
    # accumulated kill/death stats for that name in this zone. That
    # lets the judge spot patterns like "I died 3 times to Goblin
    # Pathfinders nearby - rest before they aggro" without anyone
    # writing rules. Cap at the 8 closest so the prompt stays compact.
    nearby_block = ''
    if nearby:
        rows = []
        for n in nearby[:8]:
            name = n.get('name') or '?'
            dist = n.get('distance')
            dist_s = f'{dist:.0f}y' if isinstance(dist, (int, float)) else '?'
            n_kc = int(n.get('kill_count') or 0)
            n_dc = int(n.get('death_count') or 0)
            n_avg = n.get('avg_hp_remaining_pct')
            n_avg_s = f'{n_avg:.0f}%' if isinstance(n_avg, (int, float)) else 'n/a'
            rows.append(
                f'  {name:<24} {dist_s:>5} | kills {n_kc} deaths {n_dc} '
                f'avg HP remaining {n_avg_s}'
            )
        nearby_block = (
            '\nNearby mobs (closest first; history is per-name in this zone):\n'
            + '\n'.join(rows) + '\n'
        )

    return (
        f'Player:\n'
        f'  job:   {job_s}\n'
        f'  HP:    {hp_s}\n'
        f'  MP:    {mp_s}\n'
        f'\n'
        f'Session:\n'
        f'  kills so far:        {kills_total}\n'
        f'  most recently killed: {last_mob}\n'
        f'\n'
        f'Fight history vs. "{last_mob}" (this character, this zone):\n'
        f'  kills:               {kc}\n'
        f'  deaths:              {dc}\n'
        f'  avg HP remaining:    {avg_hp_s}\n'
        f'  avg damage taken:    {avg_dmg_s}\n'
        f'{nearby_block}'
        f'\nGoal: {goal or "(no explicit user goal)"}\n'
    )


def _parse_decision(text: str) -> dict[str, Any] | None:
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
    if decision not in ('rest', 'continue'):
        return None
    return {
        'decision': decision,
        'reason':   str(obj.get('reason') or '').strip()[:200],
    }


class RestJudge:
    """One instance per orchestrator. Each post-kill call is a separate
    request - caller holds the rid until consumed via `status()`."""

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway | None):
        self.cfg = cfg
        self.llm = llm
        self._results: dict[int, dict[str, Any]] = {}
        self._pending: set[int] = set()
        self._next_rid: int = 1
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.llm is not None and self.llm.available

    def request(self, player: dict[str, Any], session: dict[str, Any],
                history: dict[str, Any], goal: str,
                nearby: list[dict[str, Any]] | None = None) -> int | None:
        """Fire a judgment. Returns the rid for polling, or None if the
        LLM is unavailable (caller should fall back immediately).

        `nearby` is the post-kill snapshot's nearby_enemies list with
        each entry annotated with this character's per-name fight
        history in this zone (kill_count, death_count,
        avg_hp_remaining_pct). The judge uses it to spot dangerous
        neighbours that haven't aggroed yet - e.g. an aggressor we've
        died to before that's now within scan range."""
        if not self.available():
            return None
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            self._pending.add(rid)
        t = threading.Thread(
            target=self._worker,
            args=(rid, player, session, history, goal, nearby),
            name=f'rest-judge-{rid}', daemon=True,
        )
        t.start()
        return rid

    def status(self, rid: int) -> dict[str, Any] | None:
        """Returns the verdict dict (with 'decision': 'rest'|'continue'|'error')
        once available, or None while pending. Caller should `discard(rid)`
        after acting on the verdict."""
        with self._lock:
            if rid in self._pending:
                return None
            return self._results.get(rid)

    def discard(self, rid: int) -> None:
        with self._lock:
            self._results.pop(rid, None)

    def _worker(self, rid: int, player: dict[str, Any],
                session: dict[str, Any], history: dict[str, Any],
                goal: str, nearby: list[dict[str, Any]] | None) -> None:
        prompt = _build_prompt(player, session, history, goal, nearby)
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
                source='rest_judge',
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
                self._results[rid] = {'decision': 'error', 'reason': err or 'unknown'}

        verdict = self._results[rid].get('decision')
        reason  = self._results[rid].get('reason')
        event_kwargs: dict[str, Any] = {
            'character': self.cfg.character,
            'source':    'rest_judge',
            'type_':     'judgment',
            'rid':       rid,
            'decision':  verdict,
            'reason':    reason,
            'latency_s': round(latency, 3),
        }
        if decision is None:
            event_kwargs['raw_response'] = raw_text[:500]
        _events.append(self.cfg.paths.events_file(), **event_kwargs)
        # /echo is fired by the consumer (farming._tick_judging_rest)
        # when the verdict is consumed. No double-echo here.
