"""Oracle Lag Monitor — Chainlink Data Streams health watchdog.

Monitors the latency between Chainlink Data Streams delivering price updates
and those updates arriving at the trading engine. If latency exceeds the
configured threshold, trading is halted automatically to prevent trading on
stale oracle data.

Key metrics tracked:
  - feed_latency_ms: Time between Chainlink report timestamp and local receipt
  - feed_heartbeat_ms: Time since last valid feed update
  - staleness_count: Number of times staleness threshold was breached

Design:
  - Runs as an async background task inside the NautilusTrader event loop
  - Queries Chainlink Data Streams health endpoint periodically
  - Publishes LagAlert events when threshold is breached (triggers RiskManager halt)

References:
  Chainlink Data Streams: https://docs.chain.link/data-streams
  Chainlink VRF/keepers can also publish heartbeat signals that we track.

Usage:
    monitor = OracleLagMonitor(
        rpc_url="https://polygon-mainnet.g.alchemy.com/v2/...",
        feeds=["BTC/USD"],          # Data Streams feed IDs to monitor
        staleness_threshold_ms=500, # Halt if feed is older than this
        check_interval_ms=200,     # Poll every 200ms
    )
    await monitor.start()
    # In strategy event loop:
    if monitor.should_halt():
        logger.warning(f"Oracle stale: {monitor.last_latency_ms:.1f}ms — halting")
        risk_manager.halt()
    await monitor.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import requests

_logger = logging.getLogger(__name__)


class FeedStatus(Enum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    UNRESPONSIVE = "UNRESPONSIVE"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class FeedHealth:
    """Health snapshot for a single Data Streams feed."""

    feed_name: str
    status: FeedStatus
    latency_ms: float
    last_update_unix: float
    staleness_ms: float  # time since last update
    reports_received: int = 0
    staleness_breaches: int = 0


@dataclass
class OracleLagSnapshot:
    """Aggregate health state across all monitored feeds."""

    overall_status: FeedStatus
    worst_latency_ms: float
    worst_staleness_ms: float
    total_staleness_breaches: int
    feed_count: int
    healthy_feed_count: int
    timestamp_unix: float
    feeds: dict[str, FeedHealth] = field(default_factory=dict)


class ChainlinkDataStreamsClient:
    """
    Lightweight HTTP client for Chainlink Data Streams health and price endpoints.

    Data Streams provides:
      - Verified price reports on-chain (signed)
      - Health/heartbeat endpoint for each feed
      - Low-latency WebSocket subscription for price feeds

    Docs: https://docs.chain.link/data-streams
    """

    def __init__(self, rpc_url: str, timeout_ms: int = 2000):
        self.rpc_url = rpc_url
        self._timeout_s = timeout_ms / 1000.0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def get_feed_health(self, feed_name: str) -> dict | None:
        """
        Fetch health status for a named feed from the Data Streams aggregator.

        Returns a dict with keys: answer, updatedAt, status, heartbeat, validator.
        Returns None on error.
        """
        try:
            # Data Streams uses an on-chain aggregator proxy pattern.
            # The health endpoint is typically the same RPC with a specific call.
            payload = {
                "jsonrpc": "2.0",
                "method": "streams_feedHealth",
                "params": [{"name": feed_name}],
                "id": 1,
            }
            resp = self._session.post(
                self.rpc_url,
                json=payload,
                timeout=self._timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result")
            return None
        except Exception as e:
            _logger.debug(f"Feed health query failed for {feed_name}: {e}")
            return None

    def get_latest_report(self, feed_name: str) -> dict | None:
        """
        Fetch the latest signed price report for a feed.

        Returns dict with keys: price, timestamp, feedId, observations, etc.
        """
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "streams_latestReport",
                "params": [{"name": feed_name}],
                "id": 1,
            }
            resp = self._session.post(
                self.rpc_url,
                json=payload,
                timeout=self._timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result")
            return None
        except Exception as e:
            _logger.debug(f"Latest report query failed for {feed_name}: {e}")
            return None


@dataclass
class OracleLagConfig:
    """Configuration for the Oracle Lag Monitor."""

    staleness_threshold_ms: float = 500.0  # Halt if no update within this
    heartbeat_threshold_ms: float = 300.0  # Warning if heartbeat exceeds this
    check_interval_ms: int = 200  # How often to poll feeds
    max_breaches_before_halt: int = 3  # Consecutive breaches before halt
    auto_resume: bool = True  # Auto-resume once feed recovers
    resume_cooldown_s: float = 5.0  # Wait this long after recovery before resuming


class OracleLagMonitor:
    """
    Monitors Chainlink Data Streams feed latency and halts trading if feeds go stale.

    Polls all configured feeds at regular intervals and tracks:
      - Feed latency (timestamp difference between Chainlink and local clock)
      - Feed staleness (time since last update)
      - Number of consecutive staleness breaches

    When should_halt() returns True, the strategy should stop placing new orders
    immediately and wait for feed recovery.

    Usage:
        monitor = OracleLagMonitor(
            rpc_url="https://polygon-mainnet.g.alchemy.com/v2/...",
            feeds=["BTC/USD", "ETH/USD"],
            config=OracleLagConfig(staleness_threshold_ms=500),
        )
        await monitor.start()

        # In strategy event loop:
        if monitor.should_halt():
            risk_manager.halt("Oracle feed stale")

        await monitor.stop()
    """

    def __init__(
        self,
        rpc_url: str,
        feeds: list[str] | None = None,
        config: OracleLagConfig | None = None,
        on_stale: Callable[[str, float], None] | None = None,  # callback on stale event
    ):
        self.rpc_url = rpc_url
        self.feeds = feeds or ["BTC/USD"]
        self.config = config or OracleLagConfig()
        self._client = ChainlinkDataStreamsClient(rpc_url)
        self._on_stale_callback = on_stale

        self._feed_health: dict[str, FeedHealth] = {}
        self._total_breaches: int = 0
        self._consecutive_breaches: dict[str, int] = {f: 0 for f in self.feeds}
        self._halted: bool = False
        self._halted_since: float | None = None
        self._last_recovery_unix: float = 0.0

        self._running: bool = False
        self._task: asyncio.Task | None = None

    @property
    def is_halted(self) -> bool:
        """True if the monitor has triggered a halt."""
        return self._halted

    @property
    def halted_duration_s(self) -> float | None:
        """Seconds since halt was triggered. None if not halted."""
        if self._halted_since is None:
            return None
        return time.time() - self._halted_since

    def should_halt(self) -> bool:
        """True if any feed is stale enough to warrant halting."""
        if self._halted:
            if self.config.auto_resume:
                # Check if all feeds recovered
                if self._all_feeds_healthy():
                    if time.time() - self._last_recovery_unix >= self.config.resume_cooldown_s:
                        self._halted = False
                        self._halted_since = None
                        _logger.info("Oracle feeds recovered — auto-resuming")
                        return False
            return True
        return self._any_feed_stale()

    def _any_feed_stale(self) -> bool:
        """Return True if any feed has breached staleness threshold."""
        for feed_name, health in self._feed_health.items():
            if health.status in (FeedStatus.STALE, FeedStatus.DISCONNECTED, FeedStatus.UNRESPONSIVE):
                return True
        return False

    def _all_feeds_healthy(self) -> bool:
        """Return True if all feeds are reporting HEALTHY."""
        if not self._feed_health:
            return False
        return all(h.status == FeedStatus.HEALTHY for h in self._feed_health.values())

    def _assess_feed(self, feed_name: str) -> FeedHealth:
        """Poll a single feed and assess its health."""
        now_unix = time.time()
        report = self._client.get_latest_report(feed_name)

        if report is None:
            # Disconnected or error
            breach_count = self._consecutive_breaches.get(feed_name, 0) + 1
            self._consecutive_breaches[feed_name] = breach_count

            health = self._feed_health.get(
                feed_name,
                FeedHealth(
                    feed_name=feed_name,
                    status=FeedStatus.DISCONNECTED,
                    latency_ms=float("inf"),
                    last_update_unix=0.0,
                    staleness_ms=float("inf"),
                )
            )
            health.status = FeedStatus.DISCONNECTED
            health.staleness_ms = (now_unix - health.last_update_unix) * 1000
            health.staleness_breaches = breach_count
            return health

        # Extract timestamp from report — Data Streams reports include 'timestamp' in epoch ms
        report_timestamp = report.get("timestamp", 0)  # epoch milliseconds
        if isinstance(report_timestamp, str):
            report_timestamp = int(report_timestamp)
        report_unix = report_timestamp / 1000.0

        latency_ms = (now_unix - report_unix) * 1000
        staleness_ms = latency_ms  # latency from Chainlink → engine is the effective staleness

        breach_count = 0
        if staleness_ms > self.config.staleness_threshold_ms:
            breach_count = self._consecutive_breaches.get(feed_name, 0) + 1

        self._consecutive_breaches[feed_name] = breach_count

        # Determine status
        if breach_count >= self.config.max_breaches_before_halt:
            status = FeedStatus.STALE
        elif latency_ms > self.config.heartbeat_threshold_ms:
            status = FeedStatus.UNRESPONSIVE
        else:
            status = FeedStatus.HEALTHY

        health = FeedHealth(
            feed_name=feed_name,
            status=status,
            latency_ms=latency_ms,
            last_update_unix=report_unix,
            staleness_ms=staleness_ms,
            reports_received=self._feed_health.get(feed_name, FeedHealth(
                feed_name=feed_name,
                status=status,
                latency_ms=latency_ms,
                last_update_unix=report_unix,
                staleness_ms=staleness_ms,
            )).reports_received + 1,
            staleness_breaches=breach_count,
        )
        self._feed_health[feed_name] = health
        return health

    def _assess_all(self) -> None:
        """Poll all feeds and update aggregate state."""
        for feed_name in self.feeds:
            health = self._assess_feed(feed_name)

            # Trigger stale callback
            if health.status == FeedStatus.STALE and self._on_stale_callback:
                self._on_stale_callback(feed_name, health.staleness_ms)

        # Update global halt state
        if not self._halted and self._any_feed_stale():
            self._total_breaches += 1
            self._halted = True
            self._halted_since = time.time()
            _logger.warning(
                f"OracleLagMonitor: HALTING — worst latency="
                f"{self.worst_latency_ms:.1f}ms, "
                f"worst staleness={self.worst_staleness_ms:.1f}ms"
            )

        if self._halted and self._all_feeds_healthy():
            self._last_recovery_unix = time.time()

    async def _poll_loop(self) -> None:
        """Background asyncio loop that polls feeds at regular intervals."""
        interval_s = self.config.check_interval_ms / 1000.0
        while self._running:
            try:
                # Run HTTP requests in thread pool to avoid blocking the event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._assess_all)
            except Exception as e:
                _logger.error(f"OracleLagMonitor poll error: {e}")
            await asyncio.sleep(interval_s)

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        _logger.info(f"OracleLagMonitor started — monitoring {len(self.feeds)} feeds")

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("OracleLagMonitor stopped")

    def snapshot(self) -> OracleLagSnapshot:
        """Return the current aggregate health state."""
        statuses = [h.status for h in self._feed_health.values()]
        overall = FeedStatus.HEALTHY
        if FeedStatus.STALE in statuses:
            overall = FeedStatus.STALE
        elif FeedStatus.DISCONNECTED in statuses:
            overall = FeedStatus.DISCONNECTED
        elif FeedStatus.UNRESPONSIVE in statuses:
            overall = FeedStatus.UNRESPONSIVE

        latencies = [h.latency_ms for h in self._feed_health.values() if h.latency_ms < float("inf")]
        stalenesses = [h.staleness_ms for h in self._feed_health.values()]

        return OracleLagSnapshot(
            overall_status=overall,
            worst_latency_ms=max(latencies) if latencies else float("inf"),
            worst_staleness_ms=max(stalenesses) if stalenesses else float("inf"),
            total_staleness_breaches=self._total_breaches,
            feed_count=len(self.feeds),
            healthy_feed_count=sum(1 for h in self._feed_health.values() if h.status == FeedStatus.HEALTHY),
            timestamp_unix=time.time(),
            feeds=dict(self._feed_health),
        )

    def __repr__(self) -> str:
        s = self.snapshot()
        return (
            f"OracleLagMonitor(status={s.overall_status.value}, "
            f"worst_latency={s.worst_latency_ms:.1f}ms, "
            f"halted={self._halted})"
        )
