"""
screener.py — headless port of StockPulse's discovery phase.

Calls the Anthropic Messages API with the web-search tool and asks for today's
best trading candidates. The system prompt is lifted verbatim from the
StockPulse browser app so the screening logic stays identical; this file just
makes it runnable without a browser, so a pipeline can pick up the results.

Writes candidates.json:
    {
      "generated": "2026-08-21T09:14:03",
      "marketCondition": "BULLISH",
      "indices": {...}, "sectors": [...],
      "opportunities": [
        {"ticker":"NVDA","category":"MOMENTUM","reason":"...",
         "urgency":"HIGH","initialConviction":75}
      ]
    }

Usage:
    python screener.py                     # write candidates.json
    python screener.py --limit 5           # keep only the top 5
    python screener.py --list-models       # show model IDs your key can use
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# StockPulse pins claude-sonnet-4-20250514, which has since been retired.
# Override with STOCKPULSE_MODEL in .env. Run --list-models to see valid ids.
DEFAULT_MODEL = os.environ.get("STOCKPULSE_MODEL", "claude-sonnet-5")

# StockPulse used 3000, sized for a 2024 model with no extended thinking.
# Current models spend budget on thinking blocks and search results before
# emitting any text, so this needs to be generous or the run dies with
# stop_reason=max_tokens having produced nothing.
MAX_TOKENS = int(os.environ.get("STOCKPULSE_MAX_TOKENS", "16000"))
MAX_SEARCHES = int(os.environ.get("STOCKPULSE_MAX_SEARCHES", "8"))

# Verbatim from StockPulse's discover(). Do not reword casually — the JSON shape
# downstream (and the browser app) both depend on it.
SYSTEM_PROMPT = """You are a market scanner. Search the web for today's best stock and ETF trading opportunities. Find: biggest movers, Reddit trending stocks (WSB/r/stocks), major catalysts (earnings/FDA/M&A/analyst calls), unusual options activity, technical breakouts, momentum plays, and notable ETFs (leveraged, sector, thematic).
Also get: SPY QQQ DIA IWM current price and % change; VIX; 10yr yield; sector ETF changes for XLK XLV XLF XLE XLY XLU XLB XLRE.
Return ONLY valid JSON, no markdown:
{"marketCondition":"BULLISH","indices":{"spy":{"price":0,"chg":0},"qqq":{"price":0,"chg":0},"dia":{"price":0,"chg":0},"iwm":{"price":0,"chg":0},"vix":{"price":0},"tenYear":{"yield":0}},"sectors":[{"name":"Technology","chg":0},{"name":"Healthcare","chg":0},{"name":"Finance","chg":0},{"name":"Energy","chg":0},{"name":"Cons Disc","chg":0},{"name":"Utilities","chg":0},{"name":"Materials","chg":0},{"name":"Real Est","chg":0}],"opportunities":[{"ticker":"TICKER","category":"MOMENTUM","reason":"Specific reason right now","urgency":"HIGH","initialConviction":75}]}
category: MOMENTUM, CATALYST, SOCIAL, BREAKOUT, UNUSUAL, MOVER, or ETF. Return 8-10 opportunities ordered by urgency."""

USER_PROMPT = "Scan the market right now. Find best trading opportunities today. Return the full JSON."


class ScreenerError(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not k:
        raise ScreenerError(
            "ANTHROPIC_API_KEY is not set.\n"
            "Add it to the .env file next to this script:\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n"
            "It is the same key StockPulse holds in your browser."
        )
    return k


def _headers() -> dict:
    return {
        "content-type": "application/json",
        "anthropic-version": VERSION,
        "x-api-key": _key(),
    }


def list_models() -> list[dict]:
    """Ask the API which models this key can actually use."""
    r = requests.get(f"{API}/models", headers=_headers(), timeout=30)
    if not r.ok:
        raise ScreenerError(f"Could not list models ({r.status_code}): {r.text[:300]}")
    return r.json().get("data", [])


def _post(body: dict, retries: int = 5, log=print) -> dict:
    """POST to /messages, honouring 429 backoff the way StockPulse does."""
    for attempt in range(retries):
        r = requests.post(f"{API}/messages", headers=_headers(), json=body, timeout=300)

        if r.status_code == 429:
            wait = int(r.headers.get("retry-after") or 0) or (attempt + 1) * 30
            log(f"  rate limited — waiting {wait}s (attempt {attempt + 1}/{retries})")
            time.sleep(wait)
            continue

        if r.status_code == 404:
            names = ", ".join(m.get("id", "?") for m in list_models()[:12]) or "(none returned)"
            raise ScreenerError(
                f"Model '{body.get('model')}' was not found — it may have been retired.\n"
                f"Models available to your key: {names}\n"
                f"Set a working one in .env:  STOCKPULSE_MODEL=<id>"
            )

        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:300])
            except ValueError:
                msg = r.text[:300]
            raise ScreenerError(f"Anthropic API error {r.status_code}: {msg}")

        return r.json()

    raise ScreenerError("Rate-limit retries exhausted. Wait a minute and run it again.")


def _extract_json(text: str) -> dict:
    """Same tolerant extraction StockPulse uses: first '{' to last '}'."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ScreenerError(f"No JSON in the response. Preview: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ScreenerError(
            f"JSON parse failed: {exc}. Preview: {text[start:start + 200]}"
        ) from exc


def discover(model: str = DEFAULT_MODEL, log=print) -> dict:
    """Run the discovery call and return the parsed result."""
    log(f"Calling Anthropic ({model}) — up to {MAX_SEARCHES} web searches, "
        f"{MAX_TOKENS} token budget…")
    started = time.time()

    data = _post({
        "model": model,
        "max_tokens": MAX_TOKENS,
        # Cap the searches. Each result block eats budget, and an uncapped
        # model will happily run a dozen before it starts writing JSON.
        "tools": [{"type": "web_search_20250305", "name": "web_search",
                   "max_uses": MAX_SEARCHES}],
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": USER_PROMPT}],
    }, log=log)

    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        stop = data.get("stop_reason")
        searches = sum(1 for b in blocks if b.get("type") == "web_search_tool_result")
        if stop == "max_tokens":
            raise ScreenerError(
                f"Ran out of token budget before writing any JSON "
                f"(max_tokens={MAX_TOKENS}, {searches} searches completed).\n"
                f"Raise it in .env:  STOCKPULSE_MAX_TOKENS={MAX_TOKENS * 2}\n"
                f"Or search less:    STOCKPULSE_MAX_SEARCHES={max(2, MAX_SEARCHES // 2)}"
            )
        kinds = ", ".join(b.get("type", "?") for b in blocks) or "(no blocks)"
        raise ScreenerError(
            f"No text in the response. Block types: {kinds}. Stop reason: {stop}"
        )

    result = _extract_json(text)
    usage = data.get("usage", {}) or {}
    result["_meta"] = {
        "model": model,
        "elapsed_seconds": round(time.time() - started, 1),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "web_searches": usage.get("server_tool_use", {}).get("web_search_requests"),
    }
    return result


def run(limit: int | None, out_path: Path, model: str = DEFAULT_MODEL, log=print) -> dict:
    result = discover(model=model, log=log)

    opps = result.get("opportunities") or []
    if not opps:
        raise ScreenerError(
            "Discovery returned no opportunities. The market may be closed — try again later."
        )

    # Keep urgency ordering, then trim. HIGH first, as the prompt asks for.
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    opps.sort(key=lambda o: rank.get(str(o.get("urgency", "")).upper(), 3))
    if limit:
        opps = opps[:limit]
    result["opportunities"] = opps

    result["generated"] = datetime.now().isoformat(timespec="seconds")
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    meta = result["_meta"]
    log(f"\nMarket condition: {result.get('marketCondition', 'unknown')}")
    log(f"Found {len(opps)} candidates in {meta['elapsed_seconds']}s "
        f"({meta.get('web_searches')} web searches, {meta.get('output_tokens')} output tokens)\n")
    for i, o in enumerate(opps, 1):
        log(f"  {i}. {o.get('ticker', '?'):<7} {str(o.get('urgency', '')):<7} "
            f"{str(o.get('category', '')):<9} conv {o.get('initialConviction', '?')}")
        log(f"     {o.get('reason', '')[:100]}")
    log(f"\nWrote {out_path}")
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="Find today's trading candidates.")
    p.add_argument("--limit", type=int, default=None,
                   help="keep only the top N candidates by urgency")
    p.add_argument("--out", type=Path, default=HERE / "candidates.json",
                   help="where to write the candidate list")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model id")
    p.add_argument("--list-models", action="store_true",
                   help="print the model ids your key can use, then exit")
    a = p.parse_args()

    try:
        if a.list_models:
            for m in list_models():
                print(f"  {m.get('id')}   {m.get('display_name', '')}")
            return 0
        run(limit=a.limit, out_path=a.out, model=a.model)
        return 0
    except ScreenerError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
