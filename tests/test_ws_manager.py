import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch, sentinel
from src.ws_manager import (
    WsManager,
    WebSocketConnection,
    EndpointConfig,
    ConnectionHealth,
    ConnectionState,
)


# ─── EndpointConfig ────────────────────────────────────────────────────────────


class TestEndpointConfig:
    def test_default_values(self):
        cfg = EndpointConfig(key="test", url="wss://example.com")
        assert cfg.key == "test"
        assert cfg.url == "wss://example.com"
        assert cfg.subscriptions == []
        assert cfg.ping_interval == 20.0
        assert cfg.ping_timeout == 10.0
        assert cfg.max_reconnect_attempts == 10
        assert cfg.reconnect_base_delay == 1.0
        assert cfg.reconnect_max_delay == 60.0

    def test_custom_values(self):
        cfg = EndpointConfig(
            key="binance",
            url="wss://stream.binance.com:9443/ws",
            subscriptions=["btcusdt@trade"],
            ping_interval=30.0,
            max_reconnect_attempts=5,
        )
        assert cfg.subscriptions == ["btcusdt@trade"]
        assert cfg.ping_interval == 30.0
        assert cfg.max_reconnect_attempts == 5


# ─── ConnectionHealth ───────────────────────────────────────────────────────────


class TestConnectionHealth:
    def test_construction(self):
        h = ConnectionHealth(key="test", state=ConnectionState.CONNECTED)
        assert h.key == "test"
        assert h.state == ConnectionState.CONNECTED
        assert h.reconnect_attempts == 0
        assert h.total_messages == 0


# ─── WebSocketConnection ───────────────────────────────────────────────────────


class TestWebSocketConnectionCallbacks:
    def test_on_message_returns_self(self):
        cfg = EndpointConfig(key="test", url="wss://example.com")
        conn = WebSocketConnection(cfg)
        result = conn.on_message(lambda msg: None)
        assert result is conn

    def test_on_disconnect_returns_self(self):
        cfg = EndpointConfig(key="test", url="wss://example.com")
        conn = WebSocketConnection(cfg)
        result = conn.on_disconnect(lambda: None)
        assert result is conn

    def test_on_connect_returns_self(self):
        cfg = EndpointConfig(key="test", url="wss://example.com")
        conn = WebSocketConnection(cfg)
        result = conn.on_connect(lambda: None)
        assert result is conn

    def test_state_starts_disconnected(self):
        cfg = EndpointConfig(key="test", url="wss://example.com")
        conn = WebSocketConnection(cfg)
        assert conn.state == ConnectionState.DISCONNECTED

    def test_health_reflects_state(self):
        cfg = EndpointConfig(key="test", url="wss://example.com")
        conn = WebSocketConnection(cfg)
        h = conn.health
        assert h.key == "test"
        assert h.state == ConnectionState.DISCONNECTED


# ─── WsManager ────────────────────────────────────────────────────────────────


class TestWsManagerConstruction:
    def test_empty_manager(self):
        mgr = WsManager()
        assert mgr.primary is None
        assert len(mgr.all_health) == 0

    def test_add_endpoint_chainable(self):
        mgr = WsManager()
        result = mgr.add_endpoint(key="binance", url="wss://binance.com/ws")
        assert result is mgr

    def test_add_multiple_endpoints(self):
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")
        assert len(mgr.all_health) == 2

    def test_set_primary(self):
        mgr = WsManager()
        mgr.add_endpoint(key="primary", url="wss://primary.com")
        mgr.add_endpoint(key="backup", url="wss://backup.com")
        mgr.set_primary("backup")
        assert mgr.primary == "backup"


class TestWsManagerCallbacks:
    def test_on_message_chainable(self):
        mgr = WsManager()
        result = mgr.on_message(lambda k, msg: None)
        assert result is mgr

    def test_on_failover_chainable(self):
        mgr = WsManager()
        result = mgr.on_failover(lambda old, new: None)
        assert result is mgr


class TestWsManagerFailoverLogic:
    def test_get_next_primary_returns_next_in_order(self):
        """_get_next_primary returns the endpoint after the given one in fallback_order."""
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")
        mgr.add_endpoint(key="c", url="wss://c.com")
        mgr.set_primary("a")
        # fallback_order = [a, b, c]; next after a is b
        next_key = mgr._get_next_primary("a")
        assert next_key == "b"

    def test_get_next_primary_wraps_around(self):
        """Last endpoint wraps to first."""
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")
        mgr.set_primary("a")
        # With a as primary, fallback_order = [a, b]; next after b is a
        next_key = mgr._get_next_primary("b")
        assert next_key == "a"

    def test_set_primary_keeps_all_endpoints_in_order(self):
        """set_primary reorders but never drops endpoints."""
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")
        mgr.add_endpoint(key="c", url="wss://c.com")
        mgr.set_primary("b")
        assert mgr.primary == "b"
        # All 3 endpoints must still be present
        assert set(mgr._fallback_order) == {"a", "b", "c"}
        # b must be first
        assert mgr._fallback_order[0] == "b"

    def test_primary_pushed_to_front_on_set_primary(self):
        """Calling set_primary twice puts the new primary at front."""
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")
        mgr.set_primary("b")
        mgr.set_primary("a")
        assert mgr._fallback_order[0] == "a"


class TestWsManagerConnect:
    @pytest.mark.asyncio
    async def test_connect_requires_endpoints(self):
        mgr = WsManager()
        with pytest.raises(RuntimeError, match="No endpoints configured"):
            await mgr.connect()

    @pytest.mark.asyncio
    async def test_connect_all_endpoints(self):
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")

        with patch.object(WebSocketConnection, "connect", new_callable=AsyncMock) as mock_connect:
            await mgr.connect()
            assert mock_connect.call_count == 2
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_all(self):
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        with patch.object(WebSocketConnection, "connect", new_callable=AsyncMock):
            await mgr.connect()
            await mgr.stop()
            assert mgr._running is False


class TestWsManagerHealth:
    def test_all_health_returns_dict(self):
        mgr = WsManager()
        mgr.add_endpoint(key="a", url="wss://a.com")
        mgr.add_endpoint(key="b", url="wss://b.com")
        health = mgr.all_health
        assert isinstance(health, dict)
        assert len(health) == 2
        assert "a" in health
        assert "b" in health


# ─── ConnectionState enum ───────────────────────────────────────────────────────


class TestConnectionState:
    def test_all_states_present(self):
        assert ConnectionState.DISCONNECTED.value == "DISCONNECTED"
        assert ConnectionState.CONNECTING.value == "CONNECTING"
        assert ConnectionState.CONNECTED.value == "CONNECTED"
        assert ConnectionState.RECONNECTING.value == "RECONNECTING"
        assert ConnectionState.FAILED.value == "FAILED"
