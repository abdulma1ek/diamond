# Diamond

Algorithmic BTC/USDT trading bot built on [NautilusTrader](https://nautilustrader.io/).

## Strategy

5-minute prediction model using Binance Futures funding rates, order book imbalance (OBI), and cumulative volume delta (CVD) as leading indicators. Pairs with live Polymarket execution for non-directional signal confirmation.

## Architecture

```
src/
├── config.py           — Market and risk parameters
├── strategy.py         — Core signal generation logic
├── v3_strategy.py      — Stable variant
├── pricing.py          — Black-Scholes fair value + calibration
├── market_calibration.py — Empirical corrections from market data
├── risk.py             — Position sizing and circuit breakers
├── execution.py        — Order management and fee modeling
├── polymarket_feed.py   — Polymarket WebSocket client
├── polymarket_wss.py   — Low-latency market data handler
├── dashboard_api.py    — Live dashboard backend
└── log_engine.py       — Structured logging
```

## Getting Started

```bash
# Install dependencies
uv sync

# Run type checks
pyright .

# Run backtest
python run_backtest.py

# Paper trade
python run_paper.py
```

## Stack

- **Framework:** NautilusTrader 1.222+
- **Data:** Binance Futures WebSocket (real-time)
- **Execution:** Polymarket CLOB
- **Oracle:** Chainlink Data Streams
- **Language:** Python 3.12+

## License

MIT
