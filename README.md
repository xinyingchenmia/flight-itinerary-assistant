# Flight Itinerary Assistant

Query international flights in natural language — not just for price, but for what booking sites don't show you: whether a connection is actually long enough, whether you need a transit visa, whether there's ground transport after landing.

## How it works

A hybrid pipeline of deterministic code and LLM agents:

```
Natural-language query
  → Query parsing (LLM intent recognition — not an agent: single call, no tools, no loop)
  → Fetch (manual capture from Ctrip; anti-bot blocks automated browsers, see below)
  → Cross-platform matching / filtering / sorting (deterministic code)
  → Risk Review Agent (verifies visa rules, connection times, etc. via web search tools)
  → Trip Planning Agent (proactive suggestion menu, multi-round tool-calling research)
  → Results
```

Only Risk Review and Trip Planning are true Agents (tool access, autonomous next-step decisions). Everything else is a fixed, code-orchestrated workflow.

## Stack

Python 3.14 · `uv` · `claude-agent-sdk` (structured output + tool calling) · FastAPI · Pydantic v2 · Playwright (browser session, not automated scraping)

## Run it

```bash
uv sync
uv run python scripts/run_web.py
```

Open `http://127.0.0.1:8787`. Requires `ANTHROPIC_API_KEY` set (or an already-authenticated `claude` CLI).

## About fetching

Ctrip runs anti-bot detection (WhaleGuard) that blocks automated Playwright sessions outright, so fetching is done via manual capture instead: you search normally in your own logged-in browser, and a script (`scripts/capture_pull.js`, packaged as a drag-to-bookmarks-bar bookmarklet in the web UI) listens to the responses the page itself receives and auto-downloads them — it doesn't construct, replay, or bypass any anti-bot mechanism.

## Layout

```
src/flight_assistant/
  query_parse/      intent recognition / query parsing
  risk_review/       risk review agent
  clarification/      trip planning agent
  web/                local web backend
  fetchers/            Ctrip/Fliggy response parsers
scripts/               CLI entry points, capture scripts
```
