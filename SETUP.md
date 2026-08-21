# This fork — what's different and why

A fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
at **v0.3.1**, plus three files that turn it from a single-ticker CLI tool into a
screener-driven pipeline you can drive from a chat window.

Nothing in `tradingagents/` or `cli/` has been modified. Everything added is
additive and lives at the repo root, so pulling upstream changes stays clean.

---

## What was added

| File | What it does |
|---|---|
| `screener.py` | Headless port of StockPulse's discovery phase. One Anthropic call with web search → today's candidates → `candidates.json` |
| `pipeline.py` | Runs the screener, then feeds each candidate through TradingAgents sequentially. Instruments timing and rate limits. Writes a digest |
| `mcp_server.py` | Exposes TradingAgents as MCP tools so Claude Desktop can start and read analyses |

## The system

```
screener.py            one Anthropic call, web search, ~60s
      ↓                candidates.json  [ticker, category, reason, urgency, conviction]
pipeline.py            sequential, one ticker at a time
      ↓                TradingAgents multi-agent run per ticker
digest                 ~/.tradingagents/pipeline/run_<stamp>.{md,json}
```

Runs are sequential on purpose. Parallel runs collide with Gemini's per-minute
limit and fail partway, which costs more time than it saves.

---

## Setup from scratch

Requires Python 3.10+ (built and tested on 3.13) and git.

```powershell
git clone https://github.com/firecrest37/TradingAgents.git
cd TradingAgents
python -m venv venv
.\venv\Scripts\pip.exe install .
.\venv\Scripts\pip.exe install mcp        # only if you want the Claude Desktop tools
```

Then create `.env` in the repo root:

```
GOOGLE_API_KEY=<from aistudio.google.com>
TRADINGAGENTS_LLM_PROVIDER=google
TRADINGAGENTS_DEEP_THINK_LLM=gemini-3.5-flash
TRADINGAGENTS_QUICK_THINK_LLM=gemini-3.1-flash-lite
TRADINGAGENTS_LLM_MAX_RETRIES=6
ANTHROPIC_API_KEY=<from console.anthropic.com>
STOCKPULSE_MODEL=claude-sonnet-5
```

`.env` is gitignored. It must never be committed.

**Write `.env` without a byte-order mark.** PowerShell's `Set-Content -Encoding UTF8`
adds one, and a BOM on the first line corrupts the first key. Use:

```powershell
[System.IO.File]::WriteAllText("$PWD\.env", $text, (New-Object System.Text.UTF8Encoding $false))
```

---

## Running it

```powershell
.\venv\Scripts\python.exe screener.py --limit 5        # candidates only
.\venv\Scripts\python.exe pipeline.py --limit 5        # screen + analyse
.\venv\Scripts\python.exe pipeline.py --skip-screen    # reuse candidates.json
.\venv\Scripts\python.exe pipeline.py --dry-run        # plan only, spend nothing
.\venv\Scripts\python.exe -m cli.main                  # upstream interactive CLI
```

Useful flags: `--depth light|medium|deep` (1/2/3 debate rounds),
`--date YYYY-MM-DD`, `--limit N`.

`screener.py --list-models` prints the model IDs your Anthropic key can reach.
Run it first whenever discovery starts failing — retired models are the usual cause.

---

## Measured performance

From a real run, 2026-08-21, light depth, Gemini free tier:

| Metric | Value |
|---|---|
| Screening | 60s, 8 web searches, ~5,100 output tokens |
| Analysis per ticker | **2m 06s** |
| Rate-limit events | ~3.7 per ticker, all absorbed by retries |
| Failures | 0 of 3 |
| Projected 8-candidate cycle | ~17 minutes |

Rate limiting is routine, not exceptional — the retry budget handles it. Watch
the rate-limit count in the digest. If it climbs and runs start failing, that's
the signal to move work off the API.

---

## The five upstream contact points

`mcp_server.py` and `pipeline.py` depend on exactly these parts of upstream.
Nothing else upstream can break them. Check these after any version bump:

1. `TradingAgentsGraph(selected_analysts=, debug=, config=)` constructor signature
2. `.propagate(ticker, date)` returning a `(final_state, decision)` tuple
3. `.save_reports(final_state, ticker)` returning a path to the report tree
4. Report tree layout — `1_analysts/market.md`, `2_research/bull.md`, `4_risk/*.md`, `5_portfolio/decision.md`
5. Config keys `max_debate_rounds` and `max_risk_discuss_rounds`

## Pulling upstream updates

```powershell
git remote add upstream https://github.com/TauricResearch/TradingAgents.git   # once
git fetch upstream
git log --oneline HEAD..upstream/main          # what's new
git diff HEAD upstream/main -- tradingagents/graph/trading_graph.py   # check contact points
git merge upstream/main
```

Then re-run a single ticker before trusting a full pipeline run.

---

## Corrections to the widely-shared YouTube walkthrough

That video covers v0.2.4. Following it literally wastes time:

- **Finnhub is not used.** It was removed after 0.2.4. The key it tells you to get is dead weight.
- **Alpha Vantage is not needed.** Market data, indicators, fundamentals and news all default to yfinance, which is keyless.
- **You need exactly one key** for TradingAgents itself — the LLM provider. (The second key here is for the screener, which the video knows nothing about.)
- **`python -m cli.main` needs a real console.** Windows Terminal, PowerShell, or cmd. Not a piped or embedded terminal.

## Notes on StockPulse

`stockpulse.html` is a browser app that does the same discovery *plus* a
single-model per-stock analysis. That second phase is deliberately **not** used
here — TradingAgents replaces it with four analysts, an adversarial bull/bear
debate, and a three-agent risk committee. Running both means paying for a weaker
version of the analysis you're about to run properly.

The screening prompt in `screener.py` is lifted verbatim from StockPulse's
`discover()` so both stay in sync. If you change one, change the other.

Its keys live in browser localStorage, not in the file.

## Local models (Ollama) — assessed, deferred

Hardware: 2× RTX 3080, 10 GB each. Local model on hand: `gemma4:31b` at 19 GB.

19 GB of weights against ~20 GB of total VRAM leaves no room for KV cache, and
this workload grows context steadily as analyst reports feed the debate chain.
It would spill into system RAM and slow to a crawl.

If API rate limiting ever becomes a real problem, the path is a **hybrid**, not a
wholesale swap: put a ~8–9 GB model on one card for `quick_think` (the
high-volume analyst work) and leave `deep_think` on the API for the three agents
that must emit valid JSON — research manager, trader, portfolio manager. Small
models fumble structured output, and those three are where it would hurt.
