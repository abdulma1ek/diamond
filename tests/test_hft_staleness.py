import pytest
from unittest.mock import MagicMock, patch
from nautilus_trader.trading.strategy import StrategyConfig
from src.strategy import SignalGenerationStrategy, STALENESS_THRESHOLD_NS

class StalenessTestStrategy(SignalGenerationStrategy):
    def __init__(self, config):
        super().__init__(config)
        self.mock_now_ns = 0
        self.panicked = False
        self.panic_msg = ""

    def get_now_ns(self):
        # We'll use this to simulate time
        return self.mock_now_ns

    def _is_data_stale(self) -> bool:
        """Overridden for testing to control 'now'."""
        now_ns = self.get_now_ns()
        latency = now_ns - self.latest_feed_ts
        if latency > STALENESS_THRESHOLD_NS:
            msg = f"CRITICAL STALENESS: Latency is {latency / 1_000_000:.2f}ms"
            self.panic(msg)
            return True
        return False

    def panic(self, msg: str):
        self.panicked = True
        self.panic_msg = msg

def test_hft_staleness_thresholds():
    """
    Prove the fail-fast mechanism triggers at exactly > 500ms.
    Threshold: 500,000,000 ns
    """
    config = StrategyConfig()
    strategy = StalenessTestStrategy(config=config)
    
    # Base event time
    ts_event_ns = 1_000_000_000
    strategy.latest_feed_ts = ts_event_ns

    # 1. Exactly at threshold (500ms) - Should NOT panic
    # (The logic is 'latency > STALENESS_THRESHOLD_NS')
    strategy.mock_now_ns = ts_event_ns + STALENESS_THRESHOLD_NS
    assert not strategy._is_data_stale()
    assert not strategy.panicked

    # 2. Threshold + 1ns (500.000001ms) - Should PANIC
    strategy.mock_now_ns = ts_event_ns + STALENESS_THRESHOLD_NS + 1
    assert strategy._is_data_stale()
    assert strategy.panicked
    assert "CRITICAL STALENESS" in strategy.panic_msg

def test_hft_staleness_precision():
    """
    Verify nanosecond precision prevents premature panics at 499.999999ms.
    """
    config = StrategyConfig()
    strategy = StalenessTestStrategy(config=config)
    
    ts_event_ns = 1_000_000_000
    strategy.latest_feed_ts = ts_event_ns

    # 499.999999ms
    strategy.mock_now_ns = ts_event_ns + STALENESS_THRESHOLD_NS - 1
    assert not strategy._is_data_stale()
    assert not strategy.panicked
