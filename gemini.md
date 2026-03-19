# Project Context: 5-Min BTC Prediction Bot (NautilusTrader)

## 1. Architecture & Framework Philosophy
We are building a **5-Minute High-Frequency Prediction** bot using **NautilusTrader**, a hybrid Python/Rust platform.
-   **Core Principle:** "Crash-only" design. The system prioritizes data integrity over availability. If an arithmetic error or invalid state occurs, the bot must fail fast (panic) rather than propagate corrupt data [1, 2].
-   **Concurrency:** We use **Actor-based** concurrency. Strategies and components communicate via a `MessageBus`. Do NOT use standard Python `threading` for logic; rely on the Nautilus event loop [3, 4].
-   **One Node Per Process:** Never attempt to run multiple `TradingNode` instances in the same process due to global Rust state locks [5].

## 2. Coding Conventions (Strict)
NautilusTrader bridges Python to a Rust backend. To prevent serialization errors, follow these strict typing rules:
-   **No Floats for Money:** ALWAYS use the `Money`, `Price`, and `Quantity` objects. Never use raw Python `float` for currency or volume.
    -   *Bad:* `price = 95000.50`
    -   *Good:* `price = Price.from_str("95000.50")` or `qty = instrument.make_qty(0.1)` [6, 7].
-   **Type Hinting:** All Python functions must have strict type hints to aid the Cython compiler and internal type checkers.
-   **Configuration:** Use `TradingNodeConfig` and component-specific config objects (e.g., `BinanceDataClientConfig`), not raw dictionaries [8].

## 3. Venue & Instrument Specifics
We are arbitraging/predicting between Binance Perpetual signals and Polymarket execution.

### Binance (Signal Source)
-   **Symbology:** Nautilus appends `-PERP` to perpetual contracts.
    -   Use: `InstrumentId.from_str("BTCUSDT-PERP.BINANCE")` [9, 10].
-   **Data Subscription:** We need Funding Rates and Mark Prices.
    -   Subscribe to: `BinanceFuturesMarkPriceUpdate` via `subscribe_data` [11].
    -   *Note:* Real-time funding rate analysis is our primary alpha signal [11].

### Polymarket (Execution Venue)
-   **Symbology:** Polymarket instruments are Binary Option tokens (CTF).
    -   Resolution: Settled via **Chainlink Data Streams** (near-instant resolution). The bot no longer accounts for the old UMA 2-hour window [12, 13].
-   **Order Types:** Use `Limit` orders. Polymarket operates a CLOB (Central Limit Order Book) on Polygon [14].
    -   *Warning:* 2026 fee schedule introduced Taker Fees on 5-min markets (~3.15% at 50% probability). Logic must account for this barrier [15, 16].

## 4. Bot Skills (2026 Update)
-   **Oracle-Lag Arbitrage Protection:** Real-time monitoring of Chainlink Data Streams to prevent being picked off by oracle updates.
-   **Dynamic Fee Modeling:** Real-time calculation of the taker fee barrier based on the current probability curve (0.44% - 3.15%).
-   **Empirical Market Calibration:** `src/market_calibration.py` applies research-grounded corrections derived from the `prediction-market-analysis` framework (36GB Polymarket + Kalshi trade history):
    -   `CalibrationAdjuster`: Corrects raw B-S fair values for the documented longshot bias at extreme prices (< 15% / > 85%).
    -   `EVCalculator`: Computes EV on both YES and NO sides and selects the better direction.
    -   `HourlySignalFilter`: Multiplies signal scores by a UTC-hour factor (0.60× at 03:00 UTC dead zone → 1.12× at 16:00 UTC US peak).
    -   `LongshotBiasFilter`: Requires 50% additional edge in longshot price territory.
    -   **Source:** `research/prediction_market_analysis/` — run analyses via `python main.py` in that directory.

## 5. Implementation Pattern
Follow the "Three-File" pattern for clarity:
1.  `strategy.py`: Contains the `Strategy` class inheriting from `nautilus_trader.trading.strategy.Strategy`. Handle `on_data` events here.
2.  `config.py`: Defines the `TradingNodeConfig` and registers `BinanceLiveDataClientFactory` and `PolymarketLiveExecClientFactory` [17].
3.  `run.py`: The entry point that builds and runs the node.

## 6. Critical Reference: Common Pitfalls
-   **Timeouts:** API requests to Polymarket (Polygon RPC) can be slow. Use private Alchemy/QuickNode endpoints [18].
-   **State Reset:** If the bot crashes, do not attempt to "resume" memory state. Nautilus re-hydrates state from the cache/database upon restart [19].

## 7. Project Progress
-   **Phase 1 (Infra):** ✅ COMPLETED. Node connectivity and Binance/Polymarket feeds established.
-   **Phase 2 (Signal):** ✅ COMPLETED. V3 `SignalGenerationStrategy` with OBI/CVD/Funding implemented and tested.
-   **Phase 3 (Infrastructure Hardening):** IN PROGRESS. V4 upgrades:
    -   ✅ Empirical calibration framework (`src/market_calibration.py`) integrated from `prediction-market-analysis`.
    -   ✅ Oracle-lag protection (30s no-trade zone) implemented and tested.
    -   ✅ Staleness panic at 500ms implemented and tested.
    -   ⏳ `TickToTradeLogger` (nanosecond execution fidelity) — active task.
    -   ⏳ `FastLaneBundleService` (MEV protection via FastLane Atlas).
    -   ⏳ `FeeOptimizer` (dynamic taker fee barrier engine).
    -   ⚠️ `LatencyLogger` threading violation unresolved (must replace with async actor).
    -   ⚠️ Fee value mismatch: `pricing.py` uses 1.56% at 50c; spec/tests expect 3.15% — pending fix.
