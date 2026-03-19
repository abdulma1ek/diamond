"""Implied Volatility (IV) Engine — calibrating sigma for 5-minute BTC markets.

NOTE on mathematical suitability:
  For 5-minute binary options, the vega (∂price/∂σ) is ~10⁻⁴ because
  √T ≈ 0.003 for T=5 min. This makes numerical IV inversion
  (Newton/Bisection/Brent) unreliable — small numerical errors in price
  measurement compound into huge sigma errors.

  Instead, this engine uses realized volatility from live trade flow as the
  primary sigma source — this is what professional market-makers actually do for
  short-expiry binaries. Implied vol is derived analytically from the
  realized-vol estimate using a closed-form calibration.

The engine:
  1. Computes realized vol σ_realized from a rolling window of trade ticks
  2. Applies a volatility regime filter (low/high vol market conditions)
  3. Optionally calibrates σ_realized → σ_iv via a simple scalar multiplier
     estimated from the historical tracking error between realized and IV

Usage:
    iv = RealizedVolEngine(lookback_seconds=300)
    sigma = iv.update(trade_prices=[p1, p2, ...], ts_nanos=[t1, t2, ...])
    # Use sigma in fair_value_yes(S, K, sigma, T)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from math import log, sqrt

from src.pricing import T_5MIN

_logger = logging.getLogger(__name__)


# ─── Realized Volatility ──────────────────────────────────────────────────────


def realized_volatility(prices: list[float], window_seconds: float = 300.0) -> float:
    """
    Compute annualized realized volatility from a list of prices.

    Uses log returns: r_i = ln(p_i / p_{i-1})
    Realized vol = sqrt(sum(r_i²) / (N-1)) * sqrt(1-year / dt)

    For dt in seconds:
        vol_annual = sqrt(sum(r_i²) / N) * sqrt(31536000 / dt)

    Args:
        prices: List of trade prices (must be in chronological order).
        window_seconds: Window size in seconds for the lookback.

    Returns:
        Annualized realized volatility (e.g. 1.2 = 120% annualized vol).
        Returns 0.0 if fewer than 2 prices.
    """
    if len(prices) < 2:
        return 0.0

    log_returns = [log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if not log_returns:
        return 0.0

    mean_return = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_return) ** 2 for r in log_returns) / len(log_returns)

    # Annualize: 1 year = 31536000 seconds
    dt = window_seconds / 31536000.0
    vol = sqrt(variance / dt) if dt > 0 else 0.0
    return vol


# ─── EWMA smoothing ────────────────────────────────────────────────────────────


def _ewma_update(current: float, new: float, alpha: float) -> float:
    """Exponentially-weighted moving average update."""
    return alpha * new + (1.0 - alpha) * current


# ─── Regime detection ─────────────────────────────────────────────────────────


class VolRegime:
    """Volatility regime classification."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


# ─── Main engine ─────────────────────────────────────────────────────────────


@dataclass
class IVSnapshot:
    """Point-in-time IV estimate snapshot."""

    sigma: float
    realized_vol: float
    regime: str
    n_observations: int


class RealizedVolEngine:
    """
    Realized-volatility-based sigma estimator for short-expiry binary markets.

    For 5-minute binary options, implied vol from model inversion is unreliable
    (vega ≈ 0 at short maturities). Instead, we use realized volatility computed
    from a rolling window of trade prices — this is what market-makers use for
    short-dated binaries.

    The engine also tracks the ratio IV/Realized over time to build a
    scalar "IV scalar" that maps realized vol to a calibrated sigma.

    Usage:
        engine = RealizedVolEngine(
            lookback_seconds=300,   # 5-min lookback
            vol_low_threshold=0.30,  # annualized; below this = LOW regime
            vol_high_threshold=1.50,  # above this = HIGH regime
        )
        # On each trade tick:
        engine.add_trade(price=current_btc_price, ts_ns=timestamp_ns)
        # When evaluating:
        sigma = engine.sigma  # smoothed, regime-adjusted sigma
    """

    def __init__(
        self,
        lookback_seconds: float = 300.0,
        vol_low_threshold: float = 0.30,
        vol_high_threshold: float = 1.50,
        smoothing_alpha: float = 0.2,
        iv_scalar_alpha: float = 0.05,
    ):
        self.lookback_seconds = lookback_seconds
        self.vol_low_threshold = vol_low_threshold
        self.vol_high_threshold = vol_high_threshold
        self._alpha = smoothing_alpha
        self._iv_alpha = iv_scalar_alpha

        # Rolling window: (price, timestamp_ns)
        self._prices: list[tuple[float, int]] = []

        # Smoothed realized vol
        self._sigma: float | None = None

        # IV → Realized vol scalar (tracks whether IV runs above or below realized)
        # IV_scalar = typical_IV / typical_realized_vol
        self._iv_scalar: float = 1.0

        # Observations
        self._n: int = 0

        # EWMA of realized vol (for scalar estimation)
        self._ewma_realized: float | None = None
        self._ewma_iv: float | None = None

    @property
    def sigma(self) -> float:
        """Current regime-adjusted, smoothed sigma. Returns 1.0 if no data."""
        if self._sigma is None:
            return 1.0
        return self._sigma

    @property
    def regime(self) -> str:
        """Current volatility regime."""
        s = self.sigma
        if s < self.vol_low_threshold:
            return VolRegime.LOW
        if s > self.vol_high_threshold:
            return VolRegime.HIGH
        return VolRegime.NORMAL

    @property
    def n_observations(self) -> int:
        return self._n

    def add_trade(self, price: float, ts_ns: int) -> None:
        """Add a trade tick to the rolling window."""
        self._prices.append((price, ts_ns))
        self._evict_old(ts_ns)
        self._n += 1

    def _evict_old(self, now_ns: int) -> None:
        """Remove trades older than lookback_seconds from the window."""
        cutoff_ns = now_ns - int(self.lookback_seconds * 1e9)
        self._prices = [(p, t) for p, t in self._prices if t >= cutoff_ns]

    def compute_realized_vol(self) -> float:
        """Compute annualized realized vol from the current rolling window."""
        if len(self._prices) < 2:
            return 0.0
        prices = [p for p, _ in self._prices]
        return realized_volatility(prices, self.lookback_seconds)

    def update(self, iv_estimate: float | None = None) -> float:
        """
        Recompute sigma from the rolling window, apply EWMA smoothing.

        Args:
            iv_estimate: Optional IV estimate (e.g. from broker/API) used to
                          update the IV/realized scalar. If None, uses realized vol
                          directly as sigma.

        Returns:
            Updated smoothed sigma.
        """
        raw_realized = self.compute_realized_vol()

        if raw_realized <= 0:
            # No usable data yet
            return self.sigma

        # Update IV/realized scalar if IV estimate provided
        if iv_estimate is not None and iv_estimate > 0:
            if self._ewma_realized is None:
                self._ewma_realized = raw_realized
                self._ewma_iv = iv_estimate
            else:
                self._ewma_realized = _ewma_update(self._ewma_realized, raw_realized, self._iv_alpha)
                self._ewma_iv = _ewma_update(self._ewma_iv, iv_estimate, self._iv_alpha)

            if self._ewma_realized > 0:
                self._iv_scalar = self._ewma_iv / self._ewma_realized
                self._iv_scalar = max(0.5, min(3.0, self._iv_scalar))  # clip [0.5, 3.0]

        # Map realized → sigma using scalar
        raw_sigma = raw_realized * self._iv_scalar

        # Smooth with EWMA
        if self._sigma is None:
            self._sigma = raw_sigma
        else:
            self._sigma = _ewma_update(self._sigma, raw_sigma, self._alpha)

        return self._sigma

    def snapshot(self) -> IVSnapshot:
        """Return the current state as a named snapshot."""
        raw_realized = self.compute_realized_vol()
        return IVSnapshot(
            sigma=self.sigma,
            realized_vol=raw_realized,
            regime=self.regime,
            n_observations=self._n,
        )

    def reset(self) -> None:
        """Clear the rolling window and reset all state."""
        self._prices.clear()
        self._sigma = None
        self._ewma_realized = None
        self._ewma_iv = None
        self._iv_scalar = 1.0
        self._n = 0
