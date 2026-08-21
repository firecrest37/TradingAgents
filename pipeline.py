"""
pipeline.py — screener → TradingAgents, end to end.

Runs the discovery screener, then feeds each candidate through the full
TradingAgents multi-agent pipeline one at a time, and writes a digest.

Runs are deliberately sequential. Parallel runs collide with the Gemini free
tier's per-minute limit and fail halfway through, which costs more time than
it saves.

Everything is instrumented — per-ticker wall time, rate-limit hits, retries,
failures — so the "should we move to local models?" question gets answered by
numbers from a real run rather than by guesswork.

Usage:
    python pipeline.py                      # screen, then analyse the top 5
    python pipeline.py --limit 8            # more candidates
    python pipeline.py --skip-screen        # reuse the existing candidates.json
    python pipeline.py --depth medium       # deeper debate (slower, more calls)
    python pipeline.py --dry-run            # screen only, show the plan, stop
"""

import argparse
import contextlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv          # noqa: E402
load_dotenv(HERE / ".env")

import screener                          # noqa: E402

RUNS_DIR = Path.home() / ".tradingagents" / "pipeline"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

RATE_PAT = re.compile(r"429|rate.?limit|resource.?exhausted|quota", re.I)
RETRY_PAT = re.compile(r"retry|retrying|backoff", re.I)


class Counter(logging.Handler):
    """Counts rate-limit and retry chatter from the libraries underneath."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.rate_limits = 0
        self.retries = 0
        self.samples: list[str] = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        if RATE_PAT.search(msg):
            self.rate_limits += 1
            if len(self.samples) < 5:
                self.samples.append(msg[:200])
        elif RETRY_PAT.search(msg):
            self.retries += 1

    def reset(self):
        self.rate_limits = 0
        self.retries = 0
        self.samples = []


def human(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def analyse(ticker: str, trade_date: str, depth: str, counter: Counter, log=print):
    """Run one ticker through TradingAgents. Returns a result dict."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    rounds = {"light": 1, "medium": 2, "deep": 3}[depth]
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = rounds
    config["max_risk_discuss_rounds"] = rounds

    counter.reset()
    started = time.time()
    try:
        # The framework prints progress; keep our stdout clean for the digest.
        with contextlib.redirect_stdout(sys.stderr):
            graph = TradingAgentsGraph(
                selected_analysts=("market", "social", "news", "fundamentals"),
                debug=False,
                config=config,
            )
            final_state, decision = graph.propagate(ticker, trade_date)
            report_path = graph.save_reports(final_state, ticker)

        risk = final_state.get("risk_debate_state") or {}
        return {
            "ticker": ticker,
            "status": "done",
            "decision": str(decision),
            "portfolio_manager": risk.get("judge_decision", "") if isinstance(risk, dict) else "",
            "report_path": str(report_path),
            "seconds": round(time.time() - started, 1),
            "rate_limits": counter.rate_limits,
            "retries": counter.retries,
            "rate_limit_samples": list(counter.samples),
        }
    except Exception as exc:
        return {
            "ticker": ticker,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.time() - started, 1),
            "rate_limits": counter.rate_limits,
            "retries": counter.retries,
            "rate_limit_samples": list(counter.samples),
        }


def write_digest(run: dict, path: Path) -> None:
    done = [r for r in run["results"] if r["status"] == "done"]
    failed = [r for r in run["results"] if r["status"] != "done"]
    lines = [
        f"# Pipeline run — {run['started']}",
        "",
        f"Market condition: **{run.get('market_condition', 'unknown')}**  ",
        f"Depth: {run['depth']} · Screened: {run['screened']} · "
        f"Analysed: {len(done)} · Failed: {len(failed)}  ",
        f"Total wall time: **{human(run['total_seconds'])}**  ",
        f"Rate-limit hits: **{run['total_rate_limits']}** · Retries: {run['total_retries']}",
        "",
        "## Decisions",
        "",
        "| Ticker | Decision | Why it was screened | Urgency | Time | 429s |",
        "|---|---|---|---|---|---|",
    ]
    for r in done:
        why = (r.get("reason") or "")[:70].replace("|", "/")
        lines.append(
            f"| **{r['ticker']}** | {r['decision']} | {why} | "
            f"{r.get('urgency', '')} | {human(r['seconds'])} | {r['rate_limits']} |"
        )
    if failed:
        lines += ["", "## Failed", ""]
        for r in failed:
            lines.append(f"- **{r['ticker']}** — {r.get('error', 'unknown')}")
    if run["total_rate_limits"]:
        lines += [
            "", "## Rate limiting", "",
            f"{run['total_rate_limits']} rate-limit events across the run. "
            "If this number is large, that is the signal to move the high-volume "
            "analyst agents onto a local model.", "",
        ]
        for s in run.get("rate_limit_samples", [])[:5]:
            lines.append(f"- `{s}`")
    lines += ["", "## Portfolio manager reasoning", ""]
    for r in done:
        lines += [f"### {r['ticker']} — {r['decision']}", "",
                  r.get("portfolio_manager") or "_(none recorded)_", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Screen for stocks, then analyse each one.")
    p.add_argument("--limit", type=int, default=5, help="how many candidates to analyse")
    p.add_argument("--depth", choices=["light", "medium", "deep"], default="light")
    p.add_argument("--skip-screen", action="store_true",
                   help="reuse the existing candidates.json instead of screening again")
    p.add_argument("--dry-run", action="store_true",
                   help="screen and show the plan, but do not analyse")
    p.add_argument("--date", default=datetime.now().date().isoformat(),
                   help="trade date, YYYY-MM-DD")
    a = p.parse_args()

    counter = Counter()
    logging.getLogger().addHandler(counter)
    logging.getLogger().setLevel(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    candidates_path = HERE / "candidates.json"
    started_at = datetime.now()
    t0 = time.time()

    # ---- stage 1: screen -------------------------------------------------
    print("=" * 62)
    print("STAGE 1 — SCREENING")
    print("=" * 62)
    try:
        if a.skip_screen:
            if not candidates_path.exists():
                print("No candidates.json to reuse. Run without --skip-screen first.",
                      file=sys.stderr)
                return 1
            data = json.loads(candidates_path.read_text(encoding="utf-8"))
            print(f"Reusing {candidates_path.name} from {data.get('generated', 'unknown time')}")
            opps = (data.get("opportunities") or [])[:a.limit]
            for i, o in enumerate(opps, 1):
                print(f"  {i}. {o.get('ticker'):<7} {o.get('urgency', ''):<7} {o.get('category', '')}")
        else:
            data = screener.run(limit=a.limit, out_path=candidates_path)
            opps = data["opportunities"]
    except screener.ScreenerError as exc:
        print(f"\nScreening failed: {exc}", file=sys.stderr)
        return 1

    screen_seconds = time.time() - t0

    if a.dry_run:
        est = len(opps) * {"light": 4, "medium": 8, "deep": 16}[a.depth]
        print(f"\nDry run. Screening took {human(screen_seconds)}.")
        print(f"Analysing {len(opps)} candidates at {a.depth} depth "
              f"would take roughly {est} minutes.")
        return 0

    # ---- stage 2: analyse ------------------------------------------------
    print()
    print("=" * 62)
    print(f"STAGE 2 — ANALYSIS ({len(opps)} tickers, {a.depth} depth, sequential)")
    print("=" * 62)

    results = []
    try:
        for i, opp in enumerate(opps, 1):
            ticker = str(opp.get("ticker", "")).upper().strip()
            if not ticker:
                continue
            print(f"\n[{i}/{len(opps)}] {ticker} — {opp.get('category', '')} "
                  f"({opp.get('urgency', '')})")
            print(f"        {str(opp.get('reason', ''))[:90]}")
            r = analyse(ticker, a.date, a.depth, counter)
            r["reason"] = opp.get("reason", "")
            r["urgency"] = opp.get("urgency", "")
            r["category"] = opp.get("category", "")
            r["conviction"] = opp.get("initialConviction")
            results.append(r)
            if r["status"] == "done":
                print(f"        -> {r['decision']}   [{human(r['seconds'])}, "
                      f"{r['rate_limits']} rate-limit events]")
            else:
                print(f"        -> FAILED: {r['error'][:120]}")
    except KeyboardInterrupt:
        print("\nInterrupted — writing what finished so far.", file=sys.stderr)

    # ---- stage 3: digest -------------------------------------------------
    total = time.time() - t0
    run = {
        "started": started_at.isoformat(timespec="seconds"),
        "depth": a.depth,
        "date": a.date,
        "screened": len(opps),
        "screen_seconds": round(screen_seconds, 1),
        "total_seconds": round(total, 1),
        "market_condition": data.get("marketCondition"),
        "total_rate_limits": sum(r["rate_limits"] for r in results),
        "total_retries": sum(r["retries"] for r in results),
        "rate_limit_samples": [s for r in results for s in r.get("rate_limit_samples", [])],
        "results": results,
    }

    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    json_path = RUNS_DIR / f"run_{stamp}.json"
    md_path = RUNS_DIR / f"run_{stamp}.md"
    json_path.write_text(json.dumps(run, indent=2), encoding="utf-8")
    write_digest(run, md_path)

    done = [r for r in results if r["status"] == "done"]
    print()
    print("=" * 62)
    print("SUMMARY")
    print("=" * 62)
    print(f"  Screened            {len(opps)}")
    print(f"  Analysed            {len(done)}  ({len(results) - len(done)} failed)")
    print(f"  Screening time      {human(screen_seconds)}")
    print(f"  Total wall time     {human(total)}")
    if done:
        print(f"  Average per ticker  {human(sum(r['seconds'] for r in done) / len(done))}")
    print(f"  Rate-limit events   {run['total_rate_limits']}")
    print()
    for r in done:
        print(f"  {r['ticker']:<7} {r['decision']}")
    print()
    print(f"  Digest  {md_path}")
    print(f"  Data    {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
