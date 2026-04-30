"""In-game `/echo` helper - broadcasts a one-line summary to the
chat window so an observer can see what the agent is "thinking"
without tailing logs.

Every LLM-driven decision (planner outcome, engage/skip/rest verdicts,
research findings) should emit one short echo via `to_chat`. The
output goes through cmd_inbox.txt -> cmdrelay -> in-game chat. /echo is
local-only - never leaks to /say or /shout, never visible to other
players.

FFXI's chat line cap is around 150 chars (varies by font scaling); we
truncate aggressively at MAX_LEN to avoid mid-line cutoffs that look
worse than a clean ellipsis.
"""
from __future__ import annotations

from . import config as _config


# Per-line cap. FFXI's chat truncates around 150 chars including the
# `/echo [prefix] ` overhead, so the message body needs to land well
# under that. We chunk longer messages into multiple /echo lines on
# word boundaries instead of one truncated line.
MAX_BODY_LEN = 100
# Hard ceiling on lines per echo to avoid a runaway model spamming
# chat with a 30-line essay.
MAX_LINES = 3


def _wrap(text: str, width: int) -> list[str]:
    """Break `text` into chunks of at most `width` chars, splitting on
    spaces when possible. Long words are hard-cut. Returns a list of
    lines (no trailing whitespace)."""
    text = text.strip()
    if not text:
        return []
    words = text.split()
    lines: list[str] = []
    cur = ''
    for w in words:
        if len(w) > width:  # rare hard-wrap for huge tokens
            if cur:
                lines.append(cur)
                cur = ''
            for i in range(0, len(w), width):
                lines.append(w[i:i + width])
            continue
        candidate = (cur + ' ' + w).strip() if cur else w
        if len(candidate) <= width:
            cur = candidate
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def to_chat(cfg: _config.Config, prefix: str, message: str) -> None:
    """Append `/echo [prefix] message` to cmd_inbox.txt, splitting long
    messages into up to MAX_LINES wrapped lines. The first line shows
    `[prefix]`; continuation lines are indented to visually group them.
    Best-effort - a missing cmdrelay or a write failure is swallowed."""
    if not message:
        return
    text = str(message).strip().replace('\n', ' ').replace('\r', ' ')
    chunks = _wrap(text, MAX_BODY_LEN)
    if not chunks:
        return
    if len(chunks) > MAX_LINES:
        # Trim with an ellipsis on the last allowed line so it's clear
        # we cut off, rather than silently dropping content.
        chunks = chunks[:MAX_LINES]
        chunks[-1] = chunks[-1][: MAX_BODY_LEN - 1] + '...'
    inbox = cfg.paths.ipc_base / 'cmd_inbox.txt'
    try:
        with open(inbox, 'a', encoding='utf-8') as f:
            for i, chunk in enumerate(chunks):
                tag = f'[{prefix}] ' if i == 0 else '   '
                f.write(f'/echo {tag}{chunk}\n')
    except OSError:
        pass
