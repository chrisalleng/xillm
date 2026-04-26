"""Persistent state files for the agent (goals, gambits, knowledge).

Persistent files live under `<ipc_base>/persistent/<character>/` and
survive orchestrator restarts. Each file is the canonical source of
truth for its concern; in-memory caches are rebuilt from these on
startup.

Atomic writes (temp file + rename) so a crash mid-write doesn't leave
us with a truncated JSON file we can't load. We don't bother with file
locking — only the orchestrator writes, and it's single-process.

Phase 1 scope: load/save primitives + an empty `Goals` / `Gambits` /
`Knowledge` shape. Phase 2+ fills in the actual goal-tree and gambit-AST
schemas.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # tempfile in same dir guarantees os.replace is atomic on POSIX.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        # mkstemp creates 0600; bump to 0644 so the Lua addon (running
        # under Wine but as the same Linux user) can still read it.
        # In practice 0600 should also work — but we hit a case where
        # Wine's filesystem layer cared, so be permissive.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


@dataclass
class Goals:
    """Persistent goal tree. Phase 2 will fill in the schema."""
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> 'Goals':
        data = _read_json(path, {'nodes': {}, 'roots': []})
        return cls(nodes=data.get('nodes', {}), roots=data.get('roots', []))

    def save(self, path: Path) -> None:
        _atomic_write_json(path, {'nodes': self.nodes, 'roots': self.roots})


@dataclass
class Gambits:
    """Persistent gambit library. Phase 3 will fill in the AST schema."""
    gambits: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> 'Gambits':
        data = _read_json(path, {'gambits': []})
        return cls(gambits=data.get('gambits', []))

    def save(self, path: Path) -> None:
        _atomic_write_json(path, {'gambits': self.gambits})


@dataclass
class Knowledge:
    """Free-form learned facts. Schema is open by design — the LLM and
    orchestrator both write to it. We just wrap it for typed access."""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> 'Knowledge':
        return cls(data=_read_json(path, {}))

    def save(self, path: Path) -> None:
        _atomic_write_json(path, self.data)
