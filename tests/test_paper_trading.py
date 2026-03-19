import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from src.paper_trading import (
    PaperTradingEngine,
    PaperOrder,
    PaperTradingStats,
    FillModel,
    SpreadSlippageModel,
    FillStatus,
)


class TestSpreadSlippageModel:
    def test_fill_at_mid_high_prob(self):
        """Order at or better than mid should have ~95% fill rate."""
        model = SpreadSlippageModel(fill_prob_at_mid=0.95)
        count = 0
        for _ in range(200):
            fills, _ = model.simulate_fill(
                order_price=0.50, market_price=0.50, side="YES", spread_bps=30, depth=1000
            )
            if fills:
                count += 1
        # Should be close to 95%
        assert 180 <= count <= 200

    def test_fill_far_from_market_low_prob(self):
        """Order far from market should have low fill rate."""
        model = SpreadSlippageModel(fill_prob_far_from_market=0.20)
        count = 0
        for _ in range(200):
            fills, _ = model.simulate_fill(
                order_price=0.70, market_price=0.50, side="YES", spread_bps=30, depth=1000
            )
            if fills:
                count += 1
        assert count < 80  # well below 95%


class TestPaperOrder:
    def test_default_pending(self):
        order = PaperOrder(order_id="test-1", side="YES", price=0.50, size=1.0)
        assert order.status == FillStatus.PENDING
        assert order.fill_ns == 0
        assert order.rejection_reason is None


class TestPaperTradingStats:
    def test_stats_from_empty_engine(self):
        stats = PaperTradingStats(
            total_orders=0, filled=0, rejected=0, cancelled=0, pending=0,
            fill_rate_pct=0.0, avg_fill_price=0.0, avg_market_price=0.0,
            realized_pnl=0.0, run_duration_s=0.0,
        )
        assert stats.total_orders == 0
        assert stats.fill_rate_pct == 0.0


class TestPaperTradingEngineConstruction:
    def test_default_max_concurrent(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        assert engine.max_concurrent_orders == 10

    def test_custom_max_concurrent(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(
            mock_executor, mock_feed, bench, max_concurrent_orders=5
        )
        assert engine.max_concurrent_orders == 5


class TestPaperTradingEnginePlaceOrder:
    @pytest.mark.asyncio
    async def test_place_order_accepts_valid_order(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        accepted = await engine.place_order("order-1", "YES", price=0.50, size=1.0)
        assert accepted is True
        assert "order-1" in engine._orders

    @pytest.mark.asyncio
    async def test_place_order_rejects_invalid_price_zero(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        accepted = await engine.place_order("order-1", "YES", price=0.0, size=1.0)
        assert accepted is False
        # Benchmark should have recorded a rejection
        bench.order_rejected.assert_called_once()
        # And the order should NOT be in the orders dict
        assert "order-1" not in engine._orders

    @pytest.mark.asyncio
    async def test_place_order_rejects_invalid_price_above_one(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        accepted = await engine.place_order("order-1", "YES", price=1.5, size=1.0)
        assert accepted is False

    @pytest.mark.asyncio
    async def test_place_order_records_in_benchmark(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        await engine.place_order("order-1", "YES", price=0.50, size=1.0)
        bench.order_placed.assert_called_once()
        call_args = bench.order_placed.call_args
        assert call_args[0][0] == "order-1"  # order_id


class TestPaperTradingEngineCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_pending_order(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        await engine.place_order("order-1", "YES", price=0.50, size=1.0)
        cancelled = await engine.cancel_order("order-1")
        assert cancelled is True
        assert engine._orders["order-1"].status == FillStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_unknown_order(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        cancelled = await engine.cancel_order("nonexistent")
        assert cancelled is False


class TestPaperTradingEngineStartStop:
    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        await engine.start()
        assert engine._running is True
        await engine.stop()

    @pytest.mark.asyncio
    async def test_double_start_noop(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        await engine.start()
        await engine.start()  # no-op
        assert engine._running is True
        await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        await engine.start()
        await engine.stop()
        assert engine._running is False


class TestPaperTradingEngineCallbacks:
    @pytest.mark.asyncio
    async def test_on_fill_callback(self):
        mock_executor = MagicMock()
        mock_feed = MagicMock()
        bench = MagicMock()
        engine = PaperTradingEngine(mock_executor, mock_feed, bench)
        fill_called = False

        def on_fill(order):
            nonlocal fill_called
            fill_called = True

        engine.on_fill(on_fill)
        await engine.start()
        await engine.stop()
        # Callback should be set
        assert engine._on_fill is not None


class TestFillModelInterface:
    def test_fill_model_raises_not_implemented(self):
        model = FillModel()
        with pytest.raises(NotImplementedError):
            model.simulate_fill(0.5, 0.5, "YES", 30.0, 1000.0)
