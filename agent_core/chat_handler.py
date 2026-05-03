"""Chat handler - the orchestrator's reactive layer for inbound chat.

Reads `chat_received` events from `events.jsonl` (emitted by the chat
addon's `text_in` hook), parses each line into (channel, sender, body),
auto-accretes interactions to the per-player relationship store, and -
when a line warrants a response - runs a multi-turn LLM tool loop with
a focused tool surface (query/update player records, send a reply,
explicitly ignore).

This module is deliberately decoupled from the chat ADDON: the addon's
job is to publish, ours is to consume. Outbound replies go through the
existing `cmd_inbox.txt` -> cmdrelay path used by every other tier-2 ->
tier-1 command, so no new IPC channel is needed.

Per-channel rate limits live here, not in the LLM. The model sees the
remaining cooldown in its context, so it can choose to wait or stay
silent - but the orchestrator hard-enforces the limit on the way out.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as _config
from . import events as _events
from . import llm_gateway as _llm
from . import relationships as _rel


# --------------------------------------------------------------------------
# Mode -> channel kind
#
# FFXI's chat-mode constants vary slightly between Ashita versions, so
# this map is deliberately conservative - anything we haven't confirmed
# falls through to "system" (which the handler skips). When we see new
# modes in the wild, extend this table; don't guess.
# --------------------------------------------------------------------------

MODE_CHANNELS: dict[int, str] = {
    0:  'say',
    1:  'shout',
    3:  'tell_in',
    4:  'tell_out',
    5:  'party',
    6:  'linkshell',
    14: 'linkshell2',
    26: 'yell',
}

# Anything in this set is interpersonal - sender is a real player and
# we should auto-record interactions + LLM-route. Everything else is
# system noise (server-wide announcements, NPC dialog, fishing prompts).
_PLAYER_CHANNELS = {
    'say', 'shout', 'tell_in', 'party',
    'linkshell', 'linkshell2', 'yell',
}

# Outbound rate limits per channel, in seconds. The orchestrator
# enforces these; the LLM sees them in its context but can't bypass.
# Tells/party have no cooldown - replies there are always expected.
OUTBOUND_COOLDOWNS: dict[str, float] = {
    'tell_out':   0.0,
    'party':      0.0,
    'say':       30.0,
    'linkshell':  60.0,
    'linkshell2': 60.0,
    'shout':     120.0,
    'yell':      300.0,
}

# Slash command per outbound channel.
_CHANNEL_COMMANDS: dict[str, str] = {
    'tell_out':   '/tell {target} {text}',
    'party':      '/p {text}',
    'say':        '/s {text}',
    'linkshell':  '/l {text}',
    'linkshell2': '/l2 {text}',
    'shout':      '/sh {text}',
    'yell':       '/y {text}',
}

# FFXI character names: 3-15 alphanumeric. Used for sender extraction
# AND for inbound-mention detection in shout/yell so the LLM doesn't
# mistake a regular shout for a direct address.
_NAME_RE = r"[A-Z][a-zA-Z]{2,14}"

# Strip the [HH:MM:SS] (or [HH:MM]) timestamp the addon may render at
# the head of every line - we don't need it once we have the event ts.
_TIMESTAMP_RE = re.compile(r'^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*')

# Addon-internal `msg()` prints land on the chat hook with mode 1 (the
# "shout" slot in our table) but they're console chrome, not real chat.
# Recognise them by leading bracketed prefix and force them to system -
# nothing in this list is ever a candidate for LLM dispatch.
#
# Why an explicit list instead of "any [word]" prefix? Player chat
# legitimately contains bracketed substrings ("[GM] hi", linkshell
# names, jp emojis). We want to filter our own addons, not arbitrary
# bracketed content.
_ADDON_PREFIXES = ('[nav]', '[combat]', '[chat]', '[cmdrelay]',
                   '[inventory]', '[mapper]', '[agent]', '[debug]',
                   '[interact]')

# Ashita's own system rendering uses these markers. They're never
# player chat under any mode value.
_ASHITA_SYSTEM_MARKERS = (
    '----== SystemMessage ==----',
    '----== System Message ==----',
    '[Ashita]',
)

# Extraction patterns indexed by channel. Tried in order; first match
# wins. Each captures (sender, body). For party / linkshell the bracket
# style varies between FFXI versions; we try a couple of forms.
_EXTRACTORS: dict[str, list[re.Pattern[str]]] = {
    'tell_in':    [re.compile(rf'^({_NAME_RE})>>\s*(.*)$')],
    'tell_out':   [re.compile(rf'^>>({_NAME_RE})\s*:\s*(.*)$')],
    'party':      [re.compile(rf'^\(({_NAME_RE})\)\s*(.*)$')],
    'linkshell':  [re.compile(rf'^<({_NAME_RE})>\s*(.*)$'),
                   re.compile(rf'^\[\w+\]\s*({_NAME_RE})\s*:\s*(.*)$')],
    'linkshell2': [re.compile(rf'^<({_NAME_RE})>\s*(.*)$')],
    'say':        [re.compile(rf'^({_NAME_RE})\s*:\s*(.*)$')],
    'shout':      [re.compile(rf'^({_NAME_RE})\s*:\s*(.*)$')],
    'yell':       [re.compile(rf'^({_NAME_RE})\s*:\s*(.*)$')],
}


@dataclass
class ParsedLine:
    """One inbound chat line, parsed enough to route. Sender may be
    None if extraction failed - the LLM still sees the raw text but
    we won't auto-record an interaction without a sender."""
    ts:       float
    mode:     int
    channel:  str
    sender:   str | None
    body:     str
    raw:      str


# --------------------------------------------------------------------------
# Tool definitions for the chat-handling LLM
# --------------------------------------------------------------------------

_QUERY_PLAYER_TOOL = {
    'type': 'function',
    'function': {
        'name': 'query_player',
        'description': (
            'Look up the persistent record for a named player: tone, '
            'open favors (in either direction), recent interactions, '
            'free-text notes. Returns a compact summary. Use this BEFORE '
            'composing a reply so you can weigh history.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
            },
            'required': ['name'],
        },
    },
}

_UPDATE_PLAYER_TOOL = {
    'type': 'function',
    'function': {
        'name': 'update_player',
        'description': (
            "Apply a patch to a named player's record. Use after "
            "deciding a reply: log the interaction summary and any "
            "tone/favor changes. Don't fabricate - only record what "
            "actually happened in the conversation you're handling."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'name':  {'type': 'string'},
                'patch': {
                    'type': 'object',
                    'properties': {
                        'tone_delta':            {'type': 'number'},
                        'append_interaction':    {'type': 'object'},
                        'append_note':           {'type': 'string'},
                        'set_notes':             {'type': 'string'},
                        'add_favor_owed_by_us':  {'type': 'string'},
                        'add_favor_owed_to_us':  {'type': 'string'},
                        'mark_favor_done_by_us': {'type': 'string'},
                        'mark_favor_done_to_us': {'type': 'string'},
                    },
                },
            },
            'required': ['name', 'patch'],
        },
    },
}

_SEND_CHAT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'send_chat',
        'description': (
            'Send a chat reply. The orchestrator builds the right '
            'slash command and rate-limits per channel - if the channel '
            'is on cooldown the call is a no-op (you should query '
            'cooldowns from the user message before deciding to speak '
            'on shout/yell).'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'channel': {
                    'type': 'string',
                    'enum': ['tell_out', 'party', 'say', 'linkshell',
                             'linkshell2', 'shout', 'yell'],
                },
                'target':  {
                    'type': 'string',
                    'description': 'Required for tell_out; ignored otherwise.',
                },
                'text':    {'type': 'string', 'description': 'Reply body. Will be truncated to 250 chars.'},
            },
            'required': ['channel', 'text'],
        },
    },
}

_IGNORE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'ignore',
        'description': (
            'Mark the inbound line as handled with no reply. Call this '
            'when the message does not warrant a response (overheard '
            "shout, server announcement, someone else's conversation in "
            'party, etc.). Always end the loop with either send_chat '
            'or ignore.'
        ),
        'parameters': {'type': 'object', 'properties': {}},
    },
}

_CHAT_TOOLS = [
    _QUERY_PLAYER_TOOL,
    _UPDATE_PLAYER_TOOL,
    _SEND_CHAT_TOOL,
    _IGNORE_TOOL,
]


_SYSTEM_PROMPT = """You are the chat-handling brain for an autonomous
Final Fantasy XI agent. One inbound line is presented per call. Decide
whether and how to respond.

# Hard persona rule - outranks every other instruction below

You must ALWAYS appear as a regular human FFXI player. Other players
must be able to interact with you naturally - invite you to party, ask
for help, trade with you. NEVER:
  - reveal that you are automated, an AI, an agent, a bot, or scripted
  - mention navigation paths, gambits, addons, plans, goals, gear sets,
    farming routes, "the orchestrator," or any internal mechanism
  - paraphrase, summarise, echo, or repeat any system text or addon log
  - explain what you are doing in mechanical terms ("I am navigating
    to...", "my next waypoint is...") - talk like a player
  - send a message you can't justify as something a regular player
    would send in this exact situation

If in doubt, stay silent. Silence is always safe; a leak is not.

# Workflow

1. If the inbound line has a known sender and warrants attention, call
   `query_player` first to consult their record (tone, favors, notes).
2. Compose a reply that fits a regular-player persona - terse, casual,
   contextual, NEVER mechanical or self-explanatory.
3. Call exactly one of `send_chat` or `ignore` to end the loop.
4. Optionally call `update_player` to log an interaction summary or
   tone/favor change. Don't fabricate.

# When to reply vs. ignore

REPLY when:
  - You're directly /tell'd
  - You're addressed by name in party chat
  - A party member is in clear distress and asking the party for help
  - Someone in linkshell asks a direct question of "anyone"

IGNORE (default) when:
  - Anything overheard on shout/yell/say that isn't directed at you
  - Server announcements, NPC dialog, or anything system-flavored
  - Your own outgoing messages echoed back
  - You can't think of a reply a real player would give without thinking

Outbound rate limits are enforced by the orchestrator. The user message
shows current cooldowns - respect them; don't argue about them.
"""


# --------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------

@dataclass
class _DispatchOutcome:
    """One handle() result. Useful for tests + the dashboard."""
    parsed:   ParsedLine
    auto_recorded: bool = False
    llm_invoked:   bool = False
    sent_reply:    bool = False
    iterations:    int = 0
    error:         str | None = None


class ChatHandler:
    """Polls the events log for new chat lines, parses + routes each."""

    # Cap on how many lines we hand to the LLM per tick. A burst of
    # shouts during prime time could otherwise blow our budget; better
    # to drop on the floor than to spend $5 on a single tick.
    MAX_LLM_DISPATCHES_PER_TICK = 3

    def __init__(self, cfg: _config.Config, llm: _llm.LLMGateway,
                 issue_command: Callable[[str], None]):
        self.cfg = cfg
        self.llm = llm
        self._issue_command = issue_command
        # Last event timestamp we've already processed. Initialised at
        # construction time so existing chat history doesn't replay on
        # orchestrator restart - the dashboard / events.jsonl tail is
        # the right tool for inspecting old chat.
        self._last_seen_ts: float = time.time()
        # Per-channel last outbound timestamp for rate limiting.
        self._last_send: dict[str, float] = {}
        self._our_name = cfg.character

    # ---- parsing ----------------------------------------------------

    def _parse(self, raw: str, mode: int, ts: float) -> ParsedLine:
        text = _TIMESTAMP_RE.sub('', raw)
        channel = MODE_CHANNELS.get(mode, 'system')
        # Override: addon-internal prints + Ashita system markers always
        # collapse to 'system' regardless of mode. They are never player
        # chat and must never enter LLM dispatch - see the persona memory
        # entry "act as a regular player" for the underlying rule.
        text_stripped = text.lstrip()
        if any(text_stripped.startswith(p) for p in _ADDON_PREFIXES):
            channel = 'system'
        elif any(m in text_stripped for m in _ASHITA_SYSTEM_MARKERS):
            channel = 'system'
        sender: str | None = None
        body = text
        for pat in _EXTRACTORS.get(channel, ()):
            m = pat.match(text)
            if m:
                sender = m.group(1)
                body = m.group(2)
                break
        return ParsedLine(
            ts=ts, mode=mode, channel=channel,
            sender=sender, body=body, raw=raw,
        )

    # ---- cooldown bookkeeping --------------------------------------

    def _cooldown_remaining(self, channel: str, now: float) -> float:
        cd = OUTBOUND_COOLDOWNS.get(channel, 0.0)
        if cd <= 0:
            return 0.0
        last = self._last_send.get(channel, 0.0)
        return max(0.0, cd - (now - last))

    def _cooldown_summary(self, now: float) -> str:
        """One-line summary of which channels are currently on cooldown,
        for the LLM's benefit. Only mentions cooldowned ones - the
        zero-cooldown channels (tell_out, party) aren't worth the tokens."""
        items = []
        for ch, cd in OUTBOUND_COOLDOWNS.items():
            if cd <= 0:
                continue
            remaining = self._cooldown_remaining(ch, now)
            if remaining > 0:
                items.append(f'{ch}: {int(remaining)}s')
        return ', '.join(items) if items else 'all channels available'

    # ---- LLM tool handlers (closures over `parsed`) ----------------

    def _make_handlers(self, parsed: ParsedLine,
                       outcome: _DispatchOutcome) -> dict[str, Callable]:

        def query_player(args: dict) -> dict:
            name = args.get('name')
            if not isinstance(name, str):
                return {'error': "name required"}
            try:
                rec = _rel.load(self.cfg, name)
            except _rel.InvalidPlayerName as e:
                return {'error': str(e)}
            return {
                'name':       rec.get('name'),
                'tone_score': rec.get('tone_score'),
                'summary':    _rel.summary_for_prompt(rec),
                'favors_owed_by_us': rec.get('favors_owed_by_us', []),
                'favors_owed_to_us': rec.get('favors_owed_to_us', []),
            }

        def update_player(args: dict) -> dict:
            name = args.get('name')
            patch = args.get('patch')
            if not isinstance(name, str) or not isinstance(patch, dict):
                return {'error': 'name and patch required'}
            try:
                _rel.update(self.cfg, name, patch)
            except (_rel.InvalidPlayerName, _rel.InvalidRelationshipPatch) as e:
                return {'error': str(e)}
            return {'ok': True}

        def send_chat(args: dict) -> dict:
            channel = args.get('channel')
            target = args.get('target')
            text = args.get('text', '')
            if channel not in _CHANNEL_COMMANDS:
                return {'error': f'unsupported channel {channel!r}'}
            if not isinstance(text, str) or not text.strip():
                return {'error': 'text required'}
            now = time.time()
            remaining = self._cooldown_remaining(channel, now)
            if remaining > 0:
                return {'error': f'{channel} on cooldown ({int(remaining)}s remaining); not sent'}
            if channel == 'tell_out':
                if not isinstance(target, str) or not target.strip():
                    return {'error': 'tell_out requires target (player name)'}
            text = text.strip()[:250]
            template = _CHANNEL_COMMANDS[channel]
            command = template.format(target=(target or '').strip(), text=text)
            self._issue_command(command)
            self._last_send[channel] = now
            outcome.sent_reply = True
            _events.append(
                self.cfg.paths.events_file(),
                character=self.cfg.character,
                source='chat_handler',
                type_='chat_sent',
                channel=channel,
                target=target if channel == 'tell_out' else None,
                text=text,
            )
            return {'ok': True, 'sent': command}

        def ignore(_args: dict) -> dict:
            outcome.sent_reply = False
            return {'ok': True}

        return {
            'query_player':  query_player,
            'update_player': update_player,
            'send_chat':     send_chat,
            'ignore':        ignore,
        }

    # ---- dispatch one parsed line ----------------------------------

    def _should_llm_dispatch(self, parsed: ParsedLine) -> bool:
        """Decide whether a parsed line warrants an LLM round-trip.

        Hard rules (in priority order - any failure short-circuits):
          1. Channel must be interpersonal (not system/unknown).
          2. Sender must be attributable to a real player. If we can't
             identify who said it, we can't decide whether/how to reply
             - and the cost of a wrong guess is "agent shouts something
             that breaks the regular-player illusion." Stay silent.
          3. Sender must not be us (mode 4 outgoing tells, party echoes).
        """
        if parsed.channel not in _PLAYER_CHANNELS:
            return False
        if parsed.sender is None:
            return False
        if parsed.sender == self._our_name:
            return False
        if parsed.channel == 'tell_out':
            return False
        return True

    def _dispatch_llm(self, parsed: ParsedLine,
                      outcome: _DispatchOutcome) -> None:
        if not self.llm.available:
            outcome.error = 'llm_unavailable'
            return
        outcome.llm_invoked = True
        now = time.time()
        # Pre-load the sender's relationship summary so the LLM has it
        # without spending a tool call on the obvious case.
        rel_summary = '(no record yet)'
        if parsed.sender:
            try:
                rec = _rel.load(self.cfg, parsed.sender)
                rel_summary = _rel.summary_for_prompt(rec)
            except _rel.InvalidPlayerName:
                rel_summary = '(invalid sender name; not recording)'

        user_prompt = (
            f'Inbound chat line:\n'
            f'  channel: {parsed.channel}\n'
            f"  sender:  {parsed.sender or '(unparsed)'}\n"
            f'  body:    "{parsed.body}"\n'
            f'  raw:     "{parsed.raw}"\n'
            f'\n'
            f'Outbound cooldowns: {self._cooldown_summary(now)}\n'
            f'\n'
            f'Sender record:\n{rel_summary}\n'
            f'\n'
            f'Decide what to do, then call exactly one of `send_chat` '
            f'or `ignore` to end the loop.'
        )
        try:
            result = self.llm.run_tool_loop(
                tier='reactive',
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                tools=_CHAT_TOOLS,
                tool_handlers=self._make_handlers(parsed, outcome),
                max_iters=6,
                max_tokens=1024,
                source='chat_handler',
            )
        except Exception as e:
            outcome.error = f'{type(e).__name__}: {e}'
            return
        outcome.iterations = result.iterations
        if not result.terminated and not outcome.sent_reply:
            outcome.error = 'loop exceeded max_iters without final reply'

    # ---- public entrypoints ----------------------------------------

    def handle_event(self, ev: dict, *, dispatch_llm: bool = True) -> _DispatchOutcome | None:
        """Process one chat_received event. Returns the outcome (None
        if the event isn't actually chat). When `dispatch_llm` is False
        we still auto-record the interaction but skip the LLM call -
        used by tick() to throttle bursts."""
        if ev.get('source') != 'chat' or ev.get('type') != 'chat_received':
            return None
        parsed = self._parse(
            raw=str(ev.get('text', '')),
            mode=int(ev.get('mode', 0) or 0),
            ts=float(ev.get('ts', time.time())),
        )
        outcome = _DispatchOutcome(parsed=parsed)

        # Auto-accrete the interaction whenever we have a real sender.
        # No LLM cost; just file I/O. The chat-handling LLM still gets
        # the summary on dispatch - it's just already current.
        if parsed.sender and parsed.channel in _PLAYER_CHANNELS \
                and parsed.sender != self._our_name:
            try:
                _rel.record_interaction(
                    self.cfg, parsed.sender,
                    channel=parsed.channel,
                    direction='in',
                    text=parsed.body[:500],
                )
                outcome.auto_recorded = True
            except _rel.InvalidPlayerName:
                pass

        if dispatch_llm and self._should_llm_dispatch(parsed):
            self._dispatch_llm(parsed, outcome)
        return outcome

    def tick(self) -> int:
        """Process all chat_received events newer than _last_seen_ts.
        Returns the number of LLM dispatches actually run (capped per
        MAX_LLM_DISPATCHES_PER_TICK so a burst can't drain budget)."""
        path = self.cfg.paths.events_file()
        # Tiny epsilon so we don't re-process the line whose ts we just
        # consumed - iter_since uses >= and ts has only ms resolution.
        cutoff = self._last_seen_ts + 0.001
        events = list(_events.iter_since(path, cutoff))
        if not events:
            return 0
        # Filter to chat events for this character.
        chat_events = [
            e for e in events
            if e.get('source') == 'chat'
            and e.get('type') == 'chat_received'
            and e.get('character') == self.cfg.character
        ]
        # Always advance the watermark over EVERY observed event (even
        # non-chat ones for other sources) so the next tick doesn't
        # re-scan them.
        self._last_seen_ts = max(self._last_seen_ts,
                                 max(e.get('ts', 0.0) for e in events))

        llm_dispatches = 0
        for ev in chat_events:
            allow_llm = llm_dispatches < self.MAX_LLM_DISPATCHES_PER_TICK
            outcome = self.handle_event(ev, dispatch_llm=allow_llm)
            if outcome and outcome.llm_invoked:
                llm_dispatches += 1
        return llm_dispatches
