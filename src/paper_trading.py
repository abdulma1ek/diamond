"""Paper Trading Engine — live market simulation without real capital.

Wraps the PolymarketExecutor and simulates fill outcomes based on a realistic
slippage model. All orders are placed against the live CLOB market data but
fills are simulated — no real funds are transacted.

The engine:
  1. Intercepts order_placed() calls from the strategy
  2. Simulates a fill after a random delay drawn from the observed
     market conditions (spread, depth, order book pressure)
  3. Records fills/rejections with nanosecond timestamps for latency benchmarking
  4. Exposes a fill ratio tracker to detect degraded execution quality

Usage:
    engine = PaperTradingEngine(
        executor=executor,
        benchmark=latency_bench,
        slippage_model=SpreadSlippageModel(),
    )
    await engine.start()
    # Strategy calls strategy.order_placed() — engine intercepts and simulates
    await engine.stop()
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from src.polymarket_feed import PolymarketFeed
from src.latency_benchmark import LatencyBenchmark

_logger = logging.getLogger(__name__)


class FillStatus(Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    PENDING = "PENDING"


@dataclass
class FillModel:
    """Base class for fill simulation models."""

    def simulate_fill(
        self,
        order_price: float,
        market_price: float,
        side: str,  # "YES" or "NO"
        spread_bps: float,
        depth: float,
    ) -> tuple[bool, float]:
        """
        Returns (fills: bool, fill_price: float).

        Subclass this to implement custom slippage logic.
        """
        raise NotImplementedError


@dataclass
class SpreadSlippageModel(FillModel):
    """
    Fill simulation based on spread and order book depth.

    Fill probability:
      - If order_price is at or better than market mid: ~95% fill
      - If order_price is within spread: ~70% fill
      - If order_price is far from market: ~20% fill

    Fill price:
      - Always at order_price (limit order — no slippage for limit orders)
      - Market orders: price = market ± spread/2
    """

    fill_prob_at_mid: float = 0.95
    fill_prob_within_spread: float = 0.70
    fill_prob_far_from_market: float = 0.20

    def simulate_fill(
        self,
        order_price: float,
        market_price: float,
        side: str,
        spread_bps: float,
        depth: float,
    ) -> tuple[bool, float]:
        spread_pct = spread_bps / 10_000.0
        mid = market_price
        distance_from_mid = abs(order_price - mid) / mid if mid > 0 else 0.0

        # Determine fill probability
        if distance_from_mid <= spread_pct / 2:
            prob = self.fill_prob_at_mid
        elif distance_from_mid <= spread_pct:
            prob = self.fill_prob_within_spread
        else:
            prob = self.fill_prob_far_from_market

        # Reduce probability further if depth is thin
        prob *= min(1.0, depth / 1000.0)

        fills = random.random() < prob

        if not fills:
            return False, 0.0

        # Limit order fills at order_price
        fill_price = order_price
        return True, fill_price


@dataclass
class PaperOrder:
    """Represents a paper-traded order with simulation state."""

    order_id: str
    side: str  # "YES" or "NO"
    price: float
    size: float
    placed_ns: int = 0
    fill_ns: int = 0
    status: FillStatus = FillStatus.PENDING
    rejection_reason: str | None = None


@dataclass
class PaperTradingStats:
    """Aggregate statistics for a paper trading session."""

    total_orders: int
    filled: int
    rejected: int
    cancelled: int
    pending: int
    fill_rate_pct: float
    avg_fill_price: float
    avg_market_price: float
    realized_pnl: float
    run_duration_s: float


class PaperTradingEngine:
    """
    Simulates order fills against live market data without real capital.

    Args:
        executor: PolymarketExecutor (or compatible) for order operations
        feed: PolymarketFeed for live market data (spreads, depth)
        benchmark: LatencyBenchmark to record tick-to-fill metrics
        fill_model: FillModel determining fill probability and price
        max_concurrent_orders: Limit on simultaneous open paper orders
    """

    def __init__(
        self,
        executor,  # PolymarketExecutor-like
        feed: PolymarketFeed,  # PolymarketFeed for market data
        benchmark: LatencyBenchmark,
        fill_model: FillModel | None = None,
        max_concurrent_orders: int = 10,
    ):
        self.executor = executor
        self.feed = feed
        self.benchmark = benchmark
        self.fill_model = fill_model or SpreadSlippageModel()
        self.max_concurrent_orders = max_concurrent_orders

        self._orders: dict[str, PaperOrder] = {}
        self._running = False
        self._fill_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_fill: Callable[[PaperOrder], None] | None = None
        self._on_rejection: Callable[[str, str], None] | None = None  # order_id, reason

    def on_fill(self, cb: Callable[[PaperOrder], None]) -> "PaperTradingEngine":
        self._on_fill = cb
        return self

    def on_rejection(self, cb: Callable[[str, str], None]) -> "PaperTradingEngine":
        self._on_rejection = cb
        return self

    async def start(self) -> None:
        """Start the fill simulation loop."""
        self._running = True
        self._fill_task = asyncio.create_task(self._fill_loop())
        _logger.info("PaperTradingEngine started")

    async def stop(self) -> None:
        """Stop the engine and cancel pending orders."""
        self._running = False
        if self._fill_task:
            self._fill_task.cancel()
            try:
                await self._fill_task
            except asyncio.CancelledError:
                pass
        _logger.info("PaperTradingEngine stopped")

    async def _fill_loop(self) -> None:
        """
        Background loop that continuously checks pending orders for fill opportunity.

        Runs every 50ms and evaluates each pending order against current market
        conditions. Simulates random fill delay based on order size and market depth.
        """
        while self._running:
            try:
                await asyncio.sleep(0.05)  # 50ms tick
                await self._check_fills()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _logger.error(f"PaperTradingEngine fill loop error: {e}")

    async def _check_fills(self) -> None:
        """Evaluate pending orders for fill."""
        async with self._lock:
            for order_id, order in list(self._orders.items()):
                if order.status != FillStatus.PENDING:
                    continue

                # Get current market data
                market_price = self._get_market_price(order.side)
                spread_bps = self._get_spread_bps(order.side)
                depth = self._get_market_depth(order.side)

                if market_price <= 0:
                    continue

                fills, fill_price = self.fill_model.simulate_fill(
                    order_price=order.price,
                    market_price=market_price,
                    side=order.side,
                    spread_bps=spread_bps,
                    depth=depth,
                )

                if fills:
                    order.status = FillStatus.FILLED
                    order.fill_ns = time.time_ns()
                    self.benchmark.order_filled(order_id, ts_ns=order.fill_ns)
                    if self._on_fill:
                        self._on_fill(order)
                else:
                    # Simulate occasional rejection
                    if random.random() < 0.02:  # 2% random rejection rate
                        order.status = FillStatus.REJECTED
                        order.rejection_reason = "Simulated: market moved"
                        self.benchmark.order_rejected(order_id, reason=order.rejection_reason)
                        if self._on_rejection:
                            self._on_rejection(order_id, order.rejection_reason)

    def _get_market_price(self, side: str) -> float:
        """Get current market price for the given side."""
        try:
            if hasattr(self.feed, "get_best_bid_ask"):
                result = self.feed.get_best_bid_ask()
                if result:
                    _, ask = result
                    return float(ask) if side == "YES" else 1.0 - float(ask)
            if hasattr(self.feed, "last_price"):
                lp = self.feed.last_price
                return float(lp) if lp else 0.0
            return 0.5  # fallback mid price
        except Exception:
            return 0.5

    def _get_spread_bps(self, side: str) -> float:
        """Estimate current spread in basis points."""
        try:
            if hasattr(self.feed, "get_best_bid_ask"):
                result = self.feed.get_best_bid_ask()
                if result:
                    bid, ask = result
                    mid = (float(bid) + float(ask)) / 2.0
                    if mid > 0:
                        return abs(float(ask) - float(bid)) / mid * 10_000
            return 30.0  # default ~3bps (typical Polymarket spread)
        except Exception:
            return 30.0

    def _get_market_depth(self, side: str) -> float:
        """Get current order book depth (total volume at best levels)."""
        try:
            if hasattr(self.feed, "get_depth"):
                return self.feed.get_depth(side=side.lower())
            return 1000.0  # default depth
        except Exception:
            return 1000.0

    async def place_order(
        self,
        order_id: str,
        side: str,
        price: float,
        size: float,
        ts_ns: int | None = None,
    ) -> bool:
        """
        Simulate placing an order through the paper trading engine.

        Records order placement in the benchmark and starts fill simulation.
        Returns True if the order was accepted (passed basic validation).

        Args:
            order_id: Unique order identifier
            side: "YES" or "NO"
            price: Limit price
            size: Order size
            ts_ns: Placement timestamp (defaults to now)
        """
        now_ns = ts_ns or time.time_ns()

        # Basic validation
        if len(self._orders) >= self.max_concurrent_orders:
            self.benchmark.order_rejected(
                order_id,
                reason=f"Max concurrent orders ({self.max_concurrent_orders}) reached",
            )
            return False

        if price <= 0 or price > 1:
            self.benchmark.order_rejected(order_id, reason="Invalid price")
            return False

        async with self._lock:
            self._orders[order_id] = PaperOrder(
                order_id=order_id,
                side=side,
                price=price,
                size=size,
                placed_ns=now_ns,
            )

        self.benchmark.order_placed(order_id, ts_ns=now_ns)
        _logger.debug(f"Paper order placed: {order_id} {side} @ {price}")
        return True

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending paper order."""
        async with self._lock:
            if order_id in self._orders:
                order = self._orders[order_id]
                if order.status == FillStatus.PENDING:
                    order.status = FillStatus.CANCELLED
                    self.benchmark.order_cancelled(order_id)
                    return True
        return False

    def get_stats(self) -> PaperTradingStats:
        """Compute aggregate paper trading statistics."""
        total = len(self._orders)
        filled = sum(1 for o in self._orders.values() if o.status == FillStatus.FILLED)
        rejected = sum(1 for o in self._orders.values() if o.status == FillStatus.REJECTED)
        cancelled = sum(1 for o in self._orders.values() if o.status == FillStatus.CANCELLED)
        pending = sum(1 for o in self._orders.values() if o.status == FillStatus.PENDING)
        fill_rate = (filled / total * 100) if total > 0 else 0.0

        fill_prices = [o.price for o in self._orders.values() if o.status == FillStatus.FILLED]
        market_prices = [self._get_market_price(o.side) for o in self._orders.values() if o.status == FillStatus.FILLED]

        avg_fill = sum(fill_prices) / len(fill_prices) if fill_prices else 0.0
        avg_market = sum(m for m in market_prices if m > 0) / len(market_prices) if market_prices else 0.0

        # Realized PnL: sum of (fill_price - market_price) * size for YES orders
        realized_pnl = 0.0
        for o in self._orders.values():
            if o.status == FillStatus.FILLED:
                market_p = self._get_market_price(o.side)
                if o.side == "YES":
                    realized_pnl += (o.price - market_p) * o.size
                else:
                    realized_pnl += ((1 - o.price) - (1 - market_p)) * o.size

        return PaperTradingStats(
            total_orders=total,
            filled=filled,
            rejected=rejected,
            cancelled=cancelled,
            pending=pending,
            fill_rate_pct=fill_rate,
            avg_fill_price=avg_fill,
            avg_market_price=avg_market,
            realized_pnl=realized_pnl,
            run_duration_s=0.0,  # TODO: track start time
        )

    def print_stats(self) -> None:
        """Print a formatted paper trading report."""
        s = self.get_stats()
        print("\n" + "=" * 50)
        print("  PAPER TRADING REPORT")
        print("=" * 50)
        print(f"  Total orders:     {s.total_orders}")
        print(f"  Filled:          {s.filled} ({s.fill_rate_pct:.1f}%)")
        print(f"  Rejected:        {s.rejected}")
        print(f"  Cancelled:       {s.cancelled}")
        print(f"  Pending:         {s.pending}")
        print(f"  Avg fill price:  {s.avg_fill_price:.4f}")
        print(f"  Avg mkt price:   {s.avg_market_price:.4f}")
        print(f"  Realized PnL:    {s.realized_pnl:+.4f}")
        print("=" * 50 + "\n")
