"""FeeOptimizer — dynamic edge threshold engine.

Computes the minimum required edge for a trade given:
  1. The non-linear Polymarket taker fee curve (0.44%–3.15%)
  2. The longshot multiplier (1.5× outside 15%–85%)
  3. The base minimum edge from config

Unlike a flat fee model, this engine captures the fact that:
  - At p=50%, the fee is 3.15% → requires larger edge to be worthwhile
  - At p=5% or p=95%, the fee is ~0.57% → can tolerate smaller edge
  - At extreme prices (<15% or >85%), longshot bias requires 1.5× extra buffer

Usage:
    optimizer = FeeOptimizer(min_edge=0.05, longshot_multiplier=1.5)
    threshold = optimizer.required_edge(market_price_yes)
    if gross_edge >= threshold:
        # Trade passes fee barrier
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from src.market_calibration import LongshotBiasFilter


# Default Polymarket taker fee schedule
BASE_FEE: float = 0.0315  # 3.15% at p=0.50
LONGSHOT_MIN: float = 0.15  # 15%
LONGSHOT_MAX: float = 0.85  # 85%
LONGSHOT_MULTIPLIER: float = 1.5


class TradeDirection(Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class EdgeAssessment:
    """Result of evaluating a potential trade."""

    direction: TradeDirection
    gross_edge: Decimal
    fee: Decimal
    longshot_multiplier: float
    required_edge: Decimal
    passes: bool
    edge_remaining: Decimal  # gross_edge - required_edge; positive means profitable


class FeeOptimizer:
    """Dynamic edge threshold engine for Polymarket 5-minute markets.

    Args:
        min_edge: Base minimum edge fraction (e.g. 0.05 = 5%). Applied on top of fee.
        longshot_multiplier: Extra edge multiplier in the longshot zone (<15% or >85%).
        longshot_min: Lower bound of longshot zone (fractional probability).
        longshot_max: Upper bound of longshot zone (fractional probability).
    """

    def __init__(
        self,
        min_edge: float = 0.05,
        longshot_multiplier: float = LONGSHOT_MULTIPLIER,
        longshot_min: float = LONGSHOT_MIN,
        longshot_max: float = LONGSHOT_MAX,
    ):
        self.min_edge = Decimal(str(min_edge))
        self.longshot_multiplier = longshot_multiplier
        self.longshot_min = longshot_min
        self.longshot_max = longshot_max
        self._longshot_filter = LongshotBiasFilter(
            min_prob=longshot_min,
            max_prob=longshot_max,
            edge_multiplier=longshot_multiplier,
        )

    @staticmethod
    def polymarket_fee(price: float | Decimal) -> Decimal:
        """Compute Polymarket taker fee for a given YES-token market price.

        Fee curve: base_fee * 4 * p * (1-p)
        - p=0.50 → fee = 3.15% (maximum)
        - p=0.05 → fee = 0.57%
        - p=0.95 → fee = 0.57%
        - p=0.01 → fee = 0.13% (minimum, not quite 0.44% due to parabola shape)
        """
        p = float(price)
        fee_val = BASE_FEE * 4.0 * p * (1.0 - p)
        return Decimal(str(fee_val))

    def _get_longshot_multiplier(self, market_price: Decimal) -> float:
        """Return 1.5× if in longshot zone, else 1.0×."""
        if self._longshot_filter.is_longshot(float(market_price)):
            return self.longshot_multiplier
        return 1.0

    def required_edge(self, market_price: Decimal | float, direction: TradeDirection = TradeDirection.YES) -> Decimal:
        """Minimum edge required to clear the fee barrier.

        Formula: fee(market_price) * longshot_multiplier + base_min_edge
        """
        price_for_fee = float(market_price)
        if direction == TradeDirection.NO:
            # Fee is charged on the YES-side equivalent price even when buying NO
            # because the CLOB books it symmetrically on the YES leg
            price_for_fee = float(Decimal("1") - Decimal(str(market_price)))

        fee = self.polymarket_fee(price_for_fee)
        multiplier = self._get_longshot_multiplier(Decimal(str(market_price)))
        return (fee * Decimal(str(multiplier))) + self.min_edge

    def evaluate(
        self,
        fair_value: Decimal | float,
        market_price: Decimal | float,
        direction: TradeDirection = TradeDirection.YES,
    ) -> EdgeAssessment:
        """Evaluate whether a trade passes the fee barrier.

        Args:
            fair_value: Model fair value (calibration-adjusted) as a probability [0, 1].
            market_price: Current market price for the YES token [0, 1].
            direction: Which side to trade (YES or NO).

        Returns:
            EdgeAssessment with pass/fail and detailed breakdown.
        """
        fv = Decimal(str(fair_value))
        mp = Decimal(str(market_price))

        if direction == TradeDirection.YES:
            gross_edge = abs(fv - mp)
        else:
            # For NO: fair_value_no = 1 - fair_value_yes; market_no = 1 - market_yes
            fv_no = Decimal("1") - fv
            mp_no = Decimal("1") - mp
            gross_edge = abs(fv_no - mp_no)

        fee = self.polymarket_fee(float(mp))
        multiplier = self._get_longshot_multiplier(mp)
        required = (fee * Decimal(str(multiplier))) + self.min_edge
        passes = gross_edge >= required
        edge_remaining = gross_edge - required

        return EdgeAssessment(
            direction=direction,
            gross_edge=gross_edge,
            fee=fee,
            longshot_multiplier=multiplier,
            required_edge=required,
            passes=passes,
            edge_remaining=edge_remaining,
        )

    def best_direction(
        self,
        fair_value_yes: Decimal | float,
        market_yes: Decimal | float,
    ) -> TradeDirection | None:
        """Evaluate both YES and NO sides, return the more profitable direction.

        Returns None if neither side clears the fee barrier.
        """
        yes_assessment = self.evaluate(fair_value_yes, market_yes, TradeDirection.YES)
        no_assessment = self.evaluate(fair_value_yes, market_yes, TradeDirection.NO)

        candidates = [
            (yes_assessment, TradeDirection.YES),
            (no_assessment, TradeDirection.NO),
        ]

        # Sort by edge remaining (descending), filter to passing only
        passing = [(a, d) for a, d in candidates if a.passes]
        if not passing:
            return None

        passing.sort(key=lambda x: x[0].edge_remaining, reverse=True)
        return passing[0][1]

    def summary(self, market_price: Decimal | float) -> dict:
        """Return a human-readable fee breakdown for a given market price."""
        mp = Decimal(str(market_price))
        fee = self.polymarket_fee(float(mp))
        multiplier = self._get_longshot_multiplier(mp)
        required = (fee * Decimal(str(multiplier))) + self.min_edge
        in_longshot = self._longshot_filter.is_longshot(float(mp))

        return {
            "market_price": float(mp),
            "fee_pct": float(fee) * 100,
            "longshot_zone": in_longshot,
            "longshot_multiplier": multiplier,
            "min_edge_pct": float(self.min_edge) * 100,
            "required_edge_pct": float(required) * 100,
            "at_50c_required_pct": float(
                self.required_edge(Decimal("0.50"), TradeDirection.YES)
            ) * 100,
        }
