# Diamond

Algorithmic BTC/USDT trading bot — generating directional signals from Binance Futures order flow and executing on Polymarket.

## Overview

Diamond is a high-frequency trading strategy built on [NautilusTrader](https://nautilustrader.io/). It consumes real-time order book and trade data from Binance Futures, computes a composite directional signal, applies Black-Scholes fair-value pricing with empirical market calibration, and evaluates edge against Polymarket's live probabilities before placing orders.

## Signal Framework

The system generates signals from four Binance Futures data streams:

| Indicator | Weight | Description |
|-----------|--------|-------------|
| **OBI** (Order Book Imbalance) | 60% | `(bid_vol − ask_vol) / (bid_vol + ask_vol)` from L2 depth |
| **CVD** (Cumulative Volume Delta) | 30% | Buyer-initiated vs seller-initiated volume over a 60s rolling window |
| **Funding Rate** | 10% | Context bias from Binance's PERP funding cycle |
| **Realized Volatility** | — | 10-bar rolling annualized vol; determines signal threshold |

A composite score is computed as a weighted sum, then adjusted by UTC-hour signal-quality multipliers derived from historical trade analysis. A trade is triggered only when the adjusted score clears the volatility-regime threshold.

## Pricing Engine

Fair value is computed in two steps:

1. **Black-Scholes binary option pricing** — $P(S_T > K) = N(d_2)$ converts implied volatility into a probability estimate
2. **Empirical calibration correction** — the raw BS output is adjusted using a calibration curve fitted against historical Polymarket trade data (correcting for systematic longshot bias at prices <15% or >85%)

A trade is only placed when the calibration-adjusted edge exceeds both the dynamic taker-fee barrier and a longshot buffer in extreme-price zones.

## Risk Management

- **Staleness panic** — halts all trading if Binance-to-engine latency exceeds 500ms
- **No-trade zone** — blocks new orders in the final 30 seconds of each 5-minute market window, protecting against oracle-lag risk at market close
- **Max drawdown stop** — hard daily loss limit enforced by the risk manager
- **Heartbeat monitoring** — cancels all open Polymarket orders if the Binance WebSocket feed is lost for more than 3 seconds

## Architecture

```
src/
├── config.py              — Instrument IDs, thresholds, log paths
├── strategy.py            — SignalGenerationStrategy (main strategy class)
├── v3_strategy.py         — Stable v3 variant
├── pricing.py             — fair_value_yes/no, taker_adjusted_edge
├── market_calibration.py  — CalibrationAdjuster, LongshotBiasFilter, HourlySignalFilter, EVCalculator
├── risk.py                — RiskManager (Kelly sizing, drawdown, heartbeat)
├── execution.py           — PolymarketExecutor (order management)
├── polymarket_feed.py     — Polymarket REST + WebSocket client
├── polymarket_wss.py      — Low-latency market data handler
├── dashboard_api.py       — Live dashboard backend
└── log_engine.py          — Structured logging (LatencyLogger, TickToTradeLogger)
```

## Getting Started

```bash
# Install
uv sync

# Type check
pyright .

# Run backtest
python run_backtest.py

# Paper trade
python run_paper.py
```

Requires Python 3.12+.

## Stack

- **Framework:** NautilusTrader 1.222+ (Python/Rust)
- **Data:** Binance Futures WebSocket (real-time)
- **Execution:** Polymarket CLOB
- **Oracle:** Chainlink Data Streams
- **Language:** Python 3.12+

## License

MIT
