"""State aggregator: read per-channel state files into one world model.

Each Lua addon publishes its slice of state to
`<ipc_base>/state/<character>/<channel>.json` on every relevant change
(atomic write via temp + rename, so partial reads are impossible).
The orchestrator polls these files at ~5 Hz, builds a unified
`WorldState` snapshot, and diffs against the previous snapshot to fire
change events into the event bus.

Phase 1 scope: the load / aggregate interfaces. We DON'T yet wire it
into nav.lua's existing `nav_status.json` etc. — that migration is
Phase 1b.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Known channels we expect to see state files for. Unknown channels
# are silently ignored — it's fine for an addon to not publish yet.
KNOWN_CHANNELS: tuple[str, ...] = (
    'nav',         # zone, position, autofollow status
    'combat',      # target, hp/mp, status effects, casting state
    'inventory',   # bag contents, gear sets active
    'party',       # party members + their hp/mp/status
    'chat',        # rolling buffer of recent chat lines
    'menu',        # active NPC dialog or null
    'entities',    # known NPCs/mobs/objects in current zone
)


@dataclass
class WorldState:
    """Everything the orchestrator knows about the current frame.

    Each channel is the parsed contents of `state/<character>/<channel>.json`,
    or None if that channel hasn't published yet. Code that consumes
    WorldState should treat any field as possibly-None.
    """
    character: str
    channels: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    def get(self, channel: str) -> dict[str, Any] | None:
        return self.channels.get(channel)


def aggregate(state_dir: Path, character: str) -> WorldState:
    """Read every known channel for the given character. Missing or
    malformed files yield None for that channel."""
    ws = WorldState(character=character)
    for channel in KNOWN_CHANNELS:
        path = state_dir / f'{channel}.json'
        if not path.exists():
            ws.channels[channel] = None
            continue
        try:
            # FFXI chat occasionally smuggles non-UTF-8 control bytes
            # past chat.lua's cleaner; errors='replace' keeps the read
            # alive (a single bad line in a 200-line buffer shouldn't
            # blank the whole channel).
            with open(path, encoding='utf-8', errors='replace') as f:
                ws.channels[channel] = json.load(f)
        except (json.JSONDecodeError, OSError):
            ws.channels[channel] = None
    return ws
