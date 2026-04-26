"""Per-player relationship records.

Storage: one JSON file per known player at
    persistent/<character>/relationships/<player_name>.json

Schema (all fields optional except `name`):

    {
      "name":       "Friend",
      "first_seen": <unix ts>,
      "last_seen":  <unix ts>,
      "tone_score": 0.0,                    # running heuristic, -1..+1
      "interactions": [                     # rolling history
        {"ts": ..., "channel": "tell", "direction": "in",
         "text": "...", "summary": "..."}
      ],
      "favors_owed_by_us": [                # we owe them
        {"ts": ..., "what": "..."}
      ],
      "favors_owed_to_us": [                # they owe us
        {"ts": ..., "what": "..."}
      ],
      "favors_completed": [                 # archive of resolved favors
        {"ts": ..., "side": "by_us" | "to_us", "what": "..."}
      ],
      "notes":      ""                      # free text, LLM-curated
    }

The store is read-mostly: chat-handling LLM calls accrete interactions
and tweak tone; the goal-planner edits favors/notes when the user
mentions them. Compact summaries (`summary_for_prompt`) are what gets
pasted into LLM context windows — the raw record can grow without
bound, but the prompt slice stays bounded.

Interaction list is kept rolling at MAX_INTERACTIONS to prevent any
one chatty player from blowing out the file.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import config as _config
from . import persistence as _persistence


# Cap how many raw interactions we keep per player. Older ones get
# summarised into `notes` (manual or LLM-curated) before being dropped.
MAX_INTERACTIONS = 200

# Tone score is clipped to [-1, +1]. Anything past saturation is just
# "very friendly" / "very hostile" — finer gradation is meaningless
# noise once you're past ±0.7.
TONE_MIN = -1.0
TONE_MAX = 1.0


# Reject anything that could escape the relationships dir. FFXI names
# are alphanumeric with the occasional apostrophe — tighten to that.
_VALID_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9'_-]{1,29}$")


class InvalidPlayerName(ValueError):
    """Raised when a player name fails the safety check. We keep the
    rule strict — anything that isn't an FFXI-style name is suspicious
    enough that we'd rather error than persist."""


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise InvalidPlayerName(f'name must be a string, got {type(name).__name__}')
    s = name.strip()
    if not _VALID_NAME.match(s):
        raise InvalidPlayerName(
            f'invalid player name {name!r}: must match {_VALID_NAME.pattern}'
        )
    return s


def record_path(cfg: _config.Config, name: str) -> Path:
    safe = _validate_name(name)
    return cfg.paths.relationships_dir(cfg.character) / f'{safe}.json'


def _empty(name: str) -> dict[str, Any]:
    now = time.time()
    return {
        'name':              name,
        'first_seen':        now,
        'last_seen':         now,
        'tone_score':        0.0,
        'interactions':      [],
        'favors_owed_by_us': [],
        'favors_owed_to_us': [],
        'favors_completed':  [],
        'notes':             '',
    }


def load(cfg: _config.Config, name: str) -> dict[str, Any]:
    """Load a player record. Missing files return a fresh empty record
    (not yet persisted) so callers can treat unknown players the same
    as known-but-quiet ones."""
    safe = _validate_name(name)
    path = record_path(cfg, safe)
    if not path.exists():
        return _empty(safe)
    data = _persistence._read_json(path, _empty(safe))
    if not isinstance(data, dict) or 'name' not in data:
        return _empty(safe)
    # Defensive backfill — older records may be missing newer fields.
    template = _empty(safe)
    for k, v in template.items():
        data.setdefault(k, v)
    return data


def save(cfg: _config.Config, record: dict[str, Any]) -> Path:
    name = _validate_name(record.get('name', ''))
    path = record_path(cfg, name)
    _persistence._atomic_write_json(path, record)
    return path


def list_known(cfg: _config.Config) -> list[str]:
    """All player names we have records for. Skips junk filenames."""
    base = cfg.paths.relationships_dir(cfg.character)
    if not base.exists():
        return []
    out: list[str] = []
    for p in base.iterdir():
        if not p.is_file() or p.suffix != '.json':
            continue
        stem = p.stem
        if _VALID_NAME.match(stem):
            out.append(stem)
    return sorted(out)


# -----------------------------------------------------------------------
# Mutators (single function with patch dict — gives the LLM one tool
# call instead of ten, and atomically applies a batch of changes per
# event)
# -----------------------------------------------------------------------

_PATCH_FIELDS = {
    'tone_delta',
    'append_interaction',
    'append_note',
    'add_favor_owed_by_us',
    'add_favor_owed_to_us',
    'mark_favor_done_by_us',
    'mark_favor_done_to_us',
    'set_notes',
}


class InvalidRelationshipPatch(ValueError):
    """Raised when an `update` patch dict is structurally wrong. Like
    GambitValidationError, we collect every issue and report once."""


def _clip_tone(v: float) -> float:
    return max(TONE_MIN, min(TONE_MAX, v))


def _move_completed(record: dict[str, Any], side: str, needle: str) -> bool:
    """Find the first open favor on `side` (by_us / to_us) whose `what`
    contains `needle` (case-insensitive substring match), move it to
    favors_completed. Returns True if anything moved."""
    src_key = 'favors_owed_by_us' if side == 'by_us' else 'favors_owed_to_us'
    src = record.get(src_key) or []
    needle_l = needle.strip().lower()
    if not needle_l:
        return False
    for i, fav in enumerate(src):
        what = (fav.get('what') or '').lower()
        if needle_l in what:
            popped = src.pop(i)
            record.setdefault('favors_completed', []).append({
                'ts':   popped.get('ts', time.time()),
                'side': side,
                'what': popped.get('what', ''),
                'completed_at': time.time(),
            })
            return True
    return False


def update(cfg: _config.Config, name: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a patch dict to a player's record. Loads the current
    record (creating an empty one if absent), applies every recognised
    field in order, persists, returns the updated record.

    Patch fields (all optional):

        tone_delta              float, added to tone_score then clipped
        append_interaction      {channel, direction, text, summary?}
        append_note             string appended to notes (newline-separated)
        set_notes               string replacing notes wholesale
        add_favor_owed_by_us    string ("we promised X") — new open favor
        add_favor_owed_to_us    string ("they did X for us")
        mark_favor_done_by_us   string substring matching an open favor
                                we owed; moves it to favors_completed
        mark_favor_done_to_us   same but for favors they owed us

    Unknown fields raise — we'd rather fail than silently swallow LLM
    typos that would make a "tone update" no-op."""
    if not isinstance(patch, dict):
        raise InvalidRelationshipPatch('patch must be an object')
    bad = set(patch) - _PATCH_FIELDS
    if bad:
        raise InvalidRelationshipPatch(
            f'unknown patch fields: {sorted(bad)}; '
            f'allowed: {sorted(_PATCH_FIELDS)}'
        )

    record = load(cfg, name)
    record['last_seen'] = time.time()

    if 'tone_delta' in patch:
        delta = patch['tone_delta']
        if not isinstance(delta, (int, float)):
            raise InvalidRelationshipPatch('tone_delta must be a number')
        record['tone_score'] = _clip_tone(float(record.get('tone_score', 0.0)) + float(delta))

    if 'append_interaction' in patch:
        ev = patch['append_interaction']
        if not isinstance(ev, dict):
            raise InvalidRelationshipPatch('append_interaction must be an object')
        entry = {
            'ts':        ev.get('ts', time.time()),
            'channel':   ev.get('channel', '?'),
            'direction': ev.get('direction', '?'),
            'text':      ev.get('text', ''),
        }
        if 'summary' in ev:
            entry['summary'] = ev['summary']
        record.setdefault('interactions', []).append(entry)
        # Keep the list bounded — older entries get shed; the LLM is
        # responsible for distilling anything load-bearing into `notes`.
        if len(record['interactions']) > MAX_INTERACTIONS:
            record['interactions'] = record['interactions'][-MAX_INTERACTIONS:]

    if 'append_note' in patch:
        text = patch['append_note']
        if not isinstance(text, str):
            raise InvalidRelationshipPatch('append_note must be a string')
        cur = record.get('notes', '')
        record['notes'] = (cur + ('\n' if cur else '') + text).strip()

    if 'set_notes' in patch:
        text = patch['set_notes']
        if not isinstance(text, str):
            raise InvalidRelationshipPatch('set_notes must be a string')
        record['notes'] = text

    if 'add_favor_owed_by_us' in patch:
        what = patch['add_favor_owed_by_us']
        if not isinstance(what, str) or not what.strip():
            raise InvalidRelationshipPatch('add_favor_owed_by_us must be a non-empty string')
        record.setdefault('favors_owed_by_us', []).append({
            'ts': time.time(), 'what': what.strip(),
        })

    if 'add_favor_owed_to_us' in patch:
        what = patch['add_favor_owed_to_us']
        if not isinstance(what, str) or not what.strip():
            raise InvalidRelationshipPatch('add_favor_owed_to_us must be a non-empty string')
        record.setdefault('favors_owed_to_us', []).append({
            'ts': time.time(), 'what': what.strip(),
        })

    if 'mark_favor_done_by_us' in patch:
        needle = patch['mark_favor_done_by_us']
        if not isinstance(needle, str):
            raise InvalidRelationshipPatch('mark_favor_done_by_us must be a string')
        if not _move_completed(record, 'by_us', needle):
            raise InvalidRelationshipPatch(
                f'no open favor owed BY us matching {needle!r}'
            )

    if 'mark_favor_done_to_us' in patch:
        needle = patch['mark_favor_done_to_us']
        if not isinstance(needle, str):
            raise InvalidRelationshipPatch('mark_favor_done_to_us must be a string')
        if not _move_completed(record, 'to_us', needle):
            raise InvalidRelationshipPatch(
                f'no open favor owed TO us matching {needle!r}'
            )

    save(cfg, record)
    return record


def record_interaction(cfg: _config.Config, name: str, *,
                       channel: str, direction: str,
                       text: str, summary: str | None = None,
                       tone_delta: float = 0.0) -> dict[str, Any]:
    """Convenience wrapper for the chat-side recorder — accretes a
    single interaction with optional tone update. Future chat addon
    hooks call this on every inbound/outbound line involving a named
    player; the LLM uses `update` directly for richer changes."""
    patch: dict[str, Any] = {
        'append_interaction': {
            'channel':   channel,
            'direction': direction,
            'text':      text,
            **({'summary': summary} if summary else {}),
        },
    }
    if tone_delta:
        patch['tone_delta'] = tone_delta
    return update(cfg, name, patch)


# -----------------------------------------------------------------------
# Prompt-time summaries
# -----------------------------------------------------------------------

def summary_for_prompt(record: dict[str, Any], *,
                       recent_interactions: int = 5) -> str:
    """Compact one-paragraph summary suitable for an LLM prompt. Keeps
    counts + last few interactions + open favors + notes — drops the
    long interaction history."""
    name = record.get('name', '?')
    tone = record.get('tone_score', 0.0)
    inters = record.get('interactions') or []
    by_us = record.get('favors_owed_by_us') or []
    to_us = record.get('favors_owed_to_us') or []
    notes = (record.get('notes') or '').strip()
    # Tone bucket: render qualitatively, not as a raw number, so the
    # LLM doesn't try to thread the needle on 0.13 vs 0.17 etc.
    bucket = (
        'hostile' if tone <= -0.4 else
        'cool'    if tone <= -0.1 else
        'neutral' if tone <   0.1 else
        'warm'    if tone <   0.4 else
        'close'
    )
    lines = [
        f'{name}: {bucket} (tone {tone:+.2f}; {len(inters)} interactions)',
    ]
    if by_us:
        lines.append('  We owe: ' + '; '.join(f.get('what', '?') for f in by_us))
    if to_us:
        lines.append('  Owe us: ' + '; '.join(f.get('what', '?') for f in to_us))
    recent = inters[-recent_interactions:]
    if recent:
        lines.append('  Recent:')
        for ev in recent:
            ch = ev.get('channel', '?')
            dr = ev.get('direction', '?')
            txt = ev.get('summary') or ev.get('text') or ''
            if len(txt) > 80:
                txt = txt[:77] + '…'
            lines.append(f'    [{ch}/{dr}] {txt}')
    if notes:
        lines.append(f'  Notes: {notes}')
    return '\n'.join(lines)
