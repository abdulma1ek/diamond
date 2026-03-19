"""Tests for src/market_calibration.py — prediction-market-analysis integration."""

import pytest
from decimal import Decimal
from unittest.mock import patch
from datetime import timezone

from src.market_calibration import (
    CalibrationAdjuster,
    EVCalculator,
    HourlySignalFilter,
    LongshotBiasFilter,
    LONGSHOT_MIN_PROB,
    LONGSHOT_MAX_PROB,
    LONGSHOT_EDGE_MULTIPLIER,
)


class TestCalibrationAdjuster:
    def setup_method(self):
        self.adj = CalibrationAdjuster(alpha=0.3)

    def test_mid_price_near_zero_bias(self):
        """At 50% the calibration curve is near perfect — bias should be tiny."""
        bias = self.adj.calibration_bias(0.50)
        assert abs(bias) < 0.01

    def test_longshot_bias_is_negative(self):
        """At low prices (<15%), empirical win rate < implied → negative bias."""
        bias = self.adj.calibration_bias(0.05)
        assert bias < 0, "Longshot markets should have negative calibration bias"

    def test_high_prob_also_negative(self):
        """At high prices (>85%), the NO side is a longshot → also negative bias."""
        bias = self.adj.calibration_bias(0.95)
        assert bias < 0

    def test_adjust_reduces_longshot_fv(self):
        """Calibration correction should reduce fair value at extreme longshot prices."""
        model_prob = 0.10
        market_price = 0.05
        adjusted = self.adj.adjust(model_prob, market_price)
        assert adjusted < model_prob, "Calibration correction should lower FV for longshots"

    def test_adjust_bounded(self):
        """Adjusted probability must always stay in (0.0001, 0.9999)."""
        for p in [0.01, 0.05, 0.15, 0.50, 0.85, 0.95, 0.99]:
            adjusted = self.adj.adjust(p, p)
            assert 0.0001 <= adjusted <= 0.9999

    def test_alpha_zero_means_no_correction(self):
        """With alpha=0, adjusted value should equal the raw model probability."""
        adj = CalibrationAdjuster(alpha=0.0)
        model_prob = 0.35
        adjusted = adj.adjust(model_prob, market_price_prob=0.05)
        assert adjusted == pytest.approx(model_prob, abs=1e-6)

    def test_mispricing_pct_negative_for_longshots(self):
        """Mispricing % should be negative at low prices (overpriced longshots)."""
        pct = self.adj.mispricing_pct(0.05)
        assert pct < 0

    def test_mispricing_pct_zero_safe(self):
        """mispricing_pct with price=0 should not raise."""
        result = self.adj.mispricing_pct(0.0)
        assert result == 0.0

    def test_interpolation_monotonic(self):
        """The calibration curve interpolation should be monotonically increasing."""
        probs = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
        win_rates = [self.adj._interpolate_win_rate(p * 100) for p in probs]
        for i in range(1, len(win_rates)):
            assert win_rates[i] > win_rates[i - 1], "Calibration curve must be monotonic"


class TestEVCalculator:
    def test_ev_yes_positive(self):
        """Win prob > market price → positive EV for YES."""
        ev = EVCalculator.ev_yes(win_prob=0.60, market_price=0.50)
        assert ev == pytest.approx(0.10)

    def test_ev_yes_negative(self):
        """Win prob < market price → negative EV (don't trade)."""
        ev = EVCalculator.ev_yes(win_prob=0.40, market_price=0.50)
        assert ev == pytest.approx(-0.10)

    def test_ev_yes_zero(self):
        """Win prob == market price → zero EV (break even)."""
        ev = EVCalculator.ev_yes(win_prob=0.50, market_price=0.50)
        assert ev == pytest.approx(0.0)

    def test_ev_no_is_complement(self):
        """EV(NO) at given market = EV(YES) on the other side."""
        ev_yes = EVCalculator.ev_yes(0.60, 0.50)
        ev_no = EVCalculator.ev_no(0.60, 0.50)
        # YES side has +0.10 edge, NO side has -0.10 (same trade, opposite sign)
        assert ev_no == pytest.approx(-ev_yes)

    def test_best_side_yes_when_higher(self):
        """best_side should pick YES when win_prob > market_price."""
        side, ev = EVCalculator.best_side(win_prob_yes=0.70, market_price_yes=0.50)
        assert side == "YES"
        assert ev > 0

    def test_best_side_no_when_higher(self):
        """best_side should pick NO when win_prob_yes < market_price_yes."""
        side, ev = EVCalculator.best_side(win_prob_yes=0.30, market_price_yes=0.50)
        assert side == "NO"
        assert ev > 0

    def test_ev_formula_matches_analysis_framework(self):
        """Verify EV = win_rate - price matches ev_yes_vs_no.py formula (in prob space)."""
        # From ev_yes_vs_no.py: EV (cents) = 100 * win_rate - price
        # In probability space: EV = win_rate - price/100  → win_rate - market_price
        win_rate = 0.55
        price = 0.50
        ev = EVCalculator.ev_yes(win_rate, price)
        assert ev == pytest.approx(win_rate - price)


class TestHourlySignalFilter:
    def setup_method(self):
        self.f = HourlySignalFilter()

    def test_us_market_hours_above_one(self):
        """US market peak hours (15:00-17:00 UTC) should have multiplier > 1.0."""
        for hour in [15, 16, 17]:
            assert self.f.get_multiplier(hour) > 1.0

    def test_dead_zone_below_one(self):
        """Quiet night hours (02:00-04:00 UTC) should have multiplier < 1.0."""
        for hour in [2, 3, 4]:
            assert self.f.get_multiplier(hour) < 1.0

    def test_apply_scales_decimal(self):
        """apply() should multiply Decimal signal score by hourly multiplier."""
        score = Decimal("0.30")
        mult = self.f.get_multiplier(16)  # peak hour
        result = self.f.apply(score, utc_hour=16)
        assert float(result) == pytest.approx(float(score) * mult, rel=1e-6)

    def test_apply_zero_score(self):
        """Zero signal score should remain zero regardless of multiplier."""
        result = self.f.apply(Decimal("0.0"), utc_hour=16)
        assert result == Decimal("0.0")

    def test_is_prime_hour_true_at_peak(self):
        assert self.f.is_prime_hour(utc_hour=16) is True

    def test_is_prime_hour_false_at_dead_zone(self):
        assert self.f.is_prime_hour(utc_hour=3) is False

    def test_all_24_hours_have_multiplier(self):
        """Every UTC hour must return a valid positive multiplier."""
        for hour in range(24):
            mult = self.f.get_multiplier(hour)
            assert mult > 0

    def test_unknown_hour_defaults_to_one(self):
        """Hour not in the dict should fall back to 1.0."""
        f = HourlySignalFilter(multipliers={})
        assert f.get_multiplier(12) == 1.0


class TestLongshotBiasFilter:
    def setup_method(self):
        self.f = LongshotBiasFilter()

    def test_is_longshot_low_price(self):
        assert self.f.is_longshot(0.05) is True
        assert self.f.is_longshot(0.14) is True

    def test_is_longshot_high_price(self):
        assert self.f.is_longshot(0.86) is True
        assert self.f.is_longshot(0.95) is True

    def test_not_longshot_mid_range(self):
        assert self.f.is_longshot(0.15) is False
        assert self.f.is_longshot(0.50) is False
        assert self.f.is_longshot(0.85) is False

    def test_adjusted_min_edge_raised_in_longshot_zone(self):
        base = 0.05
        effective = self.f.adjusted_min_edge(base, 0.05)
        assert effective == pytest.approx(base * LONGSHOT_EDGE_MULTIPLIER)

    def test_adjusted_min_edge_unchanged_in_normal_zone(self):
        base = 0.05
        effective = self.f.adjusted_min_edge(base, 0.50)
        assert effective == pytest.approx(base)

    def test_boundary_at_exactly_min_prob(self):
        """Exactly at the boundary should NOT be considered a longshot."""
        assert self.f.is_longshot(LONGSHOT_MIN_PROB) is False

    def test_boundary_at_exactly_max_prob(self):
        """Exactly at the upper boundary should NOT be considered a longshot."""
        assert self.f.is_longshot(LONGSHOT_MAX_PROB) is False

    def test_custom_thresholds(self):
        f = LongshotBiasFilter(min_prob=0.20, max_prob=0.80, edge_multiplier=2.0)
        assert f.is_longshot(0.15) is True
        assert f.is_longshot(0.85) is True
        assert f.is_longshot(0.50) is False
        assert f.adjusted_min_edge(0.05, 0.15) == pytest.approx(0.10)
