import json
import logging
import asyncio
import websockets
from decimal import Decimal

from nautilus_trader.common.actor import Actor
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder, DataType
from nautilus_trader.model.enums import BookType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

log = logging.getLogger(__name__)


class PolymarketWSSActor(Actor):
    """
    Custom Actor to bridge Polymarket CLOB WSS to Nautilus OrderBooks.

    Subscribes to the 'market' channel and publishes Nautilus OrderBook
    snapshots to the MessageBus for the strategy to consume.
    """

    def __init__(self, asset_ids: list[str]):
        super().__init__()
        self.asset_ids = asset_ids
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self._books: dict[str, OrderBook] = {}
        self._running = False
        self._ws = None
        self._sub_queue: asyncio.Queue = asyncio.Queue()

    def set_assets(self, asset_ids: list[str]):
        """Update the target assets and trigger a new subscription if connected."""
        if set(asset_ids) == set(self.asset_ids):
            return

        self.asset_ids = asset_ids
        self.log.info(f"Updating Polymarket WSS subscription: {asset_ids}")
        if self._running:
            self._sub_queue.put_nowait(asset_ids)

    def on_start(self):
        """Start the WSS connection task."""
        self._running = True
        if self.asset_ids:
            self._reconnect()
        self.log.info(f"PolymarketWSSActor started for assets: {self.asset_ids}")

    def _reconnect(self):
        """Trigger a reconnection attempt via asyncio task."""
        if not self._running:
            return
        asyncio.create_task(self._connect_wss())

    async def _connect_wss(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with websockets.connect(
                self.url, additional_headers=headers
            ) as ws:
                self._ws = ws
                # 1. Subscribe to market channel
                sub_msg = {"type": "market", "assets_ids": self.asset_ids}
                await ws.send(json.dumps(sub_msg))
                self.log.info(f"Subscribed to assets: {self.asset_ids}")

                # 2. Start SUB listener (heartbeat handled by schedule_interval)
                sub_task = asyncio.create_task(self._listen_for_subs(ws))
                
                # 3. Schedule heartbeat (10s)
                self.clock.schedule_interval(10_000_000_000, self._send_heartbeat)

                # 4. Process messages
                async for message in ws:
                    if message == "PONG":
                        continue
                    try:
                        data = json.loads(message)
                        self._handle_message(data)
                    except json.JSONDecodeError:
                        self.log.debug(f"Received non-JSON message: {message}")
                        continue

                sub_task.cancel()
                self._ws = None

        except Exception as e:
            self.log.error(f"Polymarket WSS Error: {e}")
            self._ws = None
            # Reconnect backoff via Clock (5s)
            self.clock.schedule_after(5_000_000_000, self._reconnect)

    def _send_heartbeat(self):
        """Trigger heartbeat send in the event loop."""
        if self._ws:
            asyncio.create_task(self._async_send_ping())

    async def _async_send_ping(self):
        if self._ws:
            try:
                await self._ws.send("PING")
            except:
                pass

    async def _listen_for_subs(self, ws):
        """Listen for dynamic subscription updates while connection is active."""
        while self._running:
            try:
                asset_ids = await self._sub_queue.get()
                sub_msg = {"type": "market", "assets_ids": asset_ids}
                await ws.send(json.dumps(sub_msg))
                self.log.info(f"Dynamically subscribed to assets: {asset_ids}")
            except:
                break

    def _handle_message(self, data: any):
        """Handle incoming WSS messages."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "asset_id" in item:
                    self._process_book_event(item)
            return

        if not isinstance(data, dict):
            return

        event_type = data.get("event_type")

        if event_type == "book":
            self._process_book_event(data)
        elif "price_changes" in data:
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id in self.asset_ids:
                    self.log.debug(f"Price change for {asset_id}")
        elif "event_type" not in data and "asset_id" in data:
            self._process_book_event(data)

    def _timestamp_ns(self) -> int:
        """Return current nanosecond timestamp from the Nautilus clock.

        Extracted as a Python method so tests can monkeypatch it without
        needing to replace the Cython-backed `clock` property.
        """
        return self.clock.timestamp_ns()

    def _process_book_event(self, event: dict):
        """Helper to parse a book object and publish to Nautilus."""
        asset_id = event.get("asset_id")
        bids = event.get("bids", [])
        asks = event.get("asks", [])

        if not asset_id:
            return

        inst_id = InstrumentId.from_str(f"{asset_id}.POLY")

        if inst_id not in self._books:
            self._books[inst_id] = OrderBook(inst_id, BookType.L2_MBP)

        book = self._books[inst_id]
        now_ns = self._timestamp_ns()
        book.clear(now_ns)

        for b in bids:
            order = BookOrder(
                OrderSide.BUY,
                Price.from_str(b["price"]),
                Quantity.from_str(b["size"]),
                0,
            )
            book.add(order, now_ns)
        for a in asks:
            order = BookOrder(
                OrderSide.SELL,
                Price.from_str(a["price"]),
                Quantity.from_str(a["size"]),
                0,
            )
            book.add(order, now_ns)

        self.publish_data(DataType(OrderBook), book)
        self.log.info(
            f"Published OrderBook for {inst_id} (Bids: {len(bids)}, Asks: {len(asks)})"
        )

    def on_stop(self):
        self._running = False
        self.log.info("PolymarketWSSActor stopped.")
