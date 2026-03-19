"""Dual-log engine for model observability.

Two log streams:
  1. Technical Log (logs/technical.jsonl)
     - System events: connections, errors, latencies, API responses
     - For debugging: "give this to Claude and it tells you what went wrong"

  2. Thinking Log (logs/thinking.jsonl)
     - Model reasoning: signal components, why it traded/skipped, confidence
     - For understanding: "see how the model thinks as it analyzes data"

Plus a live state file (logs/state.json) — current model snapshot, overwritten
every tick for the dashboard to read.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path


LOG_DIR = Path("logs")

TECHNICAL_LOG = LOG_DIR / "technical.jsonl"
THINKING_LOG = LOG_DIR / "thinking.jsonl"
STATE_FILE = LOG_DIR / "state.json"
TRADES_LOG = LOG_DIR / "trades.jsonl"


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


@dataclass
class ModelState:
    """Current snapshot of the model's internal state.

    Written to state.json every evaluation cycle.
    The dashboard reads this file for real-time display.
    """

    # Timestamp
    ts: float = 0.0
    uptime_s: int = 0

    # Market Context
    btc_price: Decimal = Decimal("0")
    strike: Decimal = Decimal("0")
    market_question: str = ""
    market_window_start: int = 0
    market_window_end: int = 0
    time_left_s: int = 300

    # Signal Components (raw)
    obi_raw: Decimal = Decimal("0")
    cvd_raw: Decimal = Decimal("0")
    funding_raw: Decimal = Decimal("0")

    # Signal Components (smoothed)
    obi_ema: Decimal = Decimal("0")
    cvd_ema: Decimal = Decimal("0")
    momentum_ema: Decimal = Decimal("0")
    large_trade_bias: Decimal = Decimal("0")

    # Composite
    signal_score: Decimal = Decimal("0")
    signal_direction: str = ""  # YES / NO / FLAT
    signal_threshold: Decimal = Decimal("0")
    signal_passes: bool = False

    # Volatility
    realized_vol: Decimal = Decimal("0")
    vol_regime: str = "NORMAL"  # LOW / NORMAL / HIGH

    # Pricing
    fair_value_yes: Decimal = Decimal("0")
    fair_value_no: Decimal = Decimal("0")
    market_yes: Decimal = Decimal("0")
    market_no: Decimal = Decimal("0")
    edge_yes: Decimal = Decimal("0")
    edge_no: Decimal = Decimal("0")
    price_source: str = ""  # WSS / REST / SYNTH

    # Risk
    balance: Decimal = Decimal("0")
    starting_balance: Decimal = Decimal("0")
    roi_pct: Decimal = Decimal("0")
    drawdown_pct: Decimal = Decimal("0")
    risk_halted: bool = False

    # Performance
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    win_rate: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")

    # Engine
    signals_evaluated: int = 0
    signals_fired: int = 0
    trades_skipped: int = 0
    engine_status: str = "STARTING"


class LogEngine:
    """Dual-log engine for model observability.

    Usage:
        engine = LogEngine()
        engine.technical("connection", {"exchange": "binance", "status": "ok"})
        engine.thinking("evaluation", {...signal data...})
        engine.update_state(state)
        engine.trade({...trade data...})
    """

    def __init__(self, clear_on_start: bool = True) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        if clear_on_start:
            for f in [TECHNICAL_LOG, THINKING_LOG, TRADES_LOG]:
                f.write_text("")
            STATE_FILE.write_text("{}")

        self._start_time = time.time()
        self._write_count = 0

        # Buffer state writes (don't write every tick)
        self._last_state_write = 0.0
        self._state_write_interval = 0.5  # 500ms minimum between state writes

    def technical(self, event: str, data: dict | None = None) -> None:
        """Log a technical/system event.

        These logs are for debugging — errors, latencies, API responses,
        connection events, configuration changes.

        Args:
            event: Event type (e.g., "connection", "api_error", "config_change")
            data: Event-specific data
        """
        entry = {
            "ts": time.time(),
            "uptime": int(time.time() - self._start_time),
            "event": event,
        }
        if data:
            entry["data"] = data
        self._append(TECHNICAL_LOG, entry)

    def thinking(self, phase: str, data: dict) -> None:
        """Log a model reasoning event.

        These logs show HOW the model thinks — signal decomposition,
        why it chose YES vs NO, confidence levels, edge calculations.

        Args:
            phase: Reasoning phase:
                - "observation": raw data ingested
                - "analysis": signal computation + smoothing
                - "evaluation": composite score + threshold check
                - "pricing": fair value vs market price
                - "decision": trade/skip with full reasoning
                - "settlement": prediction outcome
            data: Phase-specific reasoning data
        """
        entry = {
            "ts": time.time(),
            "uptime": int(time.time() - self._start_time),
            "phase": phase,
        }
        entry.update(data)
        self._append(THINKING_LOG, entry)

    def trade(self, data: dict) -> None:
        """Log a trade execution or settlement event.

        Separate from thinking log — this is the trade ledger.
        """
        entry = {
            "ts": time.time(),
        }
        entry.update(data)
        self._append(TRADES_LOG, entry)

    def update_state(self, state: ModelState) -> None:
        """Write current model state to state.json.

        Rate-limited to avoid excessive disk I/O.
        The dashboard polls this file for real-time display.
        """
        now = time.time()
        if now - self._last_state_write < self._state_write_interval:
            return

        state.ts = now
        state.uptime_s = int(now - self._start_time)

        try:
            # Write atomically via temp file
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(state), cls=DecimalEncoder, indent=2))
            tmp.rename(STATE_FILE)
            self._last_state_write = now
        except Exception:
            pass  # Non-critical — dashboard will use stale state

    def _append(self, path: Path, entry: dict) -> None:
        """Append a JSON line to a log file."""
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry, cls=DecimalEncoder) + "\n")
        except Exception:
            pass  # Logging must never crash the strategy
