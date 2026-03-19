import pytest
from decimal import Decimal
from hypothesis import given, strategies as st
from nautilus_trader.model.objects import Price, Quantity

from src.pricing import (
    fair_value_binary_yes,
    fair_value_yes,
    fair_value_no,
    polymarket_fee,
    taker_adjusted_edge,
)

# ── Pricing PBT ─────────────────────────────────────────────────────────────

@given(
    S=st.floats(min_value=1000.0, max_value=200000.0),
    K=st.floats(min_value=1000.0, max_value=200000.0),
    sigma=st.floats(min_value=0.01, max_value=5.0),
    T=st.floats(min_value=0.00001, max_value=0.01),
)
def test_pricing_invariants_pbt(S, K, sigma, T):
    """
    Invariant: Fair Value (FV) must always be between 0.00 and 1.00.
    Invariant: |FV_YES + FV_NO - 1.0| < epsilon.
    """
    fv_yes = fair_value_yes(S, K, sigma, T)
    fv_no = fair_value_no(S, K, sigma, T)
    
    # Use float() for comparison as Price is a wrapper
    val_yes = float(fv_yes)
    val_no = float(fv_no)
    
    # 0.0 <= FV <= 1.0
    assert 0.0 <= val_yes <= 1.0
    assert 0.0 <= val_no <= 1.0
    
    # |FV_YES + FV_NO - 1.0| < epsilon
    assert abs(val_yes + val_no - 1.0) < 1e-7


# ── Fee Awareness PBT ────────────────────────────────────────────────────────

@given(
    market_price=st.floats(min_value=0.01, max_value=0.99),
)
def test_fee_curve_2026_pbt(market_price):
    """
    Verify the 2026 Dynamic Taker Fee Curve.
    Peak at 0.50 probability should be ~1.56%.
    """
    fee = float(polymarket_fee(market_price))
    
    # Formula: 0.0156 * 4 * p * (1-p)
    # At p=0.5: 0.0156 * 4 * 0.25 = 0.0156
    if abs(market_price - 0.5) < 0.001:
        assert abs(fee - 0.0156) < 0.0001
        
    # Fee should always be positive for valid prices
    assert fee > 0


@given(
    fv=st.floats(min_value=0.01, max_value=0.99),
    market_price=st.floats(min_value=0.01, max_value=0.99),
    min_edge=st.floats(min_value=0.01, max_value=0.10),
)
def test_taker_adjusted_edge_invariant(fv, market_price, min_edge):
    """
    Invariant: A trade should never be submitted if the expected "Edge" 
    is less than the current Taker Fee + Slippage buffer.
    
    taker_adjusted_edge > 0 implies abs(fv - market_price) > fee + min_edge
    """
    adj_edge = float(taker_adjusted_edge(fv, market_price, min_edge))
    fee = float(polymarket_fee(market_price))
    gross_edge = abs(fv - market_price)
    
    if adj_edge > 1e-9: # Precision threshold
        assert gross_edge > (fee + min_edge) - 1e-9
    else:
        assert gross_edge <= (fee + min_edge) + 1e-9


# ── Staleness & Oracle-Lag (Missing Implementation Tests) ───────────────────

def test_missing_oracle_lag_protection():
    """
    This test serves as a compliance check for the 30-second No-Trade Zone.
    """
    from src.strategy import SignalGenerationStrategy
    import inspect
    source = inspect.getsource(SignalGenerationStrategy)
    
    # Mandate: "Verify the presence of a 'No-Trade Zone' in the final 30 seconds"
    found_30s_check = (
        ("30" in source and "market_window" in source.lower()) or 
        ("30" in source and "settlement" in source.lower()) or
        ("30" in source and "time_left" in source.lower()) or
        ("30_000_000_000" in source) or
        ("_is_in_no_trade_zone" in source)
    )
    
    # This assertion will fail if the logic is missing
    assert found_30s_check, "CRITICAL: Missing 30-second No-Trade Zone for Oracle-Lag Protection"


def test_float_leak_compliance():
    """
    Compliance check for 'No Floats for Money' mandate.
    """
    from src.log_engine import ModelState
    import inspect
    log_source = inspect.getsource(ModelState)
    assert "btc_price: float" not in log_source, "CRITICAL: Float leak detected in ModelState.btc_price"
    assert "balance: float" not in log_source, "CRITICAL: Float leak detected in ModelState.balance"
    assert "btc_price: Decimal" in log_source or "btc_price: Price" in log_source, "CRITICAL: btc_price must be typed as Decimal or Price"
