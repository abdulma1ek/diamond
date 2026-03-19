import pytest
from math import exp

from src.pricing import (
    fair_value_yes,
    fair_value_no,
    edge,
    T_5MIN,
    polymarket_fee,
    net_edge,
)


class TestPolymarketFee:
    def test_fee_at_50c(self):
        """Fee at 50c: base_fee=0.0315 → 0.0315 * 4 * 0.5 * 0.5 = 0.0315 (spec max 3.15%)."""
        assert float(polymarket_fee(0.50)) == pytest.approx(0.0315)

    def test_fee_at_extremes(self):
        """Fee at extremes (0.44% min) should be very low."""
        assert float(polymarket_fee(0.01)) < 0.005
        assert float(polymarket_fee(0.99)) < 0.005

    def test_fee_symmetric(self):
        """Fee curve should be symmetric around 0.5."""
        assert float(polymarket_fee(0.3)) == pytest.approx(float(polymarket_fee(0.7)))


class TestNetEdge:
    def test_net_edge_profitable(self):
        """10% gross edge minus 3.15% fee at 50c should be positive (~6.85%)."""
        # gross = 0.60 - 0.50 = 0.10
        # fee(0.50) = 0.0315
        # net = 0.10 - 0.0315 = 0.0685
        assert net_edge(0.60, 0.50) == pytest.approx(0.0685)

    def test_net_edge_unprofitable(self):
        """2% gross edge minus 3.15% fee at 50c should be negative (~-1.15%)."""
        # gross = 0.52 - 0.50 = 0.02
        # fee(0.50) = 0.0315
        # net = 0.02 - 0.0315 = -0.0115
        assert net_edge(0.52, 0.50) < 0


class TestFairValueYes:
    def test_atm_near_half(self):
        """At-the-money (S==K) with moderate vol should be near 0.5."""
        fv = fair_value_yes(S=100_000, K=100_000, sigma=0.80)
        assert 0.45 < fv < 0.55

    def test_deep_itm(self):
        """Spot well above strike should be near 1.0."""
        fv = fair_value_yes(S=110_000, K=100_000, sigma=0.80)
        assert fv > 0.95

    def test_deep_otm(self):
        """Spot well below strike should be near 0.0."""
        fv = fair_value_yes(S=90_000, K=100_000, sigma=0.80)
        assert fv < 0.05

    def test_higher_vol_widens(self):
        """Higher vol should push ATM value closer to 0.5 from both sides."""
        fv_low = fair_value_yes(S=101_000, K=100_000, sigma=0.30)
        fv_high = fair_value_yes(S=101_000, K=100_000, sigma=1.50)
        # Higher vol makes outcome less certain → closer to 0.5
        assert abs(fv_high - 0.5) < abs(fv_low - 0.5)

    def test_zero_vol_above_strike(self):
        """Zero vol with S > K should return 1.0 (deterministic)."""
        assert fair_value_yes(S=100_001, K=100_000, sigma=0.0) == 1.0

    def test_zero_vol_below_strike(self):
        """Zero vol with S < K should return 0.0."""
        assert fair_value_yes(S=99_999, K=100_000, sigma=0.0) == 0.0

    def test_expired(self):
        """T=0 should settle based on current price."""
        assert fair_value_yes(S=100_001, K=100_000, sigma=0.80, T=0.0) == 1.0
        assert fair_value_yes(S=99_999, K=100_000, sigma=0.80, T=0.0) == 0.0

    def test_invalid_inputs(self):
        with pytest.raises(ValueError):
            fair_value_yes(S=0, K=100_000, sigma=0.80)
        with pytest.raises(ValueError):
            fair_value_yes(S=100_000, K=-1, sigma=0.80)

    def test_result_bounded(self):
        """Fair value should always be in [0, 1]."""
        for S in [50_000, 100_000, 150_000]:
            for sigma in [0.1, 0.5, 1.0, 2.0]:
                fv = fair_value_yes(S=S, K=100_000, sigma=sigma)
                assert 0.0 <= fv <= 1.0

    def test_t_5min_default(self):
        """Default T should be 5/525600."""
        assert T_5MIN == pytest.approx(5.0 / 525600.0)


class TestFairValueNo:
    def test_complement(self):
        """Yes + No should equal 1.0."""
        yes = fair_value_yes(S=100_000, K=100_000, sigma=0.80)
        no = fair_value_no(S=100_000, K=100_000, sigma=0.80)
        assert yes + no == pytest.approx(1.0)


class TestEdge:
    def test_positive_edge(self):
        """Model thinks Yes is more likely than market."""
        assert edge(model_prob=0.60, market_prob=0.50) == pytest.approx(0.10)

    def test_negative_edge(self):
        """Model thinks Yes is less likely than market."""
        assert edge(model_prob=0.40, market_prob=0.55) == pytest.approx(-0.15)

    def test_no_edge(self):
        assert edge(model_prob=0.50, market_prob=0.50) == pytest.approx(0.0)

    def test_edge_threshold(self):
        """5% edge requirement from spec.md."""
        e = edge(model_prob=0.60, market_prob=0.50)
        assert abs(e) > 0.05  # Tradeable
        e2 = edge(model_prob=0.52, market_prob=0.50)
        assert abs(e2) < 0.05  # Not tradeable
