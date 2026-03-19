"""WebSocket connection manager with automatic failover.

Manages multiple WebSocket connections to Binance Futures and Polymarket,
automatically failing over to the next endpoint when the primary disconnects.
Supports exponential backoff reconnection, connection health tracking, and
seamless subscription state transfer on failover.

Architecture:
  WsManager
    ├── _connections: dict[EndpointKey, WebSocketConnection]
    ├── _primary: EndpointKey — currently active primary connection
    ├── _fallback_order: list[EndpointKey] — ordered failover priority
    ├── _subscriptions: set[str] — active channel subscriptions
    └── _health: dict[EndpointKey, ConnectionHealth]

Usage:
    manager = WsManager()
    manager.add_endpoint(
        key="binance_primary",
        url="wss://stream.binance.com:9443/ws",
        subscriptions=["btcusdt@trade", "btcusdt@depth20"],
        ping_interval=20,
        ping_timeout=10,
    )
    manager.add_endpoint(
        key="binance_backup",
        url="wss://stream.binance.com:9443/ws",
        subscriptions=["btcusdt@trade", "btcusdt@depth20"],
    )
    manager.on_message("binance_primary", lambda msg: process(msg))
    manager.on_disconnect("binance_primary", lambda: logger.warning("Primary disconnected"))
    await manager.connect()
    # Manager auto-failovers, reconnects, and maintains subscriptions
    await manager.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

import websockets
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

_logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


@dataclass
class ConnectionHealth:
    """Health metrics for a single WebSocket endpoint."""

    key: str
    state: ConnectionState
    latency_ms: float = 0.0
    last_pong_unix: float = 0.0
    last_error: str | None = None
    reconnect_attempts: int = 0
    total_messages: int = 0
    connected_since_unix: float = 0.0


@dataclass
class EndpointConfig:
    """Configuration for a single WebSocket endpoint."""

    key: str
    url: str
    subscriptions: list[str] = field(default_factory=list)
    ping_interval: float = 20.0  # seconds
    ping_timeout: float = 10.0   # seconds
    max_reconnect_attempts: int = 10
    reconnect_base_delay: float = 1.0  # seconds
    reconnect_max_delay: float = 60.0   # seconds
    message_timeout: float = 5.0   # seconds to wait for a message before checking liveness


class WebSocketConnection:
    """A single WebSocket connection with reconnect logic."""

    def __init__(self, config: EndpointConfig):
        self.config = config
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._state = ConnectionState.DISCONNECTED
        self._reconnect_attempts = 0
        self._last_pong_unix = 0.0
        self._total_messages = 0
        self._connected_since_unix = 0.0
        self._last_message_unix = 0.0
        self._running = False

        # Callbacks
        self._on_message: Callable[[str], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None
        self._on_connect: Callable[[], None] | None = None
        self._on_health_change: Callable[[ConnectionHealth], None] | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def health(self) -> ConnectionHealth:
        return ConnectionHealth(
            key=self.config.key,
            state=self._state,
            latency_ms=0.0,  # computed by manager
            last_pong_unix=self._last_pong_unix,
            last_error=None,
            reconnect_attempts=self._reconnect_attempts,
            total_messages=self._total_messages,
            connected_since_unix=self._connected_since_unix,
        )

    def on_message(self, cb: Callable[[str], None]) -> "WebSocketConnection":
        self._on_message = cb
        return self

    def on_disconnect(self, cb: Callable[[], None]) -> "WebSocketConnection":
        self._on_disconnect = cb
        return self

    def on_connect(self, cb: Callable[[], None]) -> "WebSocketConnection":
        self._on_connect = cb
        return self

    def on_health_change(self, cb: Callable[[ConnectionHealth], None]) -> "WebSocketConnection":
        self._on_health_change = cb
        return self

    async def connect(self) -> None:
        """Establish the WebSocket connection and start read loop."""
        if self._running:
            return
        self._running = True
        self._state = ConnectionState.CONNECTING

        try:
            self._ws = await ws_connect(
                self.config.url,
                ping_interval=self.config.ping_interval,
                ping_timeout=self.config.ping_timeout,
                open_timeout=10.0,
                close_timeout=5.0,
            )
            self._state = ConnectionState.CONNECTED
            self._connected_since_unix = time.time()
            self._last_pong_unix = time.time()
            self._reconnect_attempts = 0
            _logger.info(f"[{self.config.key}] Connected to {self.config.url}")

            if self._on_connect:
                self._on_connect()

            # Send initial subscriptions
            for sub in self.config.subscriptions:
                await self._send_subscription(sub)

            # Start read loop
            await self._read_loop()

        except Exception as e:
            self._state = ConnectionState.DISCONNECTED
            _logger.error(f"[{self.config.key}] Connection error: {e}")
            raise

    async def _send_subscription(self, subscription: str) -> None:
        """Send a subscription/unsubscription message."""
        if self._ws is None:
            return
        # Binance format: {"method": "SUBSCRIBE", "params": ["btcusdt@trade"], "id": 1}
        msg = json.dumps({"method": "SUBSCRIBE", "params": [subscription], "id": 1})
        await self._ws.send(msg)
        _logger.debug(f"[{self.config.key}] Subscribed to {subscription}")

    async def _read_loop(self) -> None:
        """Main message reading loop with reconnect-on-disconnect."""
        while self._running:
            try:
                if self._ws is None:
                    break
                msg = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=self.config.message_timeout,
                )
                self._total_messages += 1
                self._last_message_unix = time.time()

                if msg is None:
                    continue

                # Handle pong response (websockets library handles ping automatically,
                # but we track last_pong on the handler side)
                if isinstance(msg, str):
                    data = json.loads(msg)
                    # Binance sends pong responses to ping id; update last_pong
                    if data.get("result") is None and "id" in data:
                        self._last_pong_unix = time.time()

                if self._on_message:
                    self._on_message(msg if isinstance(msg, str) else msg.decode())

            except asyncio.TimeoutError:
                # No message received within timeout — check if connection is still alive
                if self._ws and self._ws.open:
                    # Connection is open but quiet — this is normal for a streaming WS
                    continue
                else:
                    break

            except ConnectionClosed as e:
                _logger.warning(
                    f"[{self.config.key}] Connection closed: code={e.code} reason={e.reason}"
                )
                self._state = ConnectionState.RECONNECTING
                if self._on_disconnect:
                    self._on_disconnect()
                break

            except Exception as e:
                _logger.error(f"[{self.config.key}] Read loop error: {e}")
                self._state = ConnectionState.RECONNECTING
                if self._on_disconnect:
                    self._on_disconnect()
                break

    async def reconnect(self) -> bool:
        """Attempt to reconnect with exponential backoff. Returns True on success."""
        self._state = ConnectionState.RECONNECTING
        self._reconnect_attempts += 1

        attempt = self._reconnect_attempts
        delay = min(
            self.config.reconnect_base_delay * (2 ** (attempt - 1)),
            self.config.reconnect_max_delay,
        )

        _logger.info(
            f"[{self.config.key}] Reconnecting in {delay:.1f}s "
            f"(attempt {attempt}/{self.config.max_reconnect_attempts})"
        )
        await asyncio.sleep(delay)

        if not self._running:
            return False

        try:
            await self.connect()
            return True
        except Exception as e:
            _logger.error(f"[{self.config.key}] Reconnect failed: {e}")
            if self._reconnect_attempts >= self.config.max_reconnect_attempts:
                self._state = ConnectionState.FAILED
            return False

    async def stop(self) -> None:
        """Gracefully stop the connection."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close(code=1000, reason="Manager shutdown")
            except Exception:
                pass
        self._state = ConnectionState.DISCONNECTED
        _logger.info(f"[{self.config.key}] Stopped")

    async def resubscribe(self) -> None:
        """Resend all subscriptions — called after failover reconnect."""
        for sub in self.config.subscriptions:
            await self._send_subscription(sub)


class WsManager:
    """
    Multi-endpoint WebSocket manager with automatic failover.

    Maintains an ordered priority list of endpoints. When the primary
    disconnects, the manager automatically promotes the next available
    endpoint and resubscribes to all active channels.

    Usage:
        manager = WsManager()
        manager.add_endpoint(key="binance_1", url="wss://...", subscriptions=[...])
        manager.add_endpoint(key="binance_2", url="wss://...", subscriptions=[...])
        manager.on_message(lambda key, msg: handle(key, msg))
        await manager.connect()
    """

    def __init__(self):
        self._endpoints: dict[str, EndpointConfig] = {}
        self._connections: dict[str, WebSocketConnection] = {}
        self._primary: str | None = None
        self._fallback_order: list[str] = []
        self._subscriptions: dict[str, set[str]] = {}  # key → subscriptions
        self._running = False
        self._manager_task: asyncio.Task | None = None

        # Callbacks
        self._on_message: Callable[[str, str], None] | None = None
        self._on_failover: Callable[[str, str], None] | None = None  # old_key, new_key

    def add_endpoint(self, **kwargs) -> "WsManager":
        """Add an endpoint configuration. Chainable."""
        config = EndpointConfig(**kwargs)
        self._endpoints[config.key] = config
        self._connections[config.key] = WebSocketConnection(config)
        self._subscriptions[config.key] = set(config.subscriptions)
        return self

    def on_message(self, cb: Callable[[str, str], None]) -> "WsManager":
        """Set message handler (endpoint_key, message). Chainable."""
        self._on_message = cb
        return self

    def on_failover(self, cb: Callable[[str, str], None]) -> "WsManager":
        """Set failover handler (old_key, new_key). Chainable."""
        self._on_failover = cb
        return self

    def set_primary(self, key: str) -> None:
        """Promote an endpoint to primary."""
        if key not in self._endpoints:
            raise ValueError(f"Unknown endpoint: {key}")
        old_primary = self._primary
        self._primary = key
        if key in self._fallback_order:
            self._fallback_order.remove(key)
        # Reorder: primary first, then fallback_order
        self._fallback_order = [key] + [
            k for k in self._fallback_order if k != key
        ]
        if old_primary and old_primary != key:
            _logger.info(f"Failover: {old_primary} → {key}")

    def _get_next_primary(self, current: str) -> str | None:
        """Get the next available endpoint in fallback order."""
        try:
            idx = self._fallback_order.index(current)
            if idx + 1 < len(self._fallback_order):
                return self._fallback_order[idx + 1]
        except ValueError:
            pass
        return self._fallback_order[0] if self._fallback_order else None

    async def connect(self) -> None:
        """Connect all endpoints, set primary, start manager loop."""
        if not self._endpoints:
            raise RuntimeError("No endpoints configured — call add_endpoint() first")
        self._running = True

        # Set fallback order
        self._fallback_order = list(self._endpoints.keys())
        self._primary = self._fallback_order[0]

        # Wire up callbacks for each connection
        for key, conn in self._connections.items():
            conn.on_message(lambda msg, k=key: self._handle_message(k, msg))
            conn.on_disconnect(lambda k=key: asyncio.create_task(self._handle_disconnect(k)))
            conn.on_connect(lambda k=key: self._handle_connect(k))

        # Connect all endpoints concurrently
        await asyncio.gather(
            *[conn.connect() for conn in self._connections.values()],
            return_exceptions=True,
        )

        _logger.info(f"WsManager started — primary={self._primary}")

    def _handle_message(self, key: str, msg: str) -> None:
        if self._on_message:
            self._on_message(key, msg)

    def _handle_connect(self, key: str) -> None:
        _logger.info(f"[WsManager] {key} connected")

    async def _handle_disconnect(self, key: str) -> None:
        """Handle disconnect and trigger failover if needed."""
        if not self._running:
            return

        conn = self._connections[key]

        # If disconnected primary, failover
        if key == self._primary:
            new_primary = self._get_next_primary(key)
            if new_primary:
                self.set_primary(new_primary)
                old_key, new_key = key, new_primary
                _logger.warning(f"Primary {key} disconnected — failing over to {new_primary}")

                # Wait for new primary to connect before proceeding
                # Cancel reconnect on old
                await conn.stop()

                if self._on_failover:
                    self._on_failover(old_key, new_key)
            else:
                _logger.error(f"No fallback endpoint available for {key}")

        # Attempt reconnect in background
        asyncio.create_task(self._reconnect_loop(key))

    async def _reconnect_loop(self, key: str) -> None:
        """Reconnect loop with backoff."""
        conn = self._connections[key]
        while self._running and conn.state in (ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED):
            try:
                await conn.connect()
                # Re-subscribe
                await conn.resubscribe()
                _logger.info(f"[WsManager] {key} reconnected")
                break
            except Exception as e:
                _logger.warning(f"[WsManager] {key} reconnect failed: {e}")
                await asyncio.sleep(2)
            if conn.state == ConnectionState.CONNECTED:
                break

    async def stop(self) -> None:
        """Stop all connections."""
        self._running = False
        await asyncio.gather(
            *[conn.stop() for conn in self._connections.values()],
            return_exceptions=True,
        )
        _logger.info("WsManager stopped")

    @property
    def primary(self) -> str | None:
        return self._primary

    @property
    def all_health(self) -> dict[str, ConnectionHealth]:
        return {key: conn.health for key, conn in self._connections.items()}
