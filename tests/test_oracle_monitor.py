import pytest
import asyncio
import time
from unittest.mock import patch, MagicMock

from src.oracle_monitor import (
    OracleLagMonitor,
    OracleLagConfig,
    ChainlinkDataStreamsClient,
    FeedHealth,
    FeedStatus,
    OracleLagSnapshot,
)


class TestFeedStatus:
    def test_status_values(self):
        assert FeedStatus.HEALTHY.value == "HEALTHY"
        assert FeedStatus.STALE.value == "STALE"
        assert FeedStatus.UNRESPONSIVE.value == "UNRESPONSIVE"
        assert FeedStatus.DISCONNECTED.value == "DISCONNECTED"


class TestFeedHealth:
    def test_default_construction(self):
        h = FeedHealth(
            feed_name="BTC/USD",
            status=FeedStatus.HEALTHY,
            latency_ms=12.5,
            last_update_unix=time.time(),
            staleness_ms=12.5,
        )
        assert h.feed_name == "BTC/USD"
        assert h.status == FeedStatus.HEALTHY
        assert h.reports_received == 0
        assert h.staleness_breaches == 0


class TestOracleLagConfig:
    def test_defaults(self):
        cfg = OracleLagConfig()
        assert cfg.staleness_threshold_ms == 500.0
        assert cfg.heartbeat_threshold_ms == 300.0
        assert cfg.check_interval_ms == 200
        assert cfg.max_breaches_before_halt == 3
        assert cfg.auto_resume is True
        assert cfg.resume_cooldown_s == 5.0


class TestChainlinkDataStreamsClient:
    def test_client_init(self):
        client = ChainlinkDataStreamsClient("https://polygon-mainnet.g.alchemy.com/v2/key")
        assert client.rpc_url == "https://polygon-mainnet.g.alchemy.com/v2/key"
        assert client._timeout_s == 2.0


class TestOracleLagMonitorConstruction:
    def test_default_feeds(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        assert monitor.feeds == ["BTC/USD"]

    def test_custom_feeds(self):
        monitor = OracleLagMonitor(
            rpc_url="https://example.com",
            feeds=["BTC/USD", "ETH/USD", "SOL/USD"],
        )
        assert len(monitor.feeds) == 3
        assert "ETH/USD" in monitor.feeds

    def test_custom_config(self):
        cfg = OracleLagConfig(staleness_threshold_ms=1000.0, auto_resume=False)
        monitor = OracleLagMonitor(rpc_url="https://example.com", config=cfg)
        assert monitor.config.staleness_threshold_ms == 1000.0
        assert monitor.config.auto_resume is False

    def test_initial_not_halted(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        assert monitor.is_halted is False
        assert monitor.should_halt() is False


class TestOracleLagMonitorSnapshot:
    def test_empty_snapshot(self):
        # Even with an explicit empty list, constructor defaults to ["BTC/USD"]
        # since feeds=[]
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        snap = monitor.snapshot()
        assert snap.overall_status == FeedStatus.HEALTHY
        assert snap.worst_latency_ms == float("inf")
        assert snap.feed_count == 1  # default BTC/USD
        assert snap.healthy_feed_count == 0

    def test_snapshot_with_feeds(self):
        monitor = OracleLagMonitor(
            rpc_url="https://example.com",
            feeds=["BTC/USD", "ETH/USD"],
        )
        snap = monitor.snapshot()
        assert snap.feed_count == 2
        assert snap.timestamp_unix > 0


class TestOracleLagMonitorStalenessLogic:
    def test_should_halt_when_stale_and_not_already_halted(self):
        monitor = OracleLagMonitor(
            rpc_url="https://example.com",
            feeds=["BTC/USD"],
            config=OracleLagConfig(max_breaches_before_halt=1),
        )
        # Simulate a stale feed by directly setting health
        monitor._feed_health["BTC/USD"] = FeedHealth(
            feed_name="BTC/USD",
            status=FeedStatus.STALE,
            latency_ms=600.0,
            last_update_unix=time.time() - 10,
            staleness_ms=600.0,
            staleness_breaches=3,
        )
        assert monitor.should_halt() is True

    def test_should_not_halt_when_all_healthy(self):
        monitor = OracleLagMonitor(
            rpc_url="https://example.com",
            feeds=["BTC/USD"],
            config=OracleLagConfig(auto_resume=True),
        )
        monitor._feed_health["BTC/USD"] = FeedHealth(
            feed_name="BTC/USD",
            status=FeedStatus.HEALTHY,
            latency_ms=10.0,
            last_update_unix=time.time(),
            staleness_ms=10.0,
            staleness_breaches=0,
        )
        monitor._halted = False
        monitor._halted_since = time.time() - 1
        # Since auto_resume=True and feeds are healthy, it should not stay halted
        assert monitor.should_halt() is False

    def test_auto_resume_after_cooldown(self):
        monitor = OracleLagMonitor(
            rpc_url="https://example.com",
            feeds=["BTC/USD"],
            config=OracleLagConfig(auto_resume=True, resume_cooldown_s=1.0),
        )
        # Simulate all feeds healthy and recovery time passed
        monitor._halted = True
        monitor._halted_since = time.time() - 2  # past cooldown
        monitor._feed_health["BTC/USD"] = FeedHealth(
            feed_name="BTC/USD",
            status=FeedStatus.HEALTHY,
            latency_ms=10.0,
            last_update_unix=time.time(),
            staleness_ms=10.0,
            staleness_breaches=0,
        )
        # should_halt should un-halt after cooldown
        assert monitor.should_halt() is False
        assert monitor.is_halted is False

    def test_halted_duration_property(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        assert monitor.halted_duration_s is None
        monitor._halted = True
        monitor._halted_since = time.time() - 3.0
        dur = monitor.halted_duration_s
        assert dur is not None
        assert dur >= 3.0


class TestChainlinkClientMock:
    def test_get_feed_health_returns_none_on_error(self):
        client = ChainlinkDataStreamsClient("https://bad-url")
        with patch.object(client._session, "post", side_effect=Exception("Network error")):
            result = client.get_feed_health("BTC/USD")
            assert result is None

    def test_get_latest_report_returns_none_on_error(self):
        client = ChainlinkDataStreamsClient("https://bad-url")
        with patch.object(client._session, "post", side_effect=Exception("Timeout")):
            result = client.get_latest_report("BTC/USD")
            assert result is None


class TestMonitorAsyncLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        await monitor.start()
        assert monitor._running is True
        await monitor.stop()
        assert monitor._running is False

    @pytest.mark.asyncio
    async def test_double_start_noop(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        await monitor.start()
        await monitor.start()  # Should be no-op
        assert monitor._running is True
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_noop(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        await monitor.stop()  # Should not raise
        assert monitor._running is False


class TestRepr:
    def test_repr(self):
        monitor = OracleLagMonitor(rpc_url="https://example.com")
        r = repr(monitor)
        assert "OracleLagMonitor" in r
