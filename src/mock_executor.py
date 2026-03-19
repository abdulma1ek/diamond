"""Mock Polymarket executor for backtesting.

Records all order submissions and simulates fills at the requested price.
Implements the same interface as PolymarketExecutor so the strategy
code runs identically in backtest and live modes.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from nautilus_trader.model.objects import Price, Quantity

from src.execution import OrderBookSnapshot, TradeResult

log = logging.getLogger(__name__)


@dataclass
class FillRecord:
    """A single simulated fill."""

    side: str
    price: Price
    size: Quantity
    token_id: str


class MockExecutor:
    """Drop-in replacement for PolymarketExecutor during backtesting.

    - get_midpoint() returns a configurable value (set by the backtest harness)
    - place_limit_order() always fills and records the trade
    - cancel_all_orders() clears the log
    """

    def __init__(self, default_midpoint: Decimal = Decimal("0.50")) -> None:
        self._midpoint: Decimal = default_midpoint
        self.fills: list[FillRecord] = []
        self.cancelled_count: int = 0

    # ── Control (called by backtest harness) ─────────────────────

    def set_midpoint(self, midpoint: Decimal) -> None:
        """Set the simulated market midpoint (called each bar by the harness)."""
        self._midpoint = midpoint

    # ── Same interface as PolymarketExecutor ─────────────────────

    def get_order_book(self, token_id: str) -> OrderBookSnapshot | None:
        spread = Decimal("0.02")
        mid = self._midpoint
        return OrderBookSnapshot(
            best_bid=mid - spread / Decimal("2"),
            best_ask=mid + spread / Decimal("2"),
            midpoint=mid,
            bid_depth=Decimal("100.0"),
            ask_depth=Decimal("100.0"),
        )

    def get_midpoint(self, token_id: str) -> Decimal | None:
        return self._midpoint

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: Price,
        size: Quantity,
    ) -> TradeResult:
        """Simulate a fill at the requested price."""
        fill = FillRecord(
            side=side,
            price=price,
            size=size,
            token_id=token_id,
        )
        self.fills.append(fill)
        order_id = f"MOCK-{len(self.fills):04d}"
        log.info(f"[MockExecutor] Fill: {side} {size}@{price} → {order_id}")
        return TradeResult(success=True, order_id=order_id)

    def cancel_all_orders(self) -> bool:
        self.cancelled_count += 1
        return True

    # ── Analysis helpers ─────────────────────────────────────────

    def total_trades(self) -> int:
        return len(self.fills)

    def pnl_summary(self) -> dict:
        """Compute basic PnL assuming binary outcome (win=1.0, lose=0.0).

        This is a simplified view — actual PnL depends on market resolution.
        Returns dict with buy_count, sell_count, total_exposure, avg_price.
        """
        buys = [f for f in self.fills if f.side == "BUY"]
        sells = [f for f in self.fills if f.side == "SELL"]

        buy_exposure = sum(Decimal(str(f.price)) * Decimal(str(f.size)) for f in buys)
        sell_exposure = sum(Decimal(str(f.price)) * Decimal(str(f.size)) for f in sells)

        return {
            "total_trades": len(self.fills),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_exposure": buy_exposure,
            "sell_exposure": sell_exposure,
            "avg_buy_price": sum(Decimal(str(f.price)) for f in buys) / len(buys) if buys else Decimal("0"),
            "avg_sell_price": sum(Decimal(str(f.price)) for f in sells) / len(sells) if sells else Decimal("0"),
        }
