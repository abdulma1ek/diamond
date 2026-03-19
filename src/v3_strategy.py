"""V3 Production Strategy — Signal-Smoothed BTC Prediction Bot.

Key improvements over V2:
  - EMA-smoothed OBI and CVD (eliminates tick-level noise)
  - Adaptive threshold based on volatility regime
  - Signal cooldown (30s minimum between trades)
  - Proper Kelly sizing capped at 15%
  - Real strike from Polymarket market question
  - Momentum confirmation (OBI and CVD must agree)
  - Full dual-log observability (technical + thinking)
"""

from collections import deque
from decimal import Decimal
from math import log, sqrt
from typing import Deque
import time
from datetime import timedelta

from nautilus_trader.core.rust.model import AggressorSide
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import Bar, DataType, TradeTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.objects import Price, Quantity

from src.pricing import fair_value_binary_yes, fair_value_yes, fair_value_no, taker_adjusted_edge, polymarket_fee
from src.paper_strategy import PaperTradingStrategy
from src.polymarket_feed import fetch_current_market, MarketWindow
from src.log_engine import LogEngine, ModelState


# ── Signal Processing Constants ──────────────────────────────────────

# EMA smoothing factors (higher = more responsive, lower = smoother)
OBI_EMA_ALPHA = Decimal("0.08")  # ~12-tick half-life for order book imbalance
CVD_EMA_ALPHA = Decimal("0.05")  # ~20-tick half-life for cumulative volume delta
MOMENTUM_EMA_ALPHA = Decimal("0.10")  # responsive

# Signal weights
W_OBI = Decimal("0.40")
W_CVD = Decimal("0.25")
W_MOMENTUM = Decimal("0.25")
W_FUNDING = Decimal("0.10")

# Thresholds
THRESHOLD_LOW_VOL = Decimal("0.20")
THRESHOLD_HIGH_VOL = Decimal("0.12")
HIGH_VOL_CUTOFF = 0.80  # annualized vol above this = "high vol regime"

# Cooldown: minimum seconds between consecutive trades
SIGNAL_COOLDOWN_S = 30

# Risk: maximum stake as fraction of balance
MAX_STAKE_FRACTION = Decimal("0.15")

# Price safeguard: don't buy tokens above this (poor risk/reward)
MAX_TOKEN_PRICE = Decimal("0.98")

# Minimum edge required to execute a trade
MIN_EDGE = Decimal("0.04")

# Diagnostic logging interval
DIAG_INTERVAL_S = 30


class V3ProductionStrategy(PaperTradingStrategy):
    """V3 Production Strategy with EMA-smoothed signals and full observability."""

    def __init__(
        self,
        config,
        starting_balance: Decimal = Decimal("10.0"),
        polymarket_executor=None,
        polymarket_actor=None,
        log_engine: LogEngine | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            config=config,
            starting_balance=starting_balance,
            polymarket_executor=polymarket_executor,
            **kwargs,
        )
        self.binance_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        self.poly_actor = polymarket_actor
        self.le = log_engine or LogEngine()

        # Polymarket book cache (WSS backed)
        self._poly_books: dict[InstrumentId, OrderBook] = {}

        # Strike (K) — from Polymarket market question
        self.K: Price | None = None

        # ── Signal State ─────────────────────────────────────────
        self.trade_deltas: Deque[tuple[int, Decimal]] = deque(maxlen=10000)
        self.bar_closes: Deque[Price] = deque(maxlen=60)
        self.latest_funding_rate: Decimal = Decimal("0.0")
        self.realized_vol: float = 0.0

        # EMA-smoothed signals
        self._obi_ema: Decimal = Decimal("0.0")
        self._cvd_ema: Decimal = Decimal("0.0")
        self._momentum_ema: Decimal = Decimal("0.0")
        self._obi_raw: Decimal = Decimal("0.0")
        self._cvd_raw: Decimal = Decimal("0.0")
        self._obi_initialized: bool = False
        self._cvd_initialized: bool = False
        self._momentum_initialized: bool = False

        # Micro-momentum tracking
        self._spot_history: Deque[tuple[float, Price]] = deque(maxlen=500)

        # Large trade detection
        self._avg_trade_size: Decimal = Decimal("0.0")
        self._trade_count: int = 0
        self._large_trade_bias: Decimal = Decimal("0.0")
        self.LARGE_TRADE_MULTIPLIER = Decimal("5.0")
        self.LARGE_TRADE_DECAY = Decimal("0.95")

        self._is_up_down: bool = False

        # ── Counters ─────────────────────────────────────────────
        self.start_time = time.time()
        self.total_signals = 0
        self.total_trades = 0
        self.trades_skipped = 0
        self._last_trade_time: float = 0.0
        self._last_diag_time: float = 0.0

        # ── Model State (written to state.json for dashboard) ────
        self._state = ModelState()
        self._state.starting_balance = starting_balance
        self._state.balance = starting_balance
        self._state.engine_status = "INITIALIZING"

        # Log engine init
        self.le.technical(
            "strategy_init",
            {
                "version": "V3",
                "balance": float(starting_balance),
                "weights": {"obi": float(W_OBI), "cvd": float(W_CVD), "funding": float(W_FUNDING)},
                "thresholds": {
                    "low_vol": float(THRESHOLD_LOW_VOL),
                    "high_vol": float(THRESHOLD_HIGH_VOL),
                },
                "ema_alpha": {"obi": float(OBI_EMA_ALPHA), "cvd": float(CVD_EMA_ALPHA)},
                "cooldown_s": SIGNAL_COOLDOWN_S,
                "max_stake": float(MAX_STAKE_FRACTION),
            },
        )

    # ── Lifecycle ────────────────────────────────────────────────

    def on_start(self) -> None:
        super().on_start()

        # 1. Heartbeat timer for dashboard (1s)
        self.clock.set_timer(
            name="dashboard_heartbeat",
            interval=timedelta(seconds=1),
            callback=self._on_heartbeat_timer,
        )

        # 2. Subscribe to OrderBook snapshots via MessageBus (for Polymarket WSS)
        self.msgbus.subscribe(
            topic=DataType(OrderBook).topic,
            handler=self.on_order_book,
        )

        # Subscribe to Mark Price for funding rates
        try:
            from nautilus_trader.adapters.binance.futures.types import (
                BinanceFuturesMarkPriceUpdate,
            )

            self.subscribe_data(
                data_type=DataType(BinanceFuturesMarkPriceUpdate),
                instrument_id=self.binance_id,
            )
            self.le.technical("subscription", {"type": "mark_price", "status": "ok"})
        except Exception as e:
            self.le.technical(
                "subscription",
                {"type": "mark_price", "status": "failed", "error": str(e)},
            )

        self._state.engine_status = "RUNNING"
        self.le.update_state(self._state)
        
        self.le.technical("strategy_start", {"status": "running"})
        self.log.info("V3 Strategy: started with EMA-smoothed signals")

    def _on_heartbeat_timer(self, event) -> None:
        """Periodic heartbeat to keep dashboard alive."""
        self.le.update_state(self._state)

    # ── Data Handlers ────────────────────────────────────────────

    def on_bar(self, bar: Bar) -> None:
        """Update realized volatility from 1-min bar closes (Price objects)."""
        self.bar_closes.append(bar.close)
        if len(self.bar_closes) >= 2:
            closes = [float(p) for p in list(self.bar_closes)]
            log_returns = [
                log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            ]
            mean_ret = sum(log_returns) / len(log_returns)
            variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
            self.realized_vol = sqrt(variance) * sqrt(525600)

    def on_order_book(self, order_book: OrderBook) -> None:
        """Handle order book updates from Binance (OBI) and Polymarket (WSS cache)."""
        if ".POLY" in str(order_book.instrument_id):
            self._poly_books[order_book.instrument_id] = order_book
            return

        if order_book.instrument_id == self.binance_id:
            bids = order_book.bids()
            asks = order_book.asks()
            total_bid_vol = sum(Decimal(str(level.size())) for level in bids)
            total_ask_vol = sum(Decimal(str(level.size())) for level in asks)
            denom = total_bid_vol + total_ask_vol
            if denom > 0:
                self._obi_raw = (total_bid_vol - total_ask_vol) / denom
                if not self._obi_initialized:
                    self._obi_ema = self._obi_raw
                    self._obi_initialized = True
                else:
                    self._obi_ema = OBI_EMA_ALPHA * self._obi_raw + (Decimal("1.0") - OBI_EMA_ALPHA) * self._obi_ema
            
            self.latest_feed_ts = order_book.ts_event
            self._evaluate_v3_signal()

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Update CVD and latest spot from Binance trade ticks."""
        if tick.instrument_id == self.binance_id:
            self.latest_spot = tick.price
            self._state.btc_price = Decimal(str(self.latest_spot))
            self.latest_feed_ts = tick.ts_event
            
            if self._is_up_down and self.K is None and self.latest_spot:
                self.K = self.latest_spot
                self.log.info(f"V3 Strategy: Proactively initialized strike K={self.K} for Up/Down market")

            size = Decimal(str(tick.size))
            side = Decimal("1") if tick.aggressor_side == AggressorSide.BUYER else Decimal("-1")
            self.trade_deltas.append((tick.ts_event, side * size))

            # 1. Micro-momentum tracking
            self._spot_history.append((time.time(), self.latest_spot))

            # 2. Large trade detection
            self._trade_count += 1
            self._avg_trade_size = (
                self._avg_trade_size * Decimal(str(self._trade_count - 1)) + size
            ) / Decimal(str(self._trade_count))

            if (
                self._trade_count > 100
                and size > self._avg_trade_size * self.LARGE_TRADE_MULTIPLIER
            ):
                direction = Decimal("1.0") if tick.aggressor_side == AggressorSide.BUYER else Decimal("-1.0")
                magnitude = min(
                    Decimal("1.0"), size / (self._avg_trade_size * self.LARGE_TRADE_MULTIPLIER * Decimal("2"))
                )
                self._large_trade_bias += direction * magnitude
                self._large_trade_bias = max(Decimal("-1.0"), min(Decimal("1.0"), self._large_trade_bias))

            # Decay bias
            self._large_trade_bias *= self.LARGE_TRADE_DECAY

            if self.risk_manager:
                self.risk_manager.update_heartbeat(tick.ts_event)
            
            self.le.update_state(self._state)

    def on_data(self, data):
        """Handle custom data types like MarkPriceUpdate for funding."""
        try:
            from nautilus_trader.adapters.binance.futures.types import (
                BinanceFuturesMarkPriceUpdate,
            )

            if isinstance(data, BinanceFuturesMarkPriceUpdate):
                self.latest_funding_rate = Decimal(str(data.funding_rate))
        except ImportError:
            pass

    # ── Signal Engine ────────────────────────────────────────────

    def _compute_momentum(self) -> Decimal:
        """10-second BTC return, normalized to [-1, 1]."""
        now = time.time()
        cutoff = now - 10.0
        # Snapshot history for iteration
        history_snapshot = list(self._spot_history)
        old_prices = [p for t, p in history_snapshot if t <= cutoff]
        if not old_prices or self.latest_spot is None:
            return Decimal("0.0")
        old_price = float(old_prices[-1])
        latest_val = float(self.latest_spot)
        ret = (latest_val - old_price) / old_price
        # Normalize: ±0.1% return maps to ±1.0
        return Decimal(str(max(-1.0, min(1.0, ret / 0.001))))

    def _compute_cvd_window(self, window_s: int) -> Decimal:
        """CVD over specified window, normalized to [-1, 1]."""
        now_ns = self.clock.timestamp_ns()
        cutoff = now_ns - (window_s * 1_000_000_000)
        total_buy = Decimal("0.0")
        total_sell = Decimal("0.0")
        # Snapshot for thread-safe iteration
        deltas_snapshot = list(self.trade_deltas)
        for ts, delta in deltas_snapshot:
            if ts >= cutoff:
                if delta > 0:
                    total_buy += delta
                else:
                    total_sell += abs(delta)
        total = total_buy + total_sell
        if total == 0:
            return Decimal("0.0")
        return (total_buy - total_sell) / total

    def _compute_cvd(self) -> Decimal:
        """CVD over rolling 1-min window, normalized to [-1, 1]."""
        return self._compute_cvd_window(60)

    def _evaluate_v3_signal(self) -> None:
        """Composite signal with EMA smoothing and adaptive thresholds."""
        if self._is_data_stale():
            return

        if self.latest_spot is None:
            self._state.engine_status = "WAITING FOR DATA"
            return

        # ── 0. Ensure strike is set ──
        if self.K is None:
            self._state.engine_status = "WAITING FOR STRIKE"
            return

        self._state.engine_status = "RUNNING"

        # ── 1. Compute raw signals and apply EMAs ────────────────
        self._cvd_raw = self._compute_cvd()
        if not self._cvd_initialized:
            self._cvd_ema = self._cvd_raw
            self._cvd_initialized = True
        else:
            self._cvd_ema = (
                CVD_EMA_ALPHA * self._cvd_raw + (Decimal("1.0") - CVD_EMA_ALPHA) * self._cvd_ema
            )

        momentum_raw = self._compute_momentum()
        if not self._momentum_initialized:
            self._momentum_ema = momentum_raw
            self._momentum_initialized = True
        else:
            self._momentum_ema = (
                MOMENTUM_EMA_ALPHA * momentum_raw
                + (Decimal("1.0") - MOMENTUM_EMA_ALPHA) * self._momentum_ema
            )

        # ── 2. Funding bias ──────────────────────────────────────
        funding_bias = Decimal(str(max(-1.0, min(1.0, -float(self.latest_funding_rate) * 10000))))

        # ── 3. Composite signal (using smoothed values) ──────────
        signal_score = (
            W_OBI * self._obi_ema
            + W_CVD * self._cvd_ema
            + W_MOMENTUM * self._momentum_ema
            + W_FUNDING * funding_bias
        )

        # Apply large trade boost
        signal_score += self._large_trade_bias * Decimal("0.15")
        signal_score = max(Decimal("-1.0"), min(Decimal("1.0"), signal_score))

        self.total_signals += 1

        # ── 4. Volatility regime → adaptive threshold ────────────
        if self.realized_vol > HIGH_VOL_CUTOFF:
            threshold = THRESHOLD_HIGH_VOL
            vol_regime = "HIGH"
        elif self.realized_vol > 0:
            threshold = THRESHOLD_LOW_VOL
            vol_regime = "NORMAL"
        else:
            threshold = THRESHOLD_LOW_VOL
            vol_regime = "WAITING"

        # ── 5. Direction and threshold check ─────────────────────
        if signal_score > threshold:
            direction = "YES"
            passes = True
        elif signal_score < -threshold:
            direction = "NO"
            passes = True
        else:
            direction = "FLAT"
            passes = False

        # ── 6. Momentum confirmation: 2 of 3 must agree ──────────
        if passes:
            indicators = [self._obi_ema, self._cvd_ema, self._momentum_ema]
            signal_dir = 1 if signal_score > 0 else -1
            agreement = sum(1 for ind in indicators if (ind > 0) == (signal_dir > 0))
            if agreement < 2:
                passes = False
                direction = "FLAT"

        # ── 7. Get live market prices for logging ────────────────
        market_yes, market_no = self._get_poly_prices()

        # ── 8. Compute fair value for dashboard ──────────────────
        now_ns = self.clock.timestamp_ns()
        now_s = now_ns / 1_000_000_000
        time_left_s = (
            max(1, self._current_window.end_ts - now_s) if self._current_window else 300
        )
        fv = fair_value_yes(
            S=self.latest_spot,
            K=self.K,
            sigma=max(0.10, self.realized_vol),
            T=time_left_s / 31536000,
        )

        # ── 9. Update model state for dashboard ──────────────────
        self._state.btc_price = Decimal(str(self.latest_spot))
        self._state.strike = Decimal(str(self.K))
        self._state.time_left_s = int(time_left_s)
        self._state.obi_raw = self._obi_raw
        self._state.cvd_raw = self._cvd_raw
        self._state.obi_ema = self._obi_ema
        self._state.cvd_ema = self._cvd_ema
        self._state.funding_raw = funding_bias
        self._state.signal_score = signal_score
        self._state.signal_direction = direction
        self._state.signal_threshold = threshold
        self._state.signal_passes = passes
        self._state.realized_vol = Decimal(str(self.realized_vol))
        self._state.vol_regime = vol_regime
        self._state.fair_value_yes = Decimal(str(fv))
        self._state.fair_value_no = Decimal("1.0") - Decimal(str(fv))
        self._state.market_yes = Decimal(str(market_yes or 0.0))
        self._state.market_no = Decimal(str(market_no or 0.0))
        self._state.edge_yes = Decimal(str(fv)) - Decimal(str(market_yes or 0.5))
        self._state.edge_no = (Decimal("1.0") - Decimal(str(fv))) - Decimal(str(market_no or 0.5))
        self._state.signals_evaluated = self.total_signals
        self._state.signals_fired = self.total_trades + self.trades_skipped
        self._state.trades_skipped = self.trades_skipped
        self._state.total_trades = self.total_trades
        self._state.wins = self.paper.wins
        self._state.losses = self.paper.losses
        self._state.pending = len(self.paper.pending)
        self._state.balance = self.paper.balance
        self._state.total_pnl = self.paper.total_pnl
        total = self.paper.wins + self.paper.losses
        self._state.win_rate = Decimal(str((self.paper.wins / total * 100) if total > 0 else 0.0))
        self._state.roi_pct = ((self.paper.balance - self.paper.starting_balance) / self.paper.starting_balance * Decimal("100"))
        if self._current_window:
            self._state.market_question = self._current_window.question
            self._state.market_window_start = self._current_window.start_ts
            self._state.market_window_end = self._current_window.end_ts
        self._state.price_source = self._get_price_source()

        # New fields for V4
        self._state.momentum_ema = self._momentum_ema
        self._state.large_trade_bias = self._large_trade_bias

        self.le.update_state(self._state)

        # ── 10. Log model thinking ────────────
        self.le.thinking(
            "evaluation",
            {
                "score": signal_score,
                "obi_ema": self._obi_ema,
                "cvd_ema": self._cvd_ema,
                "momentum_ema": self._momentum_ema,
                "funding": funding_bias,
                "large_trade_bias": self._large_trade_bias,
                "vol": self.realized_vol,
                "vol_regime": vol_regime,
                "threshold": threshold,
                "direction": direction,
                "passes": passes,
                "price": float(self.latest_spot),
                "strike": float(self.K),
                "fv_yes": float(fv),
                "time_left": int(time_left_s),
            },
        )

        # ── 11. Periodic diagnostic ──────────────────────────────
        now = time.time()
        if now - self._last_diag_time > DIAG_INTERVAL_S:
            self.log.info(
                f"DIAG | score={signal_score:+.4f} | OBI_ema={self._obi_ema:+.4f} "
                f"CVD_ema={self._cvd_ema:+.4f} MOM_ema={self._momentum_ema:+.4f} "
                f"LTB={self._large_trade_bias:+.4f} | "
                f"K={self.K} S={self.latest_spot} | "
                f"thr={threshold:.2f} dir={direction} pass={passes}"
            )
            self._last_diag_time = now

        # ── 12. Execute if signal passes ─────────────────────────
        if passes:
            self._price_and_log(direction, signal_score)

    # ── Execution ────────────────────────────────────────────────

    def _price_and_log(self, direction: str, signal_score: Decimal) -> None:
        """V3 Pricing with full observability and proper risk controls."""

        # ── Cooldown check ───────────────────────────────────────
        now = time.time()
        if now - self._last_trade_time < SIGNAL_COOLDOWN_S:
            remaining = int(SIGNAL_COOLDOWN_S - (now - self._last_trade_time))
            self.le.thinking(
                "decision",
                {
                    "action": "COOLDOWN",
                    "reason": f"Must wait {remaining}s (cooldown={SIGNAL_COOLDOWN_S}s)",
                    "direction": direction,
                    "score": float(signal_score),
                },
            )
            return

        # ── Market window check ──────────────────────────────────
        if not self._current_window or self._current_window.closed:
            self._refresh_market_window()
            if not self._current_window:
                self.le.technical("market_window", {"status": "unavailable"})
                return

        # Oracle-Lag Protection Check
        if self._is_in_no_trade_zone():
            self.log.warning("SIGNAL BLOCKED: Within 30s No-Trade Zone (Oracle Lag Protection)")
            return

        # ── Black-Scholes pricing ────────────────────────────────
        now_ns = self.clock.timestamp_ns()
        now_s = now_ns / 1_000_000_000
        time_left_s = max(1, self._current_window.end_ts - now_s)
        vol = max(0.10, self.realized_vol)

        fv_yes = fair_value_yes(
            S=self.latest_spot, K=self.K, sigma=vol, T=time_left_s / 31536000
        )

        # ── Get executable Polymarket prices ─────────────────────
        market_yes, market_no = self._get_poly_prices_decimal()
        source = self._get_price_source()

        # Final fallback to synthetic
        if market_yes is None or market_no is None:
            market_yes = Decimal(str(fv_yes)).quantize(Decimal("0.01"))
            market_no = Decimal("1.0") - market_yes
            source = "SYNTH"

        # ── Edge calculation ─────────────────────────────────────
        if direction == "YES":
            target_fv = Decimal(str(fv_yes))
            target_market = market_yes
        else:
            target_fv = Decimal("1.0") - Decimal(str(fv_yes))
            target_market = market_no

        adj_edge = taker_adjusted_edge(
            fv=target_fv,
            market_price=target_market,
            min_edge=float(MIN_EDGE),
        )

        self.log.warning(
            f"SIGNAL {direction} | score={signal_score:+.4f} | FV={target_fv:.4f} "
            f"Mkt={target_market:.2f} | adj_edge={adj_edge:+.4f} | src={source}",
            color=LogColor.YELLOW,
        )

        # ── Decision reasoning ───────────────────────────────────
        reasoning = {
            "direction": direction,
            "score": float(signal_score),
            "fv": float(target_fv),
            "target_price": float(target_market),
            "edge": float(adj_edge),
            "source": source,
            "obi_ema": float(self._obi_ema),
            "cvd_ema": float(self._cvd_ema),
            "vol": round(vol, 6),
            "time_left": int(time_left_s),
            "balance": float(self.paper.balance),
        }

        # ── Gate 1: Price too high (poor R/R) ────────────────────
        if target_market >= MAX_TOKEN_PRICE:
            reasoning["action"] = "SKIP"
            reasoning["reason"] = (
                f"Price {target_market:.2f} >= {MAX_TOKEN_PRICE} (poor R/R)"
            )
            self.trades_skipped += 1
            self.le.thinking("decision", reasoning)
            self.log.info(f"SKIP | Price {target_market:.2f} too high")
            return

        # ── Gate 2: Insufficient edge ────────────────────────────
        if adj_edge < 0:
            reasoning["action"] = "SKIP"
            reasoning["reason"] = f"Edge {adj_edge:+.4f} below threshold"
            self.trades_skipped += 1
            self.le.thinking("decision", reasoning)
            self.log.info(f"SKIP | Edge {adj_edge:+.4f} insufficient")
            return

        # ── Gate 3: Risk manager ─────────────────────────────────
        if self.risk_manager:
            now_ns = self.clock.timestamp_ns()
            if not self.risk_manager.can_trade(now_ns):
                reasoning["action"] = "SKIP"
                reasoning["reason"] = "Risk manager halted"
                self.trades_skipped += 1
                self.le.thinking("decision", reasoning)
                self.le.technical("risk_halt", {"reason": "can_trade() returned False"})
                return

        # ── All gates passed — EXECUTE ───────────────────────────
        self.total_trades += 1
        self._last_trade_time = now
        reasoning["action"] = "TRADE"
        reasoning["reason"] = "All gates passed"

        # Kelly sizing (capped at MAX_STAKE_FRACTION)
        stake_fraction = Decimal("0.10")
        if self.risk_manager and self.risk_manager.current_balance > 0:
            if Decimal("0.01") < target_market < Decimal("0.99"):
                kelly = self.risk_manager.kelly_size(
                    win_prob=target_fv,
                    win_payout=Decimal("1.0") - target_market,
                    loss_amount=target_market,
                )
                if kelly > 0:
                    stake_fraction = min(
                        kelly / self.risk_manager.current_balance,
                        MAX_STAKE_FRACTION,
                    )

        reasoning["stake_fraction"] = float(stake_fraction)
        self.le.thinking("decision", reasoning)

        # Paper trade execution
        pred = self.paper.make_prediction(
            direction=direction,
            btc_spot=Decimal(str(self.latest_spot)),
            market_yes=market_yes,
            market_no=market_no,
            signal_score=float(signal_score),
            fair_value=Decimal(str(fv_yes)),
            stake_fraction=stake_fraction,
        )

        if pred:
            self.le.trade(
                {
                    "event": "open",
                    "direction": direction,
                    "btc_entry": float(pred.btc_entry),
                    "entry_price": float(pred.entry_price),
                    "num_tokens": float(pred.num_tokens),
                    "stake": float(pred.stake),
                    "edge": float(adj_edge),
                    "fv": float(target_fv),
                    "signal_score": float(signal_score),
                    "balance_after": float(self.paper.balance),
                }
            )

            self._print_scoresheet(
                direction, signal_score, fv_yes, adj_edge, time_left_s, source
            )

    # ── Market Window ────────────────────────────────────────────

    def _parse_strike_from_question(self, question: str) -> float:
        """Extract strike price from Polymarket question like 'BTC > $87,400?'"""
        import re

        match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", question)
        if match:
            return float(match.group(1).replace(",", ""))
        return 0.0

    def _refresh_market_window(self) -> None:
        """Fetch current 5-min market and sync strike + WSS actor."""
        super()._refresh_market_window()
        if self._current_window:
            # Detect market type
            self._is_up_down = "Up or Down" in self._current_window.question
            
            # Sync strike K
            strike = self._current_window.strike
            if strike == 0:
                strike = self._parse_strike_from_question(self._current_window.question)

            # For Up/Down markets, if we still have no strike, 
            # we initialize K from the latest spot if we are at/near the window start
            if self._is_up_down and strike == 0 and self.latest_spot:
                strike = float(self.latest_spot)
                self.log.info(f"V3 Strategy: Up/Down market detected, initializing strike K={strike:.2f} from spot")

            if strike > 0:
                self.K = Price.from_str(f"{strike:.2f}")

            # Prune old books from cache to prevent "sticking" to dead markets
            if len(self._poly_books) > 10:
                self._poly_books.clear()

            if self.poly_actor:
                asset_ids = [
                    self._current_window.yes_token_id,
                    self._current_window.no_token_id,
                ]
                self.poly_actor.set_assets(asset_ids)

    # ── Settlement Override ──────────────────────────────────────

    def _on_settle_timer(self, event) -> None:
        """Override settlement to log outcomes."""
        if self.latest_spot is None:
            return

        settled = self.paper.settle_predictions(Decimal(str(self.latest_spot)))

        for pred in settled:
            self.le.trade(
                {
                    "event": "settle",
                    "direction": pred.direction,
                    "correct": pred.correct,
                    "btc_entry": float(pred.btc_entry),
                    "btc_exit": float(pred.btc_exit),
                    "entry_price": float(pred.entry_price),
                    "pnl": float(pred.pnl),
                    "balance_after": float(self.paper.balance),
                }
            )

            self.le.thinking(
                "settlement",
                {
                    "direction": pred.direction,
                    "predicted": "UP" if pred.direction == "YES" else "DOWN",
                    "actual": "UP" if pred.btc_exit > pred.btc_entry else "DOWN",
                    "correct": pred.correct,
                    "btc_move": float(pred.btc_exit - pred.btc_entry),
                    "entry_price": float(pred.entry_price),
                    "pnl": float(pred.pnl),
                    "new_balance": float(self.paper.balance),
                    "new_roi": float(
                        (self.paper.balance - self.paper.starting_balance)
                        / self.paper.starting_balance
                        * 100
                    ),
                },
            )

        if self.risk_manager:
            self.risk_manager.update_balance(self.paper.balance)

        self._refresh_market_window()

        self._settle_count += 1
        if self._settle_count % 10 == 0 or settled:
            self.log.warning(self.paper.get_scoreboard(), color=LogColor.GREEN)

    # ── Helpers ──────────────────────────────────────────────────

    def _get_poly_prices(self) -> tuple[float | None, float | None]:
        """Get Polymarket YES/NO prices as floats (for logging)."""
        if not self._current_window:
            return None, None

        yes_id = InstrumentId.from_str(f"{self._current_window.yes_token_id}.POLY")
        no_id = InstrumentId.from_str(f"{self._current_window.no_token_id}.POLY")

        # Staleness threshold: 10 seconds
        STALENESS_NS = 10_000_000_000
        now_ns = self.clock.timestamp_ns()

        if yes_id in self._poly_books and no_id in self._poly_books:
            y_book = self._poly_books[yes_id]
            n_book = self._poly_books[no_id]

            # Check if both books have been updated recently
            if (now_ns - y_book.ts_last < STALENESS_NS) and (
                now_ns - n_book.ts_last < STALENESS_NS
            ):
                if y_book.best_ask_price() > 0 and n_book.best_ask_price() > 0:
                    p_yes = float(y_book.best_ask_price())
                    p_no = float(n_book.best_ask_price())
                    # Filter out empty book signals (1.0)
                    if p_yes < 1.0 and p_no < 1.0:
                        return p_yes, p_no
            else:
                self.log.debug("WSS books stale - falling back to REST")

        # REST fallback
        if self.executor:
            yes_book = self.executor.get_order_book(self._current_window.yes_token_id)
            no_book = self.executor.get_order_book(self._current_window.no_token_id)
            if yes_book and no_book:
                p_yes = float(yes_book.best_ask)
                p_no = float(no_book.best_ask)
                # Filter out empty book signals (1.0)
                if p_yes < 1.0 and p_no < 1.0:
                    return p_yes, p_no

        return None, None

    def _get_poly_prices_decimal(self) -> tuple[Decimal | None, Decimal | None]:
        """Get Polymarket YES/NO prices as Decimals (for execution)."""
        prices = self._get_poly_prices()
        if prices[0] is not None and prices[1] is not None:
            return Decimal(str(prices[0])), Decimal(str(prices[1]))
        return None, None

    def _get_price_source(self) -> str:
        """Determine what source we're getting Polymarket prices from."""
        if not self._current_window:
            return "NONE"

        yes_id = InstrumentId.from_str(f"{self._current_window.yes_token_id}.POLY")
        if yes_id in self._poly_books:
            return "WSS"

        if self.executor:
            return "REST"

        return "SYNTH"

    def _print_scoresheet(self, side, score, fv, edge, time_left, source):
        """Compact performance scoresheet."""
        uptime = int(time.time() - self.start_time)
        total = self.paper.wins + self.paper.losses
        wr = f"{self.paper.wins / total * 100:.0f}%" if total > 0 else "N/A"

        output = [
            "",
            "=" * 50,
            f"  V3 TRADE #{self.total_trades} | Uptime: {uptime}s | Src: {source}",
            "=" * 50,
            f"  Signal:    {side} (Score: {score:+.4f})",
            f"  Fair Val:  {fv:.4f} | Edge: {edge:+.4f}",
            f"  Balance:   ${self.paper.balance:.2f} | ROI: {self._state.roi_pct:+.1f}%",
            f"  Time Rem:  {int(time_left)}s",
            f"  Record:    {self.paper.wins}W-{self.paper.losses}L ({wr}) | "
            f"{self.total_trades} trades, {self.trades_skipped} skipped",
            "=" * 50,
        ]
        self.log.warning("\n".join(output), color=LogColor.GREEN)
