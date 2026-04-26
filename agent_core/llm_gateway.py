"""LLM gateway: the only place agent_core talks to the model provider.

We use the OpenAI Python SDK pointed at any OpenAI-compatible endpoint
(Groq for free-tier dev, OpenAI proper, Anthropic's OpenAI-compat shim,
Together, a local llama.cpp server, etc). The provider URL and API key
come from `Config.llm.base_url` / `Config.llm.api_key`.

Each call:
    - selects the per-tier model from Config.llm
    - records token counts + latency to `events.jsonl` so the dashboard
      can show running cost
    - returns text + the usage figures to the caller

Phase 1 scope: a working `call()` that takes a plain prompt and returns
text, plus a `healthcheck()` that does one cheap reactive-tier call.
The full tool surface (read_world_state, update_goals, etc.) lands in
Phase 2 alongside the goal manager — tool calls go through the same
client, just with a `tools=[...]` param.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from openai import OpenAI  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

from . import config as _config
from . import events as _events


@dataclass
class CallResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    model: str


class LLMGateway:
    """Thin wrapper around an OpenAI-compatible client with our tiering + logging."""

    def __init__(self, cfg: _config.Config):
        self.cfg = cfg
        if OpenAI is None or not cfg.llm.api_key:
            self.client = None
        else:
            self.client = OpenAI(
                api_key=cfg.llm.api_key,
                base_url=cfg.llm.base_url,
            )

    @property
    def available(self) -> bool:
        """True iff we have both the SDK and an API key."""
        return self.client is not None

    def _model_for(self, tier: str) -> str:
        return getattr(self.cfg.llm, tier)

    def call(self, tier: str, prompt: str, *, max_tokens: int = 1024) -> CallResult:
        """Dispatch a single text prompt at the named tier. Phase 1
        helper — no tools, no streaming. Tools come in Phase 2."""
        if self.client is None:
            raise RuntimeError(
                'LLM client unavailable. Install `openai` and set '
                'AGENT_LLM_API_KEY (or put it in agent_core/config.toml) '
                'to use the LLM gateway.'
            )
        model = self._model_for(tier)
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': prompt}],
        )
        latency = time.time() - t0
        text = (resp.choices[0].message.content or '') if resp.choices else ''
        # Groq + OpenAI both populate `usage`; some providers omit it.
        usage = getattr(resp, 'usage', None)
        in_tok = getattr(usage, 'prompt_tokens', 0) if usage else 0
        out_tok = getattr(usage, 'completion_tokens', 0) if usage else 0
        result = CallResult(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_s=latency,
            model=model,
        )
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source='llm_gateway',
            type_='llm_call',
            tier=tier,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_s=round(latency, 3),
        )
        return result

    def healthcheck(self) -> CallResult | None:
        """One reactive-tier round trip to confirm creds + model ID work.
        Returns the CallResult on success, None on any failure."""
        if not self.available:
            return None
        try:
            return self.call(
                'reactive',
                'Reply with the single word: ok',
                max_tokens=8,
            )
        except Exception:
            return None

    # -------------------------------------------------------------------
    # Multi-turn tool-use loop
    #
    # The single-turn `Planner.plan()` pattern works for "decide and
    # commit" calls (set goals, deploy gambits) where the LLM doesn't
    # need to read state mid-decision. Other surfaces — chat handling,
    # combat-log review — need to query, decide, possibly act, possibly
    # query more. That's what this helper is for.
    #
    # Contract: pass a tools list (OpenAI shape) and a parallel handler
    # dict (name → callable). Each iteration the LLM either emits tool
    # calls (we run them, append the results, continue) or returns a
    # plain message (we stop and hand the text back). max_iters caps the
    # loop so a misbehaving model can't run up unbounded tokens.
    # -------------------------------------------------------------------

    def run_tool_loop(
        self,
        tier: str,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[[dict[str, Any]], Any]],
        *,
        max_iters: int = 5,
        max_tokens: int = 2048,
        source: str = 'tool_loop',
    ) -> 'ToolLoopResult':
        """Run a tool-use conversation. Returns a ToolLoopResult with
        the final assistant text, the list of (name, args, result) tool
        calls actually executed, and aggregated token counts."""
        if self.client is None:
            raise RuntimeError(
                'LLM client unavailable. Install `openai` and set '
                'AGENT_LLM_API_KEY (or put it in agent_core/config.toml).'
            )
        model = self._model_for(tier)
        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_prompt},
        ]
        applied: list[tuple[str, dict[str, Any], Any]] = []
        in_total = out_total = 0
        latency_total = 0.0
        final_text = ''
        terminated = False
        last_iter_had_calls = False

        for iter_idx in range(max_iters):
            t0 = time.time()
            resp = self.client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice='auto',  # let the model stop when it's done
                messages=messages,
            )
            latency_total += time.time() - t0
            usage = getattr(resp, 'usage', None)
            if usage is not None:
                in_total += getattr(usage, 'prompt_tokens', 0) or 0
                out_total += getattr(usage, 'completion_tokens', 0) or 0

            choice = resp.choices[0] if resp.choices else None
            if choice is None:
                break
            assistant_msg = choice.message
            tool_calls = getattr(assistant_msg, 'tool_calls', None) or []

            # Echo the assistant turn back into the conversation. Keep
            # `tool_calls` so the next request stays well-formed; the
            # OpenAI shape requires every tool result to reference its
            # originating call id.
            echoed: dict[str, Any] = {
                'role':    'assistant',
                'content': assistant_msg.content or '',
            }
            if tool_calls:
                echoed['tool_calls'] = [
                    {
                        'id':       call.id,
                        'type':     'function',
                        'function': {
                            'name':      call.function.name,
                            'arguments': call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ]
            messages.append(echoed)

            if not tool_calls:
                final_text = assistant_msg.content or ''
                terminated = True
                break

            last_iter_had_calls = True
            for call in tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or '{}')
                except json.JSONDecodeError as e:
                    result: Any = {'error': f'malformed args: {e}'}
                    applied.append((name, {}, result))
                    messages.append({
                        'role':         'tool',
                        'tool_call_id': call.id,
                        'content':      json.dumps(result),
                    })
                    continue
                handler = tool_handlers.get(name)
                if handler is None:
                    result = {'error': f'unknown tool {name!r}'}
                else:
                    try:
                        result = handler(args)
                    except Exception as e:
                        result = {'error': f'{type(e).__name__}: {e}'}
                applied.append((name, args, result))
                # Tool messages must be JSON strings per the OpenAI spec.
                # Wrap any non-string result in JSON; strings pass through
                # so a handler can return preformatted text if it wants.
                payload = result if isinstance(result, str) else json.dumps(result, default=str)
                messages.append({
                    'role':         'tool',
                    'tool_call_id': call.id,
                    'content':      payload,
                })

        # Log a single rolled-up event so the dashboard's per-call cost
        # view stays stable. Per-turn detail is recoverable from the
        # provider's logs if we ever need it.
        _events.append(
            self.cfg.paths.events_file(),
            character=self.cfg.character,
            source=source,
            type_='llm_tool_loop',
            tier=tier,
            model=model,
            iterations=iter_idx + 1 if (terminated or last_iter_had_calls) else 0,
            input_tokens=in_total,
            output_tokens=out_total,
            latency_s=round(latency_total, 3),
            terminated=terminated,
            tool_calls=len(applied),
        )
        return ToolLoopResult(
            final_text=final_text,
            applied=applied,
            input_tokens=in_total,
            output_tokens=out_total,
            latency_s=latency_total,
            iterations=iter_idx + 1,
            terminated=terminated,
            model=model,
        )


@dataclass
class ToolLoopResult:
    final_text: str
    # (tool_name, args, result) per executed call, in order.
    applied: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    iterations: int = 0
    # True if the loop ended because the LLM returned no tool calls
    # (clean termination); False if we hit max_iters mid-tool-call.
    terminated: bool = False
    model: str = ''
