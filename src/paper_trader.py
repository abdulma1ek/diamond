"""Paper trading tracker synced to Polymarket prices.

Records predictions using real Polymarket YES/NO prices as entry,
then settles after 5 minutes based on actual BTC price movement.
Binary option payout:
  - Buy YES at market price → if BTC goes UP, token pays $1.00
  - Buy NO at market price  → if BTC goes DOWN, token pays $1.00
  - PnL = (1.0 - entry_price) * num_tokens if correct
  - PnL = -entry_price * num_tokens if wrong
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger(__name__)


@dataclass
class Prediction:
    """A single paper trade synced to Polymarket."""

    direction: str  # "YES" or "NO"
    btc_entry: Decimal  # BTC spot price at prediction time
    market_yes: Decimal  # Polymarket YES price at entry
    market_no: Decimal  # Polymarket NO price at entry
    entry_price: Decimal  # price paid for the token (YES or NO)
    num_tokens: Decimal  # how many tokens bought (stake / entry_price)
    stake: Decimal  # USD amount risked
    signal_score: float
    fair_value: Decimal  # model's fair value for YES
    model_edge: Decimal  # |fair_value - market_price|
    timestamp: float  # unix timestamp
    settled: bool = False
    correct: bool = False
    btc_exit: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")


class PaperTrader:
    """Simulated account using real Polymarket prices.

    Fetches live YES/NO prices from Polymarket, simulates buying tokens,
    and settles after 5 minutes based on actual BTC price movement.
    """

    def __init__(self, starting_balance: Decimal = Decimal("10.0")) -> None:
        self.starting_balance: Decimal = starting_balance
        self.balance: Decimal = starting_balance
        self.predictions: list[Prediction] = []
        self.pending: list[Prediction] = []

        # Stats
        self.wins: int = 0
        self.losses: int = 0
        self.total_pnl: Decimal = Decimal("0")

        # Cooldown: only one prediction per 5-min window
        self._last_prediction_window: int = 0

    def current_window_id(self) -> int:
        """Get the current 5-minute window ID."""
        now = int(time.time())
        return now - (now % 300)

    def can_predict(self) -> bool:
        """Check if we can make a prediction (one per 5-min window, have balance)."""
        window = self.current_window_id()
        if window == self._last_prediction_window:
            return False
        if self.balance <= Decimal("0.01"):
            return False
        return True

    def make_prediction(
        self,
        direction: str,
        btc_spot: Decimal,
        market_yes: Decimal,
        market_no: Decimal,
        signal_score: float,
        fair_value: Decimal,
        stake_fraction: Decimal = Decimal("0.10"),
    ) -> Prediction | None:
        """Record a prediction using real Polymarket prices."""
        if not self.can_predict():
            return None

        # Entry price = the token price for our direction
        if direction == "YES":
            entry_price = market_yes
            model_edge = fair_value - market_yes
        else:
            entry_price = market_no
            model_edge = (Decimal("1.0") - fair_value) - market_no

        if entry_price <= 0 or entry_price >= Decimal("1.0"):
            return None

        # Stake = fraction of balance
        stake = min(self.balance * stake_fraction, self.balance)
        if stake < Decimal("0.01"):
            return None

        # Number of tokens = stake / price_per_token
        num_tokens = stake / entry_price

        pred = Prediction(
            direction=direction,
            btc_entry=btc_spot,
            market_yes=market_yes,
            market_no=market_no,
            entry_price=entry_price,
            num_tokens=num_tokens,
            stake=stake,
            signal_score=signal_score,
            fair_value=fair_value,
            model_edge=model_edge,
            timestamp=time.time(),
        )

        self.pending.append(pred)
        self.predictions.append(pred)
        self._last_prediction_window = self.current_window_id()

        log.warning(
            f"PAPER TRADE #{len(self.predictions)}: BUY {direction} | "
            f"BTC=${btc_spot:,.2f} | "
            f"YES=${market_yes:.2f} NO=${market_no:.2f} | "
            f"entry=${entry_price:.2f} x {num_tokens:.2f} tokens | "
            f"stake=${stake:.2f} | edge={model_edge:+.4f} | "
            f"balance=${self.balance:.2f}"
        )
        return pred

    def settle_predictions(self, current_btc: Decimal) -> list[Prediction]:
        """Settle pending predictions that are >= 5 minutes old."""
        now = time.time()
        settled: list[Prediction] = []

        still_pending: list[Prediction] = []
        for pred in self.pending:
            elapsed = now - pred.timestamp
            if elapsed >= 300:  # 5 minutes
                pred.btc_exit = current_btc
                pred.settled = True

                # Did BTC go up or down?
                btc_went_up = current_btc > pred.btc_entry

                if pred.direction == "YES":
                    pred.correct = btc_went_up
                else:
                    pred.correct = not btc_went_up

                # Binary option payout
                if pred.correct:
                    # Token pays $1.00 → profit = (1.0 - entry_price) * num_tokens
                    pred.pnl = (Decimal("1.0") - pred.entry_price) * pred.num_tokens
                    self.balance += pred.stake + pred.pnl  # return stake + profit
                    self.wins += 1
                else:
                    # Token pays $0.00 → lose entire stake
                    pred.pnl = -pred.stake
                    self.losses += 1

                self.total_pnl += pred.pnl

                result = "WIN" if pred.correct else "LOSS"
                btc_change = current_btc - pred.btc_entry
                log.warning(
                    f"SETTLED #{self.predictions.index(pred) + 1}: {result} | "
                    f"{pred.direction} @ ${pred.entry_price:.2f} | "
                    f"BTC ${pred.btc_entry:,.2f} -> ${current_btc:,.2f} "
                    f"({btc_change:+,.2f}) | "
                    f"pnl=${pred.pnl:+.2f} | balance=${self.balance:.2f}"
                )
                settled.append(pred)
            else:
                remaining = int(300 - elapsed)
                still_pending.append(pred)

        self.pending = still_pending
        return settled

    def get_scoreboard(self) -> str:
        """Return a formatted scoreboard string."""
        total = self.wins + self.losses
        win_rate = (self.wins / total * 100) if total > 0 else 0
        roi = ((self.balance - self.starting_balance) / self.starting_balance) * 100

        lines = [
            "",
            "=" * 60,
            "  PAPER TRADING SCOREBOARD (Polymarket Synced)",
            "=" * 60,
            f"  Balance:      ${self.balance:.2f} (started: ${self.starting_balance:.2f})",
            f"  ROI:          {roi:+.1f}%",
            f"  Total PnL:    ${self.total_pnl:+.2f}",
            f"  Record:       {self.wins}W - {self.losses}L",
            f"  Win Rate:     {win_rate:.1f}%",
            f"  Pending:      {len(self.pending)}",
        ]

        # Show last 5 trades
        recent = [p for p in self.predictions if p.settled][-5:]
        if recent:
            lines.append("")
            lines.append("  Recent Trades:")
            for p in recent:
                idx = self.predictions.index(p) + 1
                result = "W" if p.correct else "L"
                lines.append(
                    f"    #{idx} {result} {p.direction:3s} "
                    f"@ ${p.entry_price:.2f} | "
                    f"BTC ${p.btc_entry:,.0f}->${p.btc_exit:,.0f} | "
                    f"pnl ${p.pnl:+.2f}"
                )

        lines.append("=" * 60)
        return "\n".join(lines)
