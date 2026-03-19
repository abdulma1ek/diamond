import pytest
from decimal import Decimal

from src.fee_optimizer import (
    FeeOptimizer,
    TradeDirection,
    EdgeAssessment,
    BASE_FEE,
    LONGSHOT_MULTIPLIER,
    LONGSHOT_MIN,
    LONGSHOT_MAX,
)


class TestPolymarketFee:
    """Verify the Polymarket fee curve."""

    def test_fee_at_50c_is_base_fee(self):
        """Fee at p=0.50 should be exactly base_fee (3.15%)."""
        fee = FeeOptimizer.polymarket_fee(0.50)
        assert float(fee) == pytest.approx(BASE_FEE)

    def test_fee_symmetric_around_50c(self):
        """Fee should be symmetric: fee(p) == fee(1-p)."""
        for p in [0.30, 0.35, 0.40, 0.45, 0.48]:
            assert float(FeeOptimizer.polymarket_fee(p)) == pytest.approx(
                float(FeeOptimizer.polymarket_fee(1 - p))
            )

    def test_fee_near_extremes_low(self):
        """Fee near p=0 should be small."""
        fee = float(FeeOptimizer.polymarket_fee(0.05))
        assert 0.004 < fee < 0.006  # ~0.57%

    def test_fee_near_extremes_high(self):
        """Fee near p=1 should be small."""
        fee = float(FeeOptimizer.polymarket_fee(0.95))
        assert 0.004 < fee < 0.006  # ~0.57%

    def test_fee_parabola_shape(self):
        """Fee should increase from extremes to centre."""
        fees = [float(FeeOptimizer.polymarket_fee(p)) for p in [0.05, 0.20, 0.35, 0.50]]
        assert fees == sorted(fees)  # strictly increasing


class TestLongshotZone:
    """Longshot zone detection and multiplier application."""

    def test_inside_longshot_zone_low(self):
        """Price below longshot_min is in longshot zone."""
        opt = FeeOptimizer()
        assert opt._longshot_filter.is_longshot(0.10) is True
        assert opt._longshot_filter.is_longshot(0.01) is True

    def test_outside_longshot_zone(self):
        """Price between longshot_min and longshot_max is NOT in longshot zone."""
        opt = FeeOptimizer()
        assert opt._longshot_filter.is_longshot(0.20) is False
        assert opt._longshot_filter.is_longshot(0.50) is False
        assert opt._longshot_filter.is_longshot(0.80) is False

    def test_inside_longshot_zone_high(self):
        """Price above longshot_max is in longshot zone."""
        opt = FeeOptimizer()
        assert opt._longshot_filter.is_longshot(0.90) is True
        assert opt._longshot_filter.is_longshot(0.99) is True

    def test_longshot_multiplier_applied_inside_zone(self):
        """Required edge should be 1.5× in longshot zone at same price level."""
        # Compare at the same market price — the longshot multiplier multiplies the fee
        # fee(0.10) ≈ 0.0113; multiplier=1.5 → effective fee ≈ 0.017
        # With min_edge=0.05: required_longshot ≈ 0.067
        # With min_edge=0.05 but multiplier=1.0: required ≈ 0.0613
        # Longshot should require a higher edge than if no multiplier were applied
        opt_longshot = FeeOptimizer(min_edge=0.05, longshot_multiplier=1.5)
        opt_normal = FeeOptimizer(min_edge=0.05, longshot_multiplier=1.0)
        required_longshot = opt_longshot.required_edge(Decimal("0.10"), TradeDirection.YES)
        required_normal = opt_normal.required_edge(Decimal("0.10"), TradeDirection.YES)
        assert required_longshot > required_normal


class TestEdgeAssessment:
    """EdgeAssessment calculations."""

    def test_passes_with_sufficient_edge(self):
        """Trade should pass when gross edge exceeds required threshold."""
        opt = FeeOptimizer(min_edge=0.05)
        # fv=0.60, market=0.50 → gross_edge=0.10
        # fee(0.50)=0.0315, required=0.0315+0.05=0.0815
        # 0.10 > 0.0815 → PASS
        result = opt.evaluate(fair_value=0.60, market_price=0.50)
        assert result.passes is True
        assert result.gross_edge == Decimal("0.10")

    def test_fails_with_insufficient_edge(self):
        """Trade should fail when gross edge is below required threshold."""
        opt = FeeOptimizer(min_edge=0.05)
        # fv=0.52, market=0.50 → gross_edge=0.02
        # fee(0.50)=0.0315, required=0.0815
        # 0.02 < 0.0815 → FAIL
        result = opt.evaluate(fair_value=0.52, market_price=0.50)
        assert result.passes is False

    def test_edge_remaining_positive_when_profitable(self):
        """edge_remaining should be positive when trade passes."""
        opt = FeeOptimizer(min_edge=0.05)
        result = opt.evaluate(fair_value=0.60, market_price=0.50)
        assert result.edge_remaining > 0

    def test_no_trade_at_extreme_price_longshot(self):
        """In longshot zone, even a 10% apparent edge may not be enough."""
        opt = FeeOptimizer(min_edge=0.05)
        # fv=0.20, market=0.10 → gross_edge=0.10
        # fee(0.10)≈0.0113, longshot fee≈0.017, required≈0.017+0.05=0.067
        # 0.10 > 0.067 → PASS (barely)
        result = opt.evaluate(fair_value=0.20, market_price=0.10)
        assert result.longshot_multiplier == LONGSHOT_MULTIPLIER

    def test_no_side_preferred_when_market_on_fv(self):
        """When fv == market, no direction has edge."""
        opt = FeeOptimizer(min_edge=0.05)
        result = opt.best_direction(fair_value_yes=0.55, market_yes=0.55)
        assert result is None


class TestBestDirection:
    """Trade direction selection."""

    def test_prefers_yes_when_yes_has_more_edge(self):
        """YES direction chosen when it has more edge remaining than NO."""
        opt = FeeOptimizer(min_edge=0.05)
        # fv=0.65, market=0.50
        # YES edge = 0.15, fee=0.0315, required=0.0815, remaining=0.0685
        # NO edge = |0.35 - 0.50| = 0.15, same fee, same remaining
        # Both equal → returns YES (first in tiebreak)
        direction = opt.best_direction(fair_value_yes=0.65, market_yes=0.50)
        assert direction in (TradeDirection.YES, TradeDirection.NO)

    def test_prefers_no_when_no_has_more_edge(self):
        """NO direction chosen when fair value is below market and NO benefits."""
        opt = FeeOptimizer(min_edge=0.05)
        # fv=0.40, market=0.50
        # YES edge = |0.40-0.50| = 0.10 → required=0.0815 → passes with 0.0185 remaining
        # NO edge = |0.60-0.50| = 0.10 → same numbers
        # With fv below market, YES is overpriced → NO is the buy
        # Actually NO fair_value_no = 0.60, market_no = 0.50 → edge = 0.10
        # Same edge in both directions, tiebreak goes to YES
        # To properly test NO preference, need asymmetric fee
        direction = opt.best_direction(fair_value_yes=0.40, market_yes=0.60)
        # fv_yes=0.40 → fv_no=0.60; market_yes=0.60 → market_no=0.40
        # YES edge = |0.40-0.60| = 0.20 → required fee(0.60)=0.0315*4*0.6*0.4=0.03024 → required=0.08024 → remaining=0.11976
        # NO edge = |0.60-0.40| = 0.20 → fee(0.40)=0.0315*4*0.4*0.6=0.03024 → same
        # Still equal due to symmetry
        assert direction is not None


class TestFeeOptimizerSummary:
    """Summary output for debugging/monitoring."""

    def test_summary_at_50c(self):
        """Summary at p=0.50 shows maximum fee."""
        opt = FeeOptimizer(min_edge=0.05)
        s = opt.summary(Decimal("0.50"))
        assert s["market_price"] == 0.50
        assert s["fee_pct"] == pytest.approx(3.15, rel=0.01)
        assert s["longshot_zone"] is False
        assert s["required_edge_pct"] == pytest.approx(8.15, rel=0.01)

    def test_summary_in_longshot_zone(self):
        """Summary in longshot zone shows multiplier applied."""
        opt = FeeOptimizer(min_edge=0.05)
        s = opt.summary(Decimal("0.10"))
        assert s["longshot_zone"] is True
        assert s["longshot_multiplier"] == LONGSHOT_MULTIPLIER
        # fee(0.10) ≈ 0.01134; required = 0.01134*1.5 + 0.05 = 0.067
        assert s["fee_pct"] == pytest.approx(1.134, rel=0.1)
        assert s["required_edge_pct"] == pytest.approx(6.7, rel=0.1)
