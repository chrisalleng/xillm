"""Web research surface for LLM tool use - DuckDuckGo HTML search + URL
fetch, with on-disk caching.

The LLM's training-data knowledge of FFXI is unreliable for specifics
(zone level ranges, quest prereqs, item drops). This module gives the
planner / judges a way to look things up at decision time.

The agent runs against a 2007-era LandSandBoat server (RoZ + CoP only),
so retrieval must be biased toward era-correct sources. The default
search wraps the query with a `site:` hint that prefers
classicffxi.fandom.com (era-correct wiki) and the LandSandBoat repo
(server source of truth). Callers can override with explicit `site:`
clauses in the query.

Cache: every search and fetch is cached to
`persistent/<character>/web_cache/<sha256>.json` with a 7-day TTL. FFXI
content rarely changes; the first plan call after a fresh start does
the network work, and subsequent calls are free.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from . import config as _config


# How long a cached search/fetch result stays valid before we re-fetch.
# 7 days is fine for FFXI wiki content; bump if you're worried about
# wiki edits, lower if you suspect a particular page is changing.
CACHE_TTL_S = 7 * 24 * 3600

# Default per-request timeout. DDG and the wikis are usually fast; we
# fail fast rather than block a planner call for minutes.
HTTP_TIMEOUT_S = 12.0

# Truncate fetched body content to this many characters before returning
# to the LLM. ~16k chars ~ 4k tokens - plenty for a wiki article's main
# section, well under the deliberative tier's context budget.
MAX_FETCH_CHARS = 16000

# How many search results to return. 5 is enough for the LLM to choose
# the most relevant link without burning tokens on long lists.
SEARCH_RESULT_COUNT = 5

# Pretend to be a normal browser. DDG HTML serves an empty page if the
# user-agent looks like a script.
USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)


def _cache_dir(cfg: _config.Config) -> Path:
    return cfg.paths.persistent_dir(cfg.character) / 'web_cache'


def _cache_path(cfg: _config.Config, key: str) -> Path:
    h = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return _cache_dir(cfg) / f'{h}.json'


def _cache_load(cfg: _config.Config, key: str) -> Any | None:
    p = _cache_path(cfg, key)
    if not p.exists():
        return None
    try:
        with open(p, encoding='utf-8') as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - float(entry.get('ts') or 0) > CACHE_TTL_S:
        return None
    return entry.get('value')


def _cache_save(cfg: _config.Config, key: str, value: Any) -> None:
    p = _cache_path(cfg, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'ts': time.time(), 'value': value}, f)
    except OSError:
        pass


def _ddg_search(query: str) -> list[dict[str, str]]:
    """Hit DuckDuckGo's HTML endpoint and parse the result list. No
    API key, no JSON endpoint - we scrape the HTML SERP. Result links
    on duckduckgo.com/html are wrapped in a redirect URL; we unwrap
    them before returning."""
    url = 'https://html.duckduckgo.com/html/'
    resp = requests.post(
        url,
        data={'q': query},
        headers={'User-Agent': USER_AGENT},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    results: list[dict[str, str]] = []
    for r in soup.select('div.result')[:SEARCH_RESULT_COUNT * 2]:
        a = r.select_one('a.result__a')
        if a is None:
            continue
        href = a.get('href') or ''
        # DDG HTML wraps real URLs in /l/?uddg=<encoded>. Unwrap.
        if href.startswith('//') or href.startswith('/'):
            parsed = urllib.parse.urlparse('https:' + href if href.startswith('//') else href)
            qs = urllib.parse.parse_qs(parsed.query)
            uddg = qs.get('uddg') or qs.get('u')
            if uddg:
                href = uddg[0]
        title = a.get_text(' ', strip=True)
        snippet_el = r.select_one('a.result__snippet') or r.select_one('.result__snippet')
        snippet = snippet_el.get_text(' ', strip=True) if snippet_el else ''
        if not href or not title:
            continue
        results.append({'title': title, 'url': href, 'snippet': snippet})
        if len(results) >= SEARCH_RESULT_COUNT:
            break
    return results


def search(cfg: _config.Config, query: str) -> dict[str, Any]:
    """Public search entry point. Returns
    `{"query": ..., "results": [{title, url, snippet}, ...]}`. Cached
    by query string. The LLM is expected to run this first, pick the
    most relevant URL from the results, then call `fetch` on it."""
    cached = _cache_load(cfg, f'search::{query}')
    if cached is not None:
        return cached
    try:
        results = _ddg_search(query)
        out = {'query': query, 'results': results}
    except Exception as e:
        out = {
            'query':   query,
            'results': [],
            'error':   f'{type(e).__name__}: {e}',
        }
    _cache_save(cfg, f'search::{query}', out)
    return out


def _extract_main_text(html: str) -> str:
    """Pull the readable body text out of an HTML document. Wikis use
    `#mw-content-text` (MediaWiki) or `.mw-parser-output`; for generic
    pages we fall back to body. We strip nav/script/style noise."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
        tag.decompose()
    main = (
        soup.select_one('#mw-content-text')
        or soup.select_one('.mw-parser-output')
        or soup.select_one('main')
        or soup.select_one('article')
        or soup.body
        or soup
    )
    text = main.get_text('\n', strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def fetch(cfg: _config.Config, url: str) -> dict[str, Any]:
    """Fetch a URL and return its main readable text content (truncated
    to MAX_FETCH_CHARS). Cached by URL.

    Returns `{"url", "title", "content", "truncated"}` on success;
    `{"url", "error": ...}` on failure. The LLM should call this after
    `search` to read details from a chosen result, not as a primary
    research path."""
    cached = _cache_load(cfg, f'fetch::{url}')
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            url,
            headers={'User-Agent': USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        title_el = soup.find('title')
        title = title_el.get_text(strip=True) if title_el else url
        content = _extract_main_text(resp.text)
        truncated = len(content) > MAX_FETCH_CHARS
        if truncated:
            content = content[:MAX_FETCH_CHARS] + '\n\n...[truncated]'
        out = {
            'url':       url,
            'title':     title,
            'content':   content,
            'truncated': truncated,
        }
    except Exception as e:
        out = {'url': url, 'error': f'{type(e).__name__}: {e}'}
    _cache_save(cfg, f'fetch::{url}', out)
    return out


# ---------------------------------------------------------------------
# Tool schemas (OpenAI-compat) for run_tool_loop wiring.
# ---------------------------------------------------------------------

WEB_SEARCH_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'web_search',
        'description': (
            'Search the web (DuckDuckGo). Use this BEFORE committing to '
            'any plan that involves specific FFXI zones, quests, jobs, '
            'or items - your training-data FFXI knowledge is unreliable '
            'for specifics, and this server is in a 2007-era state '
            '(Rise of Zilart + Chains of Promathia ONLY, no Aht Urhgan '
            'or later). Prefer queries that target the era-correct '
            'wiki (classicffxi.fandom.com) or the LandSandBoat server '
            'source on github.com/LandSandBoat/server. Returns up to 5 '
            'results with title, url, and snippet.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {
                    'type':        'string',
                    'description': (
                        'Search query. For era-correct results, append '
                        '`site:classicffxi.fandom.com` or include the '
                        'phrase "classic FFXI" / "RoZ" / "CoP" to bias '
                        'away from later-expansion content.'
                    ),
                },
            },
            'required': ['query'],
        },
    },
}

WEB_FETCH_TOOL: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': 'web_fetch',
        'description': (
            'Fetch a URL and return its main readable text content. Use '
            'this AFTER web_search to read the full article on a chosen '
            'result. Only fetch URLs you\'ve seen in a previous search '
            'response - do not invent URLs. Content is truncated at '
            '~16k chars; use it for short, specific questions, not '
            'general browsing.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'url': {
                    'type':        'string',
                    'description': 'Full URL (must include scheme).',
                },
            },
            'required': ['url'],
        },
    },
}


def make_handlers(cfg: _config.Config) -> dict[str, Any]:
    """Build the tool_handlers dict that run_tool_loop expects. Closes
    over `cfg` so the cache lands under the right character namespace."""
    return {
        'web_search': lambda args: search(cfg, str(args.get('query') or '')),
        'web_fetch':  lambda args: fetch(cfg,  str(args.get('url')   or '')),
    }


# ---------------------------------------------------------------------
# Era constraint - shared prompt fragment, included by every system
# prompt that asks the LLM to make FFXI-specific decisions.
# ---------------------------------------------------------------------

ERA_CONSTRAINT = """\
SERVER ERA - CRITICAL CONTEXT:
This LandSandBoat server runs FFXI in a 2007-era state. ONLY the
following expansions are present:
  - Rise of Zilart (RoZ)
  - Chains of Promathia (CoP)

ANYTHING from later expansions is ABSENT - do not reference it:
  - Treasures of Aht Urhgan (NPCs, zones, jobs BLU/COR/PUP, Salvage,
    Assault, Besieged, Imperial Standing, etc.)
  - Wings of the Goddess (DNC/SCH, Campaign, past-Vana'diel zones)
  - Seekers of Adoulin / Rhapsodies of Vana'diel (GEO/RUN, Adoulin
    zones, Rhapsodies cutscenes)
  - Trusts, Records of Eminence, Job Points, Master Levels - none of
    these existed in the 2007 era

Available jobs ONLY: WAR MNK WHM BLM RDM THF PLD DRK BST BRD RNG SAM
NIN DRG SMN.

Canonical sources (in priority order):
  1. github.com/LandSandBoat/server - actual server behavior, scripts,
     mob stats, drop rates. Truth for what exists IN-GAME on this
     server.
  2. classicffxi.fandom.com - era-correct community wiki. Trust this
     for general gameplay info.
  3. AVOID ffxiclopedia, bg-wiki main pages, ffxiah for anything
     post-CoP - they include Aht Urhgan+ content.

USE web_search BEFORE committing to specific zone names, level ranges,
quest prerequisites, or job-unlock steps. Your training-data FFXI
knowledge is unreliable for specifics - it conflates eras and post-
launch retail with the classic state.
"""
