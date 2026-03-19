# Diamond V4: Strategy Revitalization

You are working on a NautilusTrader 5-minute BTC prediction bot that trades on Polymarket.
The codebase is at `/Users/abdulmalek/Diamond/1/`.

## Context

The bot has solid infrastructure (NautilusTrader event loop, Polymarket WSS, dual-log observability, risk management, retro dashboard) but the **signal engine and pricing model are not competitive enough to be profitable**. An audit revealed 5 critical weaknesses that must be fixed before deployment.

Read every file referenced below before making any changes. Understand the full architecture first.

## CRITICAL RULES

- Follow NautilusTrader conventions: Actor-based concurrency, no `threading`, no blocking `sleep`
- **NO FLOATS FOR MONEY** in any execution/risk code. Use `Decimal` for prices, quantities, balances
- Run `uv run pytest tests/ -v` after each batch of changes. All 59 existing tests must pass
- Run `uv run ruff format .` after changes
- Do NOT touch `dashboard.py` or `src/dashboard_api.py` — those are done

---

## Problem 1: Flat 0.5% Fee Is Wrong — Need Dynamic Fee Curve

**Current state:** `src/pricing.py` line 18 uses `fee_rate: float = 0.005` (flat 0.5%).

**Reality:** Polymarket 5-min crypto markets use a **dynamic fee curve** introduced Feb 2026. The fee is highest at 50-cent prices (~2-3%) and drops toward 0% and 100%. This means:
- At a 50c token, break-even win rate is ~52%, not 50%
- `MIN_EDGE = 0.02` (2%) in `src/v3_strategy.py` is **below the fee** on balanced markets
- Every trade near 50c loses money even when the prediction is directionally correct

**Fix — `src/pricing.py`:**

1. Implement the Polymarket dynamic fee function. The fee formula for short-duration crypto markets is:
   ```
   fee(p) = base_fee * 4 * p * (1 - p)
   ```
   Where `p` is the token price (0-1) and `base_fee` is approximately 0.0315 (3.15%) for 5-min markets. This gives:
   - At p=0.50: fee = 0.0315 * 4 * 0.5 * 0.5 = **3.15%**
   - At p=0.10: fee = 0.0315 * 4 * 0.1 * 0.9 = **1.13%**
   - At p=0.90: fee = 0.0315 * 4 * 0.9 * 0.1 = **1.13%**
   - At p=0.01: fee = 0.0315 * 4 * 0.01 * 0.99 = **0.12%**

2. Add a function:
   ```python
   def polymarket_fee(price: float, base_fee: float = 0.0315) -> float:
       """Polymarket dynamic taker fee for 5-min crypto markets."""
       return base_fee * 4.0 * price * (1.0 - price)
   ```

3. Update `fair_value_binary_yes()` to use dynamic fee:
   ```python
   fee = polymarket_fee(prob_yes) if fee_rate is None else fee_rate
   ```

4. Add a function that computes the **fee-adjusted edge** — the edge after accounting for the fee at the specific price point:
   ```python
   def net_edge(model_prob: float, market_price: float) -> float:
       """Edge after Polymarket dynamic fee."""
       gross_edge = model_prob - market_price
       fee = polymarket_fee(market_price)
       return gross_edge - fee
   ```

**Fix — `src/v3_strategy.py`:**

5. Change `MIN_EDGE` from `Decimal("0.02")` to `Decimal("0.04")`. At 50c tokens, fee is ~3.15%, so minimum edge must be >3.15% to be profitable. 4% gives a small buffer.

6. In `_price_and_log()`, use fee-aware edge calculation:
   ```python
   from src.pricing import polymarket_fee
   fee = Decimal(str(polymarket_fee(float(target_price))))
   net = edge - fee
   ```
   Gate on `net` instead of raw `edge`.

**Tests — `tests/test_pricing.py`:**

7. Add tests for `polymarket_fee()`:
   - `test_fee_at_50c`: fee(0.50) should be approximately 0.0315
   - `test_fee_at_extremes`: fee(0.01) < 0.005, fee(0.99) < 0.005
   - `test_fee_symmetric`: fee(0.3) == fee(0.7)
   - `test_net_edge_profitable`: net_edge(0.60, 0.50) > 0 (10% gross edge minus ~3% fee = ~7% net)
   - `test_net_edge_unprofitable`: net_edge(0.52, 0.50) < 0 (2% gross edge minus ~3% fee = negative)

---

## Problem 2: No Price Momentum Signal

**Current state:** All 3 signals (OBI, CVD, funding) are *flow-based* — they measure order book state and trade flow. None measure **price momentum** (is BTC accelerating in a direction?). For 5-minute predictions, short-term momentum is the strongest predictor of continuation.

**Fix — `src/v3_strategy.py`:**

1. Add a **micro-momentum signal**: 10-second BTC returns. Track the last N spot prices with timestamps in a deque. Compute the return over the last 10 seconds:
   ```python
   # In __init__:
   self._spot_history: Deque[tuple[float, float]] = deque(maxlen=500)  # (timestamp, price)
   self._momentum_ema: float = 0.0
   MOMENTUM_EMA_ALPHA = 0.10  # responsive

   # In on_trade_tick():
   self._spot_history.append((time.time(), self.latest_spot))

   # New method:
   def _compute_momentum(self) -> float:
       """10-second BTC return, normalized to [-1, 1]."""
       now = time.time()
       cutoff = now - 10.0
       old_prices = [p for t, p in self._spot_history if t <= cutoff]
       if not old_prices or self.latest_spot <= 0:
           return 0.0
       old_price = old_prices[-1]  # most recent price from 10s ago
       ret = (self.latest_spot - old_price) / old_price
       # Normalize: ±0.1% return maps to ±1.0 (BTC moves ~0.05% in 10s normally)
       return max(-1.0, min(1.0, ret / 0.001))
   ```

2. Update signal weights to include momentum:
   ```python
   W_OBI = 0.40       # was 0.55
   W_CVD = 0.25       # was 0.30
   W_MOMENTUM = 0.25  # NEW — strongest short-term predictor
   W_FUNDING = 0.10   # was 0.15
   ```

3. In `_evaluate_v3_signal()`, compute momentum and include in composite:
   ```python
   momentum_raw = self._compute_momentum()
   if not self._momentum_initialized:
       self._momentum_ema = momentum_raw
       self._momentum_initialized = True
   else:
       self._momentum_ema = MOMENTUM_EMA_ALPHA * momentum_raw + (1 - MOMENTUM_EMA_ALPHA) * self._momentum_ema

   signal_score = (
       W_OBI * self._obi_ema
       + W_CVD * self._cvd_ema
       + W_MOMENTUM * self._momentum_ema
       + W_FUNDING * funding_bias
   )
   ```

4. Update momentum confirmation to require 2 of 3 (OBI, CVD, momentum) to agree with signal direction, instead of requiring all to agree. The current check is too strict — it filters out too many valid signals:
   ```python
   if passes:
       indicators = [self._obi_ema, self._cvd_ema, self._momentum_ema]
       signal_dir = 1 if signal_score > 0 else -1
       agreement = sum(1 for ind in indicators if (ind > 0) == (signal_dir > 0))
       if agreement < 2:
           passes = False
           direction = "FLAT"
   ```

5. Add momentum to ModelState logging and thinking log entries so the dashboard can display it.

**Tests — `tests/test_strategy.py`:**

6. Add `test_compute_momentum`:
   - Empty history returns 0.0
   - Price increase over 10s returns positive value
   - Price decrease returns negative
   - Result clamped to [-1, 1]

7. Update `evaluate_composite_signal` tests to include momentum weight.

---

## Problem 3: Strike = Spot Price (Always FV ≈ 0.50)

**Current state:** In `src/v3_strategy.py` line 257-258:
```python
if self.K == 0:
    self.K = self.latest_spot
```

This sets the strike to the current BTC price, which means `S ≈ K`, which means `d2 ≈ 0`, which means `N(d2) ≈ 0.50`. The fair value is always approximately 50 cents regardless of signals. The model's probability assessment is essentially noise around 0.50.

**The real strike** comes from the Polymarket market question (e.g., "BTC > $87,400?"). The `MarketWindow.strike` field already contains this — it's extracted from the question text by `polymarket_feed.py`.

**Fix — `src/v3_strategy.py`:**

1. Remove the fallback `self.K = self.latest_spot`. Instead, **refuse to trade** if strike is not set from the market question:
   ```python
   if self.K == 0:
       # Don't trade without a real strike — FV would be meaningless
       return
   ```

2. In `_refresh_market_window()`, the strike update from `self._current_window.strike` already exists (line 580-582). Make it more aggressive — if the market window has no strike (strike == 0), parse it from the question text as a fallback:
   ```python
   def _parse_strike_from_question(self, question: str) -> float:
       """Extract strike price from Polymarket question like 'BTC > $87,400?'"""
       import re
       match = re.search(r'\$([0-9,]+(?:\.[0-9]+)?)', question)
       if match:
           return float(match.group(1).replace(',', ''))
       return 0.0
   ```

3. When strike differs from spot significantly, the model produces differentiated fair values — this is where edge actually comes from. Log the relationship:
   ```python
   # In thinking log:
   "strike_vs_spot": round(self.latest_spot - self.K, 2),
   "moneyness": "ITM" if self.latest_spot > self.K else "OTM",
   ```

**Fix — `src/polymarket_feed.py`:**

4. Verify that `fetch_market_window()` correctly parses the strike from the Gamma API response. Read the current implementation and ensure the `strike` field is populated from the market's question text or outcome data. If it's not being populated, add parsing logic using the regex above.

---

## Problem 4: CVD Window Too Long (5 Minutes)

**Current state:** `_compute_cvd()` in `src/v3_strategy.py` uses a 300-second (5-minute) rolling window:
```python
cutoff = now_ns - 300_000_000_000  # 300 seconds = 5 min window
```

A 5-minute CVD window is the *entire prediction window*. By the time CVD accumulates meaningful signal over 5 minutes, the window is almost over. For a 5-minute prediction, you need CVD to respond in **30-60 seconds**, not 300.

**Fix — `src/v3_strategy.py`:**

1. Change CVD window to 60 seconds:
   ```python
   CVD_WINDOW_NS = 60_000_000_000  # 60 seconds
   ```

2. In `_compute_cvd()`:
   ```python
   cutoff = now_ns - CVD_WINDOW_NS
   ```

3. Consider adding a **multi-timeframe CVD** — compute both 15-second and 60-second CVD. Use the 15s CVD for direction confirmation and 60s CVD for the composite signal. If 15s CVD disagrees with 60s CVD, it suggests a reversal may be forming:
   ```python
   def _compute_cvd_window(self, window_s: int) -> float:
       """CVD over specified window, normalized to [-1, 1]."""
       now_ns = self.clock.timestamp_ns()
       cutoff = now_ns - (window_s * 1_000_000_000)
       # ... same logic as current _compute_cvd()
   ```

**Tests — `tests/test_strategy.py`:**

4. Update `TestComputeCVD.test_cutoff_excludes_old` to use 60s window instead of 300s.

---

## Problem 5: No Liquidation / Large Trade Detection

**Current state:** `on_trade_tick()` treats all trades equally — a 0.001 BTC retail trade and a 50 BTC liquidation get the same weight in CVD. Large trades (liquidations, whale orders) are the strongest short-term predictors on Binance Futures because they cause cascading price moves.

**Fix — `src/v3_strategy.py`:**

1. Add a **large trade detector**. Track the rolling average trade size and flag trades that are >5x the average:
   ```python
   # In __init__:
   self._avg_trade_size: float = 0.0
   self._trade_count: int = 0
   self._large_trade_bias: float = 0.0  # running bias from large trades
   LARGE_TRADE_MULTIPLIER = 5.0
   LARGE_TRADE_DECAY = 0.95  # decay per tick

   # In on_trade_tick():
   size = float(tick.size)
   self._trade_count += 1
   self._avg_trade_size = (
       self._avg_trade_size * (self._trade_count - 1) + size
   ) / self._trade_count

   # Detect large trades
   if self._trade_count > 100 and size > self._avg_trade_size * LARGE_TRADE_MULTIPLIER:
       direction = 1.0 if tick.aggressor_side == AggressorSide.BUYER else -1.0
       magnitude = min(1.0, size / (self._avg_trade_size * LARGE_TRADE_MULTIPLIER * 2))
       self._large_trade_bias += direction * magnitude
       self._large_trade_bias = max(-1.0, min(1.0, self._large_trade_bias))

       self.le.thinking("large_trade", {
           "size": size,
           "avg_size": round(self._avg_trade_size, 4),
           "multiplier": round(size / self._avg_trade_size, 1),
           "direction": "BUY" if direction > 0 else "SELL",
           "new_bias": round(self._large_trade_bias, 4),
       })

   # Decay bias toward 0 every tick
   self._large_trade_bias *= LARGE_TRADE_DECAY
   ```

2. Add large trade bias as a boost to the composite signal (not a separate weight — it's an event-driven overlay):
   ```python
   # In _evaluate_v3_signal(), after computing signal_score:
   signal_score += self._large_trade_bias * 0.15  # 15% boost from large trades
   ```

3. Log `large_trade_bias` in the evaluation thinking event and in ModelState.

---

## Execution Order

1. **Read all files** listed above first. Understand the inheritance chain: `Strategy -> SignalGenerationStrategy -> PaperTradingStrategy -> V3ProductionStrategy`
2. Fix Problem 1 (fees) — this is the most critical, every trade loses money without it
3. Fix Problem 3 (strike) — second most critical, model produces no edge without real strike
4. Fix Problem 4 (CVD window) — quick fix, big impact on signal responsiveness
5. Fix Problem 2 (momentum) — new signal source
6. Fix Problem 5 (large trades) — event-driven overlay
7. Run `uv run pytest tests/ -v` — all tests must pass (59 existing + new ones)
8. Run `uv run ruff format .`

## Verification Checklist

After all changes:
- [ ] `uv run pytest tests/ -v` — 0 failures
- [ ] `uv run ruff format --check .` — 0 reformats needed
- [ ] `polymarket_fee(0.50)` returns ~0.0315
- [ ] `net_edge(0.55, 0.50)` returns ~0.0185 (5% gross - 3.15% fee)
- [ ] `net_edge(0.52, 0.50)` returns negative (unprofitable after fees)
- [ ] `MIN_EDGE` is 0.04 or higher
- [ ] Strike is never set to `self.latest_spot` — always from market question
- [ ] CVD window is 60s, not 300s
- [ ] Momentum signal is computed and included in composite
- [ ] Large trade detection logs events to thinking log
- [ ] All new signals appear in ModelState for dashboard display
