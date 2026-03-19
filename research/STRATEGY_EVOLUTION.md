# Strategy Evolution Analysis (Project Diamond)

## 1. Shift: 15m $	o$ 5m (High-Frequency Transition)
**Rationale:**
The transition from 15-minute to 5-minute resolution was driven by two major market shifts in late 2025:
- **Removal of 500ms Taker Price Delay:** Polymarket's elimination of the execution delay enabled high-frequency strategies to compete on speed.
- **Launch of Higher-Frequency Contracts:** The market liquidity migrated to 5-minute intervals, offering more "betting" opportunities and faster capital turnover.

## 2. Shift: Reactive $	o$ Predictive (Edge Finding)
**Rationale:**
In the previous 15m regime, the bot was primarily *reactive*, chasing lags between Binance and Polymarket. However, the introduction of the **2026 Taker Fee Schedule** (~3.15% taker fee at 50% probability) made simple lag-chasing unprofitable.
- **New Approach:** We moved to a predictive model using **Black-Scholes $N(d_2)$** to calculate "Fair Value."
- **Goal:** Find edges where $|FV - Market Price| > 	ext{Taker Fee}$. This allows the bot to trade ahead of the curve rather than just reacting to it.

## 3. Shift: UMA $	o$ Chainlink (Instant Settlement)
**Rationale:**
- **Previous System (UMA):** Relied on a 2-hour "Liveness Period" for disputes, locking up capital and introducing "oracle risk" during the settlement window.
- **New System (Chainlink Data Streams):** Provides near-instant resolution at the end of each 5-minute interval.
- **Impact:** Significant reduction in capital lockup time, allowing for higher frequency reinvestment and eliminating the need for liveness-window monitoring.

## 4. Complexity of the 2026 Environment
The speed war in 5-minute markets necessitates:
- **Private RPCs:** Bypassing public congestion.
- **MEV Protection:** Utilizing FastLane Atlas bundles to prevent being frontrun by searchers.
- **Dynamic Fee Modeling:** Calculating the exact taker fee barrier (0.44% - 3.15%) in real-time.

## 5. Shift: Pure Theory → Empirical Calibration (Phase 3)
**Rationale:**
The original pricing model treated Black-Scholes $N(d_2)$ output as the true fair value. However, empirical analysis of 36GB of real Polymarket and Kalshi trade/resolution data via the `prediction-market-analysis` framework revealed systematic deviations:

- **Longshot Bias:** Prediction markets consistently overprice outcomes with implied probabilities < 15% or > 85%. The raw B-S output fails to account for this, generating false positive trade signals at extremes.
- **Time-of-Day Effects:** Calibration quality degrades significantly during low-liquidity hours (00:00–07:00 UTC). Signals generated at 03:00 UTC carry meaningfully less information than the same signal at 16:00 UTC.
- **Directional EV Asymmetry:** For a given price, the EV of buying YES vs. NO is not symmetric in practice due to microstructure effects. Explicit EV comparison on both sides outperforms directional signal alone.

**New Approach:** `src/market_calibration.py` introduces four components that layer empirical corrections on top of the raw model output:
1. `CalibrationAdjuster` — blends B-S FV with empirical win-rate curve (α=0.3).
2. `EVCalculator` — selects the higher-EV side before each trade.
3. `HourlySignalFilter` — scales signal strength by UTC-hour liquidity multiplier.
4. `LongshotBiasFilter` — requires 50% additional edge in bias-heavy price zones.

**Impact:** The trade gate is no longer a single threshold on raw edge. It is a multi-layer EV filter grounded in observed market behavior, reducing false positives without reducing true signal capture.
