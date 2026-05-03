"""Runtime configuration for agent_core.

Sources, in priority order:
    1. environment variables (`AGENT_CHARACTER`, `AGENT_LLM_API_KEY`,
       `AGENT_LLM_BASE_URL`, `AGENT_LLM_REACTIVE_MODEL`, ...)
    2. `agent_core/config.toml` (gitignored - write API keys here)
    3. compiled-in defaults below

The character name is the namespace key for every per-character state /
command / persistent file. MVP runs single-character - we read it from
`AGENT_CHARACTER` or the config file once at startup.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover - 3.10 fallback only
    import tomli as tomllib  # type: ignore[import-not-found]


REPO_ROOT = Path(__file__).resolve().parent.parent
# Shared IPC root. Was previously `config/addons/nav/` - the legacy
# layout had every addon's state living inside the nav addon's
# config directory, which made the nav addon a kitchen-sink for
# unrelated features. Renamed to `config/xillm/` 2026-04-30 so the
# IPC layer is no longer pinned to one addon's identity.
DEFAULT_ASHITA_BASE = Path(
    '/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/xillm'
)


@dataclass
class LLMConfig:
    # The orchestrator talks to any OpenAI-compatible API: Groq (default
    # for free-tier dev), OpenAI, Together, llama.cpp's own server, etc.
    # Anthropic also exposes an OpenAI-compat shim - point base_url at it
    # to use Claude. base_url is provider-specific; api_key is always
    # required.
    #
    # base_url / api_key / provider here are the DEFAULT used by any
    # tier that doesn't have its own override below. The per-tier
    # `<tier>_base_url` / `<tier>_api_key` / `<tier>_provider` fields
    # let you split tiers across providers - typical setup is local
    # Ollama for fast tiers + cloud (Groq) for the heavy deliberative
    # tier where the cloud's larger models clearly outclass anything
    # that fits on our consumer GPU.
    base_url: str = 'https://api.groq.com/openai/v1'
    api_key: str = ''  # set via env var or config.toml; never commit

    # Provider routing. 'openai' goes through the OpenAI SDK (works for
    # OpenAI proper, Groq, Anthropic's compat shim, Together, etc).
    # 'ollama' bypasses the SDK and posts to Ollama's native /api/chat -
    # required for models whose tool-call format isn't parsed by Ollama's
    # OpenAI-compat shim (Qwen 2.5, GLM-4, etc). Auto-detected from
    # base_url if left empty: ":11434" -> ollama, anything else -> openai.
    provider: str = ''

    # Per-tier model defaults. Tiers are described in
    # docs/agent-architecture.md ("LLM integration"). Groq's free tier
    # offers llama-3.1-8b-instant (fast) and llama-3.3-70b-versatile
    # (smarter); we default to those. Override via env or config.toml.
    reactive: str = 'llama-3.1-8b-instant'
    periodic: str = 'llama-3.3-70b-versatile'
    deliberative: str = 'llama-3.3-70b-versatile'

    # Per-tier provider overrides. Empty string = inherit from the
    # default base_url/api_key/provider above. Setting `_provider` to
    # 'ollama' or 'openai' explicitly bypasses the auto-detect.
    reactive_base_url: str = ''
    reactive_api_key: str = ''
    reactive_provider: str = ''
    periodic_base_url: str = ''
    periodic_api_key: str = ''
    periodic_provider: str = ''
    deliberative_base_url: str = ''
    deliberative_api_key: str = ''
    deliberative_provider: str = ''


@dataclass
class Paths:
    # Shared IPC base (Ashita config dir for the nav addon today; will move
    # under .../config/addons/agent/ once the IPC layout migration lands).
    ipc_base: Path = DEFAULT_ASHITA_BASE
    # Repo-side data (collision JSONs, dropoffs, transitions, obstacles).
    collision_dir: Path = REPO_ROOT / 'nav' / 'data' / 'collision'
    obstacle_dir: Path = REPO_ROOT / 'nav' / 'data' / 'obstacles'
    dropoff_dir: Path = REPO_ROOT / 'nav' / 'data' / 'dropoffs'
    transitions_file: Path = REPO_ROOT / 'nav' / 'data' / 'zone_transitions.json'

    def state_dir(self, character: str) -> Path:
        return self.ipc_base / 'state' / character

    def commands_dir(self, character: str) -> Path:
        return self.ipc_base / 'commands' / character

    def persistent_dir(self, character: str) -> Path:
        return self.ipc_base / 'persistent' / character

    def relationships_dir(self, character: str) -> Path:
        # Per-player JSON files (interaction history, favors, tone) so
        # we don't serialise the whole social graph on every update.
        return self.persistent_dir(character) / 'relationships'

    def events_file(self) -> Path:
        return self.ipc_base / 'events.jsonl'

    def user_goal_file(self) -> Path:
        # The user's free-text goal lives in the repo root so it's easy
        # to find and edit. Empty file = "stop everything"; any non-empty
        # text gets handed to the LLM planner.
        return REPO_ROOT / 'user_goal.txt'


@dataclass
class Config:
    character: str = 'default'
    llm: LLMConfig = field(default_factory=LLMConfig)
    paths: Paths = field(default_factory=Paths)


def load() -> Config:
    """Build a Config from defaults, optional config.toml, and env overrides."""
    cfg = Config()

    toml_path = Path(__file__).parent / 'config.toml'
    if toml_path.exists():
        with open(toml_path, 'rb') as f:
            data = tomllib.load(f)
        if 'character' in data:
            cfg.character = data['character']
        if 'ipc_base' in data:
            cfg.paths.ipc_base = Path(data['ipc_base'])
        if 'llm' in data:
            llm = data['llm']
            simple_keys = (
                'base_url', 'api_key', 'provider',
                'reactive', 'periodic', 'deliberative',
                'reactive_base_url', 'reactive_api_key', 'reactive_provider',
                'periodic_base_url', 'periodic_api_key', 'periodic_provider',
                'deliberative_base_url', 'deliberative_api_key', 'deliberative_provider',
            )
            for key in simple_keys:
                if key in llm:
                    setattr(cfg.llm, key, llm[key])

    cfg.character = os.environ.get('AGENT_CHARACTER', cfg.character)
    cfg.llm.api_key = os.environ.get('AGENT_LLM_API_KEY', cfg.llm.api_key)
    cfg.llm.base_url = os.environ.get('AGENT_LLM_BASE_URL', cfg.llm.base_url)
    cfg.llm.provider = os.environ.get('AGENT_LLM_PROVIDER', cfg.llm.provider)
    for tier in ('reactive', 'periodic', 'deliberative'):
        env_key = f'AGENT_LLM_{tier.upper()}_MODEL'
        if env_key in os.environ:
            setattr(cfg.llm, tier, os.environ[env_key])
        # Per-tier endpoint overrides. e.g. AGENT_LLM_DELIBERATIVE_BASE_URL.
        for suffix in ('BASE_URL', 'API_KEY', 'PROVIDER'):
            env_key = f'AGENT_LLM_{tier.upper()}_{suffix}'
            if env_key in os.environ:
                setattr(cfg.llm, f'{tier}_{suffix.lower()}', os.environ[env_key])
    if 'AGENT_IPC_BASE' in os.environ:
        cfg.paths.ipc_base = Path(os.environ['AGENT_IPC_BASE'])

    # Auto-detect provider from base_url if not explicitly set. Same
    # rule applied to the default and to any per-tier override that
    # specified a base_url but no provider.
    def _autodetect(url: str) -> str:
        return 'ollama' if ':11434' in url else 'openai'
    if not cfg.llm.provider:
        cfg.llm.provider = _autodetect(cfg.llm.base_url)
    for tier in ('reactive', 'periodic', 'deliberative'):
        bu = getattr(cfg.llm, f'{tier}_base_url', '')
        prov = getattr(cfg.llm, f'{tier}_provider', '')
        if bu and not prov:
            setattr(cfg.llm, f'{tier}_provider', _autodetect(bu))

    return cfg
