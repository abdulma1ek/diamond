import pytest
import time
from src.latency_benchmark import (
    LatencyBenchmark,
    LatencyStats,
    OrderLifecycle,
    StageLatencies,
    _percentile,
    _compute_stage_latencies,
    LatencyStageError,
)


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 0.5) == 0.0

    def test_single_value(self):
        assert _percentile([100.0], 0.5) == 100.0
        assert _percentile([100.0], 0.99) == 100.0

    def test_median(self):
        vals = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
        assert _percentile(vals, 0.5) == 30.0

    def test_90th(self):
        vals = list(range(1, 101))  # 1..100
        p = _percentile(sorted(vals), 0.90)
        assert 89 <= p <= 91

    def test_interpolates(self):
        vals = [0.0, 100.0]
        # At 0.25: 0.0 * 0.75 + 100.0 * 0.25 = 25.0
        assert _percentile(vals, 0.25) == 25.0


class TestComputeStageLatencies:
    def test_empty(self):
        lat = _compute_stage_latencies([])
        assert lat.count == 0
        assert lat.p50_us == 0

    def test_single_value(self):
        lat = _compute_stage_latencies([1_000_000])  # 1ms = 1000µs
        assert lat.count == 1
        assert lat.min_us == 1000.0
        assert lat.max_us == 1000.0
        assert lat.p50_us == 1000.0

    def test_sorted(self):
        lat = _compute_stage_latencies([100_000, 200_000, 300_000])  # µs: 100, 200, 300
        assert lat.min_us == 100.0
        assert lat.max_us == 300.0
        assert lat.p50_us == 200.0  # median


class TestLatencyBenchmarkConstruction:
    def test_initial_state(self):
        bench = LatencyBenchmark()
        s = bench.compute_stats()
        assert s.total_orders == 0
        assert s.filled_orders == 0
        assert s.rejected_orders == 0
        assert s.tick_to_signal.count == 0

    def test_default_window_size(self):
        bench = LatencyBenchmark()
        assert bench._window_size == 1000


class TestTickAndSignal:
    def test_tick_returns_timestamp(self):
        bench = LatencyBenchmark()
        ts = bench.tick()
        assert ts > 0

    def test_signal_records_latency(self):
        bench = LatencyBenchmark()
        tick_ts = bench.tick()
        time.sleep(0.001)  # 1ms
        bench.signal(tick_ts_ns=tick_ts)
        stats = bench.compute_stats()
        assert stats.tick_to_signal.count == 1
        # Should be at least 1ms
        assert stats.tick_to_signal.min_us >= 900  # ~1000µs

    def test_signal_with_order_id(self):
        bench = LatencyBenchmark()
        tick_ts = bench.tick()
        bench.signal(tick_ts_ns=tick_ts, order_id="order-1")
        stats = bench.compute_stats()
        # signal() creates the OrderLifecycle but total_orders only increments at order_placed
        assert stats.tick_to_signal.count == 1


class TestOrderLifecycle:
    def test_full_lifecycle(self):
        bench = LatencyBenchmark()
        tick_ts = bench.tick()
        bench.signal(tick_ts_ns=tick_ts, order_id="order-1")
        bench.order_placed(order_id="order-1")
        bench.order_acked(order_id="order-1")
        bench.order_filled(order_id="order-1")
        stats = bench.compute_stats()
        assert stats.filled_orders == 1
        assert stats.tick_to_fill.count == 1

    def test_order_without_signal(self):
        """Order placed without a signal — should still be tracked."""
        bench = LatencyBenchmark()
        bench.order_placed(order_id="order-1")
        stats = bench.compute_stats()
        assert stats.total_orders == 1
        assert stats.signal_to_place.count == 0  # no signal to place from

    def test_order_rejected(self):
        bench = LatencyBenchmark()
        bench.order_placed(order_id="order-1")
        bench.order_rejected(order_id="order-1", reason="Insufficient margin")
        stats = bench.compute_stats()
        assert stats.rejected_orders == 1
        assert stats.filled_orders == 0

    def test_order_cancelled(self):
        bench = LatencyBenchmark()
        bench.order_placed(order_id="order-1")
        bench.order_cancelled(order_id="order-1")
        stats = bench.compute_stats()
        assert stats.cancelled_orders == 1
        assert stats.filled_orders == 0


class TestLatencyStats:
    def test_all_zeros_on_empty(self):
        s = LatencyStats(
            tick_to_signal=StageLatencies(0,0,0,0,0,0,0),
            signal_to_place=StageLatencies(0,0,0,0,0,0,0),
            place_to_ack=StageLatencies(0,0,0,0,0,0,0),
            ack_to_fill=StageLatencies(0,0,0,0,0,0,0),
            tick_to_fill=StageLatencies(0,0,0,0,0,0,0),
            total_orders=0,
            filled_orders=0,
            rejected_orders=0,
            cancelled_orders=0,
            p50_us=0, p90_us=0, p99_us=0, p999_us=0,
            run_duration_s=0.0,
            orders_per_second=0.0,
        )
        assert s.filled_orders == 0


class TestReset:
    def test_reset_clears_all(self):
        bench = LatencyBenchmark()
        tick_ts = bench.tick()
        bench.signal(tick_ts_ns=tick_ts, order_id="order-1")
        bench.order_placed(order_id="order-1")
        bench.order_acked(order_id="order-1")
        bench.order_filled(order_id="order-1")
        bench.reset()
        s = bench.compute_stats()
        assert s.total_orders == 0
        assert s.filled_orders == 0
        assert s.tick_to_signal.count == 0


class TestOrderSequence:
    def test_ack_before_placed_warns(self):
        bench = LatencyBenchmark()
        # ack without placed should not crash
        bench.order_acked(order_id="order-1")
        s = bench.compute_stats()
        assert s.total_orders == 0  # acked but never placed

    def test_fill_before_placed_warns(self):
        bench = LatencyBenchmark()
        bench.order_filled(order_id="order-1")
        s = bench.compute_stats()
        assert s.filled_orders == 0  # never placed


class TestWindowSize:
    def test_window_enforced(self):
        bench = LatencyBenchmark(window_size=5)
        tick_ts = bench.tick()
        for i in range(10):
            bench.signal(tick_ts_ns=tick_ts + i * 1_000_000, order_id=f"order-{i}")
        # Only last 5 should remain
        s = bench.compute_stats()
        assert s.tick_to_signal.count == 5
