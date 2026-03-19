import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from decimal import Decimal

from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.book import OrderBook

from src.polymarket_wss import PolymarketWSSActor


@pytest.mark.asyncio
async def test_wss_actor_handle_message():
    actor = PolymarketWSSActor(asset_ids=["test_asset"])

    # Patch _timestamp_ns — the thin Python wrapper around self.clock.timestamp_ns().
    # actor.clock is a Cython read-only property; we can't assign to it directly.
    actor._timestamp_ns = lambda: 1_000_000_000

    # Mock message from Polymarket
    msg = {
        "event_type": "book",
        "asset_id": "test_asset",
        "market": "0xmarket",
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.52", "size": "200"}],
    }

    # Mock publish_data
    actor.publish_data = MagicMock()

    actor._handle_message(msg)

    # Verify OrderBook was created and published
    actor.publish_data.assert_called_once()
    # publish_data is called as publish_data(DataType(OrderBook), book)
    book = actor.publish_data.call_args[0][1]

    assert isinstance(book, OrderBook)
    assert str(book.instrument_id) == "test_asset.POLY"
    assert float(book.best_bid_price()) == 0.50
    assert float(book.best_ask_price()) == 0.52
    assert float(book.best_bid_size()) == 100
    assert float(book.best_ask_size()) == 200


@pytest.mark.asyncio
async def test_wss_actor_dynamic_subscription():
    actor = PolymarketWSSActor(asset_ids=["asset1"])
    actor._ws = AsyncMock()
    actor._running = True

    # Initial assets
    assert actor.asset_ids == ["asset1"]

    # Update assets
    actor.set_assets(["asset2"])
    assert actor.asset_ids == ["asset2"]

    # Process subscription queue
    listen_task = asyncio.create_task(actor._listen_for_subs(actor._ws))

    # Give it a moment to process
    await asyncio.sleep(0.1)

    # Verify send was called with new subscription
    actor._ws.send.assert_called_once()
    sent_msg = json.loads(actor._ws.send.call_args[0][0])
    assert sent_msg["type"] == "market"
    assert sent_msg["assets_ids"] == ["asset2"]

    listen_task.cancel()
