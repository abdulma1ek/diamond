# Project: NautilusTrader 5-Min BTC Prediction Bot (Polymarket)

## 1. Core Architecture & Philosophy
- **Framework:** NautilusTrader (Python frontend, Rust backend).
- **Concurrency:** Actor-based. NEVER use `threading` or blocking `sleep`. Rely on the Nautilus event loop.
- **Fail-Fast:** The system follows a "crash-only" design. If an invalid type is passed to the Rust core, it will panic. Validate inputs immediately.
- **Strategy:** 5-Minute High-Frequency Prediction.
    - **Signal:** Binance Futures Funding Rates, Order Book Imbalance, Cumulative Volume Delta, Realized Volatility.
    - **Execution:** Polymarket CTF (Conditional Token Framework) via Polygon Network.

## 2. Critical Coding Rules (NautilusTrader)
**Violating these rules causes immediate backend crashes.**
- **NO FLOATS FOR MONEY:** Never use raw Python `float` for prices or quantities.
    - ❌ Bad: `price = 95000.50`
    - ✅ Good: `price = Price.from_str("95000.50")` or `qty = instrument.make_qty(0.1)`
- **Type Hinting:** All method arguments and return types must be strictly typed to aid Cython compilation.
- **Config Pattern:** Use `TradingNodeConfig` objects, not dictionaries.
    - Use: `BinanceDataClientConfig`, `PolymarketLiveExecClientFactory`.
- **Symbology:**
    - Binance Perps: `InstrumentId.from_str("BTCUSDT-PERP.BINANCE")`
    - Polymarket: Uses CTF IDs. Ensure markets are loaded via `PolymarketInstrumentProvider`.

## 3. 5-Minute Prediction Specifics
- **Timeframe:** 5-minute resolution.
- **Math & Probability:**
    - Treat Polymarket prices (0.00-1.00) as probabilities.
    - **Pricing Model:** Use **Binary Option Black-Scholes** (not standard BS).
    - **Conversion:** $P(S_T > K) = N(d_2)$ (Fair Value probability).
- **Data Handling:**
    - `OrderBookDepth10` for L2 order book snapshots (OBI signal).
    - `TradeTick` for trade flow (CVD signal).
    - 1-minute `Bar` for realized volatility.
    - `FundingRateUpdate` via `subscribe_funding_rates()` for background context.
    - **Oracle:** Resolution now uses **Chainlink Data Streams** (near-instant settlement).

## 4. Bot Skills (2026 Update)
- **Oracle-Lag Arbitrage Protection:** Real-time monitoring of Chainlink Data Streams to prevent being picked off by oracle updates.
- **Dynamic Fee Modeling:** Real-time calculation of the taker fee barrier based on the current probability curve (0.44% - 3.15%).
- **Empirical Market Calibration:** `src/market_calibration.py` applies research-backed corrections to raw B-S output:
    - `CalibrationAdjuster`: Blends model FV with empirical win-rate curve (longshot bias correction, α=0.3).
    - `EVCalculator`: Selects the higher-EV side (YES vs NO) explicitly before every trade.
    - `HourlySignalFilter`: Scales signal scores by UTC-hour liquidity multiplier (0.60× – 1.12×).
    - `LongshotBiasFilter`: Requires 1.5× edge in <15% / >85% price zones.
    - **Source data:** `research/prediction_market_analysis/` (36GB Polymarket + Kalshi trade history, 29 analyses).

## 5. Preferred Workflow
1. **Plan Mode:** For complex logic changes, generate a plan first.
2. **Three-File Structure:**
    - `strategy.py`: Logic and signal generation.
    - `config.py`: Component configuration and wiring.
    - `run.py`: Entry point for `TradingNode`.
3. **Verification:** Always create a backtest using `BacktestEngine` before live deployment. Live code and backtest code MUST be identical.

## 6. Common Commands
- **Run Strategy:** `python run.py`
- **Type Check:** `pyright .` (Critical for Nautilus types)
- **Format:** `ruff format .`
- **Clean:** `rm -rf .backtest_cache/` (Clear cache if data looks stale)

## 7. External Integrations (MCP & APIs)
- **Polymarket:** Use `py-clob-client` patterns for order book fetching if not using native Nautilus adapter.
- **RPC:** We use private Alchemy/QuickNode endpoints to handle the post-delay speed war.
- **FastLane Atlas:** Use bundles for MEV-protected transaction submission.
