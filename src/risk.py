"""Risk management for 5-minute BTC prediction strategy.

Implements:
  - Daily drawdown hard stop (10% default)
  - Kelly Criterion position sizing (capped at 0.1 ETH)
  - Heartbeat fail-safe (cancel all if data feed lost)
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

log = logging.getLogger(__name__)


@dataclass
class RiskManager:
    """Manages drawdown limits, position sizing, and heartbeat monitoring.

    Designed to be called from the strategy without depending on
    NautilusTrader internals — uses plain Python types for testability.
    """

    daily_drawdown_limit: Decimal = Decimal("0.10")  # 10%
    kelly_fraction_cap: Decimal = Decimal("0.10")  # max Kelly bet fraction
    max_position_size: Decimal = Decimal("0.1")  # 0.1 ETH equivalent
    heartbeat_timeout_ns: int = 3_000_000_000  # 3 seconds (spec: 3000ms)

    # Internal state
    start_of_day_balance: Decimal = field(default=Decimal("0"), init=False)
    current_balance: Decimal = field(default=Decimal("0"), init=False)
    high_water_mark: Decimal = field(default=Decimal("0"), init=False)
    last_heartbeat_ns: int = field(default=0, init=False)
    is_halted: bool = field(default=False, init=False)

    # ── Daily Reset ───────────────────────────────────────────────

    def on_daily_open(self, balance: Decimal) -> None:
        """Reset daily tracking at start of trading day."""
        self.start_of_day_balance = balance
        self.current_balance = balance
        self.high_water_mark = balance
        self.is_halted = False
        log.info(f"RiskManager: daily open, balance={balance:.2f}")

    # ── Drawdown ──────────────────────────────────────────────────

    def update_balance(self, balance: Decimal) -> bool:
        """Update balance and check drawdown. Returns True if trading allowed."""
        self.current_balance = balance
        if balance > self.high_water_mark:
            self.high_water_mark = balance

        if self.is_halted:
            return False

        if self.high_water_mark > 0:
            drawdown = (
                self.high_water_mark - self.current_balance
            ) / self.high_water_mark
            if drawdown >= self.daily_drawdown_limit:
                self.is_halted = True
                log.critical(
                    f"RiskManager: HALTED — drawdown {drawdown:.2%} >= {self.daily_drawdown_limit:.2%} | "
                    f"high={self.high_water_mark:.2f} current={self.current_balance:.2f}"
                )
                return False
        return True

    # ── Position Sizing ───────────────────────────────────────────

    def kelly_size(
        self, win_prob: Decimal, win_payout: Decimal, loss_amount: Decimal
    ) -> Decimal:
        """Calculate position size via Kelly Criterion.

        For binary outcomes (Polymarket Yes/No):
            f = (p * b - q) / b
        where p = win probability, q = 1-p, b = win_payout / loss_amount

        Returns size in ETH, capped at max_position_size.
        """
        if loss_amount <= 0 or win_payout <= 0 or self.current_balance <= 0:
            return Decimal("0")

        p = win_prob
        q = Decimal("1") - p
        b = win_payout / loss_amount

        kelly_f = (p * b - q) / b
        kelly_f = max(Decimal("0"), min(kelly_f, self.kelly_fraction_cap))

        size = self.current_balance * kelly_f
        return min(size, self.max_position_size)

    # ── Heartbeat ─────────────────────────────────────────────────

    def update_heartbeat(self, ts_ns: int) -> None:
        """Record latest heartbeat timestamp (nanoseconds)."""
        self.last_heartbeat_ns = ts_ns

    def check_heartbeat(self, now_ns: int) -> bool:
        """Check if heartbeat is still alive. Returns False if timed out."""
        if self.last_heartbeat_ns == 0:
            return True  # no heartbeat received yet, waiting

        elapsed = now_ns - self.last_heartbeat_ns
        if elapsed > self.heartbeat_timeout_ns:
            if not self.is_halted:
                self.is_halted = True
                log.critical(
                    f"RiskManager: HALTED — heartbeat timeout "
                    f"({elapsed / 1_000_000_000:.1f}s > {self.heartbeat_timeout_ns / 1_000_000_000:.1f}s)"
                )
            return False
        return True

    # ── Gate ───────────────────────────────────────────────────────

    def can_trade(self, now_ns: int) -> bool:
        """Single check: is trading currently allowed?"""
        if self.is_halted:
            return False
        return self.check_heartbeat(now_ns)
