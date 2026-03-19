# Phase 3 Roadmap: 2026 Optimization & Speed War

## Goal 0: Empirical Market Calibration — ✅ COMPLETED
- **Framework Integrated:** `research/prediction_market_analysis/` (Jon-Becker framework) provides a 36GB Parquet dataset of real Polymarket and Kalshi trade/resolution history analyzed via DuckDB across 29 statistical analyses.
- **Deliverable:** `src/market_calibration.py` — four production components wired into `strategy.py` and `pricing.py`:
    - `CalibrationAdjuster`: Corrects raw B-S fair value for empirical longshot bias (alpha=0.3 blend with calibration curve).
    - `EVCalculator`: Explicit EV computation on both YES/NO sides; always trades the higher-EV direction.
    - `HourlySignalFilter`: Scales signal scores by time-of-day multiplier (0.60× dead zone → 1.12× US peak hours 14–21 UTC).
    - `LongshotBiasFilter`: Raises minimum edge requirement 1.5× when market price is outside 15%–85% (longshot bias zone).
- **Impact:** Replaces purely theoretical trade gate with empirically grounded EV filter. Reduces false positives at extreme price levels and low-liquidity hours.

## Goal 1: High-Performance Infrastructure
- **Private RPC Integration:** Transition from public RPCs to dedicated **Alchemy** or **QuickNode** endpoints. This is critical for reliable transaction submission in 5-minute markets where milliseconds matter.
- **WebSocket Load Balancing:** Implement redundant Binance/Polygon WebSocket connections with health-check failover.

## Goal 2: MEV-Protected Submission (FastLane)
- **FastLane Atlas Integration:** Implement support for Atlas Bundles.
- **Rationale:** 5-minute markets are highly competitive; public mempool submission is susceptible to frontrunning and sandwiches. Atlas ensures transactions are submitted directly to validators with MEV protection.
- **Deliverable:** `FastLaneBundleService` in `src/execution.py`.

## Goal 3: Taker Fee Optimization (The "Barrier" Engine)
- **Dynamic Edge Threshold:** Implement a "Taker Fee Optimizer" that adjusts the required edge $|FV - Market Price|$ based on the current probability curve.
- **Math:**
    - The fee is non-linear (0.44% - 3.15%).
    - The bot must calculate the fee *before* submission to ensure the expected value (EV) remains positive after fees.
- **Note:** Fee value in `src/pricing.py` (`base_fee=0.0156`) is currently misaligned with the spec (3.15% at 50c). Pending fix by active agent.
- **Deliverable:** `FeeOptimizer` component within the `PricingEngine`.

## Goal 4: Predictor Refinement
- **Black-Scholes $N(d_2)$ Calibration:** Continuously tune Implied Volatility ($\sigma$) based on high-frequency (1s) Binance trade flow and order book imbalance.
- **Oracle-Lag Arbitrage Protection:** Monitor Chainlink Data Streams and pause trading if the delta between Binance and the latest stream update exceeds a risk threshold.
