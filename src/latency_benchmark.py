"""Tick-to-Trade latency benchmarking.

Tracks end-to-end latency at every stage of the order lifecycle:

  tick_received → signal_generated → order_placed → order_acked → order_filled

Each stage records a timestamp, and at the end of the run (or periodically)
computes P50/P90/P99/P99.9 statistics. Results are logged and exportable.

Usage:
    bench = LatencyBenchmark()
    bench.tick(ts_ns=1699999999999999999)
    # ... signal computed ...
    bench.signal(ts_ns=...)
    bench.order_placed(order_id="order-123", ts_ns=...)
    bench.order_acked(order_id="order-123", ts_ns=...)
    bench.order_filled(order_id="order-123", ts_ns=...)

    stats = bench.compute_stats()
    print(f"P99 tick->signal: {stats['tick_to_signal_p99_us']}µs")
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import DefaultDict

_logger = logging.getLogger(__name__)


# ─── Exceptions ────────────────────────────────────────────────────────────────


class LatencyStageError(Exception):
    """Raised when an event arrives out of expected order."""


# ─── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class OrderLifecycle:
    """Complete lifecycle of a single order through the system."""

    order_id: str
    tick_received_ns: int = 0
    signal_generated_ns: int = 0
    order_placed_ns: int = 0
    order_acked_ns: int = 0
    order_filled_ns: int = 0
    cancelled: bool = False
    rejected: bool = False
    rejection_reason: str | None = None


@dataclass
class StageLatencies:
    """Latency statistics for a single stage."""

    p50_us: float
    p90_us: float
    p99_us: float
    p999_us: float
    min_us: float
    max_us: float
    count: int


@dataclass
class LatencyStats:
    """Full benchmark statistics across all stages."""

    tick_to_signal: StageLatencies
    signal_to_place: StageLatencies
    place_to_ack: StageLatencies
    ack_to_fill: StageLatencies
    tick_to_fill: StageLatencies  # end-to-end
    total_orders: int
    filled_orders: int
    rejected_orders: int
    cancelled_orders: int
    p50_us: float
    p90_us: float
    p99_us: float
    p999_us: float
    run_duration_s: float
    orders_per_second: float


# ─── Percentile helpers ───────────────────────────────────────────────────────


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Compute the p-th percentile of a sorted list. p is 0.0–1.0."""
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * p
    lower = int(idx)
    upper = lower + 1
    frac = idx - lower
    if upper >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lower] * (1 - frac) + sorted_vals[upper] * frac


def _compute_stage_latencies(values_ns: list[int]) -> StageLatencies:
    """Compute percentiles from a list of nanosecond deltas."""
    if not values_ns:
        return StageLatencies(p50_us=0, p90_us=0, p99_us=0, p999_us=0, min_us=0, max_us=0, count=0)
    vals_us = sorted(v / 1000.0 for v in values_ns)
    return StageLatencies(
        p50_us=_percentile(vals_us, 0.50),
        p90_us=_percentile(vals_us, 0.90),
        p99_us=_percentile(vals_us, 0.99),
        p999_us=_percentile(vals_us, 0.999),
        min_us=vals_us[0],
        max_us=vals_us[-1],
        count=len(vals_us),
    )


# ─── Benchmark ────────────────────────────────────────────────────────────────


class LatencyBenchmark:
    """
    Tracks tick-to-trade latency across the full order lifecycle.

    Latency stages (in microseconds):

      Stage 1 — tick_to_signal:
        Time from receiving a raw market tick (Binance WebSocket message)
        to generating a composite signal score.

      Stage 2 — signal_to_place:
        Time from signal generation to submitting the order to Polymarket
        via the executor.

      Stage 3 — place_to_ack:
        Time from order placement to receiving the acknowledgment from
        Polymarket CLOB.

      Stage 4 — ack_to_fill:
        Time from order acknowledgment to the fill confirmation.

      Stage 5 — tick_to_fill:
        End-to-end: tick received → filled (total roundtrip).

    The benchmark uses a sliding window of completed orders to compute
    running statistics without accumulating unbounded memory.

    Args:
        window_size: Number of most-recent orders to use for stats.
                     None = use all orders (higher memory use).
    """

    def __init__(self, window_size: int | None = 1000):
        self._window_size = window_size

        # Per-order tracking
        self._orders: dict[str, OrderLifecycle] = {}
        self._order_sequence: list[str] = []  # insertion order for window eviction

        # Stage metrics (in ns, converted to µs at stats time)
        self._tick_to_signal_ns: list[int] = []
        self._signal_to_place_ns: list[int] = []
        self._place_to_ack_ns: list[int] = []
        self._ack_to_fill_ns: list[int] = []
        self._tick_to_fill_ns: list[int] = []

        # Global counters
        self._total_orders = 0
        self._filled_orders = 0
        self._rejected_orders = 0
        self._cancelled_orders = 0
        self._start_unix = time.time()
        self._tick_count = 0

        # Sliding window enforcement
        self._tick_to_signal_buffer: list[int] = []

    def _ns_time(self) -> int:
        """Current time in nanoseconds."""
        return time.time_ns()

    def _add_windowed(self, buffer: list[int], value: int) -> None:
        """Add value to a windowed buffer with LRU eviction."""
        buffer.append(value)
        if self._window_size and len(buffer) > self._window_size:
            buffer.pop(0)

    def tick(self, ts_ns: int | None = None) -> int:
        """Record a tick arrival timestamp. Returns the tick ts_ns."""
        self._tick_count += 1
        now_ns = ts_ns or self._ns_time()
        return now_ns

    def signal(
        self,
        tick_ts_ns: int,
        ts_ns: int | None = None,
        order_id: str | None = None,
    ) -> None:
        """
        Record signal generation for the current tick.

        Args:
            tick_ts_ns: Timestamp when the triggering tick was received.
            ts_ns: Actual signal generation timestamp (defaults to now).
            order_id: If a tradeable signal, pass the order_id to continue tracking.
        """
        signal_ns = ts_ns or self._ns_time()
        latency_ns = signal_ns - tick_ts_ns
        self._add_windowed(self._tick_to_signal_ns, latency_ns)

        if order_id:
            self._orders[order_id] = OrderLifecycle(
                order_id=order_id,
                tick_received_ns=tick_ts_ns,
                signal_generated_ns=signal_ns,
            )
            self._order_sequence.append(order_id)

    def order_placed(
        self,
        order_id: str,
        ts_ns: int | None = None,
    ) -> None:
        """Record order placement timestamp."""
        now_ns = ts_ns or self._ns_time()
        if order_id not in self._orders:
            # Order placed without a preceding signal — still track it
            self._orders[order_id] = OrderLifecycle(order_id=order_id)
            self._order_sequence.append(order_id)

        order = self._orders[order_id]
        order.order_placed_ns = now_ns

        if order.signal_generated_ns:
            latency_ns = now_ns - order.signal_generated_ns
            self._add_windowed(self._signal_to_place_ns, latency_ns)
        self._total_orders += 1

    def order_acked(
        self,
        order_id: str,
        ts_ns: int | None = None,
    ) -> None:
        """Record order acknowledgment from the CLOB."""
        now_ns = ts_ns or self._ns_time()
        if order_id not in self._orders:
            _logger.warning(f"order_acked for unknown order {order_id}")
            return
        self._orders[order_id].order_acked_ns = now_ns

        placed_ns = self._orders[order_id].order_placed_ns
        if placed_ns:
            self._add_windowed(self._place_to_ack_ns, now_ns - placed_ns)

    def order_filled(
        self,
        order_id: str,
        ts_ns: int | None = None,
    ) -> None:
        """Record order fill confirmation."""
        now_ns = ts_ns or self._ns_time()
        if order_id not in self._orders:
            _logger.warning(f"order_filled for unknown order {order_id}")
            return
        order = self._orders[order_id]
        order.order_filled_ns = now_ns

        # place → ack
        if order.order_placed_ns and order.order_acked_ns:
            self._add_windowed(self._place_to_ack_ns, order.order_acked_ns - order.order_placed_ns)
        # ack → fill
        if order.order_acked_ns:
            self._add_windowed(self._ack_to_fill_ns, now_ns - order.order_acked_ns)
        # tick → fill (end-to-end)
        if order.tick_received_ns:
            self._add_windowed(self._tick_to_fill_ns, now_ns - order.tick_received_ns)

        self._filled_orders += 1

    def order_rejected(
        self,
        order_id: str,
        reason: str,
        ts_ns: int | None = None,
    ) -> None:
        """Record order rejection."""
        now_ns = ts_ns or self._ns_time()
        if order_id in self._orders:
            self._orders[order_id].rejected = True
            self._orders[order_id].rejection_reason = reason
        self._rejected_orders += 1
        _logger.warning(f"Order {order_id} rejected: {reason}")

    def order_cancelled(
        self,
        order_id: str,
        ts_ns: int | None = None,
    ) -> None:
        """Record order cancellation."""
        now_ns = ts_ns or self._ns_time()
        if order_id in self._orders:
            self._orders[order_id].cancelled = True
        self._cancelled_orders += 1

    def compute_stats(self) -> LatencyStats:
        """Compute and return full latency statistics."""
        run_duration_s = time.time() - self._start_unix

        t2s = _compute_stage_latencies(self._tick_to_signal_ns)
        s2p = _compute_stage_latencies(self._signal_to_place_ns)
        p2a = _compute_stage_latencies(self._place_to_ack_ns)
        a2f = _compute_stage_latencies(self._ack_to_fill_ns)
        t2f = _compute_stage_latencies(self._tick_to_fill_ns)

        # Overall order latency distribution (tick_to_fill for each filled order)
        all_order_lats = sorted(
            v / 1000.0 for v in self._tick_to_fill_ns
        )
        if all_order_lats:
            overall_p50 = _percentile(all_order_lats, 0.50)
            overall_p90 = _percentile(all_order_lats, 0.90)
            overall_p99 = _percentile(all_order_lats, 0.99)
            overall_p999 = _percentile(all_order_lats, 0.999)
        else:
            overall_p50 = overall_p90 = overall_p99 = overall_p999 = 0.0

        orders_per_second = self._filled_orders / run_duration_s if run_duration_s > 0 else 0.0

        return LatencyStats(
            tick_to_signal=t2s,
            signal_to_place=s2p,
            place_to_ack=p2a,
            ack_to_fill=a2f,
            tick_to_fill=t2f,
            total_orders=self._total_orders,
            filled_orders=self._filled_orders,
            rejected_orders=self._rejected_orders,
            cancelled_orders=self._cancelled_orders,
            p50_us=overall_p50,
            p90_us=overall_p90,
            p99_us=overall_p99,
            p999_us=overall_p999,
            run_duration_s=run_duration_s,
            orders_per_second=orders_per_second,
        )

    def print_report(self) -> None:
        """Print a formatted latency benchmark report."""
        s = self.compute_stats()
        print("\n" + "=" * 60)
        print("  LATENCY BENCHMARK REPORT")
        print("=" * 60)
        print(f"\n  Run duration:       {s.run_duration_s:.1f}s")
        print(f"  Total orders:       {s.total_orders}")
        print(f"  Filled orders:     {s.filled_orders}")
        print(f"  Rejected orders:   {s.rejected_orders}")
        print(f"  Cancelled orders:  {s.cancelled_orders}")
        print(f"  Throughput:         {s.orders_per_second:.2f} fills/s")

        print(f"\n  Latency Stages (P50 / P90 / P99 / P99.9):")
        print(f"    tick → signal:    {s.tick_to_signal.p50_us:>8.1f} / {s.tick_to_signal.p90_us:>8.1f} / {s.tick_to_signal.p99_us:>8.1f} / {s.tick_to_signal.p999_us:>8.1f} µs")
        print(f"    signal → place:   {s.signal_to_place.p50_us:>8.1f} / {s.signal_to_place.p90_us:>8.1f} / {s.signal_to_place.p99_us:>8.1f} / {s.signal_to_place.p999_us:>8.1f} µs")
        print(f"    place → ack:      {s.place_to_ack.p50_us:>8.1f} / {s.place_to_ack.p90_us:>8.1f} / {s.place_to_ack.p99_us:>8.1f} / {s.place_to_ack.p999_us:>8.1f} µs")
        print(f"    ack → fill:       {s.ack_to_fill.p50_us:>8.1f} / {s.ack_to_fill.p90_us:>8.1f} / {s.ack_to_fill.p99_us:>8.1f} / {s.ack_to_fill.p999_us:>8.1f} µs")

        print(f"\n  End-to-End (tick → fill):")
        print(f"    P50:   {s.p50_us:>8.1f} µs")
        print(f"    P90:   {s.p90_us:>8.1f} µs")
        print(f"    P99:   {s.p99_us:>8.1f} µs")
        print(f"    P99.9: {s.p999_us:>8.1f} µs")
        print(f"    Min:   {s.tick_to_fill.min_us:>8.1f} µs")
        print(f"    Max:   {s.tick_to_fill.max_us:>8.1f} µs")
        print("\n" + "=" * 60)

    def reset(self) -> None:
        """Reset all counters and tracked orders."""
        self._orders.clear()
        self._order_sequence.clear()
        self._tick_to_signal_ns.clear()
        self._signal_to_place_ns.clear()
        self._place_to_ack_ns.clear()
        self._ack_to_fill_ns.clear()
        self._tick_to_fill_ns.clear()
        self._tick_to_signal_buffer.clear()
        self._total_orders = 0
        self._filled_orders = 0
        self._rejected_orders = 0
        self._cancelled_orders = 0
        self._start_unix = time.time()
        self._tick_count = 0
