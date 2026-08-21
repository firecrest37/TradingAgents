"""
TradingAgents MCP server
------------------------
Exposes the TradingAgents multi-agent pipeline as tools that Claude Desktop
can call, so a full analysis can be started and read from a chat window.

A run takes minutes, which is far longer than a tool call should block. So
runs happen on a background thread: start_analysis() returns immediately with
a job id, and check_analysis() reports progress until it is done.

Place this file in the TradingAgents folder, next to pyproject.toml.
"""

import json
import os
import sys
import threading
import traceback
import contextlib
import logging
from datetime import datetime, date as _date
from pathlib import Path

# --- stdout hygiene -------------------------------------------------------
# MCP speaks JSON-RPC over stdout. Anything else printed there corrupts the
# protocol and Claude Desktop will show the server as failed. The underlying
# libraries are chatty, so every log stream is pinned to stderr up front.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
for noisy in ("httpx", "langchain", "langgraph", "yfinance", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv                      # noqa: E402
load_dotenv(HERE / ".env")

# The SDK renamed this class in 2.0 (FastMCP -> MCPServer). Support both so
# the server works whichever version pip resolves.
try:                                                    # noqa: E402
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                                     # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

mcp = _Server("tradingagents")

# --- job bookkeeping ------------------------------------------------------
STATE_DIR = Path(os.path.expanduser("~")) / ".tradingagents" / "mcp"
STATE_DIR.mkdir(parents=True, exist_ok=True)
JOBS_FILE = STATE_DIR / "jobs.json"

_lock = threading.Lock()
_run_slot = threading.Semaphore(1)   # one analysis at a time; they are rate-limit heavy


def _load_jobs() -> dict:
    if not JOBS_FILE.exists():
        return {}
    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_jobs(jobs: dict) -> None:
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    tmp.replace(JOBS_FILE)


def _update(job_id: str, **fields) -> None:
    with _lock:
        jobs = _load_jobs()
        jobs.setdefault(job_id, {}).update(fields)
        _save_jobs(jobs)


def _get(job_id: str) -> dict | None:
    return _load_jobs().get(job_id)


def _latest() -> tuple[str, dict] | tuple[None, None]:
    jobs = _load_jobs()
    if not jobs:
        return None, None
    jid = max(jobs, key=lambda k: jobs[k].get("started", ""))
    return jid, jobs[jid]


def _resolve(job_id: str | None) -> tuple[str | None, dict | None, str | None]:
    """Return (job_id, job, error_message)."""
    if job_id:
        job = _get(job_id)
        if job is None:
            return None, None, f"No analysis with id '{job_id}'. Use list_analyses to see valid ids."
        return job_id, job, None
    jid, job = _latest()
    if jid is None:
        return None, None, "No analyses have been run yet. Use start_analysis to begin one."
    return jid, job, None


# --- the actual run -------------------------------------------------------
def _run(job_id: str, ticker: str, trade_date: str, depth: str, analysts: list[str]) -> None:
    with _run_slot:
        try:
            _update(job_id, status="running", stage="starting up")

            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            config = DEFAULT_CONFIG.copy()
            rounds = {"light": 1, "medium": 2, "deep": 3}.get(depth, 1)
            config["max_debate_rounds"] = rounds
            config["max_risk_discuss_rounds"] = rounds

            _update(job_id, stage="running the agent pipeline")

            # Any stray print() inside the framework goes to stderr, not stdout.
            with contextlib.redirect_stdout(sys.stderr):
                graph = TradingAgentsGraph(
                    selected_analysts=tuple(analysts),
                    debug=False,
                    config=config,
                )
                final_state, decision = graph.propagate(ticker, trade_date)
                report_path = graph.save_reports(final_state, ticker)

            pm = ""
            risk = final_state.get("risk_debate_state") or {}
            if isinstance(risk, dict):
                pm = risk.get("judge_decision") or ""

            _update(
                job_id,
                status="done",
                stage="complete",
                finished=datetime.now().isoformat(timespec="seconds"),
                decision=str(decision),
                portfolio_manager=pm,
                report_path=str(report_path),
            )
        except Exception as exc:
            _update(
                job_id,
                status="failed",
                stage="error",
                finished=datetime.now().isoformat(timespec="seconds"),
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc()[-3000:],
            )


# --- tools ----------------------------------------------------------------
VALID_ANALYSTS = ("market", "social", "news", "fundamentals")


@mcp.tool()
def start_analysis(
    ticker: str,
    trade_date: str = "",
    depth: str = "light",
    analysts: str = "all",
) -> str:
    """Start a full multi-agent trading analysis on a ticker.

    Runs in the background and returns immediately with a job id, because a
    complete run takes several minutes. Poll check_analysis for progress.

    ticker:     Symbol, e.g. AAPL, NVDA, SPY, BTC-USD, 0700.HK
    trade_date: YYYY-MM-DD. Defaults to today.
    depth:      'light' (1 debate round, fastest and cheapest), 'medium' (2),
                or 'deep' (3 rounds, most thorough, most API calls).
    analysts:   'all', or a comma-separated subset of:
                market, social, news, fundamentals
    """
    ticker = ticker.strip().upper()
    if not ticker:
        return "Error: no ticker given."

    if not trade_date:
        trade_date = _date.today().isoformat()
    else:
        try:
            datetime.strptime(trade_date, "%Y-%m-%d")
        except ValueError:
            return f"Error: trade_date must look like 2026-08-21, got '{trade_date}'."

    depth = depth.strip().lower()
    if depth not in ("light", "medium", "deep"):
        return f"Error: depth must be light, medium, or deep — got '{depth}'."

    if analysts.strip().lower() == "all":
        chosen = list(VALID_ANALYSTS)
    else:
        chosen = [a.strip().lower() for a in analysts.split(",") if a.strip()]
        bad = [a for a in chosen if a not in VALID_ANALYSTS]
        if bad:
            return f"Error: unknown analysts {bad}. Valid: {', '.join(VALID_ANALYSTS)}."
        if not chosen:
            return "Error: no analysts selected."

    if not os.environ.get("GOOGLE_API_KEY"):
        return ("Error: GOOGLE_API_KEY is not set. Check that the .env file sits "
                "next to mcp_server.py and contains your key.")

    job_id = f"{ticker}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    _update(
        job_id,
        status="queued",
        stage="queued",
        ticker=ticker,
        trade_date=trade_date,
        depth=depth,
        analysts=chosen,
        started=datetime.now().isoformat(timespec="seconds"),
    )

    threading.Thread(
        target=_run, args=(job_id, ticker, trade_date, depth, chosen), daemon=True
    ).start()

    mins = {"light": "3-6", "medium": "6-12", "deep": "12-25"}[depth]
    return (
        f"Started analysis of {ticker} for {trade_date} at {depth} depth.\n"
        f"Job id: {job_id}\n"
        f"Analysts: {', '.join(chosen)}\n\n"
        f"Expect roughly {mins} minutes. Call check_analysis to see whether it has finished."
    )


@mcp.tool()
def check_analysis(job_id: str = "") -> str:
    """Check whether an analysis has finished. Defaults to the most recent one."""
    jid, job, err = _resolve(job_id or None)
    if err:
        return err

    status = job.get("status", "unknown")
    head = f"{jid} — {job.get('ticker')} on {job.get('trade_date')} ({job.get('depth')} depth)"

    if status in ("queued", "running"):
        started = job.get("started", "")
        elapsed = ""
        try:
            secs = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
            elapsed = f", {int(secs // 60)}m {int(secs % 60)}s elapsed"
        except (ValueError, TypeError):
            pass
        return f"{head}\nStatus: {status} — {job.get('stage', '')}{elapsed}\nStill working. Check again shortly."

    if status == "failed":
        return (f"{head}\nStatus: FAILED\n\n{job.get('error', 'unknown error')}\n\n"
                f"Details:\n{job.get('traceback', '')}")

    return (f"{head}\nStatus: complete\n\n"
            f"DECISION: {job.get('decision', '')}\n\n"
            f"Use get_decision for the portfolio manager's reasoning, or "
            f"get_report for the full analyst write-ups.")


@mcp.tool()
def get_decision(job_id: str = "") -> str:
    """Get the final decision and the portfolio manager's reasoning for a finished analysis."""
    jid, job, err = _resolve(job_id or None)
    if err:
        return err
    if job.get("status") != "done":
        return f"{jid} is {job.get('status')}, not finished. Use check_analysis."

    return (
        f"# {job.get('ticker')} — {job.get('trade_date')}\n\n"
        f"**Decision:** {job.get('decision', '')}\n\n"
        f"## Portfolio Manager\n\n{job.get('portfolio_manager') or '(none recorded)'}"
    )


@mcp.tool()
def get_report(job_id: str = "", section: str = "all") -> str:
    """Read the written reports from a finished analysis.

    section: 'all' for the consolidated report, or one of
             market, sentiment, news, fundamentals, bull, bear,
             manager, trader, aggressive, conservative, neutral, decision
    """
    jid, job, err = _resolve(job_id or None)
    if err:
        return err
    if job.get("status") != "done":
        return f"{jid} is {job.get('status')}, not finished. Use check_analysis."

    complete = Path(job.get("report_path", ""))
    if not complete.exists():
        return f"Report file is missing from disk: {complete}"
    root = complete.parent

    section = section.strip().lower()
    if section == "all":
        target = complete
    else:
        mapping = {
            "market": "1_analysts/market.md",
            "sentiment": "1_analysts/sentiment.md",
            "social": "1_analysts/sentiment.md",
            "news": "1_analysts/news.md",
            "fundamentals": "1_analysts/fundamentals.md",
            "bull": "2_research/bull.md",
            "bear": "2_research/bear.md",
            "manager": "2_research/manager.md",
            "trader": "3_trading/trader.md",
            "aggressive": "4_risk/aggressive.md",
            "conservative": "4_risk/conservative.md",
            "neutral": "4_risk/neutral.md",
            "decision": "5_portfolio/decision.md",
        }
        rel = mapping.get(section)
        if rel is None:
            return f"Unknown section '{section}'. Valid: all, {', '.join(sorted(mapping))}."
        target = root / rel
        if not target.exists():
            return f"That section was not produced for this run (looked for {rel})."

    text = target.read_text(encoding="utf-8")
    if len(text) > 60000:
        text = text[:60000] + "\n\n[...truncated. Ask for a single section instead of 'all'.]"
    return text


@mcp.tool()
def list_analyses(limit: int = 10) -> str:
    """List recent analyses, newest first, with their ids and status."""
    jobs = _load_jobs()
    if not jobs:
        return "No analyses yet."
    ordered = sorted(jobs.items(), key=lambda kv: kv[1].get("started", ""), reverse=True)[:limit]
    lines = []
    for jid, j in ordered:
        bit = f"  {j.get('decision', '')[:80]}" if j.get("status") == "done" else ""
        lines.append(f"{jid:28}  {j.get('status', '?'):8}  {j.get('depth', ''):6}{bit}")
    return "Recent analyses (newest first):\n\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
