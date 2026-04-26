"""LLM gateway: the only place agent_core talks to Anthropic.

Every tier (reactive / periodic / deliberative) uses the same client
but with a different default model. Each call:
    - records prompt & response token counts and latency to the event log
    - serialises tool-use turns into structured commands the orchestrator
      then dispatches into `commands/<character>/*.json`

Phase 1 scope: a working client that can call the API with a tiny
"are you alive?" prompt — enough to verify creds + model IDs at
startup. The full tool surface (read_world_state, update_goals, etc.)
arrives in Phase 2 alongside the goal manager.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import anthropic  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - dependency optional during early dev
    anthropic = None  # type: ignore[assignment]

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
    """Thin wrapper around the Anthropic SDK with our tiering + logging."""

    def __init__(self, cfg: _config.Config):
        self.cfg = cfg
        if anthropic is None:
            self.client = None
        else:
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            self.client = anthropic.Anthropic(api_key=api_key) if api_key else None

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
                'Anthropic client unavailable. Install `anthropic` and '
                'set ANTHROPIC_API_KEY to use the LLM gateway.'
            )
        import time
        model = self._model_for(tier)
        t0 = time.time()
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': prompt}],
        )
        latency = time.time() - t0
        text = ''.join(
            block.text for block in resp.content if getattr(block, 'type', None) == 'text'
        )
        result = CallResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
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

    def healthcheck(self) -> bool:
        """One quick reactive-tier call to confirm creds + model ID work."""
        if not self.available:
            return False
        try:
            r = self.call('reactive', 'Reply with the single word: ok', max_tokens=8)
        except Exception:
            return False
        return 'ok' in r.text.lower()
