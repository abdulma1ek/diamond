import pytest
from decimal import Decimal
from src.risk import RiskManager


@pytest.fixture
def rm():
    return RiskManager(
        daily_drawdown_limit=Decimal("0.10"),
        kelly_fraction_cap=Decimal("0.10"),
        max_position_size=Decimal("0.1"),
        heartbeat_timeout_ns=3_000_000_000,  # 3s
    )


# ── Drawdown Tests ────────────────────────────────────────────────


class TestDrawdown:
    def test_daily_open_resets(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        assert rm.start_of_day_balance == Decimal("1000.0")
        assert rm.current_balance == Decimal("1000.0")
        assert rm.high_water_mark == Decimal("1000.0")
        assert rm.is_halted is False

    def test_balance_increase_updates_hwm(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_balance(Decimal("1050.0"))
        assert rm.high_water_mark == Decimal("1050.0")
        assert rm.is_halted is False

    def test_drawdown_below_limit_ok(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_balance(Decimal("1050.0"))
        result = rm.update_balance(Decimal("1000.0"))  # 4.76% drawdown from 1050
        assert result is True
        assert rm.is_halted is False

    def test_drawdown_exceeds_limit_halts(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        result = rm.update_balance(Decimal("899.0"))  # 10.1% drawdown
        assert result is False
        assert rm.is_halted is True

    def test_halted_stays_halted(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_balance(Decimal("899.0"))  # halts
        result = rm.update_balance(Decimal("950.0"))  # recovery doesn't un-halt
        assert result is False
        assert rm.is_halted is True

    def test_daily_open_un_halts(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_balance(Decimal("899.0"))  # halts
        rm.on_daily_open(Decimal("900.0"))  # new day resets
        assert rm.is_halted is False


# ── Kelly Criterion Tests ─────────────────────────────────────────


class TestKelly:
    def test_zero_balance(self, rm):
        rm.current_balance = Decimal("0.0")
        assert rm.kelly_size(
            win_prob=Decimal("0.6"),
            win_payout=Decimal("10.0"),
            loss_amount=Decimal("10.0"),
        ) == Decimal("0.0")

    def test_zero_loss(self, rm):
        rm.current_balance = Decimal("1000.0")
        assert rm.kelly_size(
            win_prob=Decimal("0.6"),
            win_payout=Decimal("10.0"),
            loss_amount=Decimal("0.0"),
        ) == Decimal("0.0")

    def test_even_odds_60_pct(self, rm):
        """60% win, 1:1 payout → Kelly f = (0.6*1 - 0.4)/1 = 0.20, capped at 0.10."""
        rm.current_balance = Decimal("1.0")  # 1 ETH
        size = rm.kelly_size(
            win_prob=Decimal("0.6"),
            win_payout=Decimal("10.0"),
            loss_amount=Decimal("10.0"),
        )
        # Kelly = 0.20, capped at 0.10 → 1.0 * 0.10 = 0.10
        assert size == Decimal("0.10")

    def test_negative_kelly_returns_zero(self, rm):
        """40% win, 1:1 → Kelly f = (0.4 - 0.6)/1 = -0.20 → clamped to 0."""
        rm.current_balance = Decimal("1.0")
        size = rm.kelly_size(
            win_prob=Decimal("0.4"),
            win_payout=Decimal("10.0"),
            loss_amount=Decimal("10.0"),
        )
        assert size == Decimal("0.0")

    def test_capped_at_max_position(self, rm):
        """Large balance should still cap at max_position_size."""
        rm.current_balance = Decimal("100.0")  # 100 ETH
        size = rm.kelly_size(
            win_prob=Decimal("0.6"),
            win_payout=Decimal("10.0"),
            loss_amount=Decimal("10.0"),
        )
        assert size == Decimal("0.1")  # capped at 0.1 ETH

    def test_small_edge(self, rm):
        """51% win, 1:1 → Kelly f = (0.51 - 0.49)/1 = 0.02."""
        rm.current_balance = Decimal("1.0")
        size = rm.kelly_size(
            win_prob=Decimal("0.51"),
            win_payout=Decimal("10.0"),
            loss_amount=Decimal("10.0"),
        )
        # Kelly = 0.02, below cap → 1.0 * 0.02 = 0.02
        assert size == Decimal("0.02")


# ── Heartbeat Tests ───────────────────────────────────────────────


class TestHeartbeat:
    def test_no_heartbeat_yet_ok(self, rm):
        assert rm.check_heartbeat(now_ns=1_000_000_000) is True

    def test_within_timeout_ok(self, rm):
        rm.update_heartbeat(ts_ns=0)
        assert rm.check_heartbeat(now_ns=2_900_000_000) is True  # 2.9s < 3s

    def test_timeout_halts(self, rm):
        rm.update_heartbeat(ts_ns=1)
        assert rm.check_heartbeat(now_ns=3_100_000_001) is False  # 3.1s > 3s
        assert rm.is_halted is True

    def test_timeout_halts_only_once(self, rm):
        rm.update_heartbeat(ts_ns=1)
        rm.check_heartbeat(now_ns=3_100_000_001)  # halts
        # Further checks don't re-log
        assert rm.check_heartbeat(now_ns=5_000_000_000) is False


# ── can_trade Gate ────────────────────────────────────────────────


class TestCanTrade:
    def test_ok_when_healthy(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_heartbeat(ts_ns=0)
        assert rm.can_trade(now_ns=1_000_000_000) is True

    def test_blocked_after_drawdown(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_heartbeat(ts_ns=0)
        rm.update_balance(Decimal("899.0"))
        assert rm.can_trade(now_ns=1_000_000_000) is False

    def test_blocked_after_heartbeat_timeout(self, rm):
        rm.on_daily_open(Decimal("1000.0"))
        rm.update_heartbeat(ts_ns=1)
        assert rm.can_trade(now_ns=4_000_000_001) is False
