"""Runtime configuration for agent_core.

Sources, in priority order:
    1. environment variables (`AGENT_CHARACTER`, `AGENT_LLM_REACTIVE_MODEL`, ...)
    2. `agent_core/config.toml` (created on first run with defaults if absent)
    3. compiled-in defaults below

The character name is the namespace key for every per-character state /
command / persistent file. MVP runs single-character — we read it from
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
DEFAULT_ASHITA_BASE = Path(
    '/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav'
)


@dataclass
class LLMConfig:
    # Per-tier model defaults; tune via env vars or config.toml. The tiers
    # are described in docs/agent-architecture.md ("LLM integration").
    reactive: str = 'claude-haiku-4-5'
    periodic: str = 'claude-sonnet-4-6'
    deliberative: str = 'claude-opus-4-7'


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

    def events_file(self) -> Path:
        return self.ipc_base / 'events.jsonl'


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
        if 'llm' in data:
            for tier in ('reactive', 'periodic', 'deliberative'):
                if tier in data['llm']:
                    setattr(cfg.llm, tier, data['llm'][tier])
        if 'ipc_base' in data:
            cfg.paths.ipc_base = Path(data['ipc_base'])

    cfg.character = os.environ.get('AGENT_CHARACTER', cfg.character)
    for tier in ('reactive', 'periodic', 'deliberative'):
        env_key = f'AGENT_LLM_{tier.upper()}_MODEL'
        if env_key in os.environ:
            setattr(cfg.llm, tier, os.environ[env_key])
    if 'AGENT_IPC_BASE' in os.environ:
        cfg.paths.ipc_base = Path(os.environ['AGENT_IPC_BASE'])

    return cfg
