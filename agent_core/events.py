"""Append-only cross-addon event stream.

Events are JSON objects, one per line, in `events.jsonl`. Both the
orchestrator and Lua addons write to this file (Lua appends through
io.open with 'a'); both also tail it for read-side consumption.

Each event MUST include:
    ts          float   unix epoch seconds
    character   str     namespace (matches Config.character for MVP)
    source      str     addon or module that emitted it (nav, combat, agent_core, ...)
    type        str     event class (level_up, item_received, dialog_open, ...)

Plus arbitrary type-specific fields. Readers should filter by character
and tolerate unknown types.

Phase 1 scope: the writer / iterator interfaces below. The actual event
producers (combat, interact, etc.) are wired up in later phases.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator


def append(path: Path, character: str, source: str, type_: str, **fields: Any) -> None:
    """Atomically append one event line. Creates the file if absent."""
    record = {
        'ts': time.time(),
        'character': character,
        'source': source,
        'type': type_,
        **fields,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(',', ':')) + '\n'
    # Single write() call into the OS — kernel-level atomic for short lines.
    # We don't bother with rename-tmp because partial reads aren't a concern
    # for line-oriented JSONL: a reader that sees a half-written line on a
    # crash just discards it.
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line)


def iter_since(path: Path, since_ts: float) -> Iterator[dict]:
    """Yield event records with ts >= since_ts. Skips malformed lines.

    Uses errors='replace' on the underlying read because FFXI chat text
    occasionally smuggles color/control bytes past the addon's cleaner;
    a single byte sequence shouldn't sink the whole iteration."""
    if not path.exists():
        return
    with open(path, encoding='utf-8', errors='replace') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get('ts', 0) >= since_ts:
                yield rec


def tail(path: Path, n: int = 100) -> list[dict]:
    """Return the last n events (best-effort; reads the whole file). For
    larger logs we'll add a seek-from-end implementation later."""
    if not path.exists():
        return []
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    out: list[dict] = []
    for raw in lines[-n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out
