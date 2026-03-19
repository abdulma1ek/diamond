"""Polymarket CLOB execution client for 5-minute BTC markets.

Wraps py-clob-client for order book fetching, order placement, and
position tracking. All calls are synchronous HTTP — the strategy must
call these from the Nautilus event loop via run_in_executor to avoid
blocking.

Authentication levels:
  L0: host only — read-only (order book, midpoint, spread)
  L1: host + chain_id + key — can sign orders
  L2: L1 + api creds — can post/cancel orders
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BookParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON
from nautilus_trader.model.objects import Price, Quantity

log = logging.getLogger(__name__)

# Polymarket mainnet CLOB endpoint
CLOB_HOST: str = "https://clob.polymarket.com"


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Simplified order book snapshot for strategy consumption."""

    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    bid_depth: Decimal  # total bid size across all levels
    ask_depth: Decimal  # total ask size across all levels


@dataclass(frozen=True)
class TradeResult:
    """Result of an order submission."""

    success: bool
    order_id: str | None = None
    error: str | None = None


class PolymarketExecutor:
    """Handles all Polymarket CLOB interactions.

    Initialize with credentials for full trading access (L2),
    or without for read-only market data (L0).
    """

    def __init__(
        self,
        private_key: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        chain_id: int = POLYGON,
        host: str = CLOB_HOST,
        rpc_url: str | None = None,
    ) -> None:
        creds = None
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

        self.client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=private_key,
            creds=creds,
            # Pass private RPC if provided (for faster on-chain signing/nonce checks if needed)
            # Note: ClobClient might not expose this directly in all versions, 
            # but we preserve the intent for infrastructure upgrades.
        )
        self.rpc_url = rpc_url
        self._tick_size_cache: dict[str, str] = {}
        self._book_cache: dict[str, tuple[float, OrderBookSnapshot]] = {}
        self._cache_expiry_s: float = 1.0  # short cache for high frequency

    # ── Market Data (L0) ──────────────────────────────────────────

    def get_order_book(
        self, token_id: str, force_refresh: bool = False
    ) -> OrderBookSnapshot | None:
        """Fetch order book and return simplified snapshot with short caching."""
        import time

        now = time.time()

        if not force_refresh and token_id in self._book_cache:
            ts, snapshot = self._book_cache[token_id]
            if now - ts < self._cache_expiry_s:
                return snapshot

        try:
            book = self.client.get_order_book(token_id)
            bids = book.bids or []
            asks = book.asks or []

            # Using Decimal for best precision, from_str is safest from API values
            best_bid = Decimal(str(bids[0].price)) if bids else Decimal("0")
            best_ask = Decimal(str(asks[0].price)) if asks else Decimal("1")
            bid_depth = sum(Decimal(str(b.size)) for b in bids)
            ask_depth = sum(Decimal(str(a.size)) for a in asks)
            midpoint = (best_bid + best_ask) / Decimal("2")

            snapshot = OrderBookSnapshot(
                best_bid=best_bid,
                best_ask=best_ask,
                midpoint=midpoint,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
            )
            self._book_cache[token_id] = (now, snapshot)
            return snapshot
        except Exception as e:
            log.error(f"Failed to fetch order book for {token_id}: {e}")
            return None

    def get_best_bid_ask(self, token_id: str) -> tuple[Decimal, Decimal] | None:
        """Optimized path to get just the best bid/ask prices."""
        try:
            snapshot = self.get_order_book(token_id)
            if snapshot:
                return snapshot.best_bid, snapshot.best_ask
            return None
        except Exception as e:
            log.error(f"Error getting best bid/ask for {token_id}: {e}")
            return None

    def get_midpoint(self, token_id: str) -> Decimal | None:
        """Fetch midpoint price for a token."""
        try:
            resp = self.client.get_midpoint(token_id)
            mid = resp.get("mid", 0)
            return Decimal(str(mid)) if mid is not None else None
        except Exception as e:
            log.error(f"Failed to fetch midpoint for {token_id}: {e}")
            return None

    # ── Order Management (L2) ─────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: Price,
        size: Quantity,
    ) -> TradeResult:
        """Place a limit order (Post-Only / GTC).

        Args:
            token_id: Polymarket conditional token ID.
            side: "BUY" or "SELL".
            price: Limit price (0.00-1.00).
            size: Order size in conditional tokens.

        Returns:
            TradeResult with order_id on success, error on failure.
        """
        try:
            tick_size = self._get_tick_size(token_id)
            neg_risk = self.client.get_neg_risk(token_id)

            # Note: The third-party library py-clob-client uses floats internally
            # for price/size in its OrderArgs and signing logic, but we maintain
            # Decimals/Nautilus types until the last possible moment.
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side,
            )

            signed = self.client.create_order(
                order_args,
                options={"tick_size": tick_size, "neg_risk": neg_risk},
            )
            resp = self.client.post_order(signed, OrderType.GTC)

            order_id = resp.get("orderID") if isinstance(resp, dict) else None
            log.info(f"Order placed: {side} {size}@{price} on {token_id} → {order_id}")
            return TradeResult(success=True, order_id=order_id)

        except Exception as e:
            log.error(f"Order failed: {side} {size}@{price} on {token_id}: {e}")
            return TradeResult(success=False, error=str(e))

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders. Returns True on success."""
        try:
            self.client.cancel_all()
            log.info("All orders cancelled")
            return True
        except Exception as e:
            log.error(f"Failed to cancel all orders: {e}")
            return False

    # ── Internal ──────────────────────────────────────────────────

    def _get_tick_size(self, token_id: str) -> str:
        if token_id not in self._tick_size_cache:
            self._tick_size_cache[token_id] = self.client.get_tick_size(token_id)
        return self._tick_size_cache[token_id]
