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

import time
from dataclasses import dataclass

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
