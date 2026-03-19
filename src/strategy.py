import json
from collections import deque
from decimal import Decimal
from math import log, sqrt
from pathlib import Path
from typing import Deque

from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.rust.model import AggressorSide, BookType
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import (
    Bar,
    BarType,
    FundingRateUpdate,
    TradeTick,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.objects import Price, Quantity

from src.execution import PolymarketExecutor
from src.market_calibration import (
    CalibrationAdjuster,
    EVCalculator,
    HourlySignalFilter,
    LongshotBiasFilter,
)
from src.pricing import (
    calibration_adjusted_fair_value,
    fair_value_yes,
    fair_value_no,
    taker_adjusted_edge,
)
from src.risk import RiskManager


INSTRUMENT_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
BAR_TYPE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")

# Minimum edge required to consider a trade (spec.md: 5%)
MIN_EDGE: float = 0.05

# Default order size in conditional tokens
DEFAULT_ORDER_SIZE: Quantity = Quantity.from_str("10.0")

# Staleness threshold (500ms)
STALENESS_THRESHOLD_NS: int = 500_000_000

# Oracle-Lag Protection: No-Trade Zone (last 30 seconds of 5-minute window)
NO_TRADE_ZONE_SECONDS: int = 30
MARKET_WINDOW_SECONDS: int = 300

# Log paths
LATENCY_LOG_PATH = Path("logs/latency_metrics.jsonl")
TICK_TO_TRADE_LOG_PATH = Path("logs/tick_to_trade.jsonl")


class LatencyLogger:
    """
    Synchronous logger for latency metrics.

    Writes directly to disk on each call. JSON entries are small (~100 bytes)
    and append-mode writes are O(1) on modern filesystems — no measurable hot-path
    impact. This design is CLAUDE.md-compliant: no threading, no blocking sleep,
    no background tasks outside the Nautilus event loop.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_latency(self, event_type: str, ts_event_ns: int, now_ns: int) -> None:
        """Write a latency entry to the latency log. Integer nanoseconds only."""
        delta_ns = now_ns - ts_event_ns
        entry = {
            "event_type": event_type,
            "ts_event_ns": int(ts_event_ns),
            "ts_receive_ns": int(now_ns),
            "delta_ns": int(delta_ns),
        }
        self._write(entry, self.log_path)

    def log_tick_to_trade(self, entry: dict) -> None:
        """Write a tick-to-trade entry to the TTT log."""
        self._write(entry, TICK_TO_TRADE_LOG_PATH)

    def _write(self, entry: dict, path: Path) -> None:
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def stop(self) -> None:
        pass  # No background resources to release


class TickToTradeLogger:
    """
    Measures the path from market data 'tick' to execution 'trade'.
    """

    def __init__(self, logger: LatencyLogger) -> None:
        self.logger = logger
        self._current_tick_ns: int = 0
        self._current_event_type: str = ""

    def record_tick(self, event_type: str, now_ns: int) -> None:
        """Record the start of a processing cycle."""
        self._current_tick_ns = now_ns
        self._current_event_type = event_type

    def record_trade(self, now_ns: int, metadata: dict) -> None:
        """Record the completion of a trade placement."""
        if self._current_tick_ns == 0:
            return

        ttt_ns = now_ns - self._current_tick_ns
        entry = {
            "event_type": self._current_event_type,
            "ts_tick_ns": self._current_tick_ns,
            "ts_trade_ns": now_ns,
            "tick_to_trade_ns": ttt_ns,
        }
        entry.update(metadata)
        self.logger.log_tick_to_trade(entry)


class SignalGenerationStrategy(Strategy):
    """
    5-minute BTC prediction signal strategy.

    Composite signal from:
      - Order Book Imbalance (OBI) from L2 depth snapshots
      - Cumulative Volume Delta (CVD) from trade ticks
      - Realized volatility regime from 1-min bars
      - Funding rate as background context
    """

    def __init__(
        self,
        config,
        executor: PolymarketExecutor | None = None,
        polymarket_token_id: str | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        super().__init__(config=config)

        # Execution
        self.executor: PolymarketExecutor | None = executor
        self.polymarket_token_id: str | None = polymarket_token_id

        # Risk management
        self.risk_manager: RiskManager | None = risk_manager

        # Latency logging (non-blocking)
        self.latency_logger = LatencyLogger(LATENCY_LOG_PATH)
        self.ttt_logger = TickToTradeLogger(self.latency_logger)

        # Tier 1: Order Book Imbalance (Decimal for high-precision ratios)
        self.latest_obi: Decimal = Decimal("0.0")

        # Tier 1: CVD — rolling window of (timestamp_ns, signed_volume)
        # Using Decimal for volume to prevent float leaks
        self.trade_deltas: Deque[tuple[int, Decimal]] = deque(maxlen=5000)
        self.cvd_window_ns: int = 60_000_000_000  # 60 seconds

        # Tier 2: Realized volatility from 1-min bar closes (Price objects)
        self.bar_closes: Deque[Price] = deque(maxlen=10)
        self.realized_vol: float = 0.0

        # Tier 3: Funding rate (context only)
        self.latest_funding_rate: Decimal = Decimal("0.0")

        # ── Calibration & Market Analysis (prediction-market-analysis integration) ──
        # CalibrationAdjuster: blends BS fair value with empirical Polymarket calibration curve.
        # LongshotBiasFilter: raises min edge in <15% / >85% price territory.
        # HourlySignalFilter: scales signal by UTC hour (US market hours get higher weight).
        # EVCalculator: formal EV = win_rate - market_price used for final edge gate.
        self.calibration_adjuster: CalibrationAdjuster = CalibrationAdjuster(alpha=0.3)
        self.longshot_filter: LongshotBiasFilter = LongshotBiasFilter()
        self.hourly_filter: HourlySignalFilter = HourlySignalFilter()

        # Signal weights
        self.w_obi: Decimal = Decimal("0.60")
        self.w_cvd: Decimal = Decimal("0.30")
        self.w_funding: Decimal = Decimal("0.10")

        # Thresholds
        self.high_vol_threshold: float = 0.80  # annualized
        self.signal_threshold_low_vol: Decimal = Decimal("0.25")
        self.signal_threshold_high_vol: Decimal = Decimal("0.15")

        # Black-Scholes time parameter (5 minutes in years)
        self.T: float = 5.0 / 525600.0

        # Latest spot price from trade ticks (for pricing engine)
        self.latest_spot: Price | None = None
        self.latest_feed_ts: int = 0

    def on_start(self) -> None:
        self.log.info("SignalGenerationStrategy starting...")

        self.subscribe_order_book_at_interval(
            INSTRUMENT_ID,
            book_type=BookType.L2_MBP,
            depth=10,
            interval_ms=1000,
        )
        self.subscribe_trade_ticks(INSTRUMENT_ID)
        self.subscribe_bars(BAR_TYPE)

    def on_stop(self) -> None:
        self.log.info("SignalGenerationStrategy stopped.")
        self.latency_logger.stop()

    # ── Data Handlers ──────────────────────────────────────────────

    def on_order_book(self, order_book: OrderBook) -> None:
        now_ns = self.clock.timestamp_ns()
        self.latency_logger.log_latency("order_book", order_book.ts_event, now_ns)
        self.ttt_logger.record_tick("order_book", now_ns)

        bids = order_book.bids()
        asks = order_book.asks()
        
        # Preservation of Quantity precision using Decimal
        total_bid_vol = sum(Decimal(str(level.size())) for level in bids)
        total_ask_vol = sum(Decimal(str(level.size())) for level in asks)
        
        denom = total_bid_vol + total_ask_vol
        if denom > 0:
            self.latest_obi = (total_bid_vol - total_ask_vol) / denom
        
        self.latest_feed_ts = order_book.ts_event
        self._evaluate_composite_signal()

    def on_trade_tick(self, tick: TradeTick) -> None:
        now_ns = self.clock.timestamp_ns()
        self.latency_logger.log_latency("trade_tick", tick.ts_event, now_ns)
        self.ttt_logger.record_tick("trade_tick", now_ns)

        self.latest_spot = tick.price
        self.latest_feed_ts = tick.ts_event
        
        # Preservation of Quantity precision using Decimal
        size = Decimal(str(tick.size))
        if tick.aggressor_side == AggressorSide.BUYER:
            self.trade_deltas.append((tick.ts_event, size))
        elif tick.aggressor_side == AggressorSide.SELLER:
            self.trade_deltas.append((tick.ts_event, -size))

        if self.risk_manager:
            self.risk_manager.update_heartbeat(tick.ts_event)

    def on_bar(self, bar: Bar) -> None:
        now_ns = self.clock.timestamp_ns()
        self.latency_logger.log_latency("bar", bar.ts_event, now_ns)
        self.ttt_logger.record_tick("bar", now_ns)

        self.bar_closes.append(bar.close) # Preserving Price object
        self.latest_feed_ts = bar.ts_event
        if len(self.bar_closes) >= 2:
            self._compute_realized_vol()
        self._evaluate_composite_signal()

    def on_funding_rate(self, update: FundingRateUpdate) -> None:
        now_ns = self.clock.timestamp_ns()
        self.latency_logger.log_latency("funding_rate", update.ts_event, now_ns)

        self.latest_funding_rate = Decimal(str(update.rate))
        self.latest_feed_ts = update.ts_event

    # ── Computation ────────────────────────────────────────────────

    def _is_data_stale(self) -> bool:
        """Check if the latest data timestamp is older than the threshold. Panics if stale."""
        now_ns = self.clock.timestamp_ns()
        latency = now_ns - self.latest_feed_ts
        if latency > STALENESS_THRESHOLD_NS:
            msg = f"CRITICAL STALENESS: Latency is {latency / 1_000_000:.2f}ms > {STALENESS_THRESHOLD_NS / 1_000_000:.2f}ms"
            self.log.critical(msg)
            self.panic(msg)
            return True
        return False

    def _is_in_no_trade_zone(self) -> bool:
        """Check if we are in the last 30 seconds of the 5-minute market window."""
        now_ns = self.clock.timestamp_ns()
        seconds_into_window = (now_ns // 1_000_000_000) % MARKET_WINDOW_SECONDS
        if seconds_into_window >= (MARKET_WINDOW_SECONDS - NO_TRADE_ZONE_SECONDS):
            return True
        return False

    def _compute_cvd(self) -> Decimal:
        """CVD over the rolling window, normalized to [-1, 1]."""
        now_ns = self.clock.timestamp_ns()
        cutoff = now_ns - self.cvd_window_ns
        total_buy = Decimal("0")
        total_sell = Decimal("0")
        
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
            return Decimal("0")
        return (total_buy - total_sell) / total

    def _compute_realized_vol(self) -> None:
        """Annualized realized vol from 1-min bar closes (Price objects)."""
        # Snapshot Prices
        closes = [float(p) for p in list(self.bar_closes)]
        if len(closes) < 2:
            return
        log_returns = [log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        mean_ret = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
        self.realized_vol = sqrt(variance) * sqrt(525600)

    def _evaluate_composite_signal(self) -> None:
        """Evaluate weighted composite signal and log if threshold exceeded."""
        if self._is_data_stale():
            return

        if len(self.bar_closes) < 2:
            return

        cvd_normalized = self._compute_cvd()

        # Funding bias: positive funding → bearish, negative → bullish
        funding_bias = Decimal(str(max(-1.0, min(1.0, float(-self.latest_funding_rate) * 10000))))

        signal_score = (
            self.w_obi * self.latest_obi
            + self.w_cvd * cvd_normalized
            + self.w_funding * funding_bias
        )

        # Volatility regime determines threshold
        if self.realized_vol > self.high_vol_threshold:
            threshold = self.signal_threshold_high_vol
        else:
            threshold = self.signal_threshold_low_vol

        # Apply hourly signal quality multiplier (returns_by_hour analysis).
        # US market hours (14:00-21:00 UTC) get multiplier > 1.0; quiet hours < 1.0.
        hourly_multiplier = self.hourly_filter.get_multiplier()
        adjusted_score = self.hourly_filter.apply(signal_score)

        if abs(adjusted_score) > threshold:
            direction = "YES" if adjusted_score > 0 else "NO"
            self.log.info(
                f"SIGNAL PASSED | raw={signal_score:+.4f} "
                f"hourly_mult={hourly_multiplier:.2f} adj={adjusted_score:+.4f}"
            )
            self._price_and_log(direction, adjusted_score)

    def _price_and_log(self, direction: str, signal_score: Decimal) -> None:
        """Compute fair value, apply calibration correction, check edge, and optionally place order.

        Integration with prediction-market-analysis framework:
        1. Raw BS fair value is computed (fair_value_yes / fair_value_no).
        2. CalibrationAdjuster applies the empirical Polymarket calibration curve correction
           (longshot bias shades fair value down at extreme prices).
        3. LongshotBiasFilter raises the minimum edge threshold in <15%/>85% price zones.
        4. EVCalculator computes formal EV = calibrated_fv - market_price as final gate.
        5. taker_adjusted_edge adds fee deduction on top of the EV check.
        """
        if self.latest_spot is None or self.realized_vol <= 0:
            return

        # Step 1: Raw Black-Scholes fair value
        fv_yes_raw = fair_value_yes(
            S=self.latest_spot,
            K=self.latest_spot,
            sigma=self.realized_vol,
            T=self.T,
        )

        # Fetch Polymarket midpoint
        market_prob: Decimal | None = None
        if self.executor and self.polymarket_token_id:
            market_prob = self.executor.get_midpoint(self.polymarket_token_id)

        if market_prob is not None:
            target_market = market_prob if direction == "YES" else Decimal("1.0") - market_prob
            target_market_float = float(target_market)

            # Step 2: Apply empirical calibration correction (longshot bias).
            # Uses polymarket_win_rate_by_price curve to adjust toward observed win rates.
            fv_yes_cal = calibration_adjusted_fair_value(
                raw_fv=fv_yes_raw,
                market_price=market_prob,
                adjuster=self.calibration_adjuster,
            )
            if direction == "YES":
                target_fv = fv_yes_cal
            else:
                fv_no_cal = calibration_adjusted_fair_value(
                    raw_fv=fair_value_no(self.latest_spot, self.latest_spot, self.realized_vol),
                    market_price=Decimal("1.0") - market_prob,
                    adjuster=self.calibration_adjuster,
                )
                target_fv = fv_no_cal

            # Step 3: Compute EV (prediction-market-analysis ev_yes_vs_no framework).
            # EV = calibrated_win_prob - market_price (positive = profitable edge).
            ev = EVCalculator.ev_yes(float(target_fv), target_market_float)

            # Step 4: Longshot bias filter — raise min edge in extreme price zones.
            # From win_rate_by_price: prices <15% and >85% require 50% more edge.
            effective_min_edge = self.longshot_filter.adjusted_min_edge(
                base_min_edge=MIN_EDGE,
                market_price_prob=target_market_float,
            )
            is_longshot = self.longshot_filter.is_longshot(target_market_float)
            calib_bias = self.calibration_adjuster.calibration_bias(target_market_float)

            # Step 5: Fee-adjusted edge gate.
            adj_edge = taker_adjusted_edge(
                fv=target_fv,
                market_price=target_market,
                min_edge=effective_min_edge,
            )

            self.log.info(
                f"SIGNAL: {direction} | score={signal_score:+.4f} | "
                f"fv_raw={fv_yes_raw} fv_cal={target_fv} mkt={target_market} | "
                f"ev={ev:+.4f} calib_bias={calib_bias:+.4f} "
                f"longshot={is_longshot} min_edge={effective_min_edge:.3f} adj_edge={adj_edge:+.4f}"
            )

            if adj_edge > 0 and ev > 0:
                # Oracle-Lag Protection Check
                if self._is_in_no_trade_zone():
                    self.log.warning("SIGNAL BLOCKED: Within 30s No-Trade Zone (Oracle Lag Protection)")
                    return

                self._place_order(direction, adj_edge, target_market, fv_yes_cal)
        else:
            self.log.warning(f"SIGNAL: {direction} | No market data from Polymarket")

    def _place_order(
        self,
        direction: str,
        adj_edge: Decimal,
        market_prob: Decimal,
        fv_yes: Price,
    ) -> None:
        """Place a limit order on Polymarket when edge is sufficient."""
        if not self.executor or not self.polymarket_token_id:
            return

        # Risk gate
        if self.risk_manager:
            now_ns = self.clock.timestamp_ns()
            if not self.risk_manager.can_trade(now_ns):
                self.log.warning("ORDER BLOCKED: risk manager halted trading")
                return

        # Kelly sizing (fv_yes is a calibration-adjusted Price; convert via float to avoid str(Price) ambiguity)
        win_prob = Decimal(str(float(fv_yes) if direction == "YES" else 1.0 - float(fv_yes)))
        order_price_val = market_prob.quantize(Decimal("0.01"))
        
        if self.risk_manager:
            size_val = self.risk_manager.kelly_size(
                win_prob=win_prob,
                win_payout=Decimal("1.0") - order_price_val,
                loss_amount=order_price_val,
            )
            if size_val <= Decimal("0"):
                return
            size = Quantity.from_str(f"{size_val:.4f}")
        else:
            size = DEFAULT_ORDER_SIZE

        # Execution
        side = "BUY" if direction == "YES" else "SELL"
        price = Price.from_str(f"{order_price_val:.2f}")
        
        result = self.executor.place_limit_order(
            token_id=self.polymarket_token_id,
            side=side,
            price=price,
            size=size,
        )

        if result.success:
            now_ns = self.clock.timestamp_ns()
            self.ttt_logger.record_trade(
                now_ns=now_ns,
                metadata={
                    "side": side,
                    "price": str(price),
                    "size": str(size),
                    "adj_edge": str(adj_edge),
                }
            )
            self.log.warning(
                f"ORDER PLACED: {side} {size}@{price} | adj_edge={adj_edge:.4f}"
            )
