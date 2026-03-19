"""Binary Option (Cash-or-Nothing) pricing for 5-minute BTC markets.

Implements high-precision Black-Scholes for binary options with
Polymarket-specific fee adjustments and calibration-aware edge calculation.

Calibration adjustments derived from:
  research/prediction_market_analysis/src/analysis/polymarket/polymarket_win_rate_by_price.py
"""

from decimal import Decimal
from math import log, sqrt
from scipy.stats import norm
from nautilus_trader.model.objects import Price

from src.market_calibration import CalibrationAdjuster, LongshotBiasFilter


def polymarket_fee(price: Price | Decimal | float, base_fee: float = 0.0315) -> Price:
    """
    Polymarket dynamic taker fee for 5-min crypto markets.
    Per spec (CLAUDE.md / gemini.md): range 0.44%–3.15%, maximum at p=0.50.
    Formula: base_fee * 4 * p * (1-p)  →  at p=0.50: fee = base_fee = 3.15%
    """
    p = float(price)
    fee_val = base_fee * 4.0 * p * (1.0 - p)
    return Price.from_str(f"{fee_val:.8f}")


def fair_value_binary_yes(
    S: float,
    K: float,
    sigma: float,
    T: float,
    r: float = 0.0,
) -> float:
    """
    Calculate Fair Value (intrinsic probability) for a Binary 'YES' token.
    Internal calculation helper using floats for math functions.
    """
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0

    d2 = (log(S / K) + (r - 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    prob_yes = norm.cdf(d2)
    return max(0.0001, min(0.9999, prob_yes))


def taker_adjusted_edge(
    fv: Price | Decimal | float,
    market_price: Price | Decimal | float,
    min_edge: float = 0.05,
) -> Decimal:
    """
    Calculate the Taker-Adjusted Edge.
    Formula: |FV - MarketPrice| - (Fee + MinEdge)
    Returns Decimal for high-precision comparison.
    """
    fv_val = float(fv)
    mkt_val = float(market_price)
    fee = float(polymarket_fee(market_price))
    
    gross_edge = abs(fv_val - mkt_val)
    adj_edge = gross_edge - (fee + min_edge)
    return Decimal(str(adj_edge))


# 5 minutes in years (used as default T)
T_5MIN: float = 5.0 / 525600.0


def fair_value_yes(S: Price | float, K: Price | float, sigma: float, T: float = T_5MIN) -> Price:
    """Fair value for YES token (raw probability). Returns Nautilus Price."""
    s_val = float(S)
    k_val = float(K)
    
    if s_val <= 0 or k_val <= 0:
        raise ValueError(f"S and K must be positive, got S={s_val}, K={k_val}")
    
    fv = fair_value_binary_yes(s_val, k_val, sigma, T)
    return Price.from_str(f"{fv:.8f}")


def fair_value_no(S: Price | float, K: Price | float, sigma: float, T: float = T_5MIN) -> Price:
    """Fair value for NO token (complement of YES). Returns Nautilus Price."""
    fv_yes = fair_value_yes(S, K, sigma, T)
    fv_no = 1.0 - float(fv_yes)
    return Price.from_str(f"{fv_no:.8f}")


def edge(model_prob: float | Price, market_prob: float | Price) -> float:
    """Edge = model probability minus market probability."""
    return float(model_prob) - float(market_prob)


def net_edge(fv: float, market_price: float) -> float:
    """Gross edge minus the dynamic Polymarket taker fee (no min_edge deduction).

    Kept for backward compatibility with tests. For trade gating use taker_adjusted_edge.
    Formula: |fv - market_price| - fee(market_price)
    """
    gross = abs(fv - market_price)
    fee = float(polymarket_fee(market_price))
    return gross - fee


def calibration_adjusted_fair_value(
    raw_fv: Price | float,
    market_price: Price | float | Decimal,
    adjuster: CalibrationAdjuster | None = None,
) -> Price:
    """Return a calibration-corrected fair value for a YES token.

    Applies the empirical calibration bias from prediction-market-analysis
    (polymarket_win_rate_by_price) to shade the raw Black-Scholes estimate
    toward the empirically observed win-rate curve.

    At extreme prices (< 15% or > 85%), the longshot bias means the market
    systematically overprices outcomes; this correction reduces the fair value
    estimate to avoid overpaying for biased tokens.

    Args:
        raw_fv: Raw Black-Scholes fair value probability.
        market_price: Current market ask price for the YES token.
        adjuster: CalibrationAdjuster instance; a default is created if None.

    Returns:
        Calibration-adjusted fair value as a Nautilus Price object.
    """
    if adjuster is None:
        adjuster = CalibrationAdjuster()
    adjusted = adjuster.adjust(float(raw_fv), float(market_price))
    return Price.from_str(f"{adjusted:.8f}")


def calculate_volatility_z_score(current_vol: float, vol_history: list[float]) -> float:
    """Calculate Z-Score of current 1s volatility against history."""
    if len(vol_history) < 30:
        return 0.0

    import numpy as np

    mu = np.mean(vol_history)
    std = np.std(vol_history)

    if std == 0:
        return 0.0
    return (current_vol - mu) / std
