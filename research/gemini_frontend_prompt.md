# Gemini Task: Build the Dashboard Frontend

You are building the **Streamlit frontend** for a real-time BTC prediction bot dashboard. The backend is already complete — your job is to build the UI that reads from it.

## Design Direction

**Retro terminal / early PC aesthetic.** Think: CRT phosphor green on black, scanline effects, blocky monospace fonts, blinking cursors, ASCII borders. The dashboard should feel like you're watching a quant algo run on an 80s mainframe terminal — but with modern data visualization underneath.

Key visual traits:
- **Primary font:** `IBM Plex Mono` or `JetBrains Mono` (monospace everywhere)
- **Color palette:** Black (`#0a0a0a`) background, phosphor green (`#00ff41`) for positive/active, amber (`#ffb000`) for warnings, red (`#ff073a`) for errors/losses, cyan (`#00e5ff`) for neutral data
- **Borders:** ASCII-style box drawing characters (`┌─┐│└─┘`) or thin `1px solid #333` lines
- **Text effects:** Subtle CRT glow via `text-shadow: 0 0 5px #00ff41`, optional scanline overlay
- **Headers:** ALL CAPS with ASCII art dividers like `══════════════════`
- **No emojis.** Use ASCII symbols: `[OK]`, `[!!]`, `[>>]`, `[--]`, `>>>`, `<<<`
- **Animations:** Blinking cursor `_` next to live values, pulsing glow on active status
- **Charts:** Dark theme Plotly with grid lines in `#1a1a1a`, trace colors matching the palette

## Architecture Overview

The bot runs as a separate process (`python run_paper.py`). It writes data to files in `logs/`. The Streamlit dashboard reads these files. There is **no API server** — it's pure file-based communication.

```
Bot Process                    Dashboard (Streamlit)
─────────                      ────────────────────
V3ProductionStrategy           dashboard.py
    │                              │
    ├──► logs/state.json      ◄────┤  get_model_state()
    ├──► logs/thinking.jsonl  ◄────┤  get_thinking_events()
    ├──► logs/technical.jsonl ◄────┤  get_technical_events()
    ├──► logs/trades.jsonl    ◄────┤  get_trades()
    └──► logs/nautilus.log    ◄────┤  get_system_logs()
```

## Backend API (`src/dashboard_api.py`)

Import with: `from src.dashboard_api import *`

All functions are pure reads with no side effects. Call them on each Streamlit render cycle.

### State Functions

```python
get_model_state() -> dict
```
Returns the current model snapshot (updated every 500ms by the bot). Fields:
- **Timestamp:** `ts`, `uptime_s`
- **Market:** `btc_price`, `strike`, `market_question`, `market_window_start`, `market_window_end`, `time_left_s`
- **Raw Signals:** `obi_raw`, `cvd_raw`, `funding_raw`
- **Smoothed Signals:** `obi_ema`, `cvd_ema`
- **Composite:** `signal_score`, `signal_direction` (YES/NO/FLAT), `signal_threshold`, `signal_passes` (bool)
- **Volatility:** `realized_vol`, `vol_regime` (LOW/NORMAL/HIGH)
- **Pricing:** `fair_value_yes`, `fair_value_no`, `market_yes`, `market_no`, `edge_yes`, `edge_no`, `price_source` (WSS/REST/SYNTH)
- **Risk:** `balance`, `starting_balance`, `roi_pct`, `drawdown_pct`, `risk_halted`
- **Performance:** `total_trades`, `wins`, `losses`, `pending`, `win_rate`, `total_pnl`
- **Engine:** `signals_evaluated`, `signals_fired`, `trades_skipped`, `engine_status` (STARTING/RUNNING/STOPPED)

```python
is_bot_running() -> bool
```
Returns True if the bot wrote to `state.json` within the last 5 seconds.

### Thinking Log Functions

```python
get_thinking_events(limit=200, phase=None) -> list[dict]
get_latest_evaluations(limit=100) -> list[dict]    # phase="evaluation"
get_decisions(limit=50) -> list[dict]               # phase="decision"
get_settlements(limit=50) -> list[dict]             # phase="settlement"
```

**Evaluation events** (logged every tick, ~10-50/sec):
```json
{
  "ts": 1740000000.0,
  "phase": "evaluation",
  "score": 0.1234,
  "obi_raw": 0.45, "obi_ema": 0.12,
  "cvd_raw": 0.30, "cvd_ema": 0.08,
  "funding": -0.02,
  "vol": 0.65, "vol_regime": "NORMAL",
  "threshold": 0.20,
  "direction": "YES", "passes": true,
  "price": 87500.0, "strike": 87400.0,
  "fv_yes": 0.62, "fv_no": 0.38,
  "mkt_yes": 0.55, "mkt_no": 0.48,
  "time_left": 180
}
```

**Decision events** (logged only when signal passes threshold):
```json
{
  "ts": 1740000000.0,
  "phase": "decision",
  "action": "TRADE",        // or "SKIP" or "COOLDOWN"
  "reason": "All gates passed",  // or "Edge +0.0150 < 0.02 minimum"
  "direction": "YES",
  "score": 0.2345,
  "fv": 0.62,
  "target_price": 0.55,
  "edge": 0.07,
  "source": "WSS",
  "obi_ema": 0.15,
  "cvd_ema": 0.10,
  "vol": 0.65,
  "time_left": 180,
  "balance": 9.50,
  "stake_fraction": 0.10
}
```

**Settlement events** (logged when a 5-min window resolves):
```json
{
  "ts": 1740000300.0,
  "phase": "settlement",
  "direction": "YES",
  "predicted": "UP",
  "actual": "DOWN",
  "correct": false,
  "btc_move": -125.50,
  "entry_price": 0.55,
  "pnl": -0.55,
  "new_balance": 8.95,
  "new_roi": -10.5
}
```

### Trade Log Functions

```python
get_trades(limit=100) -> list[dict]
get_open_trades() -> list[dict]       # event == "open"
get_settled_trades() -> list[dict]    # event == "settle"
```

Open trade:
```json
{
  "ts": 1740000000.0,
  "event": "open",
  "direction": "YES",
  "btc_entry": 87500.0,
  "entry_price": 0.55,
  "num_tokens": 1.82,
  "stake": 1.00,
  "edge": 0.07,
  "fv": 0.62,
  "signal_score": 0.2345,
  "balance_after": 9.00
}
```

Settled trade:
```json
{
  "ts": 1740000300.0,
  "event": "settle",
  "direction": "YES",
  "correct": true,
  "btc_entry": 87500.0,
  "btc_exit": 87650.0,
  "entry_price": 0.55,
  "pnl": 0.45,
  "balance_after": 9.45
}
```

### Technical Log Functions

```python
get_technical_events(limit=200, event=None) -> list[dict]
get_system_logs(limit=500, severity="ALL") -> list[str]  # severity: ALL/INFO/WARN/ERROR
```

Technical events have: `ts`, `uptime`, `event` (str), `data` (dict).
System logs are raw NautilusTrader log lines (plain text).

### Computed Metrics

```python
compute_performance() -> PerformanceMetrics
```
Returns a dataclass with: `total_trades`, `wins`, `losses`, `pending`, `win_rate`, `total_pnl`, `avg_pnl_per_trade`, `best_trade`, `worst_trade`, `avg_edge_at_entry`, `avg_win_pnl`, `avg_loss_pnl`, `profit_factor`, `consecutive_wins`, `consecutive_losses`, `max_consecutive_wins`, `max_consecutive_losses`.

### Time Series Data

```python
get_signal_timeseries(limit=300) -> list[dict]
```
Returns: `ts`, `price`, `score`, `obi_ema`, `cvd_ema`, `vol`, `fv_yes`, `mkt_yes`, `edge_yes`, `direction`, `passes`.

```python
get_trade_annotations() -> list[dict]
```
Returns: `ts`, `price`, `direction`, `action`, `edge`, `reason`.

```python
get_balance_history() -> list[dict]
```
Returns: `ts`, `balance`, `event`, `pnl`.

---

## Dashboard Layout Specification

### Page Structure

The dashboard should be a **single-page Streamlit app** at `dashboard.py` (replace the existing one). Use `st.set_page_config(layout="wide")`. Auto-refresh every 2 seconds with `st.rerun()`.

### Section 1: Top Status Bar

A horizontal bar across the top showing critical vitals at a glance.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  DIAMOND v3.0          STATUS: [RUNNING]          UPTIME: 00:45:23        │
│  BTC: $87,542.50       STRIKE: $87,400.00         WINDOW: 2m 34s left    │
│  BALANCE: $9.45        ROI: -5.5%                 PNL: -$0.55            │
└─────────────────────────────────────────────────────────────────────────────┘
```

Use `st.columns()` for horizontal layout. Show `engine_status` with colored indicator (green=RUNNING, red=STOPPED, amber=STARTING). If `is_bot_running()` returns False, show `[OFFLINE]` in red.

### Section 2: Signal Gauges (3 columns)

Three gauges showing the current signal components + composite score.

**Column 1: Signal Components**
Show OBI (raw + EMA), CVD (raw + EMA), Funding as horizontal bar gauges.
- Raw value in dim text, EMA value as the bar fill
- Color: green if positive, red if negative
- Range: [-1.0, +1.0]

**Column 2: Composite Signal**
Large display of the composite score with threshold markers.
```
COMPOSITE SIGNAL
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ◄─── NO ──── 0 ──── YES ────►
              ▲ +0.18
    threshold: ±0.20 (NORMAL)
    direction: FLAT
```

**Column 3: Pricing & Edge**
Show model fair value vs market price, with edge calculation.
```
  MODEL       MARKET      EDGE
  YES: 0.62   YES: 0.55   +0.07
  NO:  0.38   NO:  0.48   -0.10
  Source: WSS
```

### Section 3: Price Chart with Trade Annotations

Plotly chart showing BTC price over time with:
- Blue line: BTC price (from signal timeseries)
- Green triangle markers: YES trades
- Red inverted triangle markers: NO trades
- Horizontal dashed line: current strike price
- Use `get_signal_timeseries()` for price data
- Use `get_trade_annotations()` for markers

### Section 4: Model Thinking Log (the "brain" view)

This is the core observability section. Show the model's reasoning in a scrollable terminal-style window.

Two tabs:
1. **[DECISIONS]** — Show trade decisions with full reasoning
2. **[EVALUATIONS]** — Raw signal evaluations (high frequency)

Format decisions like:
```
[14:32:15] >>> TRADE YES <<<
  Score: +0.2345 | Threshold: 0.12 (HIGH_VOL)
  FV: 0.6200 | Market: 0.5500 | Edge: +0.0700
  OBI_ema: +0.15 | CVD_ema: +0.10 | Vol: 0.85
  Stake: 10.0% ($0.95) | Balance: $9.50
  Reason: All gates passed

[14:31:45] --- SKIP ---
  Score: +0.1800 | Threshold: 0.20 (NORMAL)
  Edge: +0.0150 < 0.02 minimum
  Reason: Edge 0.0150 < 0.02 minimum

[14:31:30] ~~~ COOLDOWN ~~~
  Must wait 15s (cooldown=30s)
```

Color-code: TRADE = green, SKIP = amber, COOLDOWN = dim

### Section 5: Performance Scoreboard

Two columns:

**Left: Stats Table**
```
╔══════════════════════════════╗
║   PERFORMANCE METRICS        ║
╠══════════════════════════════╣
║  Total Trades:     24        ║
║  Win Rate:         58.3%     ║
║  Total PnL:       +$1.45     ║
║  Avg PnL/Trade:   +$0.06    ║
║  Best Trade:      +$0.45     ║
║  Worst Trade:     -$0.55     ║
║  Profit Factor:    1.35      ║
║  Avg Edge@Entry:   4.2%      ║
║  Win Streak:       3 (max 5) ║
║  Loss Streak:      1 (max 3) ║
║  Pending:          2         ║
╚══════════════════════════════╝
```

**Right: Balance Chart**
Plotly line chart of balance over time from `get_balance_history()`.
- Green fill below line if above starting balance
- Red fill below line if below starting balance

### Section 6: Trade History Table

Sortable table of recent trades from `get_settled_trades()`.

Columns: Time | Direction | BTC Entry | BTC Exit | Entry Price | PnL | Correct
- Color rows: green for wins, red for losses
- Show open (pending) trades separately above with a "PENDING" badge

### Section 7: System Logs (collapsible)

Expandable section (`st.expander`) with two tabs:
1. **[TECHNICAL]** — Technical events from `get_technical_events()`
2. **[SYSTEM]** — Raw NautilusTrader logs from `get_system_logs()` with severity filter

Format as scrollable monospace terminal output. Most recent events at top.

---

## Files You Own

| File | Action |
|------|--------|
| `dashboard.py` | **REPLACE** entirely with the new retro-styled dashboard |

**DO NOT** touch any files in `src/` — the backend is complete.

## Commands

```bash
# Run the dashboard
uv run streamlit run dashboard.py

# Run the bot (in a separate terminal)
uv run python run_paper.py

# Format after changes
uv run ruff format .
```

## Important Notes

1. The dashboard must work even when the bot is offline — show "OFFLINE" status and empty/placeholder data gracefully
2. Use `time.sleep(2); st.rerun()` at the bottom for auto-refresh
3. All imports from the backend: `from src.dashboard_api import *`
4. The existing `dashboard.py` reads from `logs/signals.jsonl` (old format). Your new version should read from the new log files via the dashboard_api functions
5. Test that the dashboard renders without errors even with empty log files
6. Keep it fast — the `get_*` functions are already optimized, don't add unnecessary processing
