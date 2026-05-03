"""LSB script resolver - fetch the Lua source that governs an NPC's
dialog so the menu_judge has ground truth for option semantics.

When the static menu catalog (lsb_extract.py) doesn't cover the
current menu (i.e. it's a quest/mission NPC, not a vendor / home point
/ outpost overseer), the menu_judge falls back to LLM reasoning. This
module gives that LLM call the relevant Lua code as context:

  - The NPC's own script: scripts/zones/<zone>/npcs/<npc>.lua
  - Any mission/quest scripts that reference this NPC by name

The LLM reads `if option == 0 then mission:begin(player)` etc.
directly from the source - no parsing required on our side.

Cache: file contents are cached by absolute path with mtime check.
The LSB tree is large (thousands of files); searching for NPC name
across it is cheap (single pass) and cached after first hit.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from threading import Lock
from typing import Iterable


# Default LSB checkout location. Overridable per-call. Mirrors the
# ad-hoc path conventions used elsewhere in agent_core (config.toml
# could expose this later if needed).
DEFAULT_LSB_ROOT = Path('/home/chris/workspace/server')

# Cap on how much script text we attach to the LLM prompt. Mission /
# quest scripts can be 500+ lines; we want enough context to answer
# "what does option N do" without blowing the token budget. The judge
# prompt is on the reactive tier (qwen3.5:9b) which has plenty of
# context, but keeping it tight keeps latency low.
MAX_SCRIPT_CHARS = 8000

# Subtrees we search when looking for NPC references. We skip
# scripts/globals/ here because globals are too generic (every conquest
# overseer references xi.conquest.*); the NPC's own script tells the
# judge that already.
_SEARCH_SUBTREES = (
    'scripts/missions',
    'scripts/quests',
)


_lock = Lock()
_npc_index: dict[Path, dict[str, list[Path]]] = {}
_index_built_at: dict[Path, float] = {}
# Re-scan no more than this often. The tree changes when LSB is
# updated which is rare during a session.
_INDEX_TTL_S = 600.0


def _scripts_zones_dir(lsb_root: Path) -> Path:
    return lsb_root / 'scripts' / 'zones'


def _norm_zone_dirname(zone_name: str) -> str:
    """LSB zone directories use underscores in names (e.g.
    'Bastok_Markets'). Map our enum-style 'BASTOK_MARKETS' to the
    title-case dirname used on disk."""
    parts = zone_name.split('_')
    return '_'.join(p.capitalize() for p in parts)


def _read_capped(path: Path, cap: int) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    if len(text) <= cap:
        return text
    return text[:cap] + f'\n-- [truncated at {cap} chars - file is {len(text)} chars]'


def npc_script(zone_name: str, npc_name: str,
               lsb_root: Path = DEFAULT_LSB_ROOT) -> str | None:
    """Read the NPC's own script. zone_name in upper-snake-case
    (e.g. 'BASTOK_MARKETS'); npc_name as it appears on disk."""
    zone_dir = _scripts_zones_dir(lsb_root) / _norm_zone_dirname(zone_name)
    npcs_dir = zone_dir / 'npcs'
    if not npcs_dir.is_dir():
        return None
    # NPC filenames may differ from in-game names ('Rabid Wolf, I.M.'
    # -> 'Rabid_Wolf_IM.lua'). Try a few normalizations.
    candidates = _npc_filename_candidates(npc_name)
    for stem in candidates:
        path = npcs_dir / f'{stem}.lua'
        if path.exists():
            text = _read_capped(path, MAX_SCRIPT_CHARS)
            return text or None
    return None


def _npc_filename_candidates(npc_name: str) -> list[str]:
    """Reduce an in-game NPC name to LSB filename candidates. The
    LSB convention strips punctuation, replaces spaces with
    underscores, and titlecases. We also try the raw name so unusual
    casings like 'Rabid_Wolf_IM' (uppercase abbrev) work."""
    # Strip apostrophes and commas, collapse periods.
    raw = (npc_name or '').strip()
    if not raw:
        return []
    cleaned = (raw
               .replace("'", '')
               .replace(',', '')
               .replace('.', ''))
    # Standard form: title-case words joined with _.
    words = [w for w in re.split(r'\s+', cleaned) if w]
    titled = '_'.join(w.capitalize() for w in words)
    raw_under = '_'.join(words)
    # Some NPCs have ALL-CAPS abbreviations (IM, NPC ranks like
    # I.M. -> IM). Preserve casing from the original.
    keep_case = '_'.join(words)
    out = []
    for c in (titled, raw_under, keep_case):
        if c and c not in out:
            out.append(c)
    return out


def _build_npc_index(lsb_root: Path) -> dict[str, list[Path]]:
    """Scan missions/ and quests/ trees once, building NPC-name ->
    [script paths] index. The scan is fast (tens of MB total) and
    cached for INDEX_TTL_S so multiple judge calls in the same
    session share one pass."""
    index: dict[str, list[Path]] = {}
    for sub in _SEARCH_SUBTREES:
        root = lsb_root / sub
        if not root.is_dir():
            continue
        for path in root.rglob('*.lua'):
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            # NPC references in mission/quest scripts look like:
            #   ['Cleades'] = mission:progressEvent(1000),
            #   ['Rashid'] = ...
            # Capture the bracketed string-key entries.
            for m in re.finditer(r"\[\s*['\"]([A-Za-z][A-Za-z0-9 _.,'-]*)['\"]\s*\]", text):
                npc = m.group(1).strip()
                if not npc:
                    continue
                bucket = index.setdefault(npc, [])
                if path not in bucket:
                    bucket.append(path)
    return index


def _ensure_index(lsb_root: Path) -> dict[str, list[Path]]:
    """Get or build the NPC index, with TTL-based refresh."""
    with _lock:
        now = time.time()
        built = _index_built_at.get(lsb_root, 0.0)
        if lsb_root in _npc_index and (now - built) < _INDEX_TTL_S:
            return _npc_index[lsb_root]
        idx = _build_npc_index(lsb_root)
        _npc_index[lsb_root] = idx
        _index_built_at[lsb_root] = now
        return idx


def quest_or_mission_scripts(npc_name: str,
                             lsb_root: Path = DEFAULT_LSB_ROOT,
                             max_results: int = 3) -> list[tuple[str, str]]:
    """Find mission/quest scripts that mention this NPC. Returns up
    to max_results entries of (relpath, source_text). The judge gets
    these to read 'how does each option advance the quest.'"""
    if not npc_name:
        return []
    idx = _ensure_index(lsb_root)
    # Try exact match first; fall back to fuzzier substring match if
    # the in-game name uses commas/periods that the index dropped.
    paths: list[Path] = []
    if npc_name in idx:
        paths.extend(idx[npc_name])
    else:
        # Strip punctuation from the search target to match the
        # cleaned form we indexed.
        cleaned = (npc_name.replace("'", '')
                            .replace(',', '')
                            .replace('.', '')
                            .strip())
        if cleaned and cleaned in idx:
            paths.extend(idx[cleaned])
        else:
            # Fuzzy: any indexed name whose cleaned form starts the
            # same as our cleaned target. Avoids missing 'Rabid Wolf'
            # vs 'Rabid Wolf, I.M.' style mismatches.
            target = cleaned.lower()
            for k, v in idx.items():
                if k.lower().startswith(target) or target.startswith(k.lower()):
                    paths.extend(v)
    # Dedup + cap.
    seen: set[Path] = set()
    out: list[tuple[str, str]] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        out.append((str(p.relative_to(lsb_root)), _read_capped(p, MAX_SCRIPT_CHARS)))
        if len(out) >= max_results:
            break
    return out


def resolve(zone_name: str | None, npc_name: str,
            lsb_root: Path = DEFAULT_LSB_ROOT) -> dict[str, str]:
    """One-call resolver: returns whatever Lua sources we can find
    for this NPC, keyed by a short label. Empty dict if nothing.

    Output keys:
      'npc_script'     - the NPC's own behavior file
      'mission_<n>'    - mission/quest scripts that reference the NPC

    The menu_judge concatenates these into one block in the user
    message. The LLM reads them as code and reasons about which
    option to pick."""
    out: dict[str, str] = {}
    if zone_name and npc_name:
        src = npc_script(zone_name, npc_name, lsb_root=lsb_root)
        if src:
            out['npc_script'] = src
    if npc_name:
        for i, (relpath, src) in enumerate(
            quest_or_mission_scripts(npc_name, lsb_root=lsb_root)
        ):
            out[f'mission_or_quest_{i}::{relpath}'] = src
    return out
