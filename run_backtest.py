"""Backtest runner for the 5-minute BTC prediction strategy.

Downloads historical 1-minute candles from Binance Futures, feeds them
through the BacktestEngine with the same SignalGenerationStrategy used
in live trading, and prints performance statistics.

Usage:
    uv run python run_backtest.py --days 7
    uv run python run_backtest.py --days 30
"""

import argparse
import csv
import logging
import sys
import time as time_mod
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import sqrt
from pathlib import Path

import requests

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.config import StrategyConfig

from src.mock_executor import MockExecutor
from src.risk import RiskManager
from src.strategy import SignalGenerationStrategy

log = logging.getLogger(__name__)

# ── Data Download ──────────────────────────────────────────────────

BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
BAR_TYPE_STR = "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"


def download_candles(days: int, cache_path: Path) -> Path:
    """Download 1-min BTCUSDT-PERP candles from Binance, cache to CSV."""
    if cache_path.exists():
        print(f"Using cached data: {cache_path}")
        return cache_path

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_candles: list[list] = []
    cursor_ms = start_ms

    print(f"Downloading {days} days of BTCUSDT 1-min candles...")
    while cursor_ms < end_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": 1500,
        }
        resp = requests.get(BINANCE_FUTURES_URL, params=params, timeout=30)
        resp.raise_for_status()
        klines = resp.json()
        if not klines:
            break

        all_candles.extend(klines)
        cursor_ms = klines[-1][6] + 1  # past close_time
        print(f"  {len(all_candles)} candles...", end="\r")
        time_mod.sleep(0.25)

    print(f"\n  Downloaded {len(all_candles)} candles")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
        for k in all_candles:
            writer.writerow([k[0], k[1], k[2], k[3], k[4], k[5]])

    print(f"  Saved to {cache_path}")
    return cache_path


def load_bars_from_csv(csv_path: Path) -> list[Bar]:
    """Parse CSV into NautilusTrader Bar objects."""
    bar_type = BarType.from_str(BAR_TYPE_STR)
    bars: list[Bar] = []

    # TestInstrumentProvider.btcusdt_perp_binance() has price_precision=1
    # and size_precision=3, so we must match those.
    def fmt_price(val: str) -> str:
        return f"{float(val):.1f}"

    def fmt_qty(val: str) -> str:
        return f"{float(val):.3f}"

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_ns = int(row["timestamp_ms"]) * 1_000_000  # ms → ns
            bar = Bar(
                bar_type=bar_type,
                open=Price.from_str(fmt_price(row["open"])),
                high=Price.from_str(fmt_price(row["high"])),
                low=Price.from_str(fmt_price(row["low"])),
                close=Price.from_str(fmt_price(row["close"])),
                volume=Quantity.from_str(fmt_qty(row["volume"])),
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
            bars.append(bar)

    print(f"  Loaded {len(bars)} bars from {csv_path}")
    return bars


# ── Backtest Engine ────────────────────────────────────────────────


def run_backtest(bars: list[Bar], starting_balance: float = 100_000.0) -> dict:
    """Run the backtest and return performance stats."""

    # 1. Create engine
    config = BacktestEngineConfig(
        trader_id="BACKTESTER-001",
        logging=LoggingConfig(log_level="WARNING"),
    )
    engine = BacktestEngine(config=config)

    # 2. Add venue
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(starting_balance, USDT)],
        base_currency=USDT,
    )

    # 3. Add instrument
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    engine.add_instrument(instrument)

    # 4. Add bar data
    engine.add_data(bars)

    # 5. Create strategy with mock executor and risk manager
    mock_executor = MockExecutor(default_midpoint=Decimal("0.50"))
    risk_manager = RiskManager()
    risk_manager.on_daily_open(Decimal(str(starting_balance)))

    strategy_config = StrategyConfig(strategy_id="BT-001")
    strategy = SignalGenerationStrategy(
        config=strategy_config,
        executor=mock_executor,
        polymarket_token_id="BACKTEST-BTC-5M",
        risk_manager=risk_manager,
    )

    engine.add_strategy(strategy)

    # 6. Run
    print("\nRunning backtest...")
    t0 = time_mod.time()
    engine.run()
    elapsed = time_mod.time() - t0

    # 7. Collect results
    fills = mock_executor.fills
    pnl_summary = mock_executor.pnl_summary()

    # Compute strategy-level stats
    stats = compute_stats(fills, bars, elapsed)
    stats.update(pnl_summary)

    engine.dispose()
    return stats


# ── Performance Analysis ───────────────────────────────────────────


def compute_stats(fills, bars: list[Bar], elapsed: float) -> dict:
    """Compute performance statistics from backtest results."""
    total_bars = len(bars)
    total_fills = len(fills)

    if total_bars > 0:
        first_ts = bars[0].ts_event / 1_000_000_000
        last_ts = bars[-1].ts_event / 1_000_000_000
        duration_hours = (last_ts - first_ts) / 3600
    else:
        duration_hours = 0

    # Price range
    if bars:
        prices = [float(b.close) for b in bars]
        price_start = prices[0]
        price_end = prices[-1]
        price_min = min(prices)
        price_max = max(prices)
        btc_return_pct = ((price_end - price_start) / price_start) * 100
    else:
        price_start = price_end = price_min = price_max = btc_return_pct = 0

    # Signal frequency
    signals_per_hour = (total_fills / duration_hours) if duration_hours > 0 else 0

    return {
        "elapsed_seconds": round(elapsed, 2),
        "total_bars": total_bars,
        "duration_hours": round(duration_hours, 1),
        "btc_price_start": round(price_start, 2),
        "btc_price_end": round(price_end, 2),
        "btc_price_min": round(price_min, 2),
        "btc_price_max": round(price_max, 2),
        "btc_return_pct": round(btc_return_pct, 2),
        "signals_per_hour": round(signals_per_hour, 2),
    }


def print_report(stats: dict) -> None:
    """Print a formatted backtest report."""
    print("\n" + "=" * 60)
    print("  BACKTEST REPORT")
    print("=" * 60)

    print(f"\n  Duration:          {stats['duration_hours']} hours")
    print(f"  Bars processed:    {stats['total_bars']}")
    print(f"  Elapsed time:      {stats['elapsed_seconds']}s")

    print(f"\n  BTC Price Range:")
    print(f"    Start:           ${stats['btc_price_start']:,.2f}")
    print(f"    End:             ${stats['btc_price_end']:,.2f}")
    print(f"    Min:             ${stats['btc_price_min']:,.2f}")
    print(f"    Max:             ${stats['btc_price_max']:,.2f}")
    print(f"    Return:          {stats['btc_return_pct']:+.2f}%")

    print(f"\n  Trading Activity:")
    print(f"    Total trades:    {stats['total_trades']}")
    print(f"    Buy orders:      {stats['buy_count']}")
    print(f"    Sell orders:     {stats['sell_count']}")
    print(f"    Signals/hour:    {stats['signals_per_hour']}")

    print(f"\n  Exposure:")
    print(f"    Buy exposure:    {stats['buy_exposure']:.4f}")
    print(f"    Sell exposure:   {stats['sell_exposure']:.4f}")
    if stats["buy_count"] > 0:
        print(f"    Avg buy price:   {stats['avg_buy_price']:.4f}")
    if stats["sell_count"] > 0:
        print(f"    Avg sell price:  {stats['avg_sell_price']:.4f}")

    print("\n" + "=" * 60)

    if stats["total_trades"] == 0:
        print("\n  NOTE: No trades were generated. This is expected when")
        print("  backtesting with bar data only (no OrderBookDepth10 or")
        print("  TradeTick data). The OBI and CVD signals remain at 0,")
        print("  so the composite signal rarely exceeds the threshold.")
        print("  In live trading, these streams provide the primary signals.")
        print("\n  To see trade activity, consider lowering signal thresholds")
        print("  or adding synthetic tick data generation.")


# ── Main ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Backtest 5-min BTC strategy")
    parser.add_argument("--days", type=int, default=7, help="Days of historical data")
    parser.add_argument(
        "--balance", type=float, default=100_000.0, help="Starting balance (USDT)"
    )
    args = parser.parse_args()

    cache_path = Path(f"data/btcusdt_1m_{args.days}d.csv")

    # Step 1: Download data
    download_candles(args.days, cache_path)

    # Step 2: Load bars
    bars = load_bars_from_csv(cache_path)
    if not bars:
        print("Error: No bar data loaded")
        sys.exit(1)

    # Step 3: Run backtest
    stats = run_backtest(bars, starting_balance=args.balance)

    # Step 4: Print report
    print_report(stats)


if __name__ == "__main__":
    main()
