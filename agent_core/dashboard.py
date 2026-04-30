"""Observability dashboard for the running agent.

A localhost HTTP server (default port 7777) running in a background
daemon thread alongside the main poll loop. Serves:

    GET  /            HTML dashboard page (single file, no CDNs)
    GET  /api/state   JSON snapshot - world state, goals, gambits,
                      recent events, LLM cost summary

The page polls /api/state once a second and re-renders. No
WebSockets / SSE / framework - keeps the dependency footprint at
"Python stdlib + browser."

Phase 7 scope: read-only visibility. Manual controls (pause the
agent, force re-plan, send a chat message) ride on the same server
in a follow-up.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import config as _config
from . import events as _events
from . import state as _state


# Embedded HTML/CSS/JS. One self-contained page; the JS polls
# /api/state every second and re-renders. Kept terse on purpose -
# this is a debug surface, not a product UI.
_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>agent_core dashboard</title>
  <style>
    body { font: 13px/1.4 system-ui, sans-serif; margin: 0; background: #1a1a1a; color: #ddd; }
    header { padding: 8px 14px; background: #111; border-bottom: 1px solid #333; }
    header h1 { margin: 0; font-size: 14px; font-weight: normal; color: #aaa; }
    main { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; padding: 0; background: #333; }
    section { background: #1a1a1a; padding: 10px 14px; min-height: 120px; }
    section h2 { margin: 0 0 6px; font-size: 12px; font-weight: normal; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }
    .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; font-family: ui-monospace, monospace; font-size: 12px; }
    .kv > b { color: #888; font-weight: normal; }
    pre { font-family: ui-monospace, monospace; font-size: 12px; margin: 0; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow: auto; }
    .pill { display: inline-block; padding: 1px 6px; border-radius: 8px; background: #333; font-size: 11px; margin-right: 4px; }
    .pill.ok { background: #1f4a1f; color: #b8e0b8; }
    .pill.warn { background: #4a3a1f; color: #e0c8a0; }
    .pill.err { background: #4a1f1f; color: #e0a0a0; }
    .row { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
    .muted { color: #666; }
    .ev { padding: 2px 0; border-bottom: 1px solid #2a2a2a; font-family: ui-monospace, monospace; font-size: 11px; }
    .ev .src { color: #6aa; margin-right: 6px; }
    .ev .type { color: #aa6; margin-right: 6px; }
    .ev .ts { color: #555; margin-right: 6px; }
  </style>
</head>
<body>
<header><h1>agent_core dashboard <span id="char" class="muted"></span></h1></header>
<main>
  <section>
    <h2>world state</h2>
    <div class="kv" id="world"></div>
  </section>
  <section>
    <h2>goals</h2>
    <pre id="goals"></pre>
  </section>
  <section>
    <h2>gambits</h2>
    <pre id="gambits"></pre>
  </section>
  <section>
    <h2>llm cost (session)</h2>
    <div class="kv" id="llm"></div>
  </section>
  <section style="grid-column: 1 / 3;">
    <h2>recent events</h2>
    <div id="events"></div>
  </section>
</main>
<script>
function renderKV(target, obj) {
  const el = document.getElementById(target);
  el.innerHTML = '';
  for (const [k, v] of Object.entries(obj || {})) {
    const b = document.createElement('b'); b.textContent = k;
    const s = document.createElement('span'); s.textContent = v == null ? 'null' : String(v);
    el.appendChild(b); el.appendChild(s);
  }
}
function renderEvents(list) {
  const el = document.getElementById('events');
  el.innerHTML = '';
  for (const ev of (list || []).slice().reverse()) {
    const d = document.createElement('div');
    d.className = 'ev';
    const ts = new Date((ev.ts || 0) * 1000).toLocaleTimeString();
    const ts_e = document.createElement('span'); ts_e.className = 'ts'; ts_e.textContent = ts;
    const src = document.createElement('span'); src.className = 'src'; src.textContent = ev.source || '?';
    const ty = document.createElement('span'); ty.className = 'type'; ty.textContent = ev.type || '?';
    const txt = document.createElement('span');
    const rest = {...ev}; delete rest.ts; delete rest.source; delete rest.type; delete rest.character;
    txt.textContent = JSON.stringify(rest);
    d.appendChild(ts_e); d.appendChild(src); d.appendChild(ty); d.appendChild(txt);
    el.appendChild(d);
  }
}
async function tick() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    document.getElementById('char').textContent = '. ' + (d.character || '?');
    renderKV('world', d.world || {});
    document.getElementById('goals').textContent = JSON.stringify(d.goals || {}, null, 2);
    document.getElementById('gambits').textContent = JSON.stringify(d.gambits || [], null, 2);
    renderKV('llm', d.llm || {});
    renderEvents(d.events || []);
  } catch (e) {
    document.getElementById('events').textContent = 'fetch error: ' + e;
  }
}
tick(); setInterval(tick, 1000);
</script>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    cfg: _config.Config | None = None  # set by start()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Quiet stdout; the dashboard polls every second and the access
        # log is noise. Errors still go through log_error.
        return

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == '/' or self.path == '/index.html':
            body = _HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/api/state':
            cfg = self.__class__.cfg
            if cfg is None:
                self.send_error(503, 'agent_core not initialised')
                return
            body = json.dumps(_build_snapshot(cfg)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, 'not found')


def _build_snapshot(cfg: _config.Config) -> dict[str, Any]:
    """Walk the IPC + persistent files into a single dict the dashboard
    can render. Best-effort on every read; missing or malformed files
    just produce missing fields."""
    paths = cfg.paths
    state_dir = paths.state_dir(cfg.character)
    persistent_dir = paths.persistent_dir(cfg.character)

    # World state - flatten the most actionable bits of the channels
    # into a small kv table for the top-of-page view.
    ws = _state.aggregate(state_dir, cfg.character)
    nav = ws.get('nav') or {}
    # Phase 1b will move nav under state/<char>/nav.json. Until then
    # nav still publishes to the legacy nav_status.json at the IPC root,
    # so fall back there if the per-character file is missing.
    if not nav:
        legacy_nav = paths.ipc_base / 'nav_status.json'
        if legacy_nav.exists():
            try:
                nav = json.loads(legacy_nav.read_text())
            except (json.JSONDecodeError, OSError):
                nav = {}
    combat = ws.get('combat') or {}
    chat = ws.get('chat') or {}
    inventory = ws.get('inventory') or {}
    self_ = combat.get('self') or {}
    target = combat.get('target') or {}
    inv_main = (inventory.get('containers') or {}).get('inventory') or {}
    world = {
        'zone':       f"{nav.get('zone_id', '?')} ({nav.get('zone_name', '?')})",
        'pos':        f"({nav.get('x'):.1f}, {nav.get('y'):.1f}, {nav.get('z'):.1f})"
                       if nav.get('x') is not None else '?',
        'moving':     bool(nav.get('moving', False)),
        'engaged':    bool(combat.get('engaged', False)),
        'self_hp':    f"{self_.get('hp', 0)}/{self_.get('hp_max', 0)} ({self_.get('hp_pct', 0):.0f}%)",
        'self_mp':    f"{self_.get('mp', 0)}/{self_.get('mp_max', 0)}",
        'self_tp':    self_.get('tp', 0),
        'job':        f"{self_.get('main_job', '?')}/{self_.get('sub_job', '?')} "
                       f"lvl {self_.get('main_job_lvl', '?')}",
        'target':     target.get('name', '-') if target else '-',
        'target_hp':  f"{target.get('hp_pct', '?')}%" if target else '-',
        'chat_lines': len(chat.get('lines', [])) if chat else 0,
        'inv_used':   f"{inv_main.get('used', 0)}/{inv_main.get('capacity', 0)}"
                       if inv_main else '-',
    }

    # Persistent files
    goals_path = persistent_dir / 'goals.json'
    goals = {}
    if goals_path.exists():
        try:
            goals = json.loads(goals_path.read_text())
        except (json.JSONDecodeError, OSError):
            goals = {'error': 'unparseable'}

    gambit_path = paths.commands_dir(cfg.character) / 'combat.json'
    gambits = []
    if gambit_path.exists():
        try:
            gambits = json.loads(gambit_path.read_text()).get('gambits', [])
        except (json.JSONDecodeError, OSError):
            gambits = [{'error': 'unparseable'}]

    # Recent events + LLM cost summary
    events_tail = _events.tail(paths.events_file(), n=200)
    llm_calls = [e for e in events_tail if e.get('source') == 'llm_gateway' and e.get('type') == 'llm_call']
    llm_calls += [e for e in events_tail if e.get('source') == 'planner' and e.get('type') == 'plan_generated']
    in_tok = sum(e.get('input_tokens', 0) for e in llm_calls)
    out_tok = sum(e.get('output_tokens', 0) for e in llm_calls)
    by_tier = Counter(e.get('tier', '?') for e in llm_calls)
    llm = {
        'session_calls': len(llm_calls),
        'input_tokens':  in_tok,
        'output_tokens': out_tok,
        'by_tier':       ', '.join(f'{t}={n}' for t, n in by_tier.items()) or '-',
    }

    return {
        'character': cfg.character,
        'ts':        time.time(),
        'world':     world,
        'goals':     goals,
        'gambits':   gambits,
        'events':    events_tail[-100:],
        'llm':       llm,
    }


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(cfg: _config.Config, port: int = 7777) -> threading.Thread:
    """Start the dashboard server on a daemon thread. Returns the
    thread; daemon=True means it dies with the process so we don't
    have to coordinate shutdown."""
    _Handler.cfg = cfg
    server = _ThreadingServer(('127.0.0.1', port), _Handler)

    def serve() -> None:
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as e:  # pragma: no cover
            print(f'  dashboard: serve loop crashed: {e}')

    t = threading.Thread(target=serve, name='agent-dashboard', daemon=True)
    t.start()
    print(f'  dashboard: http://127.0.0.1:{port}/')
    return t
