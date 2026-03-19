# Project Diamond: 5-Min BTC Prediction Bot (NautilusTrader)

## Overview
Project Diamond is a high-frequency automated trading system designed for **5-Minute High-Frequency Prediction** on Polymarket, utilizing leading indicators from Binance Futures. The system is built on **NautilusTrader**, bridging Python-based strategy logic with a high-performance Rust execution core.

## Stack
- **Framework:** NautilusTrader (Python/Rust)
- **Data Source:** Binance Futures WebSocket (Funding Rates, OBI, CVD)
- **Execution Venue:** Polymarket CLOB (Polygon Network)
- **Oracle:** Chainlink Data Streams (Near-instant settlement)
- **Infrastructure:** Private Alchemy/QuickNode RPCs, FastLane Atlas (MEV protection)
- **Research:** `prediction-market-analysis` framework (36GB Polymarket + Kalshi trade history, 29 analyses via DuckDB)

## Key Features
- **Predictive Pricing:** Utilizes Black-Scholes $P(S_T > K) = N(d_2)$ to determine raw fair value probabilities.
- **Empirical Calibration:** `src/market_calibration.py` corrects raw B-S output using real Polymarket trade data — longshot bias correction, hourly signal scaling, and EV-based side selection.
- **Oracle-Lag Arbitrage Protection:** Real-time monitoring of Chainlink Data Streams to avoid toxic flow.
- **Dynamic Fee Modeling:** Accounts for the 2026 Polymarket taker fee schedule (0.44% - 3.15%) in real-time.
- **Crash-Only Design:** Prioritizes data integrity; the bot panics on invalid states and recovers from logs/state.

## Phase 4: Live Alpha (Infrastructure Hardening)
The current focus is on execution fidelity and latency-sensitive safety triggers.
- **Staleness Panic:** The system will immediately halt (Panic) if the Binance-to-Engine signal latency exceeds **500ms**. This prevents trading on stale order book states.
- **No-Trade Zone:** To mitigate high-volatility slippage and settlement ambiguity, the system enters a hard "No-Trade Zone" in the **final 30 seconds** of every 5-minute market window.
- **Latency Monitoring:** Real-time nanosecond-precision tracking of signal-to-order deltas.

## Performance Metrics
### Latency Benchmarks (Phase 4)
| Test Cycle | Signal-to-Engine (P50) | Signal-to-Engine (P99) | Max Latency | Outcome |
| :--- | :--- | :--- | :--- | :--- |
| Baseline (V4-dev) | - | - | - | Pending |
| Post-Remediation | - | - | - | Pending |

## Directory Structure
- `src/`: Core strategy and execution logic (current development).
- `src/market_calibration.py`: Empirical calibration components derived from prediction-market-analysis.
- `research/prediction_market_analysis/`: Prediction market research framework (29 analyses, Polymarket + Kalshi).
- `research/`: Strategy evolution notes and performance reports.
- `versions/v3_stable/`: Stable snapshot of the 15m/reactive version.
- `versions/v4_dev/`: Development environment for FastLane Atlas integration and 2026 optimizations.
- `history/`: Archival of previous research, plans, and 2025-era logs.

## Getting Started
1. **Setup Env:** Ensure Python 3.12+ and `uv` are installed.
2. **Install Deps:** `uv sync`
3. **Type Check:** `pyright .`
4. **Run Backtest:** `python run_backtest.py`
5. **Live Deployment:** `python run_paper.py` (Paper mode) or `python run.py` (Live).

## 2026 Fee Schedule Disclaimer
All execution logic must account for the ~3.15% taker fee barrier at 50% probability. "Edge" is defined as $|Fair Value - Market Price| > 	ext{Fee}$.
