"""Dashboard data backend.

Reads from the log engine's output files and provides structured data
for the Streamlit frontend. All methods are pure reads — no side effects.

Data sources:
  logs/state.json      — current model state (overwritten every 500ms)
  logs/thinking.jsonl   — model reasoning events
  logs/technical.jsonl   — system/debug events
  logs/trades.jsonl      — trade ledger (opens + settlements)
  logs/nautilus.log      — NautilusTrader system log

The frontend calls these functions on each render cycle (~1-2s).
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


LOG_DIR = Path("logs")

STATE_FILE = LOG_DIR / "state.json"
THINKING_LOG = LOG_DIR / "thinking.jsonl"
TECHNICAL_LOG = LOG_DIR / "technical.jsonl"
TRADES_LOG = LOG_DIR / "trades.jsonl"
NAUTILUS_LOG = LOG_DIR / "nautilus.log"


# ── State ────────────────────────────────────────────────────────


def get_model_state() -> dict:
    """Read the latest model state snapshot.

    Returns a dict with all fields from ModelState, or empty dict if
    the bot isn't running yet.
    """
    try:
        if STATE_FILE.exists() and STATE_FILE.stat().st_size > 2:
            return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def is_bot_running() -> bool:
    """Check if the bot is actively writing state."""
    try:
        if STATE_FILE.exists():
            age = time.time() - STATE_FILE.stat().st_mtime
            return age < 5.0  # state file updated within 5 seconds
    except OSError:
        pass
    return False


# ── Thinking Log ─────────────────────────────────────────────────


def get_thinking_events(
    limit: int = 200,
    phase: str | None = None,
) -> list[dict]:
    """Read recent model thinking events.

    Args:
        limit: Maximum events to return (most recent first).
        phase: Filter by phase (evaluation, decision, settlement, etc.)
    """
    events = _read_jsonl(THINKING_LOG, limit * 3)  # read more, then filter
    if phase:
        events = [e for e in events if e.get("phase") == phase]
    return events[-limit:]


def get_latest_evaluations(limit: int = 100) -> list[dict]:
    """Get recent signal evaluations (for charting)."""
    return get_thinking_events(limit=limit, phase="evaluation")


def get_decisions(limit: int = 50) -> list[dict]:
    """Get recent trade decisions (TRADE / SKIP / COOLDOWN)."""
    return get_thinking_events(limit=limit, phase="decision")


def get_settlements(limit: int = 50) -> list[dict]:
    """Get recent settlement outcomes."""
    return get_thinking_events(limit=limit, phase="settlement")


# ── Trade Log ────────────────────────────────────────────────────


def get_trades(limit: int = 100) -> list[dict]:
    """Read the trade ledger (opens and settlements)."""
    return _read_jsonl(TRADES_LOG, limit)


def get_open_trades() -> list[dict]:
    """Get currently open (unsettled) trades."""
    trades = _read_jsonl(TRADES_LOG, 500)
    return [t for t in trades if t.get("event") == "open"]


def get_settled_trades() -> list[dict]:
    """Get settled trades with PnL."""
    trades = _read_jsonl(TRADES_LOG, 500)
    return [t for t in trades if t.get("event") == "settle"]


# ── Technical Log ────────────────────────────────────────────────


def get_technical_events(limit: int = 200, event: str | None = None) -> list[dict]:
    """Read recent technical/system events.

    Args:
        limit: Maximum events to return.
        event: Filter by event type (e.g., "api_error", "connection").
    """
    events = _read_jsonl(TECHNICAL_LOG, limit * 2)
    if event:
        events = [e for e in events if e.get("event") == event]
    return events[-limit:]


# ── System Log ───────────────────────────────────────────────────


def get_system_logs(
    limit: int = 500,
    severity: str = "ALL",
) -> list[str]:
    """Read NautilusTrader system log lines.

    Args:
        limit: Maximum lines to return (most recent).
        severity: Filter by severity (ALL, INFO, WARN, ERROR).
    """
    if not NAUTILUS_LOG.exists():
        return []

    try:
        with open(NAUTILUS_LOG, "r") as f:
            lines = f.readlines()
    except OSError:
        return []

    if severity != "ALL":
        lines = [l for l in lines if f"[{severity}]" in l]

    return lines[-limit:]


# ── Computed Metrics ─────────────────────────────────────────────


@dataclass
class PerformanceMetrics:
    """Computed performance stats for the dashboard."""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    win_rate: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    avg_pnl_per_trade: Decimal = Decimal("0")
    best_trade: Decimal = Decimal("0")
    worst_trade: Decimal = Decimal("0")
    avg_edge_at_entry: Decimal = Decimal("0")
    avg_win_pnl: Decimal = Decimal("0")
    avg_loss_pnl: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


def compute_performance() -> PerformanceMetrics:
    """Compute performance metrics from the trade ledger."""
    m = PerformanceMetrics()

    settled = get_settled_trades()
    opens = get_open_trades()
    m.pending = len(opens)

    if not settled:
        return m

    pnls = [Decimal(str(t.get("pnl", 0))) for t in settled]
    all_trades = _read_jsonl(TRADES_LOG, 500)
    edges = [
        Decimal(str(o.get("edge", 0)))
        for o in all_trades if o.get("event") == "open"
    ]

    m.total_trades = len(settled)
    m.wins = sum(1 for t in settled if t.get("correct", False))
    m.losses = m.total_trades - m.wins
    m.win_rate = (Decimal(str(m.wins)) / Decimal(str(m.total_trades)) * Decimal("100")) if m.total_trades > 0 else Decimal("0")
    m.total_pnl = sum(pnls)
    m.avg_pnl_per_trade = m.total_pnl / Decimal(str(m.total_trades)) if m.total_trades > 0 else Decimal("0")
    m.best_trade = max(pnls) if pnls else Decimal("0")
    m.worst_trade = min(pnls) if pnls else Decimal("0")
    m.avg_edge_at_entry = sum(edges) / Decimal(str(len(edges))) if edges else Decimal("0")

    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    m.avg_win_pnl = sum(win_pnls) / Decimal(str(len(win_pnls))) if win_pnls else Decimal("0")
    m.avg_loss_pnl = sum(loss_pnls) / Decimal(str(len(loss_pnls))) if loss_pnls else Decimal("0")

    total_wins_amt = sum(win_pnls) if win_pnls else Decimal("0")
    total_loss_amt = abs(sum(loss_pnls)) if loss_pnls else Decimal("0")
    if total_loss_amt > 0:
        m.profit_factor = total_wins_amt / total_loss_amt
    else:
        m.profit_factor = Decimal("100") if total_wins_amt > 0 else Decimal("0")

    # Consecutive streaks
    streak = 0
    for t in settled:
        if t.get("correct", False):
            if streak > 0:
                streak += 1
            else:
                streak = 1
            m.max_consecutive_wins = max(m.max_consecutive_wins, streak)
        else:
            if streak < 0:
                streak -= 1
            else:
                streak = -1
            m.max_consecutive_losses = max(m.max_consecutive_losses, abs(streak))

    if streak > 0:
        m.consecutive_wins = streak
    elif streak < 0:
        m.consecutive_losses = abs(streak)

    return m


# ── Signal Time Series ──────────────────────────────────────────


def get_signal_timeseries(limit: int = 300) -> list[dict]:
    """Get evaluation data formatted for time-series charting.

    Returns list of dicts with: ts, price, score, obi_ema, cvd_ema,
    vol, fv_yes, mkt_yes, edge_yes, direction.
    """
    evals = get_latest_evaluations(limit=limit)
    series = []
    for e in evals:
        series.append(
            {
                "ts": e.get("ts", 0),
                "price": e.get("price", 0),
                "score": e.get("score", 0),
                "obi_ema": e.get("obi_ema", 0),
                "cvd_ema": e.get("cvd_ema", 0),
                "vol": e.get("vol", 0),
                "fv_yes": e.get("fv_yes", 0),
                "mkt_yes": e.get("mkt_yes"),
                "edge_yes": (e.get("fv_yes", 0.5) - (e.get("mkt_yes") or 0.5)),
                "direction": e.get("direction", "FLAT"),
                "passes": e.get("passes", False),
            }
        )
    return series


def get_trade_annotations() -> list[dict]:
    """Get trade events formatted for chart annotations.

    Returns list of dicts with: ts, price, direction, action, edge.
    """
    decisions = get_decisions(limit=100)
    annotations = []
    for d in decisions:
        if d.get("action") in ("TRADE", "SKIP"):
            annotations.append(
                {
                    "ts": d.get("ts", 0),
                    "price": d.get("price", 0) or d.get("target_price", 0),
                    "direction": d.get("direction", ""),
                    "action": d.get("action", ""),
                    "edge": d.get("edge", 0),
                    "reason": d.get("reason", ""),
                }
            )
    return annotations


# ── Balance History ──────────────────────────────────────────────


def get_balance_history() -> list[dict]:
    """Get balance over time from trade settlements."""
    trades = _read_jsonl(TRADES_LOG, 500)
    history = []
    for t in trades:
        if "balance_after" in t:
            history.append(
                {
                    "ts": t.get("ts", 0),
                    "balance": t.get("balance_after", 0),
                    "event": t.get("event", ""),
                    "pnl": t.get("pnl", 0),
                }
            )
    return history


# ── Internal Helpers ─────────────────────────────────────────────


def _read_jsonl(path: Path, limit: int) -> list[dict]:
    """Read last N lines of a JSONL file."""
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        result = []
        for line in lines[-limit:]:
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return result
    except OSError:
        return []
