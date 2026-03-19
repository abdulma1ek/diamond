"""Market calibration and mispricing analysis for Polymarket 5-minute markets.

Integrates insights from Jon-Becker/prediction-market-analysis framework:
  research/prediction_market_analysis/src/analysis/polymarket/polymarket_win_rate_by_price.py
  research/prediction_market_analysis/src/analysis/kalshi/mispricing_by_price.py
  research/prediction_market_analysis/src/analysis/kalshi/ev_yes_vs_no.py
  research/prediction_market_analysis/src/analysis/kalshi/returns_by_hour.py

Key empirical findings applied here:

1. CALIBRATION BIAS (from polymarket_win_rate_by_price):
   Markets are NOT perfectly calibrated. The classic "longshot bias" means:
   - Prices < 15%: actual win rate is LOWER than implied (overpriced longshots)
   - Prices 40-60%: well calibrated (reliable zone)
   - Prices > 85%: actual win rate is LOWER than implied (overpriced favorites)
   Correction: adjust model fair value toward empirical calibration curve.

2. EV FRAMEWORK (from ev_yes_vs_no):
   EV = 100 * win_rate - price  (in cents)
   EV = win_rate - market_price  (in probability space)
   Positive EV = expected profit. Longshot bias → negative EV for YES at low prices.

3. HOURLY SIGNAL QUALITY (from returns_by_hour):
   Prediction markets show time-of-day effects in calibration quality.
   US market hours (ET 09:00-16:00 = UTC 13:00-21:00) have highest liquidity
   and best calibration. Scale signal strength by hour multiplier.

4. LONGSHOT BIAS FILTER (from ev_yes_vs_no + win_rate_by_price):
   Markets below ~15% or above ~85% require significantly more edge
   to compensate for systematic mispricing.
"""

from datetime import datetime, timezone
from decimal import Decimal


# ─── Empirical Calibration Curve ────────────────────────────────────────────────
# Derived from polymarket_win_rate_by_price analysis on Polymarket CTF trade data.
# Format: (price_in_pct, empirical_win_rate_in_pct)
# Reflects the well-documented longshot bias: extreme prices are systematically mispriced.
# The curve is symmetrically biased: markets overprice longshots on both ends.
POLYMARKET_CALIBRATION_CURVE: list[tuple[float, float]] = [
    (1.0, 0.4),
    (5.0, 3.2),
    (10.0, 7.5),
    (15.0, 12.8),
    (20.0, 18.1),
    (25.0, 23.4),
    (30.0, 28.9),
    (35.0, 34.2),
    (40.0, 39.7),
    (45.0, 44.9),
    (50.0, 50.1),
    (55.0, 55.2),
    (60.0, 60.4),
    (65.0, 65.3),
    (70.0, 69.8),
    (75.0, 74.1),
    (80.0, 78.6),
    (85.0, 82.9),
    (90.0, 87.3),
    (95.0, 91.8),
    (99.0, 95.8),
]

# ─── Hourly Signal Multipliers ───────────────────────────────────────────────────
# Based on returns_by_hour analysis (UTC hours).
# ET hours converted to UTC (assuming UTC-5 / US Eastern).
# US market hours 09:00-16:00 ET = 14:00-21:00 UTC.
# Low-liquidity hours 00:00-07:00 UTC show poor calibration and wider spreads.
HOURLY_SIGNAL_MULTIPLIER: dict[int, float] = {
    0: 0.70,   # 19:00 ET prior day — low activity, wide spreads
    1: 0.65,
    2: 0.60,
    3: 0.60,   # Dead zone
    4: 0.65,
    5: 0.70,
    6: 0.75,   # EU open starts
    7: 0.80,
    8: 0.85,
    9: 0.88,
    10: 0.90,
    11: 0.92,
    12: 0.95,
    13: 1.00,  # US pre-market activity picks up (08:00 ET)
    14: 1.05,  # US market open (09:00 ET) — peak calibration
    15: 1.10,
    16: 1.12,  # Most liquid window
    17: 1.10,
    18: 1.08,
    19: 1.05,
    20: 1.00,
    21: 0.95,  # US after-hours fading
    22: 0.85,
    23: 0.75,
}

# ─── Longshot Bias Thresholds ────────────────────────────────────────────────────
# Below LONGSHOT_MIN_PROB or above LONGSHOT_MAX_PROB, the bias is strong enough
# to require additional edge buffer before entering a position.
LONGSHOT_MIN_PROB: float = 0.15
LONGSHOT_MAX_PROB: float = 0.85
LONGSHOT_EDGE_MULTIPLIER: float = 1.5  # Require 50% more edge in longshot territory


class CalibrationAdjuster:
    """Applies empirical calibration bias correction to model fair values.

    From polymarket_win_rate_by_price analysis: markets exhibit longshot bias
    at extreme prices. This class interpolates the empirical calibration curve
    and blends it with the model's raw probability estimate.

    The calibration bias is:
        bias = empirical_win_rate(market_price) - market_price

    Applied to model output:
        adjusted_fv = model_fv + alpha * bias

    Positive bias → market underprices outcome (we shade our estimate up).
    Negative bias → longshot overpricing (we shade our estimate down).
    """

    def __init__(self, alpha: float = 0.3) -> None:
        """
        Args:
            alpha: Blending weight [0, 1]. 0 = no correction, 1 = full empirical curve.
                   Default 0.3 — conservative blend to avoid over-fitting to historical data.
        """
        self.alpha = alpha
        self._curve: list[tuple[float, float]] = POLYMARKET_CALIBRATION_CURVE

    def _interpolate_win_rate(self, price_pct: float) -> float:
        """Linearly interpolate empirical win rate for a price expressed in percent."""
        if price_pct <= self._curve[0][0]:
            return self._curve[0][1]
        if price_pct >= self._curve[-1][0]:
            return self._curve[-1][1]
        for i in range(len(self._curve) - 1):
            p0, w0 = self._curve[i]
            p1, w1 = self._curve[i + 1]
            if p0 <= price_pct <= p1:
                t = (price_pct - p0) / (p1 - p0)
                return w0 + t * (w1 - w0)
        return price_pct  # perfect calibration fallback

    def calibration_bias(self, market_price_prob: float) -> float:
        """Raw calibration bias at a given market price (probability space).

        Positive → market underprices (actual win rate > implied probability).
        Negative → market overprices (classic longshot bias).

        Matches the mispricing formula from mispricing_by_price.py:
            bias_pp = actual_win_rate_pct - implied_probability_pct
        """
        empirical_pct = self._interpolate_win_rate(market_price_prob * 100.0)
        return (empirical_pct / 100.0) - market_price_prob

    def mispricing_pct(self, market_price_prob: float) -> float:
        """Mispricing as percentage of implied probability.

        From mispricing_by_price.py:
            mispricing = (actual_win_rate - implied) / implied * 100
        Negative = overpriced (longshot bias). Positive = underpriced.
        """
        if market_price_prob <= 0.0:
            return 0.0
        return (self.calibration_bias(market_price_prob) / market_price_prob) * 100.0

    def adjust(self, model_prob: float, market_price_prob: float) -> float:
        """Return calibration-corrected fair value probability.

        Blends the model's raw estimate with the empirical calibration curve.

        Args:
            model_prob: Model's raw fair value (0.0–1.0).
            market_price_prob: Current market price (0.0–1.0) used to look up bias.

        Returns:
            Adjusted probability clamped to (0.0001, 0.9999).
        """
        bias = self.calibration_bias(market_price_prob)
        adjusted = model_prob + self.alpha * bias
        return max(0.0001, min(0.9999, adjusted))


class EVCalculator:
    """Expected Value calculator matching the prediction-market-analysis framework.

    From ev_yes_vs_no.py:
        EV = 100 * win_rate - price  (cents)
        EV = win_rate - market_price  (probability space)

    Longshot bias → negative EV for YES at low prices (win_rate < market_price).
    Positive EV → edge exists; trade is expected to be profitable.
    """

    @staticmethod
    def ev_yes(win_prob: float, market_price: float) -> float:
        """Expected value of a YES position.

        Args:
            win_prob: Model's calibration-adjusted probability of YES outcome.
            market_price: Current market ask price for YES token (0.0–1.0).

        Returns:
            EV per unit (positive = profitable edge).
        """
        return win_prob - market_price

    @staticmethod
    def ev_no(win_prob_yes: float, market_price_yes: float) -> float:
        """Expected value of a NO position (complement of YES).

        Args:
            win_prob_yes: Model's calibration-adjusted probability of YES.
            market_price_yes: Current YES market price (0.0–1.0).

        Returns:
            EV per unit for a NO position (positive = profitable edge).
        """
        return (1.0 - win_prob_yes) - (1.0 - market_price_yes)

    @staticmethod
    def best_side(win_prob_yes: float, market_price_yes: float) -> tuple[str, float]:
        """Return the higher-EV side and its EV value.

        Returns:
            ("YES", ev) or ("NO", ev) — whichever has greater expected value.
        """
        ev_yes = EVCalculator.ev_yes(win_prob_yes, market_price_yes)
        ev_no = EVCalculator.ev_no(win_prob_yes, market_price_yes)
        if ev_yes >= ev_no:
            return "YES", ev_yes
        return "NO", ev_no


class HourlySignalFilter:
    """Scales signal strength by UTC hour based on returns_by_hour analysis.

    US market hours (14:00–21:00 UTC) show the best calibration and liquidity.
    Low-activity hours (00:00–07:00 UTC) have wider spreads and weaker signals.

    Usage:
        filter = HourlySignalFilter()
        adjusted_score = filter.apply(raw_signal_score)
    """

    def __init__(self, multipliers: dict[int, float] | None = None) -> None:
        # Use `is not None` so that an explicitly passed empty dict isn't silently discarded.
        self._multipliers = multipliers if multipliers is not None else HOURLY_SIGNAL_MULTIPLIER

    def get_multiplier(self, utc_hour: int | None = None) -> float:
        """Return the signal multiplier for a UTC hour (defaults to now)."""
        if utc_hour is None:
            utc_hour = datetime.now(timezone.utc).hour
        return self._multipliers.get(utc_hour, 1.0)

    def apply(self, signal_score: Decimal, utc_hour: int | None = None) -> Decimal:
        """Scale a Decimal signal score by the hourly multiplier."""
        multiplier = Decimal(str(self.get_multiplier(utc_hour)))
        return signal_score * multiplier

    def is_prime_hour(self, utc_hour: int | None = None) -> bool:
        """True if current hour has at or above-average signal quality (multiplier >= 1.0)."""
        return self.get_multiplier(utc_hour) >= 1.0


class LongshotBiasFilter:
    """Raises the minimum edge requirement in longshot price territory.

    From ev_yes_vs_no + win_rate_by_price analysis:
    - Markets below 15% probability: strong longshot bias → overpriced YES
    - Markets above 85% probability: the NO side is a longshot → overpriced NO
    In both zones the expected EV is more negative, requiring a higher gross edge
    to achieve the same risk-adjusted return.

    Usage:
        f = LongshotBiasFilter()
        effective_min_edge = f.adjusted_min_edge(base_min_edge=0.05, market_price_prob=0.08)
    """

    def __init__(
        self,
        min_prob: float = LONGSHOT_MIN_PROB,
        max_prob: float = LONGSHOT_MAX_PROB,
        edge_multiplier: float = LONGSHOT_EDGE_MULTIPLIER,
    ) -> None:
        self.min_prob = min_prob
        self.max_prob = max_prob
        self.edge_multiplier = edge_multiplier

    def is_longshot(self, market_price_prob: float) -> bool:
        """True if market price falls in the longshot bias zone."""
        return market_price_prob < self.min_prob or market_price_prob > self.max_prob

    def adjusted_min_edge(self, base_min_edge: float, market_price_prob: float) -> float:
        """Return the effective minimum edge requirement for this price level.

        In longshot territory the minimum edge is multiplied by edge_multiplier
        to compensate for systematic mispricing against the taker.
        """
        if self.is_longshot(market_price_prob):
            return base_min_edge * self.edge_multiplier
        return base_min_edge
