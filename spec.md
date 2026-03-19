# Specification: 5-Minute High-Frequency Prediction Bot (Polymarket)

## 1. Project Objective
Build a high-frequency automated trading system using **NautilusTrader** to execute a **5-Minute High-Frequency Prediction** strategy on Bitcoin price movements. The system arbitrages the lag between leading indicators on Binance Futures and the implied probabilities on Polymarket's CLOB.

## 2. Core Architecture
- **Framework:** NautilusTrader (Python strategy layer, Rust execution core).
- **Concurrency Model:** Actor-based asynchronous message passing (Nautilus `MessageBus`).
- **Network:** Polygon PoS (Execution) and Binance Futures (Signal).
- **Infrastructure:** Co-located VPS (Tokyo/NY4) with private Polygon RPC (Alchemy/QuickNode) and **FastLane Atlas** for MEV protection.

## 3. Data Ingestion & Signal Logic
### A. Leading Indicators (Binance Futures)
We subscribe to the `BTCUSDT-PERP.BINANCE` instrument.
1.  **Order Book Imbalance (OBI):** `(bid_vol - ask_vol) / (bid_vol + ask_vol)`.
2.  **Cumulative Volume Delta (CVD):** Tracks buyer-initiated vs. seller-initiated volume (60s window).
3.  **Realized Volatility:** 1-minute `Bar` closes (10-bar window).
4.  **Funding Rate:** Retained as a background bias with 15% weight.

### B. Execution Venue (Polymarket)
We trade Conditional Tokens (CTF) via the Polymarket CLOB.
*   **Market Resolution:** **Chainlink Data Streams** for near-instant settlement.
*   **Settlement:** Automatic at the close of each 5-minute interval. The bot no longer accounts for the old UMA 2-hour liveness window.
*   **Fees:** 2026 taker fee schedule (~3.15% at 50% probability) must be modeled dynamically.

## 4. Pricing Engine (The Math)
We convert standard volatility into binary option probabilities to determine "Fair Value," then apply empirical calibration corrections from real market data.

### Step 1: Raw Fair Value — Binary Call Option (Cash-or-Nothing)
$$ P(S_T > K) = N(d_2) $$
$$ FairValue_{Yes} = e^{-rT} \times N(d_2) $$

Where:
*   $S$: Current Binance Index Price.
*   $K$: Polymarket Strike Price.
*   $T$: Time to expiration (in years).
*   $r$: Risk-free rate.
*   $\sigma$: Implied Volatility.
*   $d_2$: $\frac{\ln(S/K) + (r - \frac{\sigma^2}{2})T}{\sigma\sqrt{T}}$

### Step 2: Empirical Calibration Correction (`src/market_calibration.py`)
Raw B-S output is adjusted using the empirical calibration curve derived from the `prediction-market-analysis` framework (36GB Polymarket trade history):

$$ FV_{adjusted} = FV_{raw} + \alpha \times \text{bias}(P_{market}) $$

Where $\text{bias}(P) = \text{empirical\_win\_rate}(P) - P$ and $\alpha = 0.3$.

**Longshot Bias:** Prices < 15% or > 85% are systematically overpriced in prediction markets. The `LongshotBiasFilter` raises the minimum required edge by 1.5× in these zones.

## 5. Execution Strategy
*   **Order Type:** `Limit` orders only.
*   **MEV Protection:** Use **FastLane Atlas Bundles** for all transaction submissions.
*   **Edge Requirement:** Dynamic. Trade only if the calibration-adjusted EV clears both the taker fee barrier and the longshot buffer:
    1. Compute $FV_{adjusted}$ via `CalibrationAdjuster`.
    2. Select side via `EVCalculator.best_side()`.
    3. Apply `HourlySignalFilter` multiplier to signal score.
    4. Require $|FV_{adjusted} - Market\_Price| > \text{TakerFee} \times \text{LongshotMultiplier}$.
    The taker fee barrier varies between 0.44% and 3.15% based on price. The longshot multiplier is 1.5× outside 15%–85%.

## 6. Risk Management (Crash-Only Design)
*   **Oracle-Lag Arbitrage Protection:** Real-time monitoring of Chainlink Data Streams to prevent being picked off by oracle updates.
*   **Max Drawdown:** Hard stop at 10% daily loss.
*   **Fail-Safe:** If Binance WebSocket heartbeat is lost > 3000ms, cancel all Polymarket open orders.
*   **Crash-Only:** The bot is designed to panic on invalid states and resume from `logs/state.json`.

## 7. Development Roadmap
1.  **Phase 1 (Infra):** ✅ DONE — Nautilus `TradingNode`, private RPCs, Binance/Polymarket feeds.
2.  **Phase 2 (Signal):** ✅ DONE — V3 `SignalGenerationStrategy` with OBI, CVD, Funding Rate, Realized Vol.
3.  **Phase 3 (Hardening):** IN PROGRESS — V4 upgrades: latency observability, empirical calibration, property-based testing.
    -   ✅ Empirical calibration framework (`src/market_calibration.py`) integrated.
    -   ✅ Oracle-lag protection (30s no-trade zone) implemented.
    -   ✅ Staleness panic at 500ms implemented.
    -   ⏳ `TickToTradeLogger` (execution fidelity logging).
    -   ⏳ `FastLaneBundleService` (MEV protection).
    -   ⏳ `FeeOptimizer` (dynamic barrier engine).
4.  **Phase 4 (Live Alpha):** Run paper mode, benchmark P50/P99 latency, validate calibration vs. live market.
5.  **Phase 5 (Production):** Full live deployment with monitoring dashboard.
