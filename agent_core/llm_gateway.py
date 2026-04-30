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
Phase 2 alongside the goal manager - tool calls go through the same
client, just with a `tools=[...]` param.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from openai import OpenAI  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

from . import config as _config
from . import events as _events


# Default Ollama context window. Ollama defaults to 2048 which silently
# truncates anything above it - way too small for our planner prompt.
# 16384 covers our worst-case (system prompt + 200-zone catalog + tools)
# with headroom; well within Llama 3.1 / Qwen 2.5 native limits.
OLLAMA_NUM_CTX = 16384


@dataclass
class NormalizedToolCall:
    """Provider-agnostic tool call. OpenAI gives us arguments as a JSON
    string we have to parse; Ollama gives us a parsed dict directly. We
    normalize to a parsed dict either way so callers don't have to care."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """Provider-agnostic chat-completion result."""
    text: str
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ''
    latency_s: float = 0.0
    model: str = ''
    # Raw assistant message in the provider's native shape, ready to be
    # appended back into a multi-turn conversation. Different providers
    # need different field combinations to keep history well-formed.
    raw_assistant_message: dict[str, Any] = field(default_factory=dict)


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
        # Lazy-built OpenAI clients keyed by (base_url, api_key). Tiers
        # may target different providers (e.g. local Ollama for reactive,
        # Groq for deliberative) so we cache one client per endpoint
        # instead of a single self.client.
        self._clients: dict[tuple[str, str], Any] = {}
        # Eagerly build the default client so legacy `self.client`
        # callers (and the healthcheck) keep working without per-tier
        # routing knowledge. Falls back to None if SDK or key missing,
        # preserving the existing `available` semantics.
        if OpenAI is not None and cfg.llm.api_key:
            self.client = self._client_for(cfg.llm.base_url, cfg.llm.api_key)
        else:
            self.client = None

    def _client_for(self, base_url: str, api_key: str):
        """Return (and lazily cache) an OpenAI SDK client for this
        endpoint. Used for OpenAI-compat tiers; Ollama-native tiers
        bypass this entirely and POST via urllib."""
        if OpenAI is None:
            return None
        key = (base_url, api_key)
        c = self._clients.get(key)
        if c is None:
            c = OpenAI(api_key=api_key, base_url=base_url)
            self._clients[key] = c
        return c

    def _resolve_tier(self, tier: str) -> tuple[str, str, str, str]:
        """Resolve (model, base_url, api_key, provider) for a tier.
        Per-tier overrides win; otherwise fall back to the [llm]
        defaults. Empty per-tier endpoint fields are treated as 'inherit'."""
        model = getattr(self.cfg.llm, tier)
        base_url = getattr(self.cfg.llm, f'{tier}_base_url', '') or self.cfg.llm.base_url
        api_key = getattr(self.cfg.llm, f'{tier}_api_key', '') or self.cfg.llm.api_key
        provider = getattr(self.cfg.llm, f'{tier}_provider', '') or self.cfg.llm.provider
        return model, base_url, api_key, provider

    @property
    def available(self) -> bool:
        """True iff we have an OpenAI client (Ollama path also requires
        the SDK to be importable for the healthcheck-style call helper,
        though the native /api/chat path uses urllib only)."""
        return self.client is not None

    @property
    def is_ollama(self) -> bool:
        """True iff the DEFAULT provider is ollama. Per-tier routing
        decisions should use _resolve_tier()'s provider value, not this."""
        return self.cfg.llm.provider == 'ollama'

    # -------------------------------------------------------------------
    # Provider-agnostic chat completion with tool support.
    #
    # OpenAI's compat shim (Groq, OpenAI proper, etc) -> goes through
    # the openai SDK. Ollama's native API -> urllib POST to /api/chat,
    # which correctly parses tool calls for models whose format the
    # OpenAI shim mangles (Qwen 2.5, GLM, etc). Both paths return a
    # NormalizedToolCall list with arguments already parsed to dicts.
    # -------------------------------------------------------------------

    def tool_chat(
        self,
        tier: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = 'auto',
        max_tokens: int = 2048,
    ) -> ChatResponse:
        """Single-turn chat completion with optional tool calling.
        Routes per tier - `tier` is one of reactive/periodic/deliberative
        and the gateway looks up the model + endpoint + provider from
        config (with per-tier overrides honoured)."""
        model, base_url, api_key, provider = self._resolve_tier(tier)
        if provider == 'ollama':
            return self._tool_chat_ollama(
                model, base_url, messages, tools, tool_choice, max_tokens,
            )
        client = self._client_for(base_url, api_key)
        return self._tool_chat_openai(
            client, model, messages, tools, tool_choice, max_tokens,
        )

    def _tool_chat_openai(
        self,
        client,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        max_tokens: int,
    ) -> ChatResponse:
        if client is None:
            raise RuntimeError('OpenAI client unavailable (no api_key?)')
        kwargs: dict[str, Any] = {
            'model':       model,
            'max_tokens':  max_tokens,
            'messages':    messages,
        }
        if tools:
            kwargs['tools']       = tools
            kwargs['tool_choice'] = tool_choice
        t0 = time.time()
        resp = client.chat.completions.create(**kwargs)
        latency = time.time() - t0
        choice = resp.choices[0] if resp.choices else None
        if choice is None:
            return ChatResponse(text='', latency_s=latency, model=model)
        msg = choice.message
        raw_calls = getattr(msg, 'tool_calls', None) or []
        norm: list[NormalizedToolCall] = []
        for c in raw_calls:
            try:
                args = json.loads(c.function.arguments or '{}')
            except json.JSONDecodeError:
                args = {}
            norm.append(NormalizedToolCall(
                id=c.id, name=c.function.name, arguments=args,
            ))
        # Echo shape for multi-turn: include tool_calls verbatim so
        # OpenAI's shim sees a well-formed assistant turn on the
        # follow-up request.
        echo: dict[str, Any] = {'role': 'assistant', 'content': msg.content or ''}
        if raw_calls:
            echo['tool_calls'] = [
                {
                    'id':       c.id,
                    'type':     'function',
                    'function': {
                        'name':      c.function.name,
                        'arguments': c.function.arguments,
                    },
                }
                for c in raw_calls
            ]
        usage = getattr(resp, 'usage', None)
        return ChatResponse(
            text=msg.content or '',
            tool_calls=norm,
            input_tokens=getattr(usage, 'prompt_tokens', 0) or 0 if usage else 0,
            output_tokens=getattr(usage, 'completion_tokens', 0) or 0 if usage else 0,
            finish_reason=getattr(choice, 'finish_reason', '') or '',
            latency_s=latency,
            model=model,
            raw_assistant_message=echo,
        )

    def _tool_chat_ollama(
        self,
        model: str,
        base_url: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str,  # ollama ignores this; tools-or-not is the only switch
        max_tokens: int,
    ) -> ChatResponse:
        # Ollama wants base_url WITHOUT the /v1 suffix the OpenAI shim uses.
        base = base_url.rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        url = f'{base}/api/chat'
        body: dict[str, Any] = {
            'model':    model,
            'messages': messages,
            'stream':   False,
            'options': {
                'num_ctx':     OLLAMA_NUM_CTX,
                'num_predict': max_tokens,
            },
        }
        if tools:
            body['tools'] = tools
        # Qwen3.x defaults to "auto" thinking which routes the actual
        # answer into a `thinking` block and leaves `content` empty.
        # Set think=false unconditionally so the answer always lands in
        # `content` - applies to JSON-output judges (tools=None) AND
        # tool-call requests. Models that don't support `think` ignore
        # the field, so this is safe regardless of which model the tier
        # is currently routed to. (Was previously gated on `tools`,
        # which left engage_judge / rest_judge getting empty content.)
        body['think'] = False
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            raise RuntimeError(f'Ollama HTTP {e.code}: {err_body}') from e
        latency = time.time() - t0
        data = json.loads(raw.decode('utf-8'))
        msg = data.get('message') or {}
        text = msg.get('content', '') or ''
        raw_calls = msg.get('tool_calls') or []
        norm: list[NormalizedToolCall] = []
        for i, c in enumerate(raw_calls):
            fn = c.get('function') or {}
            args = fn.get('arguments')
            if isinstance(args, str):
                # Some Ollama-served models hand back arg-strings; parse.
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            elif not isinstance(args, dict):
                args = {}
            # Ollama doesn't issue tool-call ids; synthesize a stable one
            # so multi-turn loops can pair tool results back to the call.
            norm.append(NormalizedToolCall(
                id=f'ollama_{int(time.time() * 1000)}_{i}',
                name=fn.get('name', '') or '',
                arguments=args,
            ))
        # Echo shape: Ollama's /api/chat echoes back whatever shape we
        # send, including its own tool_calls form. Use the message dict
        # nearly verbatim so a follow-up request stays well-formed.
        echo: dict[str, Any] = {'role': 'assistant', 'content': text}
        if raw_calls:
            echo['tool_calls'] = raw_calls
        return ChatResponse(
            text=text,
            tool_calls=norm,
            input_tokens=int(data.get('prompt_eval_count') or 0),
            output_tokens=int(data.get('eval_count') or 0),
            finish_reason='tool_calls' if norm else 'stop',
            latency_s=latency,
            model=model,
            raw_assistant_message=echo,
        )

    def _model_for(self, tier: str) -> str:
        return getattr(self.cfg.llm, tier)

    def call(self, tier: str, prompt: str, *, max_tokens: int = 1024) -> CallResult:
        """Dispatch a single text prompt at the named tier. Routes through
        tool_chat() with no tools so the per-tier provider routing kicks
        in (rather than always going through the default-endpoint client)."""
        cr = self.tool_chat(
            tier,
            [{'role': 'user', 'content': prompt}],
            tools=None,
            max_tokens=max_tokens,
        )
        model, _, _, _ = self._resolve_tier(tier)
        result = CallResult(
            text=cr.text,
            input_tokens=cr.input_tokens,
            output_tokens=cr.output_tokens,
            latency_s=cr.latency_s,
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
            latency_s=round(cr.latency_s, 3),
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
    # need to read state mid-decision. Other surfaces - chat handling,
    # combat-log review - need to query, decide, possibly act, possibly
    # query more. That's what this helper is for.
    #
    # Contract: pass a tools list (OpenAI shape) and a parallel handler
    # dict (name -> callable). Each iteration the LLM either emits tool
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
        model, _, _, _ = self._resolve_tier(tier)
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
            cr = self.tool_chat(
                tier, messages,
                tools=tools, tool_choice='auto', max_tokens=max_tokens,
            )
            latency_total += cr.latency_s
            in_total += cr.input_tokens
            out_total += cr.output_tokens

            messages.append(cr.raw_assistant_message)

            if not cr.tool_calls:
                final_text = cr.text
                terminated = True
                break

            last_iter_had_calls = True
            for call in cr.tool_calls:
                name = call.name
                args = call.arguments
                handler = tool_handlers.get(name)
                if handler is None:
                    result = {'error': f'unknown tool {name!r}'}
                else:
                    try:
                        result = handler(args)
                    except Exception as e:
                        result = {'error': f'{type(e).__name__}: {e}'}
                applied.append((name, args, result))
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
