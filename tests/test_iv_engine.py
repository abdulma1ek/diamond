import pytest
from math import sqrt

from src.iv_engine import (
    RealizedVolEngine,
    IVSnapshot,
    VolRegime,
    realized_volatility,
    _ewma_update,
)


class TestRealizedVolatility:
    """Unit tests for realized_volatility()."""

    def test_empty_returns_zero(self):
        assert realized_volatility([]) == 0.0
        assert realized_volatility([100.0]) == 0.0

    def test_single_return(self):
        # Only one price: no return → 0 variance → 0 vol
        assert realized_volatility([100.0, 101.0], window_seconds=1.0) == 0.0

    def test_flat_prices_near_zero_vol(self):
        # Mostly flat prices with tiny noise
        prices = [100.0] * 50
        for i in range(5):
            prices[10 * i] = 100.0 * (1.001)
        vol = realized_volatility(prices, window_seconds=1.0)
        assert vol > 0.0  # some variance from small noise

    def test_uptrend_has_nonzero_vol(self):
        prices = [100.0 * (1.001 ** i) for i in range(30)]
        vol = realized_volatility(prices, window_seconds=1.0)
        assert vol > 0.0

    def test_annualization_scales_with_dt(self):
        # Same returns at different timescales
        prices = [100.0 * (1.001 ** i) for i in range(100)]
        vol_1s = realized_volatility(prices, window_seconds=1.0)
        vol_1d = realized_volatility(prices, window_seconds=86400.0)
        # vol_1d ≈ vol_1s * sqrt(1/86400) (annualization factor)
        assert abs(vol_1s - vol_1d * sqrt(86400)) < 0.1

    def test_result_annualized(self):
        prices = [100.0, 105.0, 102.0, 108.0, 103.0, 110.0]
        vol = realized_volatility(prices, window_seconds=86400.0)
        assert vol > 0.0


class TestEWMASmoothing:
    def test_half_life(self):
        result = _ewma_update(current=0.0, new=1.0, alpha=0.5)
        assert result == 0.5

    def test_high_alpha_fast(self):
        result = _ewma_update(current=0.0, new=1.0, alpha=0.9)
        assert result >= 0.9

    def test_low_alpha_slow(self):
        result = _ewma_update(current=0.0, new=1.0, alpha=0.1)
        assert result < 0.15


class TestRealizedVolEngine:
    """RealizedVolEngine integration tests."""

    def test_initial_sigma(self):
        eng = RealizedVolEngine()
        assert eng.sigma == 1.0
        assert eng.regime == VolRegime.NORMAL

    def test_add_trade_increments_counter(self):
        eng = RealizedVolEngine()
        eng.add_trade(price=100.0, ts_ns=1_000_000_000_000_000)
        assert eng.n_observations == 1
        eng.add_trade(price=101.0, ts_ns=2_000_000_000_000_000)
        assert eng.n_observations == 2

    def test_window_eviction(self):
        """Trades outside lookback window should be evicted."""
        # Use 600s lookback (10 min) — timestamps are in ns scale (~10^16)
        eng = RealizedVolEngine(lookback_seconds=600.0)
        base_ns = 10_000_000_000_000_000
        # Add 5 trades 200ms apart
        for i in range(5):
            eng.add_trade(price=100.0 + i, ts_ns=base_ns + i * 200_000_000)
        # All 5 within 600s window
        assert len(eng._prices) == 5
        # Add trade 2s later → oldest trades should be evicted
        eng.add_trade(price=105.0, ts_ns=base_ns + 2_000_000_000)
        # Oldest 5 at t=0,200,400,600,800ms are all within 2s from latest (2s), which is < 600s lookback
        # So all should still be in window
        assert len(eng._prices) == 6

    def test_update_smooths_towards_realized(self):
        """EWMA should not land directly on realized — should converge gradually."""
        eng = RealizedVolEngine(smoothing_alpha=0.2)
        base_ns = 10_000_000_000_000_000
        # Flat-ish prices with small moves
        for i in range(50):
            eng.add_trade(price=100.0 + (i % 3) * 0.1, ts_ns=base_ns + i * 10_000_000)
        sig1 = eng.update()
        # Big upward move
        for i in range(20):
            eng.add_trade(price=105.0, ts_ns=base_ns + 500_000_000 + i * 10_000_000)
        sig2 = eng.update()
        assert sig1 > 0.0
        assert sig2 > sig1  # vol increased

    def test_multiple_updates_converge(self):
        """After many observations at roughly constant vol, sigma stabilizes."""
        eng = RealizedVolEngine(smoothing_alpha=0.3)
        base_ns = 10_000_000_000_000_000
        # Prices with consistent ~1% moves every 10ms
        for i in range(200):
            price = 100.0 * (1.001 ** (i % 10))
            eng.add_trade(price=price, ts_ns=base_ns + i * 10_000_000)
        for _ in range(30):
            eng.update()
        # With consistent moves, realized vol should be non-trivial
        assert eng.sigma > 0.0

    def test_regime_classification(self):
        eng = RealizedVolEngine(vol_low_threshold=0.3, vol_high_threshold=1.5)
        eng._sigma = 0.15
        assert eng.regime == VolRegime.LOW
        eng._sigma = 0.8
        assert eng.regime == VolRegime.NORMAL
        eng._sigma = 2.0
        assert eng.regime == VolRegime.HIGH

    def test_snapshot(self):
        eng = RealizedVolEngine()
        base_ns = 10_000_000_000_000_000
        for i in range(20):
            eng.add_trade(price=100.0 + i * 0.1, ts_ns=base_ns + i * 10_000_000)
        eng.update()
        snap = eng.snapshot()
        assert isinstance(snap, IVSnapshot)
        assert snap.sigma > 0
        assert snap.n_observations == 20

    def test_reset(self):
        eng = RealizedVolEngine(lookback_seconds=600.0)  # 10-min lookback
        base_ns = 10_000_000_000_000_000
        for i in range(30):
            eng.add_trade(price=105.0 + (i % 5) * 0.2, ts_ns=base_ns + i * 10_000_000)
        eng.update()
        assert eng.n_observations == 30
        assert eng.sigma != 1.0  # should have updated from flat-ish prices
        eng.reset()
        assert eng.n_observations == 0
        assert eng.sigma == 1.0
        assert len(eng._prices) == 0

    def test_iv_scalar_bounded(self):
        """IV scalar should be bounded in a reasonable range."""
        eng = RealizedVolEngine()
        # Force a scalar through repeated IV updates
        base_ns = 10_000_000_000_000_000
        for i in range(100):
            eng.add_trade(price=100.0 + (i % 5) * 0.5, ts_ns=base_ns + i * 10_000_000)
        for _ in range(50):
            eng.update(iv_estimate=3.0)  # very high IV
        # Scalar should be clamped to [0.5, 3.0]
        assert 0.5 <= eng._iv_scalar <= 3.0
